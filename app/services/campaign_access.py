"""Who may see, use and change a Campaign.

One module, and every campaign surface asks it the same question. That is the
point: before this existed the answer was "anybody with a session", spread
implicitly across four routers and about forty handlers, and the only way to
audit it was to read all of them.

The rules
---------
* **ADMIN sees, creates and changes every campaign**, and is the only role that
  can grant or revoke somebody else's access. Administrators are the deployment's
  operators; withholding a campaign from them would create work nobody could
  unblock.
* **USER sees a campaign they created** (``Campaign.created_by_user_id``) **or a
  campaign explicitly assigned to them** (a ``CampaignUserAssignment`` row), and
  nothing else. Both are durable data written by a deliberate act.
* **Everything else is refused**, including reading, editing, enrolling into,
  importing into, executing, and any programmatic call that names the campaign.

Three properties worth stating because they are the ones a reviewer should try
to break:

**It is server-side, and it is not a filter on a template.** Hiding a campaign
from a list is a courtesy. What actually refuses is
:func:`require_campaign_access`, called with the campaign id from the URL or the
form body before the handler does anything with it. A USER who types another
team's campaign id into the address bar, or POSTs to it with a valid session and
a valid CSRF token, is refused there.

**It fails closed.** The scoping helpers return a *restrictive* expression for
every caller who is not an administrator, and an actor with no resolvable user
id and no administrator role — which is what a malformed or partially resolved
request looks like — matches nothing at all. Adding a new campaign route and
forgetting to scope it is still a bug, but forgetting to *resolve* the actor is
not: the failure direction is refusal.

**It is inert where there are no accounts.** Local development and the whole
test suite run with ``AUTH__ENABLED`` off. There is then no account directory,
no role and no user id, and the entire application is already unauthenticated —
so :class:`CampaignActor` reports ``enforced=False`` and every check passes,
exactly as ``require_admin`` next door does. This is the same trade the admin
dependency already made, and it is what keeps three thousand existing tests
meaningful.

The extension
-------------
A capture credential proves an *installation*, not a person: it carries no user
id and no role. Account linking for the extension is being built on a separate
branch, so this module deliberately does not guess. :func:`actor_from_request`
resolves such a request to :data:`UNIDENTIFIED_EXTENSION` — an actor that is not
an administrator and owns nothing — and the two extension-reachable campaign
surfaces name that case explicitly rather than falling through it:

* ``GET /api/campaigns`` keeps today's behaviour for a credential with no linked
  user, because narrowing it to nothing would break the shipped extension, and
  calls :func:`scope_campaign_statement` the moment one is present.
* Filing a capture into a campaign calls :func:`may_access_campaign`, which
  refuses as soon as a user is resolvable and is not entitled to it.

When the account-linking branch lands it has one thing to do here: put the
resolved user id and role into the request scope the way the session middleware
already does. Every rule above then applies to the extension with no further
change.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from sqlalchemy import Select, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignUserAssignment
from app.models.enums import UserRole, UserState
from app.models.user import User
from app.services.audit import record_audit_event


class CampaignAccessError(Exception):
    """Raised when a caller may not use the campaign they named.

    Rendered as a 403 by the application's handler, with one message for every
    cause. A 403 rather than a 404 on purpose, and the reasoning is the same one
    ``AdminRequiredError`` records: campaign names are unique and administered,
    so pretending the campaign does not exist would not hide much, and it would
    send an operator who genuinely needs access to look for a broken link
    instead of asking for the assignment they are missing.
    """

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message
            or "You do not have access to this campaign. An administrator can assign it to you."
        )


class CampaignAssignmentError(Exception):
    """Raised when an assignment cannot be made, with a reason to show."""


@dataclass(frozen=True)
class CampaignActor:
    """The identity a campaign decision is made for.

    Built from the request scope rather than from the session cookie, for the
    reason ``app/core/auth/admin.py`` gives about roles: the middleware resolves
    the account record on every authenticated request, so a demotion or a removed
    assignment applies to the very next request rather than when a twelve-hour
    cookie expires.
    """

    user_id: uuid.UUID | None
    role: UserRole | None
    #: Whether this deployment has an account directory at all. ``False`` means
    #: hosted authentication is switched off, so there is nobody to be.
    enforced: bool = True

    @property
    def is_admin(self) -> bool:
        """Whether this actor may reach every campaign.

        ``True`` when authorization is not enforced, because the whole
        application is then open and a campaign rule cannot be the one thing
        holding a line nothing else holds.
        """

        return not self.enforced or self.role is UserRole.ADMIN

    @property
    def is_identified(self) -> bool:
        return self.user_id is not None


#: An enforced actor with no identity: an extension capture credential that has
#: not yet been linked to an account, or any authenticated request whose account
#: could not be resolved. Not an administrator and owns nothing, so every
#: restrictive rule below refuses it.
UNIDENTIFIED_EXTENSION = CampaignActor(user_id=None, role=None, enforced=True)

#: The actor used where the deployment has no account directory.
UNENFORCED = CampaignActor(user_id=None, role=None, enforced=False)


def actor_from_request(request: Request) -> CampaignActor:
    """Resolve the campaign actor for one request, from the request scope only."""

    state: dict[str, Any] = request.scope.get("state") or {}
    if not state.get("auth_enforced"):
        return UNENFORCED

    raw_role = state.get("operator_role")
    role: UserRole | None = None
    if isinstance(raw_role, str):
        try:
            role = UserRole(raw_role)
        except ValueError:  # pragma: no cover - middleware only writes valid values
            role = None

    raw_user = state.get("operator_user_id")
    user_id: uuid.UUID | None = None
    if isinstance(raw_user, str):
        try:
            user_id = uuid.UUID(raw_user)
        except ValueError:  # pragma: no cover - middleware only writes valid values
            user_id = None

    return CampaignActor(user_id=user_id, role=role, enforced=True)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _assigned_campaign_ids_subquery(user_id: uuid.UUID) -> Select[tuple[uuid.UUID]]:
    return select(CampaignUserAssignment.campaign_id).where(
        CampaignUserAssignment.user_id == user_id
    )


def campaign_visibility_clause(actor: CampaignActor) -> Any | None:
    """A WHERE clause restricting ``Campaign`` rows to what ``actor`` may see.

    ``None`` means "no restriction" and is returned **only** for an
    administrator or an unenforced deployment. Every other caller gets a real
    expression, and an actor with no user id gets one that matches nothing —
    which is why forgetting to identify a caller refuses rather than grants.
    """

    if actor.is_admin:
        return None
    if actor.user_id is None:
        # `false()` would be clearer to read but produces a constant SQLAlchemy
        # cannot always fold into a composed statement the same way; comparing
        # the primary key to NULL is never true and composes everywhere.
        return Campaign.id.is_(None)
    return or_(
        Campaign.created_by_user_id == actor.user_id,
        Campaign.id.in_(_assigned_campaign_ids_subquery(actor.user_id)),
    )


def scope_campaign_statement(statement: Select[Any], actor: CampaignActor) -> Select[Any]:
    """Apply :func:`campaign_visibility_clause` to a statement selecting Campaigns."""

    clause = campaign_visibility_clause(actor)
    if clause is None:
        return statement
    return statement.where(clause)


def accessible_campaign_ids(session: Session, actor: CampaignActor) -> frozenset[uuid.UUID] | None:
    """The campaign ids ``actor`` may reach, or ``None`` meaning "all of them".

    ``None`` is not "none": it is the administrator answer, and callers that
    filter an unrelated table by campaign use it to skip the filter entirely
    rather than materialising every id.
    """

    if actor.is_admin:
        return None
    if actor.user_id is None:
        return frozenset()
    rows = session.scalars(scope_campaign_statement(select(Campaign.id), actor)).all()
    return frozenset(rows)


def may_access_campaign(
    session: Session, campaign_id: uuid.UUID | None, actor: CampaignActor
) -> bool:
    """Whether ``actor`` may read or use the campaign with this id.

    A campaign that does not exist is ``False`` for everyone, so a caller can use
    this as the single check and does not need a separate existence test to avoid
    a confusing refusal message.
    """

    if campaign_id is None:
        return False
    statement = select(Campaign.id).where(Campaign.id == campaign_id)
    return session.scalar(scope_campaign_statement(statement, actor)) is not None


def require_campaign_access(
    session: Session, campaign_id: uuid.UUID | None, actor: CampaignActor
) -> None:
    """Refuse unless ``actor`` may use this campaign. The server-side gate."""

    if not may_access_campaign(session, campaign_id, actor):
        raise CampaignAccessError


def visible_campaigns(session: Session, actor: CampaignActor) -> list[Campaign]:
    """Every campaign ``actor`` may see, newest first — the scoped list-all."""

    statement = select(Campaign).order_by(Campaign.created_at.desc())
    return list(session.scalars(scope_campaign_statement(statement, actor)).all())


# ---------------------------------------------------------------------------
# Assignment (administrator only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssignedUser:
    """One person on a campaign, as the admin screens need to show them."""

    user_id: uuid.UUID
    email: str
    display_name: str
    role: UserRole
    state: UserState
    assigned_at: Any
    assigned_by_email: str | None
    #: ``True`` when this person created the campaign rather than being assigned
    #: to it. Creators are listed alongside assignees because they have access
    #: for a different reason and unassigning cannot remove it.
    is_creator: bool


def campaign_owner(session: Session, campaign: Campaign) -> User | None:
    """The account that created this campaign, or ``None`` for a historical row."""

    if campaign.created_by_user_id is None:
        return None
    return session.get(User, campaign.created_by_user_id)


def campaign_people(session: Session, campaign: Campaign) -> tuple[AssignedUser, ...]:
    """Creator first, then assignees by email — everyone who can reach it.

    Administrators are deliberately *not* listed. They reach every campaign by
    role, so listing them here would suggest an assignment that could be removed.
    """

    people: list[AssignedUser] = []
    owner = campaign_owner(session, campaign)
    if owner is not None:
        people.append(
            AssignedUser(
                user_id=owner.id,
                email=owner.email,
                display_name=owner.display_name or owner.email,
                role=owner.role,
                state=owner.state,
                assigned_at=campaign.created_at,
                assigned_by_email=None,
                is_creator=True,
            )
        )

    granter = User.__table__.alias("granter")
    rows = session.execute(
        select(CampaignUserAssignment, User, granter.c.email)
        .join(User, User.id == CampaignUserAssignment.user_id)
        .join(granter, granter.c.id == CampaignUserAssignment.assigned_by_user_id, isouter=True)
        .where(CampaignUserAssignment.campaign_id == campaign.id)
        .order_by(User.email)
    ).all()
    for assignment, user, granted_by in rows:
        if owner is not None and user.id == owner.id:
            # Assigning the creator is allowed but adds nothing; do not show the
            # same person twice.
            continue
        people.append(
            AssignedUser(
                user_id=user.id,
                email=user.email,
                display_name=user.display_name or user.email,
                role=user.role,
                state=user.state,
                assigned_at=assignment.created_at,
                assigned_by_email=granted_by,
                is_creator=False,
            )
        )
    return tuple(people)


def assignable_users(session: Session, campaign: Campaign) -> tuple[User, ...]:
    """Accounts an administrator may still assign to this campaign.

    Sourced from the ``users`` table — the existing authority — and never from a
    free-typed address, so a campaign cannot be assigned to somebody who has no
    account. Administrators and the creator are excluded because both already
    have access; disabled accounts are excluded because assigning one grants
    nothing.
    """

    assigned = set(
        session.scalars(
            select(CampaignUserAssignment.user_id).where(
                CampaignUserAssignment.campaign_id == campaign.id
            )
        ).all()
    )
    if campaign.created_by_user_id is not None:
        assigned.add(campaign.created_by_user_id)

    candidates = session.scalars(
        select(User)
        .where(User.state == UserState.ACTIVE, User.role == UserRole.USER)
        .order_by(User.email)
    ).all()
    return tuple(user for user in candidates if user.id not in assigned)


def assign_user(
    session: Session,
    *,
    campaign: Campaign,
    user_id: uuid.UUID,
    actor: CampaignActor,
    actor_label: str,
) -> CampaignUserAssignment:
    """Grant one account access to one campaign. Administrator only.

    Idempotent: assigning somebody who is already assigned returns the existing
    row rather than raising, because the operator's intent is satisfied either
    way and a duplicate-submitted form should not produce an error page.
    """

    _require_admin_actor(actor)
    user = session.get(User, user_id)
    if user is None:
        raise CampaignAssignmentError("That account no longer exists.")
    if user.state is not UserState.ACTIVE:
        raise CampaignAssignmentError(
            "That account is disabled. Re-enable it before assigning campaigns to it."
        )

    existing = session.scalar(
        select(CampaignUserAssignment).where(
            CampaignUserAssignment.campaign_id == campaign.id,
            CampaignUserAssignment.user_id == user_id,
        )
    )
    if existing is not None:
        return existing

    assignment = CampaignUserAssignment(
        campaign_id=campaign.id,
        user_id=user_id,
        assigned_by_user_id=actor.user_id,
    )
    session.add(assignment)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:
        # Two administrators assigning the same person at the same time. The
        # unique constraint decided; read the winner rather than failing.
        session.expunge(assignment)
        winner = session.scalar(
            select(CampaignUserAssignment).where(
                CampaignUserAssignment.campaign_id == campaign.id,
                CampaignUserAssignment.user_id == user_id,
            )
        )
        if winner is None:  # pragma: no cover - defensive
            raise
        return winner

    record_audit_event(
        session,
        actor=actor_label,
        action="campaign.user_assigned",
        entity_type="campaign",
        entity_id=str(campaign.id),
        new_state="assigned",
        reason=f"{user.email} may now use this campaign",
        context={"user_id": str(user_id), "user_email": user.email},
    )
    return assignment


def unassign_user(
    session: Session,
    *,
    campaign: Campaign,
    user_id: uuid.UUID,
    actor: CampaignActor,
    actor_label: str,
) -> bool:
    """Revoke one account's assignment. Administrator only.

    Returns whether a row was removed. Removing an assignment does not remove
    creator access: if the person created the campaign they keep it, and the
    admin screen says so rather than letting the button look like it failed.
    """

    _require_admin_actor(actor)
    assignment = session.scalar(
        select(CampaignUserAssignment).where(
            CampaignUserAssignment.campaign_id == campaign.id,
            CampaignUserAssignment.user_id == user_id,
        )
    )
    if assignment is None:
        return False

    user = session.get(User, user_id)
    session.delete(assignment)
    session.flush()
    record_audit_event(
        session,
        actor=actor_label,
        action="campaign.user_unassigned",
        entity_type="campaign",
        entity_id=str(campaign.id),
        previous_state="assigned",
        new_state="revoked",
        reason=f"{user.email if user else user_id} may no longer use this campaign",
        context={"user_id": str(user_id), "user_email": user.email if user else None},
    )
    return True


def assignments_for_user(session: Session, user_id: uuid.UUID) -> Iterable[uuid.UUID]:
    """Campaign ids assigned to one account. Used by the account screens."""

    return session.scalars(_assigned_campaign_ids_subquery(user_id)).all()


def _require_admin_actor(actor: CampaignActor) -> None:
    if not actor.is_admin:
        raise CampaignAccessError(
            "Only a platform administrator can change who a campaign is assigned to."
        )

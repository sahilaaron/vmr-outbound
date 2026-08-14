"""Shell context for the customer-facing interface.

Two things live here and nowhere else: the navigation model the design specifies,
and the "what wants you" counts the navigation badges carry. Both are computed from
committed state — a badge that over-reports would send the operator looking for
work that is not there, so every count has a query behind it.

Every count is also *authorization-scoped*, which is a stronger claim than
"computed from committed state" and the one that was missing. A badge reading
110 to a USER whose campaign list is empty is not merely a wrong number: it
tells that operator 110 decisions are theirs to make, and every one of them is
refused the moment they follow the link. See :func:`attention_counts`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.auth.context import current_operator
from app.core.config import Settings
from app.models.campaign import CampaignContact
from app.models.company import Company
from app.models.company_domain_resolution import CompanyDomainResolution
from app.models.enums import (
    CampaignContactEligibility,
    DomainResolutionState,
    PipelineStageStatus,
)
from app.services import campaign_access, drafts, identity
from app.services.resolution import service as resolution_service
from app.services.seller import profile as seller_profile

#: Nav items whose section the design names, in the design's order.
NAV_MODEL: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
    (
        "Work",
        (
            ("today", "Today", "/app"),
            ("campaigns", "Campaigns", "/app/campaigns"),
            ("review", "Review", "/app/review"),
        ),
    ),
    (
        "Records",
        (
            ("contacts", "Contacts", "/app/contacts"),
            ("companies", "Companies", "/app/companies"),
        ),
    ),
    (
        "Setup",
        (("knowledge", "Knowledge Base", "/app/knowledge"),),
    ),
)


@dataclass(frozen=True)
class NavItem:
    key: str
    label: str
    href: str
    badge: int | None = None
    badge_tone: str = ""


@dataclass(frozen=True)
class NavGroup:
    title: str
    items: tuple[NavItem, ...]


@dataclass(frozen=True)
class AttentionCounts:
    """Everything that is genuinely waiting on a person.

    Each field is one query against committed state. They are kept separate rather
    than summed into a single "needs attention" number, because the operator's next
    action is different for each and a single total hides which.
    """

    drafts_awaiting: int = 0
    ambiguous_imports: int = 0
    unresolved_domains: int = 0
    blocked_contacts: int = 0
    failed_stages: int = 0
    companies_without_domain: int = 0

    @property
    def total(self) -> int:
        return (
            self.drafts_awaiting
            + self.ambiguous_imports
            + self.unresolved_domains
            + self.blocked_contacts
            + self.failed_stages
        )


def _safe(fn: Any, default: int = 0) -> int:
    """A badge must never take a page down.

    A count that cannot be read (a missing table on a half-migrated database, a
    dropped connection) renders as zero rather than raising: the page's job is to
    show the operator what is happening, and it cannot do that from a stack trace.
    """

    try:
        return int(fn())
    except Exception:
        return default


def attention_counts(
    session: Session,
    *,
    campaign_id: uuid.UUID | None = None,
    actor: campaign_access.CampaignActor | None = None,
) -> AttentionCounts:
    """Count the things that need a human, scoped to one campaign or to all.

    Two different restrictions, and keeping them apart is the whole of this
    signature. ``campaign_id`` is the *filter* a page chose. ``actor`` is the
    *authorization*, and it is what stops this function reporting a deployment's
    work as one operator's.

    ``actor=None`` means unrestricted and is what a caller that has already
    established its own authority passes — the per-campaign loops below the
    campaign list, where ``require_campaign_access`` or a scoped
    ``list_campaigns`` already decided the operator may see that campaign. Every
    caller rendering a *global* number passes the request's real actor.

    For a restricted actor the campaign-derived counts are narrowed to the
    campaigns they created or were assigned, and an empty set yields zero rather
    than everything — the direction a set-membership filter has to fail in.

    The three counts that are not campaign work at all — ambiguous identity
    reviews, unresolved captures, companies with no domain — are queues over
    permanent records shared by the whole deployment. They are reported to an
    administrator and withheld from a restricted actor, not because a USER may
    not do that work (they may; ``/review`` and the capture pages stay
    reachable) but because this number is rendered as "decisions only you can
    make", and a shared backlog is not that. Withholding under-reports; the
    alternative over-claimed, which is the failure being repaired.
    """

    restricted_to: frozenset[uuid.UUID] | None = None
    if actor is not None:
        restricted_to = campaign_access.accessible_campaign_ids(session, actor)

    def _scope_membership(statement: Any) -> Any:
        if restricted_to is not None:
            statement = statement.where(CampaignContact.campaign_id.in_(restricted_to))
        if campaign_id is not None:
            statement = statement.where(CampaignContact.campaign_id == campaign_id)
        return statement

    def _drafts() -> int:
        return drafts.queue_counts(
            session, campaign_id=campaign_id, campaign_ids=restricted_to
        ).awaiting

    def _ambiguous() -> int:
        if campaign_id is not None or restricted_to is not None:
            return 0
        return identity.count_open_reviews(session)

    def _unresolved() -> int:
        if campaign_id is not None or restricted_to is not None:
            return 0
        return len(resolution_service.unresolved_captures(session, limit=500))

    def _blocked() -> int:
        statement = _scope_membership(
            select(func.count(CampaignContact.id)).where(
                CampaignContact.eligibility_status == CampaignContactEligibility.BLOCKED
            )
        )
        return session.scalar(statement) or 0

    def _failed() -> int:
        statement = _scope_membership(
            select(func.count(CampaignContact.id)).where(
                CampaignContact.pipeline_status.in_(
                    (PipelineStageStatus.FAILED, PipelineStageStatus.BLOCKED)
                )
            )
        )
        return session.scalar(statement) or 0

    def _no_domain() -> int:
        if restricted_to is not None:
            return 0
        return session.scalar(select(func.count(Company.id)).where(Company.domain.is_(None))) or 0

    return AttentionCounts(
        drafts_awaiting=_safe(_drafts),
        ambiguous_imports=_safe(_ambiguous),
        unresolved_domains=_safe(_unresolved),
        blocked_contacts=_safe(_blocked),
        failed_stages=_safe(_failed),
        companies_without_domain=_safe(_no_domain),
    )


def provisional_domain_count(session: Session) -> int:
    """Companies running on a domain the policy only accepted provisionally.

    Counted directly against the current decision rows: provisional and unresolved
    are different states with different consequences, and one must never be
    reported as the other.
    """

    def _count() -> int:
        return (
            session.scalar(
                select(func.count(CompanyDomainResolution.id)).where(
                    CompanyDomainResolution.is_current.is_(True),
                    CompanyDomainResolution.state == DomainResolutionState.PROVISIONAL,
                )
            )
            or 0
        )

    return _safe(_count)


def nav_groups(counts: AttentionCounts) -> tuple[NavGroup, ...]:
    """The design's three nav groups, with live badges on Today and Review."""

    badges: dict[str, tuple[int, str]] = {}
    if counts.total:
        badges["today"] = (counts.total, "")
    if counts.drafts_awaiting:
        badges["review"] = (counts.drafts_awaiting, "quiet")

    groups: list[NavGroup] = []
    for title, items in NAV_MODEL:
        built: list[NavItem] = []
        for key, label, href in items:
            badge = badges.get(key)
            built.append(
                NavItem(
                    key=key,
                    label=label,
                    href=href,
                    badge=badge[0] if badge else None,
                    badge_tone=badge[1] if badge else "",
                )
            )
        groups.append(NavGroup(title=title, items=tuple(built)))
    return tuple(groups)


def operator_identity(session: Session, settings: Settings) -> tuple[str, str, str]:
    """What the account chip says.

    Since the user-accounts slice there *is* a person to name on a hosted
    deployment, so the chip names them: the display name on their account, or
    their address when the account has no name, with the address underneath.

    Local development still has no session, and the old behaviour is kept exactly
    for it — the chip carries the seller identity from the Knowledge Base and says
    plainly that nobody is signed in. Inventing an operator name there would make
    an unauthenticated environment look authenticated, which is the one thing this
    chip must never do.
    """

    operator = current_operator()
    if operator is not None:
        signed_in = operator.display_name or operator.email
        parts = [word for word in signed_in.replace("-", " ").replace("@", " ").split() if word]
        marks = "".join(word[0] for word in parts[:2]).upper() or "VM"
        return signed_in, operator.email, marks

    name = "Operator"

    def _profile_name() -> str:
        profile = seller_profile.get_profile(session)
        return profile.name if profile is not None and profile.name else ""

    try:
        entered = _profile_name()
    except Exception:
        entered = ""
    if entered:
        name = entered

    context = f"{settings.app_env.upper()} · no sign-in in this environment"
    words = [word for word in name.replace("-", " ").split() if word]
    initials = "".join(word[0] for word in words[:2]).upper() or "VM"
    return name, context, initials

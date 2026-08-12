"""Everything that reads or changes a VMR user account.

One module so that there is exactly one place where an account is created, one
place where it is disabled, and one place where a password is written — and so
that every one of them records the same audit event without the caller having to
remember to.

The rules that are enforced *here* rather than in a route, because a route is
easy to add and easy to forget:

* An account is created with no usable password. Never a temporary one.
* Disabling, reactivating, changing a role and setting a password all bump
  ``auth_version``, which is what invalidates sessions already in browsers.
* Google linking is by ``sub`` and only ever onto an account that was already
  resolved by address. An unknown Google identity creates nothing.
* Every audit event carries the actor, the account and the transition, and none
  of them carries a password, a hash, or a raw link.

Transaction boundaries belong to the caller. Every function flushes so that ids
and timestamps are populated, and none of them commits — the web layer's
``get_db`` dependency commits on success, so an audit event and the change it
describes land together or not at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.auth.accounts import AccountSnapshot, snapshot_of
from app.core.auth.config import normalize_operator_email
from app.core.auth.passwords import (
    PasswordPolicyError,
    dummy_verify,
    hash_password,
    needs_rehash,
    validate_password,
    verify_password,
)
from app.models.enums import UserCredentialTokenPurpose, UserRole, UserState
from app.models.user import User
from app.services.audit import record_audit_event
from app.services.users import tokens as token_service

#: Audit action names. Constants rather than literals so a rename is one edit and
#: so a typo cannot silently create a second, unqueryable action.
ACTION_USER_CREATED = "user.created"
ACTION_USER_DISABLED = "user.disabled"
ACTION_USER_REACTIVATED = "user.reactivated"
ACTION_USER_ROLE_CHANGED = "user.role_changed"
ACTION_TOKEN_ISSUED = "user.credential_link_issued"
ACTION_PASSWORD_SETUP_COMPLETED = "user.password_setup_completed"
ACTION_PASSWORD_RESET_COMPLETED = "user.password_reset_completed"
ACTION_GOOGLE_LINKED = "user.google_identity_linked"
ACTION_BOOTSTRAP_ADMIN = "user.bootstrap_admin_ensured"
ACTION_SEEDED_FROM_ALLOWLIST = "user.seeded_from_configuration_allowlist"

#: The actor recorded for something the system did on its own behalf.
SYSTEM_ACTOR = "system:bootstrap"

_ENTITY_TYPE = "user"

#: The column width for both address columns, and therefore the limit the service
#: enforces so that an over-long value is a refusal rather than a driver error.
MAX_EMAIL_CHARS = 320


class UserServiceError(Exception):
    """Raised when a requested account change cannot be made.

    The message is written for an administrator reading it on the users screen.
    It never reveals anything an unauthenticated caller could not already learn,
    because only an administrator can reach the routes that raise it.
    """


@dataclass(frozen=True)
class LoginOutcome:
    """The result of one password login attempt.

    ``snapshot`` is populated only on success. Everything else is a refusal, and
    the *reason* is deliberately not exposed to the caller's response — it exists
    so the server can log and count accurately while the page says one thing.
    """

    snapshot: AccountSnapshot | None
    reason: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.snapshot is not None


# --- lookups -----------------------------------------------------------------


def get_by_email(session: Session, email: str) -> User | None:
    """Resolve an account by the one comparable form of its address."""

    normalized = normalize_operator_email(email)
    if not normalized:
        return None
    return session.scalar(select(User).where(User.email_normalized == normalized))


def get_by_google_subject(session: Session, subject: str) -> User | None:
    """Resolve an account by Google's stable identifier, when already linked."""

    if not subject:
        return None
    return session.scalar(select(User).where(User.google_subject == subject))


def get_by_id(session: Session, user_id: uuid.UUID) -> User | None:
    return session.get(User, user_id)


def list_users(session: Session) -> list[User]:
    """Every account, administrators first, then by address.

    Ordering by role before name puts the accounts that can change other accounts
    at the top of the screen, which is the list an administrator actually audits.
    """

    return list(
        session.scalars(select(User).order_by(User.role.asc(), User.email_normalized.asc())).all()
    )


def count_admins(session: Session, *, active_only: bool = True) -> int:
    statement = select(func.count(User.id)).where(User.role == UserRole.ADMIN)
    if active_only:
        statement = statement.where(User.state == UserState.ACTIVE)
    return int(session.scalar(statement) or 0)


# --- administration ----------------------------------------------------------


def create_user(
    session: Session,
    *,
    email: str,
    display_name: str | None,
    role: UserRole = UserRole.USER,
    actor: str,
    dry_run: bool = False,
) -> User:
    """Create one account with no usable password.

    "No usable password" is the whole point of this function. There is no
    temporary password to leak, to be reused on another service, or to be found
    in a chat log six months later; the account simply cannot authenticate with a
    password until its holder sets one through a one-time link.
    """

    normalized = normalize_operator_email(email)
    if not normalized:
        raise UserServiceError(
            "Enter a well-formed email address. Non-ASCII addresses are not supported."
        )
    if len(normalized) > MAX_EMAIL_CHARS or len(email.strip()) > MAX_EMAIL_CHARS:
        # Checked here rather than left to the column width. The form's
        # `maxlength` is advice a client may ignore, and a 400-character address
        # reaching `flush()` raises a driver error rather than `UserServiceError`,
        # which escapes the admin screen's handling and becomes a 500.
        raise UserServiceError(f"An email address may be at most {MAX_EMAIL_CHARS} characters.")
    if get_by_email(session, normalized) is not None:
        raise UserServiceError("An account with that email address already exists.")

    user = User(
        email=email.strip(),
        email_normalized=normalized,
        display_name=(display_name or "").strip() or None,
        role=role,
        state=UserState.ACTIVE,
        password_hash=None,
        auth_version=1,
        created_by=normalize_operator_email(actor) or None,
    )
    session.add(user)
    session.flush()

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_USER_CREATED,
        entity_type=_ENTITY_TYPE,
        entity_id=str(user.id),
        previous_state=None,
        new_state=UserState.ACTIVE.value,
        reason="Administrator created a hosted account.",
        context={"email": normalized, "role": role.value, "password_state": "not_set"},
        dry_run=dry_run,
    )
    return user


def set_state(
    session: Session,
    *,
    user: User,
    state: UserState,
    actor: str,
    dry_run: bool = False,
) -> User:
    """Disable or reactivate an account, invalidating its sessions either way.

    Reactivation bumps ``auth_version`` as well as disabling, which is not
    symmetry for its own sake: without it, the sessions that were revoked when
    the account was disabled would start working again the moment it was
    reactivated. "Reactivate" must mean "this person may sign in again", never
    "the browser tab they left open is live again".
    """

    if user.state == state:
        return user
    if state == UserState.DISABLED and user.role == UserRole.ADMIN and count_admins(session) <= 1:
        raise UserServiceError(
            "This is the only active administrator. Promote another account "
            "to administrator before disabling this one."
        )

    previous = user.state
    user.state = state
    user.auth_version += 1
    session.flush()

    record_audit_event(
        session,
        actor=actor,
        action=(ACTION_USER_DISABLED if state == UserState.DISABLED else ACTION_USER_REACTIVATED),
        entity_type=_ENTITY_TYPE,
        entity_id=str(user.id),
        previous_state=previous.value,
        new_state=state.value,
        reason="Administrator changed account state; existing sessions invalidated.",
        context={"email": user.email_normalized, "auth_version": user.auth_version},
        dry_run=dry_run,
    )
    return user


def set_role(
    session: Session,
    *,
    user: User,
    role: UserRole,
    actor: str,
    dry_run: bool = False,
) -> User:
    """Change a role, invalidating existing sessions.

    Sessions are invalidated on a *demotion* for the obvious reason and on a
    promotion for a less obvious one: role is read from the account record on
    every request, so an unbumped session would silently gain the new privilege
    mid-flight. Making both paths mint a fresh session keeps "when did this
    person become an administrator" answerable from the audit trail alone.
    """

    if user.role == role:
        return user
    if role == UserRole.USER and user.role == UserRole.ADMIN and count_admins(session) <= 1:
        raise UserServiceError(
            "This is the only active administrator. Promote another account "
            "to administrator before removing this role."
        )

    previous = user.role
    user.role = role
    user.auth_version += 1
    session.flush()

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_USER_ROLE_CHANGED,
        entity_type=_ENTITY_TYPE,
        entity_id=str(user.id),
        previous_state=previous.value,
        new_state=role.value,
        reason="Administrator changed account role; existing sessions invalidated.",
        context={"email": user.email_normalized, "auth_version": user.auth_version},
        dry_run=dry_run,
    )
    return user


def issue_credential_link(
    session: Session,
    *,
    user: User,
    actor: str,
    lifetime: timedelta = token_service.DEFAULT_TOKEN_LIFETIME,
    dry_run: bool = False,
) -> token_service.IssuedToken:
    """Issue exactly one live password-setup or reset link for ``user``.

    The purpose is derived from the account rather than chosen by the caller: an
    account with no password is being set up, one with a password is being reset,
    and letting a route decide would make the audit trail a matter of which
    button somebody clicked.
    """

    if user.state != UserState.ACTIVE:
        raise UserServiceError(
            "Reactivate this account before issuing a password link. A link "
            "issued to a disabled account would not work."
        )

    purpose = (
        UserCredentialTokenPurpose.RESET
        if user.has_password
        else UserCredentialTokenPurpose.INITIAL_SETUP
    )
    issued = token_service.issue_token(
        session,
        user=user,
        purpose=purpose,
        issued_by=normalize_operator_email(actor) or None,
        lifetime=lifetime,
    )

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_TOKEN_ISSUED,
        entity_type=_ENTITY_TYPE,
        entity_id=str(user.id),
        previous_state=None,
        new_state=purpose.value,
        reason="Administrator issued a one-time password link; earlier links invalidated.",
        # No raw token, no digest, no URL. The expiry and the purpose are what an
        # audit reader needs; the secret is what an audit reader must never have.
        context={
            "email": user.email_normalized,
            "purpose": purpose.value,
            "expires_at": issued.expires_at.isoformat(),
        },
        dry_run=dry_run,
    )
    return issued


# --- password setup and login ------------------------------------------------


def complete_password_setup(
    session: Session,
    *,
    raw_token: str,
    new_password: str,
    now: datetime | None = None,
    dry_run: bool = False,
) -> User:
    """Consume a one-time link and store the account's password hash.

    Ordering matters and is deliberate: the token is resolved first, the policy
    is applied second, and the write plus the consume happen together. A password
    that fails the policy therefore does **not** burn the link — otherwise a
    person who typed a fourteen-character password would need an administrator to
    issue a new one before they could try again.
    """

    moment = now or datetime.now(UTC)
    row, user = token_service.resolve_token(session, raw_token, now=moment)

    # Raises PasswordPolicyError, which the route renders on the form. The link
    # is still live at this point, which is the whole reason validation runs
    # after resolution rather than before it.
    validated = validate_password(new_password, email=user.email_normalized)

    was_reset = user.has_password
    user.password_hash = hash_password(validated)
    user.password_set_at = moment
    # Every earlier session for this account stops working. A password change is
    # the canonical "somebody may have had my session" event.
    user.auth_version += 1
    token_service.consume_token(session, row, now=moment)
    # Any *other* outstanding link is invalidated too: setting a password must
    # not leave a second link that could set it again.
    token_service.supersede_outstanding(session, user_id=user.id, now=moment)
    session.flush()

    record_audit_event(
        session,
        actor=f"user:{user.email_normalized}",
        action=(ACTION_PASSWORD_RESET_COMPLETED if was_reset else ACTION_PASSWORD_SETUP_COMPLETED),
        entity_type=_ENTITY_TYPE,
        entity_id=str(user.id),
        previous_state="set" if was_reset else "not_set",
        new_state="set",
        reason="Password set through a one-time link; earlier sessions invalidated.",
        context={"email": user.email_normalized, "auth_version": user.auth_version},
        dry_run=dry_run,
    )
    return user


def authenticate_password(
    session: Session, *, email: str, password: str, now: datetime | None = None
) -> LoginOutcome:
    """Verify an email/password pair. One outward shape for every failure.

    Four distinct refusals happen here — no such account, no password set,
    disabled account, wrong password — and the caller is given a
    :class:`LoginOutcome` whose ``reason`` exists only for server-side logging.
    The route renders the same message for all four, and this function spends the
    same Argon2id work on all four, so neither the text nor the timing tells an
    attacker which addresses are real.
    """

    moment = now or datetime.now(UTC)
    user = get_by_email(session, email)

    if user is None:
        dummy_verify()
        return LoginOutcome(None, reason="unknown_account")
    if not user.has_password:
        dummy_verify()
        return LoginOutcome(None, reason="password_not_set")
    if user.state != UserState.ACTIVE:
        dummy_verify()
        return LoginOutcome(None, reason="account_disabled")
    if not verify_password(user.password_hash, password):
        return LoginOutcome(None, reason="wrong_password")

    # Opportunistic upgrade: if the cost parameters have been raised since this
    # hash was written, rewrite it now while the plaintext is legitimately in
    # hand. It does not bump `auth_version` — the password did not change, so
    # signing everybody out would be a gratuitous logout on an ordinary login.
    stored = user.password_hash or ""
    if stored and needs_rehash(stored):
        user.password_hash = hash_password(password)

    user.last_login_at = moment
    session.flush()
    return LoginOutcome(snapshot_of(user))


def resolve_google_identity(
    session: Session,
    *,
    email: str,
    subject: str,
    display_name: str,
    now: datetime | None = None,
    dry_run: bool = False,
) -> User | None:
    """Resolve a validated Google assertion onto an existing account, or ``None``.

    This function is where "Google proves identity, VMR grants access" is
    actually implemented, and it deliberately never creates anything.

    Resolution order, and why:

    1. **By ``sub``, if already linked.** Google's subject is stable across a
       Workspace address rename, so an account that was linked under an old
       address keeps working under the new one.
    2. **By address, otherwise** — and on success the ``sub`` is recorded, which
       is the link. From then on rule 1 applies.

    Two shapes are refused rather than resolved, because each would create a
    second identity for one person:

    * A ``sub`` that is already linked to a *different* account than the one the
      address resolves to. That is either a Google account being reused or a
      configuration mistake, and guessing between them is how duplicate users are
      born.
    * An account that already carries a *different* ``sub``. The stable
      identifier does not change for a given Google account, so a new one on a
      known address means the address was reissued to a different person.
    """

    moment = now or datetime.now(UTC)
    normalized = normalize_operator_email(email)
    if not normalized or not subject:
        return None

    linked = get_by_google_subject(session, subject)
    by_address = get_by_email(session, normalized)

    if linked is not None:
        if by_address is not None and by_address.id != linked.id:
            # The address and the subject point at two different accounts.
            return None
        user = linked
    else:
        if by_address is None:
            # An unknown Google identity. No account is created: there is no
            # public signup, and "Google authenticated them" is not authorization.
            return None
        if by_address.google_subject and by_address.google_subject != subject:
            return None
        if by_address.state != UserState.ACTIVE:
            # Checked *before* linking, not after. Linking first and refusing
            # afterwards still commits the link, so a disabled account would
            # permanently acquire whichever Google subject happened to present a
            # matching address — and because a *different* subject is refused
            # above, that would lock the real owner out of the Google path for
            # good once the account was reactivated.
            return None
        user = by_address
        user.google_subject = subject
        user.google_linked_at = moment
        session.flush()
        record_audit_event(
            session,
            actor=f"user:{user.email_normalized}",
            action=ACTION_GOOGLE_LINKED,
            entity_type=_ENTITY_TYPE,
            entity_id=str(user.id),
            previous_state="unlinked",
            new_state="linked",
            reason="First successful Google sign-in linked a stable provider subject.",
            # The subject is an opaque provider identifier, not a secret, and
            # having it in the audit trail is what makes a future "why did this
            # account stop matching" answerable.
            context={"email": user.email_normalized, "google_subject": subject},
            dry_run=dry_run,
        )

    if user.state != UserState.ACTIVE:
        return None

    if display_name and not user.display_name:
        # Fill a name in only where the administrator left one blank. Google is
        # a convenience here, not an authority on what an account is called.
        user.display_name = display_name[:200]
    user.last_login_at = moment
    session.flush()
    return user


def stamp_login(session: Session, *, user: User, now: datetime | None = None) -> None:
    user.last_login_at = now or datetime.now(UTC)
    session.flush()


# --- bootstrap ---------------------------------------------------------------


def ensure_bootstrap_admin(session: Session, *, email: str, dry_run: bool = False) -> User | None:
    """Guarantee exactly one named administrator exists. Idempotent.

    Three cases, and the third is the one worth being careful about:

    * No account for the address — create it as ADMIN with no password.
    * An account exists with the ADMIN role — do nothing at all. In particular do
      not reactivate a disabled administrator: disabling is an explicit act and a
      restart must not undo it.
    * An account exists with the USER role — promote it and record the promotion.
      This is the path a deployment takes when the address was already in the
      configuration allow-list and was seeded as an ordinary user.

    What this never does is infer an administrator from an email domain, create a
    password, or touch any account other than the configured one.
    """

    normalized = normalize_operator_email(email)
    if not normalized:
        return None

    existing = get_by_email(session, normalized)
    if existing is None:
        user = User(
            email=normalized,
            email_normalized=normalized,
            display_name=None,
            role=UserRole.ADMIN,
            state=UserState.ACTIVE,
            password_hash=None,
            auth_version=1,
            created_by=None,
        )
        session.add(user)
        session.flush()
        record_audit_event(
            session,
            actor=SYSTEM_ACTOR,
            action=ACTION_BOOTSTRAP_ADMIN,
            entity_type=_ENTITY_TYPE,
            entity_id=str(user.id),
            previous_state=None,
            new_state=UserRole.ADMIN.value,
            reason="Bootstrapped the configured platform administrator.",
            context={"email": normalized, "created": True},
            dry_run=dry_run,
        )
        return user

    if existing.role == UserRole.ADMIN:
        return existing

    previous = existing.role
    existing.role = UserRole.ADMIN
    existing.auth_version += 1
    session.flush()
    record_audit_event(
        session,
        actor=SYSTEM_ACTOR,
        action=ACTION_BOOTSTRAP_ADMIN,
        entity_type=_ENTITY_TYPE,
        entity_id=str(existing.id),
        previous_state=previous.value,
        new_state=UserRole.ADMIN.value,
        reason="Promoted the configured platform administrator on bootstrap.",
        context={"email": normalized, "created": False},
        dry_run=dry_run,
    )
    return existing


def seed_from_allowlist(
    session: Session, *, emails: tuple[str, ...], dry_run: bool = False
) -> list[User]:
    """Give every configured legacy allow-list address an ordinary account.

    This exists for exactly one moment: the first start after the accounts
    migration, so that turning on the accounts model does not lock out the people
    who could sign in the day before.

    It is therefore a **one-shot**, not a per-start reconciliation, and the guard
    below is what makes that true. Re-running it on every boot would quietly undo
    an administrator's decisions: an account deleted from the database by a human
    operator in an emergency would come back — active, and able to sign in with
    Google immediately — at the next restart, purely because the address was
    still sitting in an environment variable nobody had thought about since the
    migration.

    "Nothing has been seeded yet" is read as "the directory holds only accounts
    this function did not create", which in practice means: nothing, or only the
    bootstrap administrator that ran moments earlier in the same startup. It never
    grants ADMIN — the bootstrap administrator is the only source of that role.
    """

    existing = list_users(session)
    seedable = {normalize_operator_email(entry) for entry in emails} - {""}
    unexplained = {
        user.email_normalized
        for user in existing
        if user.role != UserRole.ADMIN and user.email_normalized not in seedable
    }
    if unexplained:
        # The directory already holds an ordinary account that this list does not
        # explain, so somebody has been administering it. The migration moment has
        # passed; leave it alone.
        return []

    created: list[User] = []
    for entry in emails:
        normalized = normalize_operator_email(entry)
        if not normalized or get_by_email(session, normalized) is not None:
            continue
        user = User(
            email=normalized,
            email_normalized=normalized,
            display_name=None,
            role=UserRole.USER,
            state=UserState.ACTIVE,
            password_hash=None,
            auth_version=1,
            created_by=None,
        )
        session.add(user)
        session.flush()
        record_audit_event(
            session,
            actor=SYSTEM_ACTOR,
            action=ACTION_SEEDED_FROM_ALLOWLIST,
            entity_type=_ENTITY_TYPE,
            entity_id=str(user.id),
            previous_state=None,
            new_state=UserState.ACTIVE.value,
            reason=(
                "Materialised a pre-existing configuration allow-list entry as an "
                "account so that hosted access was not interrupted."
            ),
            context={"email": normalized, "role": UserRole.USER.value},
            dry_run=dry_run,
        )
        created.append(user)
    return created


__all__ = [
    "ACTION_BOOTSTRAP_ADMIN",
    "ACTION_GOOGLE_LINKED",
    "ACTION_PASSWORD_RESET_COMPLETED",
    "ACTION_PASSWORD_SETUP_COMPLETED",
    "ACTION_SEEDED_FROM_ALLOWLIST",
    "ACTION_TOKEN_ISSUED",
    "ACTION_USER_CREATED",
    "ACTION_USER_DISABLED",
    "ACTION_USER_REACTIVATED",
    "ACTION_USER_ROLE_CHANGED",
    "LoginOutcome",
    "PasswordPolicyError",
    "UserServiceError",
    "authenticate_password",
    "complete_password_setup",
    "count_admins",
    "create_user",
    "ensure_bootstrap_admin",
    "get_by_email",
    "get_by_google_subject",
    "get_by_id",
    "issue_credential_link",
    "list_users",
    "resolve_google_identity",
    "seed_from_allowlist",
    "set_role",
    "set_state",
    "stamp_login",
]

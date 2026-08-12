"""Gmail mailbox grants: connecting, reading, refreshing and disconnecting.

One operator has at most one connected mailbox, enforced by a partial unique
index rather than by a convention any code path could forget. Connecting a
second mailbox replaces the first: the previous grant is marked ``REVOKED`` and
kept, because it is the record of what was authorized and when, and because the
Gmail drafts created while it was live still point at it.

Every function here is transaction-neutral -- it flushes and never commits, so
the caller decides what one operator action means. That matters most on the
callback route, where binding a mailbox and consuming the authorization
transaction have to be one atomic outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth.config import normalize_operator_email
from app.core.gmail_config import GMAIL_COMPOSE_SCOPE, GmailSettings
from app.models.enums import GmailGrantStatus
from app.models.gmail import GmailMailboxGrant
from app.services.gmail.oauth import GmailAuthorizationError, GmailOAuthClient, GmailTokenGrant
from app.services.gmail.tokens import GmailTokenStorageError, decrypt_token, encrypt_token

#: How long before a stored access token expires it is treated as already
#: expired. A token that has ninety seconds left will not survive the round trip
#: it is about to be used for, and refreshing early costs one request.
ACCESS_TOKEN_REFRESH_MARGIN_SECONDS = 120


class GmailMailboxError(RuntimeError):
    """A mailbox grant cannot be established, read or used as asked."""


@dataclass(frozen=True)
class MailboxState:
    """What the operator surface needs to know, and nothing a token could leak."""

    #: ``"unavailable"`` (feature off or not configured), ``"disconnected"``,
    #: ``"connected"`` or ``"reconnect_required"``.
    state: str
    mailbox_address: str = ""
    granted_scopes: tuple[str, ...] = ()
    connected_at: datetime | None = None
    #: A bounded category, never a provider message.
    last_error_category: str | None = None

    @property
    def connected(self) -> bool:
        return self.state == "connected"

    @property
    def needs_reconnect(self) -> bool:
        return self.state == "reconnect_required"

    @property
    def available(self) -> bool:
        """Whether a mailbox could be connected in this deployment at all."""

        return self.state != "unavailable"


UNAVAILABLE = MailboxState(state="unavailable")
DISCONNECTED = MailboxState(state="disconnected")


def connected_grant(session: Session, *, operator_subject: str) -> GmailMailboxGrant | None:
    """The one live grant for this operator, or ``None``."""

    if not operator_subject:
        return None
    return session.scalars(
        select(GmailMailboxGrant).where(
            GmailMailboxGrant.operator_subject == operator_subject,
            GmailMailboxGrant.status == GmailGrantStatus.CONNECTED,
        )
    ).first()


def latest_grant(session: Session, *, operator_subject: str) -> GmailMailboxGrant | None:
    """The most recent grant of any status, so a reconnect state can be shown.

    Without this, an operator whose refresh token was revoked at Google would
    see "no mailbox connected" -- true in one sense and unhelpful in every
    other, because it does not say that something they had set up has stopped
    working.
    """

    if not operator_subject:
        return None
    return session.scalars(
        select(GmailMailboxGrant)
        .where(GmailMailboxGrant.operator_subject == operator_subject)
        .order_by(GmailMailboxGrant.connected_at.desc(), GmailMailboxGrant.id.desc())
        .limit(1)
    ).first()


def mailbox_state(
    session: Session, *, operator_subject: str, settings: GmailSettings, feature_on: bool
) -> MailboxState:
    """The operator-visible mailbox state for one signed-in operator."""

    if not feature_on or not settings.is_configured():
        return UNAVAILABLE
    grant = latest_grant(session, operator_subject=operator_subject)
    if grant is None:
        return DISCONNECTED
    if grant.status is GmailGrantStatus.CONNECTED:
        return MailboxState(
            state="connected",
            mailbox_address=grant.mailbox_address,
            granted_scopes=tuple(grant.granted_scopes.split()),
            connected_at=grant.connected_at,
        )
    if grant.status is GmailGrantStatus.RECONNECT_REQUIRED:
        return MailboxState(
            state="reconnect_required",
            mailbox_address=grant.mailbox_address,
            connected_at=grant.connected_at,
            last_error_category=grant.last_error_category,
        )
    return DISCONNECTED


def bind_mailbox(
    session: Session,
    *,
    operator_subject: str,
    operator_email: str,
    mailbox_address: str,
    mailbox_account_subject: str,
    grant: GmailTokenGrant,
    settings: GmailSettings,
) -> GmailMailboxGrant:
    """Record one verified mailbox authorization against one operator.

    The operator identity is passed in from the *session* the callback was
    served under, never from a request parameter. That is what makes it
    impossible for a callback replayed into another browser to bind a mailbox to
    somebody else: the route checks the transaction cookie's operator subject
    against the signed-in operator before this function is reached, and this
    function has no way to learn an operator identity from the OAuth response.
    """

    if not grant.has_compose_scope():
        raise GmailMailboxError(
            "That Google account did not grant permission to create drafts, so no mailbox "
            "was connected. Connect again and leave the draft permission ticked."
        )
    if grant.refresh_token is None:
        raise GmailMailboxError(
            "Google returned no durable authorization, so no mailbox was connected."
        )
    address = normalize_operator_email(mailbox_address)
    if not address:
        raise GmailMailboxError("Google did not return a usable mailbox address.")
    if not mailbox_account_subject.strip():
        raise GmailMailboxError("Google did not identify the mailbox account.")

    now = datetime.now(UTC)
    # Retire whatever this operator had. Kept, never deleted: the drafts created
    # under it still name it, and the record of what was authorized is the point
    # of an audit trail.
    for existing in session.scalars(
        select(GmailMailboxGrant).where(
            GmailMailboxGrant.operator_subject == operator_subject,
            GmailMailboxGrant.status == GmailGrantStatus.CONNECTED,
        )
    ).all():
        existing.status = GmailGrantStatus.REVOKED
        existing.disconnected_at = now
        existing.encrypted_refresh_token = None
        existing.encrypted_access_token = None
        existing.access_token_expires_at = None
    session.flush()

    try:
        encrypted_refresh = encrypt_token(grant.refresh_token, settings=settings)
        encrypted_access = encrypt_token(grant.access_token, settings=settings)
    except GmailTokenStorageError as exc:
        raise GmailMailboxError(str(exc)) from exc

    row = GmailMailboxGrant(
        operator_subject=operator_subject,
        operator_email=operator_email,
        mailbox_account_subject=mailbox_account_subject.strip(),
        mailbox_address=address,
        granted_scopes=" ".join(grant.granted_scopes)[:1024],
        status=GmailGrantStatus.CONNECTED,
        encrypted_refresh_token=encrypted_refresh,
        encrypted_access_token=encrypted_access,
        access_token_expires_at=now + timedelta(seconds=grant.expires_in),
        connected_at=now,
    )
    session.add(row)
    session.flush()
    return row


def disconnect(
    session: Session,
    *,
    operator_subject: str,
    settings: GmailSettings,
    client: GmailOAuthClient | None,
) -> tuple[bool, bool]:
    """Forget this operator's mailbox authorization.

    Returns ``(disconnected, revoked_at_google)``. The two are separate because
    they can genuinely differ: VMR always forgets the token, and telling Google
    to invalidate it is best effort. Reversing that order -- refusing to forget
    a token because a revocation request failed -- would leave a decryptable
    refresh token in the database for the sake of a request that may never
    succeed.
    """

    grant = connected_grant(session, operator_subject=operator_subject)
    if grant is None:
        return False, False

    revoked = False
    if client is not None and grant.encrypted_refresh_token:
        try:
            client.revoke(token=decrypt_token(grant.encrypted_refresh_token, settings=settings))
            revoked = True
        except (GmailAuthorizationError, GmailTokenStorageError):
            # Best effort, and never fatal. The local state below is what this
            # application controls and it is cleared regardless.
            revoked = False

    grant.status = GmailGrantStatus.REVOKED
    grant.disconnected_at = datetime.now(UTC)
    grant.encrypted_refresh_token = None
    grant.encrypted_access_token = None
    grant.access_token_expires_at = None
    grant.last_error_category = None
    session.flush()
    return True, revoked


def mark_reconnect_required(session: Session, *, grant: GmailMailboxGrant, category: str) -> None:
    """Move a grant to the recoverable "sign in again" state.

    The stored tokens are dropped at the same moment. A refresh token Google has
    rejected is not a credential any more, and keeping ciphertext that cannot be
    used is a secret at rest with no purpose.
    """

    grant.status = GmailGrantStatus.RECONNECT_REQUIRED
    grant.last_error_category = category[:64]
    grant.encrypted_refresh_token = None
    grant.encrypted_access_token = None
    grant.access_token_expires_at = None
    grant.disconnected_at = datetime.now(UTC)
    session.flush()


def access_token_for(
    session: Session,
    *,
    grant: GmailMailboxGrant,
    settings: GmailSettings,
    client: GmailOAuthClient,
    now: datetime | None = None,
) -> str:
    """A usable access token for ``grant``, refreshing it when necessary.

    Raises :class:`GmailMailboxError` when the grant can no longer be used, and
    moves the row to ``RECONNECT_REQUIRED`` first, so the operator surface shows
    a recoverable state rather than the same failure on every click.
    """

    moment = now or datetime.now(UTC)
    if grant.status is not GmailGrantStatus.CONNECTED:
        raise GmailMailboxError("This Gmail mailbox is no longer connected.")

    expires_at = grant.access_token_expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    fresh_enough = expires_at is not None and expires_at > moment + timedelta(
        seconds=ACCESS_TOKEN_REFRESH_MARGIN_SECONDS
    )
    if fresh_enough and grant.encrypted_access_token:
        try:
            return decrypt_token(grant.encrypted_access_token, settings=settings)
        except GmailTokenStorageError:
            # An unreadable stored token is not fatal on its own: the refresh
            # token below can mint another. Falling through is strictly better
            # than failing the operator's action over a decryption problem that
            # a refresh would fix.
            pass

    try:
        refresh_token = decrypt_token(grant.encrypted_refresh_token, settings=settings)
    except GmailTokenStorageError as exc:
        mark_reconnect_required(session, grant=grant, category="token_unreadable")
        raise GmailMailboxError(
            "This Gmail mailbox's stored authorization cannot be read. Connect Gmail again."
        ) from exc

    try:
        refreshed = client.refresh(refresh_token=refresh_token)
    except GmailAuthorizationError as exc:
        mark_reconnect_required(session, grant=grant, category="invalid_grant")
        raise GmailMailboxError(
            "Google no longer accepts this Gmail authorization. Connect Gmail again."
        ) from exc

    try:
        grant.encrypted_access_token = encrypt_token(refreshed.access_token, settings=settings)
        if refreshed.refresh_token:
            grant.encrypted_refresh_token = encrypt_token(
                refreshed.refresh_token, settings=settings
            )
    except GmailTokenStorageError as exc:
        raise GmailMailboxError(str(exc)) from exc
    grant.access_token_expires_at = moment + timedelta(seconds=refreshed.expires_in)
    grant.last_refreshed_at = moment
    session.flush()
    return refreshed.access_token


def scopes_are_sufficient(grant: GmailMailboxGrant) -> bool:
    """Whether the *granted* scopes still permit creating a draft."""

    return GMAIL_COMPOSE_SCOPE in grant.granted_scopes.split()

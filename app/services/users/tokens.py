"""One-time password-setup and password-reset links.

The whole security of the first-login flow is in this file, so every rule it
enforces is written down next to the code that enforces it.

**The secret exists once.** ``issue_token`` returns the raw value to its caller
and stores only ``sha256(raw)``. Nothing else in the application can produce the
raw value again: it is not in the database, not in a log line, not in the audit
event, and not in any response after the one that created it. An administrator
who loses the link issues a new one, which is a two-second action and is exactly
what the "issue a new link" control is for.

**A digest, not a password hash.** The stored value is a plain SHA-256 rather
than Argon2id, and that is correct rather than an oversight. A password is a
low-entropy human choice and needs a slow function to make guessing expensive. A
token is 256 bits from ``secrets.token_urlsafe`` — there is nothing to guess, so
a slow function would buy nothing and would put 40ms on every presented link.

**Four independent ways to fail, all checked.** Replayed (``consumed_at`` set),
expired (``expires_at`` passed), superseded (``superseded_at`` set, because a
newer link was issued), and belonging to a disabled account. Each has its own
column or its own lookup, so none can be satisfied by accident, and the caller
receives one indistinguishable outcome for all four.

**Issuing supersedes.** Every issue marks the account's outstanding links
superseded in the same transaction. Two live links for one account would mean an
administrator who reissued after a suspected leak had not actually revoked
anything.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import UserCredentialTokenPurpose, UserState
from app.models.user import User, UserCredentialToken

#: How long an issued link works for. Twenty-four hours is the target in the
#: issue and is what an administrator sending a link over Slack or a phone call
#: needs; it is short enough that a link forgotten in a chat log stops working
#: the next day.
DEFAULT_TOKEN_LIFETIME = timedelta(hours=24)

#: 32 bytes of entropy, URL-safe. The resulting string is 43 characters and is
#: safe to put in a path segment without escaping.
_TOKEN_ENTROPY_BYTES = 32


class CredentialTokenError(Exception):
    """Raised when a presented link cannot be used.

    Deliberately one exception with one message for every cause. An expired
    link, a replayed link, a superseded link, a link for a disabled account and
    a link that never existed must be indistinguishable to whoever is holding
    it — otherwise the page becomes an oracle for which accounts exist and which
    administrators have recently reset a password.
    """

    def __init__(self) -> None:
        super().__init__("This link is no longer valid.")


@dataclass(frozen=True)
class IssuedToken:
    """The one and only time the raw secret is available.

    Returned to the caller and then deliberately not retained anywhere. The
    ``__repr__`` is overridden because a dataclass would otherwise print the
    secret into a traceback the first time something unrelated raised while this
    object was on the stack.
    """

    user_id: uuid.UUID
    raw_token: str
    expires_at: datetime
    purpose: UserCredentialTokenPurpose

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"IssuedToken(user_id={self.user_id!r}, purpose={self.purpose!r}, "
            f"expires_at={self.expires_at!r}, raw_token=<redacted>)"
        )


def digest_token(raw: str) -> str:
    """The stored form of a presented token.

    Hex-encoded so the column is a fixed-width ASCII string that indexes and
    compares predictably regardless of database collation.
    """

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_token() -> str:
    """A fresh, unguessable one-time secret."""

    return secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)


def supersede_outstanding(
    session: Session, *, user_id: uuid.UUID, now: datetime | None = None
) -> int:
    """Mark every live link for ``user_id`` superseded. Returns how many.

    "Live" means neither consumed nor already superseded. Expired links are
    swept too: they are already refused, and leaving them unmarked would make
    "how many links did this reset invalidate" an unanswerable question.
    """

    moment = now or datetime.now(UTC)
    live = list(
        session.scalars(
            select(UserCredentialToken).where(
                UserCredentialToken.user_id == user_id,
                UserCredentialToken.consumed_at.is_(None),
                UserCredentialToken.superseded_at.is_(None),
            )
        ).all()
    )
    # Loaded and updated through the ORM rather than as a bulk `UPDATE`, because
    # a bulk statement leaves any of these rows that are already in the identity
    # map holding stale values — and `complete_password_setup` calls this
    # immediately after consuming one of them.
    for row in live:
        row.superseded_at = moment
    if live:
        session.flush()
    return len(live)


def issue_token(
    session: Session,
    *,
    user: User,
    purpose: UserCredentialTokenPurpose,
    issued_by: str | None,
    lifetime: timedelta = DEFAULT_TOKEN_LIFETIME,
    now: datetime | None = None,
) -> IssuedToken:
    """Supersede any outstanding link and mint exactly one new one."""

    moment = now or datetime.now(UTC)
    supersede_outstanding(session, user_id=user.id, now=moment)

    raw = generate_token()
    expires_at = moment + lifetime
    session.add(
        UserCredentialToken(
            user_id=user.id,
            purpose=purpose,
            token_digest=digest_token(raw),
            expires_at=expires_at,
            issued_by=issued_by,
        )
    )
    session.flush()
    return IssuedToken(user_id=user.id, raw_token=raw, expires_at=expires_at, purpose=purpose)


def resolve_token(
    session: Session, raw: str, *, now: datetime | None = None
) -> tuple[UserCredentialToken, User]:
    """Return the live token row and its account, or raise :class:`CredentialTokenError`.

    Does **not** consume. The setup page needs to know a link is good in order to
    render the form, and consuming on ``GET`` would mean a browser prefetch or a
    link preview in a chat client silently burned somebody's only link.
    """

    moment = now or datetime.now(UTC)
    if not raw or len(raw) > 512:
        raise CredentialTokenError

    row = session.scalar(
        select(UserCredentialToken).where(UserCredentialToken.token_digest == digest_token(raw))
    )
    if row is None:
        raise CredentialTokenError
    if row.consumed_at is not None or row.superseded_at is not None:
        raise CredentialTokenError
    if _as_utc(row.expires_at) <= moment:
        raise CredentialTokenError

    user = session.get(User, row.user_id)
    if user is None or user.state != UserState.ACTIVE:
        # A disabled account's outstanding link must not work. This is the check
        # that stops "disable the account" being undone by a link the person
        # already has in their inbox.
        raise CredentialTokenError
    return row, user


def consume_token(
    session: Session, row: UserCredentialToken, *, now: datetime | None = None
) -> None:
    """Burn a link permanently.

    Called inside the same transaction as the password write, so a failure part
    way leaves the link unused rather than leaving an account with no password
    and no way to set one.
    """

    row.consumed_at = now or datetime.now(UTC)
    session.flush()


def _as_utc(value: datetime) -> datetime:
    """Compare consistently whatever the driver returned.

    The column is ``timestamptz`` so psycopg returns an aware value, but a row
    constructed in a test may carry a naive one. Treating naive as UTC rather
    than raising keeps the comparison honest in both cases.
    """

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

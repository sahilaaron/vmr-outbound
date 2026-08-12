"""The account directory: the seam between a session cookie and a user record.

The previous slice could decide authorization from configuration alone, so the
authentication boundary never touched the database and said so proudly. Issue
#270 moves the authority into the ``users`` table, and that changes the trade
deliberately rather than by accident:

* **What is gained.** Disabling an account stops an *already-issued* session on
  its next request, and a password reset invalidates sessions that are already in
  browsers. Neither is expressible in a stateless cookie, and both are explicit
  requirements — an administrator who removes somebody's access at 5pm must not
  be told to wait twelve hours or redeploy.
* **What is paid.** One indexed primary-key lookup per authenticated request, and
  a database outage now refuses authenticated requests instead of serving them.

Three things keep that cost honest:

1. **Anonymous paths never reach here.** ``/healthz``, ``/readyz``, ``/version``,
   the whole sign-in surface and the ``/static/`` mount are decided before any
   lookup, so a database outage still leaves the probes answering, the sign-in
   page rendering and the stylesheet loading. That is the specific failure the
   old design worried about, and it is still handled.
2. **A lookup failure refuses without clearing the cookie.** A refusal caused by
   an unreachable database is transient: the browser keeps its session and works
   again the moment the database does. Only a *decided* refusal — no such
   account, disabled, superseded version — clears it.
3. **It is a seam, not a hard-wired query.** ``AccountDirectory`` is a protocol.
   The application binds the database-backed implementation; a test binds a
   deterministic one and exercises the boundary with no database at all, exactly
   as ``IdentityProvider`` already works for Google.

The snapshot deliberately carries the role. Role is read from the account record
on every request rather than from the session cookie, so demoting an
administrator takes effect immediately and a stale cookie cannot assert a
privilege the directory no longer grants.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models.enums import UserRole, UserState
from app.models.user import User

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.core.auth.session import OperatorSession


class AccountLookupUnavailable(RuntimeError):
    """Raised when the directory could not be consulted at all.

    Distinct from "no such account" on purpose. This one means *unknown*, and the
    boundary answers it with a refusal that leaves the session cookie in place;
    a decided refusal clears it.
    """


@dataclass(frozen=True)
class AccountSnapshot:
    """Everything the authentication boundary needs to know about an account."""

    user_id: uuid.UUID
    email: str
    display_name: str
    role: UserRole
    state: UserState
    auth_version: int
    has_password: bool

    @property
    def is_active(self) -> bool:
        return self.state == UserState.ACTIVE

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN


def snapshot_of(user: User) -> AccountSnapshot:
    """One conversion, used by the directory and by the sign-in routes alike."""

    return AccountSnapshot(
        user_id=user.id,
        email=user.email_normalized,
        display_name=user.display_name or "",
        role=user.role,
        state=user.state,
        auth_version=user.auth_version,
        has_password=user.has_password,
    )


class AccountDirectory(Protocol):
    """The one question the authentication boundary asks about an account."""

    def lookup(self, user_id: uuid.UUID) -> AccountSnapshot | None:
        """The current state of ``user_id``, or ``None`` when no such account.

        Implementations must raise :class:`AccountLookupUnavailable` when the
        answer is unknown — a dropped connection, a migration window — and must
        never return ``None`` to mean "could not tell". The two outcomes are
        handled differently and conflating them would either strand a valid
        operator or keep a deleted one signed in.
        """


class DatabaseAccountDirectory:
    """The live directory, reading one row per authenticated request."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def lookup(self, user_id: uuid.UUID) -> AccountSnapshot | None:
        try:
            with self._session_factory() as session:
                user = session.scalar(select(User).where(User.id == user_id))
                return snapshot_of(user) if user is not None else None
        except Exception as exc:  # noqa: BLE001 - every driver error is one outcome
            # Deliberately broad. A driver raises a different exception type for a
            # dropped socket, an authentication failure and a missing table, and
            # the boundary's response to all three is identical: this request
            # cannot be decided, so refuse it and keep the session intact.
            raise AccountLookupUnavailable("the account directory is unavailable") from exc


def session_account_id(session: OperatorSession | None) -> uuid.UUID | None:
    """The durable account a signed-in session belongs to, as a ``UUID``.

    The one identifier anything downstream should key ownership on. A session
    carries ``user_id`` on both login paths; it carries a Google ``subject`` only
    on one, and ``OperatorSession`` says plainly that the subject is kept for the
    audit trail rather than for access decisions.

    ``None`` when there is no session, or when the claim is not a well-formed
    UUID. The middleware has already refused anything whose ``user_id`` did not
    resolve to an active account, so a malformed value here is not a live attack
    path -- it is returned as "no owner" so that a caller cannot accidentally
    treat an unparseable claim as a match.
    """

    if session is None:
        return None
    try:
        return uuid.UUID(session.user_id)
    except (AttributeError, TypeError, ValueError):
        return None


def default_account_directory() -> DatabaseAccountDirectory:
    """The directory bound to the application's own session factory.

    Imported lazily by the caller so that constructing settings, or importing the
    auth package, never opens a database connection as a side effect.
    """

    from app.db.session import SessionLocal

    return DatabaseAccountDirectory(SessionLocal)

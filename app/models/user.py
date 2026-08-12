"""The VMR user account and its one-time credential links.

Two tables, and the whole authorization model rests on the first of them.

``users``
    One row per person who may use this deployment. It is the *authority* on
    access: an identity provider proves who somebody is, but only a row here —
    active, and matching — lets them in. There is no public signup, so a row
    exists because an administrator created it.

``user_credential_tokens``
    One row per password-setup or password-reset link that has been issued. The
    row stores a SHA-256 digest of the secret and never the secret itself, so a
    database dump does not contain a usable link.

Why an account table at all, when the previous slice deliberately had none
---------------------------------------------------------------------------
The hosted-auth slice approved operators from a configuration list
(``AUTH__ALLOWED_OPERATOR_EMAILS``), and for two named people that was the right
shape: nothing to migrate, nothing to leak, and no application bug could grant
access. Issue #270 changes the requirement rather than the judgement. A hosted
Beta with real colleagues on Gmail and Microsoft 365 mailboxes needs accounts
that an administrator can create at 11pm without editing ``/etc/vmr/vmr.env``
and restarting the service, needs a password path for people whose Google
account is not in the VMR Workspace, and needs a disable button that takes
effect on the next request rather than on the next deploy.

Three properties are carried deliberately into the schema rather than left to
application convention:

* **``email_normalized`` is unique, not ``email``.** One person is one row. The
  displayed address keeps whatever casing was typed; the comparable form is what
  both login paths and both providers resolve against, so a Google sign-in and a
  password sign-in for the same person cannot produce two accounts.
* **``google_subject`` is unique when present.** Google's ``sub`` is the stable
  account identifier; an email address is not, because a Workspace address can
  be renamed and reissued. Linking on first successful Google sign-in and
  refusing a second account for the same ``sub`` is what stops a renamed address
  becoming a second identity.
* **``auth_version`` exists to be incremented.** Every issued session carries the
  value it was minted under. Bumping the column is what makes a disable or a
  password change invalidate sessions that are already in browsers, without a
  session table and without waiting for a 12-hour expiry.

What is deliberately *not* here: anything a password could be recovered from.
``password_hash`` holds an Argon2id PHC string and nothing else, and there is no
column, index or ``__repr__`` through which it reaches a log, a template or an
API response.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import UserCredentialTokenPurpose, UserRole, UserState


class User(Base):
    """One person who may sign in to this deployment."""

    __tablename__ = "users"
    __table_args__ = (
        # The account directory is read by comparable address on every password
        # login and every Google callback, and by `google_subject` on every
        # Google callback that has already been linked.
        Index("ix_users_email_normalized", "email_normalized", unique=True),
        Index("ix_users_google_subject", "google_subject", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    #: The address exactly as the administrator entered it. Display only.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    #: The single comparable form every lookup uses. See
    #: ``app.core.auth.config.normalize_operator_email`` — one rule, applied to
    #: the configured value, the typed value and the provider's claim alike.
    email_normalized: Mapped[str] = mapped_column(String(320), nullable=False)

    #: What the account chip shows. Optional: an administrator may create an
    #: account from an address alone, and Google fills this in on first sign-in.
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role"),
        nullable=False,
        default=UserRole.USER,
        # The member NAME, because that is the label SQLAlchemy writes for a
        # Python enum column and therefore the label the type actually has.
        server_default=UserRole.USER.name,
    )
    state: Mapped[UserState] = mapped_column(
        Enum(UserState, name="user_state"),
        nullable=False,
        default=UserState.ACTIVE,
        server_default=UserState.ACTIVE.name,
    )

    #: An Argon2id PHC string, or ``NULL`` for an account that has never
    #: completed password setup. ``NULL`` is not "empty password": every login
    #: path treats it as "this account cannot authenticate with a password",
    #: which is the state every admin-created account starts in.
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    password_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Google's stable subject identifier, recorded on first successful Google
    #: sign-in. Never taken from a request field and never used as the primary
    #: lookup for an account that has not been linked yet.
    google_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    google_linked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Incremented by every revocation event. A session carrying an older value
    #: is refused on its next request. Starts at 1 so that a session minted
    #: before any revocation is distinguishable from an unset claim.
    auth_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: The comparable address of the administrator who created this account, or
    #: ``NULL`` for the bootstrap administrator and for accounts materialised
    #: from the pre-existing configuration allow-list. Deliberately a string
    #: rather than a self-referencing foreign key: an account must be deletable
    #: from the database by a human operator in an emergency without a cascade
    #: rewriting somebody else's provenance.
    created_by: Mapped[str | None] = mapped_column(String(320), nullable=True)

    @property
    def has_password(self) -> bool:
        """Whether this account can authenticate with a password today."""

        return bool(self.password_hash)

    @property
    def is_active(self) -> bool:
        return self.state == UserState.ACTIVE

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        # Neither the hash nor its presence-by-length is rendered. The one thing
        # a debug line must never make convenient is a password oracle.
        return f"User(id={self.id!r}, email={self.email_normalized!r}, role={self.role!r})"


class UserCredentialToken(Base):
    """One issued password-setup or password-reset link.

    The raw secret exists for exactly as long as it takes to render it once to
    the administrator who issued it. What survives is this row, which holds a
    digest, the account it belongs to, when it stops working, and — after use —
    when it was consumed.

    Superseding rather than deleting is deliberate. "This link was replaced at
    14:02" and "this link never existed" look identical to an attacker at the
    door, but they are very different to an administrator reading the audit
    trail three weeks later.
    """

    __tablename__ = "user_credential_tokens"
    __table_args__ = (
        # Presented tokens are looked up by digest; a user's outstanding tokens
        # are swept by user id when a new one is issued.
        Index("ix_user_credential_tokens_digest", "token_digest", unique=True),
        Index("ix_user_credential_tokens_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    purpose: Mapped[UserCredentialTokenPurpose] = mapped_column(
        Enum(UserCredentialTokenPurpose, name="user_credential_token_purpose"), nullable=False
    )

    #: SHA-256 of the raw token, hex-encoded. A digest rather than the secret so
    #: that read access to this table does not confer the ability to set
    #: somebody's password. SHA-256 is correct *here* and would be wrong for a
    #: password: the input is 256 bits of ``secrets.token_urlsafe`` entropy, so
    #: there is no guessing to slow down and no reason to pay a KDF's cost on
    #: every presented link.
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Set when the link was used to set a password. A consumed link is refused
    #: on every later presentation — this is what makes replay fail.
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Set when a newer link for the same account replaced this one.
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Comparable address of the administrator who issued the link.
    issued_by: Mapped[str | None] = mapped_column(String(320), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"UserCredentialToken(id={self.id!r}, user_id={self.user_id!r}, "
            f"purpose={self.purpose!r}, consumed={self.consumed_at is not None})"
        )

"""Account-linked extension authorization: the link, and the code that mints it.

Two tables, both short-lived by design, both owned by a ``users`` row.

``extension_sessions``
    One live row per (account, extension install). It *is* the authorization: a
    presented ``vmre1`` access token names a row here, and the row names the VMR
    account the capture belongs to. There is no shared secret anywhere in this
    model — the columns hold SHA-256 digests, so a database dump, a backup or a
    settings screen contains nothing replayable.

``extension_authorization_codes``
    One row per issued PKCE authorization code, alive for sixty seconds and
    usable once. It exists so the code that travels through the browser's
    redirect — the one part of this flow an attacker can plausibly observe — is
    worthless without the ``code_verifier`` that never left the extension.

Why a table at all, when the previous credential was configuration
------------------------------------------------------------------
``EXTENSION_AUTH__CREDENTIALS`` could express "this install may capture". It
could not express *whose* capture it is, could not be revoked without a restart,
and required a human to paste a permanent shared secret into a browser. All
three are requirements now, and all three are properties of a row rather than of
a settings file:

* **Ownership.** ``user_id`` is NOT NULL and cascades from ``users``. Deleting an
  account deletes its links; disabling one is refused on the next request,
  because the middleware re-reads the owning account every time.
* **Revocation that takes effect immediately.** ``revoked_at`` is checked before
  the digest on every request, so a disconnect, a reused refresh token or an
  administrator's decision ends the link on the next call rather than at the
  next restart.
* **Rotation.** Both digests are replaced on every refresh, which is what makes a
  stolen refresh token detectable: presenting the previous secret against a live
  row can only mean the row's secret was copied, so the whole link is revoked.

The partial unique index is the "one live link per install" rule stated where it
cannot be forgotten. Postgres enforces it for rows where ``revoked_at IS NULL``,
so a history of revoked links accumulates freely while exactly one may be live.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

#: The only scope this application issues. Written as a column rather than
#: assumed so that a second scope, if one is ever wanted, is a migration and a
#: decision instead of a silent widening of what every existing row means.
EXTENSION_SCOPE_CAPTURE = "capture"


class ExtensionSession(Base):
    """One live authorization binding a VMR account to one extension install."""

    __tablename__ = "extension_sessions"
    __table_args__ = (
        # Listing an operator's own links, and the cascade path when an account
        # is removed.
        Index("ix_extension_sessions_user_id", "user_id"),
        # One live link per install per account. Partial, so revoked rows stay
        # for the audit trail and never block a fresh authorization.
        Index(
            "uq_extension_sessions_live_install",
            "user_id",
            "extension_id",
            "installation_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    #: Also the public token id: it travels in the middle segment of both tokens
    #: so the server can find one row instead of scanning digests. Non-secret on
    #: its own — the secret is the third segment, and only its digest is stored.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    #: The 32-character ``a``-``p`` Chrome extension id this link was issued to.
    #: Checked against the approved id set on every issuance and against the
    #: request's ``Origin`` on every use, so a token minted for one install is
    #: worthless when presented by another.
    extension_id: Mapped[str] = mapped_column(String(32), nullable=False)

    #: An opaque, non-secret per-install identifier the extension generates once
    #: with ``crypto.randomUUID()``. It distinguishes two browsers belonging to
    #: the same person; it authorises nothing by itself.
    installation_id: Mapped[str] = mapped_column(String(64), nullable=False)

    scope: Mapped[str] = mapped_column(
        String(32), nullable=False, default=EXTENSION_SCOPE_CAPTURE, server_default="capture"
    )

    #: SHA-256 hex of the access secret. SHA-256 rather than a password KDF for
    #: the reason ``app/core/auth/extension.py`` states: the input is 256 bits of
    #: ``secrets.token_urlsafe`` entropy, so there is no dictionary to slow down
    #: and no reason to pay a KDF's cost on every request.
    access_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    access_token_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    refresh_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    refresh_token_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    #: What the operator sees on a "connected devices" list. Descriptive only.
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Set the moment the link stops working. Checked before the digest on every
    #: request, so a revoked row can never be resurrected by a valid secret.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Why. Never rendered to the caller — a uniform refusal is what the door
    #: says — but an operator reading the audit trail needs to tell a deliberate
    #: disconnect from a detected refresh-token reuse.
    revoked_reason: Mapped[str | None] = mapped_column(String(160), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        # Neither digest is rendered. A debug line must never be a place where
        # half a credential turns up.
        return (
            f"ExtensionSession(id={self.id!r}, user_id={self.user_id!r}, "
            f"extension_id={self.extension_id!r}, revoked={self.revoked_at is not None})"
        )


class ExtensionAuthorizationCode(Base):
    """One issued PKCE authorization code: sixty seconds, single use.

    The code is the only part of this flow that travels through a redirect, so
    it is treated as observable. What makes it safe is that it is useless on its
    own: redeeming it requires the ``code_verifier`` whose SHA-256 is recorded
    here, and that value never leaves the extension's service worker.

    Presentation consumes the row whether or not the verifier matched. That is
    deliberate: a code that could be presented repeatedly would let an attacker
    holding a stolen code brute-force the verifier, and "one presentation" is the
    only rule that closes that without a rate limiter to tune.
    """

    __tablename__ = "extension_authorization_codes"
    __table_args__ = (
        # Presented codes are looked up by digest, never by the secret itself.
        Index("ix_extension_authorization_codes_hash", "code_hash", unique=True),
        Index("ix_extension_authorization_codes_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    #: SHA-256 hex of the code. The raw value exists only in the redirect the
    #: browser follows and in the extension's memory for the moment it takes to
    #: redeem it.
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    extension_id: Mapped[str] = mapped_column(String(32), nullable=False)
    installation_id: Mapped[str] = mapped_column(String(64), nullable=False)

    #: The base64url SHA-256 of the verifier, exactly as the client sent it.
    code_challenge: Mapped[str] = mapped_column(String(128), nullable=False)

    #: Recorded so the redemption cannot be for a different destination than the
    #: one the account holder was shown and consented to.
    redirect_uri: Mapped[str] = mapped_column(String(255), nullable=False)

    scope: Mapped[str] = mapped_column(
        String(32), nullable=False, default=EXTENSION_SCOPE_CAPTURE, server_default="capture"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Set on the first presentation, valid or not. A consumed code is
    #: ``invalid_grant`` forever after.
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"ExtensionAuthorizationCode(id={self.id!r}, user_id={self.user_id!r}, "
            f"consumed={self.consumed_at is not None})"
        )

"""Gmail mailbox grants and Gmail draft lineage (#267).

Two tables, and the split between them is the same idea the sequence model
already rests on: *authority* and *artefact* are different things with different
lifetimes.

``gmail_mailbox_grants``
    One row per mailbox an operator has authorized. It carries the encrypted
    OAuth tokens, the Gmail account identity the grant was verified against, and
    the scopes Google actually returned. Disconnecting or losing the refresh
    token changes this row's status; it never deletes the drafts that were
    created while it was live.

``gmail_draft_records``
    One row per (mailbox account, exact message version) VMR has tried to draft.
    It names the Campaign Contact, the sequence, the logical message and the
    **exact immutable message version** the draft was built from -- never a
    ``(contact, position)`` pair, which would silently follow an edit onto text
    nobody drafted.

Why the idempotency key is the Gmail *account*, not the grant row
-----------------------------------------------------------------
``uq_gmail_draft_records_account_version`` is unique on
``(mailbox_account_subject, message_version_id)``. Keying on
``mailbox_grant_id`` instead would let a disconnect-and-reconnect cycle -- which
writes a new grant row for the same Google account -- create a second draft of
identical text in the same human's Drafts folder. The account subject is
Google's stable identifier for the mailbox and survives that cycle, which is
what makes "clicking twice does not duplicate" true across reconnects as well as
across refreshes.

An edit *does* legitimately produce a second draft, because an edit produces a
new ``message_version_id``. That is the replacement policy, stated as a
constraint rather than as a convention: historical lineage rows are never
rewritten, and the new version gets its own row and its own Gmail draft.

**No token, code, or client secret is stored anywhere but the two encrypted
columns on the grant.** Both are Fernet ciphertext (see
``app/services/gmail/tokens.py``), the key lives in the environment rather than
in the database, and ``__repr__`` on the grant is overridden so that a debugger,
a traceback frame or a naive log line cannot print one.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import GmailDraftStatus, GmailGrantStatus


class GmailMailboxGrant(Base):
    """One operator's authorization to create drafts in one Gmail mailbox."""

    __tablename__ = "gmail_mailbox_grants"
    __table_args__ = (
        # At most one live mailbox per operator. A disconnected or revoked row
        # stays for audit -- it is the record of what was authorized and when --
        # but only one row at a time can answer "which mailbox do drafts go to".
        Index(
            "uq_gmail_mailbox_grants_connected",
            "operator_subject",
            unique=True,
            postgresql_where=text("status = 'CONNECTED'"),
        ),
        Index("ix_gmail_mailbox_grants_operator", "operator_subject"),
        Index("ix_gmail_mailbox_grants_account", "mailbox_account_subject"),
        CheckConstraint("btrim(operator_subject) <> ''", name="operator_subject_not_blank"),
        CheckConstraint("btrim(mailbox_address) <> ''", name="mailbox_address_not_blank"),
        CheckConstraint(
            "btrim(mailbox_account_subject) <> ''", name="mailbox_account_subject_not_blank"
        ),
        # A connected grant that cannot be refreshed is not connected. Stating it
        # as a database fact means no code path can leave a row claiming a
        # working mailbox with nothing behind it.
        CheckConstraint(
            "status <> 'CONNECTED' OR encrypted_refresh_token IS NOT NULL",
            name="connected_grant_has_refresh_token",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    #: The VMR operator this mailbox belongs to, as Google's stable ``sub`` from
    #: the *hosted sign-in* assertion. Deliberately the subject rather than the
    #: address: an operator's address can be re-pointed at a different person,
    #: and a mailbox grant must not follow it.
    operator_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The approved operator address, kept for display only.
    operator_email: Mapped[str] = mapped_column(String(320), nullable=False)

    #: The Google account the mailbox belongs to, taken from the ID token
    #: returned by the *Gmail* grant -- never from a request field, and never
    #: assumed to be the operator's own sign-in account.
    mailbox_account_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    mailbox_address: Mapped[str] = mapped_column(String(320), nullable=False)

    #: The scopes Google actually granted, space-separated, exactly as returned.
    #: Recorded rather than assumed: a consent screen where the operator unticks
    #: a box returns fewer scopes than were asked for, and the honest place to
    #: find that out is here.
    granted_scopes: Mapped[str] = mapped_column(String(1024), nullable=False, default="")

    status: Mapped[GmailGrantStatus] = mapped_column(
        Enum(GmailGrantStatus, name="gmail_grant_status"),
        nullable=False,
        default=GmailGrantStatus.CONNECTED,
    )
    #: A bounded category -- ``invalid_grant``, ``revoked``, ``transport`` -- and
    #: never a provider message, which can echo a submitted token.
    last_error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: Fernet ciphertext. Never a token, never a fingerprint of one.
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:
        """A representation that cannot carry a credential.

        SQLAlchemy's default ``__repr__`` prints only the identity map key, but
        this class is a plausible thing to drop into an f-string, a structured
        log field or a debugger watch, and two of its columns are secrets.
        Writing the representation by hand -- from the four non-secret
        identifying values -- makes leaking one require deliberately reaching
        for the column rather than merely printing the object.
        """

        return (
            f"GmailMailboxGrant(id={self.id!r}, operator_email={self.operator_email!r}, "
            f"mailbox_address={self.mailbox_address!r}, status={self.status!r})"
        )


class GmailDraftRecord(Base):
    """The durable local lineage for one Gmail draft VMR tried to create."""

    __tablename__ = "gmail_draft_records"
    __table_args__ = (
        # The idempotency constraint. Same mailbox account plus same exact
        # message version is the same draft, whatever a retry believes.
        UniqueConstraint(
            "mailbox_account_subject",
            "message_version_id",
            name="uq_gmail_draft_records_account_version",
        ),
        Index("ix_gmail_draft_records_sequence", "sequence_id"),
        Index("ix_gmail_draft_records_campaign_contact", "campaign_contact_id"),
        Index("ix_gmail_draft_records_message", "message_id"),
        Index("ix_gmail_draft_records_grant", "mailbox_grant_id"),
        CheckConstraint("position >= 1 AND position <= 7", name="position_within_sequence"),
        CheckConstraint("btrim(content_fingerprint) <> ''", name="content_fingerprint_not_blank"),
        CheckConstraint("btrim(recipient_email) <> ''", name="recipient_email_not_blank"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_not_negative"),
        # Only a created draft may name a Gmail draft id, and a created draft
        # must name one. Anything else is a row claiming an outcome it does not
        # have evidence for.
        CheckConstraint(
            "(status = 'CREATED' AND gmail_draft_id IS NOT NULL) "
            "OR (status <> 'CREATED' AND gmail_draft_id IS NULL)",
            name="created_draft_names_its_gmail_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    mailbox_grant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gmail_mailbox_grants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: Denormalized from the grant so the idempotency key survives a
    #: disconnect-and-reconnect cycle. See the module docstring.
    mailbox_account_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    mailbox_address: Mapped[str] = mapped_column(String(320), nullable=False)

    campaign_contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaign_contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_sequences.id", ondelete="CASCADE"), nullable=False
    )
    sequence_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    #: The stable logical message. Survives regeneration and every edit.
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("email_sequence_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: The exact immutable version this draft's text came from. The authority.
    message_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # Named explicitly: the metadata naming convention would derive
        # `fk_gmail_draft_records_message_version_id_email_sequence_message_versions`,
        # which is 72 characters and exceeds PostgreSQL's 63-character identifier
        # limit. An implicit name that cannot be created is worse than an
        # explicit one that can.
        ForeignKey(
            "email_sequence_message_versions.id",
            ondelete="RESTRICT",
            name="fk_gmail_draft_records_message_version_id",
        ),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    recipient_email: Mapped[str] = mapped_column(String(320), nullable=False)
    #: SHA-256 over the canonical (recipient, subject, body) rendering. Lets a
    #: reader prove what was drafted without keeping a second copy of the text.
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The RFC 5322 ``Message-ID`` VMR minted for this draft. Deterministic from
    #: the message version, which is what makes the bounded reconciliation query
    #: in ``app/services/gmail/drafts.py`` possible. It is this message's own
    #: identity and is never used to imply a reply relationship to another.
    rfc_message_id: Mapped[str] = mapped_column(String(255), nullable=False)

    status: Mapped[GmailDraftStatus] = mapped_column(
        Enum(GmailDraftStatus, name="gmail_draft_status"),
        nullable=False,
        default=GmailDraftStatus.RESERVED,
    )
    #: Bounded category only -- ``http_400``, ``transport``, ``unauthorized``.
    #: Never a provider body, which can echo a submitted credential.
    failure_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    gmail_draft_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Gmail's identity for the message *inside* the draft. Not the identity of
    #: anything that was sent: sending replaces it, and nothing here sends.
    gmail_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gmail_thread_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_by: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

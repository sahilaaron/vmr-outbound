"""Manual-action audit for the seven-email package, and Today's per-user dismissals.

``sequence_email_actions``
    One row per explicit user act on one email: Actioned, Skipped or Undone.
    Append-only. Actioned records the exact message version, the actor and the
    time; the first Actioned on position 1 establishes Day 0 for the person's
    follow-up cadence. Nothing here sends anything, and Copy / Gmail draft are
    deliberately *not* actions — they are facts recorded elsewhere.

``today_dismissals``
    One row per user, Campaign and local day: "hide this Campaign's due card on
    Today for me, today". Changes no shared email or sequence state.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import EmailActionKind


class SequenceEmailAction(Base):
    __tablename__ = "sequence_email_actions"
    __table_args__ = (
        Index(
            "ix_sequence_email_actions_membership_position",
            "campaign_contact_id",
            "position",
            "occurred_at",
        ),
        Index("ix_sequence_email_actions_campaign_id", "campaign_id"),
        CheckConstraint("position >= 1 AND position <= 7", name="position_within_sequence"),
        CheckConstraint("btrim(actor) <> ''", name="actor_not_blank"),
        # An undo names what it undoes; nothing else does.
        CheckConstraint(
            "(kind = 'UNDONE' AND undoes_action_id IS NOT NULL) "
            "OR (kind <> 'UNDONE' AND undoes_action_id IS NULL)",
            name="undo_names_its_target",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaign_contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    #: The sequence's stable identity, so a regeneration does not orphan history.
    sequence_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("email_sequence_messages.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: The exact text version the act applied to. Nullable only for UNDONE.
    message_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("email_sequence_message_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[EmailActionKind] = mapped_column(
        Enum(EmailActionKind, name="email_action_kind"), nullable=False
    )
    undoes_action_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sequence_email_actions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TodayDismissal(Base):
    __tablename__ = "today_dismissals"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "campaign_id", "local_day", name="uq_today_dismissals_user_campaign_day"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    local_day: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

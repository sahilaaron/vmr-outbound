"""Suppression ledger (DAT-006).

The ledger is the single authoritative record of identities (email addresses or
whole domains) that must never enter outreach: opt-outs, hard bounces, customers,
competitors, internal exclusions, legal/compliance holds, and manual entries. It
lives independently of any contact or campaign, so a suppressed identity that
reappears in a later import is recognised and cannot silently become eligible, and
its authority survives re-import because an import never clears the ledger.

Two tables:

* :class:`Suppression` — one record per ``(type, value, reason)``. An identity may
  carry several reasons at once (e.g. both *customer* and *competitor*); each is a
  distinct record. ``is_active`` distinguishes a live suppression from one that has
  been lifted, and unsuppressing flips this flag rather than deleting the row.
* :class:`SuppressionEvent` — the append-only lifecycle history of a suppression
  (created, reactivated, deactivated), so lifting a suppression never destroys the
  record of who suppressed the identity, why, and when.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import SuppressionEventType, SuppressionReason, SuppressionType


class Suppression(Base):
    """One suppressed identity under one reason (a normalized email or domain)."""

    __tablename__ = "suppressions"
    __table_args__ = (
        # One record per identity value + type + reason. An identity may be
        # suppressed under several reasons at once; re-recording the same reason is
        # idempotent (it reactivates the existing record rather than duplicating).
        UniqueConstraint(
            "suppression_type", "value", "reason", name="uq_suppressions_type_value_reason"
        ),
        Index("ix_suppressions_value", "value"),
        # Active-suppression lookups (the enforcement hot path) filter on is_active.
        Index("ix_suppressions_value_active", "value", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    suppression_type: Mapped[SuppressionType] = mapped_column(
        Enum(SuppressionType, name="suppression_type"),
        nullable=False,
    )
    # Normalized value: a lowercase email address or a lowercase hostname.
    value: Mapped[str] = mapped_column(String(320), nullable=False)
    reason: Mapped[SuppressionReason] = mapped_column(
        Enum(SuppressionReason, name="suppression_reason"),
        nullable=False,
    )
    # Where the suppression came from (e.g. "saleshandy_bounce", "manual").
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Who created the suppression (operator id/email or service name), when known.
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Whether the suppression is currently in force. Lifting a suppression sets
    # this False (and appends a DEACTIVATED event); it never deletes the row.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"Suppression(type={self.suppression_type.value!r}, value={self.value!r}, "
            f"reason={self.reason.value!r}, active={self.is_active!r})"
        )


class SuppressionEvent(Base):
    """One append-only lifecycle event for a suppression record (DAT-006)."""

    __tablename__ = "suppression_events"
    __table_args__ = (Index("ix_suppression_events_suppression_id", "suppression_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Monotonic insertion order, so a record's events sort deterministically even
    # when several are appended in one transaction (server timestamps would tie).
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=False), nullable=False)
    suppression_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("suppressions.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[SuppressionEventType] = mapped_column(
        Enum(SuppressionEventType, name="suppression_event_type"),
        nullable=False,
    )
    # Snapshot of the reason at event time, so history is self-contained.
    reason: Mapped[SuppressionReason] = mapped_column(
        Enum(SuppressionReason, name="suppression_reason"),
        nullable=False,
    )
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The record's active state immediately after this event.
    active_after: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"SuppressionEvent(suppression_id={self.suppression_id!r}, "
            f"event={self.event_type.value!r}, active_after={self.active_after!r})"
        )

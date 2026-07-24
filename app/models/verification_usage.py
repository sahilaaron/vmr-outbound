"""Verification usage and exception log (VER-006).

Every meaningful verification event — a paid provider call, a cache reuse that
avoided a call, a transient failure, an insufficient-credit exception, a detected
stale result or evidence conflict — is appended here so provider spend and every
exception are visible and auditable. ``credited`` records whether MillionVerifier
actually charged (only ok/invalid/disposable are billed; catch-all and unknown are
free), which is what makes real operating cost, not just call volume, visible.

This log never stores secrets: the API key is never written here, and the raw
provider payload (already redacted of any credential) lives on the evidence row,
not on this event.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import VerificationUsageEventType


class VerificationUsageEvent(Base):
    """One appended verification usage/exception event."""

    __tablename__ = "verification_usage_events"
    __table_args__ = (
        Index("ix_verification_usage_events_type", "event_type"),
        Index("ix_verification_usage_events_created_at", "created_at"),
        Index("ix_verification_usage_events_email", "email"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type: Mapped[VerificationUsageEventType] = mapped_column(
        Enum(VerificationUsageEventType, name="verification_usage_event_type"),
        nullable=False,
    )
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True,
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("verification_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    # The mapped internal result when the event produced one (e.g. "valid").
    result: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Whether the provider actually billed a credit for this event.
    credited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Provider-reported remaining credit balance, when available.
    credits_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"VerificationUsageEvent(type={self.event_type.value!r}, "
            f"result={self.result!r}, credited={self.credited!r})"
        )

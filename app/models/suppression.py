"""Suppression ledger.

The ledger is the authoritative record of identities (email addresses or whole
domains) that must never enter outreach: opt-outs, hard bounces, customers,
competitors, and internal exclusions (DAT-006). It lives independently of any
contact or campaign, so a suppressed identity that reappears in a later CSV is
recognised and cannot silently become eligible. Suppression authority survives
re-import because the ledger is never cleared by an import.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import SuppressionReason, SuppressionType


class Suppression(Base):
    """One suppressed identity (a normalized email address or a domain)."""

    __tablename__ = "suppressions"
    __table_args__ = (
        # One active suppression per identity value + type.
        UniqueConstraint("suppression_type", "value", name="uq_suppressions_type_value"),
        Index("ix_suppressions_value", "value"),
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"Suppression(type={self.suppression_type.value!r}, value={self.value!r}, "
            f"reason={self.reason.value!r})"
        )

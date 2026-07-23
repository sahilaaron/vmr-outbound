"""External-provider event model (DAT-001 representation).

Represents inbound events from external providers (e.g. Saleshandy webhooks,
MillionVerifier callbacks) with idempotency via a stable external id. This slice
only represents the table; no provider integration or webhook processing exists.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ExternalEvent(Base):
    """One inbound external-provider event, deduplicated by (provider, event id)."""

    __tablename__ = "external_events"
    __table_args__ = (
        # Duplicate protection: the same provider event is ingested at most once.
        UniqueConstraint(
            "provider", "external_event_id", name="uq_external_events_provider_event_id"
        ),
        Index("ix_external_events_provider_type", "provider", "event_type"),
        Index("ix_external_events_received_at", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    # Stable id from the provider used for idempotency.
    external_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Controlled payload storage; never stores secrets.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"ExternalEvent(provider={self.provider!r}, "
            f"external_event_id={self.external_event_id!r}, event_type={self.event_type!r})"
        )

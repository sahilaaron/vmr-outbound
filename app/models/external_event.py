"""External-provider event model (DAT-001 representation; CMP-003 attribution).

Represents inbound events from external providers (e.g. Saleshandy webhooks,
MillionVerifier callbacks) with idempotency via a stable external id — this is
the existing, pre-CMP-003 "activity/history" primitive the repository already
had, and CMP-003 reuses it as the outreach-history record rather than inventing
a parallel table.

CMP-003 adds three nullable attribution columns (``contact_id``, ``campaign_id``,
``campaign_contact_id``) so a historical outreach event is queryable by contact,
by campaign, and by the specific membership it happened under. They stay
nullable because an inbound provider event can still be represented before it
is resolved to a contact (unchanged DAT-001 behaviour — this table is still
"representation only"; no webhook ingestion pipeline is built here).

``campaign_contact_id`` uses ``ON DELETE SET NULL`` rather than ``CASCADE``:
unlike a contact or a campaign, a single ``CampaignContact`` membership row can
legitimately be removed by an unrelated, pre-existing operation — DAT-004's
duplicate-contact merge coalesces two contacts' memberships in the same
campaign into one and deletes the redundant row (see
``app/services/identity.py::_apply_merge``). If that ever happens, an outreach
event must not be silently deleted with it (AGENTS.md: never erase history);
``_apply_merge`` proactively re-parents any events onto the surviving
membership before the redundant row is deleted, and the ``SET NULL`` behaviour
is the defense-in-depth fallback for any other path that removes a membership
row. ``contact_id``/``campaign_id`` are the durable attribution keys that
survive even if ``campaign_contact_id`` is ever null.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ExternalEvent(Base):
    """One inbound external-provider event, deduplicated by (provider, event id)."""

    __tablename__ = "external_events"
    __table_args__ = (
        # Duplicate protection: the same provider event is ingested at most once.
        # This is also CMP-003's DB-level guard against recording the same
        # outreach event twice (e.g. a redelivered webhook).
        UniqueConstraint(
            "provider", "external_event_id", name="uq_external_events_provider_event_id"
        ),
        Index("ix_external_events_provider_type", "provider", "event_type"),
        Index("ix_external_events_received_at", "received_at"),
        # Outreach-history read paths (CMP-003): by contact, by campaign, and by
        # the specific membership.
        Index("ix_external_events_contact_id", "contact_id"),
        Index("ix_external_events_campaign_id", "campaign_id"),
        Index("ix_external_events_campaign_contact_id", "campaign_contact_id"),
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

    # --- CMP-003 outreach-history attribution --------------------------------
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=True,
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=True,
    )
    campaign_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaign_contacts.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"ExternalEvent(provider={self.provider!r}, "
            f"external_event_id={self.external_event_id!r}, event_type={self.event_type!r})"
        )

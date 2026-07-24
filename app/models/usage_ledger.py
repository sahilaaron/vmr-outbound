"""Provider-neutral external-usage ledger (VER-006, extensible).

One row per external provider request (or cache hit that avoided one), capturing
the operational and financial facts every paid or metered integration shares:
which provider and operation, the campaign and related work, when it was
attempted, the outcome, whether it hit cache, the retry number, units consumed,
an estimated cost and the provider-reported cost when available, the currency,
and whether the charge is confirmed or uncertain.

It is deliberately provider-neutral so future metered services — research APIs,
AI models, enrichment providers, Saleshandy — record usage in the *same table*
without a schema replacement:

* ``provider`` / ``operation`` are free strings, not a MillionVerifier-specific
  enum.
* the related job is a *soft* reference (``job_id`` + ``job_kind``) rather than a
  hard foreign key to one provider's job table, so a new provider's own job/
  request identifiers fit unchanged.
* ``units`` is generic (email credits today; tokens, API calls, or messages
  tomorrow), and cost is stored as an exact decimal with an explicit currency.

This is the shared ledger primitive, not a finance dashboard: the full
multi-provider cost view is out of scope for #137.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import UsageCacheStatus, UsageChargeStatus


class UsageLedgerEntry(Base):
    """One provider-neutral external-usage/cost ledger entry."""

    __tablename__ = "usage_ledger_entries"
    __table_args__ = (
        Index("ix_usage_ledger_entries_provider", "provider"),
        Index("ix_usage_ledger_entries_provider_attempted", "provider", "attempted_at"),
        Index("ix_usage_ledger_entries_campaign_id", "campaign_id"),
        Index("ix_usage_ledger_entries_job", "job_kind", "job_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # --- Who / what -----------------------------------------------------------
    # Free strings, not enums, so a new provider needs no schema change.
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    operation: Mapped[str] = mapped_column(String(100), nullable=False)

    # --- Context --------------------------------------------------------------
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="SET NULL"),
        nullable=True,
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Soft reference to the related unit of work: a UUID plus its kind, with NO
    # hard FK, so any provider's own job/request id fits without a new column.
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    job_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # An optional provider-side request identifier (idempotency key, request id).
    request_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- Timing & outcome -----------------------------------------------------
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Provider-neutral result label (e.g. "valid", "catch_all", "error").
    result: Mapped[str | None] = mapped_column(String(50), nullable=True)
    cache_status: Mapped[UsageCacheStatus] = mapped_column(
        Enum(UsageCacheStatus, name="usage_cache_status"), nullable=False
    )
    retry_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- Units & cost ---------------------------------------------------------
    # Generic metered units consumed (credits today; tokens/messages later).
    units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Our local estimate; exact decimal to avoid float money errors.
    estimated_cost: Mapped[Decimal] = mapped_column(
        Numeric(14, 6), nullable=False, default=Decimal("0")
    )
    # Provider-reported cost when the provider returns one (null for MillionVerifier,
    # which reports only a remaining-credit balance).
    provider_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    charge_status: Mapped[UsageChargeStatus] = mapped_column(
        Enum(UsageChargeStatus, name="usage_charge_status"), nullable=False
    )
    # Provider-reported remaining balance after this request, when obtainable.
    credits_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"UsageLedgerEntry(provider={self.provider!r}, operation={self.operation!r}, "
            f"result={self.result!r}, cache={self.cache_status.value!r}, "
            f"charge={self.charge_status.value!r}, units={self.units!r})"
        )

"""Provider-neutral usage-ledger service (VER-006, extensible).

Records one ledger entry per external provider request (or avoided request) and
computes the compact per-provider usage/cost summary the operator sees. It is not
tied to MillionVerifier: any future metered provider calls :func:`record_entry`
with its own ``provider``/``operation`` strings and soft job reference, and gets
the same accounting for free.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import UsageCacheStatus, UsageChargeStatus
from app.models.usage_ledger import UsageLedgerEntry


def record_entry(
    session: Session,
    *,
    provider: str,
    operation: str,
    attempted_at: datetime,
    cache_status: UsageCacheStatus,
    charge_status: UsageChargeStatus,
    units: int = 0,
    estimated_cost: Decimal | None = None,
    provider_cost: Decimal | None = None,
    currency: str = "USD",
    result: str | None = None,
    retry_number: int = 0,
    campaign_id: uuid.UUID | None = None,
    contact_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    job_kind: str | None = None,
    request_ref: str | None = None,
    credits_remaining: int | None = None,
    reason: str | None = None,
) -> UsageLedgerEntry:
    """Append one provider-neutral usage ledger entry (caller owns the transaction)."""

    entry = UsageLedgerEntry(
        provider=provider,
        operation=operation,
        attempted_at=attempted_at,
        cache_status=cache_status,
        charge_status=charge_status,
        units=units,
        estimated_cost=estimated_cost if estimated_cost is not None else Decimal("0"),
        provider_cost=provider_cost,
        currency=currency,
        result=result,
        retry_number=retry_number,
        campaign_id=campaign_id,
        contact_id=contact_id,
        job_id=job_id,
        job_kind=job_kind,
        request_ref=request_ref,
        credits_remaining=credits_remaining,
        reason=reason,
    )
    session.add(entry)
    session.flush()
    return entry


@dataclass
class LedgerSummary:
    """Compact per-provider usage/cost summary for the operator."""

    provider: str
    currency: str = "USD"
    # Cost-rate context so the UI can be honest when the rate is unset.
    cost_per_unit: Decimal = Decimal("0")
    rate_configured: bool = False
    # Volumes.
    calls: int = 0  # real provider requests (cache MISS)
    billed_calls: int = 0  # requests with a CONFIRMED charge
    cache_hits: int = 0  # requests avoided by cache reuse
    failures: int = 0  # MISS requests that produced no result
    uncertain_charges: int = 0
    units_consumed: int = 0
    # Money.
    estimated_spend: Decimal = Decimal("0")
    provider_reported_spend: Decimal | None = None
    cache_savings_estimated: Decimal = Decimal("0")
    projected_batch_cost: Decimal = Decimal("0")
    remaining_credits: int | None = None


def provider_summary(
    session: Session,
    *,
    provider: str,
    currency: str,
    cost_per_unit: Decimal,
    pending_units: int = 0,
) -> LedgerSummary:
    """Aggregate the ledger for one provider.

    ``pending_units`` is the number of still-to-run billable-at-most units in the
    active batch (e.g. runnable verification jobs); the projected cost is an upper
    bound because some of those may return a free result.
    """

    summary = LedgerSummary(
        provider=provider,
        currency=currency,
        cost_per_unit=cost_per_unit,
        rate_configured=cost_per_unit > 0,
    )

    rows = session.execute(
        select(
            UsageLedgerEntry.cache_status,
            UsageLedgerEntry.charge_status,
            func.count(),
            func.coalesce(func.sum(UsageLedgerEntry.units), 0),
            func.coalesce(func.sum(UsageLedgerEntry.estimated_cost), 0),
        )
        .where(UsageLedgerEntry.provider == provider)
        .group_by(UsageLedgerEntry.cache_status, UsageLedgerEntry.charge_status)
    ).all()

    for cache_status, charge_status, count, units, est in rows:
        count = int(count)
        summary.units_consumed += int(units)
        summary.estimated_spend += Decimal(est)
        if cache_status == UsageCacheStatus.HIT:
            summary.cache_hits += count
        elif cache_status == UsageCacheStatus.MISS:
            summary.calls += count
            if charge_status == UsageChargeStatus.CONFIRMED:
                summary.billed_calls += count
        if charge_status == UsageChargeStatus.UNCERTAIN:
            summary.uncertain_charges += count

    # A MISS that produced no billable result and no address result is a failure
    # from a cost standpoint (error/timeout/insufficient credits): count MISS
    # entries whose result is null.
    summary.failures = (
        session.scalar(
            select(func.count()).where(
                UsageLedgerEntry.provider == provider,
                UsageLedgerEntry.cache_status == UsageCacheStatus.MISS,
                UsageLedgerEntry.result.is_(None),
            )
        )
        or 0
    )

    # Provider-reported spend, only when at least one entry carried a provider cost.
    reported = session.scalar(
        select(func.sum(UsageLedgerEntry.provider_cost)).where(
            UsageLedgerEntry.provider == provider,
            UsageLedgerEntry.provider_cost.is_not(None),
        )
    )
    summary.provider_reported_spend = Decimal(reported) if reported is not None else None

    # Latest known remaining balance.
    summary.remaining_credits = session.scalars(
        select(UsageLedgerEntry.credits_remaining)
        .where(
            UsageLedgerEntry.provider == provider,
            UsageLedgerEntry.credits_remaining.is_not(None),
        )
        .order_by(UsageLedgerEntry.attempted_at.desc())
        .limit(1)
    ).first()

    summary.cache_savings_estimated = Decimal(summary.cache_hits) * cost_per_unit
    summary.projected_batch_cost = Decimal(max(0, pending_units)) * cost_per_unit
    return summary


def recent_entries(session: Session, *, provider: str, limit: int = 25) -> list[UsageLedgerEntry]:
    return list(
        session.scalars(
            select(UsageLedgerEntry)
            .where(UsageLedgerEntry.provider == provider)
            .order_by(UsageLedgerEntry.attempted_at.desc())
            .limit(limit)
        ).all()
    )

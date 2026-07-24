"""Verification usage and exception tracking (VER-006).

Records every meaningful verification event and provides the aggregates the
operator surface shows: calls made, cache reuse that avoided calls, credited
(billed) calls, and each exception class. This is what makes real operating cost
and exception volume visible, not just call counts.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import VerificationUsageEventType
from app.models.verification_usage import VerificationUsageEvent


def record_usage(
    session: Session,
    *,
    event_type: VerificationUsageEventType,
    provider: str,
    email: str | None = None,
    contact_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    result: str | None = None,
    credited: bool = False,
    credits_remaining: int | None = None,
    reason: str | None = None,
) -> VerificationUsageEvent:
    """Append one usage/exception event (caller owns the transaction)."""

    event = VerificationUsageEvent(
        event_type=event_type,
        provider=provider,
        email=email,
        contact_id=contact_id,
        job_id=job_id,
        result=result,
        credited=credited,
        credits_remaining=credits_remaining,
        reason=reason,
    )
    session.add(event)
    session.flush()
    return event


@dataclass
class UsageSummary:
    """Aggregate verification usage for the operator view."""

    calls_made: int = 0
    credited_calls: int = 0
    cache_reuse: int = 0
    provider_errors: int = 0
    timeouts: int = 0
    insufficient_credits: int = 0
    retries_scheduled: int = 0
    stale_detected: int = 0
    conflicts_detected: int = 0
    recovered: int = 0
    latest_credits_remaining: int | None = None


def usage_summary(session: Session) -> UsageSummary:
    """Aggregate counts across all recorded usage events."""

    counts: dict[VerificationUsageEventType, int] = {
        event_type: int(count)
        for event_type, count in session.execute(
            select(VerificationUsageEvent.event_type, func.count()).group_by(
                VerificationUsageEvent.event_type
            )
        ).all()
    }
    summary = UsageSummary(
        calls_made=counts.get(VerificationUsageEventType.CALL_MADE, 0),
        cache_reuse=counts.get(VerificationUsageEventType.CACHE_REUSE, 0),
        provider_errors=counts.get(VerificationUsageEventType.PROVIDER_ERROR, 0),
        timeouts=counts.get(VerificationUsageEventType.TIMEOUT, 0),
        insufficient_credits=counts.get(VerificationUsageEventType.INSUFFICIENT_CREDITS, 0),
        retries_scheduled=counts.get(VerificationUsageEventType.RETRY_SCHEDULED, 0),
        stale_detected=counts.get(VerificationUsageEventType.STALE_DETECTED, 0),
        conflicts_detected=counts.get(VerificationUsageEventType.CONFLICT_DETECTED, 0),
        recovered=counts.get(VerificationUsageEventType.RECOVERED, 0),
    )
    summary.credited_calls = (
        session.scalar(
            select(func.count()).where(
                VerificationUsageEvent.event_type == VerificationUsageEventType.CALL_MADE,
                VerificationUsageEvent.credited.is_(True),
            )
        )
        or 0
    )
    # Latest known provider credit balance (most recent event that carried one).
    summary.latest_credits_remaining = session.scalars(
        select(VerificationUsageEvent.credits_remaining)
        .where(VerificationUsageEvent.credits_remaining.is_not(None))
        .order_by(VerificationUsageEvent.created_at.desc())
        .limit(1)
    ).first()
    return summary


def recent_events(session: Session, *, limit: int = 50) -> list[VerificationUsageEvent]:
    return list(
        session.scalars(
            select(VerificationUsageEvent)
            .order_by(VerificationUsageEvent.created_at.desc())
            .limit(limit)
        ).all()
    )

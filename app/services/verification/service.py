"""Verification orchestration (VER-001..006 wiring).

The single place that turns a queued job into durable evidence safely:

1. reuse fresh cached evidence for the *same exact address* instead of paying for
   a call (VER-003);
2. otherwise call the provider once, mapping the outcome truthfully (VER-002);
3. store address evidence (and only address evidence) with its policy version;
4. record usage/exceptions and cost visibility (VER-006);
5. retry only transient failures with bounded backoff, never a definite result or
   an operational exception (VER-005).

All of this runs against the simulator by default; the live client is only used
for the deliberate manual smoke test.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.contact import Contact
from app.models.email_evidence import ExactEmailVerification
from app.models.enums import (
    EmailPreciseStatus,
    UsageCacheStatus,
    UsageChargeStatus,
    VerificationUsageEventType,
)
from app.models.verification_job import VerificationJob
from app.services import usage_ledger
from app.services.email.candidates import generate_candidates
from app.services.imports.normalization import normalize_email
from app.services.verification import queue as jobs
from app.services.verification import usage
from app.services.verification.policy import MappedOutcome, VerificationPolicy, get_policy
from app.services.verification.provider import (
    ProviderResponse,
    ProviderTransientError,
    VerificationProvider,
    build_provider,
)

# Provider-neutral labels for the shared usage ledger.
LEDGER_PROVIDER = "millionverifier"
LEDGER_OPERATION = "verify_email"


def _now() -> datetime:
    return datetime.now(UTC)


def _cost_per_credit(settings: Settings) -> Decimal:
    return Decimal(str(settings.millionverifier_cost_per_credit))


def get_provider(settings: Settings, *, live: bool = False) -> VerificationProvider:
    """Build the configured provider. Defaults to the network-free simulator."""

    return build_provider(
        api_key=settings.millionverifier_api_key,
        base_url=settings.millionverifier_base_url,
        timeout_seconds=settings.millionverifier_timeout_seconds,
        live=live,
    )


def find_fresh_evidence(
    session: Session, email: str, policy: VerificationPolicy, now: datetime
) -> ExactEmailVerification | None:
    """Return fresh cached evidence for the *same exact address*, else None."""

    norm = normalize_email(email)
    if not norm:
        return None
    latest = session.scalars(
        select(ExactEmailVerification)
        .where(ExactEmailVerification.email == norm)
        .order_by(ExactEmailVerification.checked_at.desc())
        .limit(1)
    ).first()
    if latest is None:
        return None
    if policy.is_fresh(latest.result, latest.checked_at, now):
        return latest
    return None


@dataclass
class EnqueueOutcome:
    """Result of preparing a contact for verification."""

    email: str | None = None
    job: VerificationJob | None = None
    created: bool = False
    reused_evidence: ExactEmailVerification | None = None
    needs_review: bool = False
    review_reason: str | None = None


def prepare_and_enqueue_contact(
    session: Session,
    contact: Contact,
    *,
    settings: Settings | None = None,
    policy: VerificationPolicy | None = None,
    provider_name: str = "millionverifier",
    campaign_id: uuid.UUID | None = None,
) -> EnqueueOutcome:
    """Generate/select a candidate and enqueue it, reusing fresh evidence if any."""

    settings = settings or get_settings()
    policy = policy or get_policy(settings)

    gen = generate_candidates(session, contact)
    if gen.needs_review or gen.selected is None:
        return EnqueueOutcome(needs_review=True, review_reason=gen.review_reason)

    email = gen.selected.email
    now = _now()
    fresh = find_fresh_evidence(session, email, policy, now)
    if fresh is not None:
        usage.record_usage(
            session,
            event_type=VerificationUsageEventType.CACHE_REUSE,
            provider=provider_name,
            email=email,
            contact_id=contact.id,
            result=fresh.result.value,
            reason="reused fresh cached evidence; no provider call made",
        )
        # A cache hit avoided a provider request: log it to the neutral ledger
        # with no charge so cache savings are measurable.
        usage_ledger.record_entry(
            session,
            provider=LEDGER_PROVIDER,
            operation=LEDGER_OPERATION,
            attempted_at=now,
            cache_status=UsageCacheStatus.HIT,
            charge_status=UsageChargeStatus.NONE,
            units=0,
            currency=settings.millionverifier_currency,
            result=fresh.result.value,
            campaign_id=campaign_id,
            contact_id=contact.id,
            request_ref=f"{policy.version}:{email}",
            reason="reused fresh cached evidence; no provider call made",
        )
        return EnqueueOutcome(email=email, reused_evidence=fresh)

    job, created = jobs.enqueue_verification(
        session,
        email=email,
        policy_version=policy.version,
        max_attempts=settings.verification_max_attempts,
        contact_id=contact.id,
        campaign_id=campaign_id,
    )
    return EnqueueOutcome(email=email, job=job, created=created)


def _ledger_for_job(
    session: Session,
    job: VerificationJob,
    settings: Settings,
    *,
    cache_status: UsageCacheStatus,
    charge_status: UsageChargeStatus,
    now: datetime,
    units: int = 0,
    estimated_cost: Decimal | None = None,
    result: str | None = None,
    credits_remaining: int | None = None,
    reason: str | None = None,
) -> None:
    """Append one provider-neutral usage ledger entry for a verification job."""

    usage_ledger.record_entry(
        session,
        provider=LEDGER_PROVIDER,
        operation=LEDGER_OPERATION,
        attempted_at=now,
        cache_status=cache_status,
        charge_status=charge_status,
        units=units,
        estimated_cost=estimated_cost if estimated_cost is not None else Decimal("0"),
        currency=settings.millionverifier_currency,
        result=result,
        retry_number=job.attempts,
        campaign_id=job.campaign_id,
        contact_id=job.contact_id,
        job_id=job.id,
        job_kind="verification_job",
        request_ref=job.idempotency_key,
        credits_remaining=credits_remaining,
        reason=reason,
    )


def _store_evidence(
    session: Session,
    *,
    email: str,
    mapped: MappedOutcome,
    response: ProviderResponse,
    policy: VerificationPolicy,
    contact_id: uuid.UUID | None,
    provider_name: str,
    now: datetime,
) -> ExactEmailVerification:
    row = ExactEmailVerification(
        email=email,
        result=mapped.result,
        provider=provider_name,
        policy_version=policy.version,
        provider_result_code=str(response.resultcode) if response.resultcode is not None else None,
        provider_reference=None,
        reason=mapped.reason,
        subresult=response.subresult,
        quality=response.quality,
        is_role=response.role,
        is_free=response.free,
        did_you_mean=response.didyoumean,
        checked_at=now,
        raw_response=response.raw or None,
        contact_id=contact_id,
    )
    session.add(row)
    session.flush()
    return row


def process_job(
    session: Session,
    job: VerificationJob,
    *,
    provider: VerificationProvider,
    settings: Settings | None = None,
    policy: VerificationPolicy | None = None,
) -> VerificationJob:
    """Process one claimed job to a terminal or retry state. Never network in tests."""

    settings = settings or get_settings()
    policy = policy or get_policy(settings)
    now = _now()
    provider_name = provider.name

    if job.__dict__.get("_reclaimed"):
        usage.record_usage(
            session,
            event_type=VerificationUsageEventType.RECOVERED,
            provider=provider_name,
            email=job.email,
            contact_id=job.contact_id,
            job_id=job.id,
            reason="job reclaimed after a worker lease expired",
        )
        # The dead worker may have completed a paid call before dying: record an
        # UNCERTAIN-charge ledger entry so a possible charge is never lost.
        _ledger_for_job(
            session,
            job,
            settings,
            cache_status=UsageCacheStatus.MISS,
            charge_status=UsageChargeStatus.UNCERTAIN,
            now=now,
            result="uncertain_prior_attempt",
            reason="prior worker lease expired; a paid call may have completed before recovery",
        )

    # Cache reuse safety net: fresh evidence may have appeared since enqueue.
    fresh = find_fresh_evidence(session, job.email, policy, now)
    if fresh is not None:
        usage.record_usage(
            session,
            event_type=VerificationUsageEventType.CACHE_REUSE,
            provider=provider_name,
            email=job.email,
            contact_id=job.contact_id,
            job_id=job.id,
            result=fresh.result.value,
            reason="fresh evidence already present; skipped provider call",
        )
        _ledger_for_job(
            session,
            job,
            settings,
            cache_status=UsageCacheStatus.HIT,
            charge_status=UsageChargeStatus.NONE,
            now=now,
            result=fresh.result.value,
            reason="fresh evidence already present; skipped provider call",
        )
        precise = policy.precise_for_result(fresh.result, is_role=bool(fresh.is_role))
        return jobs.mark_succeeded(
            session, job, verification_id=fresh.id, outcome_status=precise.value, now=now
        )

    # One provider call.
    try:
        response = provider.verify(job.email)
    except ProviderTransientError as exc:
        usage.record_usage(
            session,
            event_type=VerificationUsageEventType.TIMEOUT,
            provider=provider_name,
            email=job.email,
            contact_id=job.contact_id,
            job_id=job.id,
            reason=f"transport failure: {exc}",
        )
        _maybe_retry_usage(session, job, provider_name)
        _ledger_for_job(
            session,
            job,
            settings,
            cache_status=UsageCacheStatus.MISS,
            charge_status=UsageChargeStatus.NONE,
            now=now,
            result=None,
            reason=f"transport failure (no charge): {exc}",
        )
        return jobs.schedule_retry(
            session,
            job,
            reason=f"transport failure: {exc}",
            base=settings.verification_retry_base_seconds,
            cap=settings.verification_retry_max_seconds,
            outcome_status=EmailPreciseStatus.PROVIDER_ERROR.value,
            now=now,
        )

    mapped = policy.map_response(response)

    if mapped.is_address_evidence:
        row = _store_evidence(
            session,
            email=job.email,
            mapped=mapped,
            response=response,
            policy=policy,
            contact_id=job.contact_id,
            provider_name=provider_name,
            now=now,
        )
        usage.record_usage(
            session,
            event_type=VerificationUsageEventType.CALL_MADE,
            provider=provider_name,
            email=job.email,
            contact_id=job.contact_id,
            job_id=job.id,
            result=mapped.result.value if mapped.result else None,
            credited=mapped.credited,
            credits_remaining=response.credits,
            reason=mapped.reason,
        )
        # Only ok/invalid/disposable are billed; catch-all/unknown are free.
        units = 1 if mapped.credited else 0
        cost = _cost_per_credit(settings) * units
        _ledger_for_job(
            session,
            job,
            settings,
            cache_status=UsageCacheStatus.MISS,
            charge_status=(
                UsageChargeStatus.CONFIRMED if mapped.credited else UsageChargeStatus.NONE
            ),
            now=now,
            units=units,
            estimated_cost=cost,
            result=mapped.result.value if mapped.result else None,
            credits_remaining=response.credits,
            reason=mapped.reason,
        )
        return jobs.mark_succeeded(
            session, job, verification_id=row.id, outcome_status=mapped.precise.value, now=now
        )

    if mapped.kind == "insufficient_credits":
        usage.record_usage(
            session,
            event_type=VerificationUsageEventType.INSUFFICIENT_CREDITS,
            provider=provider_name,
            email=job.email,
            contact_id=job.contact_id,
            job_id=job.id,
            credits_remaining=response.credits,
            reason=mapped.reason,
        )
        _ledger_for_job(
            session,
            job,
            settings,
            cache_status=UsageCacheStatus.MISS,
            charge_status=UsageChargeStatus.NONE,
            now=now,
            result=None,
            credits_remaining=response.credits,
            reason=mapped.reason,
        )
        return jobs.mark_failed(
            session,
            job,
            reason=mapped.reason,
            outcome_status=EmailPreciseStatus.INSUFFICIENT_CREDITS.value,
            now=now,
        )

    if mapped.retryable:  # transient provider error / result=error / unrecognised
        usage.record_usage(
            session,
            event_type=VerificationUsageEventType.PROVIDER_ERROR,
            provider=provider_name,
            email=job.email,
            contact_id=job.contact_id,
            job_id=job.id,
            reason=mapped.reason,
        )
        _maybe_retry_usage(session, job, provider_name)
        _ledger_for_job(
            session,
            job,
            settings,
            cache_status=UsageCacheStatus.MISS,
            charge_status=UsageChargeStatus.NONE,
            now=now,
            result=None,
            reason=f"provider error (no charge): {mapped.reason}",
        )
        return jobs.schedule_retry(
            session,
            job,
            reason=mapped.reason,
            base=settings.verification_retry_base_seconds,
            cap=settings.verification_retry_max_seconds,
            outcome_status=EmailPreciseStatus.PROVIDER_ERROR.value,
            now=now,
        )

    # Non-retryable provider/config error: fail without paid evidence.
    usage.record_usage(
        session,
        event_type=VerificationUsageEventType.PROVIDER_ERROR,
        provider=provider_name,
        email=job.email,
        contact_id=job.contact_id,
        job_id=job.id,
        reason=mapped.reason,
    )
    _ledger_for_job(
        session,
        job,
        settings,
        cache_status=UsageCacheStatus.MISS,
        charge_status=UsageChargeStatus.NONE,
        now=now,
        result=None,
        reason=f"provider/config error (no charge): {mapped.reason}",
    )
    return jobs.mark_failed(
        session,
        job,
        reason=mapped.reason,
        outcome_status=EmailPreciseStatus.PROVIDER_ERROR.value,
        now=now,
    )


def _maybe_retry_usage(session: Session, job: VerificationJob, provider_name: str) -> None:
    """Record a RETRY_SCHEDULED usage event when another attempt remains."""

    if job.attempts < job.max_attempts:
        usage.record_usage(
            session,
            event_type=VerificationUsageEventType.RETRY_SCHEDULED,
            provider=provider_name,
            email=job.email,
            contact_id=job.contact_id,
            job_id=job.id,
            reason=f"attempt {job.attempts}/{job.max_attempts}; backoff scheduled",
        )


def run_worker_once(
    session: Session,
    *,
    provider: VerificationProvider,
    settings: Settings | None = None,
    worker_id: str = "worker-local",
) -> VerificationJob | None:
    """Claim and process a single job. Returns the job, or None if none runnable."""

    settings = settings or get_settings()
    job = jobs.claim_next_job(
        session, worker_id=worker_id, lease_seconds=settings.verification_lease_seconds
    )
    if job is None:
        return None
    return process_job(session, job, provider=provider, settings=settings)


def run_worker(
    session: Session,
    *,
    provider: VerificationProvider,
    settings: Settings | None = None,
    max_jobs: int = 100,
    worker_id: str = "worker-local",
) -> list[VerificationJob]:
    """Drain runnable jobs up to *max_jobs* (one pass; retries stay scheduled)."""

    settings = settings or get_settings()
    processed: list[VerificationJob] = []
    for _ in range(max_jobs):
        job = run_worker_once(session, provider=provider, settings=settings, worker_id=worker_id)
        if job is None:
            break
        processed.append(job)
    return processed

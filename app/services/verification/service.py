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
from app.models.email_evidence import ExactEmailVerification, MailDomainObservation
from app.models.enums import (
    EmailPreciseStatus,
    EmailVerificationResult,
    UsageCacheStatus,
    UsageChargeStatus,
    VerificationFailureClass,
    VerificationUsageEventType,
)
from app.models.verification_attempt import VerificationAttempt
from app.models.verification_job import VerificationJob
from app.services import usage_ledger
from app.services.email.candidates import generate_candidates
from app.services.imports.normalization import normalize_email
from app.services.suppressions import evaluate_suppression
from app.services.verification import attempts as job_attempts
from app.services.verification import queue as jobs
from app.services.verification import usage
from app.services.verification.policy import MappedOutcome, VerificationPolicy, get_policy
from app.services.verification.provider import (
    LIVE_PROVIDER_LABEL,
    SIMULATOR_PROVIDER_LABEL,
    ProviderResponse,
    ProviderTransientError,
    VerificationProvider,
    build_provider,
    evidence_provider_label,
    redact_secret,
)

# Compatibility label used by the legacy single-provider console. New
# provider-specific writes pass their real provider id into ``_ledger_for_job``.
LEDGER_PROVIDER = "millionverifier"
LEDGER_OPERATION = "verify_email"


def _now() -> datetime:
    return datetime.now(UTC)


def _cost_per_credit(settings: Settings, provider_name: str) -> Decimal:
    # Only MillionVerifier currently has an operator-configured local rate.
    # DeBounce remains explicitly unestimated until a provider-specific rate is
    # configured; borrowing another vendor's rate would fabricate cost.
    if provider_name != "millionverifier":
        return Decimal("0")
    return Decimal(str(settings.millionverifier_cost_per_credit))


def get_provider(settings: Settings, *, live: bool = False) -> VerificationProvider:
    """Build the configured provider. Defaults to the network-free simulator."""

    return build_provider(
        api_key=settings.millionverifier_api_key,
        base_url=settings.millionverifier_base_url,
        timeout_seconds=settings.millionverifier_timeout_seconds,
        live=live,
    )


def _provider_provenance_satisfies(stored: str, required: str | None) -> bool:
    """Whether *stored* evidence is at least as authoritative as requested.

    Live provider evidence may safely answer a simulator caller, but simulated
    evidence must never satisfy a live execution. Unknown provider labels are
    reusable only for an exact match so a future adapter cannot accidentally
    upgrade provenance by name alone.
    """

    if required is None:
        return True
    strengths = {
        SIMULATOR_PROVIDER_LABEL: 0,
        LIVE_PROVIDER_LABEL: 1,
    }
    stored_strength = strengths.get(stored)
    required_strength = strengths.get(required)
    if stored_strength is None or required_strength is None:
        return stored == required
    return stored_strength >= required_strength


def find_fresh_evidence(
    session: Session,
    email: str,
    policy: VerificationPolicy,
    now: datetime,
    *,
    required_provider_label: str | None = None,
) -> ExactEmailVerification | None:
    """Return the newest reusable evidence for the exact address.

    Reuse is allowed only when the evidence is fresh under the active policy,
    was produced under that same policy version, and has provenance at least as
    strong as the caller requires. This prevents a live Agent execution from
    upgrading simulator evidence into a production-eligible result.
    """

    norm = normalize_email(email)
    if not norm:
        return None
    candidates = session.scalars(
        select(ExactEmailVerification)
        .where(
            ExactEmailVerification.email == norm,
            ExactEmailVerification.policy_version == policy.version,
        )
        .order_by(ExactEmailVerification.checked_at.desc())
    ).all()
    for candidate in candidates:
        if not _provider_provenance_satisfies(candidate.provider, required_provider_label):
            continue
        if policy.is_fresh(candidate.result, candidate.checked_at, now):
            return candidate
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
    # Set when the suppression ledger blocked this contact before any candidate
    # was generated or any provider work was queued (DAT-006). The reason is
    # truthful, e.g. "email opt_out" — never a silent drop.
    blocked: bool = False
    blocked_reason: str | None = None


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

    # Suppression gate (DAT-006): the ledger is consulted before a contact
    # advances toward outreach. A suppressed identity never generates a candidate
    # or queues a paid verification call; the block is explicit and truthful.
    decision = evaluate_suppression(session, email=contact.email, domain=contact.company_domain)
    if decision.blocked:
        return EnqueueOutcome(blocked=True, blocked_reason=decision.blocked_reason)

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
            provider=provider_name,
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
            origin="customer_operation",
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
    provider_name: str = "millionverifier",
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
        provider=provider_name,
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
        campaign_contact_id=job.campaign_contact_id,
        contact_id=job.contact_id,
        job_id=job.id,
        job_kind="verification_job",
        request_ref=job.idempotency_key,
        credits_remaining=credits_remaining,
        reason=reason,
        origin="customer_operation",
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
    domain = email.rsplit("@", 1)[-1] if "@" in email else None
    if (
        domain
        and mapped.result is EmailVerificationResult.CATCH_ALL
        and "simulator" not in provider_name
    ):
        session.add(
            MailDomainObservation(
                domain=domain,
                is_catch_all=True,
                accepts_all=True,
                raw_observation={
                    "provider": provider_name,
                    "verification_id": str(row.id),
                    "policy_version": policy.version,
                },
                observed_at=now,
            )
        )
        session.flush()
    return row


@dataclass(frozen=True)
class VerificationOutcome:
    """One classified verification attempt, with no queue side effects.

    This is what the verification domain knows after doing its work: what the
    provider said, what evidence exists, what it cost, and how any failure
    classifies. It deliberately says nothing about job status, retry timing or
    pipeline state — the Phase 2 Agent contract owns all of those, and this
    object is what its adapter translates.
    """

    email: str
    precise: EmailPreciseStatus
    result: EmailVerificationResult | None
    evidence: ExactEmailVerification | None
    reused: bool
    provider_called: bool
    # Simulated-vs-live provenance label, as stored on the evidence row.
    provider_label: str
    failure_class: VerificationFailureClass
    policy_version: str
    # Operator-readable and already redacted; None on success.
    reason: str | None = None
    attempt: VerificationAttempt | None = None

    @property
    def simulated(self) -> bool:
        """True when this outcome came from the network-free simulator."""

        return self.provider_label == SIMULATOR_PROVIDER_LABEL

    @property
    def is_address_evidence(self) -> bool:
        return self.evidence is not None


def verify_exact_address(
    session: Session,
    job: VerificationJob,
    *,
    provider: VerificationProvider,
    settings: Settings | None = None,
    policy: VerificationPolicy | None = None,
    reuse_fresh: bool = True,
    record_attempt: bool = True,
) -> VerificationOutcome:
    """Obtain one normalized verification outcome for one claimed job's address.

    Does every durable thing verification owns — reuse fresh evidence or make one
    provider call, store the address evidence, record usage and the neutral cost
    ledger, append the provider-facing attempt record — and then returns.

    It never sets a job status, schedules a retry, or writes pipeline state. That
    separation is the whole point: the Phase 2 worker owns the job lifecycle, and
    a domain service that also moved jobs would be a second orchestrator.

    ``reuse_fresh`` and the job's own ``input_reference["force_refresh"]`` both
    disable the cache-first shortcut. The instruction lives on the job so it
    survives whoever ends up executing it: an operator who asked for a fresh
    check must never be quietly handed the cached verdict.
    """

    settings = settings or get_settings()
    policy = policy or get_policy(settings)
    now = _now()
    provider_name = provider.name
    provider_label = evidence_provider_label(provider)
    email = job.email or ""

    def _attempt(
        *,
        provider_called: bool,
        failure_class: VerificationFailureClass,
        precise: EmailPreciseStatus,
        result: EmailVerificationResult | None = None,
        reused: bool = False,
        reason: str | None = None,
        evidence: ExactEmailVerification | None = None,
        evidence_provider: str | None = None,
    ) -> VerificationOutcome:
        effective_provider_label = evidence_provider or provider_label
        record = (
            job_attempts.record_attempt(
                session,
                job,
                started_at=now,
                finished_at=_now(),
                provider=effective_provider_label,
                provider_called=provider_called,
                reused_evidence=reused,
                failure_class=failure_class,
                precise_status=precise.value,
                verification_result=result,
                error_summary=reason,
                verification_id=evidence.id if evidence is not None else None,
                settings=settings,
            )
            if record_attempt
            else None
        )
        return VerificationOutcome(
            email=email,
            precise=precise,
            result=result,
            evidence=evidence,
            reused=reused,
            provider_called=provider_called,
            provider_label=effective_provider_label,
            failure_class=failure_class,
            policy_version=policy.version,
            reason=reason,
            attempt=record,
        )

    if not email:
        return _attempt(
            provider_called=False,
            failure_class=VerificationFailureClass.INVALID_INPUT,
            precise=EmailPreciseStatus.PROVIDER_ERROR,
            reason="verification job has no exact email address",
        )

    if jobs.lease_was_reclaimed(job):
        usage.record_usage(
            session,
            event_type=VerificationUsageEventType.RECOVERED,
            provider=provider_name,
            email=email,
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
            provider_name=provider_name,
            cache_status=UsageCacheStatus.MISS,
            charge_status=UsageChargeStatus.UNCERTAIN,
            now=now,
            result="uncertain_prior_attempt",
            reason="prior worker lease expired; a paid call may have completed before recovery",
        )

    # Cache reuse safety net: fresh evidence may have appeared since enqueue.
    may_reuse = reuse_fresh and not _force_refresh_requested(job)
    fresh = (
        find_fresh_evidence(
            session,
            email,
            policy,
            now,
            required_provider_label=provider_label,
        )
        if may_reuse
        else None
    )
    if fresh is not None:
        usage.record_usage(
            session,
            event_type=VerificationUsageEventType.CACHE_REUSE,
            provider=provider_name,
            email=email,
            contact_id=job.contact_id,
            job_id=job.id,
            result=fresh.result.value,
            reason="fresh evidence already present; skipped provider call",
        )
        _ledger_for_job(
            session,
            job,
            settings,
            provider_name=provider_name,
            cache_status=UsageCacheStatus.HIT,
            charge_status=UsageChargeStatus.NONE,
            now=now,
            result=fresh.result.value,
            reason="fresh evidence already present; skipped provider call",
        )
        precise = policy.precise_for_result(fresh.result, is_role=bool(fresh.is_role))
        return _attempt(
            provider_called=False,
            failure_class=VerificationFailureClass.NONE,
            precise=precise,
            result=fresh.result,
            reused=True,
            evidence=fresh,
            evidence_provider=fresh.provider,
        )

    # One provider call.
    try:
        response = provider.verify(email)
    except ProviderTransientError as raw_exc:
        # The live client redacts its own key before raising; redact again here so
        # a provider that forgets cannot write a credential into durable text that
        # the workbench renders (AGENTS.md: secrets never in logs).
        detail = redact_secret(str(raw_exc), settings.millionverifier_api_key)
        usage.record_usage(
            session,
            event_type=VerificationUsageEventType.TIMEOUT,
            provider=provider_name,
            email=email,
            contact_id=job.contact_id,
            job_id=job.id,
            reason=f"transport failure: {detail}",
        )
        _maybe_retry_usage(session, job, provider_name)
        _ledger_for_job(
            session,
            job,
            settings,
            provider_name=provider_name,
            cache_status=UsageCacheStatus.MISS,
            charge_status=UsageChargeStatus.NONE,
            now=now,
            result=None,
            reason=f"transport failure (no charge): {detail}",
        )
        return _attempt(
            provider_called=True,
            failure_class=VerificationFailureClass.TRANSIENT_PROVIDER,
            precise=EmailPreciseStatus.PROVIDER_ERROR,
            reason=f"transport failure: {detail}",
        )

    mapped = policy.map_response(response)

    if mapped.is_address_evidence:
        row = _store_evidence(
            session,
            email=email,
            mapped=mapped,
            response=response,
            policy=policy,
            contact_id=job.contact_id,
            # Evidence records simulated-vs-live provenance so a simulated result
            # is never displayed as an external verification (VER-007). Usage and
            # the neutral ledger keep the vendor label for cost correlation.
            provider_name=provider_label,
            now=now,
        )
        usage.record_usage(
            session,
            event_type=VerificationUsageEventType.CALL_MADE,
            provider=provider_name,
            email=email,
            contact_id=job.contact_id,
            job_id=job.id,
            result=mapped.result.value if mapped.result else None,
            credited=provider_name == "millionverifier" and mapped.credited,
            credits_remaining=response.credits,
            reason=mapped.reason,
        )
        # MillionVerifier confirms which outcomes consume a credit. DeBounce's
        # single-validation response does not confirm the charge for this exact
        # request, so it stays uncertain until usage/invoice reconciliation.
        charge_confirmed = provider_name == "millionverifier" and mapped.credited
        charge_uncertain = provider_name != "millionverifier"
        units = 1 if charge_confirmed else 0
        cost = _cost_per_credit(settings, provider_name) * units
        _ledger_for_job(
            session,
            job,
            settings,
            provider_name=provider_name,
            cache_status=UsageCacheStatus.MISS,
            charge_status=(
                UsageChargeStatus.UNCERTAIN
                if charge_uncertain
                else (UsageChargeStatus.CONFIRMED if charge_confirmed else UsageChargeStatus.NONE)
            ),
            now=now,
            units=units,
            estimated_cost=cost,
            result=mapped.result.value if mapped.result else None,
            credits_remaining=response.credits,
            reason=mapped.reason,
        )
        return _attempt(
            provider_called=True,
            failure_class=VerificationFailureClass.NONE,
            precise=mapped.precise,
            result=mapped.result,
            evidence=row,
        )

    if mapped.kind == "insufficient_credits":
        usage.record_usage(
            session,
            event_type=VerificationUsageEventType.INSUFFICIENT_CREDITS,
            provider=provider_name,
            email=email,
            contact_id=job.contact_id,
            job_id=job.id,
            credits_remaining=response.credits,
            reason=mapped.reason,
        )
        _ledger_for_job(
            session,
            job,
            settings,
            provider_name=provider_name,
            cache_status=UsageCacheStatus.MISS,
            charge_status=UsageChargeStatus.NONE,
            now=now,
            result=None,
            credits_remaining=response.credits,
            reason=mapped.reason,
        )
        return _attempt(
            provider_called=True,
            failure_class=VerificationFailureClass.INSUFFICIENT_CREDITS,
            precise=EmailPreciseStatus.INSUFFICIENT_CREDITS,
            reason=mapped.reason,
        )

    if mapped.retryable:  # transient provider error / result=error / unrecognised
        usage.record_usage(
            session,
            event_type=VerificationUsageEventType.PROVIDER_ERROR,
            provider=provider_name,
            email=email,
            contact_id=job.contact_id,
            job_id=job.id,
            reason=mapped.reason,
        )
        _maybe_retry_usage(session, job, provider_name)
        _ledger_for_job(
            session,
            job,
            settings,
            provider_name=provider_name,
            cache_status=UsageCacheStatus.MISS,
            charge_status=UsageChargeStatus.NONE,
            now=now,
            result=None,
            reason=f"provider error (no charge): {mapped.reason}",
        )
        return _attempt(
            provider_called=True,
            failure_class=VerificationFailureClass.TRANSIENT_PROVIDER,
            precise=EmailPreciseStatus.PROVIDER_ERROR,
            reason=mapped.reason,
        )

    # Non-retryable provider/config error: fail without paid evidence.
    usage.record_usage(
        session,
        event_type=VerificationUsageEventType.PROVIDER_ERROR,
        provider=provider_name,
        email=email,
        contact_id=job.contact_id,
        job_id=job.id,
        reason=mapped.reason,
    )
    _ledger_for_job(
        session,
        job,
        settings,
        provider_name=provider_name,
        cache_status=UsageCacheStatus.MISS,
        charge_status=UsageChargeStatus.NONE,
        now=now,
        result=None,
        reason=f"provider/config error (no charge): {mapped.reason}",
    )
    return _attempt(
        provider_called=True,
        failure_class=VerificationFailureClass.PERMANENT_PROVIDER,
        precise=EmailPreciseStatus.PROVIDER_ERROR,
        reason=mapped.reason,
    )


def _force_refresh_requested(job: VerificationJob) -> bool:
    """Whether this Agent Job was queued as a deliberate re-check.

    Carried in the Phase 2 ``input_reference`` rather than a dedicated column:
    the common Agent Job already provides a durable, structured place for a job's
    input, and adding a verification-only column beside it would duplicate what
    Phase 2 supplies.
    """

    return bool((job.input_reference or {}).get("force_refresh"))


def process_job(
    session: Session,
    job: VerificationJob,
    *,
    provider: VerificationProvider,
    settings: Settings | None = None,
    policy: VerificationPolicy | None = None,
    reuse_fresh: bool = True,
) -> VerificationJob:
    """Run one claimed job to a terminal or retry state through the legacy queue.

    Compatibility surface for the callers that predate the Phase 2 worker: the
    workbench verification console, ``run_worker``, and the deliberate live smoke
    command. It performs the domain work through :func:`verify_exact_address` and
    then applies the queue transitions those callers still expect.

    The Phase 2 Verification Agent adapter does **not** use this function. It
    calls :func:`verify_exact_address` and lets the common worker own every job
    transition, so retries, backoff and terminal failure have one owner.
    """

    settings = settings or get_settings()
    policy = policy or get_policy(settings)
    outcome = verify_exact_address(
        session,
        job,
        provider=provider,
        settings=settings,
        policy=policy,
        reuse_fresh=reuse_fresh,
    )

    if outcome.evidence is not None:
        return jobs.mark_succeeded(
            session,
            job,
            verification_id=outcome.evidence.id,
            outcome_status=outcome.precise.value,
        )
    if outcome.failure_class is VerificationFailureClass.TRANSIENT_PROVIDER:
        return jobs.schedule_retry(
            session,
            job,
            reason=outcome.reason or "transient provider failure",
            base=settings.verification_retry_base_seconds,
            cap=settings.verification_retry_max_seconds,
            outcome_status=EmailPreciseStatus.PROVIDER_ERROR.value,
        )
    return jobs.mark_failed(
        session,
        job,
        reason=outcome.reason or "verification produced no usable result",
        outcome_status=outcome.precise.value,
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

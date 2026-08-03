"""The durable queue for Company Intelligence production (CI-001).

A small queue, in the same shape as the Agent job queue that already works —
committed claim checkpoints, leases with expiry, bounded attempts, exponential
backoff, ``FOR UPDATE SKIP LOCKED`` so several workers stay independent — but a
**separate table**, and that separation is the design decision this module
exists to hold.

Why not the Campaign Contact Agent queue:

* Company Intelligence is **company-scoped**. The Agent queue's unit of work is
  a Campaign Contact. Classifying the same company once per contact would spend
  N model calls to learn one thing.
* The Agent queue's worker resolves every job through
  ``DEFAULT_ADAPTERS[job.agent_id]`` and requires a Campaign Contact, a Campaign
  and a per-contact stage projection. A company-scoped job has none of those.
* Joining it would need a new ``AgentIdentifier``, which
  ``_reconcile_campaign_controls`` iterates and ``get_agent_spec`` requires to be
  registered — so the new value would land in ``PIPELINE_ORDER`` and become a
  stage every enrolled Contact waits behind. That is the destabilisation the
  placement decision exists to avoid, and PostgreSQL cannot remove an enum value
  on downgrade either.

See ``docs/decisions/ADR-CI-001-pipeline-placement.md`` for the full assessment.

**Idempotency has two layers.** The queue refuses a second active job per
Company (a partial unique index, not a service check), and production itself
refuses a second version per input digest. So a duplicate enqueue is a no-op and
a duplicate execution is a no-op, independently — which is what makes a retried
or resumed backfill safe rather than merely unlikely to hurt.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.company_intelligence import (
    ACTIVE_INTELLIGENCE_JOB_STATUSES,
    CompanyIntelligenceJob,
)
from app.models.enums import IntelligenceJobStatus
from app.services.audit import record_audit_event

JOB_ACTOR = "system:company-intelligence"
TASK_KIND = "produce_company_intelligence"

#: Statuses a worker may claim.
CLAIMABLE_STATUSES: tuple[IntelligenceJobStatus, ...] = (
    IntelligenceJobStatus.PENDING,
    IntelligenceJobStatus.RETRY_SCHEDULED,
)

DEFAULT_MAX_ATTEMPTS = 3
RETRY_BASE_SECONDS = 60.0
RETRY_CAP_SECONDS = 900.0


class IntelligenceJobError(ValueError):
    """A queue operation that cannot be performed as asked."""


def _now() -> datetime:
    return datetime.now(UTC)


def idempotency_key(*, company_id: uuid.UUID, input_digest: str | None) -> str:
    """One key per (company, exact input).

    Keyed on the digest rather than on the company alone: a company whose
    research has moved on genuinely needs a second job, and a company whose
    research has not does not. When the digest is unknown — an operator asking
    for a run from a screen before the input is assembled — the key falls back to
    the company plus the task, and the active-job index does the rest.
    """

    if input_digest:
        return f"company-intelligence:{company_id}:{input_digest}"
    return f"company-intelligence:{company_id}:requested"


def enqueue(
    session: Session,
    *,
    company: Company,
    input_digest: str | None = None,
    producer_version: str | None = None,
    policy_version: str | None = None,
    priority: int = 100,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backfill_run_id: uuid.UUID | None = None,
    input_reference: dict[str, Any] | None = None,
    requested_by: str | None = None,
    available_at: datetime | None = None,
    actor: str = JOB_ACTOR,
) -> tuple[CompanyIntelligenceJob, bool]:
    """Queue one production intent. Returns ``(job, created)``.

    Never raises on a duplicate. An existing active job for the Company, or an
    existing job under the same key, is returned with ``created=False`` — an
    operator pressing a button twice and a backfill resuming over a company it
    already queued are the same event as far as the queue is concerned.
    """

    if max_attempts < 1 or max_attempts > 100:
        raise IntelligenceJobError("max_attempts must be between 1 and 100")

    key = idempotency_key(company_id=company.id, input_digest=input_digest)
    existing = session.scalars(
        select(CompanyIntelligenceJob).where(CompanyIntelligenceJob.idempotency_key == key)
    ).one_or_none()
    if existing is not None:
        return existing, False

    active = session.scalars(
        select(CompanyIntelligenceJob).where(
            CompanyIntelligenceJob.company_id == company.id,
            CompanyIntelligenceJob.status.in_(ACTIVE_INTELLIGENCE_JOB_STATUSES),
        )
    ).first()
    if active is not None:
        return active, False

    job = CompanyIntelligenceJob(
        company_id=company.id,
        task_kind=TASK_KIND,
        idempotency_key=key,
        status=IntelligenceJobStatus.PENDING,
        priority=priority,
        max_attempts=max_attempts,
        next_run_at=available_at or _now(),
        producer_version=producer_version,
        policy_version=policy_version,
        expected_input_digest=input_digest,
        backfill_run_id=backfill_run_id,
        input_reference=dict(input_reference or {}),
        requested_by=requested_by,
    )
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        # Lost a race, either on the key or on the one-active-job index. Whoever
        # won queued the same intent, so take theirs.
        winner = session.scalars(
            select(CompanyIntelligenceJob).where(CompanyIntelligenceJob.idempotency_key == key)
        ).one_or_none()
        if winner is None:
            winner = session.scalars(
                select(CompanyIntelligenceJob).where(
                    CompanyIntelligenceJob.company_id == company.id,
                    CompanyIntelligenceJob.status.in_(ACTIVE_INTELLIGENCE_JOB_STATUSES),
                )
            ).first()
        if winner is None:  # pragma: no cover - defensive
            raise
        return winner, False

    record_audit_event(
        session,
        actor=actor,
        action="company_intelligence.job_enqueued",
        entity_type="company_intelligence_job",
        entity_id=str(job.id),
        new_state=job.status.value,
        reason="company intelligence production queued",
        context={
            "company_id": str(company.id),
            "backfill_run_id": str(backfill_run_id) if backfill_run_id else None,
            "input_digest": input_digest,
        },
    )
    return job, True


def recover_expired_leases(
    session: Session, *, now: datetime | None = None
) -> list[CompanyIntelligenceJob]:
    """Return abandoned work to the queue, or fail it when attempts are spent.

    Recovery is part of every claim pass, so a worker that died needs no separate
    scheduler to clean up after it. A recovered job keeps a durable
    ``lease_expired`` marker, because "this crashed and was retried" and "this
    failed and was retried" are different stories.
    """

    moment = now or _now()
    stale = list(
        session.scalars(
            select(CompanyIntelligenceJob).where(
                CompanyIntelligenceJob.status.in_(
                    (IntelligenceJobStatus.LEASED, IntelligenceJobStatus.IN_PROGRESS)
                ),
                CompanyIntelligenceJob.lease_expires_at.is_not(None),
                CompanyIntelligenceJob.lease_expires_at < moment,
            )
        ).all()
    )
    for job in stale:
        job.lease_owner = None
        job.lease_expires_at = None
        job.error_class = "lease_expired"
        job.last_error = "the worker holding this job stopped reporting"
        if job.attempts >= job.max_attempts:
            job.status = IntelligenceJobStatus.FAILED
            job.finished_at = moment
        else:
            job.status = IntelligenceJobStatus.PENDING
            job.next_run_at = moment
    if stale:
        session.flush()
    return stale


def claim_next(
    session: Session,
    *,
    worker_id: str,
    lease_seconds: float = 300.0,
    now: datetime | None = None,
    recover: bool = True,
) -> CompanyIntelligenceJob | None:
    """Lease one due job, or return None.

    ``SKIP LOCKED`` so a second worker steps over a row the first is holding
    rather than blocking on it. The claim is a committed checkpoint in the
    caller's transaction: a process that dies after claiming leaves recoverable
    work, not a lost job.
    """

    clean_worker = worker_id.strip()
    if not clean_worker or len(clean_worker) > 100:
        raise IntelligenceJobError("worker_id must be 1 to 100 characters")
    if lease_seconds <= 0:
        raise IntelligenceJobError("lease_seconds must be positive")

    moment = now or _now()
    if recover:
        recover_expired_leases(session, now=moment)

    job = session.scalars(
        select(CompanyIntelligenceJob)
        .where(
            CompanyIntelligenceJob.status.in_(CLAIMABLE_STATUSES),
            CompanyIntelligenceJob.next_run_at <= moment,
        )
        .order_by(
            CompanyIntelligenceJob.priority.desc(),
            CompanyIntelligenceJob.next_run_at.asc(),
            CompanyIntelligenceJob.created_at.asc(),
            CompanyIntelligenceJob.id.asc(),
        )
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()
    if job is None:
        return None

    job.status = IntelligenceJobStatus.LEASED
    job.attempts += 1
    job.lease_owner = clean_worker
    job.lease_expires_at = moment + timedelta(seconds=lease_seconds)
    if job.error_class != "lease_expired":
        job.error = None
        job.error_class = None
        job.last_error = None
    if job.started_at is None:
        job.started_at = moment
    session.flush()
    return job


def mark_running(
    session: Session, *, job: CompanyIntelligenceJob, now: datetime | None = None
) -> CompanyIntelligenceJob:
    """Durable checkpoint before the slow part of the work begins."""

    job.status = IntelligenceJobStatus.IN_PROGRESS
    job.started_at = job.started_at or (now or _now())
    session.flush()
    return job


def mark_succeeded(
    session: Session,
    *,
    job: CompanyIntelligenceJob,
    result: dict[str, Any],
    now: datetime | None = None,
    actor: str = JOB_ACTOR,
) -> CompanyIntelligenceJob:
    """Close a job that produced (or truthfully reused) a version."""

    moment = now or _now()
    job.status = IntelligenceJobStatus.SUCCEEDED
    job.result = result
    job.error = None
    job.error_class = None
    job.last_error = None
    job.lease_owner = None
    job.lease_expires_at = None
    job.finished_at = moment
    session.flush()
    record_audit_event(
        session,
        actor=actor,
        action="company_intelligence.job_succeeded",
        entity_type="company_intelligence_job",
        entity_id=str(job.id),
        new_state=job.status.value,
        reason="company intelligence production finished",
        context={"company_id": str(job.company_id), **{k: v for k, v in result.items()}},
    )
    return job


def mark_failed(
    session: Session,
    *,
    job: CompanyIntelligenceJob,
    code: str,
    message: str,
    retryable: bool,
    detail: dict[str, Any] | None = None,
    now: datetime | None = None,
    actor: str = JOB_ACTOR,
) -> CompanyIntelligenceJob:
    """Fail a job, scheduling a retry only when one could plausibly help.

    ``retryable`` is the honest question "would running this again work?", not
    "was this bad?". A malformed answer is retryable; a company with no dossier
    is not, and retrying it three times only delays telling the operator why.
    """

    moment = now or _now()
    job.error = {"code": code, "message": message[:2000], "detail": detail or {}}
    job.error_class = code
    job.last_error = message[:2000]
    job.lease_owner = None
    job.lease_expires_at = None

    if retryable and job.attempts < job.max_attempts:
        job.status = IntelligenceJobStatus.RETRY_SCHEDULED
        backoff = min(RETRY_BASE_SECONDS * (2 ** (job.attempts - 1)), RETRY_CAP_SECONDS)
        job.next_run_at = moment + timedelta(seconds=backoff)
    else:
        job.status = IntelligenceJobStatus.FAILED
        job.finished_at = moment
    session.flush()
    record_audit_event(
        session,
        actor=actor,
        action="company_intelligence.job_failed",
        entity_type="company_intelligence_job",
        entity_id=str(job.id),
        new_state=job.status.value,
        reason=message[:500],
        context={
            "company_id": str(job.company_id),
            "code": code,
            "retryable": retryable,
            "attempts": job.attempts,
            "max_attempts": job.max_attempts,
        },
    )
    return job


def cancel(
    session: Session,
    *,
    job: CompanyIntelligenceJob,
    reason: str,
    actor: str = JOB_ACTOR,
    now: datetime | None = None,
) -> CompanyIntelligenceJob:
    """Stop a job that has not finished. Terminal, and audited."""

    if job.status in (
        IntelligenceJobStatus.SUCCEEDED,
        IntelligenceJobStatus.FAILED,
        IntelligenceJobStatus.CANCELLED,
    ):
        return job
    job.status = IntelligenceJobStatus.CANCELLED
    job.lease_owner = None
    job.lease_expires_at = None
    job.finished_at = now or _now()
    job.last_error = reason[:2000]
    session.flush()
    record_audit_event(
        session,
        actor=actor,
        action="company_intelligence.job_cancelled",
        entity_type="company_intelligence_job",
        entity_id=str(job.id),
        new_state=job.status.value,
        reason=reason[:500],
        context={"company_id": str(job.company_id)},
    )
    return job


def active_job_for(session: Session, *, company_id: uuid.UUID) -> CompanyIntelligenceJob | None:
    """The in-flight job for one Company, if there is one."""

    return session.scalars(
        select(CompanyIntelligenceJob).where(
            CompanyIntelligenceJob.company_id == company_id,
            CompanyIntelligenceJob.status.in_(ACTIVE_INTELLIGENCE_JOB_STATUSES),
        )
    ).first()


def queue_counts(session: Session) -> dict[str, int]:
    """``{status: count}`` for the whole queue, for the Admin surface."""

    from sqlalchemy import func

    rows = session.execute(
        select(CompanyIntelligenceJob.status, func.count(CompanyIntelligenceJob.id)).group_by(
            CompanyIntelligenceJob.status
        )
    ).all()
    counts = {status.value: 0 for status in IntelligenceJobStatus}
    for status, count in rows:
        counts[status.value] = int(count)
    return counts

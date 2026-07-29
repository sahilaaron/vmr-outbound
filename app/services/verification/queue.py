"""Postgres-backed verification job queue (VER-005 / OPS-002 / OPS-001).

Durable, idempotent, resumable background work with no external broker:

* **Idempotent enqueue** — a unique ``idempotency_key`` plus a partial unique
  index over active jobs mean concurrent duplicate requests collapse to a single
  job, so an address is verified (and paid for) at most once at a time.
* **Leased claiming** — ``SELECT … FOR UPDATE SKIP LOCKED`` hands each pending job
  to exactly one worker and stamps a lease. Concurrent workers never grab the same
  job.
* **Bounded retries** — only transient failures reschedule, with exponential
  backoff and jitter, up to ``max_attempts``; a definite result never retries.
* **Interrupted-worker recovery** — a job whose lease expires (its worker died) is
  reclaimable again, so no work is stranded.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.enums import AgentIdentifier, VerificationJobStatus
from app.models.verification_job import ACTIVE_JOB_STATUSES, VerificationJob
from app.services.agents import jobs as agent_jobs
from app.services.imports.normalization import normalize_email


def _now() -> datetime:
    return datetime.now(UTC)


def enqueue_verification(
    session: Session,
    *,
    email: str,
    policy_version: str,
    max_attempts: int,
    contact_id: uuid.UUID | None = None,
    campaign_id: uuid.UUID | None = None,
) -> tuple[VerificationJob, bool]:
    """Idempotently enqueue a verification job for an exact address.

    Returns ``(job, created)``. If an active job already exists for the address —
    or a concurrent caller wins the insert race — the existing job is returned with
    ``created=False``, so duplicate requests never create duplicate paid work.
    """

    norm = normalize_email(email)
    if not norm:
        raise ValueError("cannot enqueue verification for an empty address")

    key = f"{policy_version}:{norm}"

    existing = _find_reusable_job(session, key=key, email=norm)
    if existing is not None:
        return existing, False

    job = VerificationJob(
        agent_id=AgentIdentifier.VERIFICATION,
        task_kind="verify_exact_email",
        entity_type="email",
        email=norm,
        contact_id=contact_id,
        campaign_id=campaign_id,
        idempotency_key=key,
        policy_version=policy_version,
        status=VerificationJobStatus.PENDING,
        attempts=0,
        max_attempts=max_attempts,
        next_run_at=_now(),
        input_reference={"email": norm, "policy_version": policy_version},
    )
    savepoint = session.begin_nested()
    try:
        session.add(job)
        session.flush()
        savepoint.commit()
        return job, True
    except IntegrityError:
        # Lost the race on the unique idempotency key or the active-email index.
        savepoint.rollback()
        winner = _find_reusable_job(session, key=key, email=norm)
        if winner is None:  # pragma: no cover - defensive
            raise
        return winner, False


def _find_reusable_job(session: Session, *, key: str, email: str) -> VerificationJob | None:
    by_key = session.scalars(
        select(VerificationJob).where(VerificationJob.idempotency_key == key)
    ).first()
    if by_key is not None:
        return by_key
    return session.scalars(
        select(VerificationJob).where(
            VerificationJob.agent_id == AgentIdentifier.VERIFICATION,
            VerificationJob.email == email,
            VerificationJob.status.in_(ACTIVE_JOB_STATUSES),
        )
    ).first()


def claim_next_job(
    session: Session, *, worker_id: str, lease_seconds: float, now: datetime | None = None
) -> VerificationJob | None:
    """Compatibility claim that uses the common queue, then starts immediately."""

    leased = agent_jobs.claim_next_job(
        session,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        agent_ids=(AgentIdentifier.VERIFICATION,),
        now=now,
    )
    if leased is None:
        return None
    reclaimed = agent_jobs.lease_was_reclaimed(leased)
    agent_jobs.start_job(session, leased, worker_id=worker_id, now=now)
    leased.__dict__["_reclaimed"] = reclaimed
    return leased


def lease_was_reclaimed(job: VerificationJob) -> bool:
    """Expose the common queue's durable recovery marker to verification usage."""

    return agent_jobs.lease_was_reclaimed(job)


def compute_backoff(attempts: int, *, base: float, cap: float) -> float:
    """Exponential backoff (seconds) with bounded jitter for retry *attempts*."""

    return agent_jobs.compute_backoff(attempts, base=base, cap=cap)


def schedule_retry(
    session: Session,
    job: VerificationJob,
    *,
    reason: str,
    base: float,
    cap: float,
    outcome_status: str | None = None,
    now: datetime | None = None,
) -> VerificationJob:
    """Reschedule a transient failure, or fail the job if attempts are exhausted."""

    agent_jobs.schedule_retry(
        session,
        job,
        error_class="verification_transient",
        reason=reason,
        base_seconds=base,
        cap_seconds=cap,
        now=now,
    )
    if job.status is VerificationJobStatus.FAILED:
        job.outcome_status = outcome_status
    session.flush()
    return job


def mark_succeeded(
    session: Session,
    job: VerificationJob,
    *,
    verification_id: uuid.UUID | None,
    outcome_status: str,
    now: datetime | None = None,
) -> VerificationJob:
    job.verification_id = verification_id
    job.outcome_status = outcome_status
    return agent_jobs.mark_completed(
        session,
        job,
        result={
            "domain_outcome": "exact_email_verification",
            "verification_id": str(verification_id) if verification_id else None,
            "outcome_status": outcome_status,
        },
        outcome_committed=True,
        now=now,
    )


def mark_failed(
    session: Session,
    job: VerificationJob,
    *,
    reason: str,
    outcome_status: str,
    now: datetime | None = None,
) -> VerificationJob:
    job.outcome_status = outcome_status
    return agent_jobs.mark_failed(
        session,
        job,
        error_class="verification_failed",
        reason=reason,
        now=now,
    )


def recover_stale_jobs(session: Session, *, now: datetime | None = None) -> list[VerificationJob]:
    """Reset jobs whose worker lease expired back to PENDING (explicit recovery sweep).

    Returns the reclaimed jobs. ``claim_next_job`` can also pick these up directly;
    this sweep exists so recovery can be run and observed on demand.
    """

    return agent_jobs.recover_expired_leases(
        session,
        now=now,
        agent_ids=(AgentIdentifier.VERIFICATION,),
    )

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

import random
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.enums import VerificationJobStatus
from app.models.verification_job import ACTIVE_JOB_STATUSES, VerificationJob
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
        email=norm,
        contact_id=contact_id,
        idempotency_key=key,
        policy_version=policy_version,
        status=VerificationJobStatus.PENDING,
        attempts=0,
        max_attempts=max_attempts,
        next_run_at=_now(),
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
            VerificationJob.email == email,
            VerificationJob.status.in_(ACTIVE_JOB_STATUSES),
        )
    ).first()


def claim_next_job(
    session: Session, *, worker_id: str, lease_seconds: float, now: datetime | None = None
) -> VerificationJob | None:
    """Claim the next runnable job for *worker_id*, stamping a lease.

    Runnable = a PENDING/RETRY_SCHEDULED job due now, or an IN_PROGRESS job whose
    lease has expired (its worker died — reclaimed here). ``SKIP LOCKED`` keeps
    concurrent workers from colliding.
    """

    now = now or _now()
    stmt = (
        select(VerificationJob)
        .where(
            or_(
                (
                    VerificationJob.status.in_(
                        [VerificationJobStatus.PENDING, VerificationJobStatus.RETRY_SCHEDULED]
                    )
                )
                & (VerificationJob.next_run_at <= now),
                (VerificationJob.status == VerificationJobStatus.IN_PROGRESS)
                & (VerificationJob.lease_expires_at.is_not(None))
                & (VerificationJob.lease_expires_at < now),
            )
        )
        .order_by(VerificationJob.next_run_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = session.scalars(stmt).first()
    if job is None:
        return None

    reclaimed = job.status == VerificationJobStatus.IN_PROGRESS
    job.status = VerificationJobStatus.IN_PROGRESS
    job.attempts += 1
    job.lease_owner = worker_id
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
    session.flush()
    # Annotate whether this claim reclaimed a dead worker's job (used for a
    # RECOVERED usage event by the caller).
    job.__dict__["_reclaimed"] = reclaimed
    return job


def compute_backoff(attempts: int, *, base: float, cap: float) -> float:
    """Exponential backoff (seconds) with bounded jitter for retry *attempts*."""

    exp = min(base * (2 ** max(0, attempts - 1)), cap)
    jitter = random.uniform(0, exp * 0.25)  # noqa: S311 - jitter, not security
    return float(min(exp + jitter, cap * 1.25))


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

    now = now or _now()
    job.last_error = reason
    job.lease_owner = None
    job.lease_expires_at = None
    if job.attempts >= job.max_attempts:
        job.status = VerificationJobStatus.FAILED
        job.finished_at = now
        job.outcome_status = outcome_status
    else:
        delay = compute_backoff(job.attempts, base=base, cap=cap)
        job.status = VerificationJobStatus.RETRY_SCHEDULED
        job.next_run_at = now + timedelta(seconds=delay)
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
    now = now or _now()
    job.status = VerificationJobStatus.SUCCEEDED
    job.finished_at = now
    job.verification_id = verification_id
    job.outcome_status = outcome_status
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error = None
    session.flush()
    return job


def mark_failed(
    session: Session,
    job: VerificationJob,
    *,
    reason: str,
    outcome_status: str,
    now: datetime | None = None,
) -> VerificationJob:
    now = now or _now()
    job.status = VerificationJobStatus.FAILED
    job.finished_at = now
    job.last_error = reason
    job.outcome_status = outcome_status
    job.lease_owner = None
    job.lease_expires_at = None
    session.flush()
    return job


def recover_stale_jobs(session: Session, *, now: datetime | None = None) -> list[VerificationJob]:
    """Reset jobs whose worker lease expired back to PENDING (explicit recovery sweep).

    Returns the reclaimed jobs. ``claim_next_job`` can also pick these up directly;
    this sweep exists so recovery can be run and observed on demand.
    """

    now = now or _now()
    stale = list(
        session.scalars(
            select(VerificationJob)
            .where(
                VerificationJob.status == VerificationJobStatus.IN_PROGRESS,
                VerificationJob.lease_expires_at.is_not(None),
                VerificationJob.lease_expires_at < now,
            )
            .with_for_update(skip_locked=True)
        ).all()
    )
    for job in stale:
        job.status = VerificationJobStatus.PENDING
        job.lease_owner = None
        job.lease_expires_at = None
        job.next_run_at = now
        job.last_error = "reclaimed after worker lease expired"
    if stale:
        session.flush()
    return stale

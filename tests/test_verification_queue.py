"""VER-005 / OPS-001 / OPS-002: queue idempotency, leasing, retries, recovery."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.models.enums import VerificationJobStatus
from app.models.verification_job import VerificationJob
from app.services.verification import queue as jobs
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

POLICY = "ver-1"


def test_enqueue_is_idempotent_same_address(db_session: Session) -> None:
    j1, c1 = jobs.enqueue_verification(
        db_session, email="a@b.com", policy_version=POLICY, max_attempts=4
    )
    j2, c2 = jobs.enqueue_verification(
        db_session, email="A@b.com", policy_version=POLICY, max_attempts=4
    )
    assert c1 is True and c2 is False
    assert j1.id == j2.id


def test_active_email_partial_unique_blocks_duplicate_job(db_session: Session) -> None:
    jobs.enqueue_verification(db_session, email="a@b.com", policy_version=POLICY, max_attempts=4)
    # A different idempotency key but the same active address must be rejected by
    # the partial unique index — the DB guarantee behind "no duplicate paid calls".
    dup = VerificationJob(
        email="a@b.com",
        idempotency_key="other-key:a@b.com",
        policy_version="ver-2",
        status=VerificationJobStatus.PENDING,
        attempts=0,
        max_attempts=4,
        next_run_at=datetime.now(UTC),
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_claim_leases_one_job_and_increments_attempts(db_session: Session) -> None:
    job, _ = jobs.enqueue_verification(
        db_session, email="a@b.com", policy_version=POLICY, max_attempts=4
    )
    claimed = jobs.claim_next_job(db_session, worker_id="w1", lease_seconds=60)
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status == VerificationJobStatus.IN_PROGRESS
    assert claimed.attempts == 1
    assert claimed.lease_owner == "w1"
    # Nothing else claimable now.
    assert jobs.claim_next_job(db_session, worker_id="w2", lease_seconds=60) is None


def test_retry_schedules_backoff_then_fails_when_exhausted(db_session: Session) -> None:
    job, _ = jobs.enqueue_verification(
        db_session, email="a@b.com", policy_version=POLICY, max_attempts=2
    )
    now = datetime.now(UTC)
    jobs.claim_next_job(db_session, worker_id="w1", lease_seconds=60, now=now)
    jobs.schedule_retry(db_session, job, reason="boom", base=30, cap=1800, now=now)
    assert job.status == VerificationJobStatus.RETRY_SCHEDULED
    assert job.next_run_at > now
    # Second attempt exhausts max_attempts=2.
    jobs.claim_next_job(
        db_session, worker_id="w1", lease_seconds=60, now=job.next_run_at + timedelta(seconds=1)
    )
    jobs.schedule_retry(db_session, job, reason="boom2", base=30, cap=1800)
    assert job.status == VerificationJobStatus.FAILED


def test_backoff_grows_and_is_capped() -> None:
    b1 = jobs.compute_backoff(1, base=30, cap=1800)
    b3 = jobs.compute_backoff(3, base=30, cap=1800)
    assert b3 > b1
    assert jobs.compute_backoff(20, base=30, cap=1800) <= 1800 * 1.25


def test_recover_stale_jobs_resets_expired_lease(db_session: Session) -> None:
    job, _ = jobs.enqueue_verification(
        db_session, email="a@b.com", policy_version=POLICY, max_attempts=4
    )
    # A worker claims the job, then dies. We simulate lease expiry by sweeping
    # from a point in the future (past its 60s lease).
    jobs.claim_next_job(db_session, worker_id="dead", lease_seconds=60)
    assert job.status == VerificationJobStatus.IN_PROGRESS
    future = datetime.now(UTC) + timedelta(hours=1)
    recovered = jobs.recover_stale_jobs(db_session, now=future)
    assert job in recovered
    assert job.status == VerificationJobStatus.PENDING
    assert job.lease_owner is None


def test_expired_lease_is_reclaimable_by_claim(db_session: Session) -> None:
    job, _ = jobs.enqueue_verification(
        db_session, email="a@b.com", policy_version=POLICY, max_attempts=4
    )
    jobs.claim_next_job(db_session, worker_id="dead", lease_seconds=60)
    future = datetime.now(UTC) + timedelta(hours=1)
    reclaimed = jobs.claim_next_job(db_session, worker_id="w2", lease_seconds=60, now=future)
    assert reclaimed is not None
    assert reclaimed.id == job.id
    assert reclaimed.__dict__.get("_reclaimed") is True
    assert reclaimed.attempts == 2  # first (dead) + reclaim

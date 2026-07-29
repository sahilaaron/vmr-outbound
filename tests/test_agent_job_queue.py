"""Common PostgreSQL Agent queue: durability, concurrency, retries, and recovery."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.db.session import engine
from app.models.enums import AgentIdentifier, AgentJobStatus
from app.models.verification_job import AgentJob
from app.services.agents import jobs
from sqlalchemy.orm import Session


def _enqueue(
    db: Session,
    *,
    key: str = "phase2-job",
    max_attempts: int = 3,
    input_reference: dict[str, object] | None = None,
    available_at: datetime | None = None,
) -> AgentJob:
    job, _ = jobs.enqueue_job(
        db,
        agent_id=AgentIdentifier.IDENTITY,
        idempotency_key=key,
        task_kind="advance_campaign_contact",
        max_attempts=max_attempts,
        entity_type="test",
        input_reference=input_reference,
        available_at=available_at,
    )
    return job


def _status(job: AgentJob) -> AgentJobStatus:
    """Read mutable ORM state without mypy retaining an earlier assertion narrow."""

    return job.status


def test_enqueue_is_idempotent_and_key_reuse_conflicts(db_session: Session) -> None:
    first = _enqueue(db_session, input_reference={"contact_id": "one"})
    replay, created = jobs.enqueue_job(
        db_session,
        agent_id=AgentIdentifier.IDENTITY,
        idempotency_key="phase2-job",
        task_kind="advance_campaign_contact",
        max_attempts=3,
        entity_type="test",
        input_reference={"contact_id": "one"},
    )
    assert created is False and replay.id == first.id

    with pytest.raises(jobs.JobIdempotencyConflict):
        jobs.enqueue_job(
            db_session,
            agent_id=AgentIdentifier.IDENTITY,
            idempotency_key="phase2-job",
            task_kind="advance_campaign_contact",
            max_attempts=3,
            entity_type="test",
            input_reference={"contact_id": "different"},
        )


def test_claim_leases_then_starts_one_job(db_session: Session) -> None:
    job = _enqueue(db_session)
    claimed = jobs.claim_next_job(
        db_session,
        worker_id="worker-a",
        lease_seconds=60,
    )
    assert claimed is not None and claimed.id == job.id
    assert _status(claimed) is AgentJobStatus.LEASED
    assert claimed.attempts == 1
    assert claimed.lease_owner == "worker-a"

    jobs.start_job(db_session, claimed, worker_id="worker-a")
    assert _status(claimed) is AgentJobStatus.IN_PROGRESS


def test_claim_job_targets_one_exact_job_without_consuming_neighbour(
    db_session: Session,
) -> None:
    now = datetime.now(UTC)
    first = _enqueue(db_session, key="first", available_at=now)
    second = _enqueue(db_session, key="second", available_at=now)

    claimed = jobs.claim_job(
        db_session,
        job_id=second.id,
        worker_id="targeted-worker",
        lease_seconds=60,
        now=now,
    )

    assert claimed is second
    assert _status(second) is AgentJobStatus.LEASED
    assert _status(first) is AgentJobStatus.PENDING


def test_concurrent_claim_uses_skip_locked(
    committed_session: Session,
) -> None:
    _enqueue(committed_session)
    committed_session.commit()
    first_session = Session(bind=engine, expire_on_commit=False)
    second_session = Session(bind=engine, expire_on_commit=False)
    try:
        first = jobs.claim_next_job(
            first_session,
            worker_id="worker-a",
            lease_seconds=60,
        )
        second = jobs.claim_next_job(
            second_session,
            worker_id="worker-b",
            lease_seconds=60,
        )
        assert first is not None
        assert second is None
    finally:
        first_session.rollback()
        second_session.rollback()
        first_session.close()
        second_session.close()


def test_retry_backoff_and_limit_are_durable(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.agents.jobs.random.uniform", lambda _low, _high: 0.0)
    job = _enqueue(db_session, max_attempts=2)
    now = datetime.now(UTC)
    first = jobs.claim_next_job(
        db_session,
        worker_id="worker",
        lease_seconds=60,
        now=now,
    )
    assert first is job
    jobs.start_job(db_session, job, worker_id="worker", now=now)
    jobs.schedule_retry(
        db_session,
        job,
        error_class="provider_timeout",
        reason="temporary timeout",
        base_seconds=30,
        cap_seconds=900,
        now=now,
    )
    assert _status(job) is AgentJobStatus.RETRY_SCHEDULED
    assert job.next_run_at == now + timedelta(seconds=30)

    second = jobs.claim_next_job(
        db_session,
        worker_id="worker",
        lease_seconds=60,
        now=job.next_run_at,
    )
    assert second is job
    jobs.start_job(db_session, job, worker_id="worker", now=job.next_run_at)
    jobs.schedule_retry(
        db_session,
        job,
        error_class="provider_timeout",
        reason="still unavailable",
        base_seconds=30,
        cap_seconds=900,
        now=job.next_run_at,
    )
    assert _status(job) is AgentJobStatus.FAILED
    assert job.error is not None and job.error["retryable"] is False


def test_lease_expiry_recovers_or_fails_at_attempt_limit(db_session: Session) -> None:
    now = datetime.now(UTC)
    recoverable = _enqueue(
        db_session, key="recoverable", max_attempts=2, available_at=now
    )
    jobs.claim_next_job(
        db_session,
        worker_id="dead-worker",
        lease_seconds=10,
        now=now,
    )
    recovered = jobs.recover_expired_leases(
        db_session,
        now=now + timedelta(seconds=11),
    )
    assert recoverable in recovered
    assert _status(recoverable) is AgentJobStatus.PENDING
    assert recoverable.error_class == "lease_expired"

    reclaimed = jobs.claim_next_job(
        db_session,
        worker_id="replacement",
        lease_seconds=10,
        now=now + timedelta(seconds=11),
    )
    assert reclaimed is recoverable
    assert reclaimed.__dict__.get("_reclaimed") is True
    assert reclaimed.attempts == 2

    exhausted = _enqueue(
        db_session, key="exhausted", max_attempts=1, available_at=now
    )
    jobs.claim_next_job(
        db_session,
        worker_id="dead-worker",
        lease_seconds=10,
        now=now,
    )
    jobs.recover_expired_leases(
        db_session,
        now=now + timedelta(seconds=11),
    )
    assert _status(exhausted) is AgentJobStatus.FAILED
    assert exhausted.error is not None and exhausted.error["retryable"] is False


def test_job_survives_session_restart_and_is_reclaimed(
    committed_session: Session,
) -> None:
    now = datetime.now(UTC)
    job = _enqueue(committed_session, max_attempts=3, available_at=now)
    committed_session.commit()

    first_worker = Session(bind=engine, expire_on_commit=False)
    try:
        claimed = jobs.claim_next_job(
            first_worker,
            worker_id="worker-before-restart",
            lease_seconds=10,
            now=now,
        )
        assert claimed is not None and claimed.id == job.id
        first_worker.commit()
    finally:
        first_worker.close()

    replacement = Session(bind=engine, expire_on_commit=False)
    try:
        reclaimed = jobs.claim_next_job(
            replacement,
            worker_id="worker-after-restart",
            lease_seconds=10,
            now=now + timedelta(seconds=11),
        )
        assert reclaimed is not None and reclaimed.id == job.id
        assert reclaimed.attempts == 2
        assert reclaimed.__dict__.get("_reclaimed") is True
        replacement.commit()
    finally:
        replacement.close()

    observer = Session(bind=engine, expire_on_commit=False)
    try:
        durable_reclaim = observer.get(AgentJob, job.id)
        assert durable_reclaim is not None
        assert jobs.lease_was_reclaimed(durable_reclaim) is True
    finally:
        observer.close()

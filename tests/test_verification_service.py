"""VER wiring: process_job flows, evidence, usage, cache reuse, idempotency."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.models.contact import Contact
from app.models.email_evidence import ExactEmailVerification
from app.models.enums import (
    EmailVerificationResult,
    VerificationJobStatus,
    VerificationUsageEventType,
)
from app.models.verification_job import VerificationJob
from app.models.verification_usage import VerificationUsageEvent
from app.services.verification import queue as jobs
from app.services.verification import service
from app.services.verification.provider import ProviderResponse, ProviderTransientError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

POLICY = "ver-1"


class CountingProvider:
    """Wraps the simulator and counts real 'calls' (would-be paid requests)."""

    name = "millionverifier"

    def __init__(self, inner) -> None:  # type: ignore[no-untyped-def]
        self.inner = inner
        self.calls = 0

    def verify(self, email: str) -> ProviderResponse:
        self.calls += 1
        return self.inner.verify(email)


class ScriptedProvider:
    """Returns queued responses/exceptions in order (for retry testing)."""

    name = "millionverifier"

    def __init__(self, script) -> None:  # type: ignore[no-untyped-def]
        self.script = list(script)
        self.calls = 0

    def verify(self, email: str) -> ProviderResponse:
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _enqueue_and_run(session: Session, provider, email: str):  # type: ignore[no-untyped-def]
    job, _ = jobs.enqueue_verification(session, email=email, policy_version=POLICY, max_attempts=4)
    claimed = jobs.claim_next_job(session, worker_id="w", lease_seconds=60)
    return service.process_job(session, claimed, provider=provider)


def _count_usage(session: Session, event_type: VerificationUsageEventType) -> int:
    return (
        session.scalar(select(func.count()).where(VerificationUsageEvent.event_type == event_type))
        or 0
    )


def test_valid_result_stores_evidence_and_succeeds(db_session: Session) -> None:
    provider = CountingProvider(service.get_provider(get_settings()))
    job = _enqueue_and_run(db_session, provider, "ok@acme.com")
    assert job.status == VerificationJobStatus.SUCCEEDED
    assert provider.calls == 1
    ev = db_session.scalars(
        select(ExactEmailVerification).where(ExactEmailVerification.email == "ok@acme.com")
    ).first()
    assert ev is not None
    assert ev.result == EmailVerificationResult.VALID
    assert ev.policy_version == POLICY
    assert _count_usage(db_session, VerificationUsageEventType.CALL_MADE) == 1


def test_catch_all_and_unknown_are_not_billed(db_session: Session) -> None:
    provider = service.get_provider(get_settings())
    _enqueue_and_run(db_session, provider, "x@catchall.example")
    _enqueue_and_run(db_session, provider, "unknown@acme.com")
    billed = db_session.scalar(
        select(func.count()).where(
            VerificationUsageEvent.event_type == VerificationUsageEventType.CALL_MADE,
            VerificationUsageEvent.credited.is_(True),
        )
    )
    assert billed == 0


def test_insufficient_credits_fails_without_evidence(db_session: Session) -> None:
    provider = service.get_provider(get_settings())
    job = _enqueue_and_run(db_session, provider, "nocredits@acme.com")
    assert job.status == VerificationJobStatus.FAILED
    assert job.outcome_status == "insufficient_credits"
    # No address evidence was written.
    assert (
        db_session.scalar(
            select(func.count()).where(ExactEmailVerification.email == "nocredits@acme.com")
        )
        == 0
    )
    assert _count_usage(db_session, VerificationUsageEventType.INSUFFICIENT_CREDITS) == 1


def test_transient_failure_retries_then_succeeds(db_session: Session) -> None:
    ok = ProviderResponse(email="a@acme.com", result="ok", resultcode=1, credits=5)
    provider = ScriptedProvider([ProviderTransientError("timeout"), ok])
    job, _ = jobs.enqueue_verification(
        db_session, email="a@acme.com", policy_version=POLICY, max_attempts=4
    )
    # First attempt: transient -> retry scheduled.
    claimed = jobs.claim_next_job(db_session, worker_id="w", lease_seconds=60)
    service.process_job(db_session, claimed, provider=provider)
    assert job.status == VerificationJobStatus.RETRY_SCHEDULED
    # Advance past backoff and run again -> success.
    claimed2 = jobs.claim_next_job(
        db_session, worker_id="w", lease_seconds=60, now=job.next_run_at + timedelta(seconds=1)
    )
    service.process_job(db_session, claimed2, provider=provider)
    assert job.status == VerificationJobStatus.SUCCEEDED
    assert _count_usage(db_session, VerificationUsageEventType.TIMEOUT) == 1


def test_cache_reuse_avoids_second_paid_call(db_session: Session) -> None:
    provider = CountingProvider(service.get_provider(get_settings()))
    c = Contact(
        first_name="Jane",
        last_name="Doe",
        company_name="Acme",
        company_domain="acme.com",
        email="ok@acme.com",
        natural_key=f"jane|doe|{uuid.uuid4()}",
    )
    db_session.add(c)
    db_session.flush()
    settings = get_settings()
    # First verification makes one call.
    o1 = service.prepare_and_enqueue_contact(db_session, c, settings=settings)
    service.run_worker(db_session, provider=provider, settings=settings)
    assert provider.calls == 1
    assert o1.reused_evidence is None
    # Second time: fresh evidence exists -> reuse, no new call.
    o2 = service.prepare_and_enqueue_contact(db_session, c, settings=settings)
    assert o2.reused_evidence is not None
    assert provider.calls == 1
    assert _count_usage(db_session, VerificationUsageEventType.CACHE_REUSE) >= 1


def test_stale_evidence_triggers_new_call(db_session: Session) -> None:
    provider = CountingProvider(service.get_provider(get_settings()))
    # Seed stale valid evidence (400 days old).
    old = ExactEmailVerification(
        email="ok@acme.com",
        result=EmailVerificationResult.VALID,
        provider="millionverifier",
        policy_version=POLICY,
        checked_at=datetime.now(UTC) - timedelta(days=400),
    )
    db_session.add(old)
    db_session.flush()
    c = Contact(
        first_name="Jane",
        last_name="Doe",
        company_name="Acme",
        company_domain="acme.com",
        email="ok@acme.com",
        natural_key=f"jane|doe|{uuid.uuid4()}",
    )
    db_session.add(c)
    db_session.flush()
    settings = get_settings()
    out = service.prepare_and_enqueue_contact(db_session, c, settings=settings)
    assert out.reused_evidence is None  # stale -> not reused
    assert out.job is not None
    service.run_worker(db_session, provider=provider, settings=settings)
    assert provider.calls == 1


def test_duplicate_enqueue_makes_max_one_paid_call(db_session: Session) -> None:
    provider = CountingProvider(service.get_provider(get_settings()))
    # Simulate two concurrent duplicate requests for the same address.
    j1, c1 = jobs.enqueue_verification(
        db_session, email="ok@acme.com", policy_version=POLICY, max_attempts=4
    )
    j2, c2 = jobs.enqueue_verification(
        db_session, email="ok@acme.com", policy_version=POLICY, max_attempts=4
    )
    assert j1.id == j2.id and c2 is False
    service.run_worker(db_session, provider=provider, settings=get_settings())
    assert provider.calls == 1
    total_jobs = db_session.scalar(
        select(func.count()).where(VerificationJob.email == "ok@acme.com")
    )
    assert total_jobs == 1


def test_reclaimed_job_records_recovered_event(db_session: Session) -> None:
    provider = service.get_provider(get_settings())
    job, _ = jobs.enqueue_verification(
        db_session, email="ok@acme.com", policy_version=POLICY, max_attempts=4
    )
    jobs.claim_next_job(db_session, worker_id="dead", lease_seconds=60)
    future = datetime.now(UTC) + timedelta(hours=1)
    reclaimed = jobs.claim_next_job(db_session, worker_id="w2", lease_seconds=60, now=future)
    service.process_job(db_session, reclaimed, provider=provider)
    assert _count_usage(db_session, VerificationUsageEventType.RECOVERED) == 1

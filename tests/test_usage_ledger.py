"""Provider-neutral usage ledger: per-request entries, summary, and reuse.

Proves every MillionVerifier request produces a ledger entry with the right cost/
charge accounting, that the compact summary is correct (calls, cache savings,
failures, spend, projected batch cost), that an interrupted job records an
UNCERTAIN charge, and that a *different* provider can use the same table with no
schema change.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.core.config import get_settings
from app.models.contact import Contact
from app.models.enums import UsageCacheStatus, UsageChargeStatus
from app.models.usage_ledger import UsageLedgerEntry
from app.services import usage_ledger
from app.services.verification import queue as jobs
from app.services.verification import service
from app.services.verification.provider import ProviderResponse, ProviderTransientError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

POLICY = "ver-1"


class _Scripted:
    name = "millionverifier"

    def __init__(self, script):  # type: ignore[no-untyped-def]
        self.script = list(script)

    def verify(self, email: str) -> ProviderResponse:
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _cost_settings(monkeypatch: pytest.MonkeyPatch, rate: str = "0.001"):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MILLIONVERIFIER_COST_PER_CREDIT", rate)
    get_settings.cache_clear()
    return get_settings()


def _run(session: Session, provider, email: str, settings):  # type: ignore[no-untyped-def]
    job, _ = jobs.enqueue_verification(session, email=email, policy_version=POLICY, max_attempts=4)
    claimed = jobs.claim_next_job(session, worker_id="w", lease_seconds=60)
    return service.process_job(session, claimed, provider=provider, settings=settings)


def test_billed_result_records_confirmed_charge(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _cost_settings(monkeypatch)
    provider = service.get_provider(settings)
    _run(db_session, provider, "ok@acme.com", settings)
    entry = db_session.scalars(
        select(UsageLedgerEntry).where(UsageLedgerEntry.result == "valid")
    ).first()
    assert entry is not None
    assert entry.provider == "millionverifier"
    assert entry.operation == "verify_email"
    assert entry.cache_status == UsageCacheStatus.MISS
    assert entry.charge_status == UsageChargeStatus.CONFIRMED
    assert entry.units == 1
    assert entry.estimated_cost == Decimal("0.001000")
    assert entry.currency == "USD"
    get_settings.cache_clear()


def test_free_result_records_no_charge(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _cost_settings(monkeypatch)
    provider = service.get_provider(settings)
    _run(db_session, provider, "x@catchall.example", settings)
    entry = db_session.scalars(
        select(UsageLedgerEntry).where(UsageLedgerEntry.result == "catch_all")
    ).first()
    assert entry is not None
    assert entry.charge_status == UsageChargeStatus.NONE
    assert entry.units == 0
    assert entry.estimated_cost == Decimal("0")
    get_settings.cache_clear()


def test_cache_hit_records_hit_entry(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _cost_settings(monkeypatch)
    provider = service.get_provider(settings)
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
    service.prepare_and_enqueue_contact(db_session, c, settings=settings)
    service.run_worker(db_session, provider=provider, settings=settings)
    # Second pass reuses fresh evidence -> a HIT ledger entry, no charge.
    service.prepare_and_enqueue_contact(db_session, c, settings=settings)
    hits = db_session.scalar(
        select(func.count()).where(UsageLedgerEntry.cache_status == UsageCacheStatus.HIT)
    )
    assert hits >= 1
    get_settings.cache_clear()


def test_reclaimed_job_records_uncertain_charge(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _cost_settings(monkeypatch)
    provider = service.get_provider(settings)
    job, _ = jobs.enqueue_verification(
        db_session, email="ok@acme.com", policy_version=POLICY, max_attempts=4
    )
    jobs.claim_next_job(db_session, worker_id="dead", lease_seconds=60)
    future = datetime.now(UTC) + timedelta(hours=1)
    reclaimed = jobs.claim_next_job(db_session, worker_id="w2", lease_seconds=60, now=future)
    service.process_job(db_session, reclaimed, provider=provider, settings=settings)
    uncertain = db_session.scalar(
        select(func.count()).where(UsageLedgerEntry.charge_status == UsageChargeStatus.UNCERTAIN)
    )
    assert uncertain == 1
    get_settings.cache_clear()


def test_summary_counts_and_projected_cost(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _cost_settings(monkeypatch, rate="0.002")
    provider = service.get_provider(settings)
    # 2 billed, 1 free, plus one still-pending job (projected).
    _run(db_session, provider, "ok@acme.com", settings)
    _run(db_session, provider, "invalid@acme.com", settings)
    _run(db_session, provider, "x@catchall.example", settings)
    jobs.enqueue_verification(
        db_session, email="pending@acme.com", policy_version=POLICY, max_attempts=4
    )
    summary = usage_ledger.provider_summary(
        db_session,
        provider="millionverifier",
        currency="USD",
        cost_per_unit=Decimal("0.002"),
        pending_units=1,
    )
    assert summary.calls == 3
    assert summary.billed_calls == 2
    assert summary.units_consumed == 2
    assert summary.estimated_spend == Decimal("0.004000")
    assert summary.projected_batch_cost == Decimal("0.002")
    assert summary.rate_configured is True
    get_settings.cache_clear()


def test_failure_counts_as_ledger_failure(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _cost_settings(monkeypatch)
    provider = _Scripted([ProviderTransientError("timeout")])
    job, _ = jobs.enqueue_verification(
        db_session, email="a@acme.com", policy_version=POLICY, max_attempts=4
    )
    claimed = jobs.claim_next_job(db_session, worker_id="w", lease_seconds=60)
    service.process_job(db_session, claimed, provider=provider, settings=settings)
    summary = usage_ledger.provider_summary(
        db_session, provider="millionverifier", currency="USD", cost_per_unit=Decimal("0")
    )
    assert summary.failures == 1
    get_settings.cache_clear()


def test_second_provider_writes_same_table(db_session: Session) -> None:
    usage_ledger.record_entry(
        db_session,
        provider="openai",
        operation="draft_generation",
        attempted_at=datetime.now(UTC),
        cache_status=UsageCacheStatus.MISS,
        charge_status=UsageChargeStatus.CONFIRMED,
        units=1200,  # tokens
        estimated_cost=Decimal("0.0240"),
        currency="USD",
        result="completed",
        job_kind="draft_job",
        job_id=uuid.uuid4(),
    )
    usage_ledger.record_entry(
        db_session,
        provider="saleshandy",
        operation="schedule_send",
        attempted_at=datetime.now(UTC),
        cache_status=UsageCacheStatus.NOT_APPLICABLE,
        charge_status=UsageChargeStatus.NONE,
        units=1,
        currency="USD",
        result="scheduled",
    )
    openai = usage_ledger.provider_summary(
        db_session, provider="openai", currency="USD", cost_per_unit=Decimal("0")
    )
    assert openai.calls == 1
    assert openai.units_consumed == 1200
    saleshandy = usage_ledger.provider_summary(
        db_session, provider="saleshandy", currency="USD", cost_per_unit=Decimal("0")
    )
    # NOT_APPLICABLE is neither a call nor a cache hit.
    assert saleshandy.calls == 0
    assert saleshandy.cache_hits == 0

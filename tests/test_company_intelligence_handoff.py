"""The automatic Research → Company Intelligence handoff.

The intended operating model: Research commits a usable dossier → one
idempotent, company-scoped Company Intelligence job is enqueued in the same
transaction → the standard worker fleet processes it → the version serves every
Contact linked to the Company. No backfill run, no batch advancing, no separate
always-on worker, no per-Contact duplication.

The first test drives the *real* Research Agent through the orchestrator (the
same harness the fallback tests use) and reads the handoff off the durable
Research result; the rest pin the idempotency, fleet-consumption, sharing and
backfill-optionality guarantees at the service seams the script calls.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.core.config import get_settings
from app.models.company_intelligence import (
    CompanyIntelligenceJob,
    CompanyIntelligenceVersion,
)
from app.models.enums import IntelligenceJobStatus
from app.services.company_intelligence import backfill as ci_backfill
from app.services.company_intelligence import handoff as ci_handoff
from app.services.company_intelligence import runner as ci_runner
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.test_company_intelligence import (
    make_company,
    make_dossier,
    make_fact,
    seeded,
)
from tests.test_company_intelligence_jobs import INDUSTRY_ANSWER, ScriptedThinker, factory
from tests.test_research_claude_fallback import FakeWorker, _adapters, _fact, _setup

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _features(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("FEATURES__COMPANY_RESEARCH", "true")
    monkeypatch.setenv("FEATURES__COMPANY_INTELLIGENCE", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _jobs(session: Session) -> list[CompanyIntelligenceJob]:
    return list(
        session.scalars(
            select(CompanyIntelligenceJob).order_by(CompanyIntelligenceJob.created_at)
        ).all()
    )


def _ready_company(session: Session, *, name: str):
    """A Company with a current dossier and one sourced fact, via CI helpers."""

    company = make_company(session, name=name)
    make_dossier(session, company=company)
    make_fact(
        session,
        company=company,
        claim="headquarters: headquartered in London, United Kingdom",
        key=f"handoff:{name}",
    )
    return company


# --- 1. Research completion queues Company Intelligence automatically --------


def test_research_completion_automatically_queues_company_intelligence(
    db_session: Session,
) -> None:
    from app.services.agents.orchestrator import run_next as orchestrator_run_next

    seeded(db_session)
    membership, job = _setup(db_session)
    worker = FakeWorker(
        facts=(
            _fact("short_description", "Kiln Systems builds industrial kiln controllers"),
            _fact("headquarters", "Sheffield, United Kingdom"),
        ),
        sufficient=True,
    )
    orchestrator_run_next(db_session, worker_id="handoff-test", adapters=_adapters(worker))

    db_session.refresh(job)
    handoff = job.result["company_intelligence"]
    assert handoff["enqueued"] is True
    assert handoff["outcome"] == ci_handoff.OUTCOME_QUEUED
    assert handoff["input_digest"]

    (ci_job,) = _jobs(db_session)
    assert str(ci_job.id) == handoff["job_id"]
    assert ci_job.status is IntelligenceJobStatus.PENDING
    assert ci_job.requested_by == ci_handoff.RESEARCH_HANDOFF_ACTOR
    assert ci_job.expected_input_digest == handoff["input_digest"]
    assert ci_job.backfill_run_id is None, "the normal path needs no backfill run"
    # Company-scoped: the queue row knows a Company and nothing per-Contact.
    assert ci_job.company_id is not None


def test_an_insufficient_dossier_is_recorded_not_queued(db_session: Session) -> None:
    from app.services.agents.orchestrator import run_next as orchestrator_run_next

    seeded(db_session)
    membership, job = _setup(db_session)
    worker = FakeWorker(facts=(_fact("short_description", "Kiln controllers"),), sufficient=False)
    orchestrator_run_next(db_session, worker_id="handoff-test", adapters=_adapters(worker))

    db_session.refresh(job)
    handoff = job.result["company_intelligence"]
    assert handoff["enqueued"] is False
    assert handoff["outcome"] == ci_handoff.OUTCOME_DOSSIER_NOT_USABLE
    assert _jobs(db_session) == []


def test_with_the_feature_off_nothing_is_queued_and_the_result_says_so(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.agents.orchestrator import run_next as orchestrator_run_next

    monkeypatch.delenv("FEATURES__COMPANY_INTELLIGENCE", raising=False)
    get_settings.cache_clear()
    membership, job = _setup(db_session)
    worker = FakeWorker(
        facts=(_fact("short_description", "Kiln Systems builds kiln controllers"),),
        sufficient=True,
    )
    orchestrator_run_next(db_session, worker_id="handoff-test", adapters=_adapters(worker))

    db_session.refresh(job)
    handoff = job.result["company_intelligence"]
    assert handoff["enqueued"] is False
    assert handoff["outcome"] == ci_handoff.OUTCOME_FEATURE_DISABLED
    assert _jobs(db_session) == []


# --- 2. the standard worker fleet processes it --------------------------------


def test_the_shared_worker_consumes_the_handoff_job(db_session: Session) -> None:
    seeded(db_session)
    company = _ready_company(db_session, name="Fleet Consumed Co")
    outcome = ci_handoff.enqueue_after_research(db_session, company=company)
    assert outcome.enqueued is True

    # The exact service call scripts/run_agent_worker.py makes when the Agent
    # queue is idle.
    run = ci_runner.run_next(
        db_session, worker_id="fleet#0", thinker_factory=factory(ScriptedThinker(INDUSTRY_ANSWER))
    )
    assert run is not None and run.succeeded is True
    (ci_job,) = _jobs(db_session)
    assert ci_job.status is IntelligenceJobStatus.SUCCEEDED

    version = db_session.scalars(
        select(CompanyIntelligenceVersion).where(
            CompanyIntelligenceVersion.company_id == company.id
        )
    ).one()
    assert version.version_number == 1
    # Nothing further is due: the fleet's next poll finds an idle queue.
    assert ci_runner.run_next(db_session, worker_id="fleet#0") is None


def test_the_general_worker_launcher_dispatches_company_intelligence() -> None:
    """The shared launcher owns the dispatch, with an explicit opt-out flag."""

    spec = importlib.util.spec_from_file_location(
        "run_agent_worker_under_test", REPO_ROOT / "scripts" / "run_agent_worker.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module._run_company_intelligence_once)
    options = {option for action in module._parser()._actions for option in action.option_strings}
    assert "--skip-company-intelligence" in options


# --- 3–5. idempotency and company scoping -------------------------------------


def test_repeated_research_with_unchanged_input_does_not_duplicate_work(
    db_session: Session,
) -> None:
    seeded(db_session)
    company = _ready_company(db_session, name="Unchanged Input Co")

    first = ci_handoff.enqueue_after_research(db_session, company=company)
    second = ci_handoff.enqueue_after_research(db_session, company=company)
    assert first.enqueued is True
    assert second.enqueued is False
    assert second.outcome == ci_handoff.OUTCOME_ALREADY_QUEUED
    assert second.job_id == first.job_id
    assert len(_jobs(db_session)) == 1

    # Once answered, the digest short-circuits before any queueing at all.
    run = ci_runner.run_next(
        db_session, worker_id="fleet#0", thinker_factory=factory(ScriptedThinker(INDUSTRY_ANSWER))
    )
    assert run is not None and run.succeeded
    third = ci_handoff.enqueue_after_research(db_session, company=company)
    assert third.enqueued is False
    assert third.outcome == ci_handoff.OUTCOME_ALREADY_ANSWERED
    assert len(_jobs(db_session)) == 1


def test_changed_dossier_input_creates_exactly_one_new_job(db_session: Session) -> None:
    seeded(db_session)
    company = _ready_company(db_session, name="Changed Input Co")
    first = ci_handoff.enqueue_after_research(db_session, company=company)
    assert first.enqueued is True
    run = ci_runner.run_next(
        db_session, worker_id="fleet#0", thinker_factory=factory(ScriptedThinker(INDUSTRY_ANSWER))
    )
    assert run is not None and run.succeeded

    # New evidence changes the input digest: exactly one new job, once.
    make_fact(
        db_session,
        company=company,
        claim="office_locations: operations teams based in Manchester",
        key="handoff:changed:2",
    )
    second = ci_handoff.enqueue_after_research(db_session, company=company)
    assert second.enqueued is True
    assert second.input_digest != first.input_digest
    again = ci_handoff.enqueue_after_research(db_session, company=company)
    assert again.enqueued is False
    jobs = _jobs(db_session)
    assert len(jobs) == 2
    assert len({item.idempotency_key for item in jobs}) == 2


def test_contacts_sharing_one_company_share_one_job(db_session: Session) -> None:
    """Two Research completions for the same Company queue one job, not two."""

    seeded(db_session)
    company = _ready_company(db_session, name="Shared Company Co")
    first = ci_handoff.enqueue_after_research(db_session, company=company)
    second = ci_handoff.enqueue_after_research(db_session, company=company)
    assert first.enqueued is True and second.enqueued is False
    assert len(_jobs(db_session)) == 1


# --- 6. backfill stays functional, no longer required -------------------------


def test_backfill_remains_functional_but_is_not_part_of_the_normal_path(
    db_session: Session,
) -> None:
    seeded(db_session)
    # The normal path queued with no backfill run in sight (proved above); here
    # the deliberate tool still works for historical reprocessing.
    company = _ready_company(db_session, name="Backfill Historical Co")
    run = ci_backfill.create_run(db_session, label="historical", dry_run=False, batch_size=10)
    report = ci_backfill.advance(db_session, run=run, feature_enabled=True)
    assert report.enqueued >= 1
    jobs = _jobs(db_session)
    assert any(item.backfill_run_id == run.id for item in jobs)
    assert company.id in {item.company_id for item in jobs}

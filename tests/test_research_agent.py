"""Research Agent on the Phase 2 backbone (RES-001 / #173, #160).

Every case drives the real worker (``run_next``), so a passing test also
proves the adapter never moves a job itself and never bypasses a gate.

Nothing here reaches the network. The website worker is exercised through
its explicit collector seam and a fake registry, which is what #173
requires: synthetic fixtures, no live browsing in CI.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from app.core.config import get_settings
from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.company_dossier import CompanyDossierVersion, CompanyResearchSubmission
from app.models.contact import Contact
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    AgentJobStatus,
    CampaignStatus,
    InsightKind,
    PipelineStageStatus,
    ResearchState,
)
from app.models.insight import Insight, InsightEvidence
from app.models.pipeline import CampaignContactAgentState
from app.models.verification_job import AgentJob
from app.services import campaign_contacts, pipeline
from app.services.agents import controls
from app.services.agents.adapters import DEFAULT_ADAPTERS, ResearchAgentAdapter
from app.services.agents.orchestrator import run_next
from app.services.research.contracts import (
    ResearchRequest,
    ResearchWorkerError,
    SourcedFact,
    WorkerResult,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

WORKER = "research-worker-1"
DOMAIN = "engines.example"


# --- fakes -------------------------------------------------------------------


def _fact(field: str, value: str, *, path: str = "/about", confidence: float = 0.9) -> SourcedFact:
    return SourcedFact(
        field=field,
        value=value,
        source_url=f"https://{DOMAIN}{path}",
        retrieved_at=datetime.now(UTC),
        extraction_method="explicit_statement:explicit",
        confidence=confidence,
        excerpt=f"...{value}...",
    )


class FakeWorker:
    """A research source under full test control."""

    name = "fake"
    version = "test-1"

    def __init__(
        self,
        *,
        facts: tuple[SourcedFact, ...] = (),
        warnings: tuple[str, ...] = (),
        sufficient: bool = True,
        error: ResearchWorkerError | None = None,
    ) -> None:
        self.facts = facts
        self.warnings = warnings
        self.sufficient = sufficient
        self.error = error
        self.calls: list[ResearchRequest] = []

    def run(self, request: ResearchRequest) -> WorkerResult:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return WorkerResult(
            worker=self.name,
            worker_version=self.version,
            facts=self.facts,
            warnings=self.warnings,
            raw={"pages": [{"url": f"https://{DOMAIN}/", "page_type": "home"}]},
            sufficient=self.sufficient,
        )


def _adapters(worker: object) -> dict[AgentIdentifier, object]:
    merged = dict(DEFAULT_ADAPTERS)
    merged[AgentIdentifier.RESEARCH] = ResearchAgentAdapter(
        workers_factory=lambda _names=None: (worker,)
    )
    return merged


@pytest.fixture(autouse=True)
def _enable_feature(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("FEATURES__COMPANY_RESEARCH", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --- fixtures ----------------------------------------------------------------


def _records(db: Session, *, domain: str | None = DOMAIN) -> tuple[Campaign, Company, Contact]:
    company = Company(name="Analytical Engines", domain=domain)
    campaign = Campaign(
        name=f"Research {uuid.uuid4()}",
        status=CampaignStatus.ACTIVE,
        execution_enabled=True,
    )
    db.add_all([company, campaign])
    db.flush()
    contact = Contact(
        first_name="Ada",
        last_name="Lovelace",
        company_name=company.name,
        company_domain=company.domain,
        company_id=company.id,
        natural_key=f"ada|lovelace|{uuid.uuid4()}",
    )
    db.add(contact)
    db.flush()
    return campaign, company, contact


def _enable_research(
    db: Session,
    *,
    live: bool = True,
    status: AgentControlStatus = AgentControlStatus.ENABLED,
) -> None:
    controls.set_global_control(
        db,
        agent_id=AgentIdentifier.RESEARCH,
        status=status,
        config={"live": live},
    )


def _enrol(db: Session, campaign: Campaign, contact: Contact) -> CampaignContact:
    """Enrol and advance the membership so Research is the next stage."""

    enrolled = campaign_contacts.enrol_contact(
        db,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        source_reference="research-test",
        enqueue=False,
        desired_stage=AgentIdentifier.RESEARCH,
    )
    membership = enrolled.membership
    for agent_id in (AgentIdentifier.IDENTITY, AgentIdentifier.COMPANY):
        pipeline.transition_stage(
            db,
            membership=membership,
            agent_id=agent_id,
            target=PipelineStageStatus.COMPLETED,
            event_type=pipeline.PipelineEventType.STAGE_COMPLETED,
            actor="test-setup",
            reason_code="test_setup",
        )
    db.flush()
    return membership


def _queue(db: Session, membership: CampaignContact) -> AgentJob:
    from app.services.agents import orchestrator

    job = orchestrator.schedule_next(db, membership=membership, actor="test-setup")
    assert job is not None, "Research stage was not scheduled"
    assert job.agent_id is AgentIdentifier.RESEARCH
    db.flush()
    return job


def _stage(db: Session, membership: CampaignContact) -> CampaignContactAgentState:
    state = pipeline.agent_state(
        db, campaign_contact_id=membership.id, agent_id=AgentIdentifier.RESEARCH, create=False
    )
    assert state is not None
    return state


def _setup(db: Session, worker: FakeWorker, **kw: object) -> tuple[CampaignContact, AgentJob]:
    campaign, _company, contact = _records(db, **kw)  # type: ignore[arg-type]
    _enable_research(db)
    membership = _enrol(db, campaign, contact)
    job = _queue(db, membership)
    return membership, job


# --- the happy path ----------------------------------------------------------


def test_sourced_facts_land_as_evidence_backed_insights(db_session: Session) -> None:
    worker = FakeWorker(
        facts=(
            _fact("company_name", "Analytical Engines Ltd"),
            _fact("headquarters", "London, United Kingdom", path="/contact"),
            _fact("founded_year", "1843"),
        )
    )
    membership, job = _setup(db_session, worker)

    run_next(db_session, worker_id=WORKER, adapters=_adapters(worker))  # type: ignore[arg-type]

    db_session.refresh(job)
    assert job.status is AgentJobStatus.SUCCEEDED
    assert _stage(db_session, membership).status is PipelineStageStatus.COMPLETED

    insights = db_session.scalars(select(Insight)).all()
    assert len(insights) == 3
    assert {i.kind for i in insights} == {InsightKind.FACT}
    # Every claim must be traceable to the page it was read from.
    evidence = db_session.scalars(select(InsightEvidence)).all()
    assert len(evidence) == 3
    assert all(e.source_url.startswith(f"https://{DOMAIN}") for e in evidence)
    assert all(e.retrieved_at is not None for e in evidence)


def test_raw_payload_and_dossier_are_both_preserved(db_session: Session) -> None:
    worker = FakeWorker(facts=(_fact("company_name", "Analytical Engines Ltd"),))
    membership, _ = _setup(db_session, worker)

    run_next(db_session, worker_id=WORKER, adapters=_adapters(worker))  # type: ignore[arg-type]

    submission = db_session.scalars(select(CompanyResearchSubmission)).one()
    assert submission.payload["domain"] == DOMAIN
    assert submission.payload["workers"][0]["worker"] == "fake"

    version = db_session.scalars(select(CompanyDossierVersion)).one()
    assert version.submission_id == submission.id
    company = db_session.get(Company, membership.contact_id and submission.company_id)
    assert company is not None
    assert company.research_state in {
        ResearchState.COMPLETED,
        ResearchState.COMPLETED_WITH_WARNINGS,
    }


def test_the_worker_receives_the_resolved_domain(db_session: Session) -> None:
    worker = FakeWorker(facts=(_fact("company_name", "Analytical Engines Ltd"),))
    _setup(db_session, worker)

    run_next(db_session, worker_id=WORKER, adapters=_adapters(worker))  # type: ignore[arg-type]

    assert [c.domain for c in worker.calls] == [DOMAIN]


# --- honest outcomes ---------------------------------------------------------


def test_insufficient_evidence_completes_with_warnings_rather_than_failing(
    db_session: Session,
) -> None:
    """A thin website is a fact about the company, not a pipeline failure."""

    worker = FakeWorker(facts=(_fact("company_name", "Analytical Engines Ltd"),), sufficient=False)
    membership, job = _setup(db_session, worker)

    run_next(db_session, worker_id=WORKER, adapters=_adapters(worker))  # type: ignore[arg-type]

    db_session.refresh(job)
    assert job.status is AgentJobStatus.SUCCEEDED
    assert _stage(db_session, membership).status is PipelineStageStatus.COMPLETED
    assert job.result["sufficient"] is False

    version = db_session.scalars(select(CompanyDossierVersion)).one()
    assert any("insufficient evidence" in str(w) for w in (version.warnings or []))


def test_a_worker_warning_survives_onto_the_dossier(db_session: Session) -> None:
    worker = FakeWorker(
        facts=(_fact("company_name", "Analytical Engines Ltd"),),
        warnings=("skipped https://engines.example/private: disallowed by robots.txt",),
    )
    _setup(db_session, worker)

    run_next(db_session, worker_id=WORKER, adapters=_adapters(worker))  # type: ignore[arg-type]

    version = db_session.scalars(select(CompanyDossierVersion)).one()
    assert any("robots.txt" in str(w) for w in (version.warnings or []))


# --- refusals ----------------------------------------------------------------


def test_research_refuses_without_the_feature_flag(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = FakeWorker(facts=(_fact("company_name", "x"),))
    membership, job = _setup(db_session, worker)

    monkeypatch.setenv("FEATURES__COMPANY_RESEARCH", "false")
    get_settings.cache_clear()

    run_next(db_session, worker_id=WORKER, adapters=_adapters(worker))  # type: ignore[arg-type]

    db_session.refresh(job)
    assert job.status is AgentJobStatus.PAUSED
    assert _stage(db_session, membership).status is PipelineStageStatus.BLOCKED
    assert worker.calls == [], "no site may be read while the feature is off"


def test_research_refuses_until_the_campaign_opts_in(db_session: Session) -> None:
    worker = FakeWorker(facts=(_fact("company_name", "x"),))
    campaign, _company, contact = _records(db_session)
    _enable_research(db_session, live=False)
    membership = _enrol(db_session, campaign, contact)
    job = _queue(db_session, membership)

    run_next(db_session, worker_id=WORKER, adapters=_adapters(worker))  # type: ignore[arg-type]

    db_session.refresh(job)
    assert job.status is AgentJobStatus.PAUSED
    assert _stage(db_session, membership).status is PipelineStageStatus.BLOCKED
    assert worker.calls == [], "no site may be read before the campaign enables research"


def test_research_blocks_when_the_company_has_no_domain(db_session: Session) -> None:
    worker = FakeWorker(facts=(_fact("company_name", "x"),))
    membership, job = _setup(db_session, worker, domain=None)

    run_next(db_session, worker_id=WORKER, adapters=_adapters(worker))  # type: ignore[arg-type]

    db_session.refresh(job)
    assert job.status is AgentJobStatus.PAUSED
    assert _stage(db_session, membership).status is PipelineStageStatus.BLOCKED
    assert worker.calls == []
    assert db_session.scalars(select(Insight)).all() == []


def test_an_unreachable_site_is_terminal_not_retried(db_session: Session) -> None:
    worker = FakeWorker(
        error=ResearchWorkerError(
            "homepage unreachable: connection refused",
            code="site_unreachable",
            retryable=False,
        )
    )
    membership, job = _setup(db_session, worker)

    run_next(db_session, worker_id=WORKER, adapters=_adapters(worker))  # type: ignore[arg-type]

    db_session.refresh(job)
    assert job.status is AgentJobStatus.FAILED
    assert _stage(db_session, membership).status is PipelineStageStatus.FAILED
    assert db_session.scalars(select(CompanyResearchSubmission)).all() == []


def test_a_transient_fault_is_retried(db_session: Session) -> None:
    worker = FakeWorker(
        error=ResearchWorkerError("read timeout", code="collection_failed", retryable=True)
    )
    membership, job = _setup(db_session, worker)

    run_next(db_session, worker_id=WORKER, adapters=_adapters(worker))  # type: ignore[arg-type]

    db_session.refresh(job)
    assert job.status is AgentJobStatus.RETRY_SCHEDULED
    assert _stage(db_session, membership).status is PipelineStageStatus.RETRYING


# --- idempotency -------------------------------------------------------------


def test_rerunning_the_same_job_does_not_duplicate_insights(db_session: Session) -> None:
    """Retry safety: the same job re-executed reuses its rows."""

    facts = (_fact("company_name", "Analytical Engines Ltd"), _fact("founded_year", "1843"))
    worker = FakeWorker(facts=facts)
    membership, job = _setup(db_session, worker)

    run_next(db_session, worker_id=WORKER, adapters=_adapters(worker))  # type: ignore[arg-type]
    first = len(db_session.scalars(select(Insight)).all())

    # Re-drive the same job through the domain layer, as a lease recovery would.
    from app.services.research.agent import ResearchStepKind, execute_step

    contact = db_session.get(Contact, membership.contact_id)
    assert contact is not None
    step = execute_step(db_session, job=job, contact=contact, workers=(worker,))
    db_session.flush()

    assert step.kind is ResearchStepKind.COMPLETE
    assert len(db_session.scalars(select(Insight)).all()) == first == 2

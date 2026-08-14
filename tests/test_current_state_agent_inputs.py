"""Insights and Personalization read the Company's *current* knowledge.

Research is not a campaign-execution artefact. It is an independent Company
knowledge function that may run today, tomorrow, every day, or outside any
campaign, and each run may enrich what is already known. The Agents downstream
of it therefore ask "what does this Company currently know?", record what they
used, and never demand that a predecessor execution be reproducible.

The distinction these tests pin down:

* **Selection** is current-state. An Insights job that cannot name the exact
  Research execution behind the Company's dossier still runs.
* **Provenance** is historical. What a run recorded stays true after Research
  moves on.

Nothing here relaxes an eligibility rule. A Company with no Research knowledge
still blocks, ineligible evidence is still refused, and every historical record
survives untouched.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import NamedTuple

import pytest
from app.core.config import get_settings
from app.models.campaign import Campaign
from app.models.company import Company
from app.models.company_dossier import CompanyDossierVersion, CompanyResearchSubmission
from app.models.contact import Contact
from app.models.draft import DraftVersion
from app.models.enums import (
    AgentIdentifier,
    AgentJobStatus,
    CampaignStatus,
    InsightKind,
    InsightState,
)
from app.models.insight import Insight, InsightEvidence
from app.models.verification_job import AgentJob
from app.services import campaign_contacts
from app.services.agent_studio.insights_report import DurableInsightsReportReader
from app.services.agents.adapters import (
    AgentBlocked,
    AgentExecutionContext,
    InsightsAgentAdapter,
    PersonalizationAgentAdapter,
)
from app.services.agents.jobs import enqueue_job
from app.services.companies import dossiers
from app.services.insights import evidence as insights_evidence
from app.services.insights import lineage as insights_lineage
from app.services.personalization import policy as personalization_policy
from app.services.personalization import sequence as sequence_generation
from app.services.thinking.contracts import ThinkingRequest, ThinkingResult
from sqlalchemy import select
from sqlalchemy.orm import Session

WORKER = "current-state-test"


class ResearchRun(NamedTuple):
    """One committed Research execution and the artefacts it left behind."""

    job: AgentJob
    dossier: CompanyDossierVersion
    handle: uuid.UUID
    fact: Insight


@pytest.fixture(autouse=True)
def _company_research_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("FEATURES__COMPANY_RESEARCH", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class ScriptedThinker:
    name = "scripted"
    version = "scripted/v1"

    def __init__(self, payload: dict[str, object] | None = None) -> None:
        self._payload = payload or {}
        self.requests: list[ThinkingRequest] = []

    def think(self, request: ThinkingRequest) -> ThinkingResult:
        self.requests.append(request)
        return ThinkingResult(
            payload=self._payload,
            producer=self.name,
            producer_version=self.version,
            duration_seconds=0.01,
        )


# ---------------------------------------------------------------------------
# Fixtures for the shape hosted UAT actually produced
# ---------------------------------------------------------------------------


def _records(db: Session) -> tuple[Campaign, Company, Contact]:
    company = Company(name="Kiln Systems", domain=f"{uuid.uuid4()}.example")
    campaign = Campaign(
        name=f"Current state {uuid.uuid4()}",
        description="Plant operations and industrial workflow",
        status=CampaignStatus.ACTIVE,
        execution_enabled=True,
    )
    db.add_all([company, campaign])
    db.flush()
    contact = Contact(
        first_name="Ada",
        last_name="Lovelace",
        title="Head of Operations",
        company_name=company.name,
        company_domain=company.domain,
        company_id=company.id,
        email="ada@kiln.example",
        natural_key=f"ada|lovelace|{uuid.uuid4()}",
    )
    db.add(contact)
    db.flush()
    return campaign, company, contact


def _research_run(
    db: Session,
    *,
    company: Company,
    summary: str,
    claim: str,
    source_url: str,
    campaign: Campaign | None = None,
    contact: Contact | None = None,
) -> ResearchRun:
    """One committed Research execution: submission, current dossier, sourced fact.

    Deliberately allows ``campaign``/``contact`` to be None — Research knowledge
    belongs to the Company, and a run that happened outside this Campaign is the
    normal case this contract exists to support.
    """

    job, _ = enqueue_job(
        db,
        agent_id=AgentIdentifier.RESEARCH,
        idempotency_key=f"test:research:{uuid.uuid4()}",
        task_kind="advance_campaign_contact",
        max_attempts=3,
        campaign_id=campaign.id if campaign else None,
        contact_id=contact.id if contact else None,
        company_id=company.id,
    )
    submission, _ = dossiers.submit(
        db,
        company=company,
        producer="research-agent",
        payload={"overview": {"summary": summary}},
        request_context={"agent_job_id": str(job.id)},
    )
    dossier = dossiers.interpret(
        db,
        company=company,
        submission=submission,
        interpreter="research-agent",
        sections={"overview": {"summary": summary}},
    )
    job.status = AgentJobStatus.SUCCEEDED
    job.finished_at = datetime.now(UTC)
    job.result = {
        "company_id": str(company.id),
        "submission_id": str(submission.id),
        "dossier_version": dossier.version_number,
    }
    fact = insights_evidence.create_insight(
        db,
        claim=claim,
        kind=InsightKind.FACT,
        state=InsightState.SUPPORTED,
        evidence=[
            insights_evidence.EvidenceInput(
                source_url=source_url,
                retrieved_at=datetime.now(UTC),
                evidence_summary=claim,
                confidence=0.85,
                extraction_method="research-test/v1",
            )
        ],
        company_id=company.id,
        idempotency_key=f"research:{job.id}:website:0",
    )
    handle = db.scalars(select(InsightEvidence).where(InsightEvidence.insight_id == fact.id)).one()
    db.flush()
    return ResearchRun(job=job, dossier=dossier, handle=handle.id, fact=fact)


def _insights_context(
    db: Session,
    *,
    campaign: Campaign,
    contact: Contact,
    agent_id: AgentIdentifier = AgentIdentifier.INSIGHTS,
) -> AgentExecutionContext:
    """An execution with **no** predecessor lineage of any kind.

    No ``parent_job_id``, no pinned Research ids. This is precisely the job the
    old contract refused, and precisely the job a hosted recovery produces.
    """

    if agent_id is AgentIdentifier.PERSONALIZATION:
        personalization_policy.ensure_initial_policy(db, actor="test")
    enrolled = campaign_contacts.enrol_contact(
        db,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=False,
    )
    job, _ = enqueue_job(
        db,
        agent_id=agent_id,
        idempotency_key=f"test:{agent_id.value}:{uuid.uuid4()}",
        task_kind="advance_campaign_contact",
        max_attempts=3,
        campaign_id=campaign.id,
        campaign_contact_id=enrolled.membership.id,
        contact_id=contact.id,
        company_id=contact.company_id,
    )
    return AgentExecutionContext(
        session=db,
        job=job,
        campaign=campaign,
        membership=enrolled.membership,
        contact=contact,
        config={"live": True},
        worker_id=WORKER,
    )


def _claims(handle: uuid.UUID, claim: str) -> dict[str, object]:
    return {
        "claims": [{"claim": claim, "kind": "fact", "evidence_handles": [str(handle)]}],
        "unknowns": [],
    }


# ---------------------------------------------------------------------------
# 1. Insights input selection
# ---------------------------------------------------------------------------


def test_insights_runs_from_company_knowledge_without_predecessor_lineage(
    db_session: Session,
) -> None:
    """The live UAT blocker, as a test.

    The Research run belongs to no Campaign and no Contact, and the Insights job
    names nothing at all. The old contract refused this; the knowledge it needed
    was sitting on the Company the whole time.
    """

    campaign, company, contact = _records(db_session)
    run = _research_run(
        db_session,
        company=company,
        summary="Kiln Systems runs two plants.",
        claim="Opened a second plant in Pune.",
        source_url="https://kiln.example/news/pune",
    )
    context = _insights_context(db_session, campaign=campaign, contact=contact)
    assert context.job.parent_job_id is None
    assert "research_job_id" not in context.job.input_reference

    thinker = ScriptedThinker(_claims(run.handle, "Opened a second plant in Pune."))
    result = InsightsAgentAdapter(thinker_factory=lambda _s: thinker).execute(context)

    assert result.output_reference["insights_stored"] == 1
    assert result.output_reference["research_job_id"] == str(run.job.id)
    assert result.output_reference["submission_id"] == str(run.dossier.submission_id)
    assert result.output_reference["dossier_version_id"] == str(run.dossier.id)


def test_insights_started_after_a_later_research_run_uses_the_newer_state(
    db_session: Session,
) -> None:
    """Run A, then run B. An Insights execution begun after B reads B."""

    campaign, company, contact = _records(db_session)
    run_a = _research_run(
        db_session,
        company=company,
        summary="Kiln Systems runs one plant.",
        claim="Runs a single plant in Pune.",
        source_url="https://kiln.example/news/pune",
    )
    run_b = _research_run(
        db_session,
        company=company,
        summary="Kiln Systems runs three plants.",
        claim="Opened a third plant in Chennai.",
        source_url="https://kiln.example/news/chennai",
    )
    assert run_b.dossier.version_number > run_a.dossier.version_number
    assert run_b.dossier.is_current and not run_a.dossier.is_current

    context = _insights_context(db_session, campaign=campaign, contact=contact)
    # The answer offers A's evidence handle as well as B's; only B's is in scope.
    thinker = ScriptedThinker(
        {
            "claims": [
                {
                    "claim": "Opened a third plant in Chennai.",
                    "kind": "fact",
                    "evidence_handles": [str(run_b.handle)],
                },
                {
                    "claim": "Runs a single plant in Pune.",
                    "kind": "fact",
                    "evidence_handles": [str(run_a.handle)],
                },
            ],
            "unknowns": [],
        }
    )
    result = InsightsAgentAdapter(thinker_factory=lambda _s: thinker).execute(context)

    assert result.output_reference["research_job_id"] == str(run_b.job.id)
    assert result.output_reference["dossier_version_id"] == str(run_b.dossier.id)
    assert result.output_reference["insights_stored"] == 1
    assert result.output_reference["claims_dropped"] == 1
    assert result.output_reference["dropped"][0]["reason"] == "unsourced"
    # B's dossier and evidence reached the prompt and A's did not: this is input
    # selection, not a filter applied to the answer afterwards.
    prompt = thinker.requests[0].prompt
    assert str(run_b.handle) in prompt
    assert str(run_a.handle) not in prompt
    assert "three plants" in prompt and "one plant" not in prompt


def test_a_company_with_a_dossier_but_no_research_execution_still_blocks(
    db_session: Session,
) -> None:
    """Absence is reported, never manufactured into a thin success.

    An operator-submitted dossier is real Company knowledge, but no Research
    execution stood behind it, so there are no sourced facts to cite. Widening
    the input contract must not turn that into a stored claim.
    """

    campaign, company, contact = _records(db_session)
    submission, _ = dossiers.submit(
        db_session,
        company=company,
        producer="operator-manual",
        payload={"overview": {"summary": "Typed in by an operator."}},
    )
    dossiers.interpret(
        db_session,
        company=company,
        submission=submission,
        interpreter="operator-manual",
        sections={"overview": {"summary": "Typed in by an operator."}},
    )
    context = _insights_context(db_session, campaign=campaign, contact=contact)
    with pytest.raises(AgentBlocked) as caught:
        InsightsAgentAdapter(thinker_factory=lambda _s: ScriptedThinker()).execute(context)
    assert caught.value.code == "research_knowledge_unavailable"
    assert insights_evidence.list_for_company(db_session, company_id=company.id) == []


def test_ineligible_research_evidence_is_still_refused(db_session: Session) -> None:
    """Selection widened; the evidence gate did not.

    A citation whose source record is incomplete is dropped exactly as before,
    and with nothing left the stage blocks rather than asserting something it
    cannot show.
    """

    campaign, company, contact = _records(db_session)
    run = _research_run(
        db_session,
        company=company,
        summary="Kiln Systems runs two plants.",
        claim="Opened a second plant in Pune.",
        source_url="https://kiln.example/news/pune",
    )
    row = db_session.get(InsightEvidence, run.handle)
    assert row is not None
    row.confidence = None
    db_session.flush()

    context = _insights_context(db_session, campaign=campaign, contact=contact)
    thinker = ScriptedThinker(_claims(run.handle, "Opened a second plant in Pune."))
    with pytest.raises(AgentBlocked) as caught:
        InsightsAgentAdapter(thinker_factory=lambda _s: thinker).execute(context)
    assert caught.value.code == "insufficient_evidence"
    assert caught.value.detail["dropped"][0]["reason"] == "invalid_evidence"


# ---------------------------------------------------------------------------
# 2. Provenance stays historical
# ---------------------------------------------------------------------------


def test_an_earlier_insights_result_keeps_the_evidence_it_actually_used(
    db_session: Session,
) -> None:
    """A later Research run does not re-attribute an older Insights result."""

    campaign, company, contact = _records(db_session)
    run_a = _research_run(
        db_session,
        company=company,
        summary="Kiln Systems runs one plant.",
        claim="Runs a single plant in Pune.",
        source_url="https://kiln.example/news/pune",
    )
    first = _insights_context(db_session, campaign=campaign, contact=contact)
    executed = InsightsAgentAdapter(
        thinker_factory=lambda _s: ScriptedThinker(
            _claims(run_a.handle, "Runs a single plant in Pune.")
        )
    ).execute(first)
    # What the common worker durably records after a successful execution.
    first.job.status = AgentJobStatus.SUCCEEDED
    first.job.finished_at = datetime.now(UTC)
    first.job.result = dict(executed.result)
    db_session.flush()

    run_b = _research_run(
        db_session,
        company=company,
        summary="Kiln Systems runs three plants.",
        claim="Opened a third plant in Chennai.",
        source_url="https://kiln.example/news/chennai",
    )
    assert run_b.dossier.is_current and run_b.job.id != run_a.job.id

    provenance = insights_lineage.recorded(
        db_session, insights_job=first.job, company_id=company.id
    )
    assert provenance is not None
    assert provenance.dossier.id == run_a.dossier.id
    assert provenance.research_job.id == run_a.job.id

    report = DurableInsightsReportReader(db_session).read_job(first.job.id)
    assert report is not None
    assert report.research_dossier_id == run_a.dossier.id
    assert report.research_job_id == run_a.job.id
    assert report.research_dossier_version == run_a.dossier.version_number


def test_rerunning_insights_after_new_research_consumes_the_newer_state(
    db_session: Session,
) -> None:
    """The rerun the hosted recovery needs: newer knowledge, no rerun of Research."""

    campaign, company, contact = _records(db_session)
    run_a = _research_run(
        db_session,
        company=company,
        summary="Kiln Systems runs one plant.",
        claim="Runs a single plant in Pune.",
        source_url="https://kiln.example/news/pune",
    )
    first = _insights_context(db_session, campaign=campaign, contact=contact)
    first_result = InsightsAgentAdapter(
        thinker_factory=lambda _s: ScriptedThinker(
            _claims(run_a.handle, "Runs a single plant in Pune.")
        )
    ).execute(first)
    assert first_result.output_reference["dossier_version_id"] == str(run_a.dossier.id)

    run_b = _research_run(
        db_session,
        company=company,
        summary="Kiln Systems runs three plants.",
        claim="Opened a third plant in Chennai.",
        source_url="https://kiln.example/news/chennai",
    )
    second = _insights_context(db_session, campaign=campaign, contact=contact)
    second_result = InsightsAgentAdapter(
        thinker_factory=lambda _s: ScriptedThinker(
            _claims(run_b.handle, "Opened a third plant in Chennai.")
        )
    ).execute(second)

    assert second_result.output_reference["research_job_id"] == str(run_b.job.id)
    assert second_result.output_reference["dossier_version_id"] == str(run_b.dossier.id)
    assert second_result.output_reference["insights_stored"] == 1


def test_every_historical_research_record_survives_a_later_run(db_session: Session) -> None:
    """Enrichment adds; it never rewrites or removes."""

    _campaign, company, _contact = _records(db_session)
    run_a = _research_run(
        db_session,
        company=company,
        summary="Kiln Systems runs one plant.",
        claim="Runs a single plant in Pune.",
        source_url="https://kiln.example/news/pune",
    )
    submission_a_id = run_a.dossier.submission_id
    run_b = _research_run(
        db_session,
        company=company,
        summary="Kiln Systems runs three plants.",
        claim="Opened a third plant in Chennai.",
        source_url="https://kiln.example/news/chennai",
    )

    versions = db_session.scalars(
        select(CompanyDossierVersion)
        .where(CompanyDossierVersion.company_id == company.id)
        .order_by(CompanyDossierVersion.version_number)
    ).all()
    assert [version.id for version in versions] == [run_a.dossier.id, run_b.dossier.id]
    assert versions[0].superseded_at is not None and not versions[0].is_current
    assert versions[1].is_current

    submissions = db_session.scalars(
        select(CompanyResearchSubmission).where(CompanyResearchSubmission.company_id == company.id)
    ).all()
    assert {submission.id for submission in submissions} == {
        submission_a_id,
        run_b.dossier.submission_id,
    }

    keys = {
        insight.idempotency_key
        for insight in db_session.scalars(
            select(Insight).where(Insight.company_id == company.id)
        ).all()
    }
    assert f"research:{run_a.job.id}:website:0" in keys
    assert f"research:{run_b.job.id}:website:0" in keys


# ---------------------------------------------------------------------------
# 3. Personalization input selection
# ---------------------------------------------------------------------------


def test_personalization_runs_from_current_state_without_predecessor_lineage(
    db_session: Session,
) -> None:
    """No Insights AgentJob exists at all. The eligible Insight rows are enough."""

    campaign, company, contact = _records(db_session)
    run = _research_run(
        db_session,
        company=company,
        summary="Kiln Systems runs two plants.",
        claim="Opened a second plant in Pune.",
        source_url="https://kiln.example/news/pune",
    )
    assert (
        db_session.scalars(
            select(AgentJob).where(AgentJob.agent_id == AgentIdentifier.INSIGHTS)
        ).first()
        is None
    )

    context = _insights_context(
        db_session, campaign=campaign, contact=contact, agent_id=AgentIdentifier.PERSONALIZATION
    )
    thinker = ScriptedThinker(
        {
            "subject": "the pune plant",
            "body": "Ada - saw the Pune plant news.\n\nWorth a conversation?",
            "evidence_insight_ids": [str(run.fact.id)],
            "rationale": "Led with the plant opening.",
        }
    )
    result = PersonalizationAgentAdapter(thinker_factory=lambda _s: thinker).execute(context)

    draft = db_session.scalars(select(DraftVersion)).one()
    assert draft.subject == "the pune plant"
    assert result.output_reference["evidence_insight_ids"] == [str(run.fact.id)]
    assert result.output_reference["approved"] is False


def test_personalization_records_the_versions_and_evidence_it_used(
    db_session: Session,
) -> None:
    campaign, company, contact = _records(db_session)
    run = _research_run(
        db_session,
        company=company,
        summary="Kiln Systems runs two plants.",
        claim="Opened a second plant in Pune.",
        source_url="https://kiln.example/news/pune",
    )
    context = _insights_context(
        db_session, campaign=campaign, contact=contact, agent_id=AgentIdentifier.PERSONALIZATION
    )
    thinker = ScriptedThinker(
        {
            "subject": "the pune plant",
            "body": "Ada - saw the Pune plant news.\n\nWorth a conversation?",
            "evidence_insight_ids": [str(run.fact.id)],
            "rationale": "Led with the plant opening.",
        }
    )
    PersonalizationAgentAdapter(thinker_factory=lambda _s: thinker).execute(context)

    draft = db_session.scalars(select(DraftVersion)).one()
    decision = draft.personalization_decision
    assert isinstance(decision, dict)
    assert draft.personalization_policy_version_id is not None
    assert draft.personalization_strategy_id
    assert decision["company_intelligence"]["status"]
    assert any(str(run.fact.id) in str(entry) for entry in decision["context_used"])


def test_personalization_input_follows_the_companys_current_dossier_selection(
    db_session: Session,
) -> None:
    """Current means the Company's own selection, not the highest version number.

    An operator who reinstates an earlier reading has said which one the Company
    stands behind, and the sequence digest — the fingerprint of everything a
    generation rests on — has to move with it.
    """

    campaign, company, contact = _records(db_session)
    run_a = _research_run(
        db_session,
        company=company,
        summary="Kiln Systems runs one plant.",
        claim="Runs a single plant in Pune.",
        source_url="https://kiln.example/news/pune",
    )
    run_b = _research_run(
        db_session,
        company=company,
        summary="Kiln Systems runs three plants.",
        claim="Opened a third plant in Chennai.",
        source_url="https://kiln.example/news/chennai",
    )
    policy = personalization_policy.ensure_initial_policy(db_session, actor="test")
    enrolled = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=False,
    )
    on_b = sequence_generation.precompute_digest(
        db_session, membership=enrolled.membership, policy=policy
    )
    assert dossiers.current_version(db_session, company_id=company.id) is run_b.dossier

    dossiers.select_current(db_session, company=company, version=run_a.dossier, actor="test")
    on_a = sequence_generation.precompute_digest(
        db_session, membership=enrolled.membership, policy=policy
    )
    assert on_a != on_b

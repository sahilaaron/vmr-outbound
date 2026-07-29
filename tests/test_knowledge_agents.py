"""Research, Insights and Personalization: what they store, and what they refuse.

Every test here injects a scripted thinker. Nothing shells out, and nothing
reaches a network — which is the point of the seam these Agents were built
around.

The guarantees under test are mostly *negative*, because that is where the risk
lives. A model will always produce something plausible; the value of this layer
is what it declines to store.
"""

from __future__ import annotations

import uuid

import pytest
from app.models.campaign import Campaign
from app.models.company import Company
from app.models.contact import Contact
from app.models.draft import DraftVersion
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    CampaignStatus,
    InsightState,
    PipelineStageStatus,
    SuppressionReason,
    SuppressionType,
)
from app.services import campaign_contacts, pipeline
from app.services.agents import controls
from app.services.agents.adapters import (
    AgentBlocked,
    AgentExecutionContext,
    AgentTerminalError,
    InsightsAgentAdapter,
    PersonalizationAgentAdapter,
    ResearchAgentAdapter,
)
from app.services.agents.jobs import enqueue_job
from app.services.agents.orchestrator import run_next
from app.services.companies import dossiers
from app.services.insights import evidence as insights_evidence
from app.services.suppressions import add_suppression
from app.services.thinking.contracts import ThinkingRequest, ThinkingResult, ThinkingTimeout
from sqlalchemy import select
from sqlalchemy.orm import Session

WORKER = "knowledge-test"


class ScriptedThinker:
    """Answers with a fixed payload, or raises a fixed error."""

    name = "scripted"
    version = "scripted/v1"

    def __init__(self, payload: dict[str, object] | None = None, *, error: Exception | None = None):
        self._payload = payload or {}
        self._error = error
        self.requests: list[ThinkingRequest] = []

    def think(self, request: ThinkingRequest) -> ThinkingResult:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return ThinkingResult(
            payload=self._payload,
            producer=self.name,
            producer_version=self.version,
            duration_seconds=0.01,
        )


def _records(db: Session) -> tuple[Campaign, Company, Contact]:
    company = Company(name="Kiln Systems", domain="kiln.example")
    campaign = Campaign(
        name=f"Knowledge {uuid.uuid4()}",
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
        email="ada@kiln.example",
        natural_key="ada|lovelace|kiln.example",
    )
    db.add(contact)
    db.flush()
    return campaign, company, contact


def _enable(db: Session, agent_id: AgentIdentifier, **config: object) -> None:
    controls.set_global_control(
        db,
        agent_id=agent_id,
        status=AgentControlStatus.ENABLED,
        config={"live": True, **config},
    )


def _context(
    db: Session,
    *,
    campaign: Campaign,
    contact: Contact,
    agent_id: AgentIdentifier,
    config: dict[str, object] | None = None,
) -> AgentExecutionContext:
    """Build one execution context directly.

    Insights and Personalization sit downstream of live email verification, which
    cannot run in an offline suite. Exercising the adapter at its own boundary
    tests the thing that matters — what it stores and what it refuses — without
    pretending a paid provider answered.
    """

    enrolled = campaign_contacts.enrol_contact(
        db,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=False,
    )
    contact.company_id = (
        db.scalars(select(Company).where(Company.domain == contact.company_domain)).one().id
    )
    db.flush()
    job, _ = enqueue_job(
        db,
        agent_id=agent_id,
        idempotency_key=f"test:{agent_id.value}:{uuid.uuid4()}",
        task_kind="advance_campaign_contact",
        max_attempts=3,
        campaign_id=campaign.id,
        campaign_contact_id=enrolled.membership.id,
        contact_id=contact.id,
    )
    return AgentExecutionContext(
        session=db,
        job=job,
        campaign=campaign,
        membership=enrolled.membership,
        contact=contact,
        config={"live": True, **(config or {})},
        worker_id=WORKER,
    )


# --- Research ---------------------------------------------------------------


def test_research_stores_a_dossier_and_advances_the_pipeline(db_session: Session) -> None:
    campaign, company, contact = _records(db_session)
    _enable(db_session, AgentIdentifier.RESEARCH)
    thinker = ScriptedThinker(
        {
            "overview": {"summary": "Kiln Systems builds industrial kiln controllers."},
            "products_services": [{"name": "KilnOS", "source_url": "https://kiln.example/os"}],
            "sources": [{"url": "https://kiln.example", "title": "Home"}],
            "unknowns": ["headcount"],
        }
    )
    adapters = {AgentIdentifier.RESEARCH: ResearchAgentAdapter(thinker_factory=lambda _s: thinker)}

    enrolled = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=True,
        desired_stage=AgentIdentifier.RESEARCH,
    )
    run_next(db_session, worker_id=WORKER)  # identity
    run_next(db_session, worker_id=WORKER)  # company
    outcome = run_next(db_session, worker_id=WORKER, adapters=adapters)

    assert outcome.public_status == "completed"
    stored = dossiers.current_version(db_session, company_id=company.id)
    assert stored is not None
    assert stored.interpreter == "research-agent"
    assert stored.overview == {"summary": "Kiln Systems builds industrial kiln controllers."}
    # A section the answer never mentioned stays NULL: "did not address it" is
    # not the same as "looked and found nothing".
    assert stored.leadership is None
    assert enrolled.membership.pipeline_status is PipelineStageStatus.COMPLETED

    state = pipeline.agent_state(
        db_session,
        campaign_contact_id=enrolled.membership.id,
        agent_id=AgentIdentifier.RESEARCH,
    )
    assert state is not None and state.output_reference is not None
    assert state.output_reference["unknown_count"] == 1
    assert "leadership" in state.output_reference["sections_unaddressed"]


def test_research_may_look_things_up_and_insights_may_not(db_session: Session) -> None:
    """Only the gathering stage gets tools; later stages reason over what it stored."""

    campaign, company, contact = _records(db_session)
    research_thinker = ScriptedThinker({"overview": {"summary": "s"}})
    context = _context(
        db_session, campaign=campaign, contact=contact, agent_id=AgentIdentifier.RESEARCH
    )
    ResearchAgentAdapter(thinker_factory=lambda _s: research_thinker).execute(context)
    assert research_thinker.requests[0].allowed_tools == ("WebSearch",)

    insights_thinker = ScriptedThinker(
        {"claims": [{"claim": "c", "source_url": "https://kiln.example", "evidence_summary": "e"}]}
    )
    context = _context(
        db_session, campaign=campaign, contact=contact, agent_id=AgentIdentifier.INSIGHTS
    )
    InsightsAgentAdapter(thinker_factory=lambda _s: insights_thinker).execute(context)
    assert insights_thinker.requests[0].allowed_tools == ()


def test_no_agent_runs_without_explicit_live_configuration(db_session: Session) -> None:
    """There is no simulated mode: fabricated research would reach a real email."""

    campaign, _, contact = _records(db_session)
    for agent_id, adapter in (
        (
            AgentIdentifier.RESEARCH,
            ResearchAgentAdapter(thinker_factory=lambda _s: ScriptedThinker()),
        ),
        (
            AgentIdentifier.INSIGHTS,
            InsightsAgentAdapter(thinker_factory=lambda _s: ScriptedThinker()),
        ),
        (
            AgentIdentifier.PERSONALIZATION,
            PersonalizationAgentAdapter(thinker_factory=lambda _s: ScriptedThinker()),
        ),
    ):
        context = _context(db_session, campaign=campaign, contact=contact, agent_id=agent_id)
        context.config["live"] = False
        with pytest.raises(AgentBlocked) as caught:
            adapter.execute(context)
        assert caught.value.code == "thinking_live_disabled"


def test_a_model_timeout_is_retryable_and_stores_nothing(db_session: Session) -> None:
    campaign, company, contact = _records(db_session)
    context = _context(
        db_session, campaign=campaign, contact=contact, agent_id=AgentIdentifier.RESEARCH
    )
    adapter = ResearchAgentAdapter(
        thinker_factory=lambda _s: ScriptedThinker(error=ThinkingTimeout("too slow"))
    )
    with pytest.raises(Exception) as caught:  # noqa: PT011 - class asserted below
        adapter.execute(context)
    assert caught.value.retryable is True  # type: ignore[attr-defined]
    assert dossiers.current_version(db_session, company_id=company.id) is None


# --- Insights ---------------------------------------------------------------


def test_an_unsourced_claim_is_dropped_rather_than_stored_as_a_weaker_fact(
    db_session: Session,
) -> None:
    campaign, company, contact = _records(db_session)
    context = _context(
        db_session, campaign=campaign, contact=contact, agent_id=AgentIdentifier.INSIGHTS
    )
    dossiers.interpret(
        db_session,
        company=company,
        submission=dossiers.submit(
            db_session, company=company, producer="test", payload={"overview": {"summary": "s"}}
        )[0],
        interpreter="test",
        sections={"overview": {"summary": "s"}},
    )
    thinker = ScriptedThinker(
        {
            "claims": [
                {
                    "claim": "Opened a second plant in Pune.",
                    "kind": "fact",
                    "source_url": "https://kiln.example/news/pune",
                    "evidence_summary": "The newsroom announced the Pune plant.",
                    "confidence": 0.8,
                },
                {
                    "claim": "Probably wants to cut energy costs.",
                    "kind": "interpretation",
                    # No source at all — the risky case.
                    "evidence_summary": "",
                },
                {
                    "claim": "Uses a made-up citation.",
                    "source_url": "not-a-url",
                    "evidence_summary": "x",
                },
            ],
            "unknowns": ["current controller vendor"],
        }
    )
    result = InsightsAgentAdapter(thinker_factory=lambda _s: thinker).execute(context)

    assert result.output_reference["insights_stored"] == 1
    assert result.output_reference["claims_dropped"] == 2
    stored = insights_evidence.list_for_company(db_session, company_id=company.id)
    supported = [row for row in stored if row.state is InsightState.SUPPORTED]
    assert len(supported) == 1
    assert supported[0].claim == "Opened a second plant in Pune."
    # The gap the model named is kept as an explicit unknown, not omitted.
    assert any(row.state is InsightState.UNKNOWN for row in stored)


def test_insights_blocks_when_nothing_survives_the_evidence_gate(db_session: Session) -> None:
    campaign, company, contact = _records(db_session)
    context = _context(
        db_session, campaign=campaign, contact=contact, agent_id=AgentIdentifier.INSIGHTS
    )
    dossiers.interpret(
        db_session,
        company=company,
        submission=dossiers.submit(
            db_session, company=company, producer="test", payload={"overview": {}}
        )[0],
        interpreter="test",
        sections={"overview": {}},
    )
    thinker = ScriptedThinker({"claims": [{"claim": "Unsourced."}], "unknowns": ["everything"]})
    with pytest.raises(AgentBlocked) as caught:
        InsightsAgentAdapter(thinker_factory=lambda _s: thinker).execute(context)
    assert caught.value.code == "insufficient_evidence"


def test_insights_needs_research_first(db_session: Session) -> None:
    campaign, _, contact = _records(db_session)
    context = _context(
        db_session, campaign=campaign, contact=contact, agent_id=AgentIdentifier.INSIGHTS
    )
    with pytest.raises(AgentBlocked) as caught:
        InsightsAgentAdapter(thinker_factory=lambda _s: ScriptedThinker()).execute(context)
    assert caught.value.code == "research_missing"


# --- Personalization --------------------------------------------------------


def _with_eligible_insight(db: Session, company: Company) -> str:
    from datetime import UTC, datetime

    insight = insights_evidence.create_insight(
        db,
        claim="Opened a second plant in Pune.",
        kind=insights_evidence.InsightKind.FACT,
        state=InsightState.SUPPORTED,
        evidence=[
            insights_evidence.EvidenceInput(
                source_url="https://kiln.example/news/pune",
                retrieved_at=datetime.now(UTC),
                evidence_summary="The newsroom announced the Pune plant.",
                confidence=0.8,
                extraction_method="test/v1",
            )
        ],
        company_id=company.id,
    )
    return str(insight.id)


def test_a_draft_is_stored_unapproved_and_cites_only_supplied_evidence(
    db_session: Session,
) -> None:
    campaign, company, contact = _records(db_session)
    insight_id = _with_eligible_insight(db_session, company)
    context = _context(
        db_session, campaign=campaign, contact=contact, agent_id=AgentIdentifier.PERSONALIZATION
    )
    thinker = ScriptedThinker(
        {
            "subject": "the pune plant",
            "body": "Ada — saw the Pune plant news.\n\nWorth a conversation?",
            "evidence_insight_ids": [insight_id],
            "rationale": "Led with the plant opening because it implies new controller demand.",
        }
    )
    result = PersonalizationAgentAdapter(thinker_factory=lambda _s: thinker).execute(context)

    draft = db_session.scalars(select(DraftVersion)).one()
    assert draft.subject == "the pune plant"
    assert draft.version_number == 1
    assert draft.campaign_id == campaign.id
    assert result.output_reference["approved"] is False
    assert result.output_reference["evidence_insight_ids"] == [insight_id]
    # A draft is never an approval: nothing wrote a DraftApproval row.
    from app.models.draft import DraftApproval

    assert db_session.scalars(select(DraftApproval)).all() == []


def test_a_draft_citing_evidence_never_supplied_is_refused(db_session: Session) -> None:
    """An untraceable citation destroys the only property that makes a draft reviewable."""

    campaign, company, contact = _records(db_session)
    _with_eligible_insight(db_session, company)
    context = _context(
        db_session, campaign=campaign, contact=contact, agent_id=AgentIdentifier.PERSONALIZATION
    )
    thinker = ScriptedThinker(
        {
            "subject": "s",
            "body": "b",
            "evidence_insight_ids": [str(uuid.uuid4())],
        }
    )
    with pytest.raises(AgentTerminalError) as caught:
        PersonalizationAgentAdapter(thinker_factory=lambda _s: thinker).execute(context)
    assert caught.value.code == "citation_not_supplied"
    assert db_session.scalars(select(DraftVersion)).all() == []


def test_an_empty_answer_is_accepted_as_evidence_being_too_thin(db_session: Session) -> None:
    """Returning nothing is a legitimate answer, and better than a generic email."""

    campaign, company, contact = _records(db_session)
    _with_eligible_insight(db_session, company)
    context = _context(
        db_session, campaign=campaign, contact=contact, agent_id=AgentIdentifier.PERSONALIZATION
    )
    thinker = ScriptedThinker(
        {"subject": "", "body": "", "rationale": "Only a generic fact was available."}
    )
    with pytest.raises(AgentBlocked) as caught:
        PersonalizationAgentAdapter(thinker_factory=lambda _s: thinker).execute(context)
    assert caught.value.code == "evidence_too_thin"
    assert db_session.scalars(select(DraftVersion)).all() == []


def test_drafting_refuses_without_any_eligible_evidence(db_session: Session) -> None:
    campaign, _, contact = _records(db_session)
    context = _context(
        db_session, campaign=campaign, contact=contact, agent_id=AgentIdentifier.PERSONALIZATION
    )
    with pytest.raises(AgentBlocked) as caught:
        PersonalizationAgentAdapter(thinker_factory=lambda _s: ScriptedThinker()).execute(context)
    assert caught.value.code == "no_eligible_evidence"


def test_a_suppression_added_after_enrolment_still_stops_the_draft(db_session: Session) -> None:
    """Drafting is the first step of writing to someone, so the ledger is re-read here."""

    campaign, company, contact = _records(db_session)
    _with_eligible_insight(db_session, company)
    context = _context(
        db_session, campaign=campaign, contact=contact, agent_id=AgentIdentifier.PERSONALIZATION
    )
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="ada@kiln.example",
        reason=SuppressionReason.OPT_OUT,
        actor="test",
    )
    thinker = ScriptedThinker({"subject": "s", "body": "b", "evidence_insight_ids": []})
    with pytest.raises(AgentBlocked) as caught:
        PersonalizationAgentAdapter(thinker_factory=lambda _s: thinker).execute(context)
    assert caught.value.code == "suppression"
    assert db_session.scalars(select(DraftVersion)).all() == []

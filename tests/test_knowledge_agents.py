"""Insights and Personalization: what they store, and what they refuse.

Every test here injects a scripted thinker. Nothing shells out, and nothing
reaches a network — which is the point of the seam these Agents were built
around.

The Research Agent used to be tested here too, against a model. It is not any
more: Research gathers through the registered research workers and is covered in
`tests/test_research_agent.py`. What is left of it here is the boundary that
separates gathering from reasoning.

The guarantees under test are mostly *negative*, because that is where the risk
lives. A model will always produce something plausible; the value of this layer
is what it declines to store.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Iterator

import pytest
from app.core.config import get_settings
from app.models.campaign import Campaign
from app.models.company import Company
from app.models.contact import Contact
from app.models.draft import DraftVersion
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    CampaignStatus,
    InsightState,
    SuppressionReason,
    SuppressionType,
)
from app.services import campaign_contacts
from app.services.agents import controls
from app.services.agents.adapters import (
    DEFAULT_ADAPTERS,
    AgentBlocked,
    AgentExecutionContext,
    AgentTerminalError,
    InsightsAgentAdapter,
    PersonalizationAgentAdapter,
)
from app.services.agents.jobs import enqueue_job
from app.services.companies import dossiers
from app.services.insights import evidence as insights_evidence
from app.services.suppressions import add_suppression
from app.services.thinking.contracts import ThinkingRequest, ThinkingResult, ThinkingTimeout
from sqlalchemy import select
from sqlalchemy.orm import Session

WORKER = "knowledge-test"


@pytest.fixture(autouse=True)
def _company_research_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reach the Research Agent's *live* gate rather than its deployment gate.

    The worker-based adapter checks `features.company_research` before it checks the
    per-campaign opt-in. Without this, a test asserting "it refuses until the
    campaign opts in" would pass for the wrong reason.
    """

    monkeypatch.setenv("FEATURES__COMPANY_RESEARCH", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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


# --- The boundary between gathering and reasoning ---------------------------
#
# The Research Agent is not tested here any more, because it no longer uses a
# language model: it reads pages through the registered research workers and
# records what they said. Its coverage lives in `tests/test_research_agent.py`.
#
# What remains here is the boundary that made that split necessary — only the
# gathering stage may reach outside, and the two Agents that do use a model may
# only reason over what gathering already stored.


def test_research_uses_no_language_model_at_all(db_session: Session) -> None:
    """The strongest form of "later stages cannot invent a source".

    This used to be a weaker claim: Research was given `allowed_tools=("WebSearch",)`
    while Insights and Personalization were given none. It is now structural — the
    Research adapter has no thinking seam to pass a prompt through, so there is no
    configuration under which it could produce a fact it did not read.
    """

    research = DEFAULT_ADAPTERS[AgentIdentifier.RESEARCH]
    parameters = inspect.signature(type(research).__init__).parameters
    assert "thinker_factory" not in parameters
    assert "workers_factory" in parameters, (
        "Research must gather through the worker registry; a thinker_factory here "
        "would mean a second, model-based Research implementation had returned"
    )


def test_the_two_model_agents_get_no_tools(db_session: Session) -> None:
    """Insights and Personalization reason only over what Research stored.

    Asserted on the request the adapter actually built, not on the prompt text: an
    empty `allowed_tools` is what makes it impossible for either of them to cite a
    page the evidence chain never gathered.
    """

    campaign, company, contact = _records(db_session)
    dossiers.interpret(
        db_session,
        company=company,
        submission=dossiers.submit(
            db_session, company=company, producer="test", payload={"overview": {"summary": "s"}}
        )[0],
        interpreter="test",
        sections={"overview": {"summary": "s"}},
    )

    insights_thinker = ScriptedThinker(
        {"claims": [{"claim": "c", "source_url": "https://kiln.example", "evidence_summary": "e"}]}
    )
    context = _context(
        db_session, campaign=campaign, contact=contact, agent_id=AgentIdentifier.INSIGHTS
    )
    InsightsAgentAdapter(thinker_factory=lambda _s: insights_thinker).execute(context)
    assert insights_thinker.requests[0].allowed_tools == ()

    eligible = _with_eligible_insight(db_session, company)
    draft_thinker = ScriptedThinker(
        {
            "subject": "s",
            "body": "b",
            "rationale": "r",
            "evidence_insight_ids": [eligible],
        }
    )
    context = _context(
        db_session, campaign=campaign, contact=contact, agent_id=AgentIdentifier.PERSONALIZATION
    )
    PersonalizationAgentAdapter(thinker_factory=lambda _s: draft_thinker).execute(context)
    assert draft_thinker.requests[0].allowed_tools == ()


def test_no_agent_runs_without_explicit_live_configuration(db_session: Session) -> None:
    """There is no simulated mode: fabricated research would reach a real email.

    Research refuses with its own code because it refuses for its own reason — it is
    about to read another organisation's website, not spend a model call. The
    guarantee is identical: nothing runs until a campaign says so.
    """

    campaign, _, contact = _records(db_session)
    for agent_id, adapter, expected in (
        (AgentIdentifier.RESEARCH, DEFAULT_ADAPTERS[AgentIdentifier.RESEARCH], "research_not_live"),
        (
            AgentIdentifier.INSIGHTS,
            InsightsAgentAdapter(thinker_factory=lambda _s: ScriptedThinker()),
            "thinking_live_disabled",
        ),
        (
            AgentIdentifier.PERSONALIZATION,
            PersonalizationAgentAdapter(thinker_factory=lambda _s: ScriptedThinker()),
            "thinking_live_disabled",
        ),
    ):
        context = _context(db_session, campaign=campaign, contact=contact, agent_id=agent_id)
        context.config["live"] = False
        with pytest.raises(AgentBlocked) as caught:
            adapter.execute(context)
        assert caught.value.code == expected, agent_id


def test_a_model_timeout_is_retryable_and_stores_nothing(db_session: Session) -> None:
    """The thinking seam's failure translation, on an Agent that still uses it."""

    campaign, company, contact = _records(db_session)
    dossiers.interpret(
        db_session,
        company=company,
        submission=dossiers.submit(
            db_session, company=company, producer="test", payload={"overview": {"summary": "s"}}
        )[0],
        interpreter="test",
        sections={"overview": {"summary": "s"}},
    )
    context = _context(
        db_session, campaign=campaign, contact=contact, agent_id=AgentIdentifier.INSIGHTS
    )
    adapter = InsightsAgentAdapter(
        thinker_factory=lambda _s: ScriptedThinker(error=ThinkingTimeout("too slow"))
    )
    with pytest.raises(Exception) as caught:  # noqa: PT011 - class asserted below
        adapter.execute(context)
    assert caught.value.retryable is True  # type: ignore[attr-defined]
    assert insights_evidence.list_for_company(db_session, company_id=company.id) == []


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

"""Campaign-scoped offering research read from a URL.

The feature is one Campaign's override of what it leads with. Everything below
is written to catch the ways that could stop being true:

* the Library is never written to, and another Campaign never notices;
* only a successful run becomes current, so a failed re-analysis is harmless;
* one Campaign never pitches two different things, because preparation waits
  while the first answer is still coming and does not wait once it has one;
* a model answer that is not a usable offering is refused rather than stored
  thin;
* an address that points inside the network never reaches a prompt.

No test here shells out. The language-model seam is a scripted ``Thinker``, which
is the contract ``app/services/thinking/contracts.py`` exists to make possible.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.core.config import Settings, get_settings
from app.models.campaign import Campaign
from app.models.campaign_offering_research import CampaignOfferingResearch
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    CampaignOfferingResearchStatus,
    CampaignOfferingSource,
    SellerOfferingType,
)
from app.models.seller_knowledge import SellerOffering
from app.services.agents import controls as agent_controls
from app.services.campaign_offering import consistency, jobs, read, runner
from app.services.campaign_offering.contracts import (
    OfferingResearchMalformed,
    parse_offering_payload,
)
from app.services.campaign_offering.urls import OfferingUrlError, normalize_offering_url
from app.services.campaigns import create_campaign
from app.services.seller import campaign_offerings as seller_campaign_offerings
from app.services.seller import effective as effective_offering
from app.services.seller import profile as seller_profile
from app.services.seller import records as seller_records
from app.services.thinking.contracts import (
    ThinkingRequest,
    ThinkingResult,
    ThinkingTransient,
    ThinkingUnavailable,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

PAGE = "https://example.com/reports/cement-outlook"

GOOD_ANSWER: dict[str, Any] = {
    "readable": True,
    "source_url_read": PAGE,
    "offering_name": "EU Cement Market Outlook 2027",
    "offering_type": "research_report",
    "summary": "A 180-page outlook on European cement demand, pricing and capacity.",
    "target_audience": ["Heads of strategy", "Commercial directors at producers"],
    "customer_problems": ["Capacity decisions are made without a demand baseline"],
    "use_cases": ["Board capacity papers", "Annual pricing review"],
    "key_capabilities": ["Country-level demand model", "Quarterly price series"],
    "benefits": ["A defensible demand baseline for capacity planning"],
    "market_context": ["European cement, 2024-2027"],
    "buyer_relevance": ["Capacity decisions are being taken this planning cycle"],
    "source_evidence": ["180 pages covering 22 European markets"],
    "seller_connection": (
        "This sits directly on our published market-research capability and the "
        "manufacturing sectors we already serve."
    ),
    "unknowns": ["The page does not state a price"],
}


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class ScriptedThinker:
    """Answers with a canned payload, or raises. Records what it was asked."""

    name = "scripted"
    version = "test"

    def __init__(self, payload: dict[str, Any] | None = None, error: Exception | None = None):
        self._payload = payload
        self._error = error
        self.requests: list[ThinkingRequest] = []

    def think(self, request: ThinkingRequest) -> ThinkingResult:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return ThinkingResult(
            payload=dict(self._payload or {}),
            producer=self.name,
            producer_version=self.version,
            duration_seconds=0.01,
        )


def _factory(thinker: ScriptedThinker):
    def build(_settings: Settings):
        return thinker

    return build


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def feature_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control is default-off; every run test has to turn it on explicitly."""

    monkeypatch.setenv("FEATURES__CAMPAIGN_OFFERING_RESEARCH", "true")
    monkeypatch.setenv("FEATURES__SELLER_KNOWLEDGE_BASE", "true")
    get_settings.cache_clear()


def make_campaign(session: Session, *, name: str = "Cement EU pilot") -> Campaign:
    campaign = create_campaign(session, name=name)
    session.flush()
    return campaign


def make_offering(session: Session, *, name: str = "Cement quarterly") -> SellerOffering:
    return seller_records.create_offering(
        session,
        name=name,
        offering_type=SellerOfferingType.RESEARCH_REPORT,
        short_description="The standing quarterly subscription.",
    )


def attach_library(session: Session, campaign: Campaign, offering: SellerOffering) -> None:
    seller_campaign_offerings.associate(session, campaign=campaign, offering_id=offering.id)
    session.flush()


def run_to_completion(
    session: Session,
    campaign: Campaign,
    thinker: ScriptedThinker,
    *,
    url: str = PAGE,
) -> CampaignOfferingResearch:
    """Request, claim and execute one run, as the worker does."""

    run = jobs.request_research(session, campaign=campaign, raw_url=url)
    claimed = jobs.claim_next(session, worker_id="test-worker")
    assert claimed is not None and claimed.id == run.id
    runner.execute_run(
        session, run=claimed, thinker_factory=_factory(thinker), worker_id="test-worker"
    )
    session.flush()
    return claimed


# ---------------------------------------------------------------------------
# 1-2. Library mode is unchanged, and URL mode is a deliberate election
# ---------------------------------------------------------------------------


def test_a_campaign_starts_in_library_mode_and_resolves_to_its_library_offering(
    db_session: Session,
) -> None:
    """Case 1. Nothing about an existing Campaign changes until somebody asks."""

    campaign = make_campaign(db_session)
    offering = make_offering(db_session)
    attach_library(db_session, campaign, offering)

    assert campaign.offering_source is CampaignOfferingSource.LIBRARY

    resolved = effective_offering.resolve(db_session, campaign)
    assert resolved.primary_source == effective_offering.PRIMARY_LIBRARY
    assert resolved.has_researched_primary is False
    assert [entry.offering.name for entry in resolved.seller.offerings] == [offering.name]
    # And the prompt block is the Library block unchanged, which is what keeps a
    # Library-only Campaign's copy byte-for-byte what it was.
    library_block = effective_offering.library_summary(resolved.seller)
    assert effective_offering.with_primary(resolved, library_block) == library_block


def test_choosing_url_mode_elects_it_and_queues_the_read_in_one_step(
    db_session: Session,
) -> None:
    """Case 2 and 3. The election and the run are written together.

    A Campaign in URL mode with nothing queued would mean "waiting forever", so
    no screen can produce it: :func:`request_research` writes both or neither.
    """

    campaign = make_campaign(db_session)
    run = jobs.request_research(db_session, campaign=campaign, raw_url=PAGE)

    assert campaign.offering_source is CampaignOfferingSource.URL_RESEARCH
    assert run.status is CampaignOfferingResearchStatus.QUEUED
    assert run.version_number == 1
    assert run.source_url == PAGE
    assert run.source_host == "example.com"
    assert run.is_current is False  # nothing is current until it succeeds
    assert run.offering_context is None

    stored = db_session.scalars(
        select(CampaignOfferingResearch).where(CampaignOfferingResearch.campaign_id == campaign.id)
    ).all()
    assert len(stored) == 1


def test_a_second_request_while_one_is_in_flight_is_refused_rather_than_queued(
    db_session: Session,
) -> None:
    """A double-click must not spend two model calls on one question."""

    campaign = make_campaign(db_session)
    jobs.request_research(db_session, campaign=campaign, raw_url=PAGE)

    with pytest.raises(jobs.OfferingResearchError) as refusal:
        jobs.request_research(db_session, campaign=campaign, raw_url=PAGE)
    assert refusal.value.code == "offering_research_in_flight"

    assert (
        db_session.scalars(
            select(CampaignOfferingResearch).where(
                CampaignOfferingResearch.campaign_id == campaign.id
            )
        ).all()
        != []
    )
    assert jobs.latest_run(db_session, campaign_id=campaign.id).version_number == 1


# ---------------------------------------------------------------------------
# 4. Status transitions are persisted
# ---------------------------------------------------------------------------


def test_the_statuses_a_customer_watches_are_written_down_as_they_happen(
    db_session: Session, feature_on: None
) -> None:
    """Case 4. Claiming commits ``READING`` before the model call, not after.

    This is the whole reason the worker splits the claim into its own
    transaction: a status that only appears once the call it describes has
    finished tells the customer nothing while they are waiting.
    """

    campaign = make_campaign(db_session)
    run = jobs.request_research(db_session, campaign=campaign, raw_url=PAGE)
    assert run.status is CampaignOfferingResearchStatus.QUEUED
    assert run.read_at is None

    claimed = jobs.claim_next(db_session, worker_id="w1")
    assert claimed is not None
    assert claimed.status is CampaignOfferingResearchStatus.READING
    assert claimed.read_at is not None
    assert claimed.lease_owner == "w1"
    assert claimed.attempts == 1

    view = read.campaign_offering_view(db_session, campaign)
    assert [step.label for step in view.steps] == list(read.PROGRESS_STEPS)
    assert view.steps[0].state == "active"
    assert view.in_flight is True

    runner.execute_run(
        db_session,
        run=claimed,
        thinker_factory=_factory(ScriptedThinker(GOOD_ANSWER)),
        worker_id="w1",
    )
    assert claimed.status is CampaignOfferingResearchStatus.READY
    assert claimed.analyzed_at is not None
    assert claimed.completed_at is not None
    assert claimed.lease_owner is None


# ---------------------------------------------------------------------------
# 5-8. What a successful read does, and everything it does not touch
# ---------------------------------------------------------------------------


def test_a_successful_read_becomes_the_campaigns_primary_offering(
    db_session: Session, feature_on: None
) -> None:
    """Case 5. Structured, current, and leading."""

    campaign = make_campaign(db_session)
    thinker = ScriptedThinker(GOOD_ANSWER)
    run = run_to_completion(db_session, campaign, thinker)

    assert run.status is CampaignOfferingResearchStatus.READY
    assert run.is_current is True
    assert run.producer == runner.PRODUCER
    assert run.context_digest
    assert run.offering_context["offering_name"] == GOOD_ANSWER["offering_name"]

    resolved = effective_offering.resolve(db_session, campaign)
    assert resolved.primary_source == effective_offering.PRIMARY_URL_RESEARCH
    assert resolved.offering is not None
    assert resolved.offering.offering_name == "EU Cement Market Outlook 2027"

    # The read is of the page it was given, with fetch and nothing else.
    assert thinker.requests[0].allowed_tools == ("WebFetch",)
    assert PAGE in thinker.requests[0].prompt


def test_the_library_offering_survives_as_supporting_context(
    db_session: Session, feature_on: None
) -> None:
    """Case 6. Supporting, labelled as supporting, and still in the prompt."""

    campaign = make_campaign(db_session)
    offering = make_offering(db_session)
    attach_library(db_session, campaign, offering)
    run = run_to_completion(db_session, campaign, ScriptedThinker(GOOD_ANSWER))

    resolved = effective_offering.resolve(db_session, campaign)
    library_block = effective_offering.library_summary(resolved.seller)
    prompt_block = effective_offering.with_primary(resolved, library_block)

    assert prompt_block.startswith("PRIMARY OFFERING — EU Cement Market Outlook 2027")
    assert "SUPPORTING OFFERING AND CREDIBILITY" in prompt_block
    assert offering.name in prompt_block
    assert library_block in prompt_block
    # And the version records which Library offering was supporting it, so the
    # pitch can be reconstructed later.
    assert run.supporting_offering_id == offering.id


def test_the_result_is_campaign_scoped_and_never_written_to_the_library(
    db_session: Session, feature_on: None
) -> None:
    """Case 7. The Library is exactly what it was before the read."""

    campaign = make_campaign(db_session)
    existing = make_offering(db_session)
    attach_library(db_session, campaign, existing)
    before = {row.id: row.name for row in seller_records.list_offerings(db_session)}

    run_to_completion(db_session, campaign, ScriptedThinker(GOOD_ANSWER))

    after = {row.id: row.name for row in seller_records.list_offerings(db_session)}
    assert after == before
    assert GOOD_ANSWER["offering_name"] not in after.values()
    # And the association is untouched: the researched offering is not a row here.
    linked = seller_campaign_offerings.offerings_for_campaign(db_session, campaign.id)
    assert [item.id for item in linked] == [existing.id]


def test_another_campaign_is_completely_unaffected(db_session: Session, feature_on: None) -> None:
    """Case 8. The override is one Campaign's, and the schema says so."""

    researched = make_campaign(db_session, name="Cement EU pilot")
    neighbour = make_campaign(db_session, name="Speciality chemicals pilot")
    offering = make_offering(db_session)
    attach_library(db_session, researched, offering)
    attach_library(db_session, neighbour, offering)

    run_to_completion(db_session, researched, ScriptedThinker(GOOD_ANSWER))

    assert neighbour.offering_source is CampaignOfferingSource.LIBRARY
    assert jobs.current_version(db_session, campaign_id=neighbour.id) is None

    other = effective_offering.resolve(db_session, neighbour)
    assert other.primary_source == effective_offering.PRIMARY_LIBRARY
    assert other.offering is None
    assert consistency.offering_context_hold(db_session, neighbour) is None


# ---------------------------------------------------------------------------
# 9-11. What the per-contact Agents are handed
# ---------------------------------------------------------------------------


def test_the_resolver_leads_with_the_url_offering_once_it_is_ready(
    db_session: Session, feature_on: None
) -> None:
    """Case 9."""

    campaign = make_campaign(db_session)
    attach_library(db_session, campaign, make_offering(db_session))
    run_to_completion(db_session, campaign, ScriptedThinker(GOOD_ANSWER))

    resolved = effective_offering.resolve(db_session, campaign)
    assert resolved.primary_source == effective_offering.PRIMARY_URL_RESEARCH
    assert resolved.preparing is False and resolved.fell_back is False
    # The researched words reach keyword matching too, so relevance scoring does
    # not treat the thing being sold as unrelated to the Campaign.
    assert "cement" in effective_offering.keyword_text(resolved).lower()


def test_the_resolver_falls_back_to_the_library_when_the_read_failed(
    db_session: Session, feature_on: None
) -> None:
    """Case 10. A failed Campaign is a usable Campaign."""

    campaign = make_campaign(db_session)
    offering = make_offering(db_session)
    attach_library(db_session, campaign, offering)

    thinker = ScriptedThinker(error=ThinkingUnavailable("no CLI on PATH"))
    run = run_to_completion(db_session, campaign, thinker)

    assert run.status is CampaignOfferingResearchStatus.FAILED
    assert run.is_current is False
    assert run.failure_code == "thinking_unavailable"

    resolved = effective_offering.resolve(db_session, campaign)
    assert resolved.primary_source == effective_offering.PRIMARY_LIBRARY
    assert resolved.fell_back is True
    assert [entry.offering.name for entry in resolved.seller.offerings] == [offering.name]
    # Nothing waits on an answer that is not coming.
    assert consistency.offering_context_hold(db_session, campaign) is None


def test_a_pending_read_holds_only_the_stages_that_depend_on_the_offering(
    db_session: Session, feature_on: None
) -> None:
    """Case 11. The whole point: one Campaign, one pitch.

    Insights chooses which recipient facts matter *to this offering* and
    Personalization writes the copy, so both are held. Everything else in the
    pipeline is about the recipient and is untouched — holding those would delay
    work the offering cannot change.
    """

    campaign = make_campaign(db_session)
    attach_library(db_session, campaign, make_offering(db_session))
    campaign.execution_enabled = True
    db_session.flush()

    for agent_id in (AgentIdentifier.INSIGHTS, AgentIdentifier.PERSONALIZATION):
        agent_controls.set_global_control(
            db_session, agent_id=agent_id, status=AgentControlStatus.ENABLED
        )

    jobs.request_research(db_session, campaign=campaign, raw_url=PAGE)

    for agent_id in consistency.OFFERING_DEPENDENT_AGENTS:
        control = agent_controls.effective_control(db_session, campaign=campaign, agent_id=agent_id)
        assert control.status is AgentControlStatus.PAUSED, agent_id
        assert control.source == consistency.OFFERING_RESEARCH_SOURCE
        assert "same thing" in (control.reason or "")

    # Nothing about the recipient is held.
    for agent_id in (
        AgentIdentifier.CAPTURE,
        AgentIdentifier.IDENTITY,
        AgentIdentifier.COMPANY,
        AgentIdentifier.RESEARCH,
        AgentIdentifier.EMAIL,
        AgentIdentifier.VERIFICATION,
    ):
        control = agent_controls.effective_control(db_session, campaign=campaign, agent_id=agent_id)
        assert control.source != consistency.OFFERING_RESEARCH_SOURCE, agent_id

    # And a Campaign that is still preparing is never handed a half state: the
    # resolver reports no researched primary at all while it waits.
    resolved = effective_offering.resolve(db_session, campaign)
    assert resolved.preparing is True
    assert resolved.has_researched_primary is False

    claimed = jobs.claim_next(db_session, worker_id="w1")
    assert claimed is not None
    runner.execute_run(
        db_session,
        run=claimed,
        thinker_factory=_factory(ScriptedThinker(GOOD_ANSWER)),
        worker_id="w1",
    )

    for agent_id in consistency.OFFERING_DEPENDENT_AGENTS:
        control = agent_controls.effective_control(db_session, campaign=campaign, agent_id=agent_id)
        assert control.status is AgentControlStatus.ENABLED, agent_id


def test_an_operator_pause_outranks_the_offering_hold(
    db_session: Session, feature_on: None
) -> None:
    """A temporary hold must not overwrite the reason an operator chose.

    Both answers are "paused", so only ``source`` tells them apart — and the
    operator's own decision is the one they need to see in order to undo it.

    The pause is written as a *Campaign override* rather than a global control
    because that is the one that actually applies here: ``create_campaign``
    already writes per-Campaign Agent rows, and a global control underneath an
    override is not the effective status. Pausing the wrong level would leave the
    Agent enabled, the hold would fire, and the test would pass for a reason
    that has nothing to do with what it claims.
    """

    campaign = make_campaign(db_session)
    campaign.execution_enabled = True
    agent_controls.set_campaign_override(
        db_session,
        campaign_id=campaign.id,
        agent_id=AgentIdentifier.PERSONALIZATION,
        status=AgentControlStatus.PAUSED,
        reason="held by the operator",
    )
    jobs.request_research(db_session, campaign=campaign, raw_url=PAGE)

    control = agent_controls.effective_control(
        db_session, campaign=campaign, agent_id=AgentIdentifier.PERSONALIZATION
    )
    assert control.status is AgentControlStatus.PAUSED
    assert control.source != consistency.OFFERING_RESEARCH_SOURCE
    assert control.reason == "held by the operator"


# ---------------------------------------------------------------------------
# 12-13. Versioning
# ---------------------------------------------------------------------------


def test_re_analysing_versions_rather_than_rewriting(db_session: Session, feature_on: None) -> None:
    """Case 12. The old answer keeps its row, its URL and its timestamps."""

    campaign = make_campaign(db_session)
    first = run_to_completion(db_session, campaign, ScriptedThinker(GOOD_ANSWER))
    first_id, first_digest = first.id, first.context_digest

    second_answer = dict(GOOD_ANSWER, offering_name="EU Cement Market Outlook 2028")
    second = run_to_completion(db_session, campaign, ScriptedThinker(second_answer))

    assert second.id != first_id
    assert second.version_number == 2
    assert second.is_current is True

    db_session.refresh(first)
    assert first.status is CampaignOfferingResearchStatus.READY
    assert first.is_current is False
    assert first.superseded_at is not None
    assert first.context_digest == first_digest  # untouched
    assert first.offering_context["offering_name"] == "EU Cement Market Outlook 2027"

    resolved = effective_offering.resolve(db_session, campaign)
    assert resolved.offering.offering_name == "EU Cement Market Outlook 2028"


def test_a_failed_re_analysis_leaves_the_last_good_answer_leading(
    db_session: Session, feature_on: None
) -> None:
    """Case 13. There is no code path from a failure to the current version."""

    campaign = make_campaign(db_session)
    good = run_to_completion(db_session, campaign, ScriptedThinker(GOOD_ANSWER))

    failed = run_to_completion(
        db_session,
        campaign,
        ScriptedThinker(error=ThinkingUnavailable("the CLI is not installed")),
        url="https://example.com/reports/other",
    )
    assert failed.status is CampaignOfferingResearchStatus.FAILED
    assert failed.is_current is False

    db_session.refresh(good)
    assert good.is_current is True
    assert good.superseded_at is None

    resolved = effective_offering.resolve(db_session, campaign)
    assert resolved.offering.offering_name == "EU Cement Market Outlook 2027"
    # And the Campaign never stopped: it had a current version throughout.
    assert consistency.offering_context_hold(db_session, campaign) is None


def test_a_re_analysis_in_flight_does_not_hold_a_campaign_that_already_has_an_answer(
    db_session: Session, feature_on: None
) -> None:
    """Every contact prepared during a re-analysis uses the same version.

    Which is the property the hold protects, so holding here would stop a working
    Campaign to wait for an improvement it already has a substitute for.
    """

    campaign = make_campaign(db_session)
    campaign.execution_enabled = True
    run_to_completion(db_session, campaign, ScriptedThinker(GOOD_ANSWER))
    jobs.request_research(db_session, campaign=campaign, raw_url=PAGE)

    assert consistency.offering_context_hold(db_session, campaign) is None
    view = read.campaign_offering_view(db_session, campaign)
    assert view.reanalyzing is True
    assert view.has_offering is True


def test_changing_the_url_keeps_the_history_and_only_switches_on_success(
    db_session: Session, feature_on: None
) -> None:
    """Both halves of "Change URL": a new version, and no early switch."""

    campaign = make_campaign(db_session)
    first = run_to_completion(db_session, campaign, ScriptedThinker(GOOD_ANSWER))

    other = "https://example.com/reports/concrete-additives"
    queued = jobs.request_research(db_session, campaign=campaign, raw_url=other)
    assert queued.version_number == 2
    assert queued.source_url == other
    db_session.refresh(first)
    assert first.is_current is True  # not switched while the new read is queued

    history = jobs.history(db_session, campaign_id=campaign.id)
    assert [item.version_number for item in history] == [2, 1]
    assert {item.source_url for item in history} == {PAGE, other}


# ---------------------------------------------------------------------------
# 14-15. Addresses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("", "url_missing"),
        ("not a url at all", "url_malformed"),
        ("ftp://example.com/x", "url_scheme_not_allowed"),
        ("https://user:secret@example.com/x", "url_has_credentials"),
        ("https://" + "a" * 3000, "url_too_long"),
    ],
)
def test_an_unusable_address_is_refused_on_the_form(raw: str, code: str) -> None:
    """Case 14. Refused before it can become a queued job and a model call."""

    with pytest.raises(OfferingUrlError) as refusal:
        normalize_offering_url(raw)
    assert refusal.value.code == code


@pytest.mark.parametrize(
    "raw",
    [
        "http://localhost:8000/admin",
        "http://127.0.0.1/",
        "https://10.0.0.5/offering",
        "https://192.168.1.10/offering",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "https://build-server.internal/offer",
        "https://printer.local/x",
        "https://intranet/offer",
    ],
)
def test_an_address_inside_the_network_never_reaches_a_prompt(raw: str) -> None:
    """Case 15. The application still fetches nothing; this is the prompt's gate.

    The model's fetch runs on the worker host, so a private address would be
    resolved with that host's network position. The tool may well refuse it —
    relying on another program's policy for our own safety property is what this
    refuses to do.
    """

    with pytest.raises(OfferingUrlError) as refusal:
        normalize_offering_url(raw)
    assert refusal.value.code == "url_not_public"


def test_a_public_address_is_accepted_and_normalized() -> None:
    """The refusals above prove nothing if everything is refused."""

    normalized, host = normalize_offering_url("  EXAMPLE.com/Reports/x?a=1#section  ")
    assert normalized == "https://example.com/Reports/x?a=1"
    assert host == "example.com"


def test_a_rejected_address_writes_nothing_at_all(db_session: Session) -> None:
    campaign = make_campaign(db_session)
    with pytest.raises(OfferingUrlError):
        jobs.request_research(db_session, campaign=campaign, raw_url="http://localhost/x")
    assert campaign.offering_source is CampaignOfferingSource.LIBRARY
    assert jobs.latest_run(db_session, campaign_id=campaign.id) is None


# ---------------------------------------------------------------------------
# 16. A model that cannot answer leaves a usable Campaign
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "code", "retryable"),
    [
        ({"readable": False, "unreadable_reason": "404"}, "page_unreadable", False),
        ({"summary": "x", "seller_connection": "y"}, "offering_name_missing", True),
        ({"offering_name": "x", "seller_connection": "y"}, "offering_summary_missing", True),
        ({"offering_name": "x", "summary": "y"}, "seller_connection_missing", True),
        ("not an object", "offering_not_an_object", True),
    ],
)
def test_an_answer_that_is_not_a_usable_offering_is_refused(
    payload: Any, code: str, retryable: bool
) -> None:
    """Nothing is defaulted in to make a run succeed."""

    with pytest.raises(OfferingResearchMalformed) as refusal:
        parse_offering_payload(payload)
    assert refusal.value.code == code


def test_a_model_failure_leaves_the_campaign_usable_and_says_one_thing_to_the_customer(
    db_session: Session, feature_on: None
) -> None:
    """Case 16."""

    campaign = make_campaign(db_session)
    offering = make_offering(db_session)
    attach_library(db_session, campaign, offering)

    run = run_to_completion(
        db_session, campaign, ScriptedThinker({"readable": False, "unreadable_reason": "404"})
    )
    assert run.status is CampaignOfferingResearchStatus.FAILED
    assert run.failure_code == "page_unreadable"

    view = read.campaign_offering_view(db_session, campaign)
    assert view.failed is True
    assert view.message == read.FAILURE_MESSAGE
    # The Campaign still has an offering to sell.
    assert effective_offering.resolve(db_session, campaign).seller.offerings


def test_a_transient_model_failure_is_retried_before_it_gives_up(
    db_session: Session, feature_on: None
) -> None:
    """A retry that could plausibly help is scheduled; the version is not spent."""

    campaign = make_campaign(db_session)
    run = run_to_completion(
        db_session, campaign, ScriptedThinker(error=ThinkingTransient("overloaded"))
    )
    assert run.status is CampaignOfferingResearchStatus.QUEUED
    assert run.attempts == 1
    assert run.failure_code == "thinking_transient"
    assert run.version_number == 1
    # Still active, so the Campaign is still waiting rather than falling back.
    assert consistency.offering_context_hold(db_session, campaign) is not None


def test_the_run_records_a_stated_reason_when_the_control_is_off(db_session: Session) -> None:
    """A switched-off control must not look like a healthy queue.

    ``feature_on`` is deliberately absent here: this is the default state.
    """

    campaign = make_campaign(db_session)
    thinker = ScriptedThinker(GOOD_ANSWER)
    run = run_to_completion(db_session, campaign, thinker)

    assert run.status is CampaignOfferingResearchStatus.FAILED
    assert run.failure_code == runner.FEATURE_DISABLED_CODE
    assert thinker.requests == []  # no model call was made


# ---------------------------------------------------------------------------
# 17. Nothing technical reaches the customer
# ---------------------------------------------------------------------------


def test_the_customer_view_carries_no_diagnostics(db_session: Session, feature_on: None) -> None:
    """Case 17. The projection cannot leak what it does not read."""

    campaign = make_campaign(db_session)
    run = run_to_completion(
        db_session, campaign, ScriptedThinker(error=ThinkingUnavailable("claude: not found"))
    )
    view = read.campaign_offering_view(db_session, campaign)

    rendered = " ".join(str(value) for value in vars(view).values() if not isinstance(value, tuple))
    for leak in (
        str(run.id),
        run.failure_code or "",
        run.failure_reason or "",
        run.idempotency_key,
        "claude",
        "lease",
        "attempt",
    ):
        assert leak.lower() not in rendered.lower(), leak
    assert view.message == read.FAILURE_MESSAGE

    # An administrator does get all of it, from a different function.
    diagnostics = read.admin_history(db_session, campaign_id=campaign.id)
    assert diagnostics[0].failure_code == "thinking_unavailable"
    assert diagnostics[0].attempts == 1


def test_library_mode_reports_nothing_about_a_run_it_is_not_using(
    db_session: Session, feature_on: None
) -> None:
    """Two answers to "what is this Campaign selling?" is one too many."""

    campaign = make_campaign(db_session)
    run_to_completion(db_session, campaign, ScriptedThinker(GOOD_ANSWER))
    jobs.use_library_offering(db_session, campaign=campaign)

    view = read.campaign_offering_view(db_session, campaign)
    assert view.is_url_mode is False
    assert view.offering is None
    assert view.source_url is None
    assert view.steps == ()


# ---------------------------------------------------------------------------
# 19. Lifecycle interactions
# ---------------------------------------------------------------------------


def test_pausing_the_campaign_outranks_the_offering_hold(
    db_session: Session, feature_on: None
) -> None:
    """Case 19. A paused Campaign reports the pause, not a wait inside it."""

    campaign = make_campaign(db_session)
    campaign.execution_enabled = False
    jobs.request_research(db_session, campaign=campaign, raw_url=PAGE)

    control = agent_controls.effective_control(
        db_session, campaign=campaign, agent_id=AgentIdentifier.PERSONALIZATION
    )
    assert control.status is AgentControlStatus.DISABLED
    assert control.source == agent_controls.CAMPAIGN_EXECUTION_SOURCE


def test_returning_to_the_library_cancels_the_read_and_keeps_every_version(
    db_session: Session, feature_on: None
) -> None:
    """Leaving URL mode is an election change, never a delete.

    The in-flight run is cancelled because letting it finish would silently
    re-elect URL mode when it promoted itself.
    """

    campaign = make_campaign(db_session)
    done = run_to_completion(db_session, campaign, ScriptedThinker(GOOD_ANSWER))
    queued = jobs.request_research(db_session, campaign=campaign, raw_url=PAGE)

    jobs.use_library_offering(db_session, campaign=campaign)

    assert campaign.offering_source is CampaignOfferingSource.LIBRARY
    db_session.refresh(queued)
    assert queued.status is CampaignOfferingResearchStatus.CANCELLED
    db_session.refresh(done)
    assert done.is_current is True  # kept, so electing URL mode again costs nothing
    assert len(jobs.history(db_session, campaign_id=campaign.id)) == 2

    resolved = effective_offering.resolve(db_session, campaign)
    assert resolved.primary_source == effective_offering.PRIMARY_LIBRARY

    # And electing it again leads with the version that was already paid for.
    campaign.offering_source = CampaignOfferingSource.URL_RESEARCH
    db_session.flush()
    assert (
        effective_offering.resolve(db_session, campaign).primary_source
        == effective_offering.PRIMARY_URL_RESEARCH
    )


def test_an_expired_lease_returns_the_run_to_the_queue_and_then_gives_up(
    db_session: Session, feature_on: None
) -> None:
    """A worker that died leaves recoverable work, not a run stuck at READING."""

    from datetime import UTC, datetime, timedelta

    campaign = make_campaign(db_session)
    jobs.request_research(db_session, campaign=campaign, raw_url=PAGE)
    claimed = jobs.claim_next(db_session, worker_id="w1", lease_seconds=1)
    assert claimed is not None

    later = datetime.now(UTC) + timedelta(minutes=5)
    jobs.recover_expired_leases(db_session, now=later)
    assert claimed.status is CampaignOfferingResearchStatus.QUEUED
    assert claimed.failure_code == "lease_expired"

    jobs.claim_next(db_session, worker_id="w2", lease_seconds=1, now=later)
    assert claimed.attempts == 2
    jobs.recover_expired_leases(db_session, now=later + timedelta(minutes=5))
    assert claimed.status is CampaignOfferingResearchStatus.FAILED


# ---------------------------------------------------------------------------
# The structured contract itself
# ---------------------------------------------------------------------------


def test_the_structure_is_bounded_but_never_invented() -> None:
    """Bounds are not fabrication: lists are capped, values are not made up."""

    payload = dict(
        GOOD_ANSWER,
        target_audience=[f"role {index}" for index in range(50)] + [None, 7],
        unknowns=[],
    )
    parsed = parse_offering_payload(payload)
    assert len(parsed.target_audience) == 12
    assert all(isinstance(item, str) for item in parsed.target_audience)
    assert parsed.unknowns == ()
    assert parsed.digest() == parse_offering_payload(payload).digest()


def test_a_thin_answer_is_kept_and_flagged_rather_than_refused() -> None:
    """A thin page is a real thing; the operator should see what was found."""

    parsed = parse_offering_payload(
        {
            "offering_name": "A report",
            "summary": "It is a report.",
            "seller_connection": "It is close to what we already publish.",
        }
    )
    assert parsed.is_thin is True
    assert parsed.offering_name == "A report"


def test_a_stored_payload_that_no_longer_parses_falls_back_rather_than_breaking(
    db_session: Session, feature_on: None
) -> None:
    """A Campaign must not break because a contract moved on."""

    campaign = make_campaign(db_session)
    offering = make_offering(db_session)
    attach_library(db_session, campaign, offering)
    run = run_to_completion(db_session, campaign, ScriptedThinker(GOOD_ANSWER))

    run.offering_context = {"offering_name": "", "summary": "", "seller_connection": ""}
    db_session.flush()

    resolved = effective_offering.resolve(db_session, campaign)
    assert resolved.primary_source == effective_offering.PRIMARY_LIBRARY
    assert [entry.offering.name for entry in resolved.seller.offerings] == [offering.name]


def test_the_seller_context_reaches_the_prompt_so_the_offering_can_be_connected(
    db_session: Session, feature_on: None
) -> None:
    """ "Connecting it to your company" has to be given the company to connect to."""

    campaign = make_campaign(db_session)
    seller_profile.save_profile(
        db_session,
        name="Verified Market Research",
        short_description="Syndicated and custom market research.",
    )
    attach_library(db_session, campaign, make_offering(db_session))

    thinker = ScriptedThinker(GOOD_ANSWER)
    run_to_completion(db_session, campaign, thinker)

    prompt = thinker.requests[0].prompt
    assert "Verified Market Research" in prompt
    assert "Cement quarterly" in prompt
    assert "WHAT WE SELL" in prompt


def test_the_schema_refuses_a_current_row_that_is_not_ready(
    db_session: Session, feature_on: None
) -> None:
    """The invariant is a database fact, not a service convention."""

    from sqlalchemy.exc import IntegrityError

    campaign = make_campaign(db_session)
    run = jobs.request_research(db_session, campaign=campaign, raw_url=PAGE)
    run.is_current = True
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_two_campaigns_can_each_have_their_own_current_version(
    db_session: Session, feature_on: None
) -> None:
    """The uniqueness is per Campaign, not global."""

    first = make_campaign(db_session, name="Cement EU pilot")
    second = make_campaign(db_session, name="Chemicals pilot")
    run_to_completion(db_session, first, ScriptedThinker(GOOD_ANSWER))
    run_to_completion(
        db_session, second, ScriptedThinker(dict(GOOD_ANSWER, offering_name="Another report"))
    )

    assert jobs.current_version(db_session, campaign_id=first.id) is not None
    assert jobs.current_version(db_session, campaign_id=second.id) is not None
    assert effective_offering.resolve(db_session, second).offering.offering_name == "Another report"


def test_the_queue_hands_one_run_to_one_worker(db_session: Session, feature_on: None) -> None:
    """Claiming twice does not yield the same run twice."""

    first = make_campaign(db_session, name="Cement EU pilot")
    second = make_campaign(db_session, name="Chemicals pilot")
    jobs.request_research(db_session, campaign=first, raw_url=PAGE)
    jobs.request_research(db_session, campaign=second, raw_url=PAGE)

    claimed_ids = {
        jobs.claim_next(db_session, worker_id="w1").id,
        jobs.claim_next(db_session, worker_id="w2").id,
    }
    assert len(claimed_ids) == 2
    assert jobs.claim_next(db_session, worker_id="w3") is None


def test_the_idempotency_key_is_unique_per_campaign_version(
    db_session: Session, feature_on: None
) -> None:
    campaign = make_campaign(db_session)
    first = run_to_completion(db_session, campaign, ScriptedThinker(GOOD_ANSWER))
    second = run_to_completion(db_session, campaign, ScriptedThinker(GOOD_ANSWER))
    assert first.idempotency_key != second.idempotency_key
    assert first.idempotency_key == jobs.idempotency_key(campaign_id=campaign.id, version_number=1)


def test_deleting_a_campaign_takes_its_research_with_it(
    db_session: Session, feature_on: None
) -> None:
    """The research is the Campaign's, and it does not outlive it."""

    campaign = make_campaign(db_session)
    run_to_completion(db_session, campaign, ScriptedThinker(GOOD_ANSWER))
    campaign_id = campaign.id
    db_session.delete(campaign)
    db_session.flush()

    remaining = db_session.scalars(
        select(CampaignOfferingResearch).where(CampaignOfferingResearch.campaign_id == campaign_id)
    ).all()
    assert remaining == []


def test_archiving_a_library_offering_does_not_break_a_researched_campaign(
    db_session: Session, feature_on: None
) -> None:
    """Case 19, the Library half. Supporting context can be withdrawn safely."""

    campaign = make_campaign(db_session)
    offering = make_offering(db_session)
    attach_library(db_session, campaign, offering)
    run = run_to_completion(db_session, campaign, ScriptedThinker(GOOD_ANSWER))

    seller_records.archive_offering(db_session, offering)
    db_session.flush()

    resolved = effective_offering.resolve(db_session, campaign)
    assert resolved.primary_source == effective_offering.PRIMARY_URL_RESEARCH
    block = effective_offering.with_primary(
        resolved, effective_offering.library_summary(resolved.seller)
    )
    assert "ARCHIVED" in block
    assert run.supporting_offering_id == offering.id


def test_the_version_records_who_asked_and_what_produced_it(
    db_session: Session, feature_on: None
) -> None:
    """Provenance, without the prompt or the raw answer."""

    campaign = make_campaign(db_session)
    run = jobs.request_research(
        db_session, campaign=campaign, raw_url=PAGE, requested_by="operator"
    )
    claimed = jobs.claim_next(db_session, worker_id="w1")
    runner.execute_run(
        db_session,
        run=claimed,
        thinker_factory=_factory(ScriptedThinker(GOOD_ANSWER)),
        worker_id="w1",
    )
    assert run.requested_by == "operator"
    assert run.producer == "campaign-offering-research"
    assert run.producer_version
    assert run.context_policy_version == "1"
    stored = str(run.offering_context)
    assert "WHAT WE SELL" not in stored
    assert "WHAT TO RETURN" not in stored


def test_a_campaign_id_that_does_not_exist_is_not_a_current_version(
    db_session: Session,
) -> None:
    assert jobs.current_version(db_session, campaign_id=uuid.uuid4()) is None
    assert jobs.active_run(db_session, campaign_id=uuid.uuid4()) is None

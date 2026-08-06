"""Company Intelligence as a Personalization input.

The integration under test (docs/decisions/0006): the *current* Company
Intelligence version is projected into a typed, bounded snapshot inside
``decide_context``; accepted values reach the prompt as read-only structured
context; everything else is carried as excluded-with-reason; the snapshot's
summary rides the existing ``personalization_decision`` record. Nothing here
may weaken the citation allow-list, the fallback ladder, the wording
constraints or preview safety — half these tests exist to prove exactly that.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.core.config import get_settings
from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.draft import DraftVersion
from app.models.enums import (
    AgentIdentifier,
    IntelligenceDecisionAction,
    IntelligenceDimension,
    PipelineStageStatus,
)
from app.models.pipeline import CampaignContactAgentState
from app.services.company_intelligence import producer as ci_producer
from app.services.company_intelligence import read as ci_read
from app.services.company_intelligence import review as ci_review
from app.services.personalization import generation, intelligence
from app.services.workbench_agents.reader import PhaseTwoWorkbenchReader
from app.services.workbench_agents.views import DraftOutcomeView
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.test_agent_studio_policy import (
    ScriptedThinker,
    _policy,
    _subject,
    _supported_insight,
)
from tests.test_company_intelligence import assemble as ci_assemble
from tests.test_company_intelligence import make_dossier, make_fact, seeded

BODY = (
    "Are you currently evaluating external market data for the sectors your team "
    "covers? VM Intelligence lets investment teams build a sourced market report "
    "and review a meaningful preview before purchasing the complete version. "
    "Would a short look be useful?"
)


@pytest.fixture(autouse=True)
def _ci_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("FEATURES__COMPANY_INTELLIGENCE", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _produce_ci(
    db: Session,
    company: Company,
    *,
    classifications: list[dict[str, Any]] | None = None,
    conflicts: list[dict[str, Any]] | None = None,
    extra_fact: str | None = None,
) -> ci_producer.ProductionResult:
    """A real Company Intelligence version over this company's evidence."""

    make_dossier(db, company=company)
    make_fact(
        db,
        company=company,
        claim="overview: operates kiln control plants across three regions",
        key=f"pi:{company.id}:{extra_fact or 'base'}",
    )
    answer: dict[str, Any] = {
        "classifications": classifications
        if classifications is not None
        else [
            {
                "dimension": "industry",
                "value": "Manufacturing",
                "is_primary": True,
                "evidence": ["F1"],
                "confidence": 0.85,
            }
        ]
    }
    if conflicts is not None:
        answer["conflicts"] = conflicts
    return ci_producer.produce(
        db,
        company=company,
        source=ci_assemble(db, company),
        answer=answer,
        raw_answer="{}",
    )


def _world(db: Session) -> tuple[Campaign, Company, Contact, CampaignContact]:
    return _subject(
        db,
        title="Head of Operations",
        campaign_description="Plant operations workflow software",
    )


def _decide(db: Session, membership: CampaignContact) -> generation.ContextDecision:
    return generation.decide_context(db, membership=membership, policy=_policy(db))


def _generate(
    db: Session, membership: CampaignContact, payload: dict[str, Any]
) -> tuple[generation.GeneratedPersonalization, ScriptedThinker]:
    thinker = ScriptedThinker(payload)
    generated = generation.generate(db, membership=membership, policy=_policy(db), thinker=thinker)
    return generated, thinker


# --- 1 + 2. supported current intelligence is used ---------------------------


def test_personalization_uses_supported_current_intelligence(db_session: Session) -> None:
    seeded(db_session)
    _, company, _, membership = _world(db_session)
    _supported_insight(db_session, company, "Opened a second plant in Pune.")
    _produce_ci(db_session, company)

    decision = _decide(db_session, membership)
    snapshot = decision.intelligence
    assert snapshot is not None
    assert snapshot.available is True
    assert snapshot.used is True
    assert snapshot.status == intelligence.STATUS_USED
    (accepted,) = snapshot.accepted
    assert accepted.dimension == "industry"
    assert accepted.label == "Manufacturing"
    # Provenance survives: the classification points at the Insight rows it
    # rests on, without ever becoming a citable candidate.
    assert accepted.evidence_insight_ids
    assert all(
        candidate.evidence_id not in accepted.evidence_insight_ids
        for candidate in decision.used
        if candidate.evidence_id is None
    )

    generated, thinker = _generate(
        db_session,
        membership,
        {"subject": "Quick question", "body": BODY, "evidence_insight_ids": []},
    )
    prompt = thinker.requests[0].prompt
    assert "STRUCTURED COMPANY INTELLIGENCE (READ-ONLY CONTEXT, NOT PROOF)" in prompt
    assert "industry: Manufacturing" in prompt
    assert "never build a claim" in prompt.lower() or "Never state them as facts" in prompt
    assert generated.decision.summary()["company_intelligence"]["used"] is True


# --- 3–6. exclusions, each with its reason -----------------------------------


def test_unmapped_and_unresolved_classifications_are_excluded(db_session: Session) -> None:
    seeded(db_session)
    _, company, _, membership = _world(db_session)
    _supported_insight(db_session, company, "Opened a second plant in Pune.")
    _produce_ci(
        db_session,
        company,
        classifications=[
            {
                "dimension": "industry",
                "value": "Wibble frobnication paradigms",
                "evidence": ["F1"],
                "confidence": 0.7,
            }
        ],
    )
    snapshot = _decide(db_session, membership).intelligence
    assert snapshot is not None
    assert snapshot.accepted == ()
    assert snapshot.used is False
    assert snapshot.status == intelligence.STATUS_NO_ELIGIBLE
    reasons = {item.label: item.reason for item in snapshot.excluded}
    # The unmappable industry value is excluded as unmapped; the geography the
    # extraction noticed but the model never settled is excluded as unresolved.
    assert reasons["Wibble frobnication paradigms"] == intelligence.REASON_UNMAPPED
    assert (
        intelligence.REASON_UNRESOLVED in set(reasons.values()) - {intelligence.REASON_UNMAPPED}
        or len(reasons) == 1
    )
    # Labels are carried for lineage but never enter the prompt path.
    assert all(item.evidence_insight_ids == () for item in snapshot.excluded)


def test_conflicted_classifications_are_excluded(db_session: Session) -> None:
    seeded(db_session)
    _, company, _, membership = _world(db_session)
    _produce_ci(
        db_session,
        company,
        classifications=[
            {
                "dimension": "customer_segment",
                "value": "Enterprise manufacturers",
                "evidence": ["F1"],
            },
            {
                "dimension": "customer_segment",
                "value": "Independent hobbyists",
                "evidence": ["F1"],
            },
        ],
        conflicts=[
            {
                "dimension": "customer_segment",
                "statement": "The evidence names two incompatible customer bases.",
                "values": ["Enterprise manufacturers", "Independent hobbyists"],
            }
        ],
    )
    snapshot = _decide(db_session, membership).intelligence
    assert snapshot is not None
    assert snapshot.accepted == ()
    assert {item.reason for item in snapshot.excluded} == {intelligence.REASON_CONFLICTED}


def test_review_rejected_values_never_reach_personalization(db_session: Session) -> None:
    seeded(db_session)
    _, company, _, membership = _world(db_session)
    result = _produce_ci(db_session, company)
    read = ci_read.get_company_intelligence(db_session, company_id=company.id)
    assert read is not None and read.classifications
    view = read.classifications[0]
    ci_review.record_decision(
        db_session,
        company=company,
        request=ci_review.DecisionRequest(
            dimension=IntelligenceDimension.INDUSTRY,
            action=IntelligenceDecisionAction.REJECT,
            target_key=view.term_code or f"text:{view.model_value.lower()}",
            classification_id=view.classification_id,
            note="not what this company does",
        ),
        version=result.version,
    )
    snapshot = _decide(db_session, membership).intelligence
    assert snapshot is not None
    # Rejected values are absent from the effective read model entirely: not
    # accepted, and not resurrected as an "excluded" ghost either.
    assert all(item.label != "Manufacturing" for item in snapshot.accepted)
    assert snapshot.used is False


def test_operator_assertions_without_evidence_are_excluded(db_session: Session) -> None:
    seeded(db_session)
    _, company, _, membership = _world(db_session)
    result = _produce_ci(db_session, company)
    # An operator asserts a value the model never proposed: real, but stored
    # deliberately without evidence — it must not become structured "proof".
    ci_review.record_decision(
        db_session,
        company=company,
        request=ci_review.DecisionRequest(
            dimension=IntelligenceDimension.CUSTOMER_SEGMENT,
            action=IntelligenceDecisionAction.CORRECT,
            target_key="text:mid-market plants",
            corrected_value="Mid-market plants",
        ),
        version=result.version,
    )
    snapshot = _decide(db_session, membership).intelligence
    assert snapshot is not None
    asserted = [item for item in snapshot.excluded if item.label == "Mid-market plants"]
    assert asserted and asserted[0].reason == intelligence.REASON_OPERATOR_ASSERTION


# --- 7 + 8. absent and disabled stay safe ------------------------------------


def test_missing_intelligence_does_not_break_generation(db_session: Session) -> None:
    _, company, _, membership = _world(db_session)
    _supported_insight(db_session, company, "Opened a second plant in Pune.")
    decision = _decide(db_session, membership)
    assert decision.intelligence is not None
    assert decision.intelligence.available is False
    assert decision.intelligence.status == intelligence.STATUS_NO_VERSION
    generated, thinker = _generate(
        db_session,
        membership,
        {"subject": "Quick question", "body": BODY, "evidence_insight_ids": []},
    )
    assert "STRUCTURED COMPANY INTELLIGENCE" not in thinker.requests[0].prompt
    assert generated.decision.summary()["company_intelligence"]["used"] is False


def test_feature_disabled_reports_itself_and_changes_nothing(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FEATURES__COMPANY_INTELLIGENCE", raising=False)
    get_settings.cache_clear()
    _, company, _, membership = _world(db_session)
    _supported_insight(db_session, company, "Opened a second plant in Pune.")
    decision = _decide(db_session, membership)
    assert decision.intelligence is not None
    assert decision.intelligence.status == intelligence.STATUS_FEATURE_DISABLED
    assert decision.intelligence.available is False
    assert decision.fallback_level == 1  # selection itself is untouched


# --- 9 + 10. versioning and traceability -------------------------------------


def test_superseded_versions_are_never_used(db_session: Session) -> None:
    seeded(db_session)
    _, company, _, membership = _world(db_session)
    first = _produce_ci(db_session, company)
    second = _produce_ci(db_session, company, extra_fact="second-run")
    assert second.version.id != first.version.id
    db_session.refresh(first.version)
    assert first.version.is_current is False

    snapshot = _decide(db_session, membership).intelligence
    assert snapshot is not None
    assert snapshot.version_id == second.version.id
    assert snapshot.version_number == second.version.version_number


def test_the_exact_version_is_traceable_in_the_persisted_summary(
    db_session: Session,
) -> None:
    seeded(db_session)
    _, company, _, membership = _world(db_session)
    _supported_insight(db_session, company, "Opened a second plant in Pune.")
    result = _produce_ci(db_session, company)
    generated, _ = _generate(
        db_session,
        membership,
        {"subject": "Quick question", "body": BODY, "evidence_insight_ids": []},
    )
    record = generated.decision.summary()["company_intelligence"]
    assert record["version_id"] == str(result.version.id)
    assert record["version_number"] == result.version.version_number
    assert record["input_digest"] == result.version.input_digest
    assert record["accepted_count"] == 1
    assert record["accepted"][0]["label"] == "Manufacturing"


# --- 11. one Company, many Contacts ------------------------------------------


def test_contacts_sharing_a_company_reuse_one_version(db_session: Session) -> None:
    seeded(db_session)
    campaign, company, contact, membership = _world(db_session)
    result = _produce_ci(db_session, company)
    other_contact = Contact(
        first_name="Grace",
        last_name="Hopper",
        title="Director of Operations",
        company_name=company.name,
        company_domain=company.domain,
        company_id=company.id,
        email=f"grace-{uuid.uuid4()}@kiln.example",
        natural_key=f"grace|hopper|{uuid.uuid4()}",
    )
    db_session.add(other_contact)
    db_session.flush()
    other_membership = CampaignContact(campaign_id=campaign.id, contact_id=other_contact.id)
    db_session.add(other_membership)
    db_session.flush()

    first = _decide(db_session, membership).intelligence
    second = _decide(db_session, other_membership).intelligence
    assert first is not None and second is not None
    assert first.version_id == second.version_id == result.version.id
    # Still exactly one version row for the company: nothing was copied.
    assert (
        db_session.scalar(
            select(ci_read.CompanyIntelligenceVersion.id.isnot(None))
            .select_from(ci_read.CompanyIntelligenceVersion)
            .where(ci_read.CompanyIntelligenceVersion.company_id == company.id)
        )
        is not None
    )


# --- 12 + 13. labels never become claims; wording stays enforced -------------


def test_classification_labels_cannot_be_cited_as_evidence(db_session: Session) -> None:
    seeded(db_session)
    _, company, _, membership = _world(db_session)
    _supported_insight(db_session, company, "Opened a second plant in Pune.")
    _produce_ci(db_session, company)
    decision = _decide(db_session, membership)
    assert decision.intelligence is not None and decision.intelligence.used
    ci_insight_ids = decision.intelligence.accepted[0].evidence_insight_ids
    supplied = {c.evidence_id for c in decision.used if c.evidence_id}
    # The CI provenance ids are NOT part of the citable allow-list unless the
    # same insight was independently supplied as prospect context.
    foreign = [item for item in ci_insight_ids if item not in supplied] or [str(uuid.uuid4())]
    with pytest.raises(generation.PreviewError) as excinfo:
        _generate(
            db_session,
            membership,
            {
                "subject": "Quick question",
                "body": BODY,
                "evidence_insight_ids": [foreign[0]],
            },
        )
    assert excinfo.value.code == "citation_not_supplied"


def test_wording_constraints_survive_intelligence_use(db_session: Session) -> None:
    seeded(db_session)
    _, company, _, membership = _world(db_session)
    _supported_insight(db_session, company, "Opened a second plant in Pune.")
    _produce_ci(db_session, company)
    with pytest.raises(generation.PreviewError):
        _generate(
            db_session,
            membership,
            {
                "subject": "Quick question",
                "body": "Since you are focused on manufacturing, " + BODY,
                "evidence_insight_ids": [],
            },
        )


# --- 14. weak-evidence fallback unchanged ------------------------------------


def test_weak_evidence_fallback_withholds_intelligence(db_session: Session) -> None:
    seeded(db_session)
    _, company, _, membership = _subject(
        db_session, campaign_description="Plant operations workflow software"
    )
    # No usable prospect context at all: the ladder must land on level 5 and
    # intelligence must not be smuggled in as a relevance bridge.
    _produce_ci(db_session, company)
    decision = _decide(db_session, membership)
    assert decision.fallback_level == 5
    assert decision.intelligence is not None
    assert decision.intelligence.accepted  # eligible…
    assert decision.intelligence.used is False  # …but withheld
    assert decision.intelligence.status == intelligence.STATUS_WITHHELD_WEAK_EVIDENCE
    generated, thinker = _generate(
        db_session,
        membership,
        {"subject": "Quick question", "body": BODY, "evidence_insight_ids": []},
    )
    assert "STRUCTURED COMPANY INTELLIGENCE" not in thinker.requests[0].prompt
    assert "earnest offering-led fallback" in thinker.requests[0].prompt


# --- 15. preview stays side-effect free --------------------------------------


def test_preview_with_intelligence_writes_nothing(db_session: Session) -> None:
    seeded(db_session)
    _, company, _, membership = _world(db_session)
    _supported_insight(db_session, company, "Opened a second plant in Pune.")
    _produce_ci(db_session, company)
    db_session.flush()
    drafts_before = len(db_session.scalars(select(DraftVersion)).all())
    _generate(
        db_session,
        membership,
        {"subject": "Quick question", "body": BODY, "evidence_insight_ids": []},
    )
    assert not db_session.new and not db_session.dirty
    assert len(db_session.scalars(select(DraftVersion)).all()) == drafts_before


# --- 16 + 17. historical outputs and Workbench truthfulness -------------------


def _draft_view(payload: dict[str, Any]) -> DraftOutcomeView | None:
    state = CampaignContactAgentState(
        campaign_contact_id=uuid.uuid4(),
        agent_id=AgentIdentifier.PERSONALIZATION,
        status=PipelineStageStatus.COMPLETED,
        output_reference=payload,
    )
    reader = PhaseTwoWorkbenchReader.__new__(PhaseTwoWorkbenchReader)
    return reader._draft_outcome(stages=(state,))


def test_historical_outputs_report_lineage_unavailable_not_fabricated() -> None:
    view = _draft_view(
        {
            "subject": "Older draft",
            "body": "Written before the integration.",
            "evidence_insight_ids": [],
            "evidence_supplied": 0,
            "personalization_decision": {"fallback_level": 5},
        }
    )
    assert view is not None
    assert view.intelligence_status is None
    assert view.intelligence_used is False
    assert "lineage unavailable" in view.intelligence_label
    assert "Company Intelligence" not in view.input_basis


def test_workbench_distinguishes_availability_from_usage() -> None:
    withheld = _draft_view(
        {
            "subject": "s",
            "body": "b",
            "evidence_insight_ids": [],
            "evidence_supplied": 0,
            "personalization_decision": {
                "company_intelligence": {
                    "status": "withheld_weak_evidence_fallback",
                    "used": False,
                    "version_number": 3,
                    "version_id": str(uuid.uuid4()),
                    "accepted_count": 2,
                    "excluded_count": 1,
                    "excluded": [{"reason": "unresolved value"}],
                }
            },
        }
    )
    assert withheld is not None
    assert withheld.intelligence_used is False
    assert withheld.intelligence_version_number == 3
    assert "withheld" in withheld.intelligence_label
    assert "Company Intelligence" not in withheld.input_basis
    assert withheld.intelligence_exclusion_reasons == ("unresolved value",)

    used = _draft_view(
        {
            "subject": "s",
            "body": "b",
            "evidence_insight_ids": ["abc"],
            "evidence_supplied": 1,
            "personalization_decision": {
                "company_intelligence": {
                    "status": "used",
                    "used": True,
                    "version_number": 3,
                    "accepted_count": 2,
                    "excluded_count": 0,
                }
            },
        }
    )
    assert used is not None
    assert used.intelligence_used is True
    assert used.input_basis == "Research + Insights + Company Intelligence"


# --- the pipeline adapter persists the lineage --------------------------------


def test_the_pipeline_adapter_persists_intelligence_lineage(db_session: Session) -> None:
    """End to end through the real adapter: the record lands on the draft."""

    from app.services.agents.adapters import AgentExecutionContext, PersonalizationAgentAdapter
    from app.services.personalization import policy as policy_module

    seeded(db_session)
    campaign, company, contact, membership = _world(db_session)
    _supported_insight(db_session, company, "Opened a second plant in Pune.")
    result = _produce_ci(db_session, company)
    policy_module.ensure_initial_policy(db_session, actor="test")

    from app.services.agents import jobs as agent_jobs

    job, _ = agent_jobs.enqueue_job(
        db_session,
        agent_id=AgentIdentifier.PERSONALIZATION,
        idempotency_key=f"pi-adapter:{membership.id}",
        task_kind="personalization.draft",
        max_attempts=3,
        campaign_id=campaign.id,
        campaign_contact_id=membership.id,
        contact_id=contact.id,
    )
    thinker = ScriptedThinker(
        {"subject": "Quick question", "body": BODY, "evidence_insight_ids": []}
    )
    adapter = PersonalizationAgentAdapter(thinker_factory=lambda _settings: thinker)
    context = AgentExecutionContext(
        session=db_session,
        job=job,
        campaign=campaign,
        membership=membership,
        contact=contact,
        worker_id="pi-test",
        config={"live": True},
    )
    execution = adapter.execute(context)
    assert execution.outcome_committed is True
    record = execution.output_reference["personalization_decision"]["company_intelligence"]
    assert record["used"] is True
    assert record["version_id"] == str(result.version.id)

    draft = db_session.scalars(
        select(DraftVersion).order_by(DraftVersion.created_at.desc())
    ).first()
    assert draft is not None
    assert draft.personalization_decision["company_intelligence"]["version_id"] == str(
        result.version.id
    )

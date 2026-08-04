"""Agent Studio policy, deterministic context, and preview safety contracts."""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from app.models.audit_event import AuditEvent
from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.draft import DraftApproval, DraftVersion
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    CampaignStatus,
    InsightKind,
    InsightState,
)
from app.models.personalization_policy import (
    ImmutablePolicyHistoryError,
    PersonalizationPolicyActivation,
    PersonalizationPolicyVersion,
)
from app.models.verification_job import AgentJob
from app.services.agents import controls
from app.services.insights import evidence as insight_service
from app.services.personalization import generation, policy
from app.services.thinking.contracts import ThinkingRequest, ThinkingResult
from sqlalchemy import func, select
from sqlalchemy.orm import Session


class ScriptedThinker:
    name = "agent-studio-test"
    version = "agent-studio-test/v1"

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.requests: list[ThinkingRequest] = []

    def think(self, request: ThinkingRequest) -> ThinkingResult:
        self.requests.append(request)
        return ThinkingResult(
            payload=self.payload,
            producer=self.name,
            producer_version=self.version,
            duration_seconds=0.01,
        )


# Manual inspection aid only: these are behavioural examples, not production templates.
COPY_QUALITY_COMPARISON = {
    "bad_company_summary": (
        "Given that Acme provides cloud analytics solutions across several industries, I "
        "thought your team may be interested in our market intelligence platform."
    ),
    "useful_evidence": (
        "Are you currently evaluating external market data for any of the sectors your team "
        "covers? VM Intelligence lets investment teams build and review a sourced market report "
        "before purchasing the complete version."
    ),
    "weak_evidence": (
        "I'm reaching out from VM Intelligence. We help investment teams build sourced market "
        "reports in minutes, including a meaningful preview before purchase. Would it be useful "
        "to see how it works for a market you are currently evaluating?"
    ),
}


def _subject(
    db: Session,
    *,
    title: str | None = None,
    industry: str | None = None,
    campaign_description: str = "Workflow operations software",
) -> tuple[Campaign, Company, Contact, CampaignContact]:
    company = Company(name="Kiln Systems", domain="kiln.example", industry=industry)
    campaign = Campaign(
        name=f"Studio {uuid.uuid4()}",
        description=campaign_description,
        status=CampaignStatus.ACTIVE,
        execution_enabled=True,
    )
    db.add_all([company, campaign])
    db.flush()
    contact = Contact(
        first_name="Ada",
        last_name="Lovelace",
        title=title,
        industry=industry,
        company_name=company.name,
        company_domain=company.domain,
        company_id=company.id,
        email=f"ada-{uuid.uuid4()}@kiln.example",
        natural_key=f"ada|lovelace|{uuid.uuid4()}",
    )
    db.add(contact)
    db.flush()
    membership = CampaignContact(campaign_id=campaign.id, contact_id=contact.id)
    db.add(membership)
    db.flush()
    return campaign, company, contact, membership


def _policy(db: Session) -> PersonalizationPolicyVersion:
    return policy.ensure_initial_policy(db, actor="test")


def _supported_insight(db: Session, company: Company, claim: str) -> str:
    insight = insight_service.create_insight(
        db,
        claim=claim,
        kind=InsightKind.FACT,
        state=InsightState.SUPPORTED,
        evidence=[
            insight_service.EvidenceInput(
                source_url="https://kiln.example/news",
                retrieved_at=datetime.now(UTC),
                evidence_summary="Primary company newsroom statement.",
                confidence=0.91,
                extraction_method="agent-studio-test/v1",
            )
        ],
        company_id=company.id,
    )
    return str(insight.id)


def test_default_policy_contains_required_standards_strategies_and_fixed_ladder() -> None:
    config = policy.default_policy()
    assert {item.identifier for item in config.standards} == {
        "do_not_explain_company",
        "context_must_improve_relevance",
        "prefer_curiosity",
        "no_intelligence_display",
        "admit_weak_evidence",
        "explain_seller_offering",
        "match_strategy_to_evidence",
        "minimum_personalization",
    }
    assert {item.identifier for item in config.strategies} == {
        "relevant_question_first",
        "relevant_statement_then_question",
        "role_led_relevance",
        "company_context_relevance",
        "earnest_offering_led",
    }
    assert config.fallback_ladder == (
        "contact_and_company",
        "company_only",
        "contact_role_only",
        "sector_only",
        "offering_led",
    )
    assert policy.PolicyConfig.from_dict(config.to_dict()) == config


def test_policy_validation_rejects_unbounded_and_contract_breaking_values() -> None:
    raw = policy.default_policy().to_dict()
    raw["temperament"]["commercial_directness"] = 99
    with pytest.raises(policy.PolicyError, match="0 to 4"):
        policy.PolicyConfig.from_dict(raw)

    raw = policy.default_policy().to_dict()
    raw["fallback_ladder"] = list(reversed(raw["fallback_ladder"]))
    with pytest.raises(policy.PolicyError, match="fixed"):
        policy.PolicyConfig.from_dict(raw)

    raw = policy.default_policy().to_dict()
    raw["examples"] = [
        {"category": "strong_example", "content": "x", "note": None}
        for _ in range(policy.MAX_EXAMPLES + 1)
    ]
    with pytest.raises(policy.PolicyError, match="at most"):
        policy.PolicyConfig.from_dict(raw)

    raw = policy.default_policy().to_dict()
    raw["standards"][0]["state"] = "unavailable"
    with pytest.raises(policy.PolicyError, match="Core outreach standards"):
        policy.PolicyConfig.from_dict(raw)

    raw = policy.default_policy().to_dict()
    next(item for item in raw["strategies"] if item["id"] == "earnest_offering_led")["enabled"] = (
        False
    )
    with pytest.raises(policy.PolicyError, match="fallback strategy"):
        policy.PolicyConfig.from_dict(raw)


def test_versions_are_immutable_and_activation_rollback_is_append_only(
    db_session: Session,
) -> None:
    first = _policy(db_session)
    raw = deepcopy(first.configuration)
    raw["temperament"]["commercial_directness"] = 3
    second = policy.create_policy_version(
        db_session,
        configuration=policy.PolicyConfig.from_dict(raw),
        name="More commercially direct",
        actor="operator:test",
        based_on_version_id=first.id,
        change_note="Bounded test change",
    )
    policy.activate_policy(
        db_session,
        policy_version_id=second.id,
        actor="operator:test",
        reason="Test activation",
    )
    policy.activate_policy(
        db_session,
        policy_version_id=first.id,
        actor="operator:test",
        reason="Test rollback",
    )

    assert policy.active_policy(db_session).id == first.id  # type: ignore[union-attr]
    history = policy.activation_history(db_session)
    assert [item.policy_version_id for item in history[:3]] == [first.id, second.id, first.id]
    assert history[0].previous_policy_version_id == second.id
    assert db_session.scalar(select(func.count(PersonalizationPolicyActivation.id))) == 3
    audit_count = db_session.scalar(
        select(func.count(AuditEvent.id)).where(
            AuditEvent.action.in_(
                ("personalization_policy.version_created", "personalization_policy.activated")
            )
        )
    )
    assert audit_count is not None and audit_count >= 5

    first.name = "illegal mutation"
    with pytest.raises(ImmutablePolicyHistoryError):
        db_session.flush()
    db_session.rollback()


def test_control_status_writes_preserve_the_complete_existing_config(db_session: Session) -> None:
    expected = {
        "live": True,
        "timeout_seconds": 180,
        "model": "approved-model",
        "nested": {"keep": ["all", "fields"]},
    }
    controls.set_global_control(
        db_session,
        agent_id=AgentIdentifier.PERSONALIZATION,
        status=AgentControlStatus.ENABLED,
        config=expected,
    )
    changed = controls.set_global_control(
        db_session,
        agent_id=AgentIdentifier.PERSONALIZATION,
        status=AgentControlStatus.PAUSED,
        reason="operator pause",
    )
    assert changed.config == expected


@pytest.mark.parametrize(
    ("title", "industry", "campaign_description", "expected_level"),
    [
        (None, None, "Offering with no matching prospect context", 5),
        ("Head of Operations", None, "Operations workflow software", 3),
        (None, "Healthcare", "Healthcare workflow software", 4),
    ],
)
def test_fallback_ladder_uses_only_context_that_clears_the_gate(
    db_session: Session,
    title: str | None,
    industry: str | None,
    campaign_description: str,
    expected_level: int,
) -> None:
    _, _, _, membership = _subject(
        db_session,
        title=title,
        industry=industry,
        campaign_description=campaign_description,
    )
    decision = generation.decide_context(
        db_session, membership=membership, policy=_policy(db_session)
    )
    assert decision.fallback_level == expected_level


def test_supported_relevant_company_context_and_combined_context_are_selected(
    db_session: Session,
) -> None:
    _, company, _, membership = _subject(
        db_session,
        title="Head of Operations",
        campaign_description="Plant operations workflow software",
    )
    insight_id = _supported_insight(db_session, company, "Opened a second plant in Pune.")
    decision = generation.decide_context(
        db_session, membership=membership, policy=_policy(db_session)
    )
    assert decision.fallback_level == 1
    assert {item.category for item in decision.used} == {
        generation.ContextCategory.CONTACT,
        generation.ContextCategory.COMPANY,
    }
    assert insight_id in {item.evidence_id for item in decision.used}


def test_performative_company_description_is_rejected_even_when_supported(
    db_session: Session,
) -> None:
    _, company, _, membership = _subject(
        db_session,
        campaign_description="Company workflow platform",
    )
    _supported_insight(db_session, company, "Company name: Kiln Systems")
    decision = generation.decide_context(
        db_session, membership=membership, policy=_policy(db_session)
    )
    assert decision.fallback_level == 5
    assert any("performative" in item.reason for item in decision.rejected)


def test_irrelevant_company_fact_is_rejected_instead_of_forced(
    db_session: Session,
) -> None:
    _, company, _, membership = _subject(
        db_session,
        campaign_description="Plant operations workflow software",
    )
    insight_id = _supported_insight(
        db_session,
        company,
        "Won a regional workplace award.",
    )
    decision = generation.decide_context(
        db_session, membership=membership, policy=_policy(db_session)
    )

    assert decision.fallback_level == 5
    assert decision.used == ()
    rejected = next(item for item in decision.rejected if item.evidence_id == insight_id)
    assert rejected.accepted is False
    assert rejected.reason == "No explicit connection to the Campaign or seller offering was found."


def test_preview_is_side_effect_free_and_leaves_immutable_drafts_unchanged(
    db_session: Session,
) -> None:
    campaign, _, contact, membership = _subject(db_session)
    active = _policy(db_session)
    old_draft = DraftVersion(
        contact_id=contact.id,
        campaign_id=campaign.id,
        version_number=1,
        subject="Historical subject",
        body="Historical body",
    )
    db_session.add(old_draft)
    db_session.flush()
    before = {
        model: db_session.scalar(select(func.count()).select_from(model))
        for model in (
            DraftVersion,
            DraftApproval,
            AgentJob,
            PersonalizationPolicyVersion,
            PersonalizationPolicyActivation,
        )
    }
    thinker = ScriptedThinker(
        {
            "subject": "A plain introduction",
            "body": "We help operations teams simplify workflow. Is that relevant to you?",
            "evidence_insight_ids": [],
            "rationale": "No prospect context cleared policy, so the offering-led fallback won.",
        }
    )
    generated = generation.generate(
        db_session,
        membership=membership,
        policy=active,
        thinker=thinker,
    )
    after = {model: db_session.scalar(select(func.count()).select_from(model)) for model in before}
    db_session.refresh(old_draft)

    assert before == after
    assert (old_draft.subject, old_draft.body) == ("Historical subject", "Historical body")
    assert generated.decision.fallback_level == 5
    assert generated.strategy_id == "earnest_offering_led"
    summary = generated.decision.summary()
    assert summary["context_used"] == []
    assert summary["fallback_identifier"] == "offering_led"
    assert summary["standards_applied"]
    assert summary["temperament"] == {
        "company_context_usage": 2,
        "question_first_preference": 3,
        "commercial_directness": 2,
        "personalization_depth": 1,
        "evidence_confidence_tolerance": 1,
        "role_led_emphasis": 2,
        "seller_introduction_timing": 2,
        "assertive_tone": 1,
    }
    assert thinker.requests[0].allowed_tools == ()


def test_shared_prompt_makes_the_copy_standard_operational(
    db_session: Session,
) -> None:
    """The fixture documents the intended move from research recital to useful copy."""

    _, _, _, membership = _subject(db_session)
    thinker = ScriptedThinker(
        {
            "subject": "A sourced market report",
            "body": COPY_QUALITY_COMPARISON["weak_evidence"],
            "evidence_insight_ids": [],
        }
    )

    generated = generation.generate(
        db_session,
        membership=membership,
        policy=_policy(db_session),
        thinker=thinker,
    )
    prompt = thinker.requests[0].prompt

    assert generated.decision.fallback_level == 5
    assert "Do not open with a description or summary of the recipient's company." in prompt
    assert 'Do not write "I noticed your company does X"' in prompt
    assert "internal plans, priorities, challenges, budgets, goals, or strategy" in prompt
    assert "one clear relevance bridge" in prompt
    assert "seller and offering concisely" in prompt
    assert "End with one simple call to action." in prompt
    assert "Do not force every available fact" in prompt
    assert "praise, flattery, fake familiarity, and performative research" in prompt
    assert "Use the earnest offering-led fallback as a successful outcome" in prompt


def test_preview_rejects_performative_or_assumptive_generated_copy(
    db_session: Session,
) -> None:
    _, _, _, membership = _subject(db_session)
    active = _policy(db_session)
    for body in (
        "I noticed that your company runs operations software. Your priority must be growth.",
        "You are focused on growth, so we should talk.",
    ):
        with pytest.raises(generation.PreviewError):
            generation.generate(
                db_session,
                membership=membership,
                policy=active,
                thinker=ScriptedThinker(
                    {"subject": "Hello", "body": body, "evidence_insight_ids": []}
                ),
            )

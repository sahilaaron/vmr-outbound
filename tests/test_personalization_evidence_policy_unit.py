from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from app.services.personalization import generation, policy


class _Session:
    def __init__(self, *objects: Any) -> None:
        self.objects = {type(item): item for item in objects}

    def get(self, model: type[Any], identifier: uuid.UUID) -> Any:
        item = self.objects.get(model)
        return item if item is not None and item.id == identifier else None


def _insight(claim: str) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), claim=claim)


def _observation(confidence: float) -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        confidence=confidence,
        freshness_at=now,
        retrieved_at=now,
        created_at=now,
    )


def _company_candidates(
    monkeypatch: pytest.MonkeyPatch,
    *,
    claim: str,
    confidences: tuple[float, ...] = (0.91,),
    eligible: bool = True,
) -> list[generation.ContextCandidate]:
    insight = _insight(claim)
    monkeypatch.setattr(
        generation.insight_service,
        "list_for_company",
        lambda _session, *, company_id: [insight],
    )
    monkeypatch.setattr(
        generation.insight_service,
        "is_personalization_eligible",
        lambda _session, *, insight: eligible,
    )
    monkeypatch.setattr(
        generation,
        "_evidence_for",
        lambda _session, _insight: tuple(_observation(value) for value in confidences),
    )
    return generation._company_candidates(
        object(),
        company=SimpleNamespace(id=uuid.uuid4()),
        seller_keywords={"operations", "pune", "workflow"},
        config=policy.default_policy(),
        now=datetime.now(UTC),
    )


def _decision(
    monkeypatch: pytest.MonkeyPatch,
    *,
    company_candidates: list[generation.ContextCandidate],
    depth: policy.Scale,
    role_accepted: bool,
) -> generation.ContextDecision:
    from app.models.campaign import Campaign, CampaignContact
    from app.models.company import Company
    from app.models.contact import Contact
    from app.models.personalization_policy import PersonalizationPolicyVersion

    company = Company(id=uuid.uuid4(), name="Kiln", domain="kiln.example")
    campaign = Campaign(id=uuid.uuid4(), name="Campaign")
    contact = Contact(
        id=uuid.uuid4(),
        company_id=company.id,
        company_name=company.name,
        company_domain=company.domain,
        natural_key=f"contact|{uuid.uuid4()}",
    )
    membership = CampaignContact(id=uuid.uuid4(), campaign_id=campaign.id, contact_id=contact.id)
    raw = policy.default_policy().to_dict()
    raw["temperament"]["personalization_depth"] = int(depth)
    version = PersonalizationPolicyVersion(
        id=uuid.uuid4(),
        version_number=1,
        schema_version=policy.POLICY_SCHEMA_VERSION,
        name="Test policy",
        configuration=raw,
        validation_summary={},
        created_by="test",
    )
    session = _Session(company, campaign, contact)
    monkeypatch.setattr(
        generation.seller_context,
        "assemble",
        lambda _session, *, campaign_id: SimpleNamespace(
            profile=None,
            offerings=(),
            global_restricted_claims=(),
        ),
    )
    monkeypatch.setattr(
        generation, "_company_candidates", lambda *args, **kwargs: company_candidates
    )
    monkeypatch.setattr(
        generation,
        "_role_candidate",
        lambda *args, **kwargs: generation.ContextCandidate(
            generation.ContextCategory.CONTACT,
            "Contact role",
            "Head of Operations",
            None,
            role_accepted,
            "Recorded role is relevant." if role_accepted else "No relevant role.",
        ),
    )
    monkeypatch.setattr(
        generation,
        "_sector_candidate",
        lambda *args, **kwargs: generation.ContextCandidate(
            generation.ContextCategory.SECTOR,
            "Sector context",
            "",
            None,
            False,
            "No sector context.",
        ),
    )
    monkeypatch.setattr(
        generation.intelligence_input,
        "assemble",
        lambda *args, **kwargs: SimpleNamespace(accepted=False, summary=lambda: {}),
    )
    return generation.decide_context(session, membership=membership, policy=version)


def _accepted_company(label: str, confidence: float) -> generation.ContextCandidate:
    return generation.ContextCandidate(
        generation.ContextCategory.COMPANY,
        label,
        f"{label} supports operations workflow relevance.",
        str(uuid.uuid4()),
        True,
        "Supported and relevant.",
        confidence,
    )


def test_substantive_labeled_company_context_is_not_rejected_as_performative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = _company_candidates(
        monkeypatch,
        claim="Headquarters: Pune operations hub serving two manufacturing regions.",
    )

    assert candidates[0].accepted is True


@pytest.mark.parametrize(
    "claim",
    (
        "Company name: Kiln Systems",
        "Legal name: Kiln Systems Private Limited",
        "Alternate name: Kiln",
        "Logo URL: https://kiln.example/logo.png",
    ),
)
def test_noninformational_company_metadata_remains_rejected(
    monkeypatch: pytest.MonkeyPatch,
    claim: str,
) -> None:
    candidates = _company_candidates(monkeypatch, claim=claim)

    assert candidates[0].accepted is False
    assert "performative" in candidates[0].reason


def test_incomplete_or_unsupported_insight_remains_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = _company_candidates(
        monkeypatch,
        claim="Opened a second plant for operations in Pune.",
        eligible=False,
    )

    assert candidates[0].accepted is False
    assert candidates[0].reason == (
        "The claim is unsupported, conflicting, unknown, or incompletely sourced."
    )


def test_weaker_secondary_citation_does_not_veto_strong_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = _company_candidates(
        monkeypatch,
        claim="Opened a second plant for operations in Pune.",
        confidences=(0.93, 0.55),
    )

    assert candidates[0].accepted is True
    assert candidates[0].confidence == pytest.approx(0.93)


def test_evidence_below_the_configured_floor_remains_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = _company_candidates(
        monkeypatch,
        claim="Opened a second plant for operations in Pune.",
        confidences=(0.79, 0.55),
    )

    assert candidates[0].accepted is False
    assert candidates[0].confidence == pytest.approx(0.79)
    assert "below policy threshold 0.80" in candidates[0].reason


def test_low_depth_selects_two_company_candidates_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = [
        _accepted_company("medium", 0.91),
        _accepted_company("highest", 0.95),
        _accepted_company("second", 0.93),
    ]

    decision = _decision(
        monkeypatch,
        company_candidates=candidates,
        depth=policy.Scale.LOW,
        role_accepted=False,
    )

    assert [item.label for item in decision.used] == ["highest", "second"]


def test_minimum_depth_can_select_the_combined_relevance_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = _decision(
        monkeypatch,
        company_candidates=[_accepted_company("company", 0.95)],
        depth=policy.Scale.MINIMUM,
        role_accepted=True,
    )

    assert decision.fallback_identifier == "contact_and_company"
    assert {item.category for item in decision.used} == {
        generation.ContextCategory.CONTACT,
        generation.ContextCategory.COMPANY,
    }


@pytest.mark.parametrize(
    ("depth", "expected_limit"),
    (
        (policy.Scale.MINIMUM, 1),
        (policy.Scale.LOW, 2),
        (policy.Scale.BALANCED, 2),
        (policy.Scale.HIGH, 3),
        (policy.Scale.MAXIMUM, 3),
    ),
)
def test_company_context_limit_is_explicit_and_bounded(
    depth: policy.Scale,
    expected_limit: int,
) -> None:
    raw = policy.default_policy().to_dict()
    raw["temperament"]["personalization_depth"] = int(depth)
    config = policy.PolicyConfig.from_dict(raw)

    assert policy.company_context_limit(config) == expected_limit


def test_equal_confidence_candidates_have_a_stable_evidence_id_tiebreaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = [_accepted_company(label, 0.95) for label in ("zeta", "alpha", "beta")]

    decision = _decision(
        monkeypatch,
        company_candidates=candidates,
        depth=policy.Scale.LOW,
        role_accepted=False,
    )

    expected = sorted(candidates, key=lambda item: item.evidence_id or "")[:2]
    assert [item.evidence_id for item in decision.used] == [item.evidence_id for item in expected]


def test_fallback_and_decision_provenance_remain_truthful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejected = generation.ContextCandidate(
        generation.ContextCategory.COMPANY,
        "Company insight rejected-id",
        "Generic praise with no offering relevance.",
        "rejected-id",
        False,
        "No explicit connection to the Campaign or seller offering was found.",
        0.95,
    )

    decision = _decision(
        monkeypatch,
        company_candidates=[rejected],
        depth=policy.Scale.LOW,
        role_accepted=False,
    )
    summary = decision.summary()

    assert decision.fallback_identifier == "offering_led"
    assert decision.used == ()
    assert "Company insight rejected-id" in summary["context_omitted"]
    assert {
        "context": "Company insight rejected-id",
        "reason": "No explicit connection to the Campaign or seller offering was found.",
    } in summary["omission_reasons"]
    assert "prompt" not in summary
    assert "rationale" not in summary

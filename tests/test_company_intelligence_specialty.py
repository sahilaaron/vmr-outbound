"""Specialty hardening tests (CI-002).

The question every case here asks: would a neutral analyst defend this as a
description of what the company does?

"Semiconductor failure analysis" survives that question. "World-class
customer-centric solutions" does not, and neither does "driving growth" — the
first is a compliment and the second is something the customer receives.

The hard cases are the interesting ones, and they are the reason the rules are
phrase-aware rather than token-aware: "next-generation sequencing" is a
laboratory technique, "next-generation solutions" is a brochure, and one word
separates them.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.models.enums import (
    IntelligenceDimension,
    IntelligenceEvidenceStatus,
    IntelligenceValueState,
)
from app.services.company_intelligence import producer as ci_producer
from app.services.company_intelligence import read as ci_read
from app.services.company_intelligence import specialty as sp
from sqlalchemy.orm import Session

from tests.test_company_intelligence import (
    assemble,
    make_company,
    make_dossier,
    make_fact,
    seeded,
)

# --- the rules, in isolation ------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "antibody-drug conjugate development",
        "semiconductor failure analysis",
        "cold-chain logistics",
        "industrial wastewater treatment",
        "clinical trial recruitment",
        "wafer-level reliability testing",
        "sterile fill-finish manufacturing",
        "private equity due diligence",
        "EV battery thermal management",
        "geospatial image analysis",
        "grid-scale battery integration",
    ],
)
def test_a_concrete_specialty_is_accepted_unchanged(value: str) -> None:
    verdict = sp.evaluate(value)
    assert verdict.action is sp.SpecialtyAction.ACCEPT
    assert verdict.cleaned_value is None


@pytest.mark.parametrize(
    "value",
    [
        "innovation",
        "customer-centric solutions",
        "world-class quality",
        "trusted partner",
        "end-to-end excellence",
        "market-leading expertise",
        "digital transformation leader",
        "technology",
        "consulting",
        "solutions",
        "services",
        "manufacturing",
    ],
)
def test_marketing_and_bare_fields_never_become_settled(value: str) -> None:
    verdict = sp.evaluate(value)
    assert verdict.action in (sp.SpecialtyAction.UNRESOLVED, sp.SpecialtyAction.REJECT)
    if verdict.action is sp.SpecialtyAction.UNRESOLVED:
        assert verdict.reason in (sp.REASON_TOO_BROAD, sp.REASON_PROMOTIONAL)


@pytest.mark.parametrize(
    "value",
    [
        "driving growth",
        "improving efficiency",
        "accelerating transformation",
        "unlocking value",
        "enhancing customer experience",
        "delivering value to shareholders",
        "reducing operational costs",
    ],
)
def test_outcome_language_is_rejected(value: str) -> None:
    verdict = sp.evaluate(value)
    assert verdict.action is sp.SpecialtyAction.REJECT
    assert verdict.reason == sp.REJECT_OUTCOME_CLAIM


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("leading cold-chain logistics provider", "cold chain logistics"),
        ("world-class semiconductor failure analysis", "semiconductor failure analysis"),
        ("innovative industrial wastewater treatment", "industrial wastewater treatment"),
        ("cold chain logistics solutions provider", "cold chain logistics"),
        ("premier clinical trial recruitment services", "clinical trial recruitment"),
        ("industrial wastewater treatment services", "industrial wastewater treatment"),
    ],
)
def test_a_removable_modifier_is_removed_and_nothing_else_changes(
    value: str, expected: str
) -> None:
    verdict = sp.evaluate(value)
    assert verdict.action is sp.SpecialtyAction.CLEAN
    assert verdict.cleaned_value == expected
    # Cleaning only ever strips from the edges: every surviving word was already
    # in the original, in the same order.
    assert all(word in value.lower().replace("-", " ") for word in expected.split())


@pytest.mark.parametrize(
    "value",
    [
        "next-generation sequencing",
        "advanced driver assistance systems",
        "global navigation satellite systems",
        "sustainable aviation fuel",
        "advanced materials",
        "green hydrogen",
    ],
)
def test_a_modifier_whose_removal_would_change_the_meaning_is_protected(
    value: str,
) -> None:
    verdict = sp.evaluate(value)
    assert verdict.action is sp.SpecialtyAction.ACCEPT, (
        f"{value!r} is technical vocabulary, not a boast"
    )


def test_a_promotional_word_inside_a_phrase_is_not_surgically_removed() -> None:
    """Stripping from the middle would invent a phrase nobody wrote."""

    verdict = sp.evaluate("cold chain innovative packaging")
    assert verdict.action is sp.SpecialtyAction.UNRESOLVED
    assert verdict.reason == sp.REASON_PROMOTIONAL


def test_purely_promotional_wording_is_rejected_rather_than_kept() -> None:
    verdict = sp.evaluate("world-class trusted partner")
    assert verdict.action is sp.SpecialtyAction.REJECT
    assert verdict.reason == sp.REJECT_MARKETING_ONLY


def test_an_empty_or_absurdly_long_value_is_handled() -> None:
    assert sp.evaluate("").action is sp.SpecialtyAction.REJECT
    assert sp.evaluate("   ").action is sp.SpecialtyAction.REJECT
    long_value = " ".join(["wastewater"] * 40)
    assert sp.evaluate(long_value).action is sp.SpecialtyAction.UNRESOLVED


def test_near_duplicates_share_a_key_and_unrelated_values_do_not() -> None:
    assert sp.duplicate_key("battery pack assemblies") == sp.duplicate_key("battery pack assembly")
    assert sp.duplicate_key("thermal analysis") == sp.duplicate_key("thermal analyses")
    assert sp.duplicate_key("thermal analysis") != sp.duplicate_key("thermal simulation")
    # A sibilant ending is not a plural marker.
    assert sp.duplicate_key("failure analysis") == "failure analysis"
    assert sp.duplicate_key("power electronics") == "power electronics"
    assert sp.duplicate_key("process diagnostics") == "process diagnostics"


def test_the_hygiene_rules_are_versioned() -> None:
    assert sp.SPECIALTY_HYGIENE_VERSION


# --- through the producer ---------------------------------------------------


def produce(session: Session, answer: dict[str, Any], *, name: str) -> Any:
    seeded(session)
    company = make_company(session, name=name)
    make_dossier(session, company=company)
    make_fact(
        session,
        company=company,
        claim="overview: performs semiconductor failure analysis for fabless customers",
        key=f"sp:{name}:1",
    )
    make_fact(
        session,
        company=company,
        claim="products: supplies thermal test sockets",
        key=f"sp:{name}:2",
    )
    ci_producer.produce(
        session, company=company, source=assemble(session, company), answer=answer, raw_answer="{}"
    )
    return company


def specialties(session: Session, company: Any) -> dict[str, ci_read.ClassificationView]:
    view = ci_read.get_company_intelligence(session, company_id=company.id)
    assert view is not None
    return {row.display_value: row for row in view.for_dimension(IntelligenceDimension.SPECIALTY)}


def test_a_cleaned_specialty_is_settled_and_keeps_the_original_wording(
    db_session: Session,
) -> None:
    company = produce(
        db_session,
        {
            "classifications": [
                {
                    "dimension": "specialty",
                    "value": "leading semiconductor failure analysis provider",
                    "evidence": ["F1"],
                    "confidence": 0.8,
                }
            ]
        },
        name="Clean Co",
    )
    rows = specialties(db_session, company)
    row = rows["semiconductor failure analysis"]
    assert row.state is IntelligenceValueState.RESOLVED
    assert row.cleaned_value == "semiconductor failure analysis"
    assert row.model_value == "leading semiconductor failure analysis provider"


def test_a_broad_specialty_is_kept_unresolved_not_dropped(db_session: Session) -> None:
    company = produce(
        db_session,
        {
            "classifications": [
                {"dimension": "specialty", "value": "technology", "evidence": ["F1"]}
            ]
        },
        name="Broad Co",
    )
    rows = specialties(db_session, company)
    assert "technology" in rows
    row = rows["technology"]
    assert row.state is IntelligenceValueState.UNRESOLVED
    assert row.unresolved_reason == sp.REASON_TOO_BROAD
    # Evidence-backed but not settled: those are different facts.
    assert row.evidence_status is IntelligenceEvidenceStatus.SUPPORTED


def test_a_marketing_only_specialty_is_rejected_and_reported(db_session: Session) -> None:
    company = produce(
        db_session,
        {
            "classifications": [
                {"dimension": "specialty", "value": "unlocking value", "evidence": ["F1"]},
                {
                    "dimension": "specialty",
                    "value": "semiconductor failure analysis",
                    "evidence": ["F1"],
                },
            ]
        },
        name="Marketing Co",
    )
    rows = specialties(db_session, company)
    assert set(rows) == {"semiconductor failure analysis"}


def test_an_exact_duplicate_specialty_is_stored_once(db_session: Session) -> None:
    company = produce(
        db_session,
        {
            "classifications": [
                {
                    "dimension": "specialty",
                    "value": "semiconductor failure analysis",
                    "evidence": ["F1"],
                },
                {
                    "dimension": "specialty",
                    "value": "Semiconductor Failure Analysis",
                    "evidence": ["F1"],
                },
            ]
        },
        name="Dup Co",
    )
    assert len(specialties(db_session, company)) == 1


def test_a_near_duplicate_specialty_is_stored_once(db_session: Session) -> None:
    company = produce(
        db_session,
        {
            "classifications": [
                {
                    "dimension": "specialty",
                    "value": "thermal test socket design",
                    "evidence": ["F2"],
                },
                {
                    "dimension": "specialty",
                    "value": "thermal test socket designs",
                    "evidence": ["F2"],
                },
            ]
        },
        name="NearDup Co",
    )
    assert len(specialties(db_session, company)) == 1


def test_a_specialty_that_merely_repeats_a_product_is_kept_but_unsettled(
    db_session: Session,
) -> None:
    company = produce(
        db_session,
        {
            "classifications": [
                {"dimension": "product", "value": "thermal test sockets", "evidence": ["F2"]},
                {"dimension": "specialty", "value": "thermal test socket", "evidence": ["F2"]},
            ]
        },
        name="Overlap Co",
    )
    rows = specialties(db_session, company)
    row = rows["thermal test socket"]
    assert row.state is IntelligenceValueState.UNRESOLVED
    assert row.unresolved_reason == sp.REASON_DIMENSION_OVERLAP


def test_the_four_neighbouring_dimensions_stay_separate(db_session: Session) -> None:
    company = produce(
        db_session,
        {
            "classifications": [
                {"dimension": "product", "value": "thermal test sockets", "evidence": ["F2"]},
                {
                    "dimension": "service",
                    "value": "failure analysis reporting",
                    "evidence": ["F1"],
                },
                {
                    "dimension": "capability",
                    "value": "focused ion beam milling",
                    "evidence": ["F1"],
                },
                {
                    "dimension": "specialty",
                    "value": "semiconductor failure analysis",
                    "evidence": ["F1"],
                },
            ]
        },
        name="Four Co",
    )
    view = ci_read.get_company_intelligence(db_session, company_id=company.id)
    assert view is not None
    for dimension in (
        IntelligenceDimension.PRODUCT,
        IntelligenceDimension.SERVICE,
        IntelligenceDimension.CAPABILITY,
        IntelligenceDimension.SPECIALTY,
    ):
        assert len(view.for_dimension(dimension)) == 1


def test_a_specialty_with_an_invalid_evidence_handle_is_unsupported_not_settled(
    db_session: Session,
) -> None:
    company = produce(
        db_session,
        {
            "classifications": [
                {
                    "dimension": "specialty",
                    "value": "wafer-level reliability testing",
                    "evidence": ["F44"],
                }
            ]
        },
        name="BadCite Co",
    )
    row = specialties(db_session, company)["wafer-level reliability testing"]
    assert row.state is IntelligenceValueState.UNRESOLVED
    assert row.unresolved_reason == ci_producer.REASON_NO_EVIDENCE
    assert row.evidence_status is IntelligenceEvidenceStatus.INSUFFICIENT


def test_specialty_ordering_and_caps_are_deterministic(db_session: Session) -> None:
    values = [
        f"semiconductor {word} analysis"
        for word in (
            "failure",
            "defect",
            "thermal",
            "acoustic",
            "optical",
            "electrical",
            "magnetic",
            "chemical",
            "structural",
            "surface",
        )
    ]
    answer = {
        "classifications": [
            {"dimension": "specialty", "value": value, "evidence": ["F1"]} for value in values
        ]
    }
    first = produce(db_session, answer, name="Cap A")
    second = produce(db_session, answer, name="Cap B")
    cap = ci_producer.DIMENSION_CAPS[IntelligenceDimension.SPECIALTY]
    left = list(specialties(db_session, first))
    right = list(specialties(db_session, second))
    assert len(left) == cap
    assert left == right, "the same answer must order and cap identically every time"


def test_the_prompt_states_the_dimension_boundary_and_the_negative_examples(
    db_session: Session,
) -> None:
    from app.services.company_intelligence import prompts

    seeded(db_session)
    company = make_company(db_session, name="Prompt Spec")
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="overview: builds controllers", key="ps:1")
    text = prompts.classification_prompt(
        assemble(db_session, company),
        vocabularies=ci_producer.vocabulary_for_prompt(db_session),
    )
    assert "FOUR DIMENSIONS THAT LOOK ALIKE" in text
    assert "WHAT COUNTS AS A SPECIALTY" in text
    assert "customer-centric solutions" in text
    assert "semiconductor failure analysis" in text

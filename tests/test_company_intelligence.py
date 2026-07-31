"""Company Intelligence domain tests (CI-001).

Organised around the guarantees rather than around the modules, because the
guarantees are what a reader needs to be able to check:

* a vocabulary is a versioned edition, and replacing it does not rewrite history;
* normalization is exact and never guesses;
* production is idempotent for one exact input and versions on any change to it;
* a classification without evidence is stored as unsupported, never as a fact;
* conflicting evidence stays conflicting;
* a malformed answer stores nothing;
* an operator decision is a record, not an edit, and survives a new version;
* the effective value says who is responsible for it;
* Research, canonical Company fields and outreach eligibility are untouched.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from app.models.company import Company
from app.models.company_dossier import CompanyDossierVersion, CompanyResearchSubmission
from app.models.company_intelligence import (
    CompanyIntelligenceClassification,
    CompanyIntelligenceConflict,
    CompanyIntelligenceDecision,
    CompanyIntelligenceEvidenceLink,
    CompanyIntelligenceVersion,
)
from app.models.enums import (
    InsightKind,
    InsightState,
    IntelligenceDecisionAction,
    IntelligenceDimension,
    IntelligenceEvidenceStatus,
    IntelligenceNormalization,
    IntelligenceValueSource,
    IntelligenceValueState,
    TaxonomyAliasSource,
)
from app.models.insight import Insight
from app.models.intelligence_taxonomy import (
    IntelligenceTaxonomy,
    IntelligenceTaxonomyAlias,
    IntelligenceTaxonomyTerm,
)
from app.services.companies import dossiers as company_dossiers
from app.services.company_intelligence import inputs as ci_inputs
from app.services.company_intelligence import producer as ci_producer
from app.services.company_intelligence import read as ci_read
from app.services.company_intelligence import review as ci_review
from app.services.company_intelligence import taxonomy as ci_taxonomy
from app.services.company_intelligence.normalization import normalize_term
from app.services.company_intelligence.seed import seed_vocabularies
from app.services.insights.evidence import EvidenceInput, create_insight
from sqlalchemy import select
from sqlalchemy.orm import Session

PRODUCER = "test-producer"
PRODUCER_VERSION = "1"
POLICY = ci_producer.POLICY_VERSION


# --- helpers ----------------------------------------------------------------


def make_company(session: Session, *, name: str = "Kiln Systems") -> Company:
    company = Company(name=name, domain=f"{normalize_term(name).replace(' ', '')}.example")
    session.add(company)
    session.flush()
    return company


def make_dossier(
    session: Session,
    *,
    company: Company,
    payload: dict[str, Any] | None = None,
    sections: dict[str, Any] | None = None,
) -> CompanyDossierVersion:
    submission, _ = company_dossiers.submit(
        session,
        company=company,
        producer="test-research",
        payload=payload or {"seed": str(uuid.uuid4())},
    )
    return company_dossiers.interpret(
        session,
        company=company,
        submission=submission,
        interpreter="test-research",
        sections=sections
        or {
            "overview": {"summary": "Builds industrial kiln controllers."},
            "sources": [{"url": "https://kiln.example/about", "title": "About"}],
        },
    )


def make_fact(
    session: Session,
    *,
    company: Company,
    claim: str,
    url: str = "https://kiln.example/about",
    key: str | None = None,
) -> Insight:
    return create_insight(
        session,
        claim=claim,
        kind=InsightKind.FACT,
        state=InsightState.SUPPORTED,
        evidence=[
            EvidenceInput(
                source_url=url,
                retrieved_at=datetime.now(UTC),
                evidence_summary=claim,
                confidence=0.9,
                extraction_method="test",
            )
        ],
        company_id=company.id,
        idempotency_key=key or f"test:{uuid.uuid4()}",
        actor="test",
    )


def seeded(session: Session) -> None:
    seed_vocabularies(session)


def assemble(session: Session, company: Company) -> ci_inputs.IntelligenceInput:
    return ci_inputs.assemble(
        session,
        company=company,
        producer=PRODUCER,
        producer_version=PRODUCER_VERSION,
        policy_version=POLICY,
    )


def answer(
    *,
    classifications: list[dict[str, Any]] | None = None,
    conflicts: list[dict[str, Any]] | None = None,
    unknown: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"classifications": classifications or []}
    if conflicts is not None:
        payload["conflicts"] = conflicts
    if unknown is not None:
        payload["unknown_dimensions"] = unknown
    return payload


# --- normalization ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Pharma & Healthcare", "pharma and healthcare"),
        ("  PHARMA and healthcare ", "pharma and healthcare"),
        ("Pharma/Healthcare", "pharma healthcare"),
        ("Café Systems", "cafe systems"),
        ("R&D", "r and d"),
        ("", ""),
        ("---", ""),
    ],
)
def test_normalization_is_exact_and_boring(raw: str, expected: str) -> None:
    assert normalize_term(raw) == expected


def test_normalization_does_not_stem_or_pluralise() -> None:
    # "Coating" and "Coatings" stay different strings on purpose: making them
    # equal is an alias decision somebody signs, not a rule in a matcher.
    assert normalize_term("Coating") != normalize_term("Coatings")


# --- taxonomy ---------------------------------------------------------------


def test_seed_publishes_the_supplied_taxonomy_verbatim(db_session: Session) -> None:
    report = seed_vocabularies(db_session)
    assert "industry" in report.created

    edition = ci_taxonomy.active_taxonomy(db_session, dimension=IntelligenceDimension.INDUSTRY)
    assert edition is not None
    categories = ci_taxonomy.list_terms(db_session, taxonomy=edition, depth=0)
    assert len(categories) == 16
    assert "Pharma & Healthcare" in {term.canonical_label for term in categories}

    children = ci_taxonomy.list_terms(db_session, taxonomy=edition, depth=1)
    assert len(children) == 245


def test_seed_is_idempotent_and_never_edits_a_published_edition(db_session: Session) -> None:
    first = seed_vocabularies(db_session)
    before = db_session.scalar(
        select(IntelligenceTaxonomyTerm.id).where(IntelligenceTaxonomyTerm.code == "manufacturing")
    )
    second = seed_vocabularies(db_session)
    after = db_session.scalar(
        select(IntelligenceTaxonomyTerm.id).where(IntelligenceTaxonomyTerm.code == "manufacturing")
    )
    assert first.created and not second.created
    assert second.skipped
    assert before == after


def test_repeated_others_entries_are_disambiguated_not_collapsed(db_session: Session) -> None:
    seeded(db_session)
    edition = ci_taxonomy.active_taxonomy(db_session, dimension=IntelligenceDimension.INDUSTRY)
    assert edition is not None
    others = [
        term
        for term in ci_taxonomy.list_terms(db_session, taxonomy=edition, depth=1)
        if term.code.endswith("-others")
    ]
    assert len(others) == 16
    assert len({term.normalized_label for term in others}) == 16

    # And the bare, genuinely ambiguous word resolves to nothing rather than to
    # whichever row the database happened to return first.
    resolution = ci_taxonomy.resolve(
        db_session, dimension=IntelligenceDimension.SUBINDUSTRY, value="Others"
    )
    assert resolution.normalization is IntelligenceNormalization.UNMAPPED


def test_resolution_matches_canonical_then_approved_alias_only(db_session: Session) -> None:
    seeded(db_session)
    canonical = ci_taxonomy.resolve(
        db_session, dimension=IntelligenceDimension.INDUSTRY, value="Pharma & Healthcare"
    )
    assert canonical.normalization is IntelligenceNormalization.CANONICAL

    alias = ci_taxonomy.resolve(
        db_session, dimension=IntelligenceDimension.INDUSTRY, value="pharma"
    )
    assert alias.normalization is IntelligenceNormalization.ALIAS
    assert alias.term is not None and alias.term.code == "pharma-and-healthcare"

    nothing = ci_taxonomy.resolve(
        db_session, dimension=IntelligenceDimension.INDUSTRY, value="Pharmaceutical-ish"
    )
    assert nothing.normalization is IntelligenceNormalization.UNMAPPED
    assert nothing.term is None


def test_a_model_suggested_alias_does_not_resolve_until_approved(db_session: Session) -> None:
    seeded(db_session)
    edition = ci_taxonomy.active_taxonomy(db_session, dimension=IntelligenceDimension.INDUSTRY)
    assert edition is not None
    term = db_session.scalars(
        select(IntelligenceTaxonomyTerm).where(IntelligenceTaxonomyTerm.code == "manufacturing")
    ).one()

    ci_taxonomy.add_alias(
        db_session,
        term=term,
        alias="widget shops",
        source=TaxonomyAliasSource.MODEL_SUGGESTION,
    )
    unapproved = ci_taxonomy.resolve(
        db_session, dimension=IntelligenceDimension.INDUSTRY, value="widget shops"
    )
    assert unapproved.normalization is IntelligenceNormalization.UNMAPPED

    ci_taxonomy.add_alias(
        db_session,
        term=term,
        alias="widget shops",
        source=TaxonomyAliasSource.OPERATOR,
        created_by="operator",
    )
    approved = ci_taxonomy.resolve(
        db_session, dimension=IntelligenceDimension.INDUSTRY, value="widget shops"
    )
    assert approved.normalization is IntelligenceNormalization.ALIAS


def test_one_alias_cannot_mean_two_terms(db_session: Session) -> None:
    seeded(db_session)
    terms = {
        term.code: term
        for term in db_session.scalars(
            select(IntelligenceTaxonomyTerm).where(
                IntelligenceTaxonomyTerm.code.in_(["manufacturing", "retail"])
            )
        ).all()
    }
    ci_taxonomy.add_alias(db_session, term=terms["manufacturing"], alias="widgets")
    with pytest.raises(ci_taxonomy.TaxonomyError):
        ci_taxonomy.add_alias(db_session, term=terms["retail"], alias="widgets")


def test_activating_a_new_edition_retires_the_old_one_without_deleting_it(
    db_session: Session,
) -> None:
    seeded(db_session)
    old = ci_taxonomy.active_taxonomy(db_session, dimension=IntelligenceDimension.BUSINESS_MODEL)
    assert old is not None

    new = ci_taxonomy.create_taxonomy(
        db_session,
        dimension=IntelligenceDimension.BUSINESS_MODEL,
        version="2027.01",
        title="Business model (second edition)",
    )
    ci_taxonomy.add_term(db_session, taxonomy=new, code="b2b", canonical_label="B2B")
    ci_taxonomy.activate_taxonomy(db_session, taxonomy=new)

    db_session.refresh(old)
    assert old.is_active is False
    assert old.retired_at is not None
    assert db_session.get(IntelligenceTaxonomy, old.id) is not None, (
        "a retired edition must remain readable"
    )
    assert ci_taxonomy.active_versions(db_session)["business_model"] == "2027.01"


def test_dimensions_without_a_vocabulary_report_not_applicable(db_session: Session) -> None:
    seeded(db_session)
    resolution = ci_taxonomy.resolve(
        db_session, dimension=IntelligenceDimension.PRODUCT, value="stainless ball valves"
    )
    assert resolution.normalization is IntelligenceNormalization.NOT_APPLICABLE


# --- input assembly ---------------------------------------------------------


def test_a_company_without_a_dossier_cannot_be_classified(db_session: Session) -> None:
    company = make_company(db_session)
    with pytest.raises(ci_inputs.IntelligenceInputError) as excinfo:
        assemble(db_session, company)
    assert excinfo.value.reason_code == ci_inputs.REASON_NO_DOSSIER


def test_a_dossier_with_no_evidence_at_all_cannot_be_classified(db_session: Session) -> None:
    company = make_company(db_session)
    submission, _ = company_dossiers.submit(
        db_session, company=company, producer="test-research", payload={"empty": True}
    )
    company_dossiers.interpret(
        db_session,
        company=company,
        submission=submission,
        interpreter="test-research",
        sections={},
    )
    with pytest.raises(ci_inputs.IntelligenceInputError) as excinfo:
        assemble(db_session, company)
    assert excinfo.value.reason_code == ci_inputs.REASON_NO_EVIDENCE


def test_only_supported_claims_are_offered_to_the_producer(db_session: Session) -> None:
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")
    create_insight(
        db_session,
        claim="we could not establish the headcount",
        kind=InsightKind.INTERPRETATION,
        state=InsightState.UNKNOWN,
        evidence=[],
        company_id=company.id,
        idempotency_key="test:unknown",
        actor="test",
    )
    source = assemble(db_session, company)
    assert len(source.facts) == 1
    assert source.facts[0].ref == "F1"


def test_digest_is_stable_and_changes_with_every_input(db_session: Session) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")

    first = assemble(db_session, company).digest
    assert first == assemble(db_session, company).digest, "same inputs must hash the same"

    make_fact(db_session, company=company, claim="products: kiln controllers")
    assert assemble(db_session, company).digest != first, "a new fact must change the digest"

    with_new_producer = ci_inputs.assemble(
        db_session,
        company=company,
        producer=PRODUCER,
        producer_version="2",
        policy_version=POLICY,
    )
    assert with_new_producer.digest != assemble(db_session, company).digest


def test_a_new_dossier_version_changes_the_digest(db_session: Session) -> None:
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")
    before = assemble(db_session, company).digest

    make_dossier(db_session, company=company, payload={"round": 2})
    assert assemble(db_session, company).digest != before


# --- production -------------------------------------------------------------


def _produce(
    session: Session,
    company: Company,
    payload: dict[str, Any],
    *,
    raw: str = '{"ok":true}',
) -> ci_producer.ProductionResult:
    return ci_producer.produce(
        session,
        company=company,
        source=assemble(session, company),
        answer=payload,
        raw_answer=raw,
    )


def test_a_supported_classification_is_stored_resolved_and_normalized(
    db_session: Session,
) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")

    result = _produce(
        db_session,
        company,
        answer(
            classifications=[
                {
                    "dimension": "industry",
                    "value": "Manufacturing",
                    "is_primary": True,
                    "evidence": ["F1"],
                    "confidence": 0.9,
                    "rationale": "the about page says so",
                }
            ]
        ),
    )
    assert result.created is True

    row = db_session.scalars(
        select(CompanyIntelligenceClassification).where(
            CompanyIntelligenceClassification.intelligence_version_id == result.version.id
        )
    ).one()
    assert row.state is IntelligenceValueState.RESOLVED
    assert row.evidence_status is IntelligenceEvidenceStatus.SUPPORTED
    assert row.normalization is IntelligenceNormalization.CANONICAL
    assert row.term_code == "manufacturing"
    assert row.model_value == "Manufacturing"
    assert row.is_primary is True
    assert row.confidence_band is not None and row.confidence_band.value == "high"

    links = db_session.scalars(
        select(CompanyIntelligenceEvidenceLink).where(
            CompanyIntelligenceEvidenceLink.classification_id == row.id
        )
    ).all()
    assert len(links) == 1
    assert links[0].insight_id is not None


def test_an_uncited_value_is_kept_but_marked_unsupported(db_session: Session) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")

    result = _produce(
        db_session,
        company,
        answer(
            classifications=[
                {"dimension": "industry", "value": "Manufacturing", "evidence": []},
            ]
        ),
    )
    row = db_session.scalars(
        select(CompanyIntelligenceClassification).where(
            CompanyIntelligenceClassification.intelligence_version_id == result.version.id
        )
    ).one()
    assert row.state is IntelligenceValueState.UNRESOLVED
    assert row.evidence_status is IntelligenceEvidenceStatus.INSUFFICIENT
    assert row.unresolved_reason == ci_producer.REASON_NO_EVIDENCE
    # Kept, not dropped: review cannot judge what it cannot see.
    assert row.model_value == "Manufacturing"


def test_a_citation_that_was_never_shown_is_refused(db_session: Session) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")

    result = _produce(
        db_session,
        company,
        answer(
            classifications=[
                {
                    "dimension": "industry",
                    "value": "Manufacturing",
                    "evidence": ["F9", "https://elsewhere.example/page"],
                }
            ]
        ),
    )
    row = db_session.scalars(
        select(CompanyIntelligenceClassification).where(
            CompanyIntelligenceClassification.intelligence_version_id == result.version.id
        )
    ).one()
    assert row.evidence_count == 0
    assert row.state is IntelligenceValueState.UNRESOLVED
    assert any("F9" in warning for warning in result.warnings)


def test_an_unmapped_value_keeps_its_wording_and_stays_unresolved(db_session: Session) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: kiln automation")

    result = _produce(
        db_session,
        company,
        answer(
            classifications=[
                {
                    "dimension": "industry",
                    "value": "Kiln automation",
                    "evidence": ["F1"],
                    "confidence": 0.5,
                }
            ]
        ),
    )
    row = db_session.scalars(
        select(CompanyIntelligenceClassification).where(
            CompanyIntelligenceClassification.intelligence_version_id == result.version.id
        )
    ).one()
    assert row.normalization is IntelligenceNormalization.UNMAPPED
    assert row.state is IntelligenceValueState.UNRESOLVED
    assert row.unresolved_reason == ci_producer.REASON_UNMAPPED
    # Evidence still counts: the value is supported, it just has no canonical form.
    assert row.evidence_status is IntelligenceEvidenceStatus.SUPPORTED
    assert row.model_value == "Kiln automation"


def test_conflicting_evidence_is_never_flattened(db_session: Session) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")
    make_fact(
        db_session,
        company=company,
        claim="industries served: chemical and material",
        url="https://kiln.example/markets",
    )

    result = _produce(
        db_session,
        company,
        answer(
            classifications=[
                {"dimension": "industry", "value": "Manufacturing", "evidence": ["F1"]},
                {"dimension": "industry", "value": "Chemical & Material", "evidence": ["F2"]},
            ],
            conflicts=[
                {
                    "dimension": "industry",
                    "values": ["Manufacturing", "Chemical & Material"],
                    "statement": "the about page and markets page name different industries",
                    "evidence": ["F1", "F2"],
                }
            ],
        ),
    )
    rows = db_session.scalars(
        select(CompanyIntelligenceClassification).where(
            CompanyIntelligenceClassification.intelligence_version_id == result.version.id
        )
    ).all()
    assert {row.state for row in rows} == {IntelligenceValueState.CONFLICTED}
    assert {row.conflict_group for row in rows} == {0}

    conflict = db_session.scalars(
        select(CompanyIntelligenceConflict).where(
            CompanyIntelligenceConflict.intelligence_version_id == result.version.id
        )
    ).one()
    assert conflict.member_count == 2
    assert result.version.conflict_count == 1


def test_a_one_sided_conflict_is_dropped(db_session: Session) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")

    result = _produce(
        db_session,
        company,
        answer(
            classifications=[
                {"dimension": "industry", "value": "Manufacturing", "evidence": ["F1"]}
            ],
            conflicts=[
                {
                    "dimension": "industry",
                    "values": ["Manufacturing"],
                    "statement": "not really a conflict",
                }
            ],
        ),
    )
    assert result.version.conflict_count == 0
    assert any("fewer than two" in warning for warning in result.warnings)


def test_an_unknown_dimension_is_stored_as_a_looked_and_found_nothing_row(
    db_session: Session,
) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")

    result = _produce(
        db_session,
        company,
        answer(
            classifications=[
                {"dimension": "industry", "value": "Manufacturing", "evidence": ["F1"]}
            ],
            unknown=["business_model"],
        ),
    )
    rows = {
        row.dimension: row
        for row in db_session.scalars(
            select(CompanyIntelligenceClassification).where(
                CompanyIntelligenceClassification.intelligence_version_id == result.version.id
            )
        ).all()
    }
    unknown_row = rows[IntelligenceDimension.BUSINESS_MODEL]
    assert unknown_row.state is IntelligenceValueState.UNKNOWN
    assert unknown_row.unresolved_reason == ci_producer.REASON_SILENT
    # And a dimension nobody addressed has no row at all.
    assert IntelligenceDimension.GEOGRAPHY not in rows


def test_unknown_dimension_names_and_junk_entries_are_dropped_with_a_warning(
    db_session: Session,
) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")

    result = _produce(
        db_session,
        company,
        answer(
            classifications=[
                {"dimension": "vibe", "value": "Excellent", "evidence": ["F1"]},
                {"dimension": "industry", "value": "", "evidence": ["F1"]},
                "not an object",
                {"dimension": "industry", "value": "Manufacturing", "evidence": ["F1"]},
            ]
        ),
    )
    assert result.classifications == 1
    assert len(result.warnings) >= 3


def test_a_malformed_answer_stores_nothing(db_session: Session) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")
    source = assemble(db_session, company)

    with pytest.raises(ci_producer.IntelligenceMalformed):
        ci_producer.produce(
            db_session,
            company=company,
            source=source,
            answer={"classifications": "Manufacturing"},
        )
    assert (
        db_session.scalars(
            select(CompanyIntelligenceVersion).where(
                CompanyIntelligenceVersion.company_id == company.id
            )
        ).first()
        is None
    )


def test_a_malformed_answer_is_retryable_and_a_bad_input_is_not(db_session: Session) -> None:
    assert ci_producer.IntelligenceMalformed("x").retryable is True
    assert ci_producer.IntelligenceProducerError("x").retryable is False


def test_production_is_idempotent_for_one_exact_input(db_session: Session) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")
    payload = answer(
        classifications=[{"dimension": "industry", "value": "Manufacturing", "evidence": ["F1"]}]
    )

    first = _produce(db_session, company, payload)
    second = _produce(db_session, company, payload)

    assert first.created is True
    assert second.created is False and second.reused is True
    assert first.version.id == second.version.id
    assert (
        len(
            db_session.scalars(
                select(CompanyIntelligenceVersion).where(
                    CompanyIntelligenceVersion.company_id == company.id
                )
            ).all()
        )
        == 1
    )


def test_new_evidence_produces_a_new_version_and_supersedes_the_old_one(
    db_session: Session,
) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")
    first = _produce(
        db_session,
        company,
        answer(
            classifications=[
                {"dimension": "industry", "value": "Manufacturing", "evidence": ["F1"]}
            ]
        ),
    )

    make_fact(db_session, company=company, claim="products: kiln controllers")
    second = _produce(
        db_session,
        company,
        answer(
            classifications=[
                {"dimension": "industry", "value": "Manufacturing", "evidence": ["F1"]},
                {"dimension": "product", "value": "Kiln controllers", "evidence": ["F2"]},
            ]
        ),
    )

    db_session.refresh(first.version)
    assert second.version.version_number == 2
    assert second.version.is_current is True
    assert first.version.is_current is False
    assert first.version.superseded_at is not None
    # The superseded version's rows are untouched.
    assert (
        len(
            db_session.scalars(
                select(CompanyIntelligenceClassification).where(
                    CompanyIntelligenceClassification.intelligence_version_id == first.version.id
                )
            ).all()
        )
        == 1
    )


def test_a_producer_version_change_produces_a_new_version(db_session: Session) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")
    payload = answer(
        classifications=[{"dimension": "industry", "value": "Manufacturing", "evidence": ["F1"]}]
    )
    _produce(db_session, company, payload)

    newer = ci_inputs.assemble(
        db_session,
        company=company,
        producer=PRODUCER,
        producer_version="2",
        policy_version=POLICY,
    )
    result = ci_producer.produce(
        db_session, company=company, source=newer, answer=payload, raw_answer="{}"
    )
    assert result.created is True
    assert result.version.version_number == 2
    assert result.version.producer_version == "2"


def test_dimension_caps_drop_extra_values_with_a_warning(db_session: Session) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: many")

    values = [f"Model {index}" for index in range(6)]
    result = _produce(
        db_session,
        company,
        answer(
            classifications=[
                {"dimension": "business_model", "value": value, "evidence": ["F1"]}
                for value in values
            ]
        ),
    )
    cap = ci_producer.DIMENSION_CAPS[IntelligenceDimension.BUSINESS_MODEL]
    assert result.classifications == cap
    assert any("cap" in warning for warning in result.warnings)


def test_the_raw_answer_is_hashed_never_stored(db_session: Session) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")

    secret = '{"classifications": [], "note": "SUPER-SECRET-PROMPT-CONTENT"}'
    result = _produce(
        db_session,
        company,
        answer(
            classifications=[
                {"dimension": "industry", "value": "Manufacturing", "evidence": ["F1"]}
            ]
        ),
        raw=secret,
    )
    stored = repr(
        {
            column.name: getattr(result.version, column.name)
            for column in CompanyIntelligenceVersion.__table__.columns
        }
    )
    assert "SUPER-SECRET-PROMPT-CONTENT" not in stored
    assert result.version.answer_digest is not None and len(result.version.answer_digest) == 64


# --- boundaries -------------------------------------------------------------


def test_production_writes_no_canonical_company_field(db_session: Session) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")
    before = {
        "industry": company.industry,
        "country": company.country,
        "company_size": company.company_size,
        "name": company.name,
        "domain": company.domain,
    }

    _produce(
        db_session,
        company,
        answer(
            classifications=[
                {
                    "dimension": "industry",
                    "value": "Manufacturing",
                    "evidence": ["F1"],
                    "is_primary": True,
                }
            ]
        ),
    )
    db_session.refresh(company)
    assert {
        "industry": company.industry,
        "country": company.country,
        "company_size": company.company_size,
        "name": company.name,
        "domain": company.domain,
    } == before


def test_production_does_not_touch_research(db_session: Session) -> None:
    seeded(db_session)
    company = make_company(db_session)
    dossier = make_dossier(db_session, company=company)
    fact = make_fact(db_session, company=company, claim="industries served: manufacturing")
    dossier_before = (dossier.id, dossier.version_number, dossier.is_current, dossier.warnings)
    fact_before = (fact.id, fact.claim, fact.state)
    submissions_before = len(
        db_session.scalars(
            select(CompanyResearchSubmission).where(
                CompanyResearchSubmission.company_id == company.id
            )
        ).all()
    )

    _produce(
        db_session,
        company,
        answer(
            classifications=[
                {"dimension": "industry", "value": "Manufacturing", "evidence": ["F1"]}
            ]
        ),
    )
    db_session.refresh(dossier)
    db_session.refresh(fact)
    assert (dossier.id, dossier.version_number, dossier.is_current, dossier.warnings) == (
        dossier_before
    )
    assert (fact.id, fact.claim, fact.state) == fact_before
    assert (
        len(
            db_session.scalars(
                select(CompanyResearchSubmission).where(
                    CompanyResearchSubmission.company_id == company.id
                )
            ).all()
        )
        == submissions_before
    )


def test_an_intelligence_version_cannot_read_another_companys_dossier(
    db_session: Session,
) -> None:
    seeded(db_session)
    mine = make_company(db_session, name="Kiln Systems")
    theirs = make_company(db_session, name="Other Corp")
    make_dossier(db_session, company=mine)
    stolen = make_dossier(db_session, company=theirs)

    db_session.add(
        CompanyIntelligenceVersion(
            company_id=mine.id,
            version_number=1,
            dossier_version_id=stolen.id,
            dossier_version_number=stolen.version_number,
            producer="x",
            producer_version="1",
            policy_version="1",
            input_digest="deadbeef",
        )
    )
    with pytest.raises(Exception):  # noqa: B017 - the database refuses; the class varies
        db_session.flush()
    db_session.rollback()


# --- operator decisions -----------------------------------------------------


def _one_industry_version(session: Session, company: Company) -> ci_producer.ProductionResult:
    return _produce(
        session,
        company,
        answer(
            classifications=[
                {
                    "dimension": "industry",
                    "value": "Manufacturing",
                    "evidence": ["F1"],
                    "is_primary": True,
                    "confidence": 0.8,
                }
            ]
        ),
    )


def test_confirming_a_value_never_edits_the_produced_version(db_session: Session) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")
    result = _one_industry_version(db_session, company)
    row = db_session.scalars(
        select(CompanyIntelligenceClassification).where(
            CompanyIntelligenceClassification.intelligence_version_id == result.version.id
        )
    ).one()
    before = (row.state, row.evidence_status, row.model_value, row.term_code)

    ci_review.record_decision(
        db_session,
        company=company,
        version=result.version,
        request=ci_review.DecisionRequest(
            dimension=IntelligenceDimension.INDUSTRY,
            action=IntelligenceDecisionAction.CONFIRM,
            target_key=ci_review.target_key_for(row),
            classification_id=row.id,
            note="checked the about page",
        ),
        actor="sahil",
    )
    db_session.refresh(row)
    assert (row.state, row.evidence_status, row.model_value, row.term_code) == before

    view = ci_read.get_company_intelligence(db_session, company_id=company.id)
    assert view is not None
    industry = view.for_dimension(IntelligenceDimension.INDUSTRY)[0]
    assert industry.source is IntelligenceValueSource.OPERATOR_CONFIRMED
    assert industry.decided_by == "sahil"
    assert industry.decision_note == "checked the about page"


def test_correcting_replaces_the_effective_value_and_keeps_the_original(
    db_session: Session,
) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")
    result = _one_industry_version(db_session, company)
    row = db_session.scalars(
        select(CompanyIntelligenceClassification).where(
            CompanyIntelligenceClassification.intelligence_version_id == result.version.id
        )
    ).one()
    retail = db_session.scalars(
        select(IntelligenceTaxonomyTerm).where(IntelligenceTaxonomyTerm.code == "retail")
    ).one()

    ci_review.record_decision(
        db_session,
        company=company,
        version=result.version,
        request=ci_review.DecisionRequest(
            dimension=IntelligenceDimension.INDUSTRY,
            action=IntelligenceDecisionAction.CORRECT,
            target_key=ci_review.target_key_for(row),
            classification_id=row.id,
            corrected_term_id=retail.id,
            set_primary=True,
            note="they sell direct, not to manufacturers",
        ),
        actor="sahil",
    )

    view = ci_read.get_company_intelligence(db_session, company_id=company.id)
    assert view is not None
    industry = view.for_dimension(IntelligenceDimension.INDUSTRY)
    assert len(industry) == 1
    assert industry[0].display_value == "Retail"
    assert industry[0].source is IntelligenceValueSource.OPERATOR_CORRECTED
    assert industry[0].is_primary is True
    # The model's own wording survives in the stored version.
    db_session.refresh(row)
    assert row.model_value == "Manufacturing"
    assert row.term_code == "manufacturing"


def test_rejecting_removes_the_value_from_the_effective_set_only(db_session: Session) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")
    result = _one_industry_version(db_session, company)
    row = db_session.scalars(
        select(CompanyIntelligenceClassification).where(
            CompanyIntelligenceClassification.intelligence_version_id == result.version.id
        )
    ).one()

    ci_review.record_decision(
        db_session,
        company=company,
        version=result.version,
        request=ci_review.DecisionRequest(
            dimension=IntelligenceDimension.INDUSTRY,
            action=IntelligenceDecisionAction.REJECT,
            target_key=ci_review.target_key_for(row),
            classification_id=row.id,
        ),
    )
    view = ci_read.get_company_intelligence(db_session, company_id=company.id)
    assert view is not None
    assert view.for_dimension(IntelligenceDimension.INDUSTRY) == ()
    assert db_session.get(CompanyIntelligenceClassification, row.id) is not None


def test_marking_unresolved_is_visible_as_such(db_session: Session) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")
    result = _one_industry_version(db_session, company)
    row = db_session.scalars(
        select(CompanyIntelligenceClassification).where(
            CompanyIntelligenceClassification.intelligence_version_id == result.version.id
        )
    ).one()

    ci_review.record_decision(
        db_session,
        company=company,
        version=result.version,
        request=ci_review.DecisionRequest(
            dimension=IntelligenceDimension.INDUSTRY,
            action=IntelligenceDecisionAction.MARK_UNRESOLVED,
            target_key=ci_review.target_key_for(row),
            classification_id=row.id,
        ),
    )
    view = ci_read.get_company_intelligence(db_session, company_id=company.id)
    assert view is not None
    industry = view.for_dimension(IntelligenceDimension.INDUSTRY)[0]
    assert industry.state is IntelligenceValueState.UNRESOLVED
    assert industry.source is IntelligenceValueSource.OPERATOR_UNRESOLVED
    assert view.primary_industry() is None


def test_a_second_decision_supersedes_the_first_without_deleting_it(
    db_session: Session,
) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")
    result = _one_industry_version(db_session, company)
    row = db_session.scalars(
        select(CompanyIntelligenceClassification).where(
            CompanyIntelligenceClassification.intelligence_version_id == result.version.id
        )
    ).one()
    request = ci_review.DecisionRequest(
        dimension=IntelligenceDimension.INDUSTRY,
        action=IntelligenceDecisionAction.CONFIRM,
        target_key=ci_review.target_key_for(row),
        classification_id=row.id,
    )
    first = ci_review.record_decision(
        db_session, company=company, version=result.version, request=request
    )
    second = ci_review.record_decision(
        db_session,
        company=company,
        version=result.version,
        request=ci_review.DecisionRequest(
            dimension=IntelligenceDimension.INDUSTRY,
            action=IntelligenceDecisionAction.REJECT,
            target_key=ci_review.target_key_for(row),
            classification_id=row.id,
        ),
    )
    db_session.refresh(first)
    assert first.is_current is False
    assert first.superseded_at is not None
    assert first.superseded_by_id == second.id
    assert len(ci_review.decision_history(db_session, company_id=company.id)) == 2
    assert len(ci_review.current_decisions(db_session, company_id=company.id)) == 1


def test_a_confirmation_survives_a_later_production_run(db_session: Session) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")
    first = _one_industry_version(db_session, company)
    row = db_session.scalars(
        select(CompanyIntelligenceClassification).where(
            CompanyIntelligenceClassification.intelligence_version_id == first.version.id
        )
    ).one()
    ci_review.record_decision(
        db_session,
        company=company,
        version=first.version,
        request=ci_review.DecisionRequest(
            dimension=IntelligenceDimension.INDUSTRY,
            action=IntelligenceDecisionAction.CONFIRM,
            target_key=ci_review.target_key_for(row),
            classification_id=row.id,
        ),
        actor="sahil",
    )

    make_fact(db_session, company=company, claim="products: kiln controllers")
    _produce(
        db_session,
        company,
        answer(
            classifications=[
                {"dimension": "industry", "value": "Manufacturing", "evidence": ["F1"]},
                {"dimension": "product", "value": "Kiln controllers", "evidence": ["F2"]},
            ]
        ),
    )

    view = ci_read.get_company_intelligence(db_session, company_id=company.id)
    assert view is not None
    assert view.current_version is not None and view.current_version.version_number == 2
    industry = view.for_dimension(IntelligenceDimension.INDUSTRY)[0]
    assert industry.source is IntelligenceValueSource.OPERATOR_CONFIRMED, (
        "an operator's confirmation must not be discarded by a later model run"
    )


def test_a_decision_the_newest_version_no_longer_proposes_is_reported_as_such(
    db_session: Session,
) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")
    first = _one_industry_version(db_session, company)
    row = db_session.scalars(
        select(CompanyIntelligenceClassification).where(
            CompanyIntelligenceClassification.intelligence_version_id == first.version.id
        )
    ).one()
    ci_review.record_decision(
        db_session,
        company=company,
        version=first.version,
        request=ci_review.DecisionRequest(
            dimension=IntelligenceDimension.INDUSTRY,
            action=IntelligenceDecisionAction.CONFIRM,
            target_key=ci_review.target_key_for(row),
            classification_id=row.id,
        ),
    )

    make_fact(db_session, company=company, claim="industries served: retail")
    _produce(
        db_session,
        company,
        answer(
            classifications=[
                {"dimension": "industry", "value": "Retail", "evidence": ["F2"]},
            ]
        ),
    )
    view = ci_read.get_company_intelligence(db_session, company_id=company.id)
    assert view is not None
    assert view.stale_decision_count == 1
    kept = [item for item in view.for_dimension(IntelligenceDimension.INDUSTRY)]
    assert {item.display_value for item in kept} == {"Retail", "Manufacturing"}
    assert any(item.operator_only for item in kept)


def test_a_correction_must_use_the_active_vocabulary(db_session: Session) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")
    result = _one_industry_version(db_session, company)

    retired = ci_taxonomy.create_taxonomy(
        db_session,
        dimension=IntelligenceDimension.INDUSTRY,
        version="2020.01",
        title="Old industries",
    )
    stale_term = ci_taxonomy.add_term(
        db_session, taxonomy=retired, code="legacy", canonical_label="Legacy"
    )
    with pytest.raises(ci_review.IntelligenceReviewError):
        ci_review.record_decision(
            db_session,
            company=company,
            version=result.version,
            request=ci_review.DecisionRequest(
                dimension=IntelligenceDimension.INDUSTRY,
                action=IntelligenceDecisionAction.CORRECT,
                target_key="manufacturing",
                corrected_term_id=stale_term.id,
            ),
        )


def test_mapping_an_alias_teaches_the_next_run_not_the_stored_one(
    db_session: Session,
) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: kiln automation")
    result = _produce(
        db_session,
        company,
        answer(
            classifications=[
                {"dimension": "industry", "value": "Kiln automation", "evidence": ["F1"]}
            ]
        ),
    )
    row = db_session.scalars(
        select(CompanyIntelligenceClassification).where(
            CompanyIntelligenceClassification.intelligence_version_id == result.version.id
        )
    ).one()
    assert row.normalization is IntelligenceNormalization.UNMAPPED

    manufacturing = db_session.scalars(
        select(IntelligenceTaxonomyTerm).where(IntelligenceTaxonomyTerm.code == "manufacturing")
    ).one()
    ci_review.map_alias(
        db_session,
        dimension=IntelligenceDimension.INDUSTRY,
        alias="Kiln automation",
        term_id=manufacturing.id,
        actor="sahil",
    )

    db_session.refresh(row)
    assert row.normalization is IntelligenceNormalization.UNMAPPED, (
        "a stored version is immutable; an alias changes the next run, not this one"
    )
    assert (
        ci_taxonomy.resolve(
            db_session, dimension=IntelligenceDimension.INDUSTRY, value="Kiln automation"
        ).normalization
        is IntelligenceNormalization.ALIAS
    )
    stored = db_session.scalars(
        select(IntelligenceTaxonomyAlias).where(
            IntelligenceTaxonomyAlias.normalized_alias == "kiln automation"
        )
    ).one()
    assert stored.approved_at is not None
    assert stored.created_by == "sahil"


# --- read model -------------------------------------------------------------


def test_read_model_for_a_company_with_nothing_says_so(db_session: Session) -> None:
    company = make_company(db_session)
    view = ci_read.get_company_intelligence(db_session, company_id=company.id)
    assert view is not None
    assert view.has_intelligence is False
    assert view.current_version is None
    assert view.classifications == ()


def test_read_model_separates_latest_reviewed_from_latest_model_version(
    db_session: Session,
) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")
    first = _one_industry_version(db_session, company)
    row = db_session.scalars(
        select(CompanyIntelligenceClassification).where(
            CompanyIntelligenceClassification.intelligence_version_id == first.version.id
        )
    ).one()

    view = ci_read.get_company_intelligence(db_session, company_id=company.id)
    assert view is not None
    assert view.latest_model_version is not None
    assert view.latest_reviewed_version is None, "nobody has reviewed anything yet"

    ci_review.record_decision(
        db_session,
        company=company,
        version=first.version,
        request=ci_review.DecisionRequest(
            dimension=IntelligenceDimension.INDUSTRY,
            action=IntelligenceDecisionAction.CONFIRM,
            target_key=ci_review.target_key_for(row),
            classification_id=row.id,
        ),
    )
    reviewed = ci_read.get_company_intelligence(db_session, company_id=company.id)
    assert reviewed is not None
    assert reviewed.latest_reviewed_version is not None
    assert reviewed.latest_reviewed_version.version_number == 1


def test_settled_values_exclude_unsupported_and_conflicted(db_session: Session) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")

    _produce(
        db_session,
        company,
        answer(
            classifications=[
                {"dimension": "industry", "value": "Manufacturing", "evidence": ["F1"]},
                {"dimension": "company_type", "value": "Manufacturer", "evidence": []},
            ]
        ),
    )
    view = ci_read.get_company_intelligence(db_session, company_id=company.id)
    assert view is not None
    assert view.settled_values(IntelligenceDimension.INDUSTRY) == ("Manufacturing",)
    assert view.settled_values(IntelligenceDimension.COMPANY_TYPE) == ()
    assert len(view.unresolved()) == 1


def test_primary_industry_refuses_to_guess_when_conflicted(db_session: Session) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")
    make_fact(db_session, company=company, claim="industries served: retail")

    _produce(
        db_session,
        company,
        answer(
            classifications=[
                {
                    "dimension": "industry",
                    "value": "Manufacturing",
                    "evidence": ["F1"],
                    "is_primary": True,
                },
                {"dimension": "industry", "value": "Retail", "evidence": ["F2"]},
            ],
            conflicts=[
                {
                    "dimension": "industry",
                    "values": ["Manufacturing", "Retail"],
                    "statement": "two pages say different things",
                }
            ],
        ),
    )
    view = ci_read.get_company_intelligence(db_session, company_id=company.id)
    assert view is not None
    assert view.primary_industry() is None
    assert len(view.conflicts) == 1
    assert set(view.conflicts[0].values) == {"Manufacturing", "Retail"}


def test_evidence_is_visible_through_the_read_model(db_session: Session) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    fact = make_fact(db_session, company=company, claim="industries served: manufacturing")
    _one_industry_version(db_session, company)

    view = ci_read.get_company_intelligence(db_session, company_id=company.id)
    assert view is not None
    industry = view.for_dimension(IntelligenceDimension.INDUSTRY)[0]
    assert len(industry.evidence) == 1
    assert industry.evidence[0].insight_id == fact.id
    assert industry.evidence[0].source_url == "https://kiln.example/about"


def test_get_many_returns_one_entry_per_known_company(db_session: Session) -> None:
    seeded(db_session)
    first = make_company(db_session, name="Kiln Systems")
    second = make_company(db_session, name="Other Corp")
    make_dossier(db_session, company=first)
    make_fact(db_session, company=first, claim="industries served: manufacturing")
    _one_industry_version(db_session, first)

    views = ci_read.get_many(db_session, company_ids=[first.id, second.id, uuid.uuid4()])
    assert set(views) == {first.id, second.id}
    assert views[first.id].has_intelligence is True
    assert views[second.id].has_intelligence is False


def test_decisions_are_audited(db_session: Session) -> None:
    from app.models.audit_event import AuditEvent

    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")
    result = _one_industry_version(db_session, company)
    row = db_session.scalars(
        select(CompanyIntelligenceClassification).where(
            CompanyIntelligenceClassification.intelligence_version_id == result.version.id
        )
    ).one()
    ci_review.record_decision(
        db_session,
        company=company,
        version=result.version,
        request=ci_review.DecisionRequest(
            dimension=IntelligenceDimension.INDUSTRY,
            action=IntelligenceDecisionAction.CONFIRM,
            target_key=ci_review.target_key_for(row),
            classification_id=row.id,
        ),
        actor="sahil",
    )
    actions = {
        event.action
        for event in db_session.scalars(
            select(AuditEvent).where(AuditEvent.entity_id == str(company.id))
        ).all()
    }
    assert "company_intelligence.version_produced" in actions
    assert "company_intelligence.decision_recorded" in actions


def test_decisions_cannot_cross_companies(db_session: Session) -> None:
    seeded(db_session)
    mine = make_company(db_session, name="Kiln Systems")
    theirs = make_company(db_session, name="Other Corp")
    make_dossier(db_session, company=mine)
    make_fact(db_session, company=mine, claim="industries served: manufacturing")
    result = _one_industry_version(db_session, mine)
    row = db_session.scalars(
        select(CompanyIntelligenceClassification).where(
            CompanyIntelligenceClassification.intelligence_version_id == result.version.id
        )
    ).one()

    with pytest.raises(ci_review.IntelligenceReviewError):
        ci_review.record_decision(
            db_session,
            company=theirs,
            version=result.version,
            request=ci_review.DecisionRequest(
                dimension=IntelligenceDimension.INDUSTRY,
                action=IntelligenceDecisionAction.CONFIRM,
                target_key=ci_review.target_key_for(row),
                classification_id=row.id,
            ),
        )


def test_decision_rows_survive_a_deleted_version_reference(db_session: Session) -> None:
    seeded(db_session)
    company = make_company(db_session)
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="industries served: manufacturing")
    result = _one_industry_version(db_session, company)
    row = db_session.scalars(
        select(CompanyIntelligenceClassification).where(
            CompanyIntelligenceClassification.intelligence_version_id == result.version.id
        )
    ).one()
    decision = ci_review.record_decision(
        db_session,
        company=company,
        version=result.version,
        request=ci_review.DecisionRequest(
            dimension=IntelligenceDimension.INDUSTRY,
            action=IntelligenceDecisionAction.CONFIRM,
            target_key=ci_review.target_key_for(row),
            classification_id=row.id,
        ),
    )
    db_session.delete(result.version)
    db_session.flush()
    db_session.refresh(decision)
    assert decision.intelligence_version_id is None
    assert db_session.get(CompanyIntelligenceDecision, decision.id) is not None, (
        "an operator's judgement outlives the version that prompted it"
    )

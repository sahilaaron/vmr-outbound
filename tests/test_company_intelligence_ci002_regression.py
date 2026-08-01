"""CI-002 regression and boundary tests.

CI-002 is an enhancement of a shipped system, so most of its risk is not in what
it adds — it is in what it might quietly break or quietly widen. These tests are
the fence.

Each one names a guarantee CI-001 established and proves CI-002 did not spend it:
the architecture is unchanged, the model still gets no tools, one model call per
new input, nothing is persisted from a malformed answer, no canonical Company
field moves, Research is not mutated, Personalization and Sending are untouched,
no pipeline stage or Agent appeared, review stays append-only, the feature stays
off by default, and the customer app never sees any of it.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from app.core.config import get_settings
from app.main import create_app
from app.models.company import Company
from app.models.company_dossier import CompanyDossierVersion, CompanyResearchSubmission
from app.models.company_intelligence import (
    CompanyIntelligenceClassification,
    CompanyIntelligenceDecision,
    CompanyIntelligenceVersion,
)
from app.models.enums import (
    AgentIdentifier,
    IntelligenceDecisionAction,
    IntelligenceDimension,
    IntelligenceValueState,
)
from app.models.insight import Insight
from app.services.agents.registry import PIPELINE_ORDER
from app.services.company_intelligence import producer as ci_producer
from app.services.company_intelligence import read as ci_read
from app.services.company_intelligence import review as ci_review
from app.services.company_intelligence import runner as ci_runner
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.test_company_intelligence import (
    assemble,
    make_company,
    make_dossier,
    make_fact,
    seeded,
)
from tests.test_company_intelligence_jobs import INDUSTRY_ANSWER, ScriptedThinker, factory

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("FEATURES__COMPANY_INTELLIGENCE", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def ready(session: Session, *, name: str = "Kiln Systems") -> Company:
    seeded(session)
    company = make_company(session, name=name)
    make_dossier(session, company=company)
    make_fact(
        session,
        company=company,
        claim="headquarters: headquartered in London, United Kingdom",
        key=f"reg:{name}:1",
    )
    return company


# --- the architecture is unchanged -----------------------------------------


def test_no_new_agent_identifier_exists() -> None:
    assert [member.value for member in AgentIdentifier] == [
        "capture",
        "identity",
        "company",
        "research",
        "email",
        "verification",
        "insights",
        "personalization",
        "sending",
    ]


def test_no_new_pipeline_stage_exists() -> None:
    assert len(PIPELINE_ORDER) == 9
    assert PIPELINE_ORDER[3] is AgentIdentifier.RESEARCH
    assert PIPELINE_ORDER[6] is AgentIdentifier.INSIGHTS


def test_company_intelligence_still_has_its_own_company_scoped_queue() -> None:
    from app.models.company_intelligence import CompanyIntelligenceJob

    columns = {column.name for column in CompanyIntelligenceJob.__table__.columns}
    assert "company_id" in columns
    assert "campaign_contact_id" not in columns
    assert "agent_id" not in columns


def test_personalization_and_sending_are_untouched_by_this_branch() -> None:
    """Read as a boundary check rather than a diff: nothing in the Company
    Intelligence package may import the drafting or sending path, in either
    direction."""

    package = REPO_ROOT / "app" / "services" / "company_intelligence"
    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "services.drafts" not in text, path
        assert "PersonalizationAgentAdapter" not in text, path
        assert "saleshandy" not in text.lower(), path
        assert "app.services.agents" not in text, path


def test_the_producer_never_writes_research_or_canonical_company_fields(
    db_session: Session,
) -> None:
    company = ready(db_session, name="Boundary Co")
    before = {
        "name": company.name,
        "domain": company.domain,
        "industry": company.industry,
        "country": company.country,
        "company_size": company.company_size,
    }
    dossier_ids = {
        row.id
        for row in db_session.scalars(
            select(CompanyDossierVersion).where(CompanyDossierVersion.company_id == company.id)
        ).all()
    }
    insight_claims = {
        row.claim
        for row in db_session.scalars(select(Insight).where(Insight.company_id == company.id)).all()
    }
    submissions = len(
        db_session.scalars(
            select(CompanyResearchSubmission).where(
                CompanyResearchSubmission.company_id == company.id
            )
        ).all()
    )

    source = assemble(db_session, company)
    handles = {item.place.label: item.handle for item in source.geography.candidates}
    ci_producer.produce(
        db_session,
        company=company,
        source=source,
        answer={
            "classifications": [
                {"dimension": "specialty", "value": "kiln control engineering", "evidence": ["F1"]}
            ],
            "geography": [
                {
                    "candidate": handles["London"],
                    "relationship": "headquarters",
                    "evidence": ["F1"],
                }
            ],
        },
        raw_answer="{}",
    )
    db_session.refresh(company)

    assert {
        "name": company.name,
        "domain": company.domain,
        "industry": company.industry,
        "country": company.country,
        "company_size": company.company_size,
    } == before
    assert {
        row.id
        for row in db_session.scalars(
            select(CompanyDossierVersion).where(CompanyDossierVersion.company_id == company.id)
        ).all()
    } == dossier_ids
    assert {
        row.claim
        for row in db_session.scalars(select(Insight).where(Insight.company_id == company.id)).all()
    } == insight_claims
    assert (
        len(
            db_session.scalars(
                select(CompanyResearchSubmission).where(
                    CompanyResearchSubmission.company_id == company.id
                )
            ).all()
        )
        == submissions
    )


# --- the model boundary ------------------------------------------------------


def test_the_model_still_gets_no_tools(db_session: Session, enabled: None) -> None:
    company = ready(db_session, name="Tools Co")
    thinker = ScriptedThinker(INDUSTRY_ANSWER)
    ci_runner.produce_for_company(db_session, company=company, thinker_factory=factory(thinker))
    assert thinker.requests[0].allowed_tools == ()


def test_geography_and_specialty_share_the_one_model_call(
    db_session: Session, enabled: None
) -> None:
    company = ready(db_session, name="OneCall Co")
    thinker = ScriptedThinker(INDUSTRY_ANSWER)
    ci_runner.produce_for_company(db_session, company=company, thinker_factory=factory(thinker))
    assert len(thinker.requests) == 1, (
        "one structured answer covers every dimension; a second call for geography "
        "would double the cost of every company"
    )
    prompt = thinker.requests[0].prompt
    assert "GEOGRAPHY CANDIDATES" in prompt
    assert "WHAT COUNTS AS A SPECIALTY" in prompt


def test_idempotency_is_still_checked_before_the_model_call(
    db_session: Session, enabled: None
) -> None:
    company = ready(db_session, name="Idem Co")
    ci_runner.produce_for_company(
        db_session, company=company, thinker_factory=factory(ScriptedThinker(INDUSTRY_ANSWER))
    )
    second = ScriptedThinker(INDUSTRY_ANSWER)
    outcome = ci_runner.produce_for_company(
        db_session, company=company, thinker_factory=factory(second)
    )
    assert outcome.succeeded and not outcome.created
    assert second.requests == [], "unchanged evidence must not spend a second call"


def test_a_malformed_answer_still_persists_nothing(db_session: Session, enabled: None) -> None:
    company = ready(db_session, name="Malformed Co")
    outcome = ci_runner.produce_for_company(
        db_session,
        company=company,
        thinker_factory=factory(ScriptedThinker({"classifications": "nope"})),
    )
    assert not outcome.succeeded and outcome.retryable
    assert (
        db_session.scalars(
            select(CompanyIntelligenceVersion).where(
                CompanyIntelligenceVersion.company_id == company.id
            )
        ).first()
        is None
    )


def test_a_malformed_geography_block_does_not_sink_the_whole_answer(
    db_session: Session,
) -> None:
    company = ready(db_session, name="PartialGeo Co")
    result = ci_producer.produce(
        db_session,
        company=company,
        source=assemble(db_session, company),
        answer={
            "classifications": [
                {"dimension": "specialty", "value": "kiln control engineering", "evidence": ["F1"]}
            ],
            "geography": "not a list",
        },
        raw_answer="{}",
    )
    assert result.created
    assert any("`geography` was not a list" in warning for warning in result.warnings)


def test_the_prompt_and_raw_answer_are_still_not_persisted(db_session: Session) -> None:
    company = ready(db_session, name="Secret Co")
    secret = '{"classifications": [], "note": "SECRET-PROMPT-CONTENT"}'
    result = ci_producer.produce(
        db_session,
        company=company,
        source=assemble(db_session, company),
        answer={"classifications": []},
        raw_answer=secret,
    )
    stored = repr(
        {
            column.name: getattr(result.version, column.name)
            for column in CompanyIntelligenceVersion.__table__.columns
        }
    )
    assert "SECRET-PROMPT-CONTENT" not in stored
    assert result.version.answer_digest and len(result.version.answer_digest) == 64


def test_the_producer_receives_no_session_and_cannot_browse() -> None:
    """The geography extractor works on the assembled input alone.

    Its signature is the guarantee: nothing it is given can reach the network or
    the database, so "does not browse" is a property of the boundary rather than
    a rule somebody has to remember.
    """

    from app.services.company_intelligence import geography as geo

    signature = inspect.signature(geo.extract_candidates)
    assert list(signature.parameters) == ["source", "base"]


# --- existing data keeps working --------------------------------------------


def test_a_ci001_shaped_classification_still_reads(db_session: Session) -> None:
    """A row written before CI-002 has NULL in all three new columns, which is
    the truthful reading: it never asserted a relationship."""

    company = ready(db_session, name="Legacy Co")
    result = ci_producer.produce(
        db_session,
        company=company,
        source=assemble(db_session, company),
        answer={
            "classifications": [
                {"dimension": "industry", "value": "Manufacturing", "evidence": ["F1"]}
            ]
        },
        raw_answer="{}",
    )
    row = db_session.scalars(
        select(CompanyIntelligenceClassification).where(
            CompanyIntelligenceClassification.intelligence_version_id == result.version.id,
            CompanyIntelligenceClassification.dimension == IntelligenceDimension.INDUSTRY,
        )
    ).one()
    assert row.geo_relationship is None
    assert row.presence_kind is None
    assert row.normalized_value is None

    view = ci_read.get_company_intelligence(db_session, company_id=company.id)
    assert view is not None
    industry = view.for_dimension(IntelligenceDimension.INDUSTRY)[0]
    assert industry.display_value == "Manufacturing"
    assert industry.geo_relationship is None
    assert industry.state is IntelligenceValueState.RESOLVED


def test_every_ci001_dimension_still_classifies(db_session: Session) -> None:
    company = ready(db_session, name="AllDims Co")
    result = ci_producer.produce(
        db_session,
        company=company,
        source=assemble(db_session, company),
        answer={
            "classifications": [
                {"dimension": "industry", "value": "Manufacturing", "evidence": ["F1"]},
                {
                    "dimension": "subindustry",
                    "value": "Industrial Machinery & Equipment",
                    "evidence": ["F1"],
                },
                {"dimension": "product", "value": "kiln controllers", "evidence": ["F1"]},
                {"dimension": "service", "value": "commissioning support", "evidence": ["F1"]},
                {
                    "dimension": "capability",
                    "value": "high-temperature calibration",
                    "evidence": ["F1"],
                },
                {
                    "dimension": "specialty",
                    "value": "industrial furnace control",
                    "evidence": ["F1"],
                },
                {"dimension": "operating_market", "value": "Europe", "evidence": ["F1"]},
                {"dimension": "customer_segment", "value": "OEMs", "evidence": ["F1"]},
                {"dimension": "business_model", "value": "B2B", "evidence": ["F1"]},
                {"dimension": "company_type", "value": "Manufacturer", "evidence": ["F1"]},
            ]
        },
        raw_answer="{}",
    )
    assert result.created
    view = ci_read.get_company_intelligence(db_session, company_id=company.id)
    assert view is not None
    for dimension in (
        IntelligenceDimension.INDUSTRY,
        IntelligenceDimension.SUBINDUSTRY,
        IntelligenceDimension.PRODUCT,
        IntelligenceDimension.SERVICE,
        IntelligenceDimension.CAPABILITY,
        IntelligenceDimension.SPECIALTY,
        IntelligenceDimension.OPERATING_MARKET,
        IntelligenceDimension.CUSTOMER_SEGMENT,
        IntelligenceDimension.BUSINESS_MODEL,
        IntelligenceDimension.COMPANY_TYPE,
    ):
        assert view.for_dimension(dimension), dimension.value


def test_reading_the_model_repeatedly_writes_nothing(db_session: Session) -> None:
    company = ready(db_session, name="ReadOnly Co")
    ci_producer.produce(
        db_session,
        company=company,
        source=assemble(db_session, company),
        answer={"classifications": []},
        raw_answer="{}",
    )
    db_session.flush()
    before = len(db_session.scalars(select(CompanyIntelligenceClassification)).all())
    for _ in range(3):
        ci_read.get_company_intelligence(db_session, company_id=company.id)
    db_session.flush()
    assert len(db_session.scalars(select(CompanyIntelligenceClassification)).all()) == before


def test_review_remains_append_only_for_a_geography_value(db_session: Session) -> None:
    company = ready(db_session, name="Review Geo Co")
    source = assemble(db_session, company)
    handles = {item.place.label: item.handle for item in source.geography.candidates}
    result = ci_producer.produce(
        db_session,
        company=company,
        source=source,
        answer={
            "classifications": [],
            "geography": [
                {
                    "candidate": handles["London"],
                    "relationship": "headquarters",
                    "evidence": ["F1"],
                }
            ],
        },
        raw_answer="{}",
    )
    row = db_session.scalars(
        select(CompanyIntelligenceClassification).where(
            CompanyIntelligenceClassification.intelligence_version_id == result.version.id,
            CompanyIntelligenceClassification.term_code == "gb-london",
        )
    ).one()
    before = (row.geo_relationship, row.presence_kind, row.state, row.model_value)

    ci_review.record_decision(
        db_session,
        company=company,
        version=result.version,
        request=ci_review.DecisionRequest(
            dimension=IntelligenceDimension.GEOGRAPHY,
            action=IntelligenceDecisionAction.CONFIRM,
            target_key=ci_review.target_key_for(row),
            classification_id=row.id,
            note="checked the about page",
        ),
        actor="sahil",
    )
    db_session.refresh(row)
    assert (row.geo_relationship, row.presence_kind, row.state, row.model_value) == before

    decisions = db_session.scalars(
        select(CompanyIntelligenceDecision).where(
            CompanyIntelligenceDecision.company_id == company.id
        )
    ).all()
    assert len(decisions) == 1
    view = ci_read.get_company_intelligence(db_session, company_id=company.id)
    assert view is not None
    london = next(item for item in view.geographies() if item.term_code == "gb-london")
    assert london.operator_confirmed is True


# --- gating ------------------------------------------------------------------


def test_the_feature_flag_default_is_still_off() -> None:
    assert get_settings().features.company_intelligence is False


def test_the_customer_app_receives_no_company_intelligence_routes(
    monkeypatch: pytest.MonkeyPatch, committed_session: Session
) -> None:
    """Asserted behaviourally rather than by walking ``app.routes``.

    This FastAPI version keeps included routers as opaque objects rather than
    flattening their routes onto the app, so introspection there proves nothing
    about what a request actually reaches. What matters is the response.
    """

    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__COMPANY_INTELLIGENCE", "true")
    get_settings.cache_clear()
    try:
        with TestClient(create_app(get_settings())) as http:
            # The operator surface is reachable...
            assert http.get("/admin/company-intelligence").status_code == 200
            # ...and the customer interface knows nothing about it.
            customer = http.get("/app")
            assert customer.status_code == 200
            assert "company-intelligence" not in customer.text
            assert "Company Intelligence" not in customer.text
            for path in (
                "/app/company-intelligence",
                "/app/companies/00000000-0000-0000-0000-000000000000/intelligence",
            ):
                assert http.get(path).status_code == 404, path
    finally:
        get_settings.cache_clear()


def test_determinism_the_same_answer_produces_the_same_rows(db_session: Session) -> None:
    answer: dict[str, Any] = {
        "classifications": [
            {"dimension": "specialty", "value": "industrial furnace control", "evidence": ["F1"]},
            {"dimension": "industry", "value": "Manufacturing", "evidence": ["F1"]},
        ]
    }

    def shape(name: str) -> list[tuple[str, int, str, str | None]]:
        company = ready(db_session, name=name)
        source = assemble(db_session, company)
        handles = {item.place.label: item.handle for item in source.geography.candidates}
        payload = {
            **answer,
            "geography": [
                {
                    "candidate": handles["London"],
                    "relationship": "headquarters",
                    "evidence": ["F1"],
                }
            ],
        }
        result = ci_producer.produce(
            db_session, company=company, source=source, answer=payload, raw_answer="{}"
        )
        rows = db_session.scalars(
            select(CompanyIntelligenceClassification)
            .where(CompanyIntelligenceClassification.intelligence_version_id == result.version.id)
            .order_by(
                CompanyIntelligenceClassification.dimension,
                CompanyIntelligenceClassification.rank,
            )
        ).all()
        return [(row.dimension.value, row.rank, row.state.value, row.term_code) for row in rows]

    assert shape("Determinism A") == shape("Determinism B")

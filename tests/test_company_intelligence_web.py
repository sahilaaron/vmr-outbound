"""Admin surface tests for Company Intelligence (CI-001).

Three things these tests are actually about:

* **The gate is real.** With the switch off, the routes do not exist. Not a page
  saying "disabled" — a 404, because a disabled feature that still renders is a
  feature that can be reached by accident.
* **The screens tell the truth.** An unverified classification must not look
  confirmed, an unmapped value must not look canonical, and a superseded version
  must render exactly as produced.
* **The rest of the application is untouched.** The customer interface, the
  existing workbench pages and every canonical Company field are the same with
  the feature on as with it off.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from app.core.config import get_settings
from app.main import create_app
from app.models.company import Company
from app.models.company_intelligence import (
    CompanyIntelligenceBackfillRun,
    CompanyIntelligenceClassification,
    CompanyIntelligenceJob,
)
from app.models.enums import IntelligenceDimension
from app.services.company_intelligence import producer as ci_producer
from app.services.company_intelligence import review as ci_review
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


@pytest.fixture()
def workbench_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__COMPANY_INTELLIGENCE", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def workbench_only(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.delenv("FEATURES__COMPANY_INTELLIGENCE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def client() -> TestClient:
    return TestClient(create_app(get_settings()))


def classified_company(session: Session, *, name: str = "Kiln Systems") -> Company:
    seeded(session)
    company = make_company(session, name=name)
    make_dossier(session, company=company)
    make_fact(session, company=company, claim="industries served: manufacturing")
    make_fact(session, company=company, claim="products: kiln automation rigs")
    ci_producer.produce(
        session,
        company=company,
        source=assemble(session, company),
        answer={
            "classifications": [
                {
                    "dimension": "industry",
                    "value": "Manufacturing",
                    "is_primary": True,
                    "evidence": ["F1"],
                    "confidence": 0.85,
                    "rationale": "the about page names the sector",
                },
                {
                    "dimension": "industry",
                    "value": "Kiln automation",
                    "evidence": ["F2"],
                    "confidence": 0.4,
                },
                {"dimension": "product", "value": "Kiln controllers", "evidence": ["F2"]},
            ],
            "unknown_dimensions": ["business_model"],
        },
        raw_answer="{}",
    )
    session.commit()
    return company


# --- the gate ---------------------------------------------------------------


def test_the_routes_do_not_exist_while_the_feature_is_off(workbench_only: None) -> None:
    with client() as http:
        assert http.get("/admin/company-intelligence").status_code == 404
        assert http.get("/admin/company-intelligence/taxonomy").status_code == 404
        assert http.get("/admin/company-intelligence/backfill").status_code == 404


def test_the_existing_workbench_is_unaffected_by_the_switch(
    workbench_only: None, committed_session: Session
) -> None:
    with client() as http:
        assert http.get("/admin").status_code == 200
        assert http.get("/companies").status_code == 200
        assert http.get("/app").status_code == 200


def test_turning_it_on_changes_nothing_about_the_customer_interface(
    workbench_env: None, committed_session: Session
) -> None:
    with client() as http:
        response = http.get("/app")
        assert response.status_code == 200
        assert "Company Intelligence" not in response.text, (
            "the classification surface is operator-only in this release"
        )


# --- the pages --------------------------------------------------------------


def test_the_index_lists_companies_and_their_state(
    workbench_env: None, committed_session: Session
) -> None:
    company = classified_company(committed_session)
    with client() as http:
        response = http.get("/admin/company-intelligence")
        assert response.status_code == 200
        assert company.name in response.text
        assert "verified until an operator confirms it" in response.text


def test_the_detail_page_shows_evidence_uncertainty_and_provenance(
    workbench_env: None, committed_session: Session
) -> None:
    company = classified_company(committed_session)
    with client() as http:
        response = http.get(f"/admin/companies/{company.id}/intelligence")
        assert response.status_code == 200
        body = response.text

    assert "Manufacturing" in body
    assert "canonical" in body
    # The unmapped value keeps the producer's own wording and says it is unmapped.
    assert "Kiln automation" in body
    assert "unmapped" in body
    # Provenance and uncertainty are both visible.
    assert "model" in body
    assert "evidence" in body
    assert "https://kiln.example/about" in body
    assert "unknown" in body


def test_an_unclassifiable_company_says_why_on_the_page(
    workbench_env: None, committed_session: Session
) -> None:
    company = make_company(committed_session, name="No Research Ltd")
    committed_session.commit()
    with client() as http:
        response = http.get(f"/admin/companies/{company.id}/intelligence")
        assert response.status_code == 200
        assert "no current research dossier" in response.text
        assert "Run classification" not in response.text


def test_a_missing_company_is_a_404(workbench_env: None, committed_session: Session) -> None:
    with client() as http:
        response = http.get("/admin/companies/00000000-0000-0000-0000-000000000000/intelligence")
        assert response.status_code == 404


def test_a_superseded_version_renders_exactly_as_produced(
    workbench_env: None, committed_session: Session
) -> None:
    company = classified_company(committed_session)
    row = committed_session.scalars(
        select(CompanyIntelligenceClassification).where(
            CompanyIntelligenceClassification.company_id == company.id,
            CompanyIntelligenceClassification.dimension == IntelligenceDimension.INDUSTRY,
            CompanyIntelligenceClassification.rank == 0,
        )
    ).one()
    version_id = row.intelligence_version_id

    with client() as http:
        http.post(
            f"/admin/companies/{company.id}/intelligence/decisions",
            data={
                "dimension": "industry",
                "action": "reject",
                "target_key": "manufacturing",
                "classification_id": str(row.id),
            },
            follow_redirects=False,
        )
        response = http.get(f"/admin/companies/{company.id}/intelligence/versions/{version_id}")
    assert response.status_code == 200
    assert "Manufacturing" in response.text, (
        "a rejected value must still appear on the version that produced it"
    )
    assert "exactly as it was produced" in response.text


def test_the_taxonomy_page_shows_editions_terms_and_alias_approval(
    workbench_env: None, committed_session: Session
) -> None:
    seeded(committed_session)
    committed_session.commit()
    with client() as http:
        response = http.get("/admin/company-intelligence/taxonomy?dimension=industry")
    assert response.status_code == 200
    assert "Pharma &amp; Healthcare" in response.text or "Pharma & Healthcare" in response.text
    assert "2026.07" in response.text
    assert "released edition" in response.text


# --- operator actions -------------------------------------------------------


def test_running_a_classification_queues_a_job_rather_than_producing_inline(
    workbench_env: None, committed_session: Session
) -> None:
    seeded(committed_session)
    company = make_company(committed_session)
    make_dossier(committed_session, company=company)
    make_fact(committed_session, company=company, claim="industries served: manufacturing")
    committed_session.commit()

    with client() as http:
        response = http.post(
            f"/admin/companies/{company.id}/intelligence/run", follow_redirects=False
        )
    assert response.status_code == 303

    jobs = committed_session.scalars(
        select(CompanyIntelligenceJob).where(CompanyIntelligenceJob.company_id == company.id)
    ).all()
    assert len(jobs) == 1
    assert jobs[0].requested_by == "operator"


def test_pressing_run_twice_does_not_queue_twice(
    workbench_env: None, committed_session: Session
) -> None:
    seeded(committed_session)
    company = make_company(committed_session)
    make_dossier(committed_session, company=company)
    make_fact(committed_session, company=company, claim="industries served: manufacturing")
    committed_session.commit()

    with client() as http:
        http.post(f"/admin/companies/{company.id}/intelligence/run", follow_redirects=False)
        http.post(f"/admin/companies/{company.id}/intelligence/run", follow_redirects=False)

    assert (
        len(
            committed_session.scalars(
                select(CompanyIntelligenceJob).where(
                    CompanyIntelligenceJob.company_id == company.id
                )
            ).all()
        )
        == 1
    )


def test_confirming_from_the_page_records_a_decision_without_editing_the_version(
    workbench_env: None, committed_session: Session
) -> None:
    company = classified_company(committed_session)
    row = committed_session.scalars(
        select(CompanyIntelligenceClassification).where(
            CompanyIntelligenceClassification.company_id == company.id,
            CompanyIntelligenceClassification.rank == 0,
            CompanyIntelligenceClassification.dimension == IntelligenceDimension.INDUSTRY,
        )
    ).one()
    before = (row.state, row.model_value, row.term_code)

    with client() as http:
        response = http.post(
            f"/admin/companies/{company.id}/intelligence/decisions",
            data={
                "dimension": "industry",
                "action": "confirm",
                "target_key": "manufacturing",
                "target_label": "Manufacturing",
                "classification_id": str(row.id),
                "note": "checked the about page",
            },
            follow_redirects=False,
        )
    assert response.status_code == 303

    committed_session.expire_all()
    refreshed = committed_session.get(CompanyIntelligenceClassification, row.id)
    assert refreshed is not None
    assert (refreshed.state, refreshed.model_value, refreshed.term_code) == before

    decisions = ci_review.current_decisions(committed_session, company_id=company.id)
    assert len(decisions) == 1
    assert decisions[0].actor == "operator"
    assert decisions[0].note == "checked the about page"


def test_mapping_an_alias_from_the_page_updates_the_vocabulary_only(
    workbench_env: None, committed_session: Session
) -> None:
    from app.models.intelligence_taxonomy import IntelligenceTaxonomyAlias, IntelligenceTaxonomyTerm

    company = classified_company(committed_session)
    manufacturing = committed_session.scalars(
        select(IntelligenceTaxonomyTerm).where(IntelligenceTaxonomyTerm.code == "manufacturing")
    ).one()

    with client() as http:
        response = http.post(
            f"/admin/companies/{company.id}/intelligence/aliases",
            data={
                "dimension": "industry",
                "alias": "Kiln automation",
                "term_id": str(manufacturing.id),
            },
            follow_redirects=False,
        )
    assert response.status_code == 303

    alias = committed_session.scalars(
        select(IntelligenceTaxonomyAlias).where(
            IntelligenceTaxonomyAlias.normalized_alias == "kiln automation"
        )
    ).one()
    assert alias.approved_at is not None
    # The stored classification is untouched.
    row = committed_session.scalars(
        select(CompanyIntelligenceClassification).where(
            CompanyIntelligenceClassification.company_id == company.id,
            CompanyIntelligenceClassification.model_value == "Kiln automation",
        )
    ).one()
    assert row.term_id is None


def test_a_rejected_decision_reports_its_reason_rather_than_500ing(
    workbench_env: None, committed_session: Session
) -> None:
    company = classified_company(committed_session)
    with client() as http:
        response = http.post(
            f"/admin/companies/{company.id}/intelligence/decisions",
            data={
                "dimension": "not-a-dimension",
                "action": "confirm",
                "target_key": "manufacturing",
            },
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert "err=" in response.headers["location"]


# --- backfill surface -------------------------------------------------------


def test_a_dry_run_can_be_opened_and_advanced_from_the_page(
    workbench_env: None, committed_session: Session
) -> None:
    classified_company(committed_session)
    with client() as http:
        created = http.post(
            "/admin/company-intelligence/backfill",
            data={"label": "preview", "mode": "preview", "batch_size": "10"},
            follow_redirects=False,
        )
        assert created.status_code == 303
        run_id = created.headers["location"].split("run=")[1].split("&")[0]

        advanced = http.post(
            f"/admin/company-intelligence/backfill/{run_id}/advance", follow_redirects=False
        )
        assert advanced.status_code == 303

        page = http.get(f"/admin/company-intelligence/backfill?run={run_id}")
    assert page.status_code == 200
    assert "considered" in page.text

    run = committed_session.get(CompanyIntelligenceBackfillRun, run_id)
    assert run is not None
    assert run.dry_run is True
    assert committed_session.scalars(select(CompanyIntelligenceJob)).first() is None, (
        "a dry run must queue nothing"
    )


def test_an_invalid_batch_size_is_reported_not_crashed(
    workbench_env: None, committed_session: Session
) -> None:
    with client() as http:
        response = http.post(
            "/admin/company-intelligence/backfill",
            data={"label": "bad", "mode": "preview", "batch_size": "0"},
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert "err=" in response.headers["location"]


def test_no_canonical_company_field_is_written_by_any_admin_action(
    workbench_env: None, committed_session: Session
) -> None:
    company = classified_company(committed_session)
    before: dict[str, Any] = {
        "name": company.name,
        "domain": company.domain,
        "industry": company.industry,
        "country": company.country,
        "company_size": company.company_size,
    }
    row = committed_session.scalars(
        select(CompanyIntelligenceClassification).where(
            CompanyIntelligenceClassification.company_id == company.id,
            CompanyIntelligenceClassification.rank == 0,
        )
    ).first()
    assert row is not None

    with client() as http:
        http.post(
            f"/admin/companies/{company.id}/intelligence/decisions",
            data={
                "dimension": "industry",
                "action": "confirm",
                "target_key": "manufacturing",
                "classification_id": str(row.id),
            },
            follow_redirects=False,
        )
        http.post(f"/admin/companies/{company.id}/intelligence/run", follow_redirects=False)

    committed_session.expire_all()
    refreshed = committed_session.get(Company, company.id)
    assert refreshed is not None
    assert {
        "name": refreshed.name,
        "domain": refreshed.domain,
        "industry": refreshed.industry,
        "country": refreshed.country,
        "company_size": refreshed.company_size,
    } == before


# --- CI-002: geography and specialty on the review screens -------------------


def geography_company(session: Session, *, name: str = "Placed Systems") -> Company:
    """A company whose evidence names a headquarters, a plant and a market."""

    seeded(session)
    company = make_company(session, name=name)
    make_dossier(session, company=company)
    make_fact(
        session,
        company=company,
        claim="headquarters: headquartered in London, United Kingdom",
        key=f"web-geo:{name}:1",
    )
    make_fact(
        session,
        company=company,
        claim="office_locations: runs a manufacturing plant in Pune",
        key=f"web-geo:{name}:2",
    )
    make_fact(
        session,
        company=company,
        claim="overview: serves customers across Germany",
        key=f"web-geo:{name}:3",
    )
    source = assemble(session, company)
    handles = {item.place.label: item.handle for item in source.geography.candidates}
    ci_producer.produce(
        session,
        company=company,
        source=source,
        answer={
            "classifications": [
                {
                    "dimension": "specialty",
                    "value": "leading kiln control engineering provider",
                    "evidence": ["F1"],
                    "confidence": 0.8,
                },
                {"dimension": "specialty", "value": "technology", "evidence": ["F1"]},
            ],
            "geography": [
                {
                    "candidate": handles["London"],
                    "relationship": "headquarters",
                    "evidence": ["F1"],
                    "confidence": 0.9,
                },
                {
                    "candidate": handles["Pune"],
                    "relationship": "manufacturing",
                    "evidence": ["F2"],
                    "confidence": 0.85,
                },
                {
                    "candidate": handles["Germany"],
                    "relationship": "commercial_market",
                    "evidence": ["F3"],
                    "confidence": 0.7,
                },
            ],
        },
        raw_answer="{}",
    )
    session.commit()
    return company


def test_the_detail_page_separates_physical_presence_from_market(
    workbench_env: None, committed_session: Session
) -> None:
    company = geography_company(committed_session)
    with client() as http:
        body = http.get(f"/admin/companies/{company.id}/intelligence").text

    assert "Where this company is" in body
    assert "London" in body and "Pune" in body and "Germany" in body
    assert "headquarters" in body
    assert "manufacturing" in body
    # The two presence kinds render differently, which is the whole point.
    assert "physical" in body
    assert "market" in body
    # Country codes are shown so an operator can see what a filter would match.
    assert "GB" in body and "IN" in body


def test_the_detail_page_explains_why_a_value_is_unresolved(
    workbench_env: None, committed_session: Session
) -> None:
    company = geography_company(committed_session, name="Explained Systems")
    with client() as http:
        body = http.get(f"/admin/companies/{company.id}/intelligence").text

    assert "names a whole field rather than a concentration" in body, (
        "a reason code alone is not an explanation an operator can act on"
    )


def test_the_detail_page_shows_a_cleaned_specialty_beside_its_original(
    workbench_env: None, committed_session: Session
) -> None:
    company = geography_company(committed_session, name="Cleaned Systems")
    with client() as http:
        body = http.get(f"/admin/companies/{company.id}/intelligence").text

    assert "kiln control engineering" in body
    assert "cleaned from" in body
    assert "leading kiln control engineering provider" in body


def test_a_geography_value_can_be_confirmed_through_the_existing_review_flow(
    workbench_env: None, committed_session: Session
) -> None:
    company = geography_company(committed_session, name="Reviewed Systems")
    row = committed_session.scalars(
        select(CompanyIntelligenceClassification).where(
            CompanyIntelligenceClassification.company_id == company.id,
            CompanyIntelligenceClassification.term_code == "gb-london",
        )
    ).one()
    before = (row.geo_relationship, row.presence_kind, row.state)

    with client() as http:
        response = http.post(
            f"/admin/companies/{company.id}/intelligence/decisions",
            data={
                "dimension": "geography",
                "action": "confirm",
                "target_key": "gb-london",
                "target_label": "London",
                "classification_id": str(row.id),
                "note": "the about page says so",
            },
            follow_redirects=False,
        )
    assert response.status_code == 303

    committed_session.expire_all()
    refreshed = committed_session.get(CompanyIntelligenceClassification, row.id)
    assert refreshed is not None
    assert (refreshed.geo_relationship, refreshed.presence_kind, refreshed.state) == before

    decisions = ci_review.current_decisions(committed_session, company_id=company.id)
    assert [decision.dimension.value for decision in decisions] == ["geography"]
    assert decisions[0].target_label == "London"


def test_the_version_page_shows_the_relationship_as_produced(
    workbench_env: None, committed_session: Session
) -> None:
    company = geography_company(committed_session, name="Versioned Systems")
    row = committed_session.scalars(
        select(CompanyIntelligenceClassification).where(
            CompanyIntelligenceClassification.company_id == company.id,
            CompanyIntelligenceClassification.term_code == "in-pune",
        )
    ).one()
    with client() as http:
        body = http.get(
            f"/admin/companies/{company.id}/intelligence/versions/{row.intelligence_version_id}"
        ).text
    assert "manufacturing" in body
    assert "exactly as it was produced" in body


def test_the_vocabulary_browser_shows_the_geography_edition(
    workbench_env: None, committed_session: Session
) -> None:
    seeded(committed_session)
    committed_session.commit()
    with client() as http:
        body = http.get("/admin/company-intelligence/taxonomy?dimension=geography").text
    assert "United Kingdom" in body
    assert "London" in body
    assert "released edition" in body

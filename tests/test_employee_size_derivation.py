"""INS-002 deterministic Employee Size parsing, lineage and conflict policy."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.models.company import Company
from app.models.enums import AgentIdentifier, AgentJobStatus, InsightKind, InsightState
from app.models.insight import Insight, InsightEvidence
from app.models.verification_job import AgentJob
from app.services.companies import dossiers
from app.services.insights import employee_size
from app.services.insights.evidence import (
    EvidenceInput,
    create_insight,
    is_personalization_eligible,
)
from sqlalchemy import select
from sqlalchemy.orm import Session


def _job(db: Session, agent_id: AgentIdentifier) -> AgentJob:
    job = AgentJob(
        agent_id=agent_id,
        idempotency_key=f"ins-002:{agent_id.value}:{uuid.uuid4()}",
        task_kind="advance_campaign_contact",
        status=AgentJobStatus.SUCCEEDED,
        attempts=1,
        max_attempts=3,
        input_reference={},
        result={},
        finished_at=datetime.now(UTC),
    )
    db.add(job)
    db.flush()
    return job


def _setup(db: Session) -> tuple[Company, AgentJob, AgentJob, object]:
    company = Company(name="Kiln Systems", domain=f"{uuid.uuid4()}.example")
    db.add(company)
    db.flush()
    research_job = _job(db, AgentIdentifier.RESEARCH)
    insights_job = _job(db, AgentIdentifier.INSIGHTS)
    submission, _ = dossiers.submit(
        db,
        company=company,
        producer="test-research",
        payload={"overview": {"summary": "bounded test evidence"}},
    )
    dossier = dossiers.interpret(
        db,
        company=company,
        submission=submission,
        interpreter="test-research",
        sections={"overview": {"summary": "bounded test evidence"}},
    )
    return company, research_job, insights_job, dossier


def _handle(
    db: Session,
    *,
    company: Company,
    research_job: AgentJob,
    wording: str,
    index: int,
    observed_at: datetime | None = None,
) -> uuid.UUID:
    observed = observed_at or datetime.now(UTC)
    insight = create_insight(
        db,
        claim=wording,
        kind=InsightKind.FACT,
        state=InsightState.SUPPORTED,
        evidence=[
            EvidenceInput(
                source_url=f"https://kiln.example/source/{index}",
                retrieved_at=observed,
                published_at=observed,
                freshness_at=observed,
                evidence_summary=wording,
                excerpt=wording,
                confidence=0.8,
                extraction_method="research-test/v1",
            )
        ],
        company_id=company.id,
        idempotency_key=f"research:{research_job.id}:website:{index}",
    )
    return (
        db.scalars(select(InsightEvidence).where(InsightEvidence.insight_id == insight.id)).one().id
    )


def _derive(
    db: Session,
    *,
    wording: str,
    context: str = "current",
    observed_at: datetime | None = None,
) -> Insight:
    company, research_job, insights_job, dossier = _setup(db)
    handle = _handle(
        db,
        company=company,
        research_job=research_job,
        wording=wording,
        index=0,
        observed_at=observed_at,
    )
    catalog = employee_size.research_evidence_catalog(
        db, research_job_id=research_job.id, company_id=company.id
    )
    return employee_size.derive_and_store(
        db,
        company_id=company.id,
        insights_job=insights_job,
        dossier=dossier,
        catalog=catalog,
        model_output={
            "candidates": [
                {
                    "source_wording": wording,
                    "evidence_handles": [str(handle)],
                    "observation_context": context,
                }
            ]
        },
        actor="test",
    )


@pytest.mark.parametrize(
    ("wording", "exact", "approximate", "lower", "upper", "band", "status"),
    [
        ("The company employs 430 people", 430, None, 430, 430, "251_500", "supported"),
        ("The workforce is 51–100 employees", None, None, 51, 100, "51_100", "supported"),
        ("A team of approximately 75", None, 75, 51, 100, "51_100", "supported"),
        ("More than 1,000 employees", None, None, 1001, None, "unknown", "unresolved"),
        ("More than 10,000 employees", None, None, 10001, None, "10001_plus", "supported"),
        ("Fewer than 11 staff", None, None, None, 10, "1_10", "supported"),
        ("The company has 5,001 team members", 5001, None, 5001, 5001, "5001_10000", "supported"),
    ],
)
def test_numeric_forms_normalize_deterministically(
    db_session: Session,
    wording: str,
    exact: int | None,
    approximate: int | None,
    lower: int | None,
    upper: int | None,
    band: str,
    status: str,
) -> None:
    payload = _derive(db_session, wording=wording).structured_payload or {}
    assert payload["exact_count"] == exact
    assert payload["approximate_count"] == approximate
    assert payload["lower_bound"] == lower
    assert payload["upper_bound"] == upper
    assert payload["normalized_band"] == band
    assert payload["status"] == status


@pytest.mark.parametrize(
    ("boundary", "band"),
    [
        (10, "1_10"),
        (11, "11_50"),
        (50, "11_50"),
        (51, "51_100"),
        (100, "51_100"),
        (101, "101_250"),
        (250, "101_250"),
        (251, "251_500"),
        (500, "251_500"),
        (501, "501_1000"),
        (1_000, "501_1000"),
        (1_001, "1001_5000"),
        (5_000, "1001_5000"),
        (5_001, "5001_10000"),
        (10_000, "5001_10000"),
        (10_001, "10001_plus"),
    ],
)
def test_all_band_boundaries_are_stable(boundary: int, band: str) -> None:
    assert employee_size.band_for_count(boundary).value == band


@pytest.mark.parametrize(
    ("wording", "context"),
    [
        ("Founded by a team of 12", "current"),
        ("The customer employs 5,000 people", "customer"),
        ("The parent company employs 5,000 people", "parent"),
        ("The subsidiary employs 250 people", "subsidiary"),
        ("Across its portfolio companies, the group employs 20,000", "portfolio"),
        ("The Pune office has 80 staff", "office"),
        ("The company plans to hire 100 employees", "planned"),
        ("The company laid off 100 employees", "layoff"),
        ("The company uses 50 contractors", "contractor"),
        ("A growing global workforce", "current"),
    ],
)
def test_non_subject_and_non_numeric_wording_never_settles(
    db_session: Session, wording: str, context: str
) -> None:
    payload = _derive(db_session, wording=wording, context=context).structured_payload or {}
    assert payload["status"] in {"unavailable", "unresolved"}
    assert payload["exact_count"] is None
    assert payload["normalized_band"] == "unknown"


def test_conflicting_current_sources_retain_both_and_settle_nothing(
    db_session: Session,
) -> None:
    company, research_job, insights_job, dossier = _setup(db_session)
    handles = [
        _handle(
            db_session,
            company=company,
            research_job=research_job,
            wording=wording,
            index=index,
        )
        for index, wording in enumerate(
            ("The company employs 750 people", "The company employs 1,200 people")
        )
    ]
    catalog = employee_size.research_evidence_catalog(
        db_session, research_job_id=research_job.id, company_id=company.id
    )
    insight = employee_size.derive_and_store(
        db_session,
        company_id=company.id,
        insights_job=insights_job,
        dossier=dossier,
        catalog=catalog,
        model_output={
            "candidates": [
                {"evidence_handles": [str(handle)], "observation_context": "current"}
                for handle in handles
            ]
        },
        actor="test",
    )
    payload = insight.structured_payload or {}
    assert insight.state is InsightState.CONFLICTING
    assert payload["status"] == "conflicted"
    assert payload["exact_count"] is None
    assert len(payload["conflicts"]) == 2
    assert (
        len(
            list(
                db_session.scalars(
                    select(InsightEvidence).where(InsightEvidence.insight_id == insight.id)
                )
            )
        )
        == 2
    )


def test_matching_sources_support_one_value(db_session: Session) -> None:
    company, research_job, insights_job, dossier = _setup(db_session)
    handles = [
        _handle(
            db_session,
            company=company,
            research_job=research_job,
            wording="The company employs 430 people",
            index=index,
        )
        for index in range(2)
    ]
    insight = employee_size.derive_and_store(
        db_session,
        company_id=company.id,
        insights_job=insights_job,
        dossier=dossier,
        catalog=employee_size.research_evidence_catalog(
            db_session, research_job_id=research_job.id, company_id=company.id
        ),
        model_output={
            "candidates": [
                {"evidence_handles": [str(handle)], "observation_context": "current"}
                for handle in handles
            ]
        },
        actor="test",
    )
    payload = insight.structured_payload or {}
    assert payload["status"] == "supported"
    assert payload["exact_count"] == 430
    assert payload["normalized_band"] == "251_500"


def test_new_current_source_supersedes_historical_without_deleting_it(
    db_session: Session,
) -> None:
    company, research_job, insights_job, dossier = _setup(db_session)
    old = _handle(
        db_session,
        company=company,
        research_job=research_job,
        wording="In 2022 the company employed 750 people",
        index=0,
        observed_at=datetime.now(UTC) - timedelta(days=800),
    )
    new = _handle(
        db_session,
        company=company,
        research_job=research_job,
        wording="The company employs 1,200 people",
        index=1,
    )
    insight = employee_size.derive_and_store(
        db_session,
        company_id=company.id,
        insights_job=insights_job,
        dossier=dossier,
        catalog=employee_size.research_evidence_catalog(
            db_session, research_job_id=research_job.id, company_id=company.id
        ),
        model_output={
            "candidates": [
                {"evidence_handles": [str(old)], "observation_context": "historical"},
                {"evidence_handles": [str(new)], "observation_context": "current"},
            ]
        },
        actor="test",
    )
    payload = insight.structured_payload or {}
    assert payload["status"] == "supported"
    assert payload["exact_count"] == 1_200
    assert len(payload["observations"]) == 2


def test_stale_only_and_invalid_handle_never_become_eligible(db_session: Session) -> None:
    stale = _derive(
        db_session,
        wording="In 2022 the company employed 75 people",
        context="historical",
        observed_at=datetime.now(UTC) - timedelta(days=800),
    )
    assert (stale.structured_payload or {})["status"] == "stale"
    assert employee_size.downstream_eligible(stale)[0] is False

    company, research_job, insights_job, dossier = _setup(db_session)
    valid = _handle(
        db_session,
        company=company,
        research_job=research_job,
        wording="The company employs 75 people",
        index=0,
    )
    unavailable = employee_size.derive_and_store(
        db_session,
        company_id=company.id,
        insights_job=insights_job,
        dossier=dossier,
        catalog=employee_size.research_evidence_catalog(
            db_session, research_job_id=research_job.id, company_id=company.id
        ),
        model_output={
            "candidates": [
                {
                    "evidence_handles": [str(valid), str(uuid.uuid4())],
                    "observation_context": "current",
                }
            ]
        },
        actor="test",
    )
    assert (unavailable.structured_payload or {})["status"] == "unavailable"
    assert employee_size.downstream_eligible(unavailable)[0] is False


def test_history_is_append_only_and_only_latest_projection_is_eligible(
    db_session: Session,
) -> None:
    company, research_job, first_job, dossier = _setup(db_session)
    handle = _handle(
        db_session,
        company=company,
        research_job=research_job,
        wording="The company employs 75 people",
        index=0,
    )
    catalog = employee_size.research_evidence_catalog(
        db_session, research_job_id=research_job.id, company_id=company.id
    )
    output = {"candidates": [{"evidence_handles": [str(handle)], "observation_context": "current"}]}
    first = employee_size.derive_and_store(
        db_session,
        company_id=company.id,
        insights_job=first_job,
        dossier=dossier,
        catalog=catalog,
        model_output=output,
        actor="test",
        now=datetime.now(UTC) - timedelta(seconds=1),
    )
    second_job = _job(db_session, AgentIdentifier.INSIGHTS)
    second = employee_size.derive_and_store(
        db_session,
        company_id=company.id,
        insights_job=second_job,
        dossier=dossier,
        catalog=catalog,
        model_output=output,
        actor="test",
        now=datetime.now(UTC),
    )
    current = employee_size.current_derivation(db_session, company_id=company.id)
    assert current is not None and current.id == second.id
    assert first.id != second.id
    assert is_personalization_eligible(db_session, insight=first) is False
    assert is_personalization_eligible(db_session, insight=second) is True
    assert (
        len(
            list(
                db_session.scalars(
                    select(Insight).where(
                        Insight.company_id == company.id,
                        Insight.insight_type == employee_size.EMPLOYEE_SIZE_TYPE,
                    )
                )
            )
        )
        == 2
    )

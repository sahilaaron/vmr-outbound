"""Durable, exact-job Insights Agent Studio report contracts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.company_dossier import CompanyDossierVersion
from app.models.contact import Contact
from app.models.enums import (
    AgentIdentifier,
    AgentJobStatus,
    CampaignStatus,
    InsightKind,
    InsightState,
)
from app.models.insight import Insight, InsightEvidence
from app.models.verification_job import AgentJob
from app.services.agent_studio.insights_report import (
    DurableInsightsReportReader,
    InsightsReportState,
)
from app.services.companies import dossiers
from app.services.insights import employee_size
from app.services.insights.evidence import EvidenceInput, create_insight
from app.services.personalization import policy as personalization_policy
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def _job(
    *,
    agent: AgentIdentifier,
    campaign: Campaign,
    membership: CampaignContact,
    contact: Contact,
    parent: AgentJob | None = None,
    status: AgentJobStatus = AgentJobStatus.SUCCEEDED,
) -> AgentJob:
    now = datetime.now(UTC)
    return AgentJob(
        agent_id=agent,
        idempotency_key=f"pipeline:{membership.id}:{agent.value}:v{uuid.uuid4().int}",
        task_kind="advance_campaign_contact",
        campaign_id=campaign.id,
        campaign_contact_id=membership.id,
        contact_id=contact.id,
        company_id=contact.company_id,
        parent_job_id=parent.id if parent else None,
        status=status,
        attempts=2,
        max_attempts=3,
        input_reference={},
        created_at=now - timedelta(minutes=1),
        updated_at=now,
        next_run_at=now,
        started_at=now - timedelta(seconds=30),
        finished_at=now if status in {AgentJobStatus.SUCCEEDED, AgentJobStatus.FAILED} else None,
    )


def _subject(db: Session) -> tuple[Campaign, Company, Contact, CampaignContact]:
    campaign = Campaign(
        name=f"Insights report {uuid.uuid4()}",
        status=CampaignStatus.ACTIVE,
        execution_enabled=True,
    )
    company = Company(name="Kiln Systems", domain=f"{uuid.uuid4()}.example")
    db.add_all([campaign, company])
    db.flush()
    contact = Contact(
        first_name="Ada",
        last_name="Lovelace",
        company_name=company.name,
        company_domain=company.domain,
        company_id=company.id,
        natural_key=f"ada|lovelace|{uuid.uuid4()}",
    )
    db.add(contact)
    db.flush()
    membership = CampaignContact(campaign_id=campaign.id, contact_id=contact.id)
    db.add(membership)
    db.flush()
    return campaign, company, contact, membership


def _complete(
    db: Session, *, evidence_confidences: tuple[float, ...] = (0.9,)
) -> tuple[AgentJob, uuid.UUID, uuid.UUID]:
    campaign, company, contact, membership = _subject(db)
    research = _job(
        agent=AgentIdentifier.RESEARCH,
        campaign=campaign,
        membership=membership,
        contact=contact,
    )
    db.add(research)
    db.flush()
    submission, _ = dossiers.submit(
        db,
        company=company,
        producer="research-agent",
        payload={"overview": {"summary": "The company employs 430 people."}},
        request_context={"agent_job_id": str(research.id)},
    )
    exact_dossier = dossiers.interpret(
        db,
        company=company,
        submission=submission,
        interpreter="research-agent",
        sections={"overview": {"summary": "The company employs 430 people."}},
    )
    research.result = {
        "company_id": str(company.id),
        "submission_id": str(submission.id),
        "dossier_version": exact_dossier.version_number,
    }
    insights = _job(
        agent=AgentIdentifier.INSIGHTS,
        campaign=campaign,
        membership=membership,
        contact=contact,
        parent=research,
    )
    insights.input_reference = {
        "research_job_id": str(research.id),
        "research_submission_id": str(submission.id),
        "research_dossier_version_id": str(exact_dossier.id),
    }
    db.add(insights)
    db.flush()
    claim = create_insight(
        db,
        claim="The company employs 430 people.",
        kind=InsightKind.FACT,
        state=InsightState.SUPPORTED,
        evidence=[
            EvidenceInput(
                source_url=(
                    "https://kiln.example/about?token=secret"
                    if index == 0
                    else f"https://kiln.example/about/{index}?token=secret"
                ),
                retrieved_at=datetime.now(UTC),
                evidence_summary="The company employs 430 people.",
                confidence=confidence,
                extraction_method="insights-test/v1",
            )
            for index, confidence in enumerate(evidence_confidences)
        ],
        company_id=company.id,
        idempotency_key=f"insights-agent:{insights.id}:0",
        producer_job_id=insights.id,
        dossier_version_id=exact_dossier.id,
        derivation_version="insights-test/v1",
    )
    insights.result = {
        "domain_outcome": "insights_recorded",
        "company_id": str(company.id),
        "research_job_id": str(research.id),
        "submission_id": str(submission.id),
        "dossier_version_id": str(exact_dossier.id),
        "dossier_version": exact_dossier.version_number,
        "insights": [{"insight_id": str(claim.id)}],
        "dropped": [{"index": 1, "reason": "unsourced"}],
    }
    db.flush()
    return insights, exact_dossier.id, claim.id


def test_complete_report_uses_exact_lineage_and_sanitized_evidence(db_session: Session) -> None:
    job, dossier_id, claim_id = _complete(db_session)
    report = DurableInsightsReportReader(db_session).read_job(job.id)
    assert report is not None
    assert report.report_state is InsightsReportState.COMPLETE
    assert report.research_dossier_id == dossier_id
    assert report.claims[0].insight_id == claim_id
    assert report.claims[0].evidence[0].source_url == "https://kiln.example/about"
    assert report.claims[0].downstream_eligible is True
    assert report.dropped_claims is not None
    assert "global claim ledger" in report.unavailable[2]


def test_report_uses_strongest_complete_support_for_personalization_policy(
    db_session: Session,
) -> None:
    personalization_policy.ensure_initial_policy(db_session, actor="test")
    job, _, claim_id = _complete(db_session, evidence_confidences=(0.93, 0.55))

    report = DurableInsightsReportReader(db_session).read_job(job.id)

    assert report is not None
    claim = next(item for item in report.claims if item.insight_id == claim_id)
    assert claim.confidence == 0.93
    assert claim.downstream_eligible is True


def test_later_current_dossier_is_not_substituted(db_session: Session) -> None:
    job, exact_id, _ = _complete(db_session)
    company = db_session.get(Company, job.company_id)
    assert company is not None
    later_submission, _ = dossiers.submit(
        db_session,
        company=company,
        producer="later-research",
        payload={"overview": {"summary": "later"}},
    )
    later = dossiers.interpret(
        db_session,
        company=company,
        submission=later_submission,
        interpreter="later-research",
        sections={"overview": {"summary": "later"}},
    )
    assert later.id != exact_id and later.is_current
    report = DurableInsightsReportReader(db_session).read_job(job.id)
    assert report is not None and report.research_dossier_id == exact_id


def test_report_exposes_structured_employee_size_and_eligibility(db_session: Session) -> None:
    job, dossier_id, _ = _complete(db_session)
    company = db_session.get(Company, job.company_id)
    dossier = db_session.get(CompanyDossierVersion, dossier_id)
    research_job_id = uuid.UUID(str(job.input_reference["research_job_id"]))
    research_job = db_session.get(AgentJob, research_job_id)
    assert company is not None and dossier is not None and research_job is not None
    research_fact = create_insight(
        db_session,
        claim="The company employs 430 people.",
        kind=InsightKind.FACT,
        state=InsightState.SUPPORTED,
        evidence=[
            EvidenceInput(
                source_url="https://kiln.example/workforce",
                retrieved_at=datetime.now(UTC),
                evidence_summary="The company employs 430 people.",
                confidence=0.9,
                extraction_method="research-test/v1",
            )
        ],
        company_id=company.id,
        idempotency_key=f"research:{research_job.id}:website:0",
    )
    handle = (
        db_session.scalars(
            select(InsightEvidence).where(InsightEvidence.insight_id == research_fact.id)
        )
        .one()
        .id
    )
    employee_size.derive_and_store(
        db_session,
        company_id=company.id,
        insights_job=job,
        dossier=dossier,
        catalog=employee_size.research_evidence_catalog(
            db_session, research_job_id=research_job.id, company_id=company.id
        ),
        model_output={
            "candidates": [
                {
                    "source_wording": "The company employs 430 people.",
                    "evidence_handles": [str(handle)],
                    "observation_context": "current",
                }
            ]
        },
        actor="test",
    )
    report = DurableInsightsReportReader(db_session).read_job(job.id)
    assert report is not None
    structured = next(claim for claim in report.claims if claim.employee_size is not None)
    assert structured.employee_size is not None
    assert structured.employee_size.exact_count == 430
    assert structured.employee_size.normalized_band == "251_500"
    assert structured.employee_size.status == "supported"
    assert structured.downstream_eligible is True


def test_partial_and_unavailable_states_are_deterministic(db_session: Session) -> None:
    job, _, claim_id = _complete(db_session)
    claim = db_session.get(Insight, claim_id)
    assert claim is not None
    claim.producer_job_id = None
    claim.idempotency_key = None
    job.status = AgentJobStatus.IN_PROGRESS
    job.finished_at = None
    db_session.flush()
    partial = DurableInsightsReportReader(db_session).read_job(job.id)
    assert partial is not None
    assert partial.report_state is InsightsReportState.PARTIAL

    campaign, _, contact, membership = _subject(db_session)
    missing = _job(
        agent=AgentIdentifier.INSIGHTS,
        campaign=campaign,
        membership=membership,
        contact=contact,
        status=AgentJobStatus.FAILED,
    )
    db_session.add(missing)
    db_session.flush()
    unavailable = DurableInsightsReportReader(db_session).read_job(missing.id)
    assert unavailable is not None
    assert unavailable.report_state is InsightsReportState.UNAVAILABLE


def test_wrong_unknown_and_cross_owner_jobs_are_safe_not_found(db_session: Session) -> None:
    job, _, _ = _complete(db_session)
    reader = DurableInsightsReportReader(db_session)
    assert reader.read_job(uuid.uuid4()) is None
    job.agent_id = AgentIdentifier.EMAIL
    db_session.flush()
    assert reader.read_job(job.id) is None

    job.agent_id = AgentIdentifier.INSIGHTS
    _, _, other_contact, _ = _subject(db_session)
    job.contact_id = other_contact.id
    db_session.flush()
    assert reader.read_job(job.id) is None


def test_report_read_performs_no_writes_or_job_actions(db_session: Session) -> None:
    job, _, _ = _complete(db_session)
    db_session.flush()
    before = db_session.scalar(select(func.count()).select_from(Insight))
    report = DurableInsightsReportReader(db_session).read_job(job.id)
    after = db_session.scalar(select(func.count()).select_from(Insight))
    assert report is not None
    assert before == after
    assert not db_session.new
    assert not db_session.dirty


def test_error_and_lease_are_sanitized_and_retries_are_not_generations(
    db_session: Session,
) -> None:
    job, _, _ = _complete(db_session)
    job.status = AgentJobStatus.FAILED
    job.error_class = "RuntimeError"
    job.last_error = "TOKEN=secret at /root/private/key.txt"
    job.lease_owner = "worker-1"
    job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=1)
    db_session.flush()
    report = DurableInsightsReportReader(db_session).read_job(job.id)
    assert report is not None
    assert "secret" not in (report.error_detail or "")
    assert "/root/" not in (report.error_detail or "")
    assert report.attempts == 2
    assert len(report.related_generations) == 1

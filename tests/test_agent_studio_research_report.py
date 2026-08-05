"""Durable, read-only Agent Studio Research report projection."""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta

from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.company_domain_resolution import CompanyDomainResolution
from app.models.company_dossier import CompanyDossierVersion, CompanyResearchSubmission
from app.models.contact import Contact
from app.models.enums import (
    AgentIdentifier,
    AgentJobStatus,
    CampaignStatus,
    DomainResolutionKind,
    DomainResolutionState,
    InsightKind,
    InsightState,
    PipelineEventType,
    PipelineStageStatus,
)
from app.models.insight import Insight, InsightEvidence
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.models.pipeline import CampaignContactAgentState, PipelineEvent
from app.models.verification_job import AgentJob
from app.services.agent_studio import research_report
from app.services.agent_studio.research_report import (
    DurableResearchReportReader,
    ResearchReportState,
)
from app.services.companies import dossiers
from app.services.insights.evidence import EvidenceInput, create_insight
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def _subject(
    db: Session,
) -> tuple[Campaign, Company, Contact, CampaignContact, LinkedInProfileSnapshot]:
    now = datetime.now(UTC)
    campaign = Campaign(
        name=f"Research report {uuid.uuid4()}",
        status=CampaignStatus.ACTIVE,
        execution_enabled=True,
    )
    company = Company(name="Kiln Systems", domain="kiln.example")
    db.add_all([campaign, company])
    db.flush()
    contact = Contact(
        first_name="Ada",
        last_name="Lovelace",
        email=f"ada-{uuid.uuid4()}@kiln.example",
        company_name=company.name,
        company_domain=company.domain,
        company_id=company.id,
        natural_key=f"ada|lovelace|{uuid.uuid4()}",
    )
    capture = LinkedInProfileSnapshot(
        client_capture_id=str(uuid.uuid4()),
        content_hash=uuid.uuid4().hex,
        schema_version="test/v1",
        source="test",
        source_url="https://www.linkedin.com/in/ada",
        normalized_profile_url="https://www.linkedin.com/in/ada",
        captured_at=now,
        extraction_status="complete",
        payload={},
        profile_fields={},
    )
    db.add_all([contact, capture])
    db.flush()
    membership = CampaignContact(
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_capture_id=capture.id,
    )
    db.add(membership)
    db.flush()
    decision = CompanyDomainResolution(
        capture_id=capture.id,
        resolved_company_id=company.id,
        decision_number=1,
        is_current=True,
        state=DomainResolutionState.CONFIRMED,
        decision_kind=DomainResolutionKind.AUTOMATIC,
        policy_version="domain-policy/v1",
        selected_domain=company.domain,
        reasons=["permanent_company_domain"],
        provider_call_made=False,
        decided_by="test",
        decided_at=now,
    )
    db.add(decision)
    db.flush()
    return campaign, company, contact, membership, capture


def _job(
    *,
    campaign: Campaign,
    contact: Contact,
    membership: CampaignContact,
    company: Company,
    capture: LinkedInProfileSnapshot,
    generation: int,
    created_at: datetime,
    status: AgentJobStatus = AgentJobStatus.SUCCEEDED,
    attempts: int = 1,
) -> AgentJob:
    return AgentJob(
        agent_id=AgentIdentifier.RESEARCH,
        task_kind="advance_campaign_contact",
        entity_type="campaign_contact",
        entity_id=membership.id,
        contact_id=contact.id,
        campaign_id=campaign.id,
        campaign_contact_id=membership.id,
        company_id=company.id,
        capture_id=capture.id,
        idempotency_key=f"pipeline:{membership.id}:research:v{generation}",
        status=status,
        attempts=attempts,
        max_attempts=3,
        next_run_at=created_at,
        input_reference={},
        created_at=created_at,
        updated_at=created_at,
        started_at=created_at + timedelta(seconds=1),
        finished_at=(created_at + timedelta(seconds=4))
        if status in {AgentJobStatus.SUCCEEDED, AgentJobStatus.FAILED}
        else None,
    )


def _complete_report(db: Session) -> tuple[CampaignContact, AgentJob, AgentJob, uuid.UUID]:
    campaign, company, contact, membership, capture = _subject(db)
    now = datetime.now(UTC)
    first_job = _job(
        campaign=campaign,
        contact=contact,
        membership=membership,
        company=company,
        capture=capture,
        generation=1,
        created_at=now - timedelta(hours=1),
    )
    second_job = _job(
        campaign=campaign,
        contact=contact,
        membership=membership,
        company=company,
        capture=capture,
        generation=2,
        created_at=now,
        attempts=2,
    )
    db.add_all([first_job, second_job])
    db.flush()

    payload = {
        "domain": "kiln.example",
        "company_id": str(company.id),
        "workers": [
            {
                "worker": "website",
                "worker_version": "1",
                "raw": {
                    "pages": [
                        {
                            "url": (
                                "https://collector:password@kiln.example/about"
                                "?token=sk_live_hidden#private"
                            ),
                            "title": "Kiln token=secret /workspace/private/data.json",
                            "page_type": "about",
                            "retrieval_method": "http",
                            "retrieved_at": now.isoformat(),
                        }
                    ],
                    "errors": [
                        {
                            "url": "https://kiln.example/careers?api_key=hidden",
                            "stage": "fetch",
                            "error": (
                                "Authorization: Bearer abcdefghijklmnop from "
                                "/root/worker/secrets.txt"
                            ),
                            "at": now.isoformat(),
                        }
                    ],
                },
            }
        ],
    }
    submission, created = dossiers.submit(
        db,
        company=company,
        producer="research-agent",
        producer_version="1",
        submitted_by="test",
        payload=payload,
        request_context={"agent_job_id": str(first_job.id)},
    )
    assert created
    first_dossier = dossiers.interpret(
        db,
        company=company,
        submission=submission,
        interpreter="research-agent",
        interpreter_version="1",
        sections={"overview": {"facts": []}},
        created_by="test",
    )
    # The second execution found the same raw bytes.  Submission deduplication
    # intentionally retains the first job's request context.
    reused, created_again = dossiers.submit(
        db,
        company=company,
        producer="research-agent",
        producer_version="1",
        submitted_by="test",
        payload=payload,
        request_context={"agent_job_id": str(second_job.id)},
    )
    assert not created_again
    assert reused.id == submission.id
    assert reused.request_context == {"agent_job_id": str(first_job.id)}
    second_dossier = dossiers.interpret(
        db,
        company=company,
        submission=reused,
        interpreter="research-agent",
        interpreter_version="2",
        sections={"overview": {"facts": ["updated"]}, "sources": []},
        warnings=[
            "token=warning /tmp/private.log",
            {"diagnostic": "opaque-unkeyed-value"},
        ],
        created_by="test",
    )
    latest_company_dossier = dossiers.interpret(
        db,
        company=company,
        submission=reused,
        interpreter="later-reinterpreter",
        interpreter_version="3",
        sections={"overview": {"facts": ["later interpretation"]}},
        created_by="test",
    )
    assert latest_company_dossier.version_number == 3
    first_job.result = {
        "domain": company.domain,
        "company_id": str(company.id),
        "submission_id": str(submission.id),
        "dossier_version": first_dossier.version_number,
        "domain_outcome": "researched the company website",
    }
    second_job.result = {
        "domain": company.domain,
        "company_id": str(company.id),
        "submission_id": str(submission.id),
        "dossier_version": second_dossier.version_number,
        "domain_outcome": "researched the company website",
    }

    insight = create_insight(
        db,
        claim="products: plant workflow software",
        kind=InsightKind.FACT,
        state=InsightState.SUPPORTED,
        evidence=[
            EvidenceInput(
                source_url=("https://reader:password@kiln.example/about?authorization=hidden"),
                source_title="Kiln Systems",
                retrieved_at=now,
                evidence_summary="Official company page.",
                confidence=0.92,
                extraction_method="website/v1",
                source_record_type="company_research_submission",
                source_record_id=submission.id,
            )
        ],
        company_id=company.id,
        idempotency_key=f"research:{second_job.id}:website:0",
        actor="test",
    )
    db.add_all(
        [
            PipelineEvent(
                campaign_contact_id=membership.id,
                agent_id=AgentIdentifier.RESEARCH,
                job_id=second_job.id,
                event_type=PipelineEventType.JOB_LEASED,
                from_status=PipelineStageStatus.WAITING,
                to_status=PipelineStageStatus.WAITING,
                reason_code="worker_claim",
                retryable=False,
                detail={"worker_id": "research-host:123", "attempt": 2},
                actor="research-host:123",
                occurred_at=now + timedelta(seconds=1),
            ),
            PipelineEvent(
                campaign_contact_id=membership.id,
                agent_id=AgentIdentifier.RESEARCH,
                job_id=second_job.id,
                event_type=PipelineEventType.JOB_STARTED,
                from_status=PipelineStageStatus.WAITING,
                to_status=PipelineStageStatus.RUNNING,
                reason_code="worker_started",
                retryable=False,
                detail={"worker_id": "research-host:123", "attempt": 2},
                actor="research-host:123",
                occurred_at=now + timedelta(seconds=2),
            ),
            CampaignContactAgentState(
                campaign_contact_id=membership.id,
                agent_id=AgentIdentifier.RESEARCH,
                status=PipelineStageStatus.COMPLETED,
                attempt_count=2,
                latest_job_id=second_job.id,
                output_reference={"submission_id": str(submission.id)},
                completed_at=now + timedelta(seconds=4),
            ),
        ]
    )
    db.flush()
    return membership, first_job, second_job, insight.id


def test_complete_report_uses_exact_committed_artifacts_and_is_read_only(
    db_session: Session,
) -> None:
    membership, first_job, second_job, insight_id = _complete_report(db_session)
    models = (
        AgentJob,
        CompanyResearchSubmission,
        CompanyDossierVersion,
        Insight,
        InsightEvidence,
        PipelineEvent,
    )
    before = {model: db_session.scalar(select(func.count()).select_from(model)) for model in models}
    reader = DurableResearchReportReader(db_session)

    report = reader.read(membership.id)
    repeated = reader.read_job(second_job.id)

    assert report is not None
    assert repeated is not None
    assert repeated.job_id == report.job_id
    assert repeated.submission == report.submission
    assert repeated.dossier == report.dossier
    assert report.report_state is ResearchReportState.COMPLETE
    assert report.job_id == second_job.id
    assert report.submission is not None
    assert report.submission.link_source == "job result"
    assert report.dossier is not None
    assert report.dossier.version_number == 2
    assert report.dossier.status == "superseded"
    assert report.domain_state == "confirmed"
    assert report.domain_resolution is not None
    assert report.domain_resolution.scope == "current decision for execution capture"
    assert report.worker_identity == ("website/1",)
    assert report.execution_workers == ("research-host:123",)
    assert report.attempts == 2
    assert [item.generation for item in report.retry_history] == [2, 1]
    assert [item.job_id for item in report.retry_history] == [second_job.id, first_job.id]
    assert report.sourced_facts is not None
    assert report.sourced_facts[0].insight_id == insight_id
    assert report.sourced_facts[0].evidence[0].source_record_id == report.submission_id
    assert report.urls_attempted == (
        "https://kiln.example/about",
        "https://kiln.example/careers",
    )
    assert report.successful_reads is not None
    assert report.successful_reads[0].url == "https://kiln.example/about"
    assert report.collection_failure_details is not None
    assert report.collection_failure_details[0].url == "https://kiln.example/careers"
    assert report.rejected_evidence is None
    rendered = repr(report)
    for secret in (
        "sk_live_hidden",
        "password",
        "authorization=hidden",
        "abcdefghi",
        "/workspace/private",
        "/root/worker",
        "/tmp/private",
        "opaque-unkeyed-value",
    ):
        assert secret not in rendered
    assert "[redacted]" in rendered
    assert "[local path]" in rendered
    after = {model: db_session.scalar(select(func.count()).select_from(model)) for model in models}
    assert before == after
    pending_unrelated = Company(name="Must not be flushed by report loading")
    db_session.add(pending_unrelated)
    reader.read(membership.id)
    assert pending_unrelated in db_session.new
    assert pending_unrelated.id is None
    db_session.expunge(pending_unrelated)
    assert not db_session.new
    assert not db_session.dirty


def test_partial_and_unavailable_reports_are_explicit_and_errors_are_sanitized(
    db_session: Session,
) -> None:
    campaign, company, contact, membership, capture = _subject(db_session)
    reader = DurableResearchReportReader(db_session)

    unavailable = reader.read(membership.id)
    assert unavailable is not None
    assert unavailable.report_state is ResearchReportState.UNAVAILABLE
    assert unavailable.job_id is None

    now = datetime.now(UTC)
    failed = _job(
        campaign=campaign,
        contact=contact,
        membership=membership,
        company=company,
        capture=capture,
        generation=1,
        created_at=now,
        status=AgentJobStatus.FAILED,
    )
    failed.error_class = "provider_token=supersecret"
    failed.last_error = (
        "GET https://user:password@kiln.example/?api_key=supersecret failed "
        "from C:\\Users\\operator\\worker.py and /home/operator/.env "
        "AWS_SECRET_ACCESS_KEY=environment-secret"
    )
    failed.error = {
        "class": failed.error_class,
        "message": failed.last_error,
        "retryable": False,
        "authorization": "Bearer never-render-this",
    }
    db_session.add(failed)
    db_session.flush()

    partial = reader.read_job(failed.id)
    assert partial is not None
    assert partial.report_state is ResearchReportState.PARTIAL
    assert partial.retryable_error is False
    assert partial.submission is None
    assert "[redacted]" in (partial.error_type or "")
    assert "[local path]" in (partial.error_detail or "")
    rendered = repr(partial)
    assert "supersecret" not in rendered
    assert "password" not in rendered
    assert "never-render-this" not in rendered
    assert "environment-secret" not in rendered
    assert "operator/worker.py" not in rendered


def test_unknown_non_research_and_cross_owner_jobs_are_not_reports(db_session: Session) -> None:
    campaign, company, contact, membership, capture = _subject(db_session)
    reader = DurableResearchReportReader(db_session)
    now = datetime.now(UTC)
    verification = AgentJob(
        agent_id=AgentIdentifier.VERIFICATION,
        task_kind="verify_exact_email",
        entity_type="campaign_contact",
        entity_id=membership.id,
        campaign_id=campaign.id,
        campaign_contact_id=membership.id,
        contact_id=contact.id,
        idempotency_key=f"verification:{uuid.uuid4()}",
        status=AgentJobStatus.PENDING,
        next_run_at=now,
        input_reference={},
    )
    other_campaign = Campaign(name=f"Other {uuid.uuid4()}")
    db_session.add_all([verification, other_campaign])
    db_session.flush()
    mismatched = _job(
        campaign=other_campaign,
        contact=contact,
        membership=membership,
        company=company,
        capture=capture,
        generation=1,
        created_at=now,
    )
    db_session.add(mismatched)
    db_session.flush()

    assert reader.read_job(uuid.uuid4()) is None
    assert reader.read_job(verification.id) is None
    assert reader.read_job(mismatched.id) is None
    membership_report = reader.read(membership.id)
    assert membership_report is not None
    assert membership_report.report_state is ResearchReportState.UNAVAILABLE


def test_cross_company_submission_is_withheld(db_session: Session) -> None:
    campaign, company, contact, membership, capture = _subject(db_session)
    other_company = Company(name="Other Company", domain="other.example")
    db_session.add(other_company)
    db_session.flush()
    submission, _created = dossiers.submit(
        db_session,
        company=other_company,
        producer="research-agent",
        payload={"workers": []},
        request_context=None,
    )
    now = datetime.now(UTC)
    job = _job(
        campaign=campaign,
        contact=contact,
        membership=membership,
        company=company,
        capture=capture,
        generation=1,
        created_at=now,
    )
    job.result = {
        "company_id": str(company.id),
        "submission_id": str(submission.id),
        "dossier_version": 1,
    }
    db_session.add(job)
    db_session.flush()

    report = DurableResearchReportReader(db_session).read_job(job.id)

    assert report is not None
    assert report.report_state is ResearchReportState.PARTIAL
    assert report.company_id == company.id
    assert report.submission is None
    assert report.submission_id is None
    assert other_company.name not in repr(report)
    assert any("Company ownership" in item for item in report.unavailable)


def test_agent_studio_has_one_durable_research_report_service() -> None:
    source = inspect.getsource(research_report)
    assert "class DurableResearchReportReader" in source
    assert "class PersistedResearchReportReader" not in source
    assert not hasattr(research_report, "PersistedResearchReportReader")


# --- RES-002 fallback lineage -------------------------------------------------


def _lineage_job(db: Session) -> tuple[Company, AgentJob]:
    campaign, company, contact, membership, capture = _subject(db)
    job = _job(
        campaign=campaign,
        contact=contact,
        membership=membership,
        company=company,
        capture=capture,
        generation=1,
        created_at=datetime.now(UTC),
    )
    db.add(job)
    db.flush()
    return company, job


def test_the_report_shows_both_attempts_and_sanitizes_what_it_shows(
    db_session: Session,
) -> None:
    """The operator must be able to read the whole execution truth off one page.

    Which worker was tried, why its output was rejected, whether the Claude
    fallback ran, what it produced, and which sources it cited. Every string
    below arrives from a subprocess boundary and is treated accordingly: local
    paths, credentials in URLs and query strings are removed on the way out,
    while the shape of the failure survives so it stays diagnosable.
    """

    company, job = _lineage_job(db_session)
    job.result = {
        "domain": company.domain,
        "company_id": str(company.id),
        "domain_outcome": "researched the company through cited public web sources",
        "dossier_basis": "claude_cli_fallback",
        "deterministic": {
            "workers": ["website"],
            "usable": False,
            "reason_code": "deterministic_worker_failed",
            "reason": "the deterministic research worker(s) returned no result (site_unreachable)",
            "failures": [
                {
                    "worker": "website",
                    "reason_code": "site_unreachable",
                    "retryable": False,
                    "reason": "homepage unreachable, launched from /root/worker/run.py",
                }
            ],
        },
        "fallback": {
            "attempted": True,
            "status": "succeeded",
            "trigger_reason_code": "deterministic_worker_failed",
            "trigger_reason": "the deterministic research worker(s) returned no result",
            "producer": "claude-cli",
            "producer_version": "research-claude-fallback/1",
            "evidence_accepted": 2,
            "claims_rejected": 3,
            "rejection_reasons": ["uncited", "missing_excerpt"],
            "source_urls": [
                "https://trade.example/kiln-systems?token=sk_live_abcdefgh",
                "ask the sales team",
            ],
            "error": None,
            "duration_seconds": 12.5,
            "reused_committed_attempt": False,
            "tools": ["WebSearch", "WebFetch"],
        },
    }
    db_session.flush()

    report = DurableResearchReportReader(db_session).read_job(job.id)
    assert report is not None
    assert report.dossier_basis == "claude_cli_fallback"

    assert report.deterministic is not None
    assert report.deterministic.workers == ("website",)
    assert report.deterministic.usable is False
    assert report.deterministic.reason_code == "deterministic_worker_failed"
    failure = report.deterministic.failures[0]
    assert failure.research_worker == "website"
    assert failure.stage == "site_unreachable"
    assert "/root/worker/run.py" not in failure.error
    assert "[local path]" in failure.error
    assert "homepage unreachable" in failure.error, "the failure must stay diagnosable"

    assert report.fallback is not None
    assert report.fallback.attempted is True
    assert report.fallback.status == "succeeded"
    assert report.fallback.producer_version == "research-claude-fallback/1"
    assert report.fallback.evidence_accepted == 2
    assert report.fallback.claims_rejected == 3
    assert report.fallback.rejection_reasons == ("uncited", "missing_excerpt")
    assert report.fallback.tools == ("WebSearch", "WebFetch")
    # The credential-bearing query string is gone; the page it cites remains.
    assert report.fallback.source_urls == ("https://trade.example/kiln-systems",)
    assert "sk_live_abcdefgh" not in repr(report)


def test_a_failed_execution_still_reports_its_fallback_error(db_session: Session) -> None:
    """A run that produced nothing is where the lineage matters most.

    Read from the stored error detail rather than the result, because a job that
    ended terminally has no result — and "we tried the fallback and this is what
    it said" is precisely the thing an operator opens this page for.
    """

    _company, job = _lineage_job(db_session)
    job.status = AgentJobStatus.FAILED
    job.error_class = "thinking_unavailable"
    job.last_error = "The Claude CLI executable was not found on PATH."
    job.error = {
        "class": "thinking_unavailable",
        "message": "The Claude CLI executable was not found on PATH.",
        "retryable": False,
        "detail": {
            "reason_code": "all_workers_failed",
            "dossier_basis": "no_sourced_evidence",
            "deterministic": {
                "workers": ["website"],
                "usable": False,
                "reason_code": "deterministic_worker_failed",
                "reason": "the deterministic research worker(s) returned no result",
                "failures": [],
            },
            "fallback": {
                "attempted": True,
                "status": "failed",
                "producer_version": "research-claude-fallback/1",
                "error": "The Claude CLI at C:\\Users\\op\\bin\\claude.exe could not be executed.",
                "error_code": "thinking_unavailable",
                "retryable": False,
                "tools": ["WebSearch", "WebFetch"],
            },
        },
    }
    db_session.flush()

    report = DurableResearchReportReader(db_session).read_job(job.id)
    assert report is not None
    assert report.dossier_basis == "no_sourced_evidence"
    assert report.fallback is not None
    assert report.fallback.status == "failed"
    assert report.fallback.retryable is False
    assert report.fallback.error is not None
    assert "[local path]" in report.fallback.error
    assert "C:\\Users\\op\\bin" not in report.fallback.error
    assert report.deterministic is not None and report.deterministic.usable is False


def test_an_execution_predating_the_fallback_says_so_rather_than_guessing(
    db_session: Session,
) -> None:
    """An absent lineage is reported as absent, never defaulted into a claim."""

    company, job = _lineage_job(db_session)
    job.result = {
        "domain": company.domain,
        "company_id": str(company.id),
        "domain_outcome": "researched the company website",
    }
    db_session.flush()

    report = DurableResearchReportReader(db_session).read_job(job.id)
    assert report is not None
    assert report.deterministic is None
    assert report.fallback is None
    assert report.dossier_basis is None
    assert any("predates the Research fallback" in item for item in report.unavailable)

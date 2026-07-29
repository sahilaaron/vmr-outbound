"""Email parent → authoritative Verification child → Campaign pipeline."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_discovery import EmailCandidateAttempt, EmailCandidateAttemptStatus
from app.models.email_evidence import ExactEmailVerification
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    AgentJobStatus,
    CampaignStatus,
    CompanyFieldSource,
    EmailVerificationResult,
    PipelineEventType,
    PipelineStageStatus,
    ResearchState,
)
from app.models.pipeline import PipelineEvent
from app.models.verification_job import AgentJob
from app.services import campaign_contacts, pipeline
from app.services.agents import controls
from app.services.agents.adapters import (
    DEFAULT_ADAPTERS,
    AgentAdapter,
    VerificationAgentAdapter,
)
from app.services.agents.orchestrator import reconcile_agent_control, run_next
from app.services.companies import provenance as company_provenance
from app.services.verification.decisions import VerificationDecision
from app.services.verification.provider import ProviderResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
WORKER = "email-integration-worker"


class ScriptedLiveProvider:
    """Non-network provider seam used by the real Verification adapter."""

    name = "millionverifier"
    simulated = False

    def __init__(self, results: list[str]) -> None:
        self.results = list(results)
        self.calls: list[str] = []

    def verify(self, email: str) -> ProviderResponse:
        self.calls.append(email)
        result = self.results.pop(0)
        code = {"ok": 1, "invalid": 6}[result]
        return ProviderResponse(
            email=email,
            result=result,
            resultcode=code,
            credits=100,
            raw={"email": email, "result": result, "resultcode": code},
            livemode=True,
        )


def _adapters(provider: ScriptedLiveProvider) -> dict[AgentIdentifier, AgentAdapter]:
    adapters = dict(DEFAULT_ADAPTERS)
    adapters[AgentIdentifier.VERIFICATION] = VerificationAgentAdapter(
        provider_factory=lambda _settings: provider
    )
    return adapters


def _records(session: Session) -> tuple[Campaign, Company, Contact]:
    company = Company(
        name="Analytical Engines",
        domain="engines.example",
        research_state=ResearchState.COMPLETED,
    )
    campaign = Campaign(
        name=f"Email integration {uuid.uuid4()}",
        status=CampaignStatus.ACTIVE,
        execution_enabled=True,
    )
    session.add_all([company, campaign])
    session.flush()
    company_provenance.record_observation(
        session,
        company=company,
        field_name="company_size",
        value="51",
        source_kind=CompanyFieldSource.IMPORT,
        source_reference=f"integration-company-size:{company.id}",
        observed_at=NOW,
        created_by="test",
    )
    company_provenance.reconcile_field(
        session,
        company=company,
        field_name="company_size",
        actor="test",
    )
    contact = Contact(
        first_name="Ada",
        last_name="Lovelace",
        company_name=company.name,
        company_domain=company.domain,
        company_id=company.id,
        natural_key=f"ada|lovelace|{uuid.uuid4()}",
    )
    session.add(contact)
    session.flush()
    return campaign, company, contact


def _enrol_to_email(
    db_session: Session,
) -> tuple[Campaign, Contact, CampaignContact]:
    campaign, _, contact = _records(db_session)
    controls.set_global_control(
        db_session,
        agent_id=AgentIdentifier.EMAIL,
        status=AgentControlStatus.ENABLED,
    )
    controls.set_global_control(
        db_session,
        agent_id=AgentIdentifier.VERIFICATION,
        status=AgentControlStatus.ENABLED,
        config={"live": True},
    )
    enrolled = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        source_reference="email-integration",
        desired_stage=AgentIdentifier.VERIFICATION,
        enqueue=True,
    )
    membership = enrolled.membership

    # Identity and Company use their real adapters. Research is intentionally
    # outside this vertical path and is skipped through the shared contract.
    run_next(db_session, worker_id=WORKER)
    run_next(db_session, worker_id=WORKER)
    pipeline.skip_current_stage(
        db_session,
        membership=membership,
        agent_id=AgentIdentifier.RESEARCH,
        reason="Email integration test supplies sourced company evidence directly.",
        actor="test",
    )
    return campaign, contact, membership


def test_campaign_email_agent_tries_one_child_at_a_time_and_accepts_second(
    db_session: Session,
) -> None:
    _, contact, membership = _enrol_to_email(db_session)

    provider = ScriptedLiveProvider(["invalid", "ok"])
    adapters = _adapters(provider)

    # Parent queues candidate 1 and yields. Child 1 definitively rejects. Parent
    # resumes and queues candidate 2. Child 2 accepts. Only then does the parent
    # write Contact.email and complete Email + Verification projections.
    parent_wait = run_next(db_session, worker_id=WORKER, adapters=adapters)
    assert parent_wait.agent_id is AgentIdentifier.EMAIL
    assert parent_wait.public_status == "paused"
    assert parent_wait.job is not None
    parent_job = parent_wait.job

    first_child = run_next(db_session, worker_id=WORKER, adapters=adapters)
    assert first_child.agent_id is AgentIdentifier.VERIFICATION
    assert first_child.job is not None
    assert first_child.job.status is AgentJobStatus.PAUSED
    assert first_child.job.error is not None
    assert first_child.job.error["detail"]["decision"] == (
        VerificationDecision.TRY_NEXT_CANDIDATE.value
    )

    second_wait = run_next(db_session, worker_id=WORKER, adapters=adapters)
    assert second_wait.agent_id is AgentIdentifier.EMAIL
    assert second_wait.public_status == "paused"

    second_child = run_next(db_session, worker_id=WORKER, adapters=adapters)
    assert second_child.agent_id is AgentIdentifier.VERIFICATION
    assert second_child.public_status == "completed"
    assert second_child.job is not None
    assert second_child.job.parent_job_id == parent_job.id
    assert second_child.job.result is not None
    assert second_child.job.result["decision"] == VerificationDecision.ACCEPT.value

    accepted = run_next(db_session, worker_id=WORKER, adapters=adapters)
    assert accepted.agent_id is AgentIdentifier.EMAIL
    assert accepted.public_status == "completed"
    assert accepted.job is not None
    assert contact.email == "alovelace@engines.example"
    assert provider.calls == [
        "ada.lovelace@engines.example",
        "alovelace@engines.example",
    ]

    attempts = list(
        db_session.scalars(
            select(EmailCandidateAttempt)
            .where(EmailCandidateAttempt.email_job_id == accepted.job.id)
            .order_by(EmailCandidateAttempt.candidate_index)
        ).all()
    )
    assert [row.status for row in attempts] == [
        EmailCandidateAttemptStatus.REJECTED.value,
        EmailCandidateAttemptStatus.ACCEPTED.value,
    ]
    children = list(
        db_session.scalars(
            select(AgentJob)
            .where(AgentJob.parent_job_id == accepted.job.id)
            .order_by(AgentJob.created_at)
        ).all()
    )
    assert len(children) == 2
    assert all(child.agent_id is AgentIdentifier.VERIFICATION for child in children)

    email_state = pipeline.agent_state(
        db_session,
        campaign_contact_id=membership.id,
        agent_id=AgentIdentifier.EMAIL,
        create=False,
    )
    verification_state = pipeline.agent_state(
        db_session,
        campaign_contact_id=membership.id,
        agent_id=AgentIdentifier.VERIFICATION,
        create=False,
    )
    assert email_state is not None
    assert email_state.status is PipelineStageStatus.COMPLETED
    assert verification_state is not None
    assert verification_state.status is PipelineStageStatus.COMPLETED
    assert verification_state.output_reference is not None
    assert verification_state.output_reference["decision"] == VerificationDecision.ACCEPT.value
    assert membership.pipeline_status is PipelineStageStatus.COMPLETED
    assert membership.next_stage is None

    events = list(
        db_session.scalars(
            select(PipelineEvent)
            .where(PipelineEvent.campaign_contact_id == membership.id)
            .order_by(PipelineEvent.occurred_at, PipelineEvent.id)
        ).all()
    )
    assert any(
        event.event_type is PipelineEventType.STAGE_WAITING
        and event.agent_id is AgentIdentifier.EMAIL
        and event.reason_code == "waiting_on_verification"
        for event in events
    )
    assert any(
        event.event_type is PipelineEventType.STAGE_COMPLETED
        and event.agent_id is AgentIdentifier.VERIFICATION
        and event.detail.get("decision") == VerificationDecision.ACCEPT.value
        for event in events
    )

    evidence = db_session.get(ExactEmailVerification, attempts[1].verification_id)
    # The accepted attempt points to exact live evidence; generated text alone
    # never writes the permanent Contact.
    assert attempts[1].verification_id is not None
    assert evidence is not None
    assert evidence.result is EmailVerificationResult.VALID


def test_campaign_email_disablement_while_child_waits_prevents_provider_work(
    db_session: Session,
) -> None:
    campaign, contact, membership = _enrol_to_email(db_session)
    provider = ScriptedLiveProvider(["ok"])
    adapters = _adapters(provider)

    parent_wait = run_next(db_session, worker_id=WORKER, adapters=adapters)
    assert parent_wait.agent_id is AgentIdentifier.EMAIL
    assert parent_wait.job is not None
    assert parent_wait.job.status is AgentJobStatus.PAUSED

    controls.set_campaign_override(
        db_session,
        campaign_id=campaign.id,
        agent_id=AgentIdentifier.EMAIL,
        status=AgentControlStatus.DISABLED,
        reason="campaign Email stop",
    )
    reconcile_agent_control(
        db_session,
        agent_id=AgentIdentifier.EMAIL,
        campaign_id=campaign.id,
        actor="test",
    )

    blocked_child = run_next(db_session, worker_id=WORKER, adapters=adapters)
    assert blocked_child.agent_id is AgentIdentifier.VERIFICATION
    assert blocked_child.job is not None
    assert blocked_child.job.status is AgentJobStatus.PAUSED
    assert blocked_child.job.error_class == "requesting_email_agent_disabled"
    assert provider.calls == []
    assert contact.email is None
    assert parent_wait.job.result is not None
    assert parent_wait.job.result["domain_outcome"] == "campaign_override_disabled"
    assert parent_wait.job.result["control_source"] == "campaign_override"
    email_state = pipeline.agent_state(
        db_session,
        campaign_contact_id=membership.id,
        agent_id=AgentIdentifier.EMAIL,
        create=False,
    )
    assert email_state is not None
    assert email_state.status is PipelineStageStatus.DISABLED

    controls.set_campaign_override(
        db_session,
        campaign_id=campaign.id,
        agent_id=AgentIdentifier.EMAIL,
        status=AgentControlStatus.ENABLED,
    )
    reconcile_agent_control(
        db_session,
        agent_id=AgentIdentifier.EMAIL,
        campaign_id=campaign.id,
        actor="test",
    )

    resumed_parent = run_next(db_session, worker_id=WORKER, adapters=adapters)
    assert resumed_parent.agent_id is AgentIdentifier.EMAIL
    assert resumed_parent.public_status == "paused"
    assert blocked_child.job.status is AgentJobStatus.PENDING

    accepted_child = run_next(db_session, worker_id=WORKER, adapters=adapters)
    assert accepted_child.agent_id is AgentIdentifier.VERIFICATION
    assert accepted_child.public_status == "completed"
    assert provider.calls == ["ada.lovelace@engines.example"]

    accepted_parent = run_next(db_session, worker_id=WORKER, adapters=adapters)
    assert accepted_parent.agent_id is AgentIdentifier.EMAIL
    assert accepted_parent.public_status == "completed"
    assert contact.email == "ada.lovelace@engines.example"


def test_global_email_pause_prevents_candidate_and_child_creation(
    db_session: Session,
) -> None:
    _, contact, membership = _enrol_to_email(db_session)
    controls.set_global_control(
        db_session,
        agent_id=AgentIdentifier.EMAIL,
        status=AgentControlStatus.PAUSED,
        reason="global Email maintenance",
    )
    reconcile_agent_control(
        db_session,
        agent_id=AgentIdentifier.EMAIL,
        actor="test",
    )

    parent = db_session.scalars(
        select(AgentJob).where(
            AgentJob.campaign_contact_id == membership.id,
            AgentJob.agent_id == AgentIdentifier.EMAIL,
        )
    ).one()
    assert parent.status is AgentJobStatus.PAUSED
    assert parent.result is not None
    assert parent.result["domain_outcome"] == "agent_paused"
    assert contact.email is None
    assert (
        db_session.scalars(
            select(AgentJob).where(
                AgentJob.parent_job_id == parent.id,
                AgentJob.agent_id == AgentIdentifier.VERIFICATION,
            )
        ).all()
        == []
    )
    state = pipeline.agent_state(
        db_session,
        campaign_contact_id=membership.id,
        agent_id=AgentIdentifier.EMAIL,
        create=False,
    )
    assert state is not None
    assert state.status is PipelineStageStatus.PAUSED

"""Workbench projection of the integrated Email Agent contract."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.models.campaign import CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_candidate import EmailCandidate
from app.models.email_discovery import EmailCandidateAttempt, EmailCandidateAttemptStatus
from app.models.enums import (
    AgentIdentifier,
    EmailCandidateSource,
    PipelineEventType,
    PipelineStageStatus,
)
from app.models.verification_job import AgentJob
from app.services.email.agent import STATE_KEY, EmailExecutionOutcome, enqueue_email_job
from app.services.email.discovery_policy import POLICY_IDENTIFIER, POLICY_VERSION
from app.services.pipeline import transition_stage
from app.services.workbench_agents.reader import PhaseTwoWorkbenchReader
from sqlalchemy.orm import Session

from tests.workbench_scenario import Scenario, build

NOW = datetime(2026, 7, 29, 13, 0, tzinfo=UTC)


def _email_execution(
    db: Session,
) -> tuple[Scenario, CampaignContact, Contact, Company, AgentJob]:
    scenario = build(db)
    membership = scenario.membership("healthy")
    contact = scenario.contacts["healthy"]
    company = Company(name="Northwind Email", domain="northwind.example.com")
    db.add(company)
    db.flush()
    contact.company_id = company.id
    contact.company_domain = company.domain
    job, created = enqueue_email_job(
        db,
        contact_id=contact.id,
        company_id=company.id,
        campaign_id=scenario.campaign.id,
        campaign_contact_id=membership.id,
        idempotency_scope="workbench-email-projection",
    )
    assert created is True
    return scenario, membership, contact, company, job


def _candidate(db: Session, *, contact_id: uuid.UUID, email: str, index: int) -> EmailCandidate:
    row = EmailCandidate(
        contact_id=contact_id,
        email=email,
        source=EmailCandidateSource.GENERATED,
        pattern="first.last",
        local_part=email.split("@", 1)[0],
        domain=email.split("@", 1)[1],
        engine_version=POLICY_VERSION,
        rank=index,
        rank_score=float(index),
        rank_reason=f"Email policy position {index + 1}",
        selected=index == 0,
        selection_reason="accepted by authoritative Verification" if index == 0 else None,
    )
    db.add(row)
    db.flush()
    return row


def _state(*, terminal_outcome: str | None, accepted_email: str | None = None) -> dict[str, object]:
    return {
        "policy_identifier": POLICY_IDENTIFIER,
        "policy_version": POLICY_VERSION,
        "policy_outcome": "ready",
        "normalization_version": "email-normalization-v1",
        "employee_count_class": "more_than_50",
        "employee_evidence": {"freshness": "fresh"},
        "normalized_domain": "northwind.example.com",
        "ordered_candidate_formats": ["first.last", "flast", "lastf"],
        "candidates": [
            {
                "candidate_index": 0,
                "format": "first.last",
                "email": "alice.nakamura@northwind.example.com",
            },
            {"candidate_index": 1, "format": "flast", "email": "anakamura@northwind.example.com"},
            {"candidate_index": 2, "format": "lastf", "email": "nakamuraa@northwind.example.com"},
        ],
        "current_candidate_index": 0,
        "accepted_candidate_index": 0 if accepted_email else None,
        "accepted_email": accepted_email,
        "terminal_outcome": terminal_outcome,
        "reason": None,
        "force_refresh": False,
        "refresh_scope": None,
    }


def test_workbench_projects_accepted_email_and_candidate_attempt(db_session: Session) -> None:
    scenario, membership, contact, company, job = _email_execution(db_session)
    accepted = "alice.nakamura@northwind.example.com"
    candidate = _candidate(db_session, contact_id=contact.id, email=accepted, index=0)
    attempt = EmailCandidateAttempt(
        email_job_id=job.id,
        candidate_id=candidate.id,
        contact_id=contact.id,
        company_id=company.id,
        campaign_id=scenario.campaign.id,
        campaign_contact_id=membership.id,
        candidate_index=0,
        candidate_format="first.last",
        normalized_email=accepted,
        normalized_domain="northwind.example.com",
        policy_identifier=POLICY_IDENTIFIER,
        policy_version=POLICY_VERSION,
        employee_count_class="more_than_50",
        employee_evidence_freshness="fresh",
        status=EmailCandidateAttemptStatus.ACCEPTED.value,
        verification_decision="accept",
        verification_result={"decision": "accept"},
        resolved_at=NOW,
    )
    db_session.add(attempt)
    contact.email = accepted
    job.result = {
        STATE_KEY: _state(
            terminal_outcome=EmailExecutionOutcome.VERIFIED_EMAIL_ACCEPTED.value,
            accepted_email=accepted,
        ),
        "domain_outcome": EmailExecutionOutcome.VERIFIED_EMAIL_ACCEPTED.value,
        "email": accepted,
        "email_policy_identifier": POLICY_IDENTIFIER,
        "email_policy_version": POLICY_VERSION,
        "verification_provider": "millionverifier",
        "verification_policy_version": "ver-1",
    }
    transition_stage(
        db_session,
        membership=membership,
        agent_id=AgentIdentifier.EMAIL,
        target=PipelineStageStatus.COMPLETED,
        event_type=PipelineEventType.STAGE_COMPLETED,
        actor="test",
        job=job,
        output_reference={"email": accepted, "candidate_attempt_id": str(attempt.id)},
    )

    execution = PhaseTwoWorkbenchReader(db_session).contact_execution(
        scenario.campaign.id, membership.id
    )

    assert execution is not None
    assert execution.email is not None
    assert execution.email.accepted is True
    assert execution.email.accepted_email == accepted
    assert execution.email.outcome_committed is True
    assert execution.email.attempts[0].verification_decision == "accept"
    assert execution.email.attempts[0].email == accepted


def test_workbench_projects_no_verified_address_without_inventing_acceptance(
    db_session: Session,
) -> None:
    scenario, membership, contact, company, job = _email_execution(db_session)
    addresses = [
        "alice.nakamura@northwind.example.com",
        "anakamura@northwind.example.com",
        "nakamuraa@northwind.example.com",
    ]
    for index, address in enumerate(addresses):
        candidate = _candidate(db_session, contact_id=contact.id, email=address, index=index)
        db_session.add(
            EmailCandidateAttempt(
                email_job_id=job.id,
                candidate_id=candidate.id,
                contact_id=contact.id,
                company_id=company.id,
                campaign_id=scenario.campaign.id,
                campaign_contact_id=membership.id,
                candidate_index=index,
                candidate_format=("first.last", "flast", "lastf")[index],
                normalized_email=address,
                normalized_domain="northwind.example.com",
                policy_identifier=POLICY_IDENTIFIER,
                policy_version=POLICY_VERSION,
                employee_count_class="more_than_50",
                employee_evidence_freshness="fresh",
                status=EmailCandidateAttemptStatus.REJECTED.value,
                verification_decision="try_next_candidate",
                resolved_at=NOW,
            )
        )
    contact.email = None
    job.result = {
        STATE_KEY: _state(terminal_outcome=EmailExecutionOutcome.NO_VERIFIED_ADDRESS.value),
        "domain_outcome": EmailExecutionOutcome.NO_VERIFIED_ADDRESS.value,
    }
    transition_stage(
        db_session,
        membership=membership,
        agent_id=AgentIdentifier.EMAIL,
        target=PipelineStageStatus.FAILED,
        event_type=PipelineEventType.FAILED_TERMINAL,
        actor="test",
        job=job,
        reason_code=EmailExecutionOutcome.NO_VERIFIED_ADDRESS.value,
        reason_detail="None of the allowed candidates produced a verified address.",
    )

    execution = PhaseTwoWorkbenchReader(db_session).contact_execution(
        scenario.campaign.id, membership.id
    )

    assert execution is not None
    assert execution.email is not None
    assert execution.email.no_verified_address is True
    assert execution.email.accepted is False
    assert execution.email.accepted_email is None
    assert execution.email.attempted_count == 3
    assert [row.status for row in execution.email.attempts] == ["rejected"] * 3


def test_contact_email_alone_does_not_make_email_agent_outcome_accepted(
    db_session: Session,
) -> None:
    scenario, membership, _contact, _company, job = _email_execution(db_session)
    job.result = {STATE_KEY: _state(terminal_outcome=None)}

    execution = PhaseTwoWorkbenchReader(db_session).contact_execution(
        scenario.campaign.id, membership.id
    )

    assert execution is not None
    assert execution.contact_email is not None
    assert execution.email is not None
    assert execution.email.accepted is False
    assert execution.email.accepted_email is None

"""Durable Email Agent policy, orchestration, and provenance tests."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.audit_event import AuditEvent
from app.models.company import Company
from app.models.company_domain_resolution import CompanyDomainResolution
from app.models.contact import Contact
from app.models.email_candidate import EmailCandidate
from app.models.email_discovery import EmailCandidateAttempt, EmailCandidateAttemptStatus
from app.models.email_evidence import ExactEmailVerification
from app.models.enums import (
    AgentIdentifier,
    AgentJobStatus,
    CompanyFieldSource,
    DomainResolutionKind,
    DomainResolutionState,
    EmailVerificationResult,
    ResearchState,
    SuppressionReason,
    SuppressionType,
)
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.models.verification_job import AgentJob
from app.services.agents import jobs
from app.services.companies import provenance as company_provenance
from app.services.email.agent import (
    STATE_KEY,
    EmailAgentStateError,
    EmailExecutionOutcome,
    EmailExecutionStep,
    EmailExecutionStepKind,
    VerificationPortDecision,
    VerificationPortOutcome,
    enqueue_email_job,
    execute_step,
)
from app.services.email.candidates import generate_candidates
from app.services.email.discovery_policy import POLICY_IDENTIFIER, POLICY_VERSION
from app.services.suppressions import add_suppression
from app.services.verification.policy import VerificationPolicy, get_policy
from app.services.verification.provider import SIMULATOR_PROVIDER_LABEL
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


@dataclass
class EmailFixture:
    company: Company
    contact: Contact
    job: AgentJob


@pytest.fixture()
def email_api_client(db_session: Session) -> Iterator[TestClient]:
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


class StoredFakeVerificationPort:
    """Test-only port whose outcomes live entirely in shared durable rows."""

    def __init__(self) -> None:
        self.created_children = 0

    def ensure_child(
        self,
        session: Session,
        *,
        parent_job: AgentJob,
        attempt: EmailCandidateAttempt,
        actor: str,
    ) -> AgentJob:
        child, created = jobs.enqueue_job(
            session,
            agent_id=AgentIdentifier.VERIFICATION,
            idempotency_key=f"test-verification:{attempt.id}",
            task_kind="verify_email_candidate",
            max_attempts=4,
            campaign_id=parent_job.campaign_id,
            campaign_contact_id=parent_job.campaign_contact_id,
            contact_id=parent_job.contact_id,
            company_id=parent_job.company_id,
            entity_type="email_candidate_attempt",
            entity_id=attempt.id,
            input_reference={
                "candidate_attempt_id": str(attempt.id),
                "email": attempt.normalized_email,
            },
            parent_job_id=parent_job.id,
            actor=actor,
        )
        child.email = attempt.normalized_email
        child.policy_version = "ver-1"
        if created:
            self.created_children += 1
        session.flush()
        return child

    def outcome_for(
        self,
        session: Session,
        *,
        child_job: AgentJob,
        attempt: EmailCandidateAttempt,
    ) -> VerificationPortOutcome | None:
        del session, attempt
        value = child_job.result
        if not isinstance(value, dict):
            return None
        decision_value = value.get("decision")
        if not isinstance(decision_value, str):
            return None
        verification_id = value.get("verification_id")
        reason_value = value.get("reason")
        reference_value = value.get("reference")
        return VerificationPortOutcome(
            decision=VerificationPortDecision(decision_value),
            verification_id=(
                uuid.UUID(verification_id) if isinstance(verification_id, str) else None
            ),
            production_eligible=value.get("production_eligible") is True,
            reason=reason_value if isinstance(reason_value, str) else None,
            reference=dict(reference_value) if isinstance(reference_value, dict) else {},
        )

    def resolve(
        self,
        session: Session,
        *,
        job: AgentJob,
        decision: VerificationPortDecision,
        reason: str | None = None,
        provider: str = "millionverifier",
    ) -> ExactEmailVerification | None:
        attempt = current_attempt(session, job)
        assert attempt.verification_job_id is not None
        child = session.get(AgentJob, attempt.verification_job_id)
        assert child is not None

        evidence: ExactEmailVerification | None = None
        if decision in {
            VerificationPortDecision.ACCEPT,
            VerificationPortDecision.REJECT,
            VerificationPortDecision.SIMULATED,
        }:
            result = (
                EmailVerificationResult.INVALID
                if decision is VerificationPortDecision.REJECT
                else EmailVerificationResult.VALID
            )
            evidence = ExactEmailVerification(
                email=attempt.normalized_email,
                result=result,
                provider=(
                    SIMULATOR_PROVIDER_LABEL
                    if decision is VerificationPortDecision.SIMULATED
                    else provider
                ),
                policy_version="ver-1",
                provider_reference=f"test-evidence:{attempt.id}",
                reason=reason,
                is_role=False,
                checked_at=NOW,
                contact_id=attempt.contact_id,
            )
            session.add(evidence)
            session.flush()

        child.result = {
            "decision": decision.value,
            "verification_id": str(evidence.id) if evidence is not None else None,
            "production_eligible": decision is VerificationPortDecision.ACCEPT,
            "reason": reason,
            "reference": {
                "authoritative_test_contract": True,
                "provider": provider,
            },
        }
        child.verification_id = evidence.id if evidence is not None else None
        if decision is VerificationPortDecision.RETRYABLE:
            child.status = AgentJobStatus.RETRY_SCHEDULED
            child.error_class = "provider_transient"
            child.last_error = reason or "retry later"
        elif decision in {
            VerificationPortDecision.TERMINAL,
            VerificationPortDecision.REFUSED,
        }:
            child.status = AgentJobStatus.FAILED
            child.error_class = decision.value
            child.last_error = reason
            child.finished_at = NOW
        else:
            child.status = AgentJobStatus.SUCCEEDED
            child.finished_at = NOW
        session.flush()
        return evidence


def make_email_fixture(
    session: Session,
    *,
    employee_count: str = "51",
    first_name: str | None = "Ada",
    last_name: str | None = "Lovelace",
    domain: str | None = "engines.example",
    force_refresh: bool = False,
    refresh_scope: str | None = None,
    research_state: ResearchState = ResearchState.COMPLETED,
) -> EmailFixture:
    company = Company(
        name=f"Analytical Engines {uuid.uuid4()}",
        domain=domain,
        research_state=research_state,
    )
    session.add(company)
    session.flush()
    company_provenance.record_observation(
        session,
        company=company,
        field_name="company_size",
        value=employee_count,
        source_kind=CompanyFieldSource.IMPORT,
        source_reference=f"test-company-size:{company.id}",
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
        first_name=first_name,
        last_name=last_name,
        company_name=company.name,
        company_domain=domain,
        company_id=company.id,
        natural_key=f"{first_name}|{last_name}|{domain}|{uuid.uuid4()}",
    )
    session.add(contact)
    session.flush()
    job, created = enqueue_email_job(
        session,
        contact_id=contact.id,
        company_id=company.id,
        force_refresh=force_refresh,
        refresh_scope=refresh_scope,
    )
    assert created is True
    return EmailFixture(company=company, contact=contact, job=job)


def record_domain_state(
    session: Session,
    fixture: EmailFixture,
    state: DomainResolutionState,
) -> CompanyDomainResolution:
    snapshot = LinkedInProfileSnapshot(
        client_capture_id=str(uuid.uuid4()),
        content_hash="a" * 64,
        schema_version="test/1",
        source="test",
        extraction_status="complete",
        payload={},
        profile_fields={},
    )
    session.add(snapshot)
    session.flush()
    resolution = CompanyDomainResolution(
        capture_id=snapshot.id,
        resolved_company_id=fixture.company.id,
        decision_number=1,
        is_current=True,
        state=state,
        decision_kind=DomainResolutionKind.AUTOMATIC,
        policy_version="domain-resolution-test-v1",
        selected_domain=(
            fixture.company.domain if state is not DomainResolutionState.UNRESOLVED else None
        ),
        reasons=["test"],
        provider_call_made=False,
        decided_by="test",
    )
    session.add(resolution)
    session.flush()
    return resolution


def verification_policy() -> VerificationPolicy:
    return get_policy(get_settings())


def run_step(
    session: Session,
    fixture: EmailFixture,
    port: StoredFakeVerificationPort,
) -> EmailExecutionStep:
    return execute_step(
        session,
        job=fixture.job,
        contact=fixture.contact,
        membership=None,
        verification_port=port,
        verification_policy=verification_policy(),
        now=NOW,
    )


def attempts(session: Session, job: AgentJob) -> list[EmailCandidateAttempt]:
    return list(
        session.scalars(
            select(EmailCandidateAttempt)
            .where(EmailCandidateAttempt.email_job_id == job.id)
            .order_by(EmailCandidateAttempt.candidate_index)
        ).all()
    )


def current_attempt(session: Session, job: AgentJob) -> EmailCandidateAttempt:
    state = cast(dict[str, Any], (job.result or {})[STATE_KEY])
    index = int(state["current_candidate_index"])
    return session.scalars(
        select(EmailCandidateAttempt).where(
            EmailCandidateAttempt.email_job_id == job.id,
            EmailCandidateAttempt.candidate_index == index,
        )
    ).one()


def reject_current(
    session: Session,
    fixture: EmailFixture,
    port: StoredFakeVerificationPort,
) -> EmailExecutionStep:
    port.resolve(
        session,
        job=fixture.job,
        decision=VerificationPortDecision.REJECT,
        reason="definitive mailbox rejection",
    )
    return run_step(session, fixture, port)


@pytest.mark.parametrize("accepted_index", [0, 1, 2])
def test_first_second_or_third_candidate_can_be_accepted(
    db_session: Session,
    accepted_index: int,
) -> None:
    fixture = make_email_fixture(db_session)
    port = StoredFakeVerificationPort()

    step = run_step(db_session, fixture, port)
    assert step.outcome is EmailExecutionOutcome.WAITING_ON_VERIFICATION
    for _ in range(accepted_index):
        step = reject_current(db_session, fixture, port)
        assert step.outcome is EmailExecutionOutcome.WAITING_ON_VERIFICATION

    accepted_attempt = current_attempt(db_session, fixture.job)
    expected = accepted_attempt.normalized_email
    evidence = port.resolve(
        db_session,
        job=fixture.job,
        decision=VerificationPortDecision.ACCEPT,
    )
    assert evidence is not None
    step = run_step(db_session, fixture, port)

    assert step.kind is EmailExecutionStepKind.COMPLETE
    assert step.outcome is EmailExecutionOutcome.VERIFIED_EMAIL_ACCEPTED
    assert fixture.contact.email == expected
    assert len(attempts(db_session, fixture.job)) == accepted_index + 1
    assert port.created_children == accepted_index + 1
    assert attempts(db_session, fixture.job)[-1].verification_id == evidence.id


def test_all_three_rejections_finish_truthfully_without_contact_email(
    db_session: Session,
) -> None:
    fixture = make_email_fixture(db_session)
    port = StoredFakeVerificationPort()
    assert run_step(db_session, fixture, port).kind is EmailExecutionStepKind.WAITING

    for index in range(3):
        step = reject_current(db_session, fixture, port)
        if index < 2:
            assert step.kind is EmailExecutionStepKind.WAITING

    assert step.kind is EmailExecutionStepKind.TERMINAL
    assert step.outcome is EmailExecutionOutcome.NO_VERIFIED_ADDRESS
    assert fixture.contact.email is None
    assert len(attempts(db_session, fixture.job)) == 3
    assert all(
        attempt.status == EmailCandidateAttemptStatus.REJECTED.value
        for attempt in attempts(db_session, fixture.job)
    )


def test_acceptance_stops_immediately_and_terminal_replay_is_write_idempotent(
    db_session: Session,
) -> None:
    fixture = make_email_fixture(db_session)
    port = StoredFakeVerificationPort()
    run_step(db_session, fixture, port)
    port.resolve(
        db_session,
        job=fixture.job,
        decision=VerificationPortDecision.ACCEPT,
    )
    first = run_step(db_session, fixture, port)
    audit_count = db_session.scalar(
        select(func.count()).where(AuditEvent.action == "contact.email_accepted")
    )
    assert audit_count == 1

    replay = run_step(db_session, fixture, StoredFakeVerificationPort())
    assert replay.outcome is first.outcome
    assert replay.output_reference["replayed"] is True
    assert len(attempts(db_session, fixture.job)) == 1
    assert (
        db_session.scalar(select(func.count()).where(AuditEvent.action == "contact.email_accepted"))
        == 1
    )
    assert (
        db_session.scalar(select(func.count()).where(AgentJob.parent_job_id == fixture.job.id)) == 1
    )


def test_only_one_child_exists_while_verification_is_retryable(
    db_session: Session,
) -> None:
    fixture = make_email_fixture(db_session)
    port = StoredFakeVerificationPort()
    run_step(db_session, fixture, port)
    child_id = current_attempt(db_session, fixture.job).verification_job_id
    port.resolve(
        db_session,
        job=fixture.job,
        decision=VerificationPortDecision.RETRYABLE,
        reason="temporary provider timeout",
    )

    retryable = run_step(db_session, fixture, port)
    duplicate = run_step(db_session, fixture, port)
    assert retryable.outcome is EmailExecutionOutcome.RETRYABLE_VERIFICATION_DEPENDENCY
    assert duplicate.outcome is EmailExecutionOutcome.RETRYABLE_VERIFICATION_DEPENDENCY
    assert current_attempt(db_session, fixture.job).verification_job_id == child_id
    assert port.created_children == 1
    assert len(attempts(db_session, fixture.job)) == 1


@pytest.mark.parametrize(
    ("decision", "expected_outcome", "expected_status"),
    [
        (
            VerificationPortDecision.TERMINAL,
            EmailExecutionOutcome.TERMINAL_VERIFICATION_FAILURE,
            EmailCandidateAttemptStatus.TERMINAL_NO_RESULT,
        ),
        (
            VerificationPortDecision.REFUSED,
            EmailExecutionOutcome.TERMINAL_VERIFICATION_REFUSAL,
            EmailCandidateAttemptStatus.REFUSED,
        ),
        (
            VerificationPortDecision.SIMULATED,
            EmailExecutionOutcome.SIMULATED_VERIFICATION_REFUSED,
            EmailCandidateAttemptStatus.SIMULATED,
        ),
    ],
)
def test_terminal_refused_and_simulated_child_outcomes_do_not_advance(
    db_session: Session,
    decision: VerificationPortDecision,
    expected_outcome: EmailExecutionOutcome,
    expected_status: EmailCandidateAttemptStatus,
) -> None:
    fixture = make_email_fixture(db_session)
    port = StoredFakeVerificationPort()
    run_step(db_session, fixture, port)
    port.resolve(db_session, job=fixture.job, decision=decision, reason="test outcome")

    step = run_step(db_session, fixture, port)
    assert step.outcome is expected_outcome
    assert fixture.contact.email is None
    assert len(attempts(db_session, fixture.job)) == 1
    assert attempts(db_session, fixture.job)[0].status == expected_status.value


def test_duplicate_parent_execution_reuses_attempt_child_and_enqueue_intent(
    db_session: Session,
) -> None:
    fixture = make_email_fixture(db_session)
    immutable_input = dict(fixture.job.input_reference)
    port = StoredFakeVerificationPort()

    assert run_step(db_session, fixture, port).kind is EmailExecutionStepKind.WAITING
    assert run_step(db_session, fixture, port).kind is EmailExecutionStepKind.WAITING
    duplicate, created = enqueue_email_job(
        db_session,
        contact_id=fixture.contact.id,
        company_id=fixture.company.id,
    )

    assert duplicate.id == fixture.job.id
    assert created is False
    assert fixture.job.input_reference == immutable_input
    assert len(attempts(db_session, fixture.job)) == 1
    assert port.created_children == 1


def test_new_worker_resumes_from_stored_rejection_and_creates_only_next_child(
    db_session: Session,
) -> None:
    fixture = make_email_fixture(db_session)
    first_worker = StoredFakeVerificationPort()
    run_step(db_session, fixture, first_worker)
    first_worker.resolve(
        db_session,
        job=fixture.job,
        decision=VerificationPortDecision.REJECT,
    )

    replacement_worker = StoredFakeVerificationPort()
    step = run_step(db_session, fixture, replacement_worker)
    assert step.outcome is EmailExecutionOutcome.WAITING_ON_VERIFICATION
    rows = attempts(db_session, fixture.job)
    assert [row.status for row in rows] == [
        EmailCandidateAttemptStatus.REJECTED.value,
        EmailCandidateAttemptStatus.WAITING.value,
    ]
    assert rows[0].verification_job_id != rows[1].verification_job_id
    assert replacement_worker.created_children == 1


def test_lease_recovery_preserves_email_checkpoint_and_child_relationship(
    db_session: Session,
) -> None:
    fixture = make_email_fixture(db_session)
    port = StoredFakeVerificationPort()
    run_step(db_session, fixture, port)
    state_before = dict(cast(dict[str, Any], (fixture.job.result or {})[STATE_KEY]))
    child_id = current_attempt(db_session, fixture.job).verification_job_id
    fixture.job.status = AgentJobStatus.IN_PROGRESS
    fixture.job.attempts = 1
    fixture.job.max_attempts = 3
    fixture.job.lease_owner = "dead-worker"
    fixture.job.lease_expires_at = NOW - timedelta(minutes=1)
    db_session.flush()

    recovered = jobs.recover_expired_leases(
        db_session,
        now=NOW,
        agent_ids=(AgentIdentifier.EMAIL,),
    )
    assert [row.id for row in recovered] == [fixture.job.id]
    assert fixture.job.status is AgentJobStatus.PENDING
    assert (fixture.job.result or {})[STATE_KEY] == state_before
    assert current_attempt(db_session, fixture.job).verification_job_id == child_id


def test_generated_candidate_is_not_a_permanent_email_before_acceptance(
    db_session: Session,
) -> None:
    fixture = make_email_fixture(db_session)
    step = run_step(db_session, fixture, StoredFakeVerificationPort())
    assert step.kind is EmailExecutionStepKind.WAITING
    assert fixture.contact.email is None
    assert (
        db_session.scalar(
            select(func.count()).where(
                EmailCandidate.contact_id == fixture.contact.id,
                EmailCandidate.engine_version == POLICY_VERSION,
            )
        )
        == 3
    )


def test_legacy_candidate_regeneration_preserves_audited_attempt_rows(
    db_session: Session,
) -> None:
    fixture = make_email_fixture(db_session)
    run_step(db_session, fixture, StoredFakeVerificationPort())
    row = attempts(db_session, fixture.job)[0]
    candidate_id = row.candidate_id

    regenerated = generate_candidates(db_session, fixture.contact)

    assert regenerated.selected is not None
    assert db_session.get(EmailCandidateAttempt, row.id) is row
    assert db_session.get(EmailCandidate, candidate_id) is not None


def test_existing_fresh_accepted_email_is_reused_without_candidates_or_child(
    db_session: Session,
) -> None:
    fixture = make_email_fixture(db_session)
    fixture.contact.email = "known@engines.example"
    evidence = ExactEmailVerification(
        email=fixture.contact.email,
        result=EmailVerificationResult.VALID,
        provider="millionverifier",
        policy_version="ver-1",
        provider_reference="existing-verified-address",
        is_role=False,
        checked_at=NOW,
        contact_id=fixture.contact.id,
    )
    db_session.add(evidence)
    db_session.flush()

    step = run_step(db_session, fixture, StoredFakeVerificationPort())
    state = cast(dict[str, Any], (fixture.job.result or {})[STATE_KEY])
    assert step.outcome is EmailExecutionOutcome.EXISTING_EMAIL_REUSED
    assert step.result["verification_id"] == str(evidence.id)
    assert state["policy_identifier"] == POLICY_IDENTIFIER
    assert state["policy_version"] == POLICY_VERSION
    assert state["employee_count_class"] == "more_than_50"
    assert state["ordered_candidate_formats"] == []
    assert attempts(db_session, fixture.job) == []
    assert (
        db_session.scalar(select(func.count()).where(AgentJob.parent_job_id == fixture.job.id)) == 0
    )


def test_unknown_employee_count_blocks_then_replans_when_sourced_evidence_arrives(
    db_session: Session,
) -> None:
    fixture = make_email_fixture(db_session, employee_count="unknown")
    port = StoredFakeVerificationPort()
    blocked = run_step(db_session, fixture, port)
    assert blocked.outcome is EmailExecutionOutcome.EMPLOYEE_COUNT_UNKNOWN
    assert port.created_children == 0

    company_provenance.record_observation(
        db_session,
        company=fixture.company,
        field_name="company_size",
        value="51",
        source_kind=CompanyFieldSource.IMPORT,
        source_reference="new-employee-count-source",
        observed_at=NOW + timedelta(seconds=1),
        created_by="test",
    )
    company_provenance.reconcile_field(
        db_session,
        company=fixture.company,
        field_name="company_size",
        actor="test",
    )
    resumed = run_step(db_session, fixture, port)
    state = cast(dict[str, Any], (fixture.job.result or {})[STATE_KEY])

    assert resumed.outcome is EmailExecutionOutcome.WAITING_ON_VERIFICATION
    assert state["employee_count_class"] == "more_than_50"
    assert len(state["prior_policy_outcomes"]) == 1
    assert state["prior_policy_outcomes"][0]["domain_outcome"] == (
        EmailExecutionOutcome.EMPLOYEE_COUNT_UNKNOWN.value
    )


def test_stale_company_evidence_blocks_then_resumes_when_freshness_is_restored(
    db_session: Session,
) -> None:
    fixture = make_email_fixture(db_session, research_state=ResearchState.STALE)
    port = StoredFakeVerificationPort()
    blocked = run_step(db_session, fixture, port)
    assert blocked.outcome is EmailExecutionOutcome.EMPLOYEE_COUNT_STALE
    assert port.created_children == 0

    fixture.company.research_state = ResearchState.COMPLETED
    db_session.flush()
    resumed = run_step(db_session, fixture, port)
    assert resumed.outcome is EmailExecutionOutcome.WAITING_ON_VERIFICATION


def test_employee_evidence_change_during_verification_requires_scoped_refresh(
    db_session: Session,
) -> None:
    fixture = make_email_fixture(db_session, employee_count="51")
    port = StoredFakeVerificationPort()
    run_step(db_session, fixture, port)
    child_id = current_attempt(db_session, fixture.job).verification_job_id

    company_provenance.record_observation(
        db_session,
        company=fixture.company,
        field_name="company_size",
        value="50",
        source_kind=CompanyFieldSource.MANUAL,
        source_reference="operator-correction",
        observed_at=NOW + timedelta(seconds=1),
        created_by="operator",
    )
    company_provenance.reconcile_field(
        db_session,
        company=fixture.company,
        field_name="company_size",
        actor="operator",
    )
    blocked = run_step(db_session, fixture, port)

    assert blocked.outcome is EmailExecutionOutcome.EMPLOYEE_COUNT_UNKNOWN
    assert "explicitly scoped" in (blocked.reason or "")
    assert current_attempt(db_session, fixture.job).verification_job_id == child_id
    assert port.created_children == 1


def test_unusable_identity_blocks_then_resumes_after_observed_name_correction(
    db_session: Session,
) -> None:
    fixture = make_email_fixture(db_session, first_name=None)
    port = StoredFakeVerificationPort()
    blocked = run_step(db_session, fixture, port)
    assert blocked.outcome is EmailExecutionOutcome.MISSING_OR_UNUSABLE_IDENTITY

    fixture.contact.first_name = "Ada"
    db_session.flush()
    resumed = run_step(db_session, fixture, port)
    assert resumed.outcome is EmailExecutionOutcome.WAITING_ON_VERIFICATION


def test_forced_refresh_is_explicit_scoped_idempotent_and_does_not_reuse(
    db_session: Session,
) -> None:
    fixture = make_email_fixture(
        db_session,
        force_refresh=True,
        refresh_scope="operator-case-224",
    )
    fixture.contact.email = "known@engines.example"
    db_session.add(
        ExactEmailVerification(
            email=fixture.contact.email,
            result=EmailVerificationResult.VALID,
            provider="millionverifier",
            policy_version="ver-1",
            is_role=False,
            checked_at=NOW,
            contact_id=fixture.contact.id,
        )
    )
    db_session.flush()

    step = run_step(db_session, fixture, StoredFakeVerificationPort())
    duplicate, created = enqueue_email_job(
        db_session,
        contact_id=fixture.contact.id,
        company_id=fixture.company.id,
        force_refresh=True,
        refresh_scope="operator-case-224",
    )
    assert step.outcome is EmailExecutionOutcome.WAITING_ON_VERIFICATION
    assert duplicate.id == fixture.job.id
    assert created is False
    attempt = attempts(db_session, fixture.job)[0]
    assert attempt.force_refresh is True
    assert attempt.refresh_scope == "operator-case-224"


@pytest.mark.parametrize(
    ("force_refresh", "refresh_scope"),
    [(True, None), (False, "not-for-standard")],
)
def test_invalid_forced_refresh_intent_is_refused(
    db_session: Session,
    force_refresh: bool,
    refresh_scope: str | None,
) -> None:
    contact = Contact(first_name="A", last_name="B", natural_key=str(uuid.uuid4()))
    db_session.add(contact)
    db_session.flush()
    with pytest.raises(EmailAgentStateError):
        enqueue_email_job(
            db_session,
            contact_id=contact.id,
            force_refresh=force_refresh,
            refresh_scope=refresh_scope,
        )


def test_missing_company_relationship_is_blocked_before_policy_or_child(
    db_session: Session,
) -> None:
    contact = Contact(
        first_name="Ada",
        last_name="Lovelace",
        company_domain="engines.example",
        natural_key=str(uuid.uuid4()),
    )
    db_session.add(contact)
    db_session.flush()
    job, _ = enqueue_email_job(db_session, contact_id=contact.id)
    step = execute_step(
        db_session,
        job=job,
        contact=contact,
        membership=None,
        verification_port=StoredFakeVerificationPort(),
        verification_policy=verification_policy(),
        now=NOW,
    )
    assert step.outcome is EmailExecutionOutcome.COMPANY_UNAVAILABLE


def test_missing_company_domain_is_blocked_before_candidate_generation(
    db_session: Session,
) -> None:
    fixture = make_email_fixture(db_session, domain=None)
    step = run_step(db_session, fixture, StoredFakeVerificationPort())
    assert step.outcome is EmailExecutionOutcome.DOMAIN_UNAVAILABLE
    assert attempts(db_session, fixture.job) == []


def test_contact_company_domain_boundary_mismatch_is_blocked(
    db_session: Session,
) -> None:
    fixture = make_email_fixture(db_session)
    fixture.contact.company_domain = "different.example"
    db_session.flush()
    step = run_step(db_session, fixture, StoredFakeVerificationPort())
    assert step.outcome is EmailExecutionOutcome.DOMAIN_INELIGIBLE
    assert step.reason_code == "company_domain_boundary_mismatch"


def test_provisional_company_domain_gate_blocks_email_discovery(
    db_session: Session,
) -> None:
    fixture = make_email_fixture(db_session)
    record_domain_state(db_session, fixture, DomainResolutionState.PROVISIONAL)
    port = StoredFakeVerificationPort()
    step = run_step(db_session, fixture, port)
    assert step.outcome is EmailExecutionOutcome.DOMAIN_INELIGIBLE
    assert "provisional" in (step.reason or "")
    assert port.created_children == 0
    assert attempts(db_session, fixture.job) == []


def test_confirmed_company_domain_gate_allows_email_discovery(
    db_session: Session,
) -> None:
    fixture = make_email_fixture(db_session)
    record_domain_state(db_session, fixture, DomainResolutionState.CONFIRMED)
    step = run_step(db_session, fixture, StoredFakeVerificationPort())
    assert step.outcome is EmailExecutionOutcome.WAITING_ON_VERIFICATION


def test_merged_contact_identity_is_blocked(
    db_session: Session,
) -> None:
    fixture = make_email_fixture(db_session)
    survivor = Contact(
        first_name="Ada",
        last_name="Lovelace",
        company_id=fixture.company.id,
        company_domain=fixture.company.domain,
        natural_key=str(uuid.uuid4()),
    )
    db_session.add(survivor)
    db_session.flush()
    fixture.contact.merged_into_id = survivor.id
    db_session.flush()
    step = run_step(db_session, fixture, StoredFakeVerificationPort())
    assert step.outcome is EmailExecutionOutcome.MISSING_OR_UNUSABLE_IDENTITY
    assert step.reason_code == "identity_ambiguous"


def test_domain_suppression_blocks_before_any_candidate_or_child(
    db_session: Session,
) -> None:
    fixture = make_email_fixture(db_session)
    add_suppression(
        db_session,
        suppression_type=SuppressionType.DOMAIN,
        value=fixture.company.domain or "",
        reason=SuppressionReason.OPT_OUT,
        source="test",
    )
    step = run_step(db_session, fixture, StoredFakeVerificationPort())
    assert step.outcome is EmailExecutionOutcome.SUPPRESSED
    assert fixture.contact.email is None
    assert attempts(db_session, fixture.job) == []


def test_exact_candidate_suppression_blocks_before_child_enqueue(
    db_session: Session,
) -> None:
    fixture = make_email_fixture(db_session)
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="ada.lovelace@engines.example",
        reason=SuppressionReason.OPT_OUT,
        source="test",
    )
    port = StoredFakeVerificationPort()
    step = run_step(db_session, fixture, port)
    assert step.outcome is EmailExecutionOutcome.SUPPRESSED
    assert len(attempts(db_session, fixture.job)) == 1
    assert attempts(db_session, fixture.job)[0].status == "refused"
    assert port.created_children == 0


def test_suppression_appearing_during_verification_prevents_accepted_write(
    db_session: Session,
) -> None:
    fixture = make_email_fixture(db_session)
    port = StoredFakeVerificationPort()
    run_step(db_session, fixture, port)
    candidate = current_attempt(db_session, fixture.job).normalized_email
    port.resolve(
        db_session,
        job=fixture.job,
        decision=VerificationPortDecision.ACCEPT,
    )
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value=candidate,
        reason=SuppressionReason.OPT_OUT,
        source="concurrent-test",
    )

    step = run_step(db_session, fixture, port)
    assert step.outcome is EmailExecutionOutcome.SUPPRESSED
    assert fixture.contact.email is None
    assert current_attempt(db_session, fixture.job).status == "refused"


def test_concurrent_fresh_accepted_email_wins_without_overwrite(
    db_session: Session,
) -> None:
    fixture = make_email_fixture(db_session)
    port = StoredFakeVerificationPort()
    run_step(db_session, fixture, port)
    port.resolve(
        db_session,
        job=fixture.job,
        decision=VerificationPortDecision.ACCEPT,
    )
    fixture.contact.email = "other@engines.example"
    other_evidence = ExactEmailVerification(
        email=fixture.contact.email,
        result=EmailVerificationResult.VALID,
        provider="millionverifier",
        policy_version="ver-1",
        is_role=False,
        checked_at=NOW,
        contact_id=fixture.contact.id,
    )
    db_session.add(other_evidence)
    db_session.flush()

    step = run_step(db_session, fixture, port)
    assert step.outcome is EmailExecutionOutcome.EXISTING_EMAIL_REUSED
    assert fixture.contact.email == "other@engines.example"
    assert step.result["verification_id"] == str(other_evidence.id)
    assert current_attempt(db_session, fixture.job).status == "refused"


def test_attempt_audit_references_policy_employee_and_exact_verification_evidence(
    db_session: Session,
) -> None:
    fixture = make_email_fixture(db_session)
    port = StoredFakeVerificationPort()
    run_step(db_session, fixture, port)
    evidence = port.resolve(
        db_session,
        job=fixture.job,
        decision=VerificationPortDecision.ACCEPT,
    )
    step = run_step(db_session, fixture, port)
    row = attempts(db_session, fixture.job)[0]

    assert evidence is not None
    assert row.policy_identifier == POLICY_IDENTIFIER
    assert row.policy_version == POLICY_VERSION
    assert row.employee_count_class == "more_than_50"
    assert row.employee_evidence_id is not None
    assert row.verification_id == evidence.id
    assert row.verification_job_id is not None
    assert step.result["address_derivation"] == (
        "generated_candidate_verified_by_exact_address_evidence"
    )


def test_attempt_repr_and_persisted_state_expose_no_provider_secret(
    db_session: Session,
) -> None:
    fixture = make_email_fixture(db_session)
    run_step(db_session, fixture, StoredFakeVerificationPort())
    row = attempts(db_session, fixture.job)[0]
    combined = f"{row!r} {fixture.job.result!r}".casefold()
    assert row.normalized_email not in repr(row)
    assert "api_key" not in combined
    assert "credential" not in combined
    assert "secret" not in combined


def test_phase2_api_exposes_authoritative_email_attempt_projection(
    db_session: Session,
    email_api_client: TestClient,
) -> None:
    fixture = make_email_fixture(db_session)
    run_step(db_session, fixture, StoredFakeVerificationPort())

    response = email_api_client.get(f"/api/agent-jobs/{fixture.job.id}/email-attempts")

    assert response.status_code == 200
    body = response.json()
    assert body["job"]["agent_id"] == "email"
    assert body["job"]["result"][STATE_KEY]["policy_version"] == POLICY_VERSION
    assert len(body["attempts"]) == 1
    assert body["attempts"][0]["policy_identifier"] == POLICY_IDENTIFIER
    assert body["attempts"][0]["verification_job_id"] is not None

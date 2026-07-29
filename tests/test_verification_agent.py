"""Verification Agent on the Phase 2 backbone (MVP-01E / #225).

The provider adapter, the outcome mapping, the freshness policy and the common
Agent Job queue all have their own suites. These tests cover the Verification
Agent boundary: what it refuses, what it reuses, what it accepts, and — above all
— what it must never allow to advance a Campaign Contact.

Every case runs through the real Phase 2 worker path, so a passing test also
proves the adapter never moves a job itself.

Nothing here reaches the network. The live branch is exercised through the
adapter's explicit provider seam with a local non-simulated fake.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from app.core.config import Settings, get_settings
from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_candidate import EmailCandidate
from app.models.email_evidence import ExactEmailVerification
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    AgentJobStatus,
    CampaignStatus,
    EmailCandidateSource,
    EmailPreciseStatus,
    EmailVerificationResult,
    PipelineStageStatus,
    SuppressionReason,
    SuppressionType,
    VerificationFailureClass,
)
from app.models.pipeline import CampaignContactAgentState, PipelineEvent
from app.models.verification_attempt import VerificationAttempt
from app.models.verification_job import AgentJob
from app.services import campaign_contacts, pipeline
from app.services.agents import controls
from app.services.agents import jobs as agent_jobs
from app.services.agents.adapters import DEFAULT_ADAPTERS, VerificationAgentAdapter
from app.services.agents.orchestrator import run_next
from app.services.suppressions import add_suppression
from app.services.verification import attempts as job_attempts
from app.services.verification import service as verification_service
from app.services.verification.decisions import VerificationDecision, decide
from app.services.verification.policy import get_policy
from app.services.verification.provider import (
    LIVE_PROVIDER_LABEL,
    SIMULATOR_PROVIDER_LABEL,
    ProviderResponse,
    ProviderTransientError,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

POLICY = "ver-1"
REAL_KEY = "not-a-test-key-live-abc123"
WORKER = "verification-agent-test"


# --- fakes ---------------------------------------------------------------------


class LiveProvider:
    """A non-simulated provider that never touches the network.

    ``simulated = False`` is the whole point: it lets the suite exercise the live
    branch the Agent requires, without a credential or a request.
    """

    name = "millionverifier"
    simulated = False

    def __init__(self, script: list[object] | None = None) -> None:
        self.script = list(script or [])
        self.calls = 0

    def verify(self, email: str) -> ProviderResponse:
        self.calls += 1
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            assert isinstance(item, ProviderResponse)
            return item
        return _ok(email)


class SimulatedProvider:
    """Declares itself simulated, exactly as the real simulator does."""

    name = "millionverifier"
    simulated = True

    def __init__(self) -> None:
        self.calls = 0

    def verify(self, email: str) -> ProviderResponse:
        self.calls += 1
        return _ok(email)


def _ok(email: str, **kw: object) -> ProviderResponse:
    base: dict[str, object] = dict(
        email=email,
        result="ok",
        resultcode=1,
        credits=100,
        raw={"result": "ok", "resultcode": 1, "email": email},
    )
    base.update(kw)
    return ProviderResponse(**base)  # type: ignore[arg-type]


def _result(email: str, result: str, code: int, **kw: object) -> ProviderResponse:
    base: dict[str, object] = dict(
        email=email,
        result=result,
        resultcode=code,
        credits=100,
        raw={"result": result, "resultcode": code, "email": email},
    )
    base.update(kw)
    return ProviderResponse(**base)  # type: ignore[arg-type]


def _error(email: str, error: str, **kw: object) -> ProviderResponse:
    base: dict[str, object] = dict(
        email=email, result=None, resultcode=None, error=error, raw={"error": error}
    )
    base.update(kw)
    return ProviderResponse(**base)  # type: ignore[arg-type]


def _adapters(provider: object) -> dict[AgentIdentifier, object]:
    merged = dict(DEFAULT_ADAPTERS)
    merged[AgentIdentifier.VERIFICATION] = VerificationAgentAdapter(
        provider_factory=lambda _settings: provider  # type: ignore[arg-type,return-value]
    )
    return merged


# --- fixtures ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def settings() -> Settings:
    return get_settings()


def _records(db: Session) -> tuple[Campaign, Company, Contact]:
    company = Company(name="Analytical Engines", domain="engines.example")
    campaign = Campaign(
        name=f"Verification {uuid.uuid4()}",
        status=CampaignStatus.ACTIVE,
        execution_enabled=True,
    )
    db.add_all([company, campaign])
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
    return campaign, company, contact


def _candidate(db: Session, contact: Contact, email: str) -> EmailCandidate:
    """The selected exact candidate the Email Agent would have produced."""

    local_part, _, domain = email.partition("@")
    candidate = EmailCandidate(
        contact_id=contact.id,
        email=email,
        local_part=local_part,
        domain=domain,
        pattern="{first}.{last}",
        source=EmailCandidateSource.GENERATED,
        engine_version="eml-1",
        rank=1,
        rank_score=1.0,
        rank_reason="deterministic test candidate",
        selected=True,
    )
    db.add(candidate)
    db.flush()
    return candidate


def _enable_verification(
    db: Session,
    *,
    live: bool = True,
    status: AgentControlStatus = AgentControlStatus.ENABLED,
) -> None:
    controls.set_global_control(
        db,
        agent_id=AgentIdentifier.VERIFICATION,
        status=status,
        config={"live": live},
    )


def _enrol(db: Session, campaign: Campaign, contact: Contact) -> CampaignContact:
    """Enrol and advance the membership so Verification is the next stage."""

    enrolled = campaign_contacts.enrol_contact(
        db,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        source_reference="verification-test",
        enqueue=False,
        desired_stage=AgentIdentifier.VERIFICATION,
    )
    membership = enrolled.membership
    # Complete the upstream stages directly: this suite is about Verification,
    # and the Identity/Company/Email adapters have their own coverage.
    for agent_id in (
        AgentIdentifier.IDENTITY,
        AgentIdentifier.COMPANY,
        AgentIdentifier.RESEARCH,
        AgentIdentifier.EMAIL,
    ):
        pipeline.transition_stage(
            db,
            membership=membership,
            agent_id=agent_id,
            target=PipelineStageStatus.COMPLETED,
            event_type=pipeline.PipelineEventType.STAGE_COMPLETED,
            actor="test-setup",
            reason_code="test_setup",
        )
    db.flush()
    return membership


def _queue_verification(
    db: Session,
    membership: CampaignContact,
    *,
    force_refresh: bool = False,
    policy_version: str | None = None,
) -> AgentJob:
    from app.services.agents import orchestrator

    job = orchestrator.schedule_next(db, membership=membership, actor="test")
    assert job is not None, "verification job was not queued"
    assert job.agent_id is AgentIdentifier.VERIFICATION
    if force_refresh or policy_version:
        payload = dict(job.input_reference or {})
        if force_refresh:
            payload["force_refresh"] = True
        if policy_version:
            payload["policy_version"] = policy_version
        job.input_reference = payload
        db.flush()
    return job


def _seed_evidence(
    db: Session,
    email: str,
    result: EmailVerificationResult,
    *,
    age_days: int = 0,
    is_role: bool = False,
    provider: str = LIVE_PROVIDER_LABEL,
    policy_version: str = POLICY,
) -> ExactEmailVerification:
    row = ExactEmailVerification(
        email=email,
        result=result,
        provider=provider,
        policy_version=policy_version,
        is_role=is_role,
        checked_at=datetime.now(UTC) - timedelta(days=age_days),
        raw_response={"result": result.value, "email": email},
    )
    db.add(row)
    db.flush()
    return row


def _run(db: Session, provider: object) -> object:
    return run_next(db, worker_id=WORKER, adapters=_adapters(provider))  # type: ignore[arg-type]


def _stage(db: Session, membership: CampaignContact) -> CampaignContactAgentState:
    state = pipeline.agent_state(
        db, campaign_contact_id=membership.id, agent_id=AgentIdentifier.VERIFICATION, create=False
    )
    assert state is not None
    return state


def _setup(
    db: Session, email: str = "ada.lovelace@engines.example", *, live: bool = True
) -> tuple[CampaignContact, AgentJob]:
    campaign, _, contact = _records(db)
    _candidate(db, contact, email)
    _enable_verification(db, live=live)
    membership = _enrol(db, campaign, contact)
    job = _queue_verification(db, membership)
    return membership, job


# --- acceptance ----------------------------------------------------------------


def test_fresh_live_valid_evidence_accepts_and_advances_the_pipeline(
    db_session: Session,
) -> None:
    membership, job = _setup(db_session)
    provider = LiveProvider()

    outcome = _run(db_session, provider)

    assert outcome.public_status == "completed"  # type: ignore[attr-defined]
    assert provider.calls == 1
    assert job.status is AgentJobStatus.SUCCEEDED
    state = _stage(db_session, membership)
    assert state.status is PipelineStageStatus.COMPLETED
    assert state.output_reference is not None
    assert state.output_reference["decision"] == VerificationDecision.ACCEPT.value
    assert state.output_reference["precise_status"] == EmailPreciseStatus.VALID.value
    assert state.output_reference["provider"] == LIVE_PROVIDER_LABEL
    # Durable evidence exists and the job points at it.
    evidence = db_session.get(
        ExactEmailVerification, uuid.UUID(state.output_reference["verification_id"])
    )
    assert evidence is not None
    assert evidence.result is EmailVerificationResult.VALID
    assert evidence.provider == LIVE_PROVIDER_LABEL
    assert evidence.raw_response is not None


# --- definitive answers that are NOT acceptance --------------------------------


@pytest.mark.parametrize(
    ("provider_result", "code", "extra", "expected"),
    [
        ("invalid", 6, {}, EmailPreciseStatus.INVALID),
        ("catch_all", 2, {}, EmailPreciseStatus.CATCH_ALL),
        ("unknown", 3, {}, EmailPreciseStatus.UNKNOWN),
        ("disposable", 5, {}, EmailPreciseStatus.DISPOSABLE),
        ("ok", 1, {"role": True}, EmailPreciseStatus.ROLE_BASED),
    ],
)
def test_a_definitive_non_valid_result_never_completes_the_stage(
    db_session: Session,
    provider_result: str,
    code: int,
    extra: dict[str, object],
    expected: EmailPreciseStatus,
) -> None:
    """The regression this Agent exists to prevent.

    ``process_job`` records every one of these as durable evidence and would mark
    the queue job succeeded. If the Agent simply mirrored queue status, an invalid
    or catch-all mailbox would complete the Verification stage and let the
    Campaign Contact advance toward outreach.
    """

    email = "ada.lovelace@engines.example"
    membership, job = _setup(db_session, email)
    provider = LiveProvider([_result(email, provider_result, code, **extra)])

    _run(db_session, provider)

    state = _stage(db_session, membership)
    assert state.status is PipelineStageStatus.BLOCKED
    assert state.status is not PipelineStageStatus.COMPLETED
    assert membership.latest_completed_stage is not AgentIdentifier.VERIFICATION
    assert job.status is AgentJobStatus.PAUSED
    # The evidence is still preserved — the address was genuinely checked.
    evidence = db_session.scalars(
        select(ExactEmailVerification).where(ExactEmailVerification.email == email)
    ).one()
    assert evidence.result.value == expected.value or expected is EmailPreciseStatus.ROLE_BASED
    assert state.reason_code is not None
    assert state.reason_code.startswith("verification_")


def test_a_blocked_verification_does_not_schedule_the_next_stage(db_session: Session) -> None:
    email = "ada.lovelace@engines.example"
    membership, _ = _setup(db_session, email)

    _run(db_session, LiveProvider([_result(email, "invalid", 6)]))

    later = db_session.scalars(
        select(AgentJob).where(
            AgentJob.campaign_contact_id == membership.id,
            AgentJob.agent_id == AgentIdentifier.INSIGHTS,
        )
    ).all()
    assert later == []


# --- refusals ------------------------------------------------------------------


def test_simulated_evidence_can_never_advance_a_production_campaign_contact(
    db_session: Session,
) -> None:
    membership, job = _setup(db_session)
    provider = SimulatedProvider()

    _run(db_session, provider)

    assert provider.calls == 0, "a simulated provider must be refused before it is called"
    assert job.status is AgentJobStatus.PAUSED
    state = _stage(db_session, membership)
    assert state.status is PipelineStageStatus.BLOCKED
    assert state.reason_code == "verification_credentials_missing"


def test_verification_without_live_authority_is_refused(db_session: Session) -> None:
    membership, job = _setup(db_session, live=False)
    provider = LiveProvider()

    _run(db_session, provider)

    assert provider.calls == 0
    state = _stage(db_session, membership)
    assert state.status is PipelineStageStatus.BLOCKED
    assert state.reason_code == "verification_live_disabled"
    assert job.status is AgentJobStatus.PAUSED


def test_suppression_is_rechecked_immediately_before_execution(db_session: Session) -> None:
    """A ledger entry added while the job waited must still stop it."""

    email = "ada.lovelace@engines.example"
    membership, job = _setup(db_session, email)
    provider = LiveProvider()

    # Suppressed after the job was queued.
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value=email,
        reason=SuppressionReason.OPT_OUT,
        source="test",
    )

    _run(db_session, provider)

    assert provider.calls == 0
    state = _stage(db_session, membership)
    assert state.status is PipelineStageStatus.BLOCKED
    assert state.reason_code == "suppression"
    assert db_session.scalars(select(ExactEmailVerification)).all() == []


def test_a_disabled_verification_agent_never_runs(db_session: Session) -> None:
    campaign, _, contact = _records(db_session)
    _candidate(db_session, contact, "ada.lovelace@engines.example")
    _enable_verification(db_session, status=AgentControlStatus.DISABLED)
    membership = _enrol(db_session, campaign, contact)
    provider = LiveProvider()

    from app.services.agents import orchestrator

    job = orchestrator.schedule_next(db_session, membership=membership, actor="test")

    _run(db_session, provider)

    assert provider.calls == 0
    state = _stage(db_session, membership)
    assert state.status is PipelineStageStatus.DISABLED
    if job is not None:
        assert job.status is not AgentJobStatus.SUCCEEDED


def test_a_campaign_override_can_disable_verification_for_one_campaign(
    db_session: Session,
) -> None:
    membership, job = _setup(db_session)
    controls.set_campaign_override(
        db_session,
        campaign_id=membership.campaign_id,
        agent_id=AgentIdentifier.VERIFICATION,
        status=AgentControlStatus.DISABLED,
        reason="campaign-level stop",
    )
    provider = LiveProvider()

    _run(db_session, provider)

    assert provider.calls == 0
    assert _stage(db_session, membership).status is PipelineStageStatus.DISABLED


def test_a_policy_version_mismatch_is_refused(db_session: Session) -> None:
    campaign, _, contact = _records(db_session)
    _candidate(db_session, contact, "ada.lovelace@engines.example")
    _enable_verification(db_session)
    membership = _enrol(db_session, campaign, contact)
    _queue_verification(db_session, membership, policy_version="ver-999")
    provider = LiveProvider()

    _run(db_session, provider)

    assert provider.calls == 0
    state = _stage(db_session, membership)
    assert state.status is PipelineStageStatus.BLOCKED
    assert state.reason_code == "verification_policy_mismatch"


def test_a_malformed_candidate_address_fails_terminally(db_session: Session) -> None:
    campaign, _, contact = _records(db_session)
    _candidate(db_session, contact, "not-an-email")
    _enable_verification(db_session)
    membership = _enrol(db_session, campaign, contact)
    _queue_verification(db_session, membership)
    provider = LiveProvider()

    _run(db_session, provider)

    assert provider.calls == 0
    state = _stage(db_session, membership)
    assert state.status is PipelineStageStatus.FAILED
    assert state.reason_code == "verification_invalid_input"


# --- reuse, staleness, conflict, forced refresh ---------------------------------


def test_fresh_evidence_is_reused_without_a_provider_call(db_session: Session) -> None:
    email = "ada.lovelace@engines.example"
    membership, _ = _setup(db_session, email)
    existing = _seed_evidence(db_session, email, EmailVerificationResult.VALID)
    provider = LiveProvider()

    _run(db_session, provider)

    assert provider.calls == 0
    state = _stage(db_session, membership)
    assert state.status is PipelineStageStatus.COMPLETED
    assert state.output_reference is not None
    assert state.output_reference["reused_evidence"] is True
    assert state.output_reference["verification_id"] == str(existing.id)


def test_stale_evidence_is_not_accepted(db_session: Session) -> None:
    """Reused evidence past its TTL is a real result but not a verification."""

    email = "ada.lovelace@engines.example"
    membership, _ = _setup(db_session, email)
    # 400 days is well past the 30-day VALID TTL, so it is not reused as fresh,
    # and the read model reports the address as stale.
    _seed_evidence(db_session, email, EmailVerificationResult.VALID, age_days=400)
    # The provider then also fails to settle it.
    provider = LiveProvider([_error(email, "invalid_api_key")])

    _run(db_session, provider)

    # Stale evidence is never reused as fresh, so a call was actually made.
    assert provider.calls == 1
    state = _stage(db_session, membership)
    assert state.status is PipelineStageStatus.FAILED
    assert state.status is not PipelineStageStatus.COMPLETED
    # The aged VALID row is still on file and was not promoted to an acceptance.
    assert db_session.scalars(
        select(ExactEmailVerification).where(ExactEmailVerification.email == email)
    ).all()


def test_conflicting_fresh_evidence_is_not_accepted(db_session: Session) -> None:
    email = "ada.lovelace@engines.example"
    membership, _ = _setup(db_session, email)
    # Two fresh results that disagree. Reuse picks the most recent, but the
    # address as a whole is not settled, so it must not be accepted — and must
    # not be reported as a plain invalid verdict either.
    _seed_evidence(db_session, email, EmailVerificationResult.VALID, age_days=1)
    _seed_evidence(db_session, email, EmailVerificationResult.INVALID)
    provider = LiveProvider()

    _run(db_session, provider)

    state = _stage(db_session, membership)
    assert state.status is not PipelineStageStatus.COMPLETED
    assert state.reason_code == "verification_conflicting_evidence"
    # A non-accepted outcome carries its decision on the event, not as a stage
    # output reference — nothing downstream may read it as a usable address.
    detail = next(
        event.detail
        for event in db_session.scalars(select(PipelineEvent)).all()
        if event.agent_id is AgentIdentifier.VERIFICATION and event.detail.get("decision")
    )
    assert detail["decision"] != VerificationDecision.ACCEPT.value
    assert detail["precise_status"] == EmailPreciseStatus.CONFLICTING_EVIDENCE.value


def test_a_live_run_never_reuses_simulator_evidence(db_session: Session) -> None:
    """Simulator evidence cannot be upgraded into a live acceptance."""

    email = "ada.lovelace@engines.example"
    membership, _ = _setup(db_session, email)
    simulated = _seed_evidence(
        db_session,
        email,
        EmailVerificationResult.VALID,
        provider=SIMULATOR_PROVIDER_LABEL,
    )
    provider = LiveProvider([_ok(email)])

    _run(db_session, provider)

    assert provider.calls == 1
    state = _stage(db_session, membership)
    assert state.status is PipelineStageStatus.COMPLETED
    assert state.output_reference is not None
    assert state.output_reference["verification_id"] != str(simulated.id)
    evidence = db_session.scalars(
        select(ExactEmailVerification)
        .where(ExactEmailVerification.email == email)
        .order_by(ExactEmailVerification.checked_at.asc())
    ).all()
    assert len(evidence) == 2
    assert evidence[0].id == simulated.id
    assert evidence[-1].id != simulated.id
    assert evidence[-1].provider == LIVE_PROVIDER_LABEL
    assert evidence[-1].result is EmailVerificationResult.VALID


def test_evidence_from_an_older_policy_is_not_reused(db_session: Session) -> None:
    email = "ada.lovelace@engines.example"
    membership, _ = _setup(db_session, email)
    old = _seed_evidence(
        db_session,
        email,
        EmailVerificationResult.VALID,
        policy_version="ver-0",
    )
    provider = LiveProvider([_ok(email)])

    _run(db_session, provider)

    assert provider.calls == 1
    state = _stage(db_session, membership)
    assert state.status is PipelineStageStatus.COMPLETED
    assert state.output_reference is not None
    assert state.output_reference["verification_id"] != str(old.id)


def test_live_lookup_rejects_simulator_evidence(db_session: Session, settings: Settings) -> None:
    email = "ada.lovelace@engines.example"
    _seed_evidence(
        db_session,
        email,
        EmailVerificationResult.VALID,
        provider=SIMULATOR_PROVIDER_LABEL,
    )

    found = verification_service.find_fresh_evidence(
        db_session,
        email,
        get_policy(settings),
        datetime.now(UTC),
        required_provider_label=LIVE_PROVIDER_LABEL,
    )

    assert found is None


def test_reused_outcome_reports_the_evidence_rows_provider(
    db_session: Session,
) -> None:
    email = "ada.lovelace@engines.example"
    _, job = _setup(db_session, email)
    evidence = _seed_evidence(
        db_session,
        email,
        EmailVerificationResult.VALID,
        provider=LIVE_PROVIDER_LABEL,
    )
    provider = SimulatedProvider()
    claimed = agent_jobs.claim_job(
        db_session,
        job_id=job.id,
        worker_id=WORKER,
        lease_seconds=60,
    )
    assert claimed is job
    agent_jobs.start_job(db_session, job, worker_id=WORKER)
    job.email = email
    job.policy_version = POLICY
    db_session.flush()

    outcome = verification_service.verify_exact_address(
        db_session,
        job,
        provider=provider,
    )

    assert provider.calls == 0
    assert outcome.reused is True
    assert outcome.evidence is not None and outcome.evidence.id == evidence.id
    assert outcome.provider_label == LIVE_PROVIDER_LABEL
    assert outcome.simulated is False
    assert outcome.attempt is not None
    assert outcome.attempt.provider == LIVE_PROVIDER_LABEL


def test_force_refresh_bypasses_fresh_evidence_and_survives_the_queue(
    db_session: Session,
) -> None:
    """The instruction lives on the durable job, not the calling frame."""

    email = "ada.lovelace@engines.example"
    campaign, _, contact = _records(db_session)
    _candidate(db_session, contact, email)
    _enable_verification(db_session)
    membership = _enrol(db_session, campaign, contact)
    existing = _seed_evidence(db_session, email, EmailVerificationResult.VALID)
    job = _queue_verification(db_session, membership, force_refresh=True)
    assert job.input_reference["force_refresh"] is True
    provider = LiveProvider([_ok(email)])

    _run(db_session, provider)

    assert provider.calls == 1
    state = _stage(db_session, membership)
    assert state.output_reference is not None
    assert state.output_reference["reused_evidence"] is False
    assert state.output_reference["verification_id"] != str(existing.id)


# --- retries, failures and their ownership --------------------------------------


def test_a_transient_provider_failure_retries_through_the_common_queue(
    db_session: Session,
) -> None:
    email = "ada.lovelace@engines.example"
    membership, job = _setup(db_session, email)
    provider = LiveProvider([ProviderTransientError("connection reset")])

    _run(db_session, provider)

    assert job.status is AgentJobStatus.RETRY_SCHEDULED
    # The common queue owns the schedule, not the adapter.
    assert job.next_run_at > datetime.now(UTC)
    assert job.error is not None and job.error["retryable"] is True
    state = _stage(db_session, membership)
    assert state.status is PipelineStageStatus.RETRYING
    assert state.retryable is True
    # No evidence was manufactured from a failure to reach a verdict.
    assert db_session.scalars(select(ExactEmailVerification)).all() == []


def test_retry_exhaustion_becomes_a_terminal_failure(db_session: Session) -> None:
    email = "ada.lovelace@engines.example"
    membership, job = _setup(db_session, email)
    provider = LiveProvider()

    for _ in range(job.max_attempts):
        provider.script.append(ProviderTransientError("boom"))
        job.next_run_at = datetime.now(UTC) - timedelta(seconds=1)
        db_session.flush()
        _run(db_session, provider)

    assert job.status is AgentJobStatus.FAILED
    assert job.attempts == job.max_attempts
    state = _stage(db_session, membership)
    assert state.status is PipelineStageStatus.FAILED
    assert state.retryable is False


def test_insufficient_credits_is_a_terminal_no_result(db_session: Session) -> None:
    email = "ada.lovelace@engines.example"
    membership, job = _setup(db_session, email)
    provider = LiveProvider([_error(email, "insufficient_credits", credits=0)])

    _run(db_session, provider)

    assert job.status is AgentJobStatus.FAILED
    state = _stage(db_session, membership)
    assert state.status is PipelineStageStatus.FAILED
    assert state.reason_code == "verification_insufficient_credits"


def test_a_rejected_credential_is_terminal_and_never_retries(db_session: Session) -> None:
    email = "ada.lovelace@engines.example"
    membership, job = _setup(db_session, email)
    provider = LiveProvider([_error(email, "invalid_api_key")])

    _run(db_session, provider)

    assert provider.calls == 1
    assert job.status is AgentJobStatus.FAILED
    assert _stage(db_session, membership).status is PipelineStageStatus.FAILED


@pytest.mark.parametrize(
    ("failure_class", "expected"),
    [
        (VerificationFailureClass.NONE, False),
        (VerificationFailureClass.INVALID_INPUT, False),
        (VerificationFailureClass.POLICY_REFUSAL, False),
        (VerificationFailureClass.TRANSIENT_PROVIDER, True),
        (VerificationFailureClass.PERMANENT_PROVIDER, False),
        (VerificationFailureClass.INSUFFICIENT_CREDITS, False),
    ],
)
def test_only_transient_provider_failures_are_classified_retryable(
    failure_class: VerificationFailureClass, expected: bool
) -> None:
    assert job_attempts.is_retryable(failure_class) is expected


# --- the decision table itself --------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        EmailPreciseStatus.INVALID,
        EmailPreciseStatus.CATCH_ALL,
        EmailPreciseStatus.UNKNOWN,
        EmailPreciseStatus.DISPOSABLE,
        EmailPreciseStatus.ROLE_BASED,
        EmailPreciseStatus.STALE_EVIDENCE,
        EmailPreciseStatus.CONFLICTING_EVIDENCE,
        EmailPreciseStatus.PROVIDER_ERROR,
        EmailPreciseStatus.INSUFFICIENT_CREDITS,
    ],
)
def test_only_valid_can_ever_be_accepted(status: EmailPreciseStatus) -> None:
    assert decide(status).decision is not VerificationDecision.ACCEPT
    assert decide(status).accepted is False


def test_valid_is_accepted_only_when_it_is_not_simulated() -> None:
    assert decide(EmailPreciseStatus.VALID).decision is VerificationDecision.ACCEPT
    simulated = decide(EmailPreciseStatus.VALID, simulated=True)
    assert simulated.decision is VerificationDecision.REFUSED
    assert simulated.accepted is False


# --- idempotency and the common lifecycle ---------------------------------------


def test_the_verification_stage_enqueues_one_job_per_membership(db_session: Session) -> None:
    from app.services.agents import orchestrator

    membership, job = _setup(db_session)
    again = orchestrator.schedule_next(db_session, membership=membership, actor="test")

    assert again is not None
    assert again.id == job.id
    jobs_for_stage = db_session.scalars(
        select(AgentJob).where(
            AgentJob.campaign_contact_id == membership.id,
            AgentJob.agent_id == AgentIdentifier.VERIFICATION,
        )
    ).all()
    assert len(jobs_for_stage) == 1


def test_replaying_a_completed_verification_makes_no_second_provider_call(
    db_session: Session,
) -> None:
    membership, job = _setup(db_session)
    provider = LiveProvider()

    _run(db_session, provider)
    assert job.status is AgentJobStatus.SUCCEEDED
    # Nothing further is claimable for this membership's verification stage.
    second = _run(db_session, provider)

    assert provider.calls == 1
    assert second.agent_id is not AgentIdentifier.VERIFICATION or second.job is None  # type: ignore[attr-defined]


def test_the_job_carries_the_real_phase_two_requesting_relationship(
    db_session: Session,
) -> None:
    """A real Agent Job reference, not an opaque string."""

    membership, job = _setup(db_session)

    assert job.agent_id is AgentIdentifier.VERIFICATION
    assert job.campaign_contact_id == membership.id
    assert job.campaign_id == membership.campaign_id
    assert job.contact_id == membership.contact_id
    assert job.task_kind == "advance_campaign_contact"
    assert job.input_reference["agent_id"] == AgentIdentifier.VERIFICATION.value
    # The pre-Phase-2 seams are gone.
    assert not hasattr(job, "agent_type")
    assert not hasattr(job, "agent_contract_version")
    assert not hasattr(job, "requested_by_agent_job")
    assert not hasattr(job, "force_refresh")


def test_lease_recovery_returns_abandoned_verification_work(db_session: Session) -> None:
    from app.services.agents import jobs as agent_jobs

    membership, job = _setup(db_session)
    claimed = agent_jobs.claim_next_job(
        db_session,
        worker_id="dead-worker",
        lease_seconds=60,
        agent_ids=(AgentIdentifier.VERIFICATION,),
    )
    assert claimed is not None
    claimed.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()

    recovered = agent_jobs.recover_expired_leases(db_session)

    assert [row.id for row in recovered] == [job.id]
    assert job.lease_owner is None


def test_agent_pause_and_resume_reconciles_queued_verification_work(
    db_session: Session,
) -> None:
    from app.services.agents.orchestrator import reconcile_agent_control

    membership, job = _setup(db_session)

    controls.set_global_control(
        db_session,
        agent_id=AgentIdentifier.VERIFICATION,
        status=AgentControlStatus.PAUSED,
        config={"live": True},
    )
    reconcile_agent_control(db_session, agent_id=AgentIdentifier.VERIFICATION, actor="test")
    assert job.status is AgentJobStatus.PAUSED

    controls.set_global_control(
        db_session,
        agent_id=AgentIdentifier.VERIFICATION,
        status=AgentControlStatus.ENABLED,
        config={"live": True},
    )
    reconcile_agent_control(db_session, agent_id=AgentIdentifier.VERIFICATION, actor="test")
    assert job.status is AgentJobStatus.PENDING


# --- attempt history and secret safety ------------------------------------------


def test_attempt_history_records_what_the_provider_did(db_session: Session) -> None:
    email = "ada.lovelace@engines.example"
    membership, job = _setup(db_session, email)
    provider = LiveProvider([ProviderTransientError("connection reset")])

    _run(db_session, provider)
    job.next_run_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()
    provider.script.append(_ok(email))
    _run(db_session, provider)

    history = job_attempts.attempts_for_job(db_session, job.id)
    assert [row.attempt_number for row in history] == [1, 2]

    failed, succeeded = history
    assert failed.provider_called is True
    assert failed.failure_class is VerificationFailureClass.TRANSIENT_PROVIDER
    assert failed.verification_id is None
    assert failed.error_summary is not None and "connection reset" in failed.error_summary
    assert failed.provider == LIVE_PROVIDER_LABEL

    assert succeeded.failure_class is VerificationFailureClass.NONE
    assert succeeded.verification_result is EmailVerificationResult.VALID
    assert succeeded.verification_id is not None
    assert succeeded.reused_evidence is False
    # History is append-only: the earlier attempt is not rewritten.
    assert failed.precise_status == EmailPreciseStatus.PROVIDER_ERROR.value


def test_a_reused_answer_is_recorded_as_no_provider_call(db_session: Session) -> None:
    email = "ada.lovelace@engines.example"
    _, job = _setup(db_session, email)
    _seed_evidence(db_session, email, EmailVerificationResult.VALID)

    _run(db_session, LiveProvider())

    history = job_attempts.attempts_for_job(db_session, job.id)
    assert [row.provider_called for row in history] == [False]
    assert [row.reused_evidence for row in history] == [True]


def test_no_provider_credential_reaches_durable_text_or_the_pipeline(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MILLIONVERIFIER_API_KEY", REAL_KEY)
    get_settings.cache_clear()

    email = "ada.lovelace@engines.example"
    membership, job = _setup(db_session, email)
    # A provider that leaks the key into its failure, as a careless adapter might.
    provider = LiveProvider([ProviderTransientError(f"denied for api={REAL_KEY}")])

    _run(db_session, provider)

    stored = db_session.scalars(select(VerificationAttempt)).all()
    assert stored
    for row in stored:
        assert row.error_summary is not None
        assert REAL_KEY not in row.error_summary
        assert "***REDACTED***" in row.error_summary
    assert job.last_error is not None and REAL_KEY not in job.last_error
    state = _stage(db_session, membership)
    assert REAL_KEY not in (state.reason_detail or "")
    for event in db_session.scalars(select(PipelineEvent)).all():
        assert REAL_KEY not in (event.reason_detail or "")
        assert REAL_KEY not in str(event.detail)


# --- the end-to-end vertical path ------------------------------------------------


def test_email_candidate_to_verified_evidence_to_pipeline_outcome(
    db_session: Session,
) -> None:
    """Email Agent candidate → Verification Agent Job → evidence → decision → pipeline.

    The Email Agent itself is #224 and is not implemented here; a deterministic
    producer stands in for it by writing exactly the selected exact candidate the
    real Email Agent would commit.
    """

    email = "ada.lovelace@engines.example"
    campaign, _, contact = _records(db_session)

    # 1. Email Agent stand-in: one selected exact candidate.
    candidate = _candidate(db_session, contact, email)
    assert candidate.selected is True

    _enable_verification(db_session)
    membership = _enrol(db_session, campaign, contact)

    # 2. Verification Agent Job through the common queue.
    job = _queue_verification(db_session, membership)
    assert job.agent_id is AgentIdentifier.VERIFICATION
    assert job.status is AgentJobStatus.PENDING

    # 3. The common worker runs it end to end.
    provider = LiveProvider([_ok(email)])
    outcome = _run(db_session, provider)
    assert outcome.public_status == "completed"  # type: ignore[attr-defined]

    # 4. Exact verification evidence exists and is live-provenanced.
    evidence = db_session.scalars(
        select(ExactEmailVerification).where(ExactEmailVerification.email == email)
    ).one()
    assert evidence.result is EmailVerificationResult.VALID
    assert evidence.provider == LIVE_PROVIDER_LABEL
    assert evidence.provider != SIMULATOR_PROVIDER_LABEL

    # 5. The decision reached the Campaign Contact pipeline.
    snapshot = pipeline.pipeline_snapshot(db_session, campaign_contact_id=membership.id)
    assert snapshot is not None
    verification_state = next(
        state for state in snapshot.stages if state.agent_id is AgentIdentifier.VERIFICATION
    )
    assert verification_state.status is PipelineStageStatus.COMPLETED
    assert verification_state.output_reference is not None
    assert verification_state.output_reference["decision"] == VerificationDecision.ACCEPT.value
    assert verification_state.latest_job_id == job.id

    # 6. The event history explains what happened, in order.
    kinds = [event.event_type.value for event in snapshot.events]
    assert "job_queued" in kinds
    assert "job_started" in kinds
    assert "stage_completed" in kinds
    assert membership.latest_completed_stage is AgentIdentifier.VERIFICATION

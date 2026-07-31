"""Agent control precedence and a real Identity -> Company vertical path."""

from __future__ import annotations

import uuid

import pytest
from app.db.session import engine
from app.models.campaign import Campaign
from app.models.company import Company
from app.models.contact import Contact
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    AgentJobStatus,
    CampaignStatus,
    PipelineEventType,
    PipelineStageStatus,
    SuppressionReason,
    SuppressionType,
)
from app.models.pipeline import CampaignContactAgentState, PipelineEvent
from app.models.verification_job import AgentJob
from app.services import campaign_contacts, campaigns, pipeline
from app.services.agents import controls, locking
from app.services.agents.adapters import (
    AgentExecutionContext,
    AgentExecutionResult,
    AgentRetryableError,
)
from app.services.agents.orchestrator import (
    claim_next_campaign_job,
    execute_started_job,
    prepare_leased_job,
    reconcile_agent_control,
    run_next,
)
from app.services.suppressions import add_suppression
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def _job_status(job: AgentJob) -> AgentJobStatus:
    """Read mutable ORM state without retaining a prior assertion narrow."""

    return job.status


def _pipeline_status(state: CampaignContactAgentState) -> PipelineStageStatus:
    return state.status


def _records(db: Session) -> tuple[Campaign, Company, Contact]:
    company = Company(name="Analytical Engines", domain="engines.example")
    campaign = Campaign(
        name=f"Orchestration {uuid.uuid4()}",
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
        natural_key="ada|lovelace|engines.example",
    )
    db.add(contact)
    db.flush()
    return campaign, company, contact


def test_campaign_override_precedes_global_but_execution_switch_is_master(
    db_session: Session,
) -> None:
    campaign, _, _ = _records(db_session)
    controls.set_global_control(
        db_session,
        agent_id=AgentIdentifier.IDENTITY,
        status=AgentControlStatus.PAUSED,
        config={"batch_size": 10},
    )
    controls.set_campaign_override(
        db_session,
        campaign_id=campaign.id,
        agent_id=AgentIdentifier.IDENTITY,
        status=AgentControlStatus.ENABLED,
        config={"batch_size": 25},
    )
    effective = controls.effective_control(
        db_session,
        campaign=campaign,
        agent_id=AgentIdentifier.IDENTITY,
    )
    assert effective.status is AgentControlStatus.ENABLED
    assert effective.source == "campaign_override"
    assert effective.config == {"batch_size": 25}

    campaigns.set_campaign_execution(
        db_session,
        campaign.id,
        enabled=False,
        reason="operator safety stop",
    )
    stopped = controls.effective_control(
        db_session,
        campaign=campaign,
        agent_id=AgentIdentifier.IDENTITY,
    )
    assert stopped.status is AgentControlStatus.DISABLED
    assert stopped.source == "campaign_execution"
    assert stopped.reason == "operator safety stop"


def test_capture_control_cannot_disable_permanent_intake(db_session: Session) -> None:
    with pytest.raises(controls.AgentControlError, match="always preserve"):
        controls.set_global_control(
            db_session,
            agent_id=AgentIdentifier.CAPTURE,
            status=AgentControlStatus.DISABLED,
        )


def test_archiving_campaign_pauses_queued_work_and_projects_disabled_state(
    db_session: Session,
) -> None:
    campaign, _, contact = _records(db_session)
    enrolled = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=True,
        desired_stage=AgentIdentifier.IDENTITY,
    )
    job = enrolled.queued_job
    assert job is not None and _job_status(job) is AgentJobStatus.PENDING

    campaigns.update_campaign(
        db_session,
        campaign.id,
        status=CampaignStatus.ARCHIVED,
        actor="test",
        reason="campaign retired",
    )

    assert campaign.execution_enabled is False
    assert _job_status(job) is AgentJobStatus.PAUSED
    assert job.error_class == "agent_disabled"
    state = pipeline.agent_state(
        db_session,
        campaign_contact_id=enrolled.membership.id,
        agent_id=AgentIdentifier.IDENTITY,
    )
    assert state is not None
    assert _pipeline_status(state) is PipelineStageStatus.DISABLED
    assert state.reason_detail == "campaign retired"


def test_paused_control_pauses_and_override_resumes_only_its_job(
    db_session: Session,
) -> None:
    campaign, _, contact = _records(db_session)
    enrolled = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=True,
        desired_stage=AgentIdentifier.IDENTITY,
    )
    job = enrolled.queued_job
    assert job is not None and _job_status(job) is AgentJobStatus.PENDING

    controls.set_global_control(
        db_session,
        agent_id=AgentIdentifier.IDENTITY,
        status=AgentControlStatus.PAUSED,
        reason="maintenance",
    )
    reconcile_agent_control(
        db_session,
        agent_id=AgentIdentifier.IDENTITY,
        actor="test",
    )
    assert _job_status(job) is AgentJobStatus.PAUSED
    state = pipeline.agent_state(
        db_session,
        campaign_contact_id=enrolled.membership.id,
        agent_id=AgentIdentifier.IDENTITY,
    )
    assert state is not None and _pipeline_status(state) is PipelineStageStatus.PAUSED

    controls.set_campaign_override(
        db_session,
        campaign_id=campaign.id,
        agent_id=AgentIdentifier.IDENTITY,
        status=AgentControlStatus.ENABLED,
        reason="this Campaign may resume",
    )
    reconcile_agent_control(
        db_session,
        campaign_id=campaign.id,
        agent_id=AgentIdentifier.IDENTITY,
        actor="test",
    )
    assert _job_status(job) is AgentJobStatus.PENDING
    assert _pipeline_status(state) is PipelineStageStatus.WAITING


def test_real_identity_and_company_adapters_advance_durable_pipeline(
    db_session: Session,
) -> None:
    campaign, company, contact = _records(db_session)
    enrolled = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        source_reference="vertical-acceptance",
        enqueue=True,
        desired_stage=AgentIdentifier.COMPANY,
    )
    assert enrolled.queued_job is not None
    assert enrolled.queued_job.agent_id is AgentIdentifier.IDENTITY

    identity = run_next(db_session, worker_id="phase2-test")
    assert identity.public_status == "completed"
    assert identity.agent_id is AgentIdentifier.IDENTITY
    company_job = db_session.scalars(
        select(AgentJob).where(
            AgentJob.campaign_contact_id == enrolled.membership.id,
            AgentJob.agent_id == AgentIdentifier.COMPANY,
        )
    ).one()
    assert company_job.status is AgentJobStatus.PENDING

    company_result = run_next(db_session, worker_id="phase2-test")
    assert company_result.public_status == "completed"
    assert company_result.agent_id is AgentIdentifier.COMPANY
    assert contact.company_id == company.id
    assert enrolled.membership.latest_completed_stage is AgentIdentifier.COMPANY
    assert enrolled.membership.pipeline_status.value == PipelineStageStatus.COMPLETED.value
    assert enrolled.membership.next_stage is None

    snapshot = pipeline.pipeline_snapshot(
        db_session,
        campaign_contact_id=enrolled.membership.id,
    )
    assert snapshot is not None
    assert snapshot.next_action == "No further stage is required."
    assert {state.agent_id for state in snapshot.stages} >= {
        AgentIdentifier.CAPTURE,
        AgentIdentifier.IDENTITY,
        AgentIdentifier.COMPANY,
    }
    assert sum(event.event_type.value == "stage_completed" for event in snapshot.events) >= 3


def test_claim_running_and_domain_outcome_are_separate_durable_checkpoints(
    committed_session: Session,
) -> None:
    campaign, _, contact = _records(committed_session)
    enrolled = campaign_contacts.enrol_contact(
        committed_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=True,
        desired_stage=AgentIdentifier.IDENTITY,
    )
    assert enrolled.queued_job is not None
    job_id = enrolled.queued_job.id
    membership_id = enrolled.membership.id
    committed_session.commit()

    claimant = Session(bind=engine, expire_on_commit=False)
    try:
        claimed = claim_next_campaign_job(
            claimant,
            worker_id="restart-safe-worker",
        )
        assert claimed is not None and claimed.id == job_id
        claimant.commit()
    finally:
        claimant.close()

    starter = Session(bind=engine, expire_on_commit=False)
    try:
        locked = locking.lock_job_context(starter, job_id)
        assert locked is not None
        leased = locked.job
        assert leased.status is AgentJobStatus.LEASED
        assert (
            prepare_leased_job(
                starter,
                job=leased,
                worker_id="restart-safe-worker",
            )
            is None
        )
        starter.commit()
    finally:
        starter.close()

    observer = Session(bind=engine, expire_on_commit=False)
    try:
        running = observer.get(AgentJob, job_id)
        assert running is not None and running.status is AgentJobStatus.IN_PROGRESS
        running_membership = observer.get(type(enrolled.membership), membership_id)
        assert running_membership is not None
        assert running_membership.pipeline_status is PipelineStageStatus.RUNNING
    finally:
        observer.close()

    finisher = Session(bind=engine, expire_on_commit=False)
    try:
        locked = locking.lock_job_context(finisher, job_id)
        assert locked is not None
        running = locked.job
        completed = execute_started_job(
            finisher,
            job=running,
            worker_id="restart-safe-worker",
        )
        assert completed.public_status == "completed"
        finisher.commit()
    finally:
        finisher.close()

    verifier = Session(bind=engine, expire_on_commit=False)
    try:
        completed_job = verifier.get(AgentJob, job_id)
        assert completed_job is not None and completed_job.status is AgentJobStatus.SUCCEEDED
        completed_membership = verifier.get(type(enrolled.membership), membership_id)
        assert completed_membership is not None
        assert completed_membership.pipeline_status is PipelineStageStatus.COMPLETED
    finally:
        verifier.close()


def test_missing_company_evidence_blocks_then_resumes_when_evidence_arrives(
    db_session: Session,
) -> None:
    campaign = Campaign(
        name=f"Evidence resume {uuid.uuid4()}",
        status=CampaignStatus.ACTIVE,
        execution_enabled=True,
    )
    contact = Contact(
        first_name="Katherine",
        last_name="Johnson",
        company_name="NASA",
        company_domain=None,
        natural_key=None,
    )
    db_session.add_all([campaign, contact])
    db_session.flush()
    enrolled = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="capture",
        enqueue=True,
        desired_stage=AgentIdentifier.COMPANY,
    )

    assert run_next(db_session, worker_id="phase2-test").public_status == "completed"
    blocked = run_next(db_session, worker_id="phase2-test")
    assert blocked.public_status == "paused"
    company_job = blocked.job
    assert company_job is not None and company_job.error_class == "company_domain_missing"
    assert enrolled.membership.pipeline_status is PipelineStageStatus.BLOCKED

    company = Company(name="NASA", domain="nasa.example")
    db_session.add(company)
    contact.company_domain = company.domain
    contact.natural_key = "katherine|johnson|nasa.example"
    db_session.flush()
    resumed = campaign_contacts.reconcile_contact_memberships(
        db_session,
        contact_id=contact.id,
        actor="test-evidence",
    )
    assert resumed == 1
    assert _job_status(company_job) is AgentJobStatus.PENDING

    completed = run_next(db_session, worker_id="phase2-test")
    assert completed.public_status == "completed"
    assert contact.company_id == company.id
    assert enrolled.membership.pipeline_status.value == PipelineStageStatus.COMPLETED.value


def test_a_disabled_skippable_stage_is_stepped_over_automatically(
    db_session: Session,
) -> None:
    """A disabled skippable Agent must not park the Contact waiting for a human.

    This replaces an earlier test that asserted the opposite — that a disabled
    Research stage left ``next_stage`` pointing at Research so an operator could
    skip it by hand. That behaviour is correct for one Contact and unusable for a
    Campaign: two thousand Contacts meant two thousand identical skips before any
    downstream Agent could run, which made "disabled" indistinguishable from
    "broken" at the only scale that matters here.

    The guarantee is now: stepped over automatically, and the history says by
    what. ``reason_code`` distinguishes it from an operator's decision, so the
    audit trail never attributes a control's effect to a person.
    """

    campaign, _, contact = _records(db_session)
    enrolled = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=True,
        desired_stage=AgentIdentifier.RESEARCH,
    )
    run_next(db_session, worker_id="phase2-test")
    run_next(db_session, worker_id="phase2-test")

    research = pipeline.agent_state(
        db_session,
        campaign_contact_id=enrolled.membership.id,
        agent_id=AgentIdentifier.RESEARCH,
    )
    assert research is not None
    assert _pipeline_status(research) is PipelineStageStatus.SKIPPED
    assert research.reason_code == "control_disabled_autoskip"
    assert enrolled.membership.next_stage is None
    assert enrolled.membership.pipeline_status is PipelineStageStatus.COMPLETED

    # The skip is a committed event, not merely a column value.
    snapshot = pipeline.pipeline_snapshot(db_session, campaign_contact_id=enrolled.membership.id)
    skips = [
        event
        for event in snapshot.events
        if event.event_type is PipelineEventType.STAGE_SKIPPED
        and event.agent_id is AgentIdentifier.RESEARCH
    ]
    assert len(skips) == 1
    assert skips[0].detail.get("auto_skipped") is True


def test_a_paused_stage_still_waits_for_an_operator_to_skip_it(
    db_session: Session,
) -> None:
    """Pausing is a human saying "hold"; it must not be stepped over.

    Disabled and paused are different intentions. Disabled means "this Campaign
    does not use this stage"; paused means "stop here, I am dealing with it".
    Auto-skipping a pause would discard the instruction that was just given, so
    the manual skip — and its ``operator_skip`` attribution — stays the only way
    past it.
    """

    campaign, _, contact = _records(db_session)
    controls.set_global_control(
        db_session,
        agent_id=AgentIdentifier.RESEARCH,
        status=AgentControlStatus.PAUSED,
        reason="held while the operator reviews the research prompt",
    )
    enrolled = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=True,
        desired_stage=AgentIdentifier.RESEARCH,
    )
    run_next(db_session, worker_id="phase2-test")
    run_next(db_session, worker_id="phase2-test")

    assert enrolled.membership.next_stage is AgentIdentifier.RESEARCH
    research = pipeline.agent_state(
        db_session,
        campaign_contact_id=enrolled.membership.id,
        agent_id=AgentIdentifier.RESEARCH,
    )
    assert research is not None
    assert _pipeline_status(research) is PipelineStageStatus.PAUSED

    skipped = pipeline.skip_current_stage(
        db_session,
        membership=enrolled.membership,
        agent_id=AgentIdentifier.RESEARCH,
        reason="Research is outside the Phase 2 vertical acceptance path.",
    )
    assert skipped.status is PipelineStageStatus.SKIPPED
    assert skipped.reason_code == "operator_skip"
    assert enrolled.membership.pipeline_status is PipelineStageStatus.COMPLETED
    assert enrolled.membership.next_stage is None


def _research_in_flight(
    db: Session,
    campaign: Campaign,
    contact: Contact,
) -> tuple[campaign_contacts.EnrollmentResult, AgentJob]:
    """Drive a Contact to a durably RUNNING Research stage with a claimed job.

    Claim and Running are committed before an adapter is ever called, so this is
    an ordinary state for the pipeline to be found in — a worker restart or an
    expired lease leaves it behind — not a contrived one. The adapter is
    deliberately never executed: what is under test is what the Campaign controls
    do to a stage whose work has already started.
    """

    controls.set_global_control(
        db,
        agent_id=AgentIdentifier.RESEARCH,
        status=AgentControlStatus.ENABLED,
    )
    enrolled = campaign_contacts.enrol_contact(
        db,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=True,
        desired_stage=AgentIdentifier.RESEARCH,
    )
    run_next(db, worker_id="phase2-test")
    run_next(db, worker_id="phase2-test")
    assert enrolled.membership.next_stage is AgentIdentifier.RESEARCH

    claimed = claim_next_campaign_job(db, worker_id="phase2-test")
    assert claimed is not None and claimed.agent_id is AgentIdentifier.RESEARCH
    assert prepare_leased_job(db, job=claimed, worker_id="phase2-test") is None
    assert _job_status(claimed) is AgentJobStatus.IN_PROGRESS
    return enrolled, claimed


def _research_state(
    db: Session,
    enrolled: campaign_contacts.EnrollmentResult,
) -> PipelineStageStatus:
    state = pipeline.agent_state(
        db,
        campaign_contact_id=enrolled.membership.id,
        agent_id=AgentIdentifier.RESEARCH,
    )
    assert state is not None
    return _pipeline_status(state)


def test_pausing_a_campaign_does_not_skip_a_running_stage(db_session: Session) -> None:
    """Pausing a Campaign must not rewrite work that has already started.

    The master switch forces every Agent to DISABLED, and a disabled skippable
    Agent is normally stepped over. Applied to a Research stage a worker is
    already running, that meant asking for RUNNING -> SKIPPED: an illegal move
    that raised out of ``transition_stage`` and reached the operator as a 500 on
    the pause button, with the Campaign left neither paused nor running.
    """

    campaign, _, contact = _records(db_session)
    enrolled, job = _research_in_flight(db_session, campaign, contact)
    assert _research_state(db_session, enrolled) is PipelineStageStatus.RUNNING

    campaigns.set_campaign_execution(
        db_session,
        campaign.id,
        enabled=False,
        reason="operator pressed pause",
    )

    assert _research_state(db_session, enrolled) is PipelineStageStatus.RUNNING
    assert _job_status(job) is AgentJobStatus.IN_PROGRESS
    assert job.lease_owner == "phase2-test"
    # The Contact is still standing at Research, not moved past it.
    assert enrolled.membership.next_stage is AgentIdentifier.RESEARCH
    snapshot = pipeline.pipeline_snapshot(db_session, campaign_contact_id=enrolled.membership.id)
    assert snapshot is not None
    assert not [
        event
        for event in snapshot.events
        if event.event_type is PipelineEventType.STAGE_SKIPPED
        and event.agent_id is AgentIdentifier.RESEARCH
    ]


def test_resuming_a_campaign_continues_the_same_job_rather_than_a_second_one(
    db_session: Session,
) -> None:
    """Resume leaves worker-owned Running work intact and enqueues nothing new."""

    campaign, _, contact = _records(db_session)
    enrolled, job = _research_in_flight(db_session, campaign, contact)
    campaigns.set_campaign_execution(db_session, campaign.id, enabled=False, reason="pause")

    campaigns.set_campaign_execution(db_session, campaign.id, enabled=True, reason="resume")

    assert _research_state(db_session, enrolled) is PipelineStageStatus.RUNNING
    assert _job_status(job) is AgentJobStatus.IN_PROGRESS
    assert job.error_class is None
    assert job.lease_owner == "phase2-test"
    assert enrolled.membership.next_stage is AgentIdentifier.RESEARCH
    research_jobs = db_session.scalar(
        select(func.count(AgentJob.id)).where(
            AgentJob.campaign_contact_id == enrolled.membership.id,
            AgentJob.agent_id == AgentIdentifier.RESEARCH,
        )
    )
    assert research_jobs == 1


def test_pausing_a_campaign_does_not_step_over_a_queued_skippable_stage(
    db_session: Session,
) -> None:
    """A pause is temporary; SKIPPED is terminal.

    The queued case never raised, so it never announced itself — it silently
    discarded every skippable stage of every Contact in the Campaign, and resume
    could not undo it because no transition leads out of SKIPPED. Only a control
    that says this Campaign *does not use* the stage may step over it.
    """

    campaign, _, contact = _records(db_session)
    controls.set_global_control(
        db_session,
        agent_id=AgentIdentifier.RESEARCH,
        status=AgentControlStatus.ENABLED,
    )
    enrolled = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=True,
        desired_stage=AgentIdentifier.RESEARCH,
    )
    run_next(db_session, worker_id="phase2-test")
    run_next(db_session, worker_id="phase2-test")
    assert _research_state(db_session, enrolled) is PipelineStageStatus.WAITING

    campaigns.set_campaign_execution(db_session, campaign.id, enabled=False, reason="pause")

    assert _research_state(db_session, enrolled) is PipelineStageStatus.DISABLED
    assert enrolled.membership.next_stage is AgentIdentifier.RESEARCH

    campaigns.set_campaign_execution(db_session, campaign.id, enabled=True, reason="resume")

    assert _research_state(db_session, enrolled) is PipelineStageStatus.WAITING
    assert enrolled.membership.next_stage is AgentIdentifier.RESEARCH


def test_disabling_one_agent_does_not_skip_its_running_stage_either(
    db_session: Session,
) -> None:
    """The same illegal move is reachable from the per-Agent control.

    Stepping over a disabled skippable Agent is right for a stage that has not
    started. It is not a reason to declare a claimed, running job skipped, so the
    guard belongs to the state of the stage and not only to the master switch.
    """

    campaign, _, contact = _records(db_session)
    enrolled, job = _research_in_flight(db_session, campaign, contact)
    controls.set_global_control(
        db_session,
        agent_id=AgentIdentifier.RESEARCH,
        status=AgentControlStatus.DISABLED,
        reason="switched off while the prompt is checked",
    )

    reconcile_agent_control(
        db_session,
        agent_id=AgentIdentifier.RESEARCH,
        campaign_id=campaign.id,
        actor="test",
    )

    assert _research_state(db_session, enrolled) is PipelineStageStatus.RUNNING
    assert _job_status(job) is AgentJobStatus.IN_PROGRESS
    assert job.lease_owner == "phase2-test"
    assert enrolled.membership.next_stage is AgentIdentifier.RESEARCH


class _AlwaysFailIdentity:
    agent_id = AgentIdentifier.IDENTITY

    def execute(self, context: AgentExecutionContext) -> AgentExecutionResult:
        del context
        raise AgentRetryableError(
            "temporary_dependency",
            "The dependency is temporarily unavailable.",
        )


def test_pausing_a_campaign_keeps_a_failed_stage_failed(db_session: Session) -> None:
    """A Campaign containing a failed stage must still be pausable.

    Nothing about FAILED leads to DISABLED — a failure is answered by a retry or
    a re-run, not by a control — so projecting the master switch onto it raised
    the same ``PipelineStateError`` as the running stage did. One stopped Contact
    was enough to make the whole Campaign unpausable, which is the opposite of
    what a safety stop is for.
    """

    campaign, _, contact = _records(db_session)
    enrolled = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=True,
        desired_stage=AgentIdentifier.IDENTITY,
    )
    assert enrolled.queued_job is not None
    enrolled.queued_job.max_attempts = 1
    db_session.flush()
    run_next(
        db_session,
        worker_id="phase2-test",
        adapters={AgentIdentifier.IDENTITY: _AlwaysFailIdentity()},
    )
    identity = pipeline.agent_state(
        db_session,
        campaign_contact_id=enrolled.membership.id,
        agent_id=AgentIdentifier.IDENTITY,
    )
    assert identity is not None and _pipeline_status(identity) is PipelineStageStatus.FAILED

    campaigns.set_campaign_execution(db_session, campaign.id, enabled=False, reason="pause")

    assert _pipeline_status(identity) is PipelineStageStatus.FAILED
    assert identity.reason_code == "temporary_dependency"


def test_safety_critical_agent_cannot_be_deliberately_skipped(
    db_session: Session,
) -> None:
    campaign, _, contact = _records(db_session)
    enrolled = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=True,
        desired_stage=AgentIdentifier.EMAIL,
    )
    run_next(db_session, worker_id="phase2-test")
    run_next(db_session, worker_id="phase2-test")
    # Research is disabled and skippable, so the pipeline steps over it without
    # help and arrives at Email on its own.
    assert enrolled.membership.next_stage is AgentIdentifier.EMAIL

    with pytest.raises(pipeline.PipelineStateError, match="safety-critical"):
        pipeline.skip_current_stage(
            db_session,
            membership=enrolled.membership,
            agent_id=AgentIdentifier.EMAIL,
            reason="Attempted safety bypass.",
        )


def test_suppression_added_after_enqueue_blocks_before_adapter_execution(
    db_session: Session,
) -> None:
    campaign, _, contact = _records(db_session)
    contact.email = "ada@engines.example"
    enrolled = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=True,
        desired_stage=AgentIdentifier.IDENTITY,
    )
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value=contact.email,
        reason=SuppressionReason.OPT_OUT,
        source="late-opt-out",
    )

    outcome = run_next(db_session, worker_id="phase2-test")
    assert outcome.public_status == "paused"
    assert outcome.job is not None and outcome.job.error_class == "suppression"
    assert enrolled.membership.pipeline_status is PipelineStageStatus.BLOCKED
    assert enrolled.membership.latest_completed_stage is AgentIdentifier.CAPTURE


class _AlwaysRetryIdentity:
    agent_id = AgentIdentifier.IDENTITY

    def execute(self, context: AgentExecutionContext) -> AgentExecutionResult:
        del context
        raise AgentRetryableError(
            "temporary_dependency",
            "The dependency is temporarily unavailable.",
        )


def test_retryable_failure_projects_terminal_failure_at_limit(
    db_session: Session,
) -> None:
    campaign, _, contact = _records(db_session)
    enrolled = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=True,
        desired_stage=AgentIdentifier.IDENTITY,
    )
    assert enrolled.queued_job is not None
    enrolled.queued_job.max_attempts = 1
    db_session.flush()

    outcome = run_next(
        db_session,
        worker_id="phase2-test",
        adapters={AgentIdentifier.IDENTITY: _AlwaysRetryIdentity()},
    )
    assert outcome.public_status == "failed"
    assert enrolled.queued_job.status is AgentJobStatus.FAILED
    state = db_session.scalars(
        select(CampaignContactAgentState).where(
            CampaignContactAgentState.campaign_contact_id == enrolled.membership.id,
            CampaignContactAgentState.agent_id == AgentIdentifier.IDENTITY,
        )
    ).one()
    assert state.status is PipelineStageStatus.FAILED
    assert state.retryable is False
    assert state.reason_code == "temporary_dependency"


def test_worker_domain_outcome_and_queue_completion_roll_back_together(
    db_session: Session,
) -> None:
    campaign, _, contact = _records(db_session)
    enrolled = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=True,
        desired_stage=AgentIdentifier.IDENTITY,
    )
    assert enrolled.queued_job is not None
    original_event_count = db_session.scalar(select(func.count()).select_from(PipelineEvent))

    savepoint = db_session.begin_nested()
    outcome = run_next(db_session, worker_id="rollback-worker")
    assert outcome.public_status == "completed"
    assert enrolled.queued_job.status is AgentJobStatus.SUCCEEDED
    savepoint.rollback()
    db_session.expire_all()

    job = db_session.get(AgentJob, enrolled.queued_job.id)
    membership = db_session.get(type(enrolled.membership), enrolled.membership.id)
    assert job is not None and job.status is AgentJobStatus.PENDING
    assert membership is not None
    assert membership.latest_completed_stage is AgentIdentifier.CAPTURE
    assert membership.next_stage is AgentIdentifier.IDENTITY
    assert (
        db_session.scalar(select(func.count()).select_from(PipelineEvent)) == original_event_count
    )

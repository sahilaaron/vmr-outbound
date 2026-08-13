"""Common scheduling and execution framework for Campaign Contact Agents."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    AgentJobStatus,
    CampaignMembershipStatus,
    PipelineEventType,
    PipelineStageStatus,
)
from app.models.verification_job import AgentJob
from app.services.agents import jobs, locking
from app.services.agents.adapters import (
    DEFAULT_ADAPTERS,
    AgentAdapter,
    AgentBlocked,
    AgentExecutionContext,
    AgentExecutionError,
    AgentRetryableError,
    AgentWaiting,
)
from app.services.agents.controls import CAMPAIGN_EXECUTION_SOURCE, effective_control
from app.services.agents.registry import get_agent_spec
from app.services.campaign_contacts import refresh_eligibility
from app.services.insights.lineage import pins_from_ancestor
from app.services.pipeline import (
    agent_state,
    append_event,
    can_transition,
    dependencies_satisfied,
    transition_stage,
)
from app.services.verification.decisions import VerificationDecision


@dataclass(frozen=True)
class WorkerExecution:
    job: AgentJob | None
    public_status: str | None
    agent_id: AgentIdentifier | None
    campaign_contact_id: uuid.UUID | None
    message: str


_EMAIL_CHILD_WAIT_CODES = frozenset(
    {
        "waiting_on_verification",
        "retryable_verification_dependency",
        "verification_dependency_agent_disabled",
        "verification_dependency_agent_paused",
    }
)


def _email_parent_for_verification_child(
    session: Session,
    job: AgentJob,
) -> AgentJob | None:
    """Resolve the one supported nested Agent relationship."""

    if job.agent_id is not AgentIdentifier.VERIFICATION or job.parent_job_id is None:
        return None
    parent = session.get(AgentJob, job.parent_job_id)
    if (
        parent is None
        or parent.agent_id is not AgentIdentifier.EMAIL
        or parent.campaign_contact_id != job.campaign_contact_id
        or parent.contact_id != job.contact_id
    ):
        return None
    return parent


def _child_decision_detail(job: AgentJob) -> dict[str, object]:
    value: object
    if job.status is AgentJobStatus.SUCCEEDED:
        value = job.result
    else:
        error = job.error
        value = error.get("detail") if isinstance(error, dict) else None
    return dict(value) if isinstance(value, dict) else {}


def _notify_email_parent(
    session: Session,
    *,
    child: AgentJob,
    parent: AgentJob,
    membership: CampaignContact,
    actor: str,
) -> None:
    """Project a committed child disposition and wake its waiting parent."""

    detail = _child_decision_detail(child)
    decision = detail.get("decision")
    root = dict(parent.result or {})
    state = root.get("email_discovery")
    if child.status is AgentJobStatus.RETRY_SCHEDULED:
        root["domain_outcome"] = "retryable_verification_dependency"
        root["verification_job_id"] = str(child.id)
        parent.result = root
        append_event(
            session,
            campaign_contact_id=membership.id,
            agent_id=AgentIdentifier.EMAIL,
            job_id=parent.id,
            event_type=PipelineEventType.STAGE_WAITING,
            actor=actor,
            reason_code="retryable_verification_dependency",
            reason_detail=child.last_error,
            retryable=True,
            detail={
                "verification_job_id": str(child.id),
                "decision": decision,
            },
        )
        session.flush()
        return

    # Only a parent paused specifically for its child dependency is resumed.
    # Operator, membership, Agent-control and suppression pauses remain
    # authoritative even if the remote answer arrived meanwhile.
    previous_status = parent.status
    jobs.resume_paused(
        session,
        parent,
        reason_codes=_EMAIL_CHILD_WAIT_CODES,
    )
    if parent.status is not AgentJobStatus.PENDING:
        return
    root["domain_outcome"] = "waiting_on_verification"
    root["verification_job_id"] = str(child.id)
    if isinstance(state, dict):
        root["email_discovery"] = state
    parent.result = root
    transition_stage(
        session,
        membership=membership,
        agent_id=AgentIdentifier.EMAIL,
        target=PipelineStageStatus.WAITING,
        event_type=PipelineEventType.STAGE_WAITING,
        actor=actor,
        job=parent,
        reason_code="verification_child_committed",
        reason_detail="A child Verification decision is committed; Email can resume.",
        detail={
            "verification_job_id": str(child.id),
            "verification_job_status": child.status.value,
            "decision": decision,
            "parent_previous_status": previous_status.value,
        },
    )


def _append_child_event(
    session: Session,
    *,
    membership: CampaignContact,
    job: AgentJob,
    event_type: PipelineEventType,
    actor: str,
    reason_code: str | None = None,
    reason_detail: str | None = None,
    retryable: bool = False,
    detail: dict[str, object] | None = None,
) -> None:
    """Record child lifecycle without mutating the top-level stage projection."""

    append_event(
        session,
        campaign_contact_id=membership.id,
        agent_id=AgentIdentifier.VERIFICATION,
        job_id=job.id,
        event_type=event_type,
        actor=actor,
        reason_code=reason_code,
        reason_detail=reason_detail,
        retryable=retryable,
        detail=detail,
    )


def _project_email_control_outcome(
    job: AgentJob,
    *,
    status: AgentControlStatus,
    source: str,
) -> None:
    if job.agent_id is not AgentIdentifier.EMAIL:
        return
    if status is AgentControlStatus.PAUSED:
        outcome = "agent_paused"
    elif source == "campaign_override":
        outcome = "campaign_override_disabled"
    else:
        outcome = "agent_disabled"
    result = dict(job.result or {})
    result["domain_outcome"] = outcome
    result["control_source"] = source
    job.result = result


def _terminal_eligibility_block(membership: CampaignContact) -> str | None:
    for reason in membership.blocking_reasons or []:
        if isinstance(reason, dict) and reason.get("terminal") is True:
            return str(reason.get("detail") or reason.get("code") or "eligibility blocked")
    return None


def stage_job_key(
    campaign_contact_id: uuid.UUID, agent_id: AgentIdentifier, *, generation: int = 1
) -> str:
    """The idempotency key for one Campaign Contact's turn at one Agent.

    ``generation`` exists so a stage can be run *again* after the reason it failed has
    been fixed. The key used to be a hardcoded ``v1``, which made that impossible:
    ``enqueue_job`` is idempotent on the key, so re-queueing returned the same failed
    job and the contact never moved. Every ordinary caller stays on generation 1, so
    two workers scheduling the same stage concurrently still converge on one job —
    that convergence is the whole reason the key exists and must not be weakened to
    allow re-runs.
    """

    return f"pipeline:{campaign_contact_id}:{agent_id.value}:v{generation}"


def schedule_next(
    session: Session,
    *,
    membership: CampaignContact,
    actor: str = "system",
    parent_job: AgentJob | None = None,
    priority: int = 100,
    allow_enqueue: bool = True,
    allow_autoskip: bool = True,
    generation: int = 1,
) -> AgentJob | None:
    """Enqueue the next eligible Agent or persist why it cannot run.

    ``generation`` is passed through to :func:`stage_job_key`; only an explicit
    operator re-run ever raises it.

    ``allow_autoskip=False`` forbids the terminal auto-skip of a disabled
    skippable stage; the stage holds at ``DISABLED`` instead. Control
    reconciliation passes it. See the comment on that branch for why the two
    callers must differ.
    """

    if membership.membership_status is not CampaignMembershipStatus.ACTIVE:
        return None
    block_reason = _terminal_eligibility_block(membership)
    if block_reason:
        return None
    agent_id = membership.next_stage
    if agent_id is None:
        return None
    campaign = session.get(Campaign, membership.campaign_id)
    if campaign is None:  # pragma: no cover - protected by FK
        return None
    state = agent_state(
        session,
        campaign_contact_id=membership.id,
        agent_id=agent_id,
        create=True,
    )
    assert state is not None
    if state.status in {
        PipelineStageStatus.COMPLETED,
        PipelineStageStatus.SKIPPED,
    }:
        return None
    satisfied, dependency = dependencies_satisfied(
        session,
        campaign_contact_id=membership.id,
        agent_id=agent_id,
    )
    if not satisfied:
        assert dependency is not None
        state.waiting_on_agent = dependency
        state.reason_code = "dependency_wait"
        state.reason_detail = f"Waiting for {dependency.value} to complete."
        membership.pipeline_status = PipelineStageStatus.WAITING
        session.flush()
        return None

    control = effective_control(session, campaign=campaign, agent_id=agent_id)
    if control.status is AgentControlStatus.DISABLED:
        spec = get_agent_spec(agent_id)
        # A disabled Agent used to stop the Contact here and wait for a human to
        # skip it, one Contact at a time. At the scale this pipeline exists for
        # that is not a pause, it is a wall: a Campaign of two thousand Contacts
        # with Research switched off needed two thousand identical clicks before
        # anything downstream could run.
        #
        # So a disabled Agent the registry marks *skippable* is now stepped over
        # automatically, with a durable SKIPPED event recording that it was the
        # control — not an operator, and not a domain refusal — that caused it.
        # "Skippable" already means "the pipeline is sound without this stage";
        # honouring that is the whole point of declaring it.
        #
        # A disabled Agent that is NOT skippable still stops dead. Sending is the
        # case that matters: switching it off must mean nothing is sent, never
        # "silently proceed as though sending had happened".
        #
        # Two further cases are never stepped over, because in both the skip
        # would assert something untrue and SKIPPED is terminal — no transition
        # leads out of it, so neither is recoverable by resuming.
        #
        # A Campaign whose master execution switch is off is *paused*, not
        # configured without this stage. The operator pressed "Pause campaign";
        # auto-skipping would answer that by permanently discarding every
        # skippable stage of every Contact in it, and resuming would find the
        # work gone rather than where it was left.
        #
        # A stage that cannot legally reach SKIPPED from where it is has work
        # that already started — RUNNING above all. Treating a claimed, running
        # Research job as unstarted work to step over is exactly wrong: the job
        # exists, may still finish, and its stage is not the Campaign's to
        # rewrite from here. This used to raise ``PipelineStateError`` out of
        # transition_stage and surface as a 500 on the pause button.
        #
        # ``allow_autoskip`` is the fourth case, and it is about *who is asking*
        # rather than about the stage. The ordinary scheduler — a worker that
        # just finished a stage and is looking for the next one — steps over a
        # disabled skippable Agent for the reasons above, and keeps doing so.
        # Control reconciliation must not, and the difference is not a taste
        # question: ``reconcile_agent_control`` selects every matching
        # Campaign Contact and calls this function on each in one transaction,
        # so one operator click on "disable Research" would walk the entire
        # cohort into SKIPPED at once. SKIPPED is absorbing — pipeline.py gives
        # it an empty outgoing transition set — so enabling the Agent an hour
        # later recovers none of it. A Campaign of two thousand Contacts loses
        # two thousand stages to a single POST whose own message told the
        # operator that nothing is discarded.
        #
        # Disabling an Agent is a statement about what may run from now on, not
        # consent to destroy work that already exists. So when reconciliation
        # asks, a disabled skippable stage falls through to the DISABLED
        # projection immediately below and *holds* there — reversible, visible,
        # and picked up again by the enable branch of reconcile_agent_control,
        # which schedules it the moment the control comes back. The auto-skip
        # keeps the case it was written for: a Contact arriving at a stage this
        # Campaign was already configured not to use.
        if (
            allow_autoskip
            and spec.skippable
            and agent_id is not AgentIdentifier.CAPTURE
            and control.source != CAMPAIGN_EXECUTION_SOURCE
            and can_transition(state.status, PipelineStageStatus.SKIPPED)
        ):
            transition_stage(
                session,
                membership=membership,
                agent_id=agent_id,
                target=PipelineStageStatus.SKIPPED,
                event_type=PipelineEventType.STAGE_SKIPPED,
                actor=actor,
                reason_code="control_disabled_autoskip",
                reason_detail=(
                    control.reason
                    or f"{spec.display_name} is disabled and skippable; stepped over "
                    "automatically so the Contact could continue."
                ),
                detail={"control_source": control.source, "auto_skipped": True},
            )
            # Recursion, not a loop, because the checks at the top of this
            # function apply again to the stage we just advanced to. It is
            # bounded by the length of the pipeline: every SKIPPED transition
            # either moves next_stage forward or ends the pipeline.
            return schedule_next(
                session,
                membership=membership,
                actor=actor,
                parent_job=parent_job,
                priority=priority,
                allow_enqueue=allow_enqueue,
                allow_autoskip=allow_autoskip,
            )
        # A control projects itself onto the stage only where that is a legal
        # move. A stage that already failed keeps its failure: "disabled" would
        # overwrite the one durable fact an operator needs, and the control's
        # actual effect — that nothing is queued from here — holds either way.
        # This is the same hazard as the skip above, and the reason a pause used
        # to be able to 500 on a Campaign that merely contained a failed stage.
        if state.status is not PipelineStageStatus.DISABLED and can_transition(
            state.status, PipelineStageStatus.DISABLED
        ):
            transition_stage(
                session,
                membership=membership,
                agent_id=agent_id,
                target=PipelineStageStatus.DISABLED,
                event_type=PipelineEventType.AGENT_DISABLED,
                actor=actor,
                reason_code=control.source,
                reason_detail=control.reason or f"{agent_id.value} is disabled.",
            )
        return None
    if control.status is AgentControlStatus.PAUSED:
        if state.status is not PipelineStageStatus.PAUSED and can_transition(
            state.status, PipelineStageStatus.PAUSED
        ):
            transition_stage(
                session,
                membership=membership,
                agent_id=agent_id,
                target=PipelineStageStatus.PAUSED,
                event_type=PipelineEventType.AGENT_PAUSED,
                actor=actor,
                reason_code=control.source,
                reason_detail=control.reason or f"{agent_id.value} is paused.",
            )
        return None
    if state.status in {PipelineStageStatus.DISABLED, PipelineStageStatus.PAUSED}:
        transition_stage(
            session,
            membership=membership,
            agent_id=agent_id,
            target=PipelineStageStatus.WAITING,
            event_type=PipelineEventType.STAGE_WAITING,
            actor=actor,
            reason_code="control_enabled",
            reason_detail=f"{agent_id.value} is enabled and ready to queue.",
        )

    active = session.scalars(
        select(AgentJob)
        .where(
            AgentJob.campaign_contact_id == membership.id,
            AgentJob.agent_id == agent_id,
            AgentJob.status.in_(
                (
                    AgentJobStatus.PENDING,
                    AgentJobStatus.LEASED,
                    AgentJobStatus.IN_PROGRESS,
                    AgentJobStatus.RETRY_SCHEDULED,
                    AgentJobStatus.PAUSED,
                )
            ),
        )
        .order_by(AgentJob.created_at.desc())
    ).first()
    if active is not None:
        state.latest_job_id = active.id
        session.flush()
        return active if allow_enqueue else None
    if not allow_enqueue:
        state.reason_code = "enqueue_deferred"
        state.reason_detail = "The next Agent is eligible but queueing was deferred by the caller."
        membership.pipeline_status = PipelineStageStatus.WAITING
        membership.current_stage = agent_id
        membership.next_stage = agent_id
        session.flush()
        return None

    spec = get_agent_spec(agent_id)
    key = stage_job_key(membership.id, agent_id, generation=generation)
    input_reference: dict[str, object] = {
        "campaign_contact_id": str(membership.id),
        "contact_id": str(membership.contact_id),
        "campaign_id": str(membership.campaign_id),
        "agent_id": agent_id.value,
        "control_source": control.source,
        "global_control_version": control.global_version,
        "campaign_control_version": control.campaign_version,
    }
    if agent_id is AgentIdentifier.INSIGHTS:
        contact = session.get(Contact, membership.contact_id)
        if contact is not None and contact.company_id is not None:
            input_reference.update(
                pins_from_ancestor(
                    session,
                    parent_job=parent_job,
                    company_id=contact.company_id,
                )
            )

    job, created = jobs.enqueue_job(
        session,
        agent_id=agent_id,
        idempotency_key=key,
        task_kind="advance_campaign_contact",
        max_attempts=spec.max_attempts,
        priority=priority,
        campaign_id=membership.campaign_id,
        campaign_contact_id=membership.id,
        contact_id=membership.contact_id,
        company_id=None,
        capture_id=membership.source_capture_id,
        entity_type="campaign_contact",
        entity_id=membership.id,
        input_reference=input_reference,
        parent_job_id=parent_job.id if parent_job else None,
        actor=actor,
    )
    state.latest_job_id = job.id
    membership.pipeline_status = PipelineStageStatus.WAITING
    membership.current_stage = agent_id
    membership.next_stage = agent_id
    session.flush()
    if created:
        append_event(
            session,
            campaign_contact_id=membership.id,
            agent_id=agent_id,
            job_id=job.id,
            event_type=PipelineEventType.JOB_QUEUED,
            from_status=state.status,
            to_status=PipelineStageStatus.WAITING,
            reason_code="eligible",
            reason_detail=f"{spec.display_name} job queued.",
            actor=actor,
        )
    return job


def reconcile_agent_control(
    session: Session,
    *,
    agent_id: AgentIdentifier,
    campaign_id: uuid.UUID | None = None,
    campaign_contact_ids: Iterable[uuid.UUID] | None = None,
    actor: str = "system",
) -> int:
    """Project a changed Agent control onto affected memberships and jobs.

    Safety controls take effect in the same transaction as the control write.
    Re-enabling resumes only jobs paused by a control; domain-blocked jobs keep
    their original reason and require their own resolution.

    Turning an Agent off holds the memberships already standing at it; it never
    skips them. Every ``schedule_next`` call made from the disable path passes
    ``allow_autoskip=False`` for that reason.
    """

    relevant_job = exists(
        select(AgentJob.id).where(
            AgentJob.campaign_contact_id == CampaignContact.id,
            AgentJob.agent_id == agent_id,
            AgentJob.status.in_(
                (
                    AgentJobStatus.PENDING,
                    AgentJobStatus.LEASED,
                    AgentJobStatus.IN_PROGRESS,
                    AgentJobStatus.RETRY_SCHEDULED,
                    AgentJobStatus.PAUSED,
                )
            ),
        )
    )
    statement = select(CampaignContact).where(
        CampaignContact.membership_status == CampaignMembershipStatus.ACTIVE,
        or_(CampaignContact.next_stage == agent_id, relevant_job),
    )
    if campaign_id is not None:
        statement = statement.where(CampaignContact.campaign_id == campaign_id)
    scoped_memberships = tuple(campaign_contact_ids or ())
    if scoped_memberships:
        statement = statement.where(CampaignContact.id.in_(scoped_memberships))
    memberships = list(
        session.scalars(statement.order_by(CampaignContact.id).with_for_update()).all()
    )
    now = datetime.now(UTC)
    changed = 0
    for membership in memberships:
        campaign = session.get(Campaign, membership.campaign_id)
        if campaign is None:  # pragma: no cover - protected by FK
            continue
        control = effective_control(session, campaign=campaign, agent_id=agent_id)
        if control.status is AgentControlStatus.ENABLED:
            controlled_jobs = list(
                locking.lock_agent_jobs(
                    session,
                    select(AgentJob).where(
                        AgentJob.campaign_contact_id == membership.id,
                        AgentJob.agent_id == agent_id,
                        AgentJob.status == AgentJobStatus.PAUSED,
                    ),
                )
            )
            for job in controlled_jobs:
                if job.status is AgentJobStatus.PAUSED and job.error_class in {
                    "agent_disabled",
                    "agent_paused",
                }:
                    job.status = AgentJobStatus.PENDING
                    job.next_run_at = now
                    job.last_error = None
                    job.error = None
                    job.error_class = None
                    changed += 1
            if membership.next_stage == agent_id:
                schedule_next(session, membership=membership, actor=actor)
            continue

        # Leased and Running work belongs to its worker until that worker reaches
        # its next safety gate or commits its outcome.  Reconciliation never clears
        # the lease or rewrites RUNNING stage state underneath it.
        in_flight = bool(
            session.scalar(
                select(
                    exists().where(
                        AgentJob.campaign_contact_id == membership.id,
                        AgentJob.agent_id == agent_id,
                        AgentJob.status.in_((AgentJobStatus.LEASED, AgentJobStatus.IN_PROGRESS)),
                    )
                )
            )
        )
        controlled_jobs = list(
            locking.lock_agent_jobs(
                session,
                select(AgentJob).where(
                    AgentJob.campaign_contact_id == membership.id,
                    AgentJob.agent_id == agent_id,
                    AgentJob.status.in_(
                        (
                            AgentJobStatus.PENDING,
                            AgentJobStatus.RETRY_SCHEDULED,
                            AgentJobStatus.PAUSED,
                        )
                    ),
                ),
            )
        )
        reason = control.reason or f"{agent_id.value} is {control.status.value}."
        reason_code = f"agent_{control.status.value}"
        has_domain_pause = False
        for job in controlled_jobs:
            if (
                job.status is AgentJobStatus.PAUSED
                and job.error_class
                not in {
                    "agent_disabled",
                    "agent_paused",
                }
                | _EMAIL_CHILD_WAIT_CODES
            ):
                # Effective controls are exposed separately; do not erase a
                # dependency or eligibility pause that already explains the
                # Campaign Contact's domain state.
                has_domain_pause = True
                continue
            if job.status is not AgentJobStatus.PAUSED or job.error_class != reason_code:
                _project_email_control_outcome(
                    job,
                    status=control.status,
                    source=control.source,
                )
                jobs.mark_paused(
                    session,
                    job,
                    reason=reason,
                    reason_code=reason_code,
                )
                changed += 1
        if membership.next_stage == agent_id and not has_domain_pause and not in_flight:
            # Reconciliation projects the control onto the stage; it never
            # discards the stage. ``allow_autoskip=False`` is what keeps that
            # true for a *skippable* Agent, whose disabled auto-skip is terminal
            # and would apply here to every selected Campaign Contact at once.
            schedule_next(
                session,
                membership=membership,
                actor=actor,
                allow_autoskip=False,
            )
    if memberships:
        session.flush()
    return changed


def _stage_after_handled_queue_result(
    session: Session,
    *,
    membership: CampaignContact,
    job: AgentJob,
    output_reference: dict[str, object],
    actor: str,
) -> PipelineStageStatus:
    if job.status is AgentJobStatus.SUCCEEDED:
        transition_stage(
            session,
            membership=membership,
            agent_id=job.agent_id,
            target=PipelineStageStatus.COMPLETED,
            event_type=PipelineEventType.STAGE_COMPLETED,
            actor=actor,
            job=job,
            output_reference=output_reference,
        )
        return PipelineStageStatus.COMPLETED
    if job.status is AgentJobStatus.RETRY_SCHEDULED:
        transition_stage(
            session,
            membership=membership,
            agent_id=job.agent_id,
            target=PipelineStageStatus.RETRYING,
            event_type=PipelineEventType.RETRY_SCHEDULED,
            actor=actor,
            job=job,
            reason_code=job.error_class or "verification_transient",
            reason_detail=job.last_error,
            retryable=True,
        )
        return PipelineStageStatus.RETRYING
    transition_stage(
        session,
        membership=membership,
        agent_id=job.agent_id,
        target=PipelineStageStatus.FAILED,
        event_type=PipelineEventType.FAILED_TERMINAL,
        actor=actor,
        job=job,
        reason_code=job.error_class or "verification_failed",
        reason_detail=job.last_error,
        retryable=False,
    )
    return PipelineStageStatus.FAILED


def _complete_verification_from_email(
    session: Session,
    *,
    membership: CampaignContact,
    email_job: AgentJob,
    actor: str,
) -> bool:
    """Project an Email-accepted child result onto the shared Verification stage."""

    if membership.next_stage is not AgentIdentifier.VERIFICATION:
        return False
    result = email_job.result or {}
    outcome = result.get("domain_outcome")

    # --- The imported-address bypass (IMP-001) -------------------------------
    #
    # An address that arrived in a contact file has no verification evidence and
    # is not going to acquire any: this path never calls MillionVerifier,
    # ZeroBounce or any other provider. The stage still has to reach a legal
    # terminal state or the Contact stops here forever, so it completes through
    # an outcome that says exactly what happened and claims nothing about the
    # mailbox. It is kept separate from the branch below precisely because that
    # branch REQUIRES an evidence reference — and inventing one to satisfy it is
    # the failure this whole design exists to prevent.
    if outcome == "imported_email_accepted":
        imported_email_id = result.get("imported_email_id")
        if not isinstance(imported_email_id, str):
            raise jobs.AgentJobError(
                "an imported-email acceptance cannot advance Verification without its "
                "imported-address evidence reference"
            )
        bypass_reference = {
            "decision": "bypassed",
            "verification_id": None,
            "email": result.get("email"),
            "provider": None,
            "provider_called": False,
            "reused_evidence": False,
            "source": "campaign_file_import",
            "imported_email_id": imported_email_id,
            "import_batch_id": result.get("import_batch_id"),
            "source_schema": result.get("source_schema"),
            "provider_claimed_status": result.get("provider_claimed_status"),
            "email_job_id": str(email_job.id),
        }
        transition_stage(
            session,
            membership=membership,
            agent_id=AgentIdentifier.VERIFICATION,
            target=PipelineStageStatus.COMPLETED,
            event_type=PipelineEventType.STAGE_COMPLETED,
            actor=actor,
            job=None,
            reason_code="verification_bypassed_imported_email",
            reason_detail=(
                "The address was supplied by a contact file import. No verification "
                "provider was called and no deliverability is claimed."
            ),
            output_reference=bypass_reference,
            detail=bypass_reference,
        )
        return True

    if outcome not in {
        "existing_accepted_email_reused",
        "verified_email_accepted",
    }:
        return False
    verification_id = result.get("verification_id")
    if not isinstance(verification_id, str):
        raise jobs.AgentJobError(
            "Email acceptance cannot advance Verification without an evidence reference"
        )

    verification_job: AgentJob | None = None
    raw_child_id = result.get("verification_job_id")
    if isinstance(raw_child_id, str):
        try:
            child_id = uuid.UUID(raw_child_id)
        except ValueError as exc:
            raise jobs.AgentJobError("Email result has an invalid Verification child id") from exc
        verification_job = session.get(AgentJob, child_id)
        if (
            verification_job is None
            or verification_job.parent_job_id != email_job.id
            or verification_job.agent_id is not AgentIdentifier.VERIFICATION
            or verification_job.status is not AgentJobStatus.SUCCEEDED
            or (verification_job.result or {}).get("decision") != VerificationDecision.ACCEPT.value
            or (verification_job.result or {}).get("verification_id") != verification_id
        ):
            raise jobs.AgentJobError(
                "Email acceptance does not match its committed Verification child"
            )

    output_reference = {
        "decision": VerificationDecision.ACCEPT.value,
        "verification_id": verification_id,
        "email": result.get("email"),
        "provider": result.get("verification_provider"),
        "policy_version": result.get("verification_policy_version"),
        "reused_evidence": result.get("domain_outcome") == "existing_accepted_email_reused",
        "source": (
            "email_agent_existing_evidence"
            if result.get("domain_outcome") == "existing_accepted_email_reused"
            else "email_agent_child"
        ),
        "email_job_id": str(email_job.id),
    }
    transition_stage(
        session,
        membership=membership,
        agent_id=AgentIdentifier.VERIFICATION,
        target=PipelineStageStatus.COMPLETED,
        event_type=PipelineEventType.STAGE_COMPLETED,
        actor=actor,
        job=verification_job,
        reason_code="email_agent_verified_address",
        reason_detail="Email Agent committed a production-eligible Verification result.",
        output_reference=output_reference,
        detail=output_reference,
    )
    return True


def _prepare_email_verification_child(
    session: Session,
    *,
    job: AgentJob,
    parent: AgentJob,
    membership: CampaignContact,
    campaign: Campaign,
    worker_id: str,
) -> WorkerExecution | None:
    """Prepare a nested Verification job without advancing its top-level stage."""

    if membership.membership_status is not CampaignMembershipStatus.ACTIVE:
        reason = f"Campaign Contact is {membership.membership_status.value}."
        jobs.mark_paused(session, job, reason=reason, reason_code="membership_paused")
        _append_child_event(
            session,
            membership=membership,
            job=job,
            event_type=PipelineEventType.MEMBERSHIP_PAUSED,
            actor=worker_id,
            reason_code="membership_status",
            reason_detail=reason,
        )
        return WorkerExecution(
            job=job,
            public_status=jobs.public_status(job),
            agent_id=job.agent_id,
            campaign_contact_id=membership.id,
            message=reason,
        )

    if refresh_eligibility(session, membership=membership, actor=worker_id):
        reason_code, reason = next(
            (
                (
                    str(item.get("code") or "eligibility_block"),
                    str(item.get("detail") or "The Campaign Contact is blocked."),
                )
                for item in membership.blocking_reasons
                if isinstance(item, dict) and item.get("terminal") is True
            ),
            ("eligibility_block", "The Campaign Contact is blocked."),
        )
        jobs.mark_paused(session, job, reason=reason, reason_code=reason_code)
        _append_child_event(
            session,
            membership=membership,
            job=job,
            event_type=PipelineEventType.ELIGIBILITY_BLOCKED,
            actor=worker_id,
            reason_code=reason_code,
            reason_detail=reason,
        )
        return WorkerExecution(
            job=job,
            public_status=jobs.public_status(job),
            agent_id=job.agent_id,
            campaign_contact_id=membership.id,
            message=reason,
        )

    parent_control = effective_control(
        session,
        campaign=campaign,
        agent_id=AgentIdentifier.EMAIL,
    )
    if parent_control.status is not AgentControlStatus.ENABLED:
        reason = (
            parent_control.reason or f"The requesting Email Agent is {parent_control.status.value}."
        )
        _project_email_control_outcome(
            parent,
            status=parent_control.status,
            source=parent_control.source,
        )
        jobs.mark_paused(
            session,
            job,
            reason=reason,
            reason_code=f"requesting_email_agent_{parent_control.status.value}",
        )
        _append_child_event(
            session,
            membership=membership,
            job=job,
            event_type=(
                PipelineEventType.AGENT_PAUSED
                if parent_control.status is AgentControlStatus.PAUSED
                else PipelineEventType.AGENT_DISABLED
            ),
            actor=worker_id,
            reason_code=parent_control.source,
            reason_detail=reason,
            detail={"parent_job_id": str(parent.id)},
        )
        return WorkerExecution(
            job=job,
            public_status=jobs.public_status(job),
            agent_id=job.agent_id,
            campaign_contact_id=membership.id,
            message=reason,
        )

    control = effective_control(
        session,
        campaign=campaign,
        agent_id=AgentIdentifier.VERIFICATION,
    )
    if control.status is not AgentControlStatus.ENABLED:
        reason = control.reason or f"verification is {control.status.value}."
        jobs.mark_paused(
            session,
            job,
            reason=reason,
            reason_code=f"agent_{control.status.value}",
        )
        _append_child_event(
            session,
            membership=membership,
            job=job,
            event_type=(
                PipelineEventType.AGENT_PAUSED
                if control.status is AgentControlStatus.PAUSED
                else PipelineEventType.AGENT_DISABLED
            ),
            actor=worker_id,
            reason_code=control.source,
            reason_detail=reason,
            detail={"parent_job_id": str(parent.id)},
        )
        _notify_email_parent(
            session,
            child=job,
            parent=parent,
            membership=membership,
            actor=worker_id,
        )
        return WorkerExecution(
            job=job,
            public_status=jobs.public_status(job),
            agent_id=job.agent_id,
            campaign_contact_id=membership.id,
            message=reason,
        )

    reclaimed = jobs.lease_was_reclaimed(job)
    _append_child_event(
        session,
        membership=membership,
        job=job,
        event_type=PipelineEventType.JOB_LEASED,
        actor=worker_id,
        reason_code="lease_reclaimed" if reclaimed else "worker_claim",
        reason_detail=(
            "A replacement worker reclaimed this Verification child." if reclaimed else None
        ),
        detail={
            "worker_id": worker_id,
            "attempt": job.attempts,
            "lease_reclaimed": reclaimed,
            "parent_job_id": str(parent.id),
        },
    )
    jobs.start_job(session, job, worker_id=worker_id)
    _append_child_event(
        session,
        membership=membership,
        job=job,
        event_type=PipelineEventType.JOB_STARTED,
        actor=worker_id,
        reason_code="worker_started",
        detail={
            "worker_id": worker_id,
            "attempt": job.attempts,
            "parent_job_id": str(parent.id),
        },
    )
    return None


def prepare_leased_job(
    session: Session,
    *,
    job: AgentJob,
    worker_id: str,
) -> WorkerExecution | None:
    """Validate a lease and durably project the job into Running.

    ``None`` means the job is ready for adapter execution. A
    :class:`WorkerExecution` means a safety gate paused or failed it instead.
    """

    locked_context = locking.lock_job_context(session, job.id)
    if locked_context is None:
        raise jobs.AgentJobNotFound(f"job {job.id} does not exist")
    job = locked_context.job

    membership = (
        session.get(CampaignContact, job.campaign_contact_id)
        if job.campaign_contact_id is not None
        else None
    )
    if membership is None:
        jobs.start_job(session, job, worker_id=worker_id)
        jobs.mark_failed(
            session,
            job,
            error_class="campaign_contact_missing",
            reason="The job has no live Campaign Contact.",
        )
        return WorkerExecution(
            job=job,
            public_status=jobs.public_status(job),
            agent_id=job.agent_id,
            campaign_contact_id=job.campaign_contact_id,
            message="Job failed: Campaign Contact is missing.",
        )
    campaign = session.get(Campaign, membership.campaign_id)
    contact = session.get(Contact, membership.contact_id)
    if campaign is None or contact is None:  # pragma: no cover - protected by FKs
        jobs.start_job(session, job, worker_id=worker_id)
        jobs.mark_failed(
            session,
            job,
            error_class="domain_record_missing",
            reason="A required Campaign or Contact record is missing.",
        )
        return WorkerExecution(
            job=job,
            public_status=jobs.public_status(job),
            agent_id=job.agent_id,
            campaign_contact_id=membership.id,
            message="Job failed: required domain record is missing.",
        )

    parent = _email_parent_for_verification_child(session, job)
    if parent is not None:
        return _prepare_email_verification_child(
            session,
            job=job,
            parent=parent,
            membership=membership,
            campaign=campaign,
            worker_id=worker_id,
        )

    if membership.membership_status is not CampaignMembershipStatus.ACTIVE:
        reason = f"Campaign Contact is {membership.membership_status.value}."
        jobs.mark_paused(session, job, reason=reason, reason_code="membership_paused")
        transition_stage(
            session,
            membership=membership,
            agent_id=job.agent_id,
            target=PipelineStageStatus.PAUSED,
            event_type=PipelineEventType.MEMBERSHIP_PAUSED,
            actor=worker_id,
            job=job,
            reason_code="membership_status",
            reason_detail=reason,
        )
        return WorkerExecution(
            job=job,
            public_status=jobs.public_status(job),
            agent_id=job.agent_id,
            campaign_contact_id=membership.id,
            message=reason,
        )

    terminal_block = refresh_eligibility(
        session,
        membership=membership,
        actor=worker_id,
    )
    if terminal_block:
        terminal_reason = next(
            (
                (
                    str(item.get("code") or "eligibility_block"),
                    str(item.get("detail") or "The Campaign Contact is blocked."),
                )
                for item in membership.blocking_reasons
                if isinstance(item, dict) and item.get("terminal") is True
            ),
            ("eligibility_block", "The Campaign Contact is blocked."),
        )
        reason_code, reason = terminal_reason
        jobs.mark_paused(
            session,
            job,
            reason=reason,
            reason_code=reason_code,
        )
        transition_stage(
            session,
            membership=membership,
            agent_id=job.agent_id,
            target=PipelineStageStatus.BLOCKED,
            event_type=PipelineEventType.ELIGIBILITY_BLOCKED,
            actor=worker_id,
            job=job,
            reason_code=reason_code,
            reason_detail=reason,
        )
        return WorkerExecution(
            job=job,
            public_status=jobs.public_status(job),
            agent_id=job.agent_id,
            campaign_contact_id=membership.id,
            message=reason,
        )

    control = effective_control(session, campaign=campaign, agent_id=job.agent_id)
    if control.status is not AgentControlStatus.ENABLED:
        reason = control.reason or f"{job.agent_id.value} is {control.status.value}."
        _project_email_control_outcome(
            job,
            status=control.status,
            source=control.source,
        )
        jobs.mark_paused(
            session,
            job,
            reason=reason,
            reason_code=f"agent_{control.status.value}",
        )
        target = (
            PipelineStageStatus.PAUSED
            if control.status is AgentControlStatus.PAUSED
            else PipelineStageStatus.DISABLED
        )
        transition_stage(
            session,
            membership=membership,
            agent_id=job.agent_id,
            target=target,
            event_type=(
                PipelineEventType.AGENT_PAUSED
                if target is PipelineStageStatus.PAUSED
                else PipelineEventType.AGENT_DISABLED
            ),
            actor=worker_id,
            job=job,
            reason_code=control.source,
            reason_detail=reason,
        )
        return WorkerExecution(
            job=job,
            public_status=jobs.public_status(job),
            agent_id=job.agent_id,
            campaign_contact_id=membership.id,
            message=reason,
        )

    satisfied, dependency = dependencies_satisfied(
        session,
        campaign_contact_id=membership.id,
        agent_id=job.agent_id,
    )
    if not satisfied:
        assert dependency is not None
        reason = f"Waiting for {dependency.value} to complete."
        jobs.mark_paused(session, job, reason=reason, reason_code="dependency_wait")
        state = agent_state(
            session,
            campaign_contact_id=membership.id,
            agent_id=job.agent_id,
            create=True,
        )
        assert state is not None
        state.waiting_on_agent = dependency
        state.reason_code = "dependency_wait"
        state.reason_detail = reason
        session.flush()
        return WorkerExecution(
            job=job,
            public_status=jobs.public_status(job),
            agent_id=job.agent_id,
            campaign_contact_id=membership.id,
            message=reason,
        )

    reclaimed = jobs.lease_was_reclaimed(job)
    append_event(
        session,
        campaign_contact_id=membership.id,
        agent_id=job.agent_id,
        job_id=job.id,
        event_type=PipelineEventType.JOB_LEASED,
        actor=worker_id,
        reason_code=("lease_reclaimed" if reclaimed else "worker_claim"),
        reason_detail=(
            "A replacement worker reclaimed this job after its prior lease expired."
            if reclaimed
            else None
        ),
        detail={
            "worker_id": worker_id,
            "attempt": job.attempts,
            "lease_reclaimed": reclaimed,
        },
    )
    jobs.start_job(session, job, worker_id=worker_id)
    transition_stage(
        session,
        membership=membership,
        agent_id=job.agent_id,
        target=PipelineStageStatus.RUNNING,
        event_type=PipelineEventType.JOB_STARTED,
        actor=worker_id,
        job=job,
        reason_code="worker_started",
        detail={"worker_id": worker_id, "attempt": job.attempts},
    )
    return None


def _execute_email_verification_child(
    session: Session,
    *,
    job: AgentJob,
    parent: AgentJob,
    membership: CampaignContact,
    campaign: Campaign,
    contact: Contact,
    worker_id: str,
    adapters: dict[AgentIdentifier, AgentAdapter],
) -> WorkerExecution:
    """Execute one already-started child through Verification's real adapter."""

    # Controls and suppression can change after the durable Running checkpoint.
    # Recheck immediately before the adapter (and therefore before any provider
    # call) while leaving the Email stage as the top-level projection.
    parent_control = effective_control(
        session,
        campaign=campaign,
        agent_id=AgentIdentifier.EMAIL,
    )
    if parent_control.status is not AgentControlStatus.ENABLED:
        reason = (
            parent_control.reason or f"The requesting Email Agent is {parent_control.status.value}."
        )
        _project_email_control_outcome(
            parent,
            status=parent_control.status,
            source=parent_control.source,
        )
        jobs.mark_paused(
            session,
            job,
            reason=reason,
            reason_code=f"requesting_email_agent_{parent_control.status.value}",
        )
        _append_child_event(
            session,
            membership=membership,
            job=job,
            event_type=(
                PipelineEventType.AGENT_PAUSED
                if parent_control.status is AgentControlStatus.PAUSED
                else PipelineEventType.AGENT_DISABLED
            ),
            actor=worker_id,
            reason_code=parent_control.source,
            reason_detail=reason,
            detail={"parent_job_id": str(parent.id)},
        )
        return WorkerExecution(
            job=job,
            public_status=jobs.public_status(job),
            agent_id=job.agent_id,
            campaign_contact_id=membership.id,
            message=reason,
        )

    control = effective_control(
        session,
        campaign=campaign,
        agent_id=AgentIdentifier.VERIFICATION,
    )
    if control.status is not AgentControlStatus.ENABLED:
        error = AgentBlocked(
            f"agent_{control.status.value}",
            control.reason or f"verification is {control.status.value}.",
        )
        return _handle_execution_error(
            session,
            membership=membership,
            job=job,
            error=error,
            actor=worker_id,
        )
    if refresh_eligibility(session, membership=membership, actor=worker_id):
        reason = _terminal_eligibility_block(membership) or "Eligibility is blocked."
        return _handle_execution_error(
            session,
            membership=membership,
            job=job,
            error=AgentBlocked("eligibility_block", reason),
            actor=worker_id,
        )

    adapter = adapters.get(AgentIdentifier.VERIFICATION)
    if adapter is None:
        return _handle_execution_error(
            session,
            membership=membership,
            job=job,
            error=AgentExecutionError(
                "adapter_missing",
                "No executable adapter is registered for verification.",
            ),
            actor=worker_id,
        )

    try:
        preserved_error: AgentExecutionError | None = None
        with session.begin_nested():
            try:
                result = adapter.execute(
                    AgentExecutionContext(
                        session=session,
                        job=job,
                        campaign=campaign,
                        membership=membership,
                        contact=contact,
                        config=control.config,
                        worker_id=worker_id,
                    )
                )
            except AgentExecutionError as exc:
                if not exc.preserve_outcome:
                    raise
                preserved_error = exc
                result = None
        if preserved_error is not None:
            return _handle_execution_error(
                session,
                membership=membership,
                job=job,
                error=preserved_error,
                actor=worker_id,
            )
        assert result is not None
        jobs.mark_completed(
            session,
            job,
            result=result.result,
            outcome_committed=result.outcome_committed,
        )
        _append_child_event(
            session,
            membership=membership,
            job=job,
            event_type=PipelineEventType.STAGE_COMPLETED,
            actor=worker_id,
            reason_code="verification_child_committed",
            detail={
                **result.result,
                "parent_job_id": str(parent.id),
            },
        )
        _notify_email_parent(
            session,
            child=job,
            parent=parent,
            membership=membership,
            actor=worker_id,
        )
        return WorkerExecution(
            job=job,
            public_status=jobs.public_status(job),
            agent_id=job.agent_id,
            campaign_contact_id=membership.id,
            message="verification child completed.",
        )
    except AgentExecutionError as exc:
        return _handle_execution_error(
            session,
            membership=membership,
            job=job,
            error=exc,
            actor=worker_id,
        )
    except Exception as exc:  # noqa: BLE001 - bounded common queue retry
        return _handle_execution_error(
            session,
            membership=membership,
            job=job,
            error=AgentRetryableError(
                "unexpected_error",
                "The Verification child encountered an unexpected operational error.",
                detail={"exception_type": type(exc).__name__},
            ),
            actor=worker_id,
        )


def execute_started_job(
    session: Session,
    *,
    job: AgentJob,
    worker_id: str,
    adapters: dict[AgentIdentifier, AgentAdapter] | None = None,
) -> WorkerExecution:
    """Execute a durably Running job and stage its domain outcome atomically."""

    locked_context = locking.lock_job_context(session, job.id)
    if locked_context is None:
        raise jobs.AgentJobNotFound(f"job {job.id} does not exist")
    job = locked_context.job

    if job.status is not AgentJobStatus.IN_PROGRESS:
        raise jobs.AgentJobError("only a running job can execute")
    if job.lease_owner != worker_id:
        raise jobs.AgentJobError("job lease belongs to a different worker")

    adapters = adapters or DEFAULT_ADAPTERS
    membership = (
        session.get(CampaignContact, job.campaign_contact_id)
        if job.campaign_contact_id is not None
        else None
    )
    if membership is None:
        jobs.mark_failed(
            session,
            job,
            error_class="campaign_contact_missing",
            reason="The job has no live Campaign Contact.",
        )
        return WorkerExecution(
            job=job,
            public_status=jobs.public_status(job),
            agent_id=job.agent_id,
            campaign_contact_id=job.campaign_contact_id,
            message="Job failed: Campaign Contact is missing.",
        )
    campaign = session.get(Campaign, membership.campaign_id)
    contact = session.get(Contact, membership.contact_id)
    if campaign is None or contact is None:  # pragma: no cover - protected by FKs
        jobs.mark_failed(
            session,
            job,
            error_class="domain_record_missing",
            reason="A required Campaign or Contact record is missing.",
        )
        transition_stage(
            session,
            membership=membership,
            agent_id=job.agent_id,
            target=PipelineStageStatus.FAILED,
            event_type=PipelineEventType.FAILED_TERMINAL,
            actor=worker_id,
            job=job,
            reason_code="domain_record_missing",
            reason_detail="A required Campaign or Contact record is missing.",
        )
        return WorkerExecution(
            job=job,
            public_status=jobs.public_status(job),
            agent_id=job.agent_id,
            campaign_contact_id=membership.id,
            message="Job failed: required domain record is missing.",
        )

    parent = _email_parent_for_verification_child(session, job)
    if parent is not None:
        return _execute_email_verification_child(
            session,
            job=job,
            parent=parent,
            membership=membership,
            campaign=campaign,
            contact=contact,
            worker_id=worker_id,
            adapters=adapters,
        )

    if membership.membership_status is not CampaignMembershipStatus.ACTIVE:
        reason = f"Campaign Contact is {membership.membership_status.value}."
        jobs.mark_paused(session, job, reason=reason, reason_code="membership_paused")
        transition_stage(
            session,
            membership=membership,
            agent_id=job.agent_id,
            target=PipelineStageStatus.PAUSED,
            event_type=PipelineEventType.MEMBERSHIP_PAUSED,
            actor=worker_id,
            job=job,
            reason_code="membership_status",
            reason_detail=reason,
        )
        return WorkerExecution(
            job=job,
            public_status=jobs.public_status(job),
            agent_id=job.agent_id,
            campaign_contact_id=membership.id,
            message=reason,
        )

    terminal_block = refresh_eligibility(
        session,
        membership=membership,
        actor=worker_id,
    )
    if terminal_block:
        reason_code, reason = next(
            (
                (
                    str(item.get("code") or "eligibility_block"),
                    str(item.get("detail") or "The Campaign Contact is blocked."),
                )
                for item in membership.blocking_reasons
                if isinstance(item, dict) and item.get("terminal") is True
            ),
            ("eligibility_block", "The Campaign Contact is blocked."),
        )
        jobs.mark_paused(session, job, reason=reason, reason_code=reason_code)
        transition_stage(
            session,
            membership=membership,
            agent_id=job.agent_id,
            target=PipelineStageStatus.BLOCKED,
            event_type=PipelineEventType.ELIGIBILITY_BLOCKED,
            actor=worker_id,
            job=job,
            reason_code=reason_code,
            reason_detail=reason,
        )
        return WorkerExecution(
            job=job,
            public_status=jobs.public_status(job),
            agent_id=job.agent_id,
            campaign_contact_id=membership.id,
            message=reason,
        )

    control = effective_control(session, campaign=campaign, agent_id=job.agent_id)
    if control.status is not AgentControlStatus.ENABLED:
        reason = control.reason or f"{job.agent_id.value} is {control.status.value}."
        _project_email_control_outcome(
            job,
            status=control.status,
            source=control.source,
        )
        jobs.mark_paused(
            session,
            job,
            reason=reason,
            reason_code=f"agent_{control.status.value}",
        )
        target = (
            PipelineStageStatus.PAUSED
            if control.status is AgentControlStatus.PAUSED
            else PipelineStageStatus.DISABLED
        )
        transition_stage(
            session,
            membership=membership,
            agent_id=job.agent_id,
            target=target,
            event_type=(
                PipelineEventType.AGENT_PAUSED
                if target is PipelineStageStatus.PAUSED
                else PipelineEventType.AGENT_DISABLED
            ),
            actor=worker_id,
            job=job,
            reason_code=control.source,
            reason_detail=reason,
        )
        return WorkerExecution(
            job=job,
            public_status=jobs.public_status(job),
            agent_id=job.agent_id,
            campaign_contact_id=membership.id,
            message=reason,
        )

    adapter = adapters.get(job.agent_id)
    if adapter is None:
        exc: AgentExecutionError = AgentExecutionError(
            "adapter_missing",
            f"No executable adapter is registered for {job.agent_id.value}.",
        )
        return _handle_execution_error(
            session,
            membership=membership,
            job=job,
            error=exc,
            actor=worker_id,
        )

    try:
        preserved_error: AgentExecutionError | None = None
        # An adapter executes behind a savepoint. Unexpected database failures
        # can therefore be classified without leaving the caller's transaction
        # unusable, and partial domain writes never masquerade as completion.
        with session.begin_nested():
            try:
                result = adapter.execute(
                    AgentExecutionContext(
                        session=session,
                        job=job,
                        campaign=campaign,
                        membership=membership,
                        contact=contact,
                        config=control.config,
                        worker_id=worker_id,
                    )
                )
            except AgentExecutionError as exc:
                if not exc.preserve_outcome:
                    raise
                # Some outcomes are durable even though the execution did not
                # succeed. A review block intentionally stages domain writes (for
                # example generated email candidates), and an adapter that has
                # already called a paid external provider must keep its evidence,
                # usage and cost records even when the call failed or the verdict
                # cannot advance the pipeline — a database rollback cannot undo
                # the request that was already sent. Commit that savepoint, then
                # project the classified execution state below.
                preserved_error = exc
                result = None
        if preserved_error is not None:
            return _handle_execution_error(
                session,
                membership=membership,
                job=job,
                error=preserved_error,
                actor=worker_id,
            )
        assert result is not None
        refresh_eligibility(
            session,
            membership=membership,
            actor=worker_id,
        )
        if result.queue_status_handled:
            stage = _stage_after_handled_queue_result(
                session,
                membership=membership,
                job=job,
                output_reference=result.output_reference,
                actor=worker_id,
            )
            if stage is PipelineStageStatus.COMPLETED:
                schedule_next(session, membership=membership, actor=worker_id, parent_job=job)
            return WorkerExecution(
                job=job,
                public_status=jobs.public_status(job),
                agent_id=job.agent_id,
                campaign_contact_id=membership.id,
                message=f"{job.agent_id.value} {stage.value}.",
            )
        jobs.mark_completed(
            session,
            job,
            result=result.result,
            outcome_committed=result.outcome_committed,
        )
        transition_stage(
            session,
            membership=membership,
            agent_id=job.agent_id,
            target=PipelineStageStatus.COMPLETED,
            event_type=PipelineEventType.STAGE_COMPLETED,
            actor=worker_id,
            job=job,
            output_reference=result.output_reference,
            detail=result.result,
        )
        if job.agent_id is AgentIdentifier.EMAIL:
            _complete_verification_from_email(
                session,
                membership=membership,
                email_job=job,
                actor=worker_id,
            )
        schedule_next(session, membership=membership, actor=worker_id, parent_job=job)
        return WorkerExecution(
            job=job,
            public_status=jobs.public_status(job),
            agent_id=job.agent_id,
            campaign_contact_id=membership.id,
            message=f"{job.agent_id.value} completed.",
        )
    except AgentExecutionError as exc:
        return _handle_execution_error(
            session,
            membership=membership,
            job=job,
            error=exc,
            actor=worker_id,
        )
    except Exception as exc:  # noqa: BLE001 - converted to a bounded retry classification
        # Name what broke. This used to read only "an unexpected operational error":
        # the exception type was recorded in `error_detail` but appeared in no
        # message, so a worker log full of these was a dead end — every line
        # identical, nothing to search for, and the one fact that identifies the
        # cause reachable only by querying the database by hand.
        #
        # Deliberately still not the exception's own message: that text is
        # unsanitized and can carry a filesystem path, a prompt fragment or a
        # credential. The type names the fault, and the full detail continues to
        # reach the job record where the Workbench sanitizes it on the way to a page.
        #
        # One exception carries its whole diagnosis in a field rather than in its
        # message, and naming the type alone throws that away. A missing import is
        # identified entirely by *which* module was missing: "ModuleNotFoundError"
        # on two hundred consecutive Research jobs says only that something is
        # unimportable, while "ModuleNotFoundError: app.services.research.website"
        # names the subtree and points straight at the install. The module *name*
        # is safe to print — it is a dotted import path, not a filesystem path,
        # a prompt fragment or a credential — which is exactly why it is admitted
        # here when the exception's own message still is not.
        missing = getattr(exc, "name", None) if isinstance(exc, ImportError) else None
        named = f"{type(exc).__name__}: {missing}" if missing else type(exc).__name__
        return _handle_execution_error(
            session,
            membership=membership,
            job=job,
            error=AgentRetryableError(
                "unexpected_error",
                (
                    f"The Agent encountered an unexpected operational error ({named}). "
                    "This is a defect rather than a data problem: the same input will "
                    "fail the same way until it is fixed."
                ),
                detail={
                    "exception_type": type(exc).__name__,
                    "exception_module": type(exc).__module__,
                    **({"missing_module": missing} if missing else {}),
                },
            ),
            actor=worker_id,
        )


def execute_leased_job(
    session: Session,
    *,
    job: AgentJob,
    worker_id: str,
    adapters: dict[AgentIdentifier, AgentAdapter] | None = None,
) -> WorkerExecution:
    """Prepare and execute a lease in one caller-owned transaction.

    This compatibility entry point is useful for tests and embedded execution.
    The production worker commits ``prepare_leased_job`` before invoking
    ``execute_started_job`` so Leased and Running survive process restarts.
    """

    rejected = prepare_leased_job(session, job=job, worker_id=worker_id)
    if rejected is not None:
        return rejected
    return execute_started_job(
        session,
        job=job,
        worker_id=worker_id,
        adapters=adapters,
    )


def _handle_execution_error(
    session: Session,
    *,
    membership: CampaignContact,
    job: AgentJob,
    error: AgentExecutionError,
    actor: str,
) -> WorkerExecution:
    parent = _email_parent_for_verification_child(session, job)
    if parent is not None:
        if isinstance(error, AgentBlocked):
            jobs.mark_paused(
                session,
                job,
                reason=error.message,
                reason_code=error.code,
                error_detail=error.detail,
            )
            event_type = PipelineEventType.ELIGIBILITY_BLOCKED
            retryable = False
        elif error.retryable:
            spec = get_agent_spec(job.agent_id)
            jobs.schedule_retry(
                session,
                job,
                error_class=error.code,
                reason=error.message,
                base_seconds=spec.retry_base_seconds,
                cap_seconds=spec.retry_cap_seconds,
                error_detail=error.detail,
            )
            event_type = (
                PipelineEventType.RETRY_SCHEDULED
                if job.status is AgentJobStatus.RETRY_SCHEDULED
                else PipelineEventType.FAILED_TERMINAL
            )
            retryable = job.status is AgentJobStatus.RETRY_SCHEDULED
        else:
            jobs.mark_failed(
                session,
                job,
                error_class=error.code,
                reason=error.message,
                error_detail=error.detail,
            )
            event_type = PipelineEventType.FAILED_TERMINAL
            retryable = False
        _append_child_event(
            session,
            membership=membership,
            job=job,
            event_type=event_type,
            actor=actor,
            reason_code=error.code,
            reason_detail=error.message,
            retryable=retryable,
            detail={
                **error.detail,
                "parent_job_id": str(parent.id),
            },
        )
        _notify_email_parent(
            session,
            child=job,
            parent=parent,
            membership=membership,
            actor=actor,
        )
        return WorkerExecution(
            job=job,
            public_status=jobs.public_status(job),
            agent_id=job.agent_id,
            campaign_contact_id=membership.id,
            message=error.message,
        )

    if isinstance(error, AgentWaiting):
        jobs.mark_paused(
            session,
            job,
            reason=error.message,
            reason_code=error.code,
            error_detail=error.detail,
        )
        transition_stage(
            session,
            membership=membership,
            agent_id=job.agent_id,
            target=PipelineStageStatus.WAITING,
            event_type=PipelineEventType.STAGE_WAITING,
            actor=actor,
            job=job,
            reason_code=error.code,
            reason_detail=error.message,
            retryable=error.retryable,
            detail=error.detail,
        )
        return WorkerExecution(
            job=job,
            public_status=jobs.public_status(job),
            agent_id=job.agent_id,
            campaign_contact_id=membership.id,
            message=error.message,
        )

    if isinstance(error, AgentBlocked):
        jobs.mark_paused(
            session,
            job,
            reason=error.message,
            reason_code=error.code,
            error_detail=error.detail,
        )
        transition_stage(
            session,
            membership=membership,
            agent_id=job.agent_id,
            target=PipelineStageStatus.BLOCKED,
            event_type=PipelineEventType.ELIGIBILITY_BLOCKED,
            actor=actor,
            job=job,
            reason_code=error.code,
            reason_detail=error.message,
            detail=error.detail,
        )
    elif error.retryable:
        spec = get_agent_spec(job.agent_id)
        jobs.schedule_retry(
            session,
            job,
            error_class=error.code,
            reason=error.message,
            base_seconds=spec.retry_base_seconds,
            cap_seconds=spec.retry_cap_seconds,
            error_detail=error.detail,
        )
        target = (
            PipelineStageStatus.RETRYING
            if job.status is AgentJobStatus.RETRY_SCHEDULED
            else PipelineStageStatus.FAILED
        )
        transition_stage(
            session,
            membership=membership,
            agent_id=job.agent_id,
            target=target,
            event_type=(
                PipelineEventType.RETRY_SCHEDULED
                if target is PipelineStageStatus.RETRYING
                else PipelineEventType.FAILED_TERMINAL
            ),
            actor=actor,
            job=job,
            reason_code=error.code,
            reason_detail=error.message,
            retryable=target is PipelineStageStatus.RETRYING,
            detail=error.detail,
        )
    else:
        jobs.mark_failed(
            session,
            job,
            error_class=error.code,
            reason=error.message,
            error_detail=error.detail,
        )
        transition_stage(
            session,
            membership=membership,
            agent_id=job.agent_id,
            target=PipelineStageStatus.FAILED,
            event_type=PipelineEventType.FAILED_TERMINAL,
            actor=actor,
            job=job,
            reason_code=error.code,
            reason_detail=error.message,
            retryable=False,
            detail=error.detail,
        )
    return WorkerExecution(
        job=job,
        public_status=jobs.public_status(job),
        agent_id=job.agent_id,
        campaign_contact_id=membership.id,
        message=error.message,
    )


def claim_next_campaign_job(
    session: Session,
    *,
    worker_id: str,
    lease_seconds: float = 120.0,
    agent_ids: Iterable[AgentIdentifier] | None = None,
    adapters: dict[AgentIdentifier, AgentAdapter] | None = None,
) -> AgentJob | None:
    """Recover abandoned work, project terminal expiry, and claim one job."""

    recovered = jobs.recover_expired_leases(session)
    for abandoned in recovered:
        if abandoned.status is not AgentJobStatus.FAILED or abandoned.campaign_contact_id is None:
            continue
        membership = session.get(CampaignContact, abandoned.campaign_contact_id)
        if membership is None:
            continue
        transition_stage(
            session,
            membership=membership,
            agent_id=abandoned.agent_id,
            target=PipelineStageStatus.FAILED,
            event_type=PipelineEventType.FAILED_TERMINAL,
            actor=worker_id,
            job=abandoned,
            reason_code="lease_expired",
            reason_detail=abandoned.last_error,
            retryable=False,
            detail={"attempts": abandoned.attempts, "max_attempts": abandoned.max_attempts},
        )

    enabled_ids = tuple(agent_ids or (adapters or DEFAULT_ADAPTERS).keys())
    job = jobs.claim_next_job(
        session,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        agent_ids=enabled_ids,
        campaign_contact_only=True,
        recover_abandoned=False,
    )
    return job


def run_next(
    session: Session,
    *,
    worker_id: str,
    lease_seconds: float = 120.0,
    agent_ids: Iterable[AgentIdentifier] | None = None,
    adapters: dict[AgentIdentifier, AgentAdapter] | None = None,
) -> WorkerExecution:
    """Claim and execute at most one Campaign Contact job in one transaction."""

    job = claim_next_campaign_job(
        session,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        agent_ids=agent_ids,
        adapters=adapters,
    )
    if job is None:
        return WorkerExecution(
            job=None,
            public_status=None,
            agent_id=None,
            campaign_contact_id=None,
            message="No due Agent job.",
        )
    return execute_leased_job(
        session,
        job=job,
        worker_id=worker_id,
        adapters=adapters,
    )

"""Durable Campaign Contact state projection and append-only event history."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.campaign import CampaignContact
from app.models.enums import (
    AgentIdentifier,
    CampaignMembershipStatus,
    PipelineEventType,
    PipelineStageStatus,
)
from app.models.pipeline import CampaignContactAgentState, PipelineEvent
from app.models.verification_job import AgentJob
from app.services.agents.registry import get_agent_spec, next_agent


class PipelineStateError(Exception):
    """An illegal or inconsistent pipeline transition."""


_ALLOWED: dict[PipelineStageStatus, frozenset[PipelineStageStatus]] = {
    PipelineStageStatus.WAITING: frozenset(
        {
            PipelineStageStatus.RUNNING,
            PipelineStageStatus.PAUSED,
            PipelineStageStatus.RETRYING,
            PipelineStageStatus.FAILED,
            PipelineStageStatus.COMPLETED,
            PipelineStageStatus.DISABLED,
            PipelineStageStatus.SKIPPED,
            PipelineStageStatus.BLOCKED,
        }
    ),
    PipelineStageStatus.RUNNING: frozenset(
        {
            PipelineStageStatus.PAUSED,
            PipelineStageStatus.RETRYING,
            PipelineStageStatus.FAILED,
            PipelineStageStatus.COMPLETED,
            PipelineStageStatus.DISABLED,
            PipelineStageStatus.BLOCKED,
        }
    ),
    PipelineStageStatus.PAUSED: frozenset(
        {
            PipelineStageStatus.WAITING,
            PipelineStageStatus.RUNNING,
            PipelineStageStatus.DISABLED,
            PipelineStageStatus.SKIPPED,
            PipelineStageStatus.BLOCKED,
        }
    ),
    PipelineStageStatus.RETRYING: frozenset(
        {
            PipelineStageStatus.RUNNING,
            PipelineStageStatus.PAUSED,
            PipelineStageStatus.FAILED,
            PipelineStageStatus.DISABLED,
            PipelineStageStatus.SKIPPED,
            PipelineStageStatus.BLOCKED,
        }
    ),
    PipelineStageStatus.FAILED: frozenset(
        {
            PipelineStageStatus.WAITING,
            PipelineStageStatus.RETRYING,
            PipelineStageStatus.SKIPPED,
        }
    ),
    PipelineStageStatus.COMPLETED: frozenset(),
    PipelineStageStatus.DISABLED: frozenset(
        {
            PipelineStageStatus.WAITING,
            PipelineStageStatus.PAUSED,
            PipelineStageStatus.SKIPPED,
            PipelineStageStatus.BLOCKED,
        }
    ),
    PipelineStageStatus.SKIPPED: frozenset(),
    PipelineStageStatus.BLOCKED: frozenset(
        {
            PipelineStageStatus.WAITING,
            PipelineStageStatus.PAUSED,
            PipelineStageStatus.DISABLED,
        }
    ),
}


def agent_state(
    session: Session,
    *,
    campaign_contact_id: uuid.UUID,
    agent_id: AgentIdentifier,
    create: bool = True,
) -> CampaignContactAgentState | None:
    state = session.scalars(
        select(CampaignContactAgentState).where(
            CampaignContactAgentState.campaign_contact_id == campaign_contact_id,
            CampaignContactAgentState.agent_id == agent_id,
        )
    ).one_or_none()
    if state is not None or not create:
        return state
    state = CampaignContactAgentState(
        campaign_contact_id=campaign_contact_id,
        agent_id=agent_id,
        status=PipelineStageStatus.WAITING,
    )
    try:
        with session.begin_nested():
            session.add(state)
            session.flush()
    except IntegrityError:
        state = session.scalars(
            select(CampaignContactAgentState).where(
                CampaignContactAgentState.campaign_contact_id == campaign_contact_id,
                CampaignContactAgentState.agent_id == agent_id,
            )
        ).one()
    return state


def append_event(
    session: Session,
    *,
    campaign_contact_id: uuid.UUID,
    event_type: PipelineEventType,
    actor: str,
    agent_id: AgentIdentifier | None = None,
    job_id: uuid.UUID | None = None,
    from_status: PipelineStageStatus | None = None,
    to_status: PipelineStageStatus | None = None,
    reason_code: str | None = None,
    reason_detail: str | None = None,
    retryable: bool = False,
    detail: dict[str, Any] | None = None,
) -> PipelineEvent:
    event = PipelineEvent(
        campaign_contact_id=campaign_contact_id,
        agent_id=agent_id,
        job_id=job_id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        reason_code=reason_code,
        reason_detail=reason_detail,
        retryable=retryable,
        detail=detail or {},
        actor=actor,
        occurred_at=datetime.now(UTC),
    )
    session.add(event)
    session.flush()
    return event


def transition_stage(
    session: Session,
    *,
    membership: CampaignContact,
    agent_id: AgentIdentifier,
    target: PipelineStageStatus,
    event_type: PipelineEventType,
    actor: str,
    job: AgentJob | None = None,
    reason_code: str | None = None,
    reason_detail: str | None = None,
    retryable: bool = False,
    output_reference: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
    allow_same: bool = True,
) -> CampaignContactAgentState:
    """Transition one Agent state and update the fast Campaign Contact projection."""

    state = agent_state(
        session,
        campaign_contact_id=membership.id,
        agent_id=agent_id,
        create=True,
    )
    assert state is not None
    previous = state.status
    if target is previous and allow_same:
        state.reason_code = reason_code
        state.reason_detail = reason_detail
        state.retryable = retryable
        state.waiting_on_agent = None
        if job is not None:
            state.latest_job_id = job.id
            state.attempt_count = max(state.attempt_count, job.attempts)
        if output_reference is not None:
            state.output_reference = output_reference
        membership.pipeline_status = target
        membership.current_stage = agent_id
        membership.next_stage = agent_id
        session.flush()
        append_event(
            session,
            campaign_contact_id=membership.id,
            agent_id=agent_id,
            job_id=job.id if job else None,
            event_type=event_type,
            from_status=previous,
            to_status=target,
            reason_code=reason_code,
            reason_detail=reason_detail,
            retryable=retryable,
            detail=detail,
            actor=actor,
        )
        return state
    if target not in _ALLOWED[previous]:
        raise PipelineStateError(
            f"cannot move {agent_id.value} from {previous.value} to {target.value}"
        )

    now = datetime.now(UTC)
    state.status = target
    state.reason_code = reason_code
    state.reason_detail = reason_detail
    state.retryable = retryable
    state.waiting_on_agent = None
    if job is not None:
        state.latest_job_id = job.id
        state.attempt_count = max(state.attempt_count, job.attempts)
    if target is PipelineStageStatus.RUNNING:
        state.started_at = state.started_at or now
    if target in {
        PipelineStageStatus.COMPLETED,
        PipelineStageStatus.SKIPPED,
        PipelineStageStatus.FAILED,
    }:
        state.completed_at = now
    if output_reference is not None:
        state.output_reference = output_reference

    membership.pipeline_status = target
    membership.current_stage = agent_id
    membership.next_stage = agent_id
    if target in {PipelineStageStatus.COMPLETED, PipelineStageStatus.SKIPPED}:
        if target is PipelineStageStatus.COMPLETED:
            membership.latest_completed_stage = agent_id
        following = next_agent(agent_id)
        if agent_id is membership.desired_stage or following is None:
            membership.next_stage = None
            membership.pipeline_status = PipelineStageStatus.COMPLETED
        else:
            membership.current_stage = following
            membership.next_stage = following
            membership.pipeline_status = PipelineStageStatus.WAITING
    session.flush()
    append_event(
        session,
        campaign_contact_id=membership.id,
        agent_id=agent_id,
        job_id=job.id if job else None,
        event_type=event_type,
        from_status=previous,
        to_status=target,
        reason_code=reason_code,
        reason_detail=reason_detail,
        retryable=retryable,
        detail=detail,
        actor=actor,
    )
    return state


def initialize_pipeline(
    session: Session,
    *,
    membership: CampaignContact,
    actor: str,
    blocked: bool,
    block_reason: str | None = None,
) -> None:
    """Initialize Capture as complete and Identity as the first queued stage."""

    capture = agent_state(
        session,
        campaign_contact_id=membership.id,
        agent_id=AgentIdentifier.CAPTURE,
        create=True,
    )
    assert capture is not None
    if capture.status is not PipelineStageStatus.COMPLETED:
        transition_stage(
            session,
            membership=membership,
            agent_id=AgentIdentifier.CAPTURE,
            target=PipelineStageStatus.COMPLETED,
            event_type=PipelineEventType.STAGE_COMPLETED,
            actor=actor,
            reason_code="permanent_contact_exists",
            reason_detail="Capture produced or selected a permanent Contact record.",
            output_reference={"contact_id": str(membership.contact_id)},
        )
    if membership.desired_stage is AgentIdentifier.CAPTURE:
        membership.current_stage = AgentIdentifier.CAPTURE
        membership.next_stage = None
        membership.pipeline_status = (
            PipelineStageStatus.BLOCKED if blocked else PipelineStageStatus.COMPLETED
        )
        session.flush()
        return
    identity = agent_state(
        session,
        campaign_contact_id=membership.id,
        agent_id=AgentIdentifier.IDENTITY,
        create=True,
    )
    assert identity is not None
    if blocked and identity.status is PipelineStageStatus.WAITING:
        transition_stage(
            session,
            membership=membership,
            agent_id=AgentIdentifier.IDENTITY,
            target=PipelineStageStatus.BLOCKED,
            event_type=PipelineEventType.ELIGIBILITY_BLOCKED,
            actor=actor,
            reason_code="suppression",
            reason_detail=block_reason,
        )
    elif not blocked:
        membership.current_stage = AgentIdentifier.IDENTITY
        membership.next_stage = AgentIdentifier.IDENTITY
        membership.pipeline_status = PipelineStageStatus.WAITING
        session.flush()
        # A state row is itself the durable projection; record why it is waiting.
        if not session.scalars(
            select(PipelineEvent).where(
                PipelineEvent.campaign_contact_id == membership.id,
                PipelineEvent.agent_id == AgentIdentifier.IDENTITY,
                PipelineEvent.event_type == PipelineEventType.STAGE_WAITING,
            )
        ).first():
            append_event(
                session,
                campaign_contact_id=membership.id,
                agent_id=AgentIdentifier.IDENTITY,
                event_type=PipelineEventType.STAGE_WAITING,
                from_status=PipelineStageStatus.WAITING,
                to_status=PipelineStageStatus.WAITING,
                reason_code="pipeline_initialized",
                reason_detail="Identity Agent is the next dependency.",
                actor=actor,
            )


def dependencies_satisfied(
    session: Session,
    *,
    campaign_contact_id: uuid.UUID,
    agent_id: AgentIdentifier,
) -> tuple[bool, AgentIdentifier | None]:
    for dependency in get_agent_spec(agent_id).dependencies:
        state = agent_state(
            session,
            campaign_contact_id=campaign_contact_id,
            agent_id=dependency,
            create=False,
        )
        if state is None or state.status not in {
            PipelineStageStatus.COMPLETED,
            PipelineStageStatus.SKIPPED,
        }:
            return False, dependency
    return True, None


def skip_current_stage(
    session: Session,
    *,
    membership: CampaignContact,
    agent_id: AgentIdentifier,
    reason: str,
    actor: str = "operator",
) -> CampaignContactAgentState:
    """Deliberately skip the current non-blocked stage with durable history."""

    clean_reason = reason.strip()
    if not clean_reason:
        raise PipelineStateError("a reason is required to skip an Agent stage")
    if len(clean_reason) > 2_000:
        raise PipelineStateError("stage skip reason must be 2000 characters or fewer")
    if membership.membership_status is not CampaignMembershipStatus.ACTIVE:
        raise PipelineStateError("only an active Campaign Contact can skip a stage")
    if membership.next_stage is not agent_id:
        raise PipelineStateError(
            f"{agent_id.value} is not the Campaign Contact's current next stage"
        )
    if agent_id is AgentIdentifier.CAPTURE:
        raise PipelineStateError("Capture cannot be skipped; a permanent Contact is required")
    if not get_agent_spec(agent_id).skippable:
        raise PipelineStateError(
            f"{get_agent_spec(agent_id).display_name} is safety-critical and cannot be skipped"
        )
    if any(
        isinstance(item, dict) and item.get("terminal") is True
        for item in membership.blocking_reasons
    ):
        raise PipelineStateError("a terminal eligibility block cannot be bypassed by skipping")
    state = agent_state(
        session,
        campaign_contact_id=membership.id,
        agent_id=agent_id,
        create=True,
    )
    assert state is not None
    if state.status is PipelineStageStatus.BLOCKED:
        raise PipelineStateError("a domain-blocked stage must be resolved or retried, not skipped")
    from app.services.agents.jobs import cancel_jobs_for_stage

    cancel_jobs_for_stage(
        session,
        campaign_contact_id=membership.id,
        agent_id=agent_id,
        reason=clean_reason,
        actor=actor,
    )
    state = transition_stage(
        session,
        membership=membership,
        agent_id=agent_id,
        target=PipelineStageStatus.SKIPPED,
        event_type=PipelineEventType.STAGE_SKIPPED,
        actor=actor,
        reason_code="operator_skip",
        reason_detail=clean_reason,
    )
    from app.services.agents.orchestrator import schedule_next

    schedule_next(session, membership=membership, actor=actor)
    return state


@dataclass(frozen=True)
class PipelineSnapshot:
    membership: CampaignContact
    stages: tuple[CampaignContactAgentState, ...]
    jobs: tuple[AgentJob, ...]
    events: tuple[PipelineEvent, ...]

    @property
    def next_action(self) -> str:
        if self.membership.membership_status is CampaignMembershipStatus.ARCHIVED:
            return "The Campaign Contact is archived; no Agent work will run."
        if self.membership.membership_status is CampaignMembershipStatus.PAUSED:
            return "The Campaign Contact is paused by an operator."
        terminal_reason = next(
            (
                str(item.get("detail"))
                for item in self.membership.blocking_reasons
                if isinstance(item, dict) and item.get("terminal") is True
            ),
            None,
        )
        if terminal_reason:
            return terminal_reason
        if self.membership.pipeline_status is PipelineStageStatus.COMPLETED:
            return "No further stage is required."
        if self.membership.next_stage is None:
            return "No next Agent is currently selected."
        state = next(
            (row for row in self.stages if row.agent_id is self.membership.next_stage),
            None,
        )
        if state and state.reason_detail:
            return state.reason_detail
        return f"{self.membership.next_stage.value} is next."


def pipeline_snapshot(
    session: Session,
    *,
    campaign_contact_id: uuid.UUID,
    event_limit: int = 200,
) -> PipelineSnapshot | None:
    membership = session.get(CampaignContact, campaign_contact_id)
    if membership is None:
        return None
    stages = tuple(
        session.scalars(
            select(CampaignContactAgentState)
            .where(CampaignContactAgentState.campaign_contact_id == membership.id)
            .order_by(CampaignContactAgentState.agent_id)
        ).all()
    )
    jobs = tuple(
        session.scalars(
            select(AgentJob)
            .where(AgentJob.campaign_contact_id == membership.id)
            .order_by(AgentJob.created_at.desc())
        ).all()
    )
    events = tuple(
        reversed(
            session.scalars(
                select(PipelineEvent)
                .where(PipelineEvent.campaign_contact_id == membership.id)
                .order_by(PipelineEvent.occurred_at.desc(), PipelineEvent.id.desc())
                .limit(event_limit)
            ).all()
        )
    )
    return PipelineSnapshot(membership=membership, stages=stages, jobs=jobs, events=events)

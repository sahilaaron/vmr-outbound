"""Scheduling the same pipeline stage twice, from two callers that disagree.

``stage_job_key`` names one Campaign Contact's turn at one Agent, and that
convergence is the point of it: two workers racing the same stage must end up
with one job. The queue enforces it by comparing the *whole* enqueue intent, so
two callers that agree about the work and differ about what queued it read as key
reuse — a hard error — rather than as the same turn asked for twice.

That is reachable from ordinary product use. A worker records the stage that
scheduled the next one as its parent; an enrolment has no parent to record, and
pins whatever control versions are current when it runs. Re-enrolling somebody
whose stage the worker had already queued therefore raised out of the queue and
became a 500 on whichever surface asked — the Google Sheets add-on, in the case
that was seen on staging.

What is proved here is the distinction the repair rests on: a collision that is
the same work converges onto the durable job, a collision that is *not* the same
work still raises, and neither leaves the session unusable.
"""

from __future__ import annotations

import uuid

import pytest
from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    CampaignStatus,
    PipelineStageStatus,
)
from app.models.verification_job import AgentJob
from app.services import campaign_contacts
from app.services import pipeline as pipeline_service
from app.services.agents import controls, jobs, orchestrator
from sqlalchemy import select
from sqlalchemy.orm import Session


def _campaign(db: Session) -> Campaign:
    campaign = Campaign(
        name=f"Stage reschedule {uuid.uuid4().hex[:8]}",
        description="Pipeline stage rescheduling",
        status=CampaignStatus.ACTIVE,
        execution_enabled=True,
    )
    db.add(campaign)
    db.flush()
    for agent_id in (AgentIdentifier.IDENTITY, AgentIdentifier.COMPANY):
        controls.set_global_control(
            db,
            agent_id=agent_id,
            status=AgentControlStatus.ENABLED,
            reason="test setup",
        )
    return campaign


def _membership(db: Session, campaign: Campaign) -> CampaignContact:
    token = uuid.uuid4().hex[:10]
    contact = Contact(
        first_name="Ada",
        last_name="Lovelace",
        company_name="Kiln Systems",
        company_domain="kiln.example",
        natural_key=f"ada|lovelace|{token}|kiln.example",
    )
    db.add(contact)
    db.flush()
    return campaign_contacts.enrol_contact(
        db,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        actor="operator",
    ).membership


def _queued_by_the_worker(db: Session, membership: CampaignContact) -> AgentJob:
    """Carry the membership into a stage the *worker* queued, then fail it.

    This is the state the defect needs and the state staging was in: the stage's
    durable job names the stage that scheduled it, which no enrolment can name.
    """

    first = membership.next_stage
    assert first is not None
    first_job = db.scalars(
        select(AgentJob).where(
            AgentJob.campaign_contact_id == membership.id,
            AgentJob.agent_id == first,
        )
    ).one()
    pipeline_service.transition_stage(
        db,
        membership=membership,
        agent_id=first,
        target=PipelineStageStatus.COMPLETED,
        event_type=pipeline_service.PipelineEventType.STAGE_COMPLETED,
        actor="worker-a",
        job=first_job,
    )
    second = orchestrator.schedule_next(
        db, membership=membership, actor="worker-a", parent_job=first_job
    )
    assert second is not None and second.parent_job_id == first_job.id
    jobs.mark_failed(
        db,
        second,
        error_class="AgentExecutionError",
        reason="the stage failed on its last attempt",
    )
    pipeline_service.transition_stage(
        db,
        membership=membership,
        agent_id=second.agent_id,
        target=PipelineStageStatus.FAILED,
        event_type=pipeline_service.PipelineEventType.FAILED_TERMINAL,
        actor="worker-a",
        job=second,
    )
    return second


def test_a_stage_converges_when_only_its_lineage_differs(db_session: Session) -> None:
    campaign = _campaign(db_session)
    membership = _membership(db_session, campaign)
    stalled = _queued_by_the_worker(db_session, membership)
    before = db_session.query(AgentJob).count()

    # The enrolment caller: no parent job, because nothing scheduled it.
    again = orchestrator.schedule_next(db_session, membership=membership, actor="operator")

    assert again is not None
    assert again.id == stalled.id
    assert db_session.query(AgentJob).count() == before


def test_a_stage_converges_when_the_control_versions_have_moved(db_session: Session) -> None:
    """The second drift seen in real data, and the same answer.

    Every stage job pins the control versions current when it was queued, and an
    operator toggling an Agent moves them. That is a fact about when the job was
    made, not about what it is for.
    """

    campaign = _campaign(db_session)
    membership = _membership(db_session, campaign)
    stalled = _queued_by_the_worker(db_session, membership)
    pinned = dict(stalled.input_reference or {})

    controls.set_global_control(
        db_session,
        agent_id=stalled.agent_id,
        status=AgentControlStatus.ENABLED,
        reason="operator toggled the control after the job was queued",
    )
    before = db_session.query(AgentJob).count()

    again = orchestrator.schedule_next(db_session, membership=membership, actor="operator")

    assert again is not None and again.id == stalled.id
    assert db_session.query(AgentJob).count() == before
    # Converging adopts the durable job as it stands; it does not rewrite what
    # that job recorded about the moment it was queued.
    assert dict(again.input_reference or {}) == pinned


def test_a_key_standing_for_different_work_still_raises(db_session: Session) -> None:
    """The case that must never be swallowed."""

    campaign = _campaign(db_session)
    membership = _membership(db_session, campaign)
    stage = membership.next_stage
    assert stage is not None
    # Cancel this stage's own job so the scheduler reaches the enqueue, then put
    # something that is *not* this stage's work at its key.
    own = db_session.scalars(
        select(AgentJob).where(
            AgentJob.campaign_contact_id == membership.id,
            AgentJob.agent_id == stage,
        )
    ).one()
    key = own.idempotency_key
    db_session.delete(own)
    db_session.flush()
    jobs.enqueue_job(
        db_session,
        agent_id=stage,
        idempotency_key=key,
        task_kind="something_else_entirely",
        max_attempts=1,
        entity_type="test",
        entity_id=uuid.uuid4(),
    )

    with pytest.raises(jobs.JobIdempotencyConflict):
        orchestrator.schedule_next(db_session, membership=membership, actor="operator")


def test_a_key_naming_another_contacts_turn_still_raises(db_session: Session) -> None:
    """Same shape, but the impostor is a real pipeline job for somebody else."""

    campaign = _campaign(db_session)
    mine = _membership(db_session, campaign)
    theirs = _membership(db_session, campaign)
    stage = mine.next_stage
    assert stage is not None
    own = db_session.scalars(
        select(AgentJob).where(
            AgentJob.campaign_contact_id == mine.id,
            AgentJob.agent_id == stage,
        )
    ).one()
    key = own.idempotency_key
    db_session.delete(own)
    db_session.flush()
    jobs.enqueue_job(
        db_session,
        agent_id=stage,
        idempotency_key=key,
        task_kind=orchestrator.STAGE_TASK_KIND,
        max_attempts=1,
        campaign_id=theirs.campaign_id,
        campaign_contact_id=theirs.id,
        contact_id=theirs.contact_id,
        entity_type="campaign_contact",
        entity_id=theirs.id,
    )

    with pytest.raises(jobs.JobIdempotencyConflict):
        orchestrator.schedule_next(db_session, membership=mine, actor="operator")


def test_the_session_is_still_usable_after_a_benign_collision(db_session: Session) -> None:
    """A caught exception is not enough if the transaction is left poisoned."""

    campaign = _campaign(db_session)
    membership = _membership(db_session, campaign)
    _queued_by_the_worker(db_session, membership)

    orchestrator.schedule_next(db_session, membership=membership, actor="operator")

    # Reads, writes and a flush all still work in the same transaction.
    assert db_session.query(AgentJob).count() >= 1
    later = _membership(db_session, campaign)
    db_session.flush()
    assert db_session.get(CampaignContact, later.id) is not None


def test_the_session_survives_a_conflict_that_is_re_raised(db_session: Session) -> None:
    """The refusal path must leave a usable transaction too, not a poisoned one."""

    campaign = _campaign(db_session)
    membership = _membership(db_session, campaign)
    stage = membership.next_stage
    assert stage is not None
    own = db_session.scalars(
        select(AgentJob).where(
            AgentJob.campaign_contact_id == membership.id,
            AgentJob.agent_id == stage,
        )
    ).one()
    key = own.idempotency_key
    db_session.delete(own)
    db_session.flush()
    jobs.enqueue_job(
        db_session,
        agent_id=stage,
        idempotency_key=key,
        task_kind="something_else_entirely",
        max_attempts=1,
        entity_type="test",
        entity_id=uuid.uuid4(),
    )

    with pytest.raises(jobs.JobIdempotencyConflict):
        orchestrator.schedule_next(db_session, membership=membership, actor="operator")

    later = _membership(db_session, campaign)
    db_session.flush()
    assert db_session.get(CampaignContact, later.id) is not None

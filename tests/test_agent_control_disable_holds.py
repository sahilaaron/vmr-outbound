"""Disabling an Agent must hold the cohort standing at it, never skip it (B-3).

``reconcile_agent_control`` selects every matching Campaign Contact and calls
``schedule_next`` on each in one transaction. While a disabled *skippable* stage
auto-skipped from that call, a single operator click on "disable Research" moved
the whole cohort to ``SKIPPED`` — which ``app/services/pipeline.py`` gives an
empty outgoing transition set, so re-enabling the Agent recovered none of it.
The operator-facing note for that same click said "nothing is discarded".

These tests hold the corrected contract: reconciliation projects the control
onto the stage and holds it at ``DISABLED``; re-enabling releases the same work
under the same job identity; and none of it grants Sending anything.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from app.models.agent import AgentControl
from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    AgentJobStatus,
    CampaignStatus,
    PipelineEventType,
    PipelineStageStatus,
)
from app.models.pipeline import CampaignContactAgentState, PipelineEvent
from app.models.verification_job import AgentJob
from app.services import campaign_contacts, pipeline
from app.services.agents import controls
from app.services.agents.orchestrator import (
    claim_next_campaign_job,
    reconcile_agent_control,
    run_next,
)
from app.services.agents.registry import get_agent_spec
from app.services.workbench_agents.commands import CommandOutcome, WorkbenchCommands
from sqlalchemy import event, select
from sqlalchemy.orm import Session

#: The reproduction used six enrolled Contacts, and the reported symptom was
#: "6 of 6 terminally skipped by one click".
COHORT = 6

#: Only the two stages ahead of Research are ever executed here. Research itself
#: must never run: this suite is about what the *control* does to a stage that is
#: waiting, and running the adapter would replace that state with its own.
_UPSTREAM = (AgentIdentifier.IDENTITY, AgentIdentifier.COMPANY)


def _campaign(session: Session, *, tag: str = "a") -> tuple[Campaign, Company]:
    company = Company(name=f"Analytical Engines {tag}", domain=f"engines-{tag}.example")
    campaign = Campaign(
        name=f"Disable holds {uuid.uuid4()}",
        status=CampaignStatus.ACTIVE,
        execution_enabled=True,
    )
    session.add_all([company, campaign])
    session.flush()
    return campaign, company


def _contacts(session: Session, *, count: int, company: Company) -> list[Contact]:
    people = [
        Contact(
            first_name="Ada",
            last_name=f"Lovelace{index}",
            company_name=company.name,
            company_domain=company.domain,
            natural_key=f"ada|lovelace{index}|{company.domain}",
        )
        for index in range(count)
    ]
    session.add_all(people)
    session.flush()
    return people


def _drain_upstream(session: Session) -> None:
    """Run Identity and Company for every enrolled Contact, and nothing else.

    ``agent_ids`` restricts what the worker may *claim*, so the Research jobs
    that Company's completion queues are left in the queue untouched.
    """

    for _ in range(200):
        outcome = run_next(session, worker_id="b3-test", agent_ids=_UPSTREAM)
        if outcome.job is None:
            return
    raise AssertionError("upstream stages did not settle")


def _cohort_at_research(
    session: Session, *, count: int = COHORT, tag: str = "a"
) -> tuple[Campaign, list[CampaignContact]]:
    """A real Campaign with execution on, parked with Research enabled and queued."""

    campaign, company = _campaign(session, tag=tag)
    controls.set_global_control(
        session,
        agent_id=AgentIdentifier.RESEARCH,
        status=AgentControlStatus.ENABLED,
        reason="research is on while the cohort is built",
    )
    memberships = [
        campaign_contacts.enrol_contact(
            session,
            campaign_id=campaign.id,
            contact_id=contact.id,
            source_type="manual",
            enqueue=True,
            desired_stage=AgentIdentifier.RESEARCH,
        ).membership
        for contact in _contacts(session, count=count, company=company)
    ]
    _drain_upstream(session)
    for membership in memberships:
        assert membership.next_stage is AgentIdentifier.RESEARCH
        assert _stage(session, membership, AgentIdentifier.RESEARCH) is PipelineStageStatus.WAITING
    return campaign, memberships


def _stage(
    session: Session, membership: CampaignContact, agent_id: AgentIdentifier
) -> PipelineStageStatus | None:
    state = pipeline.agent_state(
        session,
        campaign_contact_id=membership.id,
        agent_id=agent_id,
        create=False,
    )
    return state.status if state is not None else None


def _jobs(
    session: Session, membership: CampaignContact, agent_id: AgentIdentifier
) -> list[AgentJob]:
    return list(
        session.scalars(
            select(AgentJob).where(
                AgentJob.campaign_contact_id == membership.id,
                AgentJob.agent_id == agent_id,
            )
        ).all()
    )


def _set_status(
    session: Session, status: AgentControlStatus, *, agent_id: AgentIdentifier
) -> CommandOutcome:
    """Flip one global control through the operator's own command surface."""

    control = session.get(AgentControl, agent_id)
    return WorkbenchCommands(session).set_global_agent_status(
        agent_id,
        status,
        expected_version=control.version if control else None,
        reason="operator flipped the control",
    )


@pytest.fixture()
def captured_sql(db_session: Session) -> Iterator[list[str]]:
    """Every statement the session emits, for the locking/ordering assertions."""

    statements: list[str] = []
    connection = db_session.connection()

    def _record(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN202, ARG001
        statements.append(statement)

    event.listen(connection.engine, "before_cursor_execute", _record)
    try:
        yield statements
    finally:
        event.remove(connection.engine, "before_cursor_execute", _record)


def test_disabling_research_holds_the_cohort_and_skips_nothing(db_session: Session) -> None:
    """One click must not terminally skip a single Campaign Contact."""

    _, memberships = _cohort_at_research(db_session)

    outcome = _set_status(
        db_session, AgentControlStatus.DISABLED, agent_id=AgentIdentifier.RESEARCH
    )
    assert outcome.accepted

    skipped = [
        membership
        for membership in memberships
        if _stage(db_session, membership, AgentIdentifier.RESEARCH) is PipelineStageStatus.SKIPPED
    ]
    assert skipped == [], f"{len(skipped)} of {COHORT} were terminally skipped by one click"

    autoskips = db_session.scalars(
        select(PipelineEvent).where(
            PipelineEvent.agent_id == AgentIdentifier.RESEARCH,
            PipelineEvent.reason_code == "control_disabled_autoskip",
        )
    ).all()
    assert list(autoskips) == []

    for membership in memberships:
        assert _stage(db_session, membership, AgentIdentifier.RESEARCH) is (
            PipelineStageStatus.DISABLED
        )
        # Held *at* the disabled boundary: the stage is still the one to run.
        assert membership.next_stage is AgentIdentifier.RESEARCH
        research_jobs = _jobs(db_session, membership, AgentIdentifier.RESEARCH)
        assert len(research_jobs) == 1
        assert research_jobs[0].status is AgentJobStatus.PAUSED
        assert research_jobs[0].error_class == "agent_disabled"


def test_the_operator_note_no_longer_claims_nothing_is_discarded(db_session: Session) -> None:
    """The message that accompanied the loss must match the behaviour."""

    _, _ = _cohort_at_research(db_session)
    outcome = _set_status(
        db_session, AgentControlStatus.DISABLED, agent_id=AgentIdentifier.RESEARCH
    )

    assert outcome.in_flight_note is not None
    assert "nothing is discarded" not in outcome.in_flight_note
    assert "holds there and resumes" in outcome.in_flight_note


def test_re_enabling_releases_the_held_work_without_duplicating_a_job(
    db_session: Session,
) -> None:
    """The whole point of holding: enabling it again recovers the work."""

    _, memberships = _cohort_at_research(db_session)
    _set_status(db_session, AgentControlStatus.DISABLED, agent_id=AgentIdentifier.RESEARCH)
    held = {
        membership.id: _jobs(db_session, membership, AgentIdentifier.RESEARCH)[0].id
        for membership in memberships
    }

    outcome = _set_status(db_session, AgentControlStatus.ENABLED, agent_id=AgentIdentifier.RESEARCH)
    assert outcome.accepted

    for membership in memberships:
        assert _stage(db_session, membership, AgentIdentifier.RESEARCH) is (
            PipelineStageStatus.WAITING
        )
        research_jobs = _jobs(db_session, membership, AgentIdentifier.RESEARCH)
        # No duplicate across the disable -> enable cycle: same job, same identity.
        assert len(research_jobs) == 1
        assert research_jobs[0].id == held[membership.id]
        assert research_jobs[0].status is AgentJobStatus.PENDING

    # And the released work genuinely advances: a worker can claim it again.
    claimed = claim_next_campaign_job(
        db_session,
        worker_id="b3-test-claimer",
        agent_ids=(AgentIdentifier.RESEARCH,),
    )
    assert claimed is not None
    assert claimed.agent_id is AgentIdentifier.RESEARCH
    assert claimed.status is AgentJobStatus.LEASED
    assert claimed.id in set(held.values())


def test_a_full_disable_enable_cycle_creates_no_extra_jobs_anywhere(db_session: Session) -> None:
    """Idempotency across the cycle, counted over every Agent, not just Research."""

    _, memberships = _cohort_at_research(db_session)
    before = {
        (job.campaign_contact_id, job.agent_id, job.id)
        for membership in memberships
        for agent_id in AgentIdentifier
        for job in _jobs(db_session, membership, agent_id)
    }

    _set_status(db_session, AgentControlStatus.DISABLED, agent_id=AgentIdentifier.RESEARCH)
    _set_status(db_session, AgentControlStatus.ENABLED, agent_id=AgentIdentifier.RESEARCH)
    _set_status(db_session, AgentControlStatus.DISABLED, agent_id=AgentIdentifier.RESEARCH)
    _set_status(db_session, AgentControlStatus.ENABLED, agent_id=AgentIdentifier.RESEARCH)

    after = {
        (job.campaign_contact_id, job.agent_id, job.id)
        for membership in memberships
        for agent_id in AgentIdentifier
        for job in _jobs(db_session, membership, agent_id)
    }
    assert after == before


def test_the_hold_grants_sending_nothing(db_session: Session) -> None:
    """No path here may make Sending reachable, skippable, or auto-skipped."""

    _, memberships = _cohort_at_research(db_session)
    _set_status(db_session, AgentControlStatus.DISABLED, agent_id=AgentIdentifier.RESEARCH)
    _set_status(db_session, AgentControlStatus.ENABLED, agent_id=AgentIdentifier.RESEARCH)

    assert get_agent_spec(AgentIdentifier.SENDING).skippable is False
    for membership in memberships:
        assert _stage(db_session, membership, AgentIdentifier.SENDING) is None
        assert _jobs(db_session, membership, AgentIdentifier.SENDING) == []

    # Disabling Sending itself reconciles nothing into a skip either, and cannot:
    # a non-skippable Agent has never been eligible for the auto-skip branch.
    disabled = _set_status(
        db_session, AgentControlStatus.DISABLED, agent_id=AgentIdentifier.SENDING
    )
    assert disabled.accepted
    sending_skips = db_session.scalars(
        select(PipelineEvent).where(
            PipelineEvent.agent_id == AgentIdentifier.SENDING,
            PipelineEvent.event_type == PipelineEventType.STAGE_SKIPPED,
        )
    ).all()
    assert list(sending_skips) == []
    for membership in memberships:
        assert _stage(db_session, membership, AgentIdentifier.SENDING) is None


def test_reconciliation_stays_scoped_locked_and_ordered(
    db_session: Session, captured_sql: list[str]
) -> None:
    """The repair must not cost reconciliation its bounds or its row locks."""

    _, first = _cohort_at_research(db_session, count=3, tag="a")
    second_campaign, second = _cohort_at_research(db_session, count=3, tag="b")

    captured_sql.clear()
    reconcile_agent_control(
        db_session,
        agent_id=AgentIdentifier.RESEARCH,
        campaign_id=second_campaign.id,
        actor="b3-test",
    )

    selects = [
        statement
        for statement in captured_sql
        if "campaign_contacts" in statement and "FOR UPDATE" in statement
    ]
    assert selects, "the membership selection no longer takes row locks"
    assert any("ORDER BY campaign_contacts.id" in statement for statement in selects)

    # Campaign scope is still honoured: the other Campaign's cohort is untouched
    # even though its Contacts match the same Agent.
    for membership in first:
        assert _stage(db_session, membership, AgentIdentifier.RESEARCH) is (
            PipelineStageStatus.WAITING
        )
    for membership in second:
        assert _stage(db_session, membership, AgentIdentifier.RESEARCH) is (
            PipelineStageStatus.WAITING
        )

    # And the explicit membership scope narrows it further still.
    captured_sql.clear()
    controls.set_global_control(
        db_session,
        agent_id=AgentIdentifier.RESEARCH,
        status=AgentControlStatus.DISABLED,
        reason="scoped reconciliation",
    )
    reconcile_agent_control(
        db_session,
        agent_id=AgentIdentifier.RESEARCH,
        campaign_contact_ids=[second[0].id],
        actor="b3-test",
    )
    assert _stage(db_session, second[0], AgentIdentifier.RESEARCH) is PipelineStageStatus.DISABLED
    for membership in [*first, *second[1:]]:
        assert _stage(db_session, membership, AgentIdentifier.RESEARCH) is (
            PipelineStageStatus.WAITING
        )


def test_the_ordinary_scheduler_keeps_its_auto_skip(db_session: Session) -> None:
    """The narrow repair must not disarm the walk it was not aimed at.

    A Contact arriving at a disabled skippable stage through the normal worker
    path is still stepped over automatically — that is the behaviour the batch
    scale depends on, and only reconciliation was ever the problem.
    """

    campaign, company = _campaign(db_session, tag="walk")
    contact = _contacts(db_session, count=1, company=company)[0]
    enrolled = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=True,
        desired_stage=AgentIdentifier.RESEARCH,
    )
    # Research is disabled by registry default and stays that way here.
    run_next(db_session, worker_id="b3-test")
    run_next(db_session, worker_id="b3-test")

    state = db_session.scalars(
        select(CampaignContactAgentState).where(
            CampaignContactAgentState.campaign_contact_id == enrolled.membership.id,
            CampaignContactAgentState.agent_id == AgentIdentifier.RESEARCH,
        )
    ).one()
    assert state.status is PipelineStageStatus.SKIPPED
    assert state.reason_code == "control_disabled_autoskip"

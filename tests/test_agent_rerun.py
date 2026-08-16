"""Running an Agent again after its reason for failing has been fixed.

The operation exists because neither existing retry can do it: both refuse a terminal
failure and an exhausted attempt budget, correctly, since retrying either would fail
identically. What they cannot know is that a *person* has changed something — a
feature switch, a domain, a defect in the Agent itself — which is the whole premise
here.

So the tests are mostly about restraint. A re-run must not become a way around a
control, a suppression, or a stage that is already running; and when it refuses, it
must say which contact and why, because "nothing happened" is the one outcome an
operator cannot act on.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.audit_event import AuditEvent
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    AgentJobStatus,
    CampaignMembershipStatus,
    PipelineEventType,
    PipelineStageStatus,
)
from app.models.pipeline import CampaignContactAgentState
from app.models.verification_job import AgentJob
from app.services import campaign_contacts, pipeline
from app.services.agents import controls
from app.services.agents import jobs as agent_jobs
from app.services.agents import rerun as agent_rerun
from app.services.agents.orchestrator import stage_job_key
from app.services.agents.registry import AGENT_SPECS, PIPELINE_ORDER
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests import workbench_scenario

WORKER = "rerun-test"


def _stop_stage(
    db: Session,
    scenario: workbench_scenario.Scenario,
    key: str,
    *,
    agent_id: AgentIdentifier = AgentIdentifier.RESEARCH,
    status: PipelineStageStatus = PipelineStageStatus.FAILED,
) -> None:
    """Leave one contact stopped at one Agent, the way a real failure leaves it.

    Driven through `pipeline.transition_stage` rather than by assigning to columns, so
    the stage state and the event history are exactly what production would hold.
    """

    membership = scenario.membership(key)
    # Complete everything this Agent depends on first. A contact cannot be stopped at
    # Research without having passed Identity and Company — and re-running a stage
    # whose dependency never finished is refused, correctly, so a fixture that skipped
    # this would be testing an impossible state.
    for upstream in PIPELINE_ORDER:
        if upstream is agent_id:
            break
        state = pipeline.agent_state(
            db, campaign_contact_id=membership.id, agent_id=upstream, create=True
        )
        assert state is not None
        assert state.status is not PipelineStageStatus.BLOCKED, (
            f"{key} is blocked at {upstream.value} and could never have reached "
            f"{agent_id.value}. Pick a contact that gets that far, or block it *after* "
            "the stage fails — which is the realistic order anyway."
        )
        if state.status is not PipelineStageStatus.COMPLETED:
            pipeline.transition_stage(
                db,
                membership=membership,
                agent_id=upstream,
                target=PipelineStageStatus.COMPLETED,
                event_type=PipelineEventType.STAGE_COMPLETED,
                actor="test-setup",
                reason_code="test_setup",
            )
    # A real failure leaves a failed *job* behind, and that is not incidental: it is
    # what makes both existing retries refuse, and it is what forces the re-run onto a
    # new generation. A fixture without it tests an easier problem than the one that
    # exists.
    #
    # Fail whichever job is *currently* live for this stage rather than always
    # enqueueing generation 1. After one re-run there is already a generation-2 job
    # waiting, and re-using the `:v1` key would hand back the job that failed the
    # first time — leaving the live one untouched, so the stage would still be in
    # flight. That is the wrong shape for "this Agent has stopped again".
    job = db.scalars(
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
                )
            ),
        )
        .order_by(AgentJob.created_at.desc(), AgentJob.id.desc())
    ).first()
    if job is None:
        existing = db.scalars(
            select(AgentJob.id).where(
                AgentJob.campaign_contact_id == membership.id,
                AgentJob.agent_id == agent_id,
            )
        ).all()
        job, _ = agent_jobs.enqueue_job(
            db,
            agent_id=agent_id,
            idempotency_key=stage_job_key(membership.id, agent_id, generation=len(existing) + 1),
            task_kind="advance_campaign_contact",
            max_attempts=AGENT_SPECS[agent_id].max_attempts,
            campaign_id=membership.campaign_id,
            campaign_contact_id=membership.id,
            contact_id=membership.contact_id,
            entity_type="campaign_contact",
            entity_id=membership.id,
        )
    job.attempts = job.max_attempts  # the budget is spent, as it would be
    agent_jobs.mark_failed(
        db,
        job,
        error_class="unexpected_error",
        reason="The Agent encountered an unexpected operational error (UnicodeEncodeError).",
        error_detail={"exception_type": "UnicodeEncodeError"},
    )
    pipeline.transition_stage(
        db,
        membership=membership,
        agent_id=agent_id,
        target=status,
        event_type=PipelineEventType.FAILED_TERMINAL,
        actor="test-setup",
        job=job,
        reason_code="unexpected_error",
        reason_detail="The Agent encountered an unexpected operational error.",
    )
    membership.current_stage = agent_id
    membership.next_stage = agent_id
    membership.pipeline_status = status
    db.flush()


def _enable(db: Session, agent_id: AgentIdentifier = AgentIdentifier.RESEARCH) -> None:
    controls.set_global_control(
        db, agent_id=agent_id, status=AgentControlStatus.ENABLED, config={"live": True}
    )
    db.flush()


@pytest.fixture()
def scenario(db_session: Session) -> workbench_scenario.Scenario:
    built = workbench_scenario.build(db_session)
    db_session.commit()
    return built


# ---------------------------------------------------------------------------
# The gap this fills
# ---------------------------------------------------------------------------


def test_the_existing_retries_cannot_do_this(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """Why a third operation had to exist at all.

    Both existing paths refuse a terminal failure. That is right — nothing in the
    pipeline can tell that a human has fixed the cause — and it is also why neither
    could be reused here.
    """

    _stop_stage(db_session, scenario, "healthy")
    membership = scenario.membership("healthy")

    with pytest.raises(campaign_contacts.CampaignContactError):
        campaign_contacts.retry_processing(db_session, campaign_contact_id=membership.id)


def test_a_stopped_stage_is_queued_again_with_a_fresh_job(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    _enable(db_session)
    _stop_stage(db_session, scenario, "healthy")
    membership = scenario.membership("healthy")

    outcome = agent_rerun.rerun_stage(
        db_session,
        campaign_id=scenario.campaign.id,
        agent_id=AgentIdentifier.RESEARCH,
        reason="fixed the CLI encoding",
    )

    assert outcome.requeued == (membership.id,)
    assert not outcome.refusals
    state = pipeline.agent_state(
        db_session,
        campaign_contact_id=membership.id,
        agent_id=AgentIdentifier.RESEARCH,
        create=False,
    )
    assert state is not None
    assert state.status is PipelineStageStatus.WAITING
    assert state.reason_code == "operator_rerun"

    queued = db_session.scalars(
        select(AgentJob).where(
            AgentJob.campaign_contact_id == membership.id,
            AgentJob.agent_id == AgentIdentifier.RESEARCH,
            AgentJob.status == AgentJobStatus.PENDING,
        )
    ).all()
    assert len(queued) == 1
    assert queued[0].attempts == 0, "a re-run must start with a fresh attempt budget"


def test_the_new_job_is_a_new_generation_so_the_old_one_survives(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """The idempotency key had to gain a generation for this to be possible at all.

    `enqueue_job` is idempotent on the key, so re-queueing the original `:v1` returned
    the same failed job and the contact never moved. Each re-run is its own
    generation, which also means the failure stays on record next to the retry rather
    than being overwritten by it.
    """

    _enable(db_session)
    membership = scenario.membership("healthy")
    # A first job under the ordinary key, then a failure.
    first_key = stage_job_key(membership.id, AgentIdentifier.RESEARCH)
    assert first_key.endswith(":v1")
    _stop_stage(db_session, scenario, "healthy")

    agent_rerun.rerun_stage(
        db_session, campaign_id=scenario.campaign.id, agent_id=AgentIdentifier.RESEARCH
    )

    keys = set(
        db_session.scalars(
            select(AgentJob.idempotency_key).where(
                AgentJob.campaign_contact_id == membership.id,
                AgentJob.agent_id == AgentIdentifier.RESEARCH,
            )
        ).all()
    )
    assert len(keys) >= 1
    assert any(key.endswith(":v1") is False for key in keys), (
        "the re-run must not reuse the generation the failed job holds"
    )


def test_running_again_twice_produces_two_generations_not_one_stuck_job(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    _enable(db_session)
    _stop_stage(db_session, scenario, "healthy")
    membership = scenario.membership("healthy")

    first = agent_rerun.rerun_stage(
        db_session, campaign_id=scenario.campaign.id, agent_id=AgentIdentifier.RESEARCH
    )
    assert first.accepted
    # Fail it again, as a still-broken Agent would.
    _stop_stage(db_session, scenario, "healthy")
    second = agent_rerun.rerun_stage(
        db_session, campaign_id=scenario.campaign.id, agent_id=AgentIdentifier.RESEARCH
    )
    assert second.accepted
    assert second.generation != first.generation

    total = db_session.scalars(
        select(AgentJob).where(
            AgentJob.campaign_contact_id == membership.id,
            AgentJob.agent_id == AgentIdentifier.RESEARCH,
        )
    ).all()
    assert len(total) >= 2, "each re-run keeps its own job, so the history is readable"
    assert sum(1 for job in total if job.status is AgentJobStatus.PENDING) == 1, (
        "exactly one job may be live for a stage"
    )


# ---------------------------------------------------------------------------
# What it refuses
# ---------------------------------------------------------------------------


def test_a_disabled_agent_is_refused_rather_than_queued_and_held(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """Queueing work an Agent cannot claim is worse than refusing: it looks like it worked."""

    controls.set_global_control(
        db_session,
        agent_id=AgentIdentifier.RESEARCH,
        status=AgentControlStatus.DISABLED,
        reason="switched off while I check the prompt",
    )
    _stop_stage(db_session, scenario, "healthy")

    with pytest.raises(agent_rerun.RerunError) as caught:
        agent_rerun.rerun_stage(
            db_session, campaign_id=scenario.campaign.id, agent_id=AgentIdentifier.RESEARCH
        )
    assert "disabled" in str(caught.value)
    assert "Enable it first" in str(caught.value)


def test_a_campaign_with_execution_off_is_refused(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    from app.services.campaigns import set_campaign_execution

    _enable(db_session)
    _stop_stage(db_session, scenario, "healthy")
    set_campaign_execution(db_session, scenario.campaign.id, enabled=False, actor="test")
    db_session.flush()

    with pytest.raises(agent_rerun.RerunError) as caught:
        agent_rerun.rerun_stage(
            db_session, campaign_id=scenario.campaign.id, agent_id=AgentIdentifier.RESEARCH
        )
    assert "execution is off" in str(caught.value)


def test_the_sending_agent_can_never_be_re_run(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """No adapter means nothing to run, and this must not become a way to send."""

    with pytest.raises(agent_rerun.RerunError) as caught:
        agent_rerun.rerun_stage(
            db_session, campaign_id=scenario.campaign.id, agent_id=AgentIdentifier.SENDING
        )
    assert "no executable adapter" in str(caught.value)


def _suppress_after_the_fact(db: Session, scenario: workbench_scenario.Scenario, key: str) -> None:
    """Opt a contact out *after* the stage failed.

    The realistic order, and the one the guard exists for: a contact blocked before
    Research never reaches Research, so the only way suppression can matter to a
    re-run is if it arrived in between.
    """

    from app.models.enums import SuppressionReason, SuppressionType
    from app.services.suppressions import add_suppression

    contact = scenario.contacts[key]
    assert contact.email is not None
    add_suppression(
        db,
        suppression_type=SuppressionType.EMAIL,
        value=contact.email,
        reason=SuppressionReason.OPT_OUT,
        source="test",
    )
    db.flush()


def test_a_suppressed_contact_is_refused_by_name(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """Suppression outranks every operator action, including this one."""

    _enable(db_session)
    _stop_stage(db_session, scenario, "healthy")
    _suppress_after_the_fact(db_session, scenario, "healthy")

    outcome = agent_rerun.rerun_stage(
        db_session, campaign_id=scenario.campaign.id, agent_id=AgentIdentifier.RESEARCH
    )
    assert not outcome.accepted
    assert [refusal.code for refusal in outcome.refusals] == ["eligibility_blocked"]
    assert "suppression" in outcome.refusals[0].reason
    assert outcome.refusals[0].contact_label


def test_an_archived_membership_is_refused_by_name(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    _enable(db_session)
    _stop_stage(db_session, scenario, "healthy")
    membership = scenario.membership("healthy")
    membership.membership_status = CampaignMembershipStatus.ARCHIVED
    db_session.flush()

    outcome = agent_rerun.rerun_stage(
        db_session, campaign_id=scenario.campaign.id, agent_id=AgentIdentifier.RESEARCH
    )
    assert not outcome.accepted
    assert outcome.refusals[0].code == "membership_archived"


def test_a_suppressed_contact_is_listed_but_offered_no_button(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """The refusal has to be visible *before* the click, not only after it.

    The service refuses a suppressed contact correctly, but a page that offers the
    button anyway is teaching the operator that the button is unreliable. Two things
    both have to hold: the contact stays in the list, because the Agent really has
    stopped on them and dropping the row would make the tile's own count
    unaccountable; and the row carries the standing reason instead of a control.
    """

    _enable(db_session)
    _stop_stage(db_session, scenario, "healthy")
    _suppress_after_the_fact(db_session, scenario, "healthy")

    listed = agent_rerun.candidates(
        db_session, campaign_id=scenario.campaign.id, agent_id=AgentIdentifier.RESEARCH
    )
    membership = scenario.membership("healthy")
    held = [c for c in listed if c.campaign_contact_id == membership.id]
    assert held, "a suppressed contact must still be counted as stopped here"
    assert not held[0].runnable
    assert held[0].standing_block is not None
    assert "suppression" in held[0].standing_block


def test_asking_whether_a_re_run_can_help_does_not_write(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """`candidates` runs on a GET, so it must not re-project eligibility onto the row.

    The authoritative check writes: it sets `eligibility_status`, `blocking_reasons`
    and can transition the stage. Calling it from a page render would mean a refresh
    silently changed the pipeline, which is why the read-only predicate exists.
    """

    _enable(db_session)
    _stop_stage(db_session, scenario, "healthy")
    _suppress_after_the_fact(db_session, scenario, "healthy")
    db_session.commit()
    membership = scenario.membership("healthy")
    before = (membership.eligibility_status, list(membership.blocking_reasons or []))

    agent_rerun.candidates(
        db_session, campaign_id=scenario.campaign.id, agent_id=AgentIdentifier.RESEARCH
    )

    assert not db_session.dirty, "a page render must not leave pending writes"
    assert (membership.eligibility_status, list(membership.blocking_reasons or [])) == before


def test_the_strip_hint_follows_failures_not_standing_blocks(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """`failure_counts` feeds a hint, so it counts the case a re-run is actually for.

    A blocked contact is usually a suppression, and a hint saying "open to run again"
    on a stage where every stopped contact is suppressed is a promise the click cannot
    keep. The scenario ships a contact blocked at Identity, which is exactly that case.
    """

    _enable(db_session)
    _stop_stage(db_session, scenario, "healthy")

    identity_blocked = db_session.scalars(
        select(CampaignContactAgentState).where(
            CampaignContactAgentState.agent_id == AgentIdentifier.IDENTITY,
            CampaignContactAgentState.status == PipelineStageStatus.BLOCKED,
        )
    ).all()
    assert identity_blocked, "the scenario is expected to ship a contact blocked at Identity"

    counts = agent_rerun.failure_counts(db_session, scenario.campaign.id)
    assert counts.get("research") == 1
    assert "identity" not in counts, (
        "the Identity block is a suppression, which a re-run cannot lift, so the strip "
        "must not invite one"
    )


def test_a_stage_that_is_not_stopped_is_left_alone(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """Only failed and blocked stages are eligible. Re-running a live one duplicates work."""

    _enable(db_session)
    outcome = agent_rerun.rerun_stage(
        db_session, campaign_id=scenario.campaign.id, agent_id=AgentIdentifier.RESEARCH
    )
    assert outcome.requeued == ()
    assert outcome.refusals == ()
    assert "Nothing was stopped" in outcome.message()


def test_a_single_contact_re_run_uses_the_same_guards(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    _enable(db_session)
    _stop_stage(db_session, scenario, "healthy")
    _suppress_after_the_fact(db_session, scenario, "healthy")
    membership = scenario.membership("healthy")

    outcome = agent_rerun.rerun_stage(
        db_session,
        campaign_id=scenario.campaign.id,
        agent_id=AgentIdentifier.RESEARCH,
        campaign_contact_id=membership.id,
    )
    assert not outcome.accepted
    assert outcome.refusals[0].code == "eligibility_blocked"


def test_asking_for_a_contact_that_is_not_stopped_is_refused_outright(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    _enable(db_session)
    with pytest.raises(agent_rerun.RerunError) as caught:
        agent_rerun.rerun_stage(
            db_session,
            campaign_id=scenario.campaign.id,
            agent_id=AgentIdentifier.RESEARCH,
            campaign_contact_id=scenario.membership("retrying").id,
        )
    assert "not stopped at this Agent" in str(caught.value)


# ---------------------------------------------------------------------------
# Bounds and the record
# ---------------------------------------------------------------------------


def test_a_bulk_re_run_is_capped_and_says_so(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """No silent truncation: a dropped remainder must be reported, not implied."""

    _enable(db_session)
    for key in ("healthy", "leased", "retrying", "terminal"):
        _stop_stage(db_session, scenario, key)

    outcome = agent_rerun.rerun_stage(
        db_session,
        campaign_id=scenario.campaign.id,
        agent_id=AgentIdentifier.RESEARCH,
        limit=2,
    )
    assert outcome.requeued_count == 2
    assert outcome.capped_at == 2
    assert "ceiling" in outcome.message()


def test_the_ceiling_cannot_be_raised_by_a_caller(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """A per-contact cost means the bound is the service's, not the page's."""

    _enable(db_session)
    _stop_stage(db_session, scenario, "healthy")
    outcome = agent_rerun.rerun_stage(
        db_session,
        campaign_id=scenario.campaign.id,
        agent_id=AgentIdentifier.RESEARCH,
        limit=10_000,
    )
    assert outcome.accepted


def test_the_re_run_is_recorded_with_what_changed(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    _enable(db_session)
    _stop_stage(db_session, scenario, "healthy")

    agent_rerun.rerun_stage(
        db_session,
        campaign_id=scenario.campaign.id,
        agent_id=AgentIdentifier.RESEARCH,
        reason="entered the domain by hand",
    )

    event = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "agent.operator_rerun")
    ).one()
    assert event.reason == "entered the domain by hand"
    context = event.context or {}
    assert context["agent_id"] == "research"
    assert context["requeued"] == 1
    # The cost of the Agent is part of the record, because the decision to spend was
    # the operator's.
    assert context["spends_per_contact"] is True


def test_the_previous_failure_stays_readable_on_the_stage_history(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    _enable(db_session)
    _stop_stage(db_session, scenario, "healthy")
    membership = scenario.membership("healthy")

    agent_rerun.rerun_stage(
        db_session, campaign_id=scenario.campaign.id, agent_id=AgentIdentifier.RESEARCH
    )

    snapshot = pipeline.pipeline_snapshot(db_session, campaign_contact_id=membership.id)
    assert snapshot is not None
    codes = [event.reason_code for event in snapshot.events]
    assert "unexpected_error" in codes, "the failure that prompted the re-run must survive"
    assert "operator_rerun" in codes


def test_failure_counts_report_only_failed_stages(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    _stop_stage(db_session, scenario, "healthy")
    _stop_stage(db_session, scenario, "leased", agent_id=AgentIdentifier.EMAIL)

    counts = agent_rerun.failure_counts(db_session, scenario.campaign.id)
    assert counts.get("research") == 1
    assert counts.get("email") == 1
    # Capture is completed for every enrolled contact, so it must never be offered.
    assert "capture" not in counts


# ---------------------------------------------------------------------------
# Over HTTP
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__AGENT_WORKBENCH", "true")
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as app_client:
        yield app_client


def test_the_control_appears_only_where_something_is_stopped(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    _enable(db_session)
    url = f"/app/admin/campaigns/{scenario.campaign.id}/diagnostics?stage=research"

    assert "Run again for all" not in client.get(url).text

    _stop_stage(db_session, scenario, "healthy")
    db_session.commit()
    body = client.get(url).text
    # "all 1" would be the natural output of a count-driven label and reads as a bug.
    assert "Run again for all" not in body
    assert "Run again for this person" in body
    assert "stopped here" in body


def test_the_page_states_the_reason_where_a_re_run_cannot_help(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """The rendered page must not offer a control whose only outcome is a refusal."""

    _enable(db_session)
    _stop_stage(db_session, scenario, "healthy")
    _suppress_after_the_fact(db_session, scenario, "healthy")
    db_session.commit()

    body = client.get(
        f"/app/admin/campaigns/{scenario.campaign.id}/diagnostics?stage=research"
    ).text
    assert "1 stopped here" in body
    assert "outranks a re-run" in body
    assert "Run again" not in body


def test_a_stage_stopped_only_by_a_block_still_explains_itself(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    """The panel reads the list; it must not be gated on the strip's failure count.

    An earlier version of this gated the panel on the same number that feeds the strip
    hint. Because that number counts failures only, a stage whose one stopped contact
    was *blocked* rendered no panel at all — losing the explanation for precisely the
    contact that most needed one. The scenario's Identity block is that case.
    """

    body = client.get(
        f"/app/admin/campaigns/{scenario.campaign.id}/diagnostics?stage=identity"
    ).text
    assert "stopped here" in body
    assert "outranks a re-run" in body


def test_the_page_warns_that_the_agent_spends_before_a_bulk_run(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    _enable(db_session)
    _stop_stage(db_session, scenario, "healthy")
    db_session.commit()
    body = client.get(
        f"/app/admin/campaigns/{scenario.campaign.id}/diagnostics?stage=research"
    ).text
    assert "This Agent spends per person" in body


def test_running_again_over_http_reports_what_happened(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    _enable(db_session)
    _stop_stage(db_session, scenario, "healthy")
    db_session.commit()

    response = client.post(
        f"/app/admin/campaigns/{scenario.campaign.id}/agents/research/rerun",
        data={"reason": "fixed it"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "queued again for 1 contact" in response.text


def test_a_refusal_over_http_names_the_contact(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """The worst outcome is a button that appears to do nothing."""

    _enable(db_session)
    _stop_stage(db_session, scenario, "healthy")
    _suppress_after_the_fact(db_session, scenario, "healthy")
    db_session.commit()

    response = client.post(
        f"/app/admin/campaigns/{scenario.campaign.id}/agents/research/rerun",
        data={},
        follow_redirects=True,
    )
    assert "not re-run" in response.text
    assert scenario.contacts["healthy"].last_name in response.text


def test_an_unknown_agent_or_campaign_is_refused(client: TestClient) -> None:
    assert (
        client.post(
            f"/app/admin/campaigns/{uuid.uuid4()}/agents/nonsense/rerun",
            data={},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert (
        client.post(
            "/app/admin/campaigns/not-a-uuid/agents/research/rerun", data={}, follow_redirects=False
        ).status_code
        == 303
    )

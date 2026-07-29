"""Workbench read model and command path over the real Phase 2 backbone.

Every fact these tests assert was produced by a Phase 2 service — the Campaign by
``services.campaigns``, the memberships by ``services.campaign_contacts``, the
jobs and pipeline events by the orchestrator and the durable queue. That matters
more than usual here: the Workbench's entire job is to report Phase 2 truthfully,
so a test that seeded its own rows would prove nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    AgentJobStatus,
    CampaignContactEligibility,
    CampaignMembershipStatus,
    PipelineStageStatus,
)
from app.services.agents import controls as agent_controls
from app.services.agents.registry import AGENT_SPECS, PIPELINE_ORDER
from app.services.workbench_agents import (
    PhaseTwoWorkbenchReader,
    WorkbenchCommandError,
    WorkbenchCommands,
    WorkbenchReader,
)
from app.services.workbench_agents.sanitize import sanitize_mapping, sanitize_text
from sqlalchemy.orm import Session

from tests import workbench_scenario


@pytest.fixture()
def scenario(db_session: Session) -> Iterator[workbench_scenario.Scenario]:
    yield workbench_scenario.build(db_session)


@pytest.fixture()
def reader(db_session: Session) -> PhaseTwoWorkbenchReader:
    return PhaseTwoWorkbenchReader(db_session)


@pytest.fixture()
def commands(db_session: Session) -> WorkbenchCommands:
    return WorkbenchCommands(db_session)


# --- The read model conforms to the port -------------------------------------


def test_the_reader_satisfies_the_port(reader: PhaseTwoWorkbenchReader) -> None:
    assert isinstance(reader, WorkbenchReader)


# --- Agent overview ----------------------------------------------------------


def test_the_overview_lists_every_registered_agent_in_registry_order(
    scenario: workbench_scenario.Scenario, reader: PhaseTwoWorkbenchReader
) -> None:
    """The order is the Phase 2 registry's, not a constant in the UI."""

    overview = reader.overview()
    assert [card.agent_id for card in overview.agents] == list(PIPELINE_ORDER)
    assert len(overview.agents) == len(AGENT_SPECS)


def test_agent_cards_report_the_registry_default_until_a_control_is_stored(
    scenario: workbench_scenario.Scenario, reader: PhaseTwoWorkbenchReader
) -> None:
    overview = reader.overview()
    identity = overview.agent(AgentIdentifier.IDENTITY)
    sending = overview.agent(AgentIdentifier.SENDING)
    assert identity is not None and sending is not None
    assert identity.control.source == "registry_default"
    assert identity.control.status is AgentControlStatus.ENABLED
    # Sending is disabled by default and must stay that way with no stored control.
    assert sending.control.status is AgentControlStatus.DISABLED
    assert overview.sending_stopped is True


def test_agent_queue_counts_come_from_the_real_job_rows(
    scenario: workbench_scenario.Scenario, reader: PhaseTwoWorkbenchReader
) -> None:
    overview = reader.overview()
    totals = sum(card.queue.total for card in overview.agents)
    assert totals == overview.queue.total
    assert overview.queue.total > 0
    # The fixture leased one job, scheduled one retry and failed one terminally.
    assert overview.queue.count("leased") >= 1
    assert overview.queue.retrying >= 1
    assert overview.queue.terminal_failures >= 1


def test_terminal_and_retryable_failures_are_counted_apart(
    scenario: workbench_scenario.Scenario, reader: PhaseTwoWorkbenchReader
) -> None:
    """They need different operator responses, so one number cannot serve both."""

    overview = reader.overview()
    assert overview.queue.failed == (
        overview.queue.terminal_failures + overview.queue.retryable_failures
    )


def test_a_campaign_override_is_visible_on_the_agent_card(
    scenario: workbench_scenario.Scenario,
    reader: PhaseTwoWorkbenchReader,
    commands: WorkbenchCommands,
) -> None:
    outcome = commands.set_campaign_override(
        scenario.campaign.id,
        AgentIdentifier.EMAIL,
        AgentControlStatus.DISABLED,
        expected_version=None,
        reason="pausing discovery for the pilot",
    )
    assert outcome.accepted
    card = reader.overview().agent(AgentIdentifier.EMAIL)
    assert card is not None
    assert [name for _id, name, _status in card.overriding_campaigns] == [scenario.campaign.name]


# --- Campaign execution ------------------------------------------------------


def test_campaign_execution_reports_stage_distribution_and_blocks(
    scenario: workbench_scenario.Scenario, reader: PhaseTwoWorkbenchReader
) -> None:
    execution = reader.campaign_execution(scenario.campaign.id)
    assert execution is not None
    assert execution.enrolled_contacts == 6
    assert sum(execution.stage_counts.values()) == execution.enrolled_contacts
    assert sum(execution.pipeline_status_counts.values()) == execution.enrolled_contacts
    # The suppressed Contact is blocked, and says so.
    assert execution.suppressed_contacts >= 1
    assert execution.blocked_contacts >= 1


def test_campaign_execution_is_scoped_to_its_own_campaign(
    scenario: workbench_scenario.Scenario, reader: PhaseTwoWorkbenchReader
) -> None:
    other = reader.campaign_execution(scenario.other_campaign.id)
    assert other is not None
    assert other.enrolled_contacts == 1
    assert other.execution_enabled is False


def test_an_unknown_campaign_reads_as_absent(reader: PhaseTwoWorkbenchReader) -> None:
    assert reader.campaign_execution(uuid.uuid4()) is None


def test_stage_filtering_narrows_the_contact_page(
    scenario: workbench_scenario.Scenario, reader: PhaseTwoWorkbenchReader
) -> None:
    everything = reader.campaign_execution(scenario.campaign.id)
    assert everything is not None
    stage = next(iter(everything.contacts)).current_stage
    if stage is None:
        pytest.skip("the fixture produced no Contact with a current stage")
    filtered = reader.campaign_execution(scenario.campaign.id, stage=stage)
    assert filtered is not None
    assert filtered.contact_total <= everything.contact_total
    assert all(row.current_stage is stage for row in filtered.contacts)


# --- Contact execution -------------------------------------------------------


def test_contact_execution_shows_every_stage_and_the_ordered_history(
    scenario: workbench_scenario.Scenario, reader: PhaseTwoWorkbenchReader
) -> None:
    membership = scenario.membership("terminal")
    execution = reader.contact_execution(scenario.campaign.id, membership.id)
    assert execution is not None
    assert [stage.agent_id for stage in execution.stages] == list(PIPELINE_ORDER)
    assert execution.events, "the orchestrator should have committed pipeline events"
    times = [event.occurred_at for event in execution.events]
    assert times == sorted(times), "history must be ordered oldest first"


def test_a_stage_is_complete_only_when_an_event_committed_it(
    scenario: workbench_scenario.Scenario, reader: PhaseTwoWorkbenchReader
) -> None:
    """A succeeded job alone never sets ``outcome_committed``."""

    execution = reader.contact_execution(scenario.campaign.id, scenario.membership("healthy").id)
    assert execution is not None
    for stage in execution.stages:
        if stage.status is not PipelineStageStatus.COMPLETED:
            assert stage.outcome_committed is False


def test_the_suppressed_contact_reports_its_block_and_never_hides_it(
    scenario: workbench_scenario.Scenario, reader: PhaseTwoWorkbenchReader
) -> None:
    execution = reader.contact_execution(scenario.campaign.id, scenario.membership("suppressed").id)
    assert execution is not None
    assert execution.suppressed is True
    assert execution.eligibility is CampaignContactEligibility.BLOCKED
    assert execution.terminal_block


def test_contact_execution_refuses_a_membership_from_another_campaign(
    scenario: workbench_scenario.Scenario, reader: PhaseTwoWorkbenchReader
) -> None:
    assert reader.contact_execution(scenario.campaign.id, scenario.membership("other").id) is None


# --- Jobs --------------------------------------------------------------------


def test_the_job_list_filters_by_agent_status_and_campaign(
    scenario: workbench_scenario.Scenario, reader: PhaseTwoWorkbenchReader
) -> None:
    everything = reader.jobs()
    assert everything.total > 0
    failed = reader.jobs(status="failed")
    assert failed.total >= 1
    assert all(job.stored_status is AgentJobStatus.FAILED for job in failed.jobs)
    scoped = reader.jobs(campaign_id=scenario.other_campaign.id)
    assert all(job.campaign_id == scenario.other_campaign.id for job in scoped.jobs)
    # An unrecognised status widens rather than raising.
    assert reader.jobs(status="nonsense").total == everything.total


def test_a_job_view_exposes_lease_and_retry_timing(
    scenario: workbench_scenario.Scenario, reader: PhaseTwoWorkbenchReader
) -> None:
    leased = [job for job in reader.jobs().jobs if job.public_status == "leased"]
    retrying = [job for job in reader.jobs().jobs if job.public_status == "retrying"]
    assert leased and retrying
    assert leased[0].lease_held is True
    assert leased[0].lease_expires_at is not None
    assert retrying[0].next_run_at is not None


def test_a_terminal_failure_is_not_offered_a_retry(
    scenario: workbench_scenario.Scenario, reader: PhaseTwoWorkbenchReader
) -> None:
    failed = reader.jobs(status="failed").jobs
    assert failed
    for job in failed:
        assert job.terminal_failure is True
        assert job.retry_eligible is False
        assert job.retry_refusal


def test_an_unknown_job_reads_as_absent(reader: PhaseTwoWorkbenchReader) -> None:
    assert reader.job(uuid.uuid4()) is None


# --- Secrets -----------------------------------------------------------------


def test_credential_shaped_text_is_redacted() -> None:
    dirty = (
        "GET https://api.example.com/v3?api=sk_live_9f8a7b6c5d&email=a@b.com failed; "
        "fallback postgresql://svc:hunter2@db.internal/app; Authorization: Bearer abcdef123456"
    )
    clean = sanitize_text(dirty)
    assert clean is not None
    assert "sk_live_9f8a7b6c5d" not in clean
    assert "hunter2" not in clean
    assert "abcdef123456" not in clean
    # The shape survives, so the failure stays diagnosable.
    assert "api.example.com" in clean
    assert "[redacted]" in clean


def test_sensitive_payload_keys_are_never_rendered() -> None:
    clean = sanitize_mapping(
        {
            "api_key": "sk_live_secret",
            "DATABASE_URL": "postgresql://u:p@h/db",
            "nested": {"Token": "abc", "safe": "keep me"},
            "list": ["Bearer aaaaaaaaaaaa"],
        }
    )
    assert clean is not None
    assert clean["api_key"] == "[redacted]"
    assert clean["DATABASE_URL"] == "[redacted]"
    assert clean["nested"]["Token"] == "[redacted]"
    assert clean["nested"]["safe"] == "keep me"
    assert "aaaaaaaaaaaa" not in clean["list"][0]


def test_a_job_view_never_carries_a_raw_provider_key(
    scenario: workbench_scenario.Scenario,
    db_session: Session,
    reader: PhaseTwoWorkbenchReader,
) -> None:
    job = scenario.job_for(db_session, "terminal")
    assert job is not None
    job.last_error = "provider rejected https://api.example.com/v3?api=sk_live_TOPSECRET"
    job.input_reference = {"api_key": "sk_live_TOPSECRET", "email": "a@b.example.com"}
    db_session.flush()
    view = reader.job(job.id)
    assert view is not None
    assert view.error_message is not None
    assert "sk_live_TOPSECRET" not in view.error_message
    assert view.input_reference["api_key"] == "[redacted]"


# --- Commands: global control ------------------------------------------------


def test_pausing_an_agent_globally_is_accepted_and_reconciles(
    scenario: workbench_scenario.Scenario,
    commands: WorkbenchCommands,
    db_session: Session,
) -> None:
    outcome = commands.set_global_agent_status(
        AgentIdentifier.IDENTITY,
        AgentControlStatus.PAUSED,
        expected_version=None,
        reason="holding for a policy review",
    )
    assert outcome.accepted
    assert outcome.in_flight_note
    control = agent_controls.effective_control(
        db_session, campaign=scenario.campaign, agent_id=AgentIdentifier.IDENTITY
    )
    assert control.status is AgentControlStatus.PAUSED


def test_a_stale_control_version_is_refused_without_overwriting(
    scenario: workbench_scenario.Scenario,
    commands: WorkbenchCommands,
    db_session: Session,
) -> None:
    """The whole point of the guard: a screen from before someone else's change."""

    first = commands.set_global_agent_status(
        AgentIdentifier.IDENTITY, AgentControlStatus.PAUSED, expected_version=None
    )
    assert first.accepted
    # A second operator's page still believes there is no stored control.
    stale = commands.set_global_agent_status(
        AgentIdentifier.IDENTITY, AgentControlStatus.DISABLED, expected_version=None
    )
    assert stale.accepted is False
    assert stale.refusal_reason and "Reload" in stale.refusal_reason
    control = agent_controls.effective_control(
        db_session, campaign=scenario.campaign, agent_id=AgentIdentifier.IDENTITY
    )
    assert control.status is AgentControlStatus.PAUSED, "the newer decision must survive"


def test_the_same_command_twice_with_a_current_version_is_idempotent(
    scenario: workbench_scenario.Scenario,
    commands: WorkbenchCommands,
    db_session: Session,
) -> None:
    first = commands.set_global_agent_status(
        AgentIdentifier.IDENTITY, AgentControlStatus.PAUSED, expected_version=None
    )
    assert first.accepted
    version = commands._global_version(AgentIdentifier.IDENTITY)
    second = commands.set_global_agent_status(
        AgentIdentifier.IDENTITY, AgentControlStatus.PAUSED, expected_version=version
    )
    assert second.accepted
    assert commands._global_version(AgentIdentifier.IDENTITY) == version


def test_an_agent_without_an_adapter_cannot_be_enabled(
    scenario: workbench_scenario.Scenario, commands: WorkbenchCommands
) -> None:
    outcome = commands.set_global_agent_status(
        AgentIdentifier.SENDING, AgentControlStatus.ENABLED, expected_version=None
    )
    assert outcome.accepted is False
    assert outcome.refusal_reason and "adapter" in outcome.refusal_reason


def test_capture_cannot_be_paused(
    scenario: workbench_scenario.Scenario, commands: WorkbenchCommands
) -> None:
    """A Phase 2 rule, surfaced rather than re-implemented."""

    outcome = commands.set_global_agent_status(
        AgentIdentifier.CAPTURE, AgentControlStatus.PAUSED, expected_version=None
    )
    assert outcome.accepted is False
    assert outcome.refusal_reason


# --- Commands: Campaign overrides --------------------------------------------


def test_an_override_changes_only_its_own_campaign(
    scenario: workbench_scenario.Scenario,
    commands: WorkbenchCommands,
    db_session: Session,
) -> None:
    outcome = commands.set_campaign_override(
        scenario.campaign.id,
        AgentIdentifier.IDENTITY,
        AgentControlStatus.DISABLED,
        expected_version=None,
        reason="pausing this pilot only",
    )
    assert outcome.accepted
    here = agent_controls.effective_control(
        db_session, campaign=scenario.campaign, agent_id=AgentIdentifier.IDENTITY
    )
    there = agent_controls.effective_control(
        db_session, campaign=scenario.other_campaign, agent_id=AgentIdentifier.IDENTITY
    )
    assert here.status is AgentControlStatus.DISABLED
    assert here.source == "campaign_override"
    # The other Campaign has execution off, so its own reason wins — and it is
    # *not* the override, which is the point.
    assert there.source != "campaign_override"


def test_clearing_an_override_returns_to_the_inherited_control(
    scenario: workbench_scenario.Scenario,
    commands: WorkbenchCommands,
    db_session: Session,
) -> None:
    applied = commands.set_campaign_override(
        scenario.campaign.id,
        AgentIdentifier.IDENTITY,
        AgentControlStatus.DISABLED,
        expected_version=None,
    )
    assert applied.accepted
    version = commands._campaign_version(scenario.campaign.id, AgentIdentifier.IDENTITY)
    cleared = commands.clear_campaign_override(
        scenario.campaign.id, AgentIdentifier.IDENTITY, expected_version=version
    )
    assert cleared.accepted
    control = agent_controls.effective_control(
        db_session, campaign=scenario.campaign, agent_id=AgentIdentifier.IDENTITY
    )
    assert control.source != "campaign_override"
    # Clearing nothing is refused rather than reported as a change.
    again = commands.clear_campaign_override(
        scenario.campaign.id, AgentIdentifier.IDENTITY, expected_version=None
    )
    assert again.accepted is False


def test_an_override_for_an_unknown_campaign_is_an_error(
    scenario: workbench_scenario.Scenario, commands: WorkbenchCommands
) -> None:
    with pytest.raises(WorkbenchCommandError):
        commands.set_campaign_override(
            uuid.uuid4(),
            AgentIdentifier.IDENTITY,
            AgentControlStatus.PAUSED,
            expected_version=None,
        )


# --- Commands: sending -------------------------------------------------------


def test_stopping_sending_is_immediately_visible(
    scenario: workbench_scenario.Scenario,
    commands: WorkbenchCommands,
    reader: PhaseTwoWorkbenchReader,
) -> None:
    outcome = commands.stop_sending(expected_version=None, reason="deliverability incident")
    assert outcome.accepted
    overview = reader.overview()
    assert overview.sending_stopped is True
    assert overview.sending_control.status is AgentControlStatus.DISABLED


def test_resuming_sending_goes_through_the_phase_two_safety_check(
    scenario: workbench_scenario.Scenario, commands: WorkbenchCommands
) -> None:
    """It is a request, not a switch: Phase 2 refuses while no adapter exists."""

    stopped = commands.stop_sending(expected_version=None)
    assert stopped.accepted
    version = commands._global_version(AgentIdentifier.SENDING)
    resumed = commands.resume_sending(expected_version=version)
    assert resumed.accepted is False
    assert resumed.refusal_reason and "adapter" in resumed.refusal_reason


# --- Commands: jobs and Campaign Contacts ------------------------------------


def test_retry_is_refused_for_a_terminal_failure(
    scenario: workbench_scenario.Scenario,
    commands: WorkbenchCommands,
    db_session: Session,
) -> None:
    job = scenario.job_for(db_session, "terminal")
    assert job is not None and job.status is AgentJobStatus.FAILED
    outcome = commands.retry_job(job.id)
    assert outcome.accepted is False
    assert outcome.refusal_reason


def test_retry_is_refused_for_a_job_that_is_not_failed(
    scenario: workbench_scenario.Scenario,
    commands: WorkbenchCommands,
    db_session: Session,
) -> None:
    job = scenario.job_for(db_session, "leased")
    assert job is not None
    outcome = commands.retry_job(job.id)
    assert outcome.accepted is False


def test_retry_is_accepted_for_a_retryable_failure(
    scenario: workbench_scenario.Scenario,
    commands: WorkbenchCommands,
    db_session: Session,
) -> None:
    job = scenario.job_for(db_session, "terminal")
    assert job is not None
    workbench_scenario.make_retryable_failure(db_session, job)
    outcome = commands.retry_job(job.id, reason="the provider recovered")
    assert outcome.accepted
    db_session.refresh(job)
    assert job.status is AgentJobStatus.PENDING


def test_retrying_an_unknown_job_is_an_error(commands: WorkbenchCommands) -> None:
    with pytest.raises(WorkbenchCommandError):
        commands.retry_job(uuid.uuid4())


def test_pausing_and_resuming_a_campaign_contact_round_trips(
    scenario: workbench_scenario.Scenario,
    commands: WorkbenchCommands,
    db_session: Session,
) -> None:
    membership = scenario.membership("healthy")
    paused = commands.pause_contact(membership.id, reason="holding pending a data fix")
    assert paused.accepted
    db_session.refresh(membership)
    assert membership.membership_status is CampaignMembershipStatus.PAUSED

    resumed = commands.resume_contact(membership.id)
    assert resumed.accepted
    db_session.refresh(membership)
    assert membership.membership_status is CampaignMembershipStatus.ACTIVE


def test_a_paused_contact_refuses_a_retry(
    scenario: workbench_scenario.Scenario,
    commands: WorkbenchCommands,
) -> None:
    membership = scenario.membership("healthy")
    assert commands.pause_contact(membership.id, reason="hold").accepted
    outcome = commands.retry_contact(membership.id, reason="try again")
    assert outcome.accepted is False
    assert outcome.refusal_reason


def test_a_suppressed_contact_can_never_be_retried(
    scenario: workbench_scenario.Scenario, commands: WorkbenchCommands
) -> None:
    """Suppression is authoritative above every Agent control."""

    outcome = commands.retry_contact(
        scenario.membership("suppressed").id, reason="please try anyway"
    )
    assert outcome.accepted is False
    assert outcome.refusal_reason


def test_retrying_an_unknown_campaign_contact_is_an_error(commands: WorkbenchCommands) -> None:
    with pytest.raises(WorkbenchCommandError):
        commands.retry_contact(uuid.uuid4(), reason="x")


def test_skipping_a_critical_stage_is_refused(
    scenario: workbench_scenario.Scenario,
    commands: WorkbenchCommands,
) -> None:
    """Only a stage the registry marks skippable may be passed over.

    The fixture's Contacts sit on Identity, which is not skippable, so this is
    the refusal an operator actually meets — and it must explain itself rather
    than silently doing nothing.
    """

    outcome = commands.skip_stage(
        scenario.membership("healthy").id, reason="already resolved by hand"
    )
    assert outcome.accepted is False
    assert outcome.refusal_reason and "skipped" in outcome.refusal_reason


def test_skipping_a_stage_for_an_unknown_contact_is_an_error(
    commands: WorkbenchCommands,
) -> None:
    with pytest.raises(WorkbenchCommandError):
        commands.skip_stage(uuid.uuid4(), reason="x")


def test_a_command_outcome_always_explains_a_refusal(
    scenario: workbench_scenario.Scenario, commands: WorkbenchCommands
) -> None:
    """A refusal with no reason would leave the operator with nothing to act on."""

    refusals = [
        commands.set_global_agent_status(
            AgentIdentifier.SENDING, AgentControlStatus.ENABLED, expected_version=None
        ),
        commands.retry_contact(scenario.membership("suppressed").id, reason="try"),
        commands.clear_campaign_override(
            scenario.campaign.id, AgentIdentifier.RESEARCH, expected_version=None
        ),
    ]
    for outcome in refusals:
        assert outcome.accepted is False
        assert outcome.refusal_reason, outcome.action

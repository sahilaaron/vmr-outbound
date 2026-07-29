"""Immutable view models for the operator Workbench.

These are presentation DTOs projected from Phase 2 domain objects. They carry no
authority: every state value on them is a Phase 2 enum value that Phase 2
decided, and nothing here may derive a state Phase 2 did not commit.

Two rules are enforced by the shapes rather than by convention:

* **A stage is complete only when Phase 2 says so.** :class:`StageView` reads
  ``CampaignContactAgentState.status`` and the pipeline events behind it. A
  succeeded job is not, on its own, a completed stage — a job can succeed while
  the committed domain outcome routes the Contact somewhere else entirely — so
  no view infers one from the other.
* **A refusal is displayable, not exceptional.** ``CommandOutcome`` (in the
  command module) carries the reason a control was declined, so the UI never
  has to guess why something did not happen.

The vocabulary distinctions the operator must be able to read at a glance —
queued vs leased vs running vs retry-scheduled vs blocked vs refused vs
completed-with-outcome vs completed-without-usable-result vs terminal failure vs
cancelled vs globally disabled vs disabled-for-this-Campaign — are all present
here as separate fields, never collapsed into one label.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    AgentJobStatus,
    CampaignContactEligibility,
    CampaignMembershipStatus,
    PipelineEventType,
    PipelineStageStatus,
)

#: Public job vocabulary, as Phase 2 serializes it (``jobs.public_status``).
#: Repeated here only so templates can iterate the filter chips in a stable
#: order; the values themselves come from Phase 2.
PUBLIC_JOB_STATES: tuple[str, ...] = (
    "queued",
    "leased",
    "running",
    "retrying",
    "paused",
    "failed",
    "completed",
    "cancelled",
)


@dataclass(frozen=True)
class QueueCounts:
    """Job counts by Phase 2 public status."""

    by_status: dict[str, int] = field(default_factory=dict)
    #: Failures Phase 2 marked non-retryable. Kept apart from ``failed`` because
    #: the operator's next action is different: a terminal failure needs a
    #: decision, a retryable one needs a retry.
    terminal_failures: int = 0

    def count(self, status: str) -> int:
        return self.by_status.get(status, 0)

    @property
    def running(self) -> int:
        return self.count("running") + self.count("leased")

    @property
    def queued(self) -> int:
        return self.count("queued")

    @property
    def retrying(self) -> int:
        return self.count("retrying")

    @property
    def failed(self) -> int:
        return self.count("failed")

    @property
    def retryable_failures(self) -> int:
        return max(0, self.failed - self.terminal_failures)

    @property
    def open_work(self) -> int:
        """Work that still occupies the queue."""

        return (
            self.count("queued")
            + self.count("leased")
            + self.count("running")
            + self.count("retrying")
            + self.count("paused")
        )

    @property
    def total(self) -> int:
        return sum(self.by_status.values())


@dataclass(frozen=True)
class ControlView:
    """One Agent's control state, and exactly where it came from.

    ``source`` is Phase 2's own word for the precedence that won —
    ``registry_default``, ``global``, ``campaign_override``, ``campaign_execution``
    or ``registry`` — so an operator can always tell whether they are looking at
    a global decision, a Campaign-scoped one, or a registry fact they cannot
    override.
    """

    agent_id: AgentIdentifier
    display_name: str
    position: int
    status: AgentControlStatus
    source: str
    reason: str | None
    implemented: bool
    global_status: AgentControlStatus
    global_version: int | None = None
    campaign_version: int | None = None
    updated_by: str | None = None
    updated_at: datetime | None = None

    @property
    def campaign_scoped(self) -> bool:
        return self.source == "campaign_override"

    @property
    def blocked_by_campaign_execution(self) -> bool:
        return self.source == "campaign_execution"

    @property
    def accepting_work(self) -> bool:
        return self.status is AgentControlStatus.ENABLED


@dataclass(frozen=True)
class ActivityView:
    """One pipeline event, rendered as an activity line."""

    event_id: uuid.UUID
    occurred_at: datetime
    event_type: PipelineEventType
    agent_id: AgentIdentifier | None
    campaign_id: uuid.UUID | None
    campaign_name: str | None
    campaign_contact_id: uuid.UUID
    contact_label: str | None
    job_id: uuid.UUID | None
    from_status: PipelineStageStatus | None
    to_status: PipelineStageStatus | None
    reason_code: str | None
    reason_detail: str | None
    retryable: bool
    actor: str

    @property
    def is_failure(self) -> bool:
        return self.event_type in (
            PipelineEventType.FAILED_TERMINAL,
            PipelineEventType.FAILED_RETRYABLE,
        )


@dataclass(frozen=True)
class AgentCardView:
    """One Agent on the overview grid."""

    agent_id: AgentIdentifier
    display_name: str
    position: int
    control: ControlView
    queue: QueueCounts
    #: Campaigns whose override changes this Agent's behaviour, by name.
    overriding_campaigns: tuple[tuple[uuid.UUID, str, AgentControlStatus], ...] = ()
    latest_activity_at: datetime | None = None
    latest_activity_summary: str | None = None
    dependencies: tuple[AgentIdentifier, ...] = ()
    skippable: bool = False
    max_attempts: int = 0

    @property
    def globally_stopped(self) -> bool:
        return self.control.global_status is not AgentControlStatus.ENABLED


@dataclass(frozen=True)
class WorkbenchOverviewView:
    """The control-room landing page."""

    generated_at: datetime
    agents: tuple[AgentCardView, ...]
    queue: QueueCounts
    campaigns: tuple[CampaignSummaryView, ...]
    recent_activity: tuple[ActivityView, ...]
    sending_control: ControlView
    #: Campaign Contacts held by something authoritative, across all Campaigns.
    blocked_contacts: int = 0
    suppressed_contacts: int = 0
    active_contacts: int = 0
    waiting_contacts: int = 0
    completed_contacts: int = 0

    @property
    def sending_stopped(self) -> bool:
        return self.sending_control.global_status is not AgentControlStatus.ENABLED

    @property
    def total_contacts(self) -> int:
        return sum(campaign.enrolled_contacts for campaign in self.campaigns)

    def agent(self, agent_id: AgentIdentifier) -> AgentCardView | None:
        return next((card for card in self.agents if card.agent_id is agent_id), None)


@dataclass(frozen=True)
class CampaignSummaryView:
    """One Campaign as the overview lists it."""

    campaign_id: uuid.UUID
    name: str
    status: str
    execution_enabled: bool
    settings_version: int
    enrolled_contacts: int
    stage_counts: dict[str, int]
    pipeline_status_counts: dict[str, int]
    blocked_contacts: int
    suppressed_contacts: int
    override_count: int
    queue: QueueCounts
    sending_status: AgentControlStatus
    latest_activity_at: datetime | None = None

    @property
    def completed_contacts(self) -> int:
        return self.pipeline_status_counts.get(PipelineStageStatus.COMPLETED.value, 0)

    @property
    def progress_percent(self) -> int:
        """Completed share, floored at 0 and capped at 100.

        An empty Campaign is 0% done — not undefined, and not complete.
        """

        if self.enrolled_contacts <= 0:
            return 0
        return max(0, min(100, round(self.completed_contacts * 100 / self.enrolled_contacts)))

    @property
    def needs_attention(self) -> bool:
        return bool(self.blocked_contacts or self.queue.failed)


@dataclass(frozen=True)
class AgentDetailView:
    """One Agent, optionally scoped to one Campaign."""

    card: AgentCardView
    campaign_id: uuid.UUID | None
    campaign_name: str | None
    effective_control: ControlView
    open_jobs: tuple[JobView, ...]
    recent_activity: tuple[ActivityView, ...]

    @property
    def agent_id(self) -> AgentIdentifier:
        return self.card.agent_id

    @property
    def display_name(self) -> str:
        return self.card.display_name


@dataclass(frozen=True)
class JobView:
    """One durable Agent Job.

    ``public_status`` is Phase 2's own vocabulary; ``stored_status`` is the
    physical enum. Both are shown, because ``leased`` and ``running`` are
    genuinely different facts about a worker and an operator debugging a stuck
    queue needs to tell them apart.
    """

    job_id: uuid.UUID
    agent_id: AgentIdentifier
    agent_name: str
    public_status: str
    stored_status: AgentJobStatus
    task_kind: str
    idempotency_key: str
    attempt_count: int
    max_attempts: int
    priority: int
    campaign_id: uuid.UUID | None
    campaign_name: str | None
    campaign_contact_id: uuid.UUID | None
    contact_id: uuid.UUID | None
    contact_label: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    next_run_at: datetime | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    input_reference: dict[str, Any]
    result: dict[str, Any] | None
    error_class: str | None
    #: Sanitized. Provider credentials and connection strings never reach a page.
    error_message: str | None
    error_detail: dict[str, Any] | None
    outcome_status: str | None
    parent_job_id: uuid.UUID | None
    retryable_failure: bool
    retry_eligible: bool
    retry_refusal: str | None

    @property
    def attempts_exhausted(self) -> bool:
        return self.attempt_count >= self.max_attempts

    @property
    def terminal_failure(self) -> bool:
        return self.stored_status is AgentJobStatus.FAILED and not self.retryable_failure

    @property
    def lease_held(self) -> bool:
        return self.lease_owner is not None

    @property
    def completed_without_result(self) -> bool:
        """Succeeded, but produced nothing an operator can use downstream.

        Phase 2 refuses to complete a job without a committed domain outcome, so
        this is not "the outcome is missing" — it is "the outcome was recorded
        and carried no usable result", which is a real and different answer.
        """

        return self.stored_status is AgentJobStatus.SUCCEEDED and not self.result


@dataclass(frozen=True)
class JobListView:
    """A page of jobs plus the counts behind the filter chips."""

    jobs: tuple[JobView, ...]
    total: int
    queue: QueueCounts
    agent_filter: AgentIdentifier | None = None
    campaign_filter: uuid.UUID | None = None
    status_filter: str | None = None


@dataclass(frozen=True)
class StageView:
    """One Agent's durable state for one Campaign Contact."""

    agent_id: AgentIdentifier
    display_name: str
    position: int
    status: PipelineStageStatus
    attempt_count: int
    latest_job_id: uuid.UUID | None
    reason_code: str | None
    reason_detail: str | None
    retryable: bool
    waiting_on_agent: AgentIdentifier | None
    output_reference: dict[str, Any] | None
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime | None
    control: ControlView
    #: True only when a pipeline event committed the completion. A succeeded job
    #: alone never sets this.
    outcome_committed: bool = False

    @property
    def needs_attention(self) -> bool:
        return self.status in (PipelineStageStatus.FAILED, PipelineStageStatus.BLOCKED)

    @property
    def disabled_globally(self) -> bool:
        return not self.control.accepting_work and not self.control.campaign_scoped

    @property
    def disabled_for_campaign(self) -> bool:
        return not self.control.accepting_work and self.control.campaign_scoped


@dataclass(frozen=True)
class PipelineEventView:
    """One committed pipeline event, in order."""

    event_id: uuid.UUID
    occurred_at: datetime
    event_type: PipelineEventType
    agent_id: AgentIdentifier | None
    job_id: uuid.UUID | None
    from_status: PipelineStageStatus | None
    to_status: PipelineStageStatus | None
    reason_code: str | None
    reason_detail: str | None
    retryable: bool
    actor: str
    detail: dict[str, Any]


@dataclass(frozen=True)
class VerificationAttemptView:
    """One provider-facing attempt, as MVP-01E records it.

    Distinct from an Agent Job attempt: Phase 2 owns execution attempts, this
    says what the *provider* did. ``provider_called`` is the only honest basis
    for reading a paid-call count, so it is shown rather than inferred.
    """

    attempt_number: int
    started_at: datetime | None
    finished_at: datetime | None
    provider: str
    simulated: bool
    provider_called: bool
    reused_evidence: bool
    precise_status: str | None
    verification_result: str | None
    failure_class: str
    retryable: bool
    error_summary: str | None
    evidence_reference: uuid.UUID | None


@dataclass(frozen=True)
class VerificationEvidenceView:
    """One committed exact-address evidence row.

    Provenance is carried explicitly: a simulated result is a real normalized
    outcome and is stored as such, but it is not external verification and this
    view never lets it read as one.
    """

    evidence_id: uuid.UUID
    email: str
    result: str
    provider: str
    simulated: bool
    checked_at: datetime | None
    policy_version: str | None


@dataclass(frozen=True)
class VerificationOutcomeView:
    """The Verification stage's committed decision for one Campaign Contact.

    ``decision`` is read from the outcome Phase 2 committed — the stage's
    ``output_reference``, or the detail on the pipeline event that recorded the
    transition. It is never derived from a job having succeeded: the whole point
    of the MVP-01E decision vocabulary is that a finished job and an accepted
    address are different facts, and only one of them may advance a Contact.
    """

    #: One of the MVP-01E decisions, or None when none was committed.
    decision: str | None
    precise_status: str | None
    verification_result: str | None
    reason_code: str | None
    reason: str | None
    provider: str | None
    simulated: bool
    provider_called: bool
    reused_evidence: bool
    policy_version: str | None
    evidence_reference: uuid.UUID | None
    #: True only when a pipeline event committed the stage outcome.
    outcome_committed: bool
    stage_status: PipelineStageStatus | None
    #: Whether Phase 2 marked the stage retryable.
    retryable: bool
    #: Verification declined before any provider work: the stage is held, no
    #: decision payload was committed, and no attempt reached a provider. Read
    #: from those three observable facts, never by interpreting a reason code —
    #: the Workbench does not classify verification outcomes.
    refused_before_provider: bool = False
    attempts: tuple[VerificationAttemptView, ...] = ()
    evidence: tuple[VerificationEvidenceView, ...] = ()

    @property
    def accepted(self) -> bool:
        """Verified and pipeline-ready.

        Three conditions, all required: the committed decision is ``accept``, a
        pipeline event committed it, and the evidence was not simulated. Any one
        of them missing means the address is not verified, whatever the queue did.
        """

        return self.decision == "accept" and self.outcome_committed and not self.simulated

    @property
    def decided(self) -> bool:
        return self.decision is not None

    @property
    def terminal(self) -> bool:
        return self.decision in {"stop_no_result", "try_next_candidate", "refused"}

    @property
    def refused(self) -> bool:
        """Declined, either by a committed ``refused`` decision or before one."""

        return self.decision == "refused" or self.refused_before_provider

    @property
    def paid_calls(self) -> int:
        return sum(1 for attempt in self.attempts if attempt.provider_called)


@dataclass(frozen=True)
class ContactExecutionView:
    """How one Campaign is working on one permanent Contact.

    Campaign-specific execution only. ``contact_*`` fields identify the
    permanent record; nothing on this view is ever written back to it.
    """

    campaign_contact_id: uuid.UUID
    campaign_id: uuid.UUID
    campaign_name: str
    contact_id: uuid.UUID
    contact_label: str
    contact_email: str | None
    company_label: str | None
    membership_status: CampaignMembershipStatus
    eligibility: CampaignContactEligibility
    blocking_reasons: tuple[dict[str, Any], ...]
    desired_stage: AgentIdentifier
    current_stage: AgentIdentifier | None
    next_stage: AgentIdentifier | None
    latest_completed_stage: AgentIdentifier | None
    pipeline_status: PipelineStageStatus
    next_action: str
    stages: tuple[StageView, ...]
    jobs: tuple[JobView, ...]
    events: tuple[PipelineEventView, ...]
    verification: VerificationOutcomeView | None = None
    enrolled_at: datetime | None = None
    updated_at: datetime | None = None
    review_state: str = ""
    sending_state: str = ""

    @property
    def suppressed(self) -> bool:
        return any(reason.get("code") == "suppression" for reason in self.blocking_reasons)

    @property
    def terminal_block(self) -> str | None:
        return next(
            (
                str(reason.get("detail"))
                for reason in self.blocking_reasons
                if reason.get("terminal") is True
            ),
            None,
        )

    @property
    def completed_stages(self) -> tuple[StageView, ...]:
        return tuple(s for s in self.stages if s.status is PipelineStageStatus.COMPLETED)

    @property
    def failed_stages(self) -> tuple[StageView, ...]:
        return tuple(s for s in self.stages if s.status is PipelineStageStatus.FAILED)

    @property
    def skipped_stages(self) -> tuple[StageView, ...]:
        return tuple(s for s in self.stages if s.status is PipelineStageStatus.SKIPPED)

    @property
    def blocked_stages(self) -> tuple[StageView, ...]:
        return tuple(s for s in self.stages if s.status is PipelineStageStatus.BLOCKED)

    @property
    def total_retries(self) -> int:
        return sum(max(0, stage.attempt_count - 1) for stage in self.stages)

    @property
    def retryable_jobs(self) -> tuple[JobView, ...]:
        return tuple(job for job in self.jobs if job.retry_eligible)


@dataclass(frozen=True)
class CampaignExecutionView:
    """One Campaign's execution, for the Campaign screen."""

    campaign_id: uuid.UUID
    name: str
    status: str
    execution_enabled: bool
    settings_version: int
    disabled_reason: str | None
    enrolled_contacts: int
    stage_counts: dict[str, int]
    pipeline_status_counts: dict[str, int]
    eligibility_counts: dict[str, int]
    blocked_contacts: int
    suppressed_contacts: int
    queue: QueueCounts
    controls: tuple[ControlView, ...]
    contacts: tuple[ContactRowView, ...]
    contact_total: int
    recent_events: tuple[ActivityView, ...]
    sending_control: ControlView

    @property
    def overrides(self) -> tuple[ControlView, ...]:
        return tuple(control for control in self.controls if control.campaign_scoped)

    @property
    def completed_contacts(self) -> int:
        return self.pipeline_status_counts.get(PipelineStageStatus.COMPLETED.value, 0)

    @property
    def progress_percent(self) -> int:
        if self.enrolled_contacts <= 0:
            return 0
        return max(0, min(100, round(self.completed_contacts * 100 / self.enrolled_contacts)))


@dataclass(frozen=True)
class ContactRowView:
    """One Campaign Contact in the Campaign list."""

    campaign_contact_id: uuid.UUID
    contact_id: uuid.UUID
    contact_label: str
    company_label: str | None
    email: str | None
    membership_status: CampaignMembershipStatus
    eligibility: CampaignContactEligibility
    pipeline_status: PipelineStageStatus
    current_stage: AgentIdentifier | None
    next_stage: AgentIdentifier | None
    latest_completed_stage: AgentIdentifier | None
    blocking_detail: str | None
    suppressed: bool
    updated_at: datetime | None

"""Immutable view models for the Admin Workbench.

These DTOs follow the same two rules as ``workbench_agents.views``, on which
they build:

* **A state is displayable only when the domain committed it.** No view here
  derives a status, a count or an outcome the services did not persist.
* **Uncertainty stays explicit.** Where the product cannot know something —
  historical executions without lineage, features that are off, providers that
  are not configured — the view says so instead of guessing.

Vocabulary (docs/AGENT_WORKBENCH.md): an **Agent/Stage** is the business-level
pipeline step; a **worker** is an execution mechanism inside it; an **Agent
Job** is a durable execution request; an **attempt** is one worker claim and
execution try. The views keep those four apart on purpose.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    CampaignContactEligibility,
    CampaignMembershipStatus,
    PipelineStageStatus,
)
from app.services.workbench_agents.views import (
    ActivityView,
    AgentCardView,
    ContactExecutionView,
    ControlView,
    JobView,
    QueueCounts,
)

# --- shared -----------------------------------------------------------------


@dataclass(frozen=True)
class AttentionItem:
    """One prioritised item on the Overview that leads somewhere specific.

    ``href`` always points at the Campaign, Contact, Stage, Job or failure view
    where the operator can act; an attention item that leads nowhere is noise.
    """

    kind: str
    severity: str  # "critical" | "warning" | "info"
    title: str
    detail: str
    href: str
    count: int = 0
    occurred_at: datetime | None = None


@dataclass(frozen=True)
class CampaignHealthRow:
    """One Campaign with the counts an operator triages by."""

    campaign_id: uuid.UUID
    name: str
    status: str
    execution_enabled: bool
    disabled_reason: str | None
    enrolled: int
    completed: int
    in_progress: int
    blocked: int
    failed: int
    awaiting_review: int
    suppressed: int
    open_jobs: int
    failed_jobs: int
    created_at: datetime | None
    latest_activity_at: datetime | None

    @property
    def progress_percent(self) -> int:
        if self.enrolled <= 0:
            return 0
        return max(0, min(100, round(self.completed * 100 / self.enrolled)))

    @property
    def health(self) -> str:
        """A triage label derived only from committed counts, never a verdict."""

        if self.failed or self.failed_jobs:
            return "failing"
        if self.blocked:
            return "blocked"
        if not self.execution_enabled and self.status == "active":
            return "paused"
        if self.in_progress or self.open_jobs:
            return "working"
        if self.enrolled and self.completed >= self.enrolled:
            return "complete"
        return "idle"

    @property
    def needs_attention(self) -> bool:
        return self.health in ("failing", "blocked")


# --- overview ---------------------------------------------------------------


@dataclass(frozen=True)
class FallbackRunRow:
    """One recent Research execution whose durable result records fallback use."""

    job_id: uuid.UUID
    campaign_id: uuid.UUID | None
    campaign_contact_id: uuid.UUID | None
    contact_label: str | None
    status: str
    dossier_basis: str | None
    evidence_accepted: int | None
    claims_rejected: int | None
    finished_at: datetime | None


@dataclass(frozen=True)
class AdminOverviewView:
    """What is running, what is blocked, what failed, what needs review."""

    campaigns_total: int
    campaigns_active: int
    contacts_enrolled: int
    contacts_in_progress: int
    contacts_blocked: int
    contacts_completed: int
    queue: QueueCounts
    stale_leases: int
    review_awaiting: int | None  # None when drafting is unavailable
    recent_drafts_7d: int | None
    sending_control: ControlView
    dry_run: bool
    agents: tuple[AgentCardView, ...]
    campaigns: tuple[CampaignHealthRow, ...]
    attention: tuple[AttentionItem, ...]
    recent_activity: tuple[ActivityView, ...]
    fallback_runs: tuple[FallbackRunRow, ...]
    fallback_available: bool

    @property
    def unhealthy_campaigns(self) -> tuple[CampaignHealthRow, ...]:
        return tuple(row for row in self.campaigns if row.needs_attention)

    @property
    def active_campaigns(self) -> tuple[CampaignHealthRow, ...]:
        return tuple(row for row in self.campaigns if row.status == "active")


# --- campaigns --------------------------------------------------------------


@dataclass(frozen=True)
class CampaignsIndexView:
    rows: tuple[CampaignHealthRow, ...]
    total: int
    status_filter: str | None = None
    health_filter: str | None = None

    @property
    def counts_by_health(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.rows:
            counts[row.health] = counts.get(row.health, 0) + 1
        return counts


@dataclass(frozen=True)
class StageFunnelStep:
    """One Agent/Stage of one Campaign: control state plus contact counts."""

    agent_id: AgentIdentifier
    display_name: str
    position: int
    implemented: bool
    control: ControlView
    at_stage: int  # contacts currently at this stage
    completed_through: int  # contacts whose latest completed stage is >= this one
    failed_here: int
    blocked_here: int


@dataclass(frozen=True)
class CampaignContactRow:
    """One Campaign Contact in the Campaign detail table."""

    campaign_contact_id: uuid.UUID
    contact_id: uuid.UUID
    contact_label: str
    company_label: str | None
    email: str | None
    membership_status: CampaignMembershipStatus
    eligibility: CampaignContactEligibility
    pipeline_status: PipelineStageStatus
    current_stage: AgentIdentifier | None
    current_stage_name: str | None
    blocking_detail: str | None
    suppressed: bool
    review_state: str
    updated_at: datetime | None

    @property
    def needs_attention(self) -> bool:
        return (
            self.pipeline_status in (PipelineStageStatus.FAILED, PipelineStageStatus.BLOCKED)
            or self.eligibility is CampaignContactEligibility.BLOCKED
        )


@dataclass(frozen=True)
class CampaignDetailView:
    campaign_id: uuid.UUID
    name: str
    description: str | None
    status: str
    execution_enabled: bool
    disabled_reason: str | None
    settings_version: int
    allow_provisional_domains: bool
    created_at: datetime | None
    enrolled: int
    pipeline_status_counts: dict[str, int]
    eligibility_counts: dict[str, int]
    suppressed: int
    queue: QueueCounts
    funnel: tuple[StageFunnelStep, ...]
    warnings: tuple[str, ...]
    contacts: tuple[CampaignContactRow, ...]
    contact_total: int
    recent_failures: tuple[FailureItem, ...]
    recent_activity: tuple[ActivityView, ...]
    stage_filter: AgentIdentifier | None = None
    status_filter: str | None = None
    attention_filter: bool = False

    @property
    def completed(self) -> int:
        return self.pipeline_status_counts.get(PipelineStageStatus.COMPLETED.value, 0)

    @property
    def failed(self) -> int:
        return self.pipeline_status_counts.get(PipelineStageStatus.FAILED.value, 0)

    @property
    def blocked(self) -> int:
        return self.eligibility_counts.get(CampaignContactEligibility.BLOCKED.value, 0)

    @property
    def awaiting_review(self) -> int:
        return self.eligibility_counts.get(CampaignContactEligibility.REVIEW_REQUIRED.value, 0)

    @property
    def in_progress(self) -> int:
        return sum(
            self.pipeline_status_counts.get(status.value, 0)
            for status in (
                PipelineStageStatus.WAITING,
                PipelineStageStatus.RUNNING,
                PipelineStageStatus.RETRYING,
            )
        )

    @property
    def progress_percent(self) -> int:
        if self.enrolled <= 0:
            return 0
        return max(0, min(100, round(self.completed * 100 / self.enrolled)))


# --- contact diagnosis ------------------------------------------------------


@dataclass(frozen=True)
class StageDiagnosisView:
    """One Agent/Stage on the Contact timeline, with its execution detail.

    ``execution`` (the underlying :class:`StageView`) carries the committed
    stage state; ``latest_job`` and ``jobs`` carry the durable execution
    requests behind it. ``explanation`` is a human-readable restatement of the
    committed state — it introduces no new facts.
    """

    agent_id: AgentIdentifier
    display_name: str
    position: int
    implemented: bool
    skippable: bool
    status: PipelineStageStatus | None  # None => stage state not yet created
    explanation: str
    attempt_count: int
    reason_code: str | None
    reason_detail: str | None
    retryable: bool
    waiting_on_agent: AgentIdentifier | None
    control: ControlView
    workers: tuple[str, ...]
    latest_job: JobView | None
    jobs: tuple[JobView, ...]
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime | None
    downstream_eligible: bool
    available_action: str | None  # "retry_contact" | "skip_stage" | None
    action_note: str | None = None

    @property
    def needs_attention(self) -> bool:
        return self.status in (PipelineStageStatus.FAILED, PipelineStageStatus.BLOCKED)


@dataclass(frozen=True)
class ContactDiagnosisView:
    """The Campaign -> Contact -> Agent/Stage -> Job -> attempt path, assembled."""

    execution: ContactExecutionView
    stages: tuple[StageDiagnosisView, ...]
    research_lineage_available: bool

    @property
    def campaign_contact_id(self) -> uuid.UUID:
        return self.execution.campaign_contact_id


# --- failures ---------------------------------------------------------------

#: Failure categories the inbox can filter by. Each category is derived from a
#: committed field (job error class, stage reason code, eligibility state or
#: lease timestamps), never from prose.
FAILURE_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("research", "Research failure"),
    ("research_fallback", "Research fallback failure"),
    ("insufficient_evidence", "Insufficient evidence"),
    ("domain", "Domain problem"),
    ("verification", "Verification problem"),
    ("model_output", "Malformed model output"),
    ("provider", "Provider unavailable"),
    ("stale_job", "Stale Agent Job"),
    ("company_intelligence", "Company Intelligence failure"),
    ("retries_exhausted", "Retries exhausted"),
    ("blocked_contact", "Blocked Contact"),
    ("configuration", "Configuration issue"),
    ("other", "Other failure"),
)

FAILURE_CATEGORY_LABELS: dict[str, str] = dict(FAILURE_CATEGORIES)


@dataclass(frozen=True)
class FailureItem:
    """One normalized operational failure with a route to its diagnosis."""

    kind: str  # "job" | "stage" | "contact" | "stale_lease"
    category: str
    severity: str  # "critical" | "warning"
    reason: str
    campaign_id: uuid.UUID | None
    campaign_name: str | None
    campaign_contact_id: uuid.UUID | None
    contact_label: str | None
    company_label: str | None
    agent_id: AgentIdentifier | None
    agent_name: str | None
    job_id: uuid.UUID | None
    attempt_count: int | None
    max_attempts: int | None
    retryable: bool
    latest_at: datetime | None
    diagnosis_href: str
    action: str | None = None  # "retry_job" | "retry_contact" | None
    action_note: str | None = None

    @property
    def category_label(self) -> str:
        return FAILURE_CATEGORY_LABELS.get(self.category, self.category)


@dataclass(frozen=True)
class FailuresInboxView:
    items: tuple[FailureItem, ...]
    total: int
    counts_by_category: dict[str, int]
    category_filter: str | None = None
    campaign_filter: uuid.UUID | None = None
    agent_filter: AgentIdentifier | None = None
    campaign_options: tuple[tuple[uuid.UUID, str], ...] = ()


# --- agent/stages -----------------------------------------------------------


@dataclass(frozen=True)
class StageOpsRow:
    """One Agent/Stage across the whole application."""

    card: AgentCardView
    implemented: bool
    skippable: bool
    dependencies: tuple[AgentIdentifier, ...]
    workers: tuple[str, ...]
    override_count: int
    stage_status_counts: dict[str, int]
    avg_duration_seconds: float | None
    duration_sample: int

    @property
    def agent_id(self) -> AgentIdentifier:
        return self.card.agent_id

    @property
    def display_name(self) -> str:
        return self.card.display_name


@dataclass(frozen=True)
class StageOverrideRow:
    campaign_id: uuid.UUID
    campaign_name: str
    status: AgentControlStatus
    reason: str | None
    updated_at: datetime | None


@dataclass(frozen=True)
class StageDetailView:
    row: StageOpsRow
    effective_global: ControlView
    overrides: tuple[StageOverrideRow, ...]
    open_jobs: tuple[JobView, ...]
    recent_failures: tuple[FailureItem, ...]
    recent_activity: tuple[ActivityView, ...]
    config_summary: dict[str, Any]
    studio_href: str | None


# --- contacts / companies ---------------------------------------------------


@dataclass(frozen=True)
class MembershipRow:
    """One Campaign membership of a permanent Contact."""

    campaign_contact_id: uuid.UUID
    campaign_id: uuid.UUID
    campaign_name: str
    membership_status: CampaignMembershipStatus
    eligibility: CampaignContactEligibility
    pipeline_status: PipelineStageStatus
    current_stage: AgentIdentifier | None
    current_stage_name: str | None
    review_state: str
    enrolled_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True)
class ContactOpsRow:
    contact_id: uuid.UUID
    name: str
    title: str | None
    company_label: str | None
    company_id: uuid.UUID | None
    email: str | None
    suppressed: bool
    membership_count: int
    memberships: tuple[MembershipRow, ...]
    updated_at: datetime | None


@dataclass(frozen=True)
class ContactsIndexView:
    rows: tuple[ContactOpsRow, ...]
    total: int
    page: int
    pages: int
    query: str | None = None
    campaign_filter: uuid.UUID | None = None
    campaign_options: tuple[tuple[uuid.UUID, str], ...] = ()


@dataclass(frozen=True)
class SuppressionRow:
    suppression_type: str
    value: str
    reason: str
    active: bool
    created_at: datetime | None


@dataclass(frozen=True)
class EmailStateRow:
    email: str
    source: str
    verification_result: str | None
    verified_at: datetime | None


@dataclass(frozen=True)
class CaptureRow:
    capture_id: uuid.UUID
    kind: str
    captured_at: datetime | None
    outcome: str | None
    href: str


@dataclass(frozen=True)
class DraftSummaryRow:
    draft_version_id: uuid.UUID
    campaign_id: uuid.UUID
    campaign_name: str | None
    version_number: int
    subject: str
    approval_status: str | None
    created_at: datetime | None


@dataclass(frozen=True)
class AdminContactView:
    contact_id: uuid.UUID
    name: str
    title: str | None
    email: str | None
    company_label: str | None
    company_id: uuid.UUID | None
    company_domain: str | None
    linkedin_url: str | None
    location: str | None
    country: str | None
    created_at: datetime | None
    updated_at: datetime | None
    merged_into_id: uuid.UUID | None
    suppressions: tuple[SuppressionRow, ...]
    memberships: tuple[MembershipRow, ...]
    emails: tuple[EmailStateRow, ...]
    captures: tuple[CaptureRow, ...]
    drafts: tuple[DraftSummaryRow, ...]

    @property
    def suppressed(self) -> bool:
        return any(row.active for row in self.suppressions)


@dataclass(frozen=True)
class CompanyOpsRow:
    company_id: uuid.UUID
    name: str
    domain: str | None
    research_state: str
    contact_count: int
    dossier_count: int
    updated_at: datetime | None


@dataclass(frozen=True)
class CompaniesIndexView:
    rows: tuple[CompanyOpsRow, ...]
    total: int
    page: int
    pages: int
    query: str | None = None


@dataclass(frozen=True)
class IntelligenceJobRow:
    """The latest Company Intelligence job for a Company, and how it arrived."""

    job_id: uuid.UUID
    status: str
    requested_by: str | None
    automatic: bool  # queued by the Research handoff, not an operator or backfill
    error_class: str | None
    last_error: str | None
    attempts: int
    finished_at: datetime | None
    created_at: datetime | None


@dataclass(frozen=True)
class DossierRow:
    dossier_id: uuid.UUID
    version_number: int
    is_current: bool
    interpreter: str
    interpreter_version: str | None
    created_at: datetime | None


@dataclass(frozen=True)
class CompanyResearchJobRow:
    """One Research Agent Job that targeted this Company, with lineage."""

    job_id: uuid.UUID
    status: str
    campaign_id: uuid.UUID | None
    campaign_name: str | None
    campaign_contact_id: uuid.UUID | None
    dossier_basis: str | None
    fallback_attempted: bool | None  # None => lineage unavailable (pre-RES-002)
    fallback_status: str | None
    finished_at: datetime | None


@dataclass(frozen=True)
class AdminCompanyView:
    company_id: uuid.UUID
    name: str
    domain: str | None
    domain_state: str | None
    research_state: str
    last_researched_at: datetime | None
    linkedin_company_url: str | None
    industry: str | None
    country: str | None
    company_size: str | None
    created_at: datetime | None
    linked_contacts: tuple[ContactOpsRow, ...]
    campaign_names: tuple[str, ...]
    dossiers: tuple[DossierRow, ...]
    research_jobs: tuple[CompanyResearchJobRow, ...]
    conflicts: tuple[str, ...]
    intelligence_available: bool
    intelligence_href: str | None
    intelligence_job: IntelligenceJobRow | None = None
    intelligence_version_count: int = 0


# --- review -----------------------------------------------------------------


@dataclass(frozen=True)
class ReviewRow:
    draft_version_id: uuid.UUID
    campaign_id: uuid.UUID
    campaign_name: str
    contact_id: uuid.UUID
    campaign_contact_id: uuid.UUID | None
    contact_label: str
    version_number: int
    subject: str
    approval_status: str | None  # None => awaiting decision
    policy_version_id: uuid.UUID | None
    created_at: datetime | None


@dataclass(frozen=True)
class ReviewIndexView:
    available: bool
    awaiting: int
    approved: int
    discarded: int
    rows: tuple[ReviewRow, ...]
    view: str = "awaiting"

    @property
    def total(self) -> int:
        return self.awaiting + self.approved + self.discarded


# --- providers & usage ------------------------------------------------------


@dataclass(frozen=True)
class ProviderUsageWindow:
    calls: int = 0
    units: float = 0.0
    estimated_cost: float | None = None
    currency: str | None = None
    failures: int = 0
    cache_hits: int = 0


@dataclass(frozen=True)
class ProviderStatusView:
    provider_id: str
    display_name: str
    configured: bool
    configuration_note: str
    feature_flags: tuple[str, ...]
    enabled: bool
    usage_7d: ProviderUsageWindow
    usage_30d: ProviderUsageWindow
    last_used_at: datetime | None
    last_failure_at: datetime | None
    last_failure_reason: str | None


@dataclass(frozen=True)
class ProvidersView:
    providers: tuple[ProviderStatusView, ...]
    ledger_recent: tuple[UsageEntryRow, ...]


@dataclass(frozen=True)
class UsageEntryRow:
    provider: str
    operation: str
    result: str | None
    units: float | None
    origin: str | None
    campaign_id: uuid.UUID | None
    attempted_at: datetime | None


# --- configuration ----------------------------------------------------------


@dataclass(frozen=True)
class PolicyStatusRow:
    """The active version of one immutable policy family, if any."""

    family: str
    label: str
    active_version: str | None
    activated_at: datetime | None
    version_count: int
    manage_href: str | None
    note: str | None = None


@dataclass(frozen=True)
class OverrideSummaryRow:
    campaign_id: uuid.UUID
    campaign_name: str
    agent_id: AgentIdentifier
    agent_name: str
    status: AgentControlStatus
    reason: str | None
    updated_at: datetime | None


@dataclass(frozen=True)
class ConfigurationView:
    app_env: str
    dry_run: bool
    flags: tuple[tuple[str, bool], ...]
    controls: tuple[ControlView, ...]
    overrides: tuple[OverrideSummaryRow, ...]
    policies: tuple[PolicyStatusRow, ...]
    fallback_config: dict[str, Any] | None  # None => fallback feature unavailable
    verification_ttls: dict[str, int]


# --- system -----------------------------------------------------------------


@dataclass(frozen=True)
class AuditRow:
    created_at: datetime
    actor: str
    action: str
    entity_type: str | None
    entity_id: str | None
    previous_state: str | None
    new_state: str | None
    reason: str | None
    dry_run: bool


@dataclass(frozen=True)
class SystemView:
    app_version: str
    app_env: str
    database_ok: bool
    alembic_version: str | None
    job_counts: dict[str, int]
    queue: QueueCounts
    stale_leases: tuple[JobView, ...]
    oldest_open_job_at: datetime | None
    audit_tail: tuple[AuditRow, ...]
    features_enabled: tuple[str, ...]
    job_search_result: JobView | None = None
    job_search_query: str | None = None
    job_search_error: str | None = None
    warnings: tuple[str, ...] = ()


# --- diagnostics hub --------------------------------------------------------


@dataclass(frozen=True)
class DiagnosticLink:
    title: str
    href: str
    description: str
    available: bool
    note: str | None = None


@dataclass(frozen=True)
class DiagnosticsView:
    groups: tuple[tuple[str, tuple[DiagnosticLink, ...]], ...] = field(default_factory=tuple)

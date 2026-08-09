"""The Admin Workbench read model.

One reader assembles every Admin Workbench page from the durable Phase 2 state.
It reuses the authoritative projections that already exist —
:class:`~app.services.workbench_agents.reader.PhaseTwoWorkbenchReader` for
execution state, ``drafts`` for the review queue, ``personalization.policy``
and ``verification.studio`` for active policy versions — and adds only the
aggregations the redesigned surface needs (campaign health, the failures inbox,
provider usage windows, system state).

Everything here is read-only. The reader never writes a row, never repairs a
state and never derives an outcome the services did not commit.
"""

from __future__ import annotations

import importlib.metadata
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.agent import AgentControl, CampaignAgentOverride
from app.models.audit_event import AuditEvent
from app.models.campaign import Campaign, CampaignContact
from app.models.capture_promotion import ContactCapturePromotion
from app.models.company import Company
from app.models.company_dossier import CompanyDossierVersion
from app.models.company_intelligence import (
    CompanyIntelligenceJob,
    CompanyIntelligenceVersion,
)
from app.models.contact import Contact
from app.models.draft import DraftApproval, DraftVersion
from app.models.email_candidate import EmailCandidate
from app.models.email_evidence import ExactEmailVerification
from app.models.email_sequence import (
    EmailSequenceMessage,
    EmailSequenceMessageReview,
    EmailSequenceMessageVersion,
)
from app.models.email_verification_studio import (
    EmailPatternPolicyVersion,
    VerificationWaterfallPolicyVersion,
)
from app.models.enums import (
    AgentIdentifier,
    AgentJobStatus,
    CampaignContactEligibility,
    ImportedEmailSlot,
    ImportedEmailStageOutcome,
    IntelligenceJobStatus,
    PipelineStageStatus,
)
from app.models.imported_email import ImportedContactEmail
from app.models.pipeline import CampaignContactAgentState
from app.models.suppression import Suppression
from app.models.usage_ledger import UsageLedgerEntry
from app.models.verification_job import AgentJob
from app.services import drafts as drafts_service
from app.services.agent_studio.research_report import DurableResearchReportReader
from app.services.agents.jobs import public_status_for
from app.services.agents.registry import AGENT_SPECS, PIPELINE_ORDER
from app.services.companies import detail as company_detail_service
from app.services.company_intelligence.handoff import RESEARCH_HANDOFF_ACTOR
from app.services.personalization import policy as personalization_policy
from app.services.research.fallback import FALLBACK_WORKER_NAME
from app.services.research.workers.website import WORKER_NAME as WEBSITE_WORKER_NAME
from app.services.sequences import read as sequence_read
from app.services.sequences.lineage import bounded_lineage
from app.services.verification import studio as verification_studio
from app.services.verification.provider_registry import PROVIDERS
from app.services.workbench_agents.reader import PhaseTwoWorkbenchReader
from app.services.workbench_agents.views import (
    ActivityView,
    ContactExecutionView,
    ControlView,
    JobView,
    QueueCounts,
)

from .import_lineage import ImportLineageReader
from .views import (
    AdminCompanyView,
    AdminContactView,
    AdminOverviewView,
    AttentionItem,
    AuditRow,
    CampaignContactRow,
    CampaignDetailView,
    CampaignHealthRow,
    CampaignsIndexView,
    CaptureRow,
    CompaniesIndexView,
    CompanyOpsRow,
    CompanyResearchJobRow,
    ConfigurationView,
    ContactDiagnosisView,
    ContactOpsRow,
    ContactsIndexView,
    DossierRow,
    DraftSummaryRow,
    EmailStateRow,
    FailureItem,
    FailuresInboxView,
    FallbackRunRow,
    IntelligenceJobRow,
    MembershipRow,
    OverrideSummaryRow,
    PolicyStatusRow,
    ProviderStatusView,
    ProvidersView,
    ProviderUsageWindow,
    ReviewIndexView,
    ReviewRow,
    SequenceDiagnosisView,
    SequenceMessageDiagnosisRow,
    StageDetailView,
    StageDiagnosisView,
    StageFunnelStep,
    StageOpsRow,
    StageOverrideRow,
    SuppressionRow,
    SystemView,
    UsageEntryRow,
)

PAGE_SIZE = 50
FAILURES_CAP = 200

#: Execution mechanisms known to perform each Agent/Stage. Worker identifiers
#: come from the code that stamps them on durable results (``website`` and
#: ``claude_web`` for Research, the provider registry for Verification); the
#: rest run as the single in-process adapter named after the Agent.
STAGE_WORKERS: dict[AgentIdentifier, tuple[str, ...]] = {
    AgentIdentifier.CAPTURE: ("capture intake (extension / import)",),
    AgentIdentifier.IDENTITY: ("deterministic identity resolver",),
    AgentIdentifier.COMPANY: ("company promotion", "logo.dev domain resolution"),
    AgentIdentifier.RESEARCH: (
        f"deterministic website worker ({WEBSITE_WORKER_NAME})",
        f"Claude web fallback worker ({FALLBACK_WORKER_NAME})",
        "dossier persistence",
    ),
    AgentIdentifier.EMAIL: ("pattern-based candidate generation",),
    AgentIdentifier.VERIFICATION: tuple(
        f"{descriptor.display_name} ({provider_id})"
        for provider_id, descriptor in PROVIDERS.items()
    ),
    AgentIdentifier.INSIGHTS: ("Claude CLI (no tools)",),
    AgentIdentifier.PERSONALIZATION: ("Claude CLI (no tools)",),
    AgentIdentifier.SENDING: (),
}

_OPEN_JOB_STATUSES = (
    AgentJobStatus.PENDING,
    AgentJobStatus.LEASED,
    AgentJobStatus.IN_PROGRESS,
    AgentJobStatus.RETRY_SCHEDULED,
)

_IN_PROGRESS_PIPELINE = (
    PipelineStageStatus.WAITING,
    PipelineStageStatus.RUNNING,
    PipelineStageStatus.RETRYING,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _contact_name(contact: Contact | None) -> str:
    if contact is None:
        return "(contact removed)"
    name = " ".join(part for part in (contact.first_name, contact.last_name) if part)
    return name or contact.email or "(name not captured)"


def _company_label(contact: Contact | None) -> str | None:
    if contact is None:
        return None
    return contact.company_name or contact.company_domain


def _first_blocking_detail(reasons: Any) -> str | None:
    if not isinstance(reasons, (list, tuple)):
        return None
    for reason in reasons:
        if isinstance(reason, dict):
            detail = reason.get("detail") or reason.get("code")
            if detail:
                return str(detail)
    return None


def _is_suppressed(reasons: Any) -> bool:
    if not isinstance(reasons, (list, tuple)):
        return False
    return any(
        isinstance(reason, dict) and reason.get("code") == "suppression" for reason in reasons
    )


def _failure_category(
    agent_id: AgentIdentifier | None,
    *,
    code: str | None,
    fallback_failed: bool = False,
    attempts_exhausted: bool = False,
) -> str:
    """Map a committed failure code onto one inbox category.

    The mapping only reads fields the services wrote (agent identifier, error
    class / reason code, attempt counters); it never parses prose.
    """

    lowered = (code or "").lower()
    if fallback_failed:
        return "research_fallback"
    if "insufficient_evidence" in lowered or "no_sourced_evidence" in lowered:
        return "insufficient_evidence"
    if "credential" in lowered or "configuration" in lowered or "config" in lowered:
        return "configuration"
    if (
        "provider" in lowered
        or "insufficient_credits" in lowered
        or "rate_limit" in lowered
        or "timeout" in lowered
    ):
        return "provider"
    if "domain" in lowered:
        return "domain"
    if "malformed" in lowered or "schema" in lowered or "model_output" in lowered:
        return "model_output"
    if attempts_exhausted:
        return "retries_exhausted"
    if agent_id is AgentIdentifier.RESEARCH:
        return "research"
    if agent_id in (AgentIdentifier.VERIFICATION, AgentIdentifier.EMAIL):
        return "verification"
    return "other"


def _stage_explanation(
    status: PipelineStageStatus | None,
    *,
    display_name: str,
    control: ControlView,
    reason_detail: str | None,
    waiting_on: AgentIdentifier | None,
    implemented: bool,
) -> str:
    """A human-readable restatement of the committed stage state."""

    if not implemented:
        return f"{display_name} is not implemented in this release."
    if status is None:
        if not control.accepting_work:
            scope = "for this Campaign" if control.campaign_scoped else "globally"
            return f"Not started — {display_name} is currently not accepting work {scope}."
        return f"Not started — this Contact has not reached {display_name} yet."
    if status is PipelineStageStatus.WAITING:
        if waiting_on is not None:
            upstream = AGENT_SPECS[waiting_on].display_name
            return f"Waiting for {upstream} to finish first."
        if not control.accepting_work:
            scope = "for this Campaign" if control.campaign_scoped else "globally"
            return f"Waiting — {display_name} is not accepting work {scope}."
        return "Queued and waiting for a worker."
    if status is PipelineStageStatus.RUNNING:
        return "A worker is executing this stage now."
    if status is PipelineStageStatus.RETRYING:
        return "A previous attempt failed; a retry is scheduled."
    if status is PipelineStageStatus.PAUSED:
        return "Paused by an operator."
    if status is PipelineStageStatus.FAILED:
        return reason_detail or "The stage failed; see the Agent Job for the recorded error."
    if status is PipelineStageStatus.COMPLETED:
        return "Completed with a committed outcome."
    if status is PipelineStageStatus.DISABLED:
        scope = "for this Campaign" if control.campaign_scoped else "globally"
        return f"{display_name} is disabled {scope}."
    if status is PipelineStageStatus.SKIPPED:
        return reason_detail or "Skipped by an operator decision."
    if status is PipelineStageStatus.BLOCKED:
        return reason_detail or "Blocked — an eligibility rule prevents this stage from running."
    return status.value  # pragma: no cover - exhaustive above


class AdminWorkbenchReader:
    """Assembles every Admin Workbench page from committed domain state."""

    def __init__(self, session: Session, *, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._phase2 = PhaseTwoWorkbenchReader(session)
        #: Campaign-bound file-import lineage (IMP-001). A separate reader over
        #: the import services' own public helpers, so the Workbench explains an
        #: import without reimplementing one.
        self.imports = ImportLineageReader(session)

    # -- shared helpers ------------------------------------------------------

    def _campaign_names(self) -> dict[uuid.UUID, str]:
        return {
            campaign_id: name
            for campaign_id, name in self._session.execute(select(Campaign.id, Campaign.name)).all()
        }

    def _campaign_options(self) -> tuple[tuple[uuid.UUID, str], ...]:
        return tuple(
            (campaign_id, name)
            for campaign_id, name in self._session.execute(
                select(Campaign.id, Campaign.name).order_by(Campaign.created_at.desc())
            ).all()
        )

    def _stale_lease_filter(self) -> Any:
        return (
            AgentJob.status.in_((AgentJobStatus.LEASED, AgentJobStatus.IN_PROGRESS)),
            AgentJob.lease_expires_at.is_not(None),
            AgentJob.lease_expires_at < _utcnow(),
        )

    def _stale_lease_count(self) -> int:
        return int(
            self._session.scalar(select(func.count(AgentJob.id)).where(*self._stale_lease_filter()))
            or 0
        )

    # -- campaign health -----------------------------------------------------

    def _campaign_health_rows(self) -> tuple[CampaignHealthRow, ...]:
        campaigns = self._session.scalars(
            select(Campaign).order_by(Campaign.created_at.desc())
        ).all()
        if not campaigns:
            return ()

        def _group(rows: Sequence[Any]) -> dict[uuid.UUID, dict[str, int]]:
            grouped: dict[uuid.UUID, dict[str, int]] = {}
            for campaign_id, key, count in rows:
                grouped.setdefault(campaign_id, {})[
                    key.value if hasattr(key, "value") else str(key)
                ] = int(count)
            return grouped

        pipeline_counts = _group(
            self._session.execute(
                select(
                    CampaignContact.campaign_id,
                    CampaignContact.pipeline_status,
                    func.count(CampaignContact.id),
                ).group_by(CampaignContact.campaign_id, CampaignContact.pipeline_status)
            ).all()
        )
        eligibility_counts = _group(
            self._session.execute(
                select(
                    CampaignContact.campaign_id,
                    CampaignContact.eligibility_status,
                    func.count(CampaignContact.id),
                ).group_by(CampaignContact.campaign_id, CampaignContact.eligibility_status)
            ).all()
        )
        job_counts = _group(
            self._session.execute(
                select(
                    AgentJob.campaign_id,
                    AgentJob.status,
                    func.count(AgentJob.id),
                )
                .where(AgentJob.campaign_id.is_not(None))
                .group_by(AgentJob.campaign_id, AgentJob.status)
            ).all()
        )
        latest_activity: dict[uuid.UUID, datetime] = {
            campaign_id: moment
            for campaign_id, moment in self._session.execute(
                select(CampaignContact.campaign_id, func.max(CampaignContact.updated_at)).group_by(
                    CampaignContact.campaign_id
                )
            ).all()
            if moment is not None
        }

        rows: list[CampaignHealthRow] = []
        for campaign in campaigns:
            pipeline = pipeline_counts.get(campaign.id, {})
            eligibility = eligibility_counts.get(campaign.id, {})
            jobs = job_counts.get(campaign.id, {})
            enrolled = sum(pipeline.values())
            rows.append(
                CampaignHealthRow(
                    campaign_id=campaign.id,
                    name=campaign.name,
                    status=campaign.status.value,
                    execution_enabled=campaign.execution_enabled,
                    disabled_reason=campaign.disabled_reason,
                    enrolled=enrolled,
                    completed=pipeline.get(PipelineStageStatus.COMPLETED.value, 0),
                    in_progress=sum(
                        pipeline.get(status.value, 0) for status in _IN_PROGRESS_PIPELINE
                    ),
                    blocked=eligibility.get(CampaignContactEligibility.BLOCKED.value, 0),
                    failed=pipeline.get(PipelineStageStatus.FAILED.value, 0),
                    awaiting_review=eligibility.get(
                        CampaignContactEligibility.REVIEW_REQUIRED.value, 0
                    ),
                    suppressed=0,
                    open_jobs=sum(jobs.get(status.value, 0) for status in _OPEN_JOB_STATUSES),
                    failed_jobs=jobs.get(AgentJobStatus.FAILED.value, 0),
                    created_at=campaign.created_at,
                    latest_activity_at=latest_activity.get(campaign.id),
                )
            )
        return tuple(rows)

    # -- overview ------------------------------------------------------------

    def overview(self) -> AdminOverviewView:
        phase2 = self._phase2.overview()
        campaigns = self._campaign_health_rows()

        contacts_enrolled = int(self._session.scalar(select(func.count(CampaignContact.id))) or 0)
        pipeline_counts: dict[str, int] = {
            status.value: int(count)
            for status, count in self._session.execute(
                select(CampaignContact.pipeline_status, func.count(CampaignContact.id)).group_by(
                    CampaignContact.pipeline_status
                )
            ).all()
        }
        contacts_blocked = int(
            self._session.scalar(
                select(func.count(CampaignContact.id)).where(
                    CampaignContact.eligibility_status == CampaignContactEligibility.BLOCKED
                )
            )
            or 0
        )

        review_awaiting: int | None = None
        recent_drafts: int | None = None
        if self._settings.features.drafting or self._settings.features.email_generation:
            review_awaiting = drafts_service.queue_counts(self._session).awaiting
        drafts_7d = self._session.scalar(
            select(func.count(DraftVersion.id)).where(
                DraftVersion.created_at >= _utcnow() - timedelta(days=7)
            )
        )
        recent_drafts = int(drafts_7d or 0)

        stale = self._stale_lease_count()
        fallback_runs = self._fallback_runs(limit=8)

        attention: list[AttentionItem] = []
        failed_jobs = phase2.queue.failed
        if failed_jobs:
            attention.append(
                AttentionItem(
                    kind="failed_jobs",
                    severity="critical",
                    title=f"{failed_jobs} failed Agent Job{'s' if failed_jobs != 1 else ''}",
                    detail="Failures across all Campaigns, normalized in the Failures inbox.",
                    href="/admin/failures",
                    count=failed_jobs,
                )
            )
        if contacts_blocked:
            attention.append(
                AttentionItem(
                    kind="blocked_contacts",
                    severity="warning",
                    title=(
                        f"{contacts_blocked} blocked Contact{'s' if contacts_blocked != 1 else ''}"
                    ),
                    detail="Eligibility rules are preventing these Contacts from progressing.",
                    href="/admin/failures?category=blocked_contact",
                    count=contacts_blocked,
                )
            )
        if stale:
            attention.append(
                AttentionItem(
                    kind="stale_leases",
                    severity="warning",
                    title=f"{stale} stale Agent Job lease{'s' if stale != 1 else ''}",
                    detail="A worker claimed these Jobs but its lease has expired.",
                    href="/admin/system",
                    count=stale,
                )
            )
        if review_awaiting:
            attention.append(
                AttentionItem(
                    kind="review",
                    severity="info",
                    title=(
                        f"{review_awaiting} draft{'s' if review_awaiting != 1 else ''} "
                        "awaiting review"
                    ),
                    detail="Approval decisions happen in the Customer review queue.",
                    href="/admin/review",
                    count=review_awaiting,
                )
            )
        for row in campaigns:
            if row.needs_attention:
                attention.append(
                    AttentionItem(
                        kind="campaign",
                        severity="warning",
                        title=f"Campaign “{row.name}” needs attention",
                        detail=(
                            f"{row.failed} failed, {row.blocked} blocked, "
                            f"{row.failed_jobs} failed Job(s)."
                        ),
                        href=f"/admin/campaigns/{row.campaign_id}",
                        count=row.failed + row.blocked,
                    )
                )

        return AdminOverviewView(
            campaigns_total=len(campaigns),
            campaigns_active=sum(1 for row in campaigns if row.status == "active"),
            contacts_enrolled=contacts_enrolled,
            contacts_in_progress=sum(
                pipeline_counts.get(status.value, 0) for status in _IN_PROGRESS_PIPELINE
            ),
            contacts_blocked=contacts_blocked,
            contacts_completed=pipeline_counts.get(PipelineStageStatus.COMPLETED.value, 0),
            queue=phase2.queue,
            stale_leases=stale,
            review_awaiting=review_awaiting,
            recent_drafts_7d=recent_drafts,
            sending_control=phase2.sending_control,
            dry_run=self._settings.dry_run,
            agents=phase2.agents,
            campaigns=campaigns,
            attention=tuple(attention),
            recent_activity=phase2.recent_activity,
            fallback_runs=fallback_runs,
            fallback_available=self._settings.features.research_claude_fallback,
        )

    def _fallback_runs(self, *, limit: int) -> tuple[FallbackRunRow, ...]:
        """Recent Research jobs whose durable result records a fallback attempt."""

        jobs = self._session.scalars(
            select(AgentJob)
            .where(
                AgentJob.agent_id == AgentIdentifier.RESEARCH,
                AgentJob.status.in_((AgentJobStatus.SUCCEEDED, AgentJobStatus.FAILED)),
            )
            .order_by(AgentJob.updated_at.desc())
            .limit(50)
        ).all()
        contact_labels = self._contact_labels(
            [job.contact_id for job in jobs if job.contact_id is not None]
        )
        rows: list[FallbackRunRow] = []
        for job in jobs:
            result = job.result if isinstance(job.result, dict) else {}
            fallback = result.get("fallback")
            if not isinstance(fallback, dict) or not fallback.get("attempted"):
                continue
            rows.append(
                FallbackRunRow(
                    job_id=job.id,
                    campaign_id=job.campaign_id,
                    campaign_contact_id=job.campaign_contact_id,
                    contact_label=(contact_labels.get(job.contact_id) if job.contact_id else None),
                    status=str(fallback.get("status") or "unknown"),
                    dossier_basis=(
                        str(result["dossier_basis"]) if result.get("dossier_basis") else None
                    ),
                    evidence_accepted=(
                        int(fallback["evidence_accepted"])
                        if isinstance(fallback.get("evidence_accepted"), int)
                        else None
                    ),
                    claims_rejected=(
                        int(fallback["claims_rejected"])
                        if isinstance(fallback.get("claims_rejected"), int)
                        else None
                    ),
                    finished_at=job.finished_at,
                )
            )
            if len(rows) >= limit:
                break
        return tuple(rows)

    def _contact_labels(self, contact_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not contact_ids:
            return {}
        contacts = self._session.scalars(
            select(Contact).where(Contact.id.in_(set(contact_ids)))
        ).all()
        return {contact.id: _contact_name(contact) for contact in contacts}

    # -- campaigns -----------------------------------------------------------

    def campaigns_index(
        self, *, status: str | None = None, health: str | None = None
    ) -> CampaignsIndexView:
        rows = self._campaign_health_rows()
        total = len(rows)
        if status:
            rows = tuple(row for row in rows if row.status == status)
        if health:
            rows = tuple(row for row in rows if row.health == health)
        return CampaignsIndexView(
            rows=rows, total=total, status_filter=status, health_filter=health
        )

    def campaign_detail(
        self,
        campaign_id: uuid.UUID,
        *,
        stage: AgentIdentifier | None = None,
        status: str | None = None,
        attention: bool = False,
        limit: int = PAGE_SIZE,
        offset: int = 0,
    ) -> CampaignDetailView | None:
        campaign = self._session.get(Campaign, campaign_id)
        if campaign is None:
            return None

        execution = self._phase2.campaign_execution(campaign_id, limit=0, offset=0)
        if execution is None:  # pragma: no cover - campaign fetched above
            return None

        # Funnel: contacts at each stage, committed stage failures per agent.
        at_stage: dict[AgentIdentifier, int] = {}
        for value, count in self._session.execute(
            select(CampaignContact.current_stage, func.count(CampaignContact.id))
            .where(CampaignContact.campaign_id == campaign_id)
            .group_by(CampaignContact.current_stage)
        ).all():
            if value is not None:
                at_stage[value] = int(count)

        completed_positions: dict[int, int] = {}
        for value, count in self._session.execute(
            select(CampaignContact.latest_completed_stage, func.count(CampaignContact.id))
            .where(CampaignContact.campaign_id == campaign_id)
            .group_by(CampaignContact.latest_completed_stage)
        ).all():
            if value is not None:
                completed_positions[AGENT_SPECS[value].position] = int(count)

        stage_state_counts: dict[AgentIdentifier, dict[str, int]] = {}
        for agent_value, status_value, count in self._session.execute(
            select(
                CampaignContactAgentState.agent_id,
                CampaignContactAgentState.status,
                func.count(CampaignContactAgentState.campaign_contact_id),
            )
            .join(
                CampaignContact,
                CampaignContact.id == CampaignContactAgentState.campaign_contact_id,
            )
            .where(CampaignContact.campaign_id == campaign_id)
            .group_by(CampaignContactAgentState.agent_id, CampaignContactAgentState.status)
        ).all():
            stage_state_counts.setdefault(agent_value, {})[status_value.value] = int(count)

        # How many of this Campaign's contacts reached the far side of
        # Verification without a provider being asked. One statement, and only
        # the Verification row uses it — see StageFunnelStep.bypassed_through.
        verification_position = AGENT_SPECS[AgentIdentifier.VERIFICATION].position
        bypassed_through_verification = int(
            self._session.scalar(
                select(func.count(func.distinct(CampaignContact.id)))
                .join(
                    ImportedContactEmail,
                    ImportedContactEmail.contact_id == CampaignContact.contact_id,
                )
                .where(
                    CampaignContact.campaign_id == campaign_id,
                    ImportedContactEmail.campaign_id == campaign_id,
                    ImportedContactEmail.slot == ImportedEmailSlot.PRIMARY,
                    ImportedContactEmail.email_stage_outcome
                    == ImportedEmailStageOutcome.IMPORTED_EMAIL_ACCEPTED,
                    CampaignContact.latest_completed_stage.in_(
                        [
                            agent
                            for agent, spec in AGENT_SPECS.items()
                            if spec.position >= verification_position
                        ]
                    ),
                )
            )
            or 0
        )

        controls = {control.agent_id: control for control in execution.controls}
        funnel: list[StageFunnelStep] = []
        for agent_id in PIPELINE_ORDER:
            spec = AGENT_SPECS[agent_id]
            states = stage_state_counts.get(agent_id, {})
            funnel.append(
                StageFunnelStep(
                    agent_id=agent_id,
                    display_name=spec.display_name,
                    position=spec.position,
                    implemented=spec.implemented,
                    control=controls[agent_id],
                    at_stage=at_stage.get(agent_id, 0),
                    completed_through=sum(
                        count
                        for position, count in completed_positions.items()
                        if position >= spec.position
                    ),
                    failed_here=states.get(PipelineStageStatus.FAILED.value, 0),
                    blocked_here=states.get(PipelineStageStatus.BLOCKED.value, 0),
                    bypassed_through=(
                        bypassed_through_verification
                        if agent_id is AgentIdentifier.VERIFICATION
                        else 0
                    ),
                )
            )

        warnings: list[str] = []
        if not campaign.execution_enabled:
            reason = f" — {campaign.disabled_reason}" if campaign.disabled_reason else ""
            warnings.append(f"Campaign execution is paused{reason}.")
        for control in execution.controls:
            spec = AGENT_SPECS[control.agent_id]
            if spec.implemented and not control.accepting_work:
                scope = "for this Campaign" if control.campaign_scoped else "globally"
                warnings.append(
                    f"{spec.display_name} is not accepting work {scope} (source: {control.source})."
                )
        if campaign.allow_provisional_domains:
            warnings.append(
                "Provisional domains are allowed for Research in this Campaign; "
                "they never authorize qualification, drafting or sending."
            )

        # Contact table with filters.
        membership_statement = (
            select(CampaignContact, Contact)
            .outerjoin(Contact, Contact.id == CampaignContact.contact_id)
            .where(CampaignContact.campaign_id == campaign_id)
        )
        count_statement = select(func.count(CampaignContact.id)).where(
            CampaignContact.campaign_id == campaign_id
        )
        if stage is not None:
            membership_statement = membership_statement.where(
                CampaignContact.current_stage == stage
            )
            count_statement = count_statement.where(CampaignContact.current_stage == stage)
        if status:
            try:
                status_value = PipelineStageStatus(status)
            except ValueError:
                status_value = None
            if status_value is not None:
                membership_statement = membership_statement.where(
                    CampaignContact.pipeline_status == status_value
                )
                count_statement = count_statement.where(
                    CampaignContact.pipeline_status == status_value
                )
        if attention:
            attention_filter = or_(
                CampaignContact.pipeline_status.in_(
                    (PipelineStageStatus.FAILED, PipelineStageStatus.BLOCKED)
                ),
                CampaignContact.eligibility_status == CampaignContactEligibility.BLOCKED,
            )
            membership_statement = membership_statement.where(attention_filter)
            count_statement = count_statement.where(attention_filter)

        contact_total = int(self._session.scalar(count_statement) or 0)
        membership_rows = self._session.execute(
            membership_statement.order_by(CampaignContact.enrolled_at.desc())
            .limit(max(0, limit))
            .offset(max(0, offset))
        ).all()
        contact_rows = tuple(
            CampaignContactRow(
                campaign_contact_id=membership.id,
                contact_id=membership.contact_id,
                contact_label=_contact_name(contact),
                company_label=_company_label(contact),
                email=contact.email if contact else None,
                membership_status=membership.membership_status,
                eligibility=membership.eligibility_status,
                pipeline_status=membership.pipeline_status,
                current_stage=membership.current_stage,
                current_stage_name=(
                    AGENT_SPECS[membership.current_stage].display_name
                    if membership.current_stage
                    else None
                ),
                blocking_detail=_first_blocking_detail(membership.blocking_reasons),
                suppressed=_is_suppressed(membership.blocking_reasons),
                review_state=membership.review_state or "",
                updated_at=membership.updated_at,
            )
            for membership, contact in membership_rows
        )

        recent_failures = self._failure_items(campaign_filter=campaign_id)[:10]

        return CampaignDetailView(
            campaign_id=campaign.id,
            name=campaign.name,
            description=campaign.description,
            status=campaign.status.value,
            execution_enabled=campaign.execution_enabled,
            disabled_reason=campaign.disabled_reason,
            settings_version=campaign.settings_version,
            allow_provisional_domains=campaign.allow_provisional_domains,
            created_at=campaign.created_at,
            enrolled=execution.enrolled_contacts,
            pipeline_status_counts=execution.pipeline_status_counts,
            eligibility_counts=execution.eligibility_counts,
            suppressed=execution.suppressed_contacts,
            queue=execution.queue,
            funnel=tuple(funnel),
            warnings=tuple(warnings),
            contacts=contact_rows,
            contact_total=contact_total,
            recent_failures=tuple(recent_failures),
            recent_activity=execution.recent_events,
            stage_filter=stage,
            status_filter=status,
            attention_filter=attention,
        )

    # -- contact diagnosis ---------------------------------------------------

    def contact_diagnosis(
        self, campaign_id: uuid.UUID, campaign_contact_id: uuid.UUID
    ) -> ContactDiagnosisView | None:
        execution = self._phase2.contact_execution(campaign_id, campaign_contact_id)
        if execution is None:
            return None
        return ContactDiagnosisView(
            execution=execution,
            stages=self._diagnosis_stages(execution),
            research_lineage_available=any(
                job.agent_id is AgentIdentifier.RESEARCH for job in execution.jobs
            ),
            sequences=self._sequence_diagnoses(campaign_contact_id),
            sequences_enabled=self._settings.features.email_sequences,
        )

    def _sequence_diagnoses(
        self, campaign_contact_id: uuid.UUID
    ) -> tuple[SequenceDiagnosisView, ...]:
        """Every recent sequence version for this membership, newest first.

        Four statements in total, regardless of how many times an operator has
        regenerated. It used to be three *per version* inside a loop, so a
        contact regenerated ten times cost thirty queries and every further
        regeneration added three more. The fix is the pattern this codebase
        already uses in ``sequences.read._tallies``: fetch everything for the
        bounded history set with one ``IN`` each, then group in Python.

        Bounded twice over: ``history`` caps how many versions are read, and each
        version's validation findings and lineage are capped where they are
        rendered.
        """

        history = sequence_read.history(self._session, campaign_contact_id=campaign_contact_id)
        if not history:
            return ()

        sequence_ids = [sequence.id for sequence in history]
        sequence_keys = list({sequence.sequence_key for sequence in history})

        # 1 — the logical messages for every sequence key on the page.
        messages_by_key: dict[uuid.UUID, list[EmailSequenceMessage]] = {}
        for message in self._session.scalars(
            select(EmailSequenceMessage)
            .where(EmailSequenceMessage.sequence_key.in_(sequence_keys))
            .order_by(EmailSequenceMessage.position)
        ).all():
            messages_by_key.setdefault(message.sequence_key, []).append(message)

        # 2 — every version belonging to any sequence on the page, superseded
        # ones included: a superseded sequence version's own messages are
        # superseded too, and the diagnosis is about what each version held.
        versions_by_sequence: dict[uuid.UUID, dict[int, EmailSequenceMessageVersion]] = {}
        version_ids: list[uuid.UUID] = []
        for version in self._session.scalars(
            select(EmailSequenceMessageVersion)
            .where(EmailSequenceMessageVersion.sequence_id.in_(sequence_ids))
            .order_by(EmailSequenceMessageVersion.message_version)
        ).all():
            versions_by_sequence.setdefault(version.sequence_id, {})[version.position] = version
            version_ids.append(version.id)

        # 3 — the decisions recorded against those exact versions.
        reviews_by_version: dict[uuid.UUID, EmailSequenceMessageReview] = {}
        if version_ids:
            reviews_by_version = {
                review.message_version_id: review
                for review in self._session.scalars(
                    select(EmailSequenceMessageReview).where(
                        EmailSequenceMessageReview.message_version_id.in_(version_ids)
                    )
                ).all()
            }

        out: list[SequenceDiagnosisView] = []
        for sequence in history:
            messages = messages_by_key.get(sequence.sequence_key, [])
            versions = versions_by_sequence.get(sequence.id, {})
            findings_raw = sequence.validation_findings or {}
            findings = findings_raw.get("findings") if isinstance(findings_raw, dict) else None
            rows: list[SequenceMessageDiagnosisRow] = []
            for message in messages:
                held = versions.get(message.position)
                if held is None:
                    # This sequence version wrote nothing at this position. Said
                    # plainly rather than filled in from a neighbouring version.
                    continue
                review = reviews_by_version.get(held.id)
                rows.append(
                    SequenceMessageDiagnosisRow(
                        position=message.position,
                        message_id=message.id,
                        version_id=held.id,
                        message_version=held.message_version,
                        purpose=message.purpose.value,
                        message_type=message.message_type.value,
                        origin=held.origin.value,
                        generation_status=held.generation_status.value,
                        validation_status=held.validation_status.value,
                        review_state=(
                            review.decision.value if review is not None else "waiting for you"
                        ),
                        predecessor_message_id=message.predecessor_message_id,
                        planned_day=held.recommended_elapsed_day,
                        planned_delay_days=held.recommended_delay_days,
                        delivery_state=message.delivery_state.value,
                        warnings=tuple(held.warnings or []),
                        cited_evidence_ids=tuple(held.evidence_insight_ids or []),
                        intelligence_accepted=held.intelligence_accepted_count,
                        intelligence_excluded=held.intelligence_excluded_count,
                        decided_by=review.decided_by if review is not None else None,
                        decided_at=review.decided_at if review is not None else None,
                    )
                )
            out.append(
                SequenceDiagnosisView(
                    sequence_id=sequence.id,
                    sequence_key=sequence.sequence_key,
                    sequence_version=sequence.sequence_version,
                    agent_job_id=sequence.agent_job_id,
                    input_digest=sequence.input_digest,
                    producer=sequence.producer,
                    producer_version=sequence.producer_version,
                    sequence_producer_version=sequence.sequence_producer_version,
                    validation_policy_version=sequence.validation_policy_version,
                    policy_version_number=sequence.personalization_policy_version_number,
                    strategy_id=sequence.personalization_strategy_id,
                    generation_status=sequence.generation_status.value,
                    validation_status=sequence.validation_status.value,
                    review_state=sequence.review_state.value,
                    cadence_source=sequence.cadence_source,
                    planned_span_days=sequence.planned_span_days,
                    current_actionable_position=sequence.current_actionable_position,
                    stop_state=sequence.stop_state.value,
                    stop_reason=sequence.stop_reason.value if sequence.stop_reason else None,
                    created_at=sequence.created_at,
                    created_by=sequence.created_by,
                    superseded_at=sequence.superseded_at,
                    messages=tuple(rows),
                    validation_findings=tuple(
                        item for item in (findings or []) if isinstance(item, dict)
                    ),
                    research_lineage=bounded_lineage(sequence.research_lineage),
                    insights_lineage=bounded_lineage(sequence.insights_lineage),
                    intelligence_lineage=bounded_lineage(sequence.intelligence_lineage),
                    context_decision=bounded_lineage(sequence.personalization_decision),
                )
            )
        return tuple(out)

    def research_lineage(self, campaign_contact_id: uuid.UUID) -> Any:
        """The durable Research report (deterministic + fallback lineage)."""

        return DurableResearchReportReader(self._session).read(campaign_contact_id)

    def _diagnosis_stages(self, execution: ContactExecutionView) -> tuple[StageDiagnosisView, ...]:
        jobs_by_agent: dict[AgentIdentifier, list[JobView]] = {}
        for job in execution.jobs:
            jobs_by_agent.setdefault(job.agent_id, []).append(job)

        stage_views = {stage.agent_id: stage for stage in execution.stages}
        out: list[StageDiagnosisView] = []
        for agent_id in PIPELINE_ORDER:
            spec = AGENT_SPECS[agent_id]
            stage = stage_views.get(agent_id)
            # ``contact_execution`` always materialises a StageView per pipeline
            # position, so ``stage`` is only None for registry changes mid-flight.
            if stage is None:  # pragma: no cover - defensive
                continue
            agent_jobs = tuple(jobs_by_agent.get(agent_id, ()))
            latest_job = agent_jobs[0] if agent_jobs else None
            status = stage.status

            available_action: str | None = None
            action_note: str | None = None
            if status is PipelineStageStatus.FAILED and stage.retryable:
                available_action = "retry_contact"
                action_note = "Re-runs the failed stage through the authoritative retry path."
            elif status is PipelineStageStatus.FAILED and not stage.retryable:
                action_note = (
                    "The recorded failure is not retryable; it needs an operator decision."
                )
                if spec.skippable:
                    available_action = "skip_stage"
                    action_note += " The stage is skippable."
            elif status is PipelineStageStatus.BLOCKED:
                action_note = (
                    "Resolve the blocking condition (for example a suppression or a missing "
                    "domain); the Workbench cannot release eligibility blocks."
                )

            out.append(
                StageDiagnosisView(
                    agent_id=agent_id,
                    display_name=spec.display_name,
                    position=spec.position,
                    implemented=spec.implemented,
                    skippable=spec.skippable,
                    status=status,
                    explanation=_stage_explanation(
                        status,
                        display_name=spec.display_name,
                        control=stage.control,
                        reason_detail=stage.reason_detail,
                        waiting_on=stage.waiting_on_agent,
                        implemented=spec.implemented,
                    ),
                    attempt_count=stage.attempt_count,
                    reason_code=stage.reason_code,
                    reason_detail=stage.reason_detail,
                    retryable=stage.retryable,
                    waiting_on_agent=stage.waiting_on_agent,
                    control=stage.control,
                    workers=STAGE_WORKERS.get(agent_id, ()),
                    latest_job=latest_job,
                    jobs=agent_jobs,
                    started_at=stage.started_at,
                    completed_at=stage.completed_at,
                    updated_at=stage.updated_at,
                    downstream_eligible=status
                    in (PipelineStageStatus.COMPLETED, PipelineStageStatus.SKIPPED),
                    available_action=available_action,
                    action_note=action_note,
                )
            )
        return tuple(out)

    # -- failures ------------------------------------------------------------

    def failures(
        self,
        *,
        category: str | None = None,
        campaign_id: uuid.UUID | None = None,
        agent: AgentIdentifier | None = None,
    ) -> FailuresInboxView:
        items = self._failure_items(campaign_filter=campaign_id)
        counts: dict[str, int] = {}
        for item in items:
            counts[item.category] = counts.get(item.category, 0) + 1
        if category:
            items = [item for item in items if item.category == category]
        if agent is not None:
            items = [item for item in items if item.agent_id is agent]
        total = len(items)
        return FailuresInboxView(
            items=tuple(items[:FAILURES_CAP]),
            total=total,
            counts_by_category=counts,
            category_filter=category,
            campaign_filter=campaign_id,
            agent_filter=agent,
            campaign_options=self._campaign_options(),
        )

    def _failure_items(self, *, campaign_filter: uuid.UUID | None = None) -> list[FailureItem]:
        campaign_names = self._campaign_names()
        items: list[FailureItem] = []
        now = _utcnow()

        # 1. Committed stage failures and blocks (the primary diagnosis rows).
        state_statement = (
            select(CampaignContactAgentState, CampaignContact, Contact)
            .join(
                CampaignContact,
                CampaignContact.id == CampaignContactAgentState.campaign_contact_id,
            )
            .outerjoin(Contact, Contact.id == CampaignContact.contact_id)
            .where(
                CampaignContactAgentState.status.in_(
                    (PipelineStageStatus.FAILED, PipelineStageStatus.BLOCKED)
                )
            )
            .order_by(CampaignContactAgentState.updated_at.desc())
            .limit(FAILURES_CAP * 2)
        )
        if campaign_filter is not None:
            state_statement = state_statement.where(CampaignContact.campaign_id == campaign_filter)
        for state, membership, contact in self._session.execute(state_statement).all():
            spec = AGENT_SPECS[state.agent_id]
            failed = state.status is PipelineStageStatus.FAILED
            fallback_failed = bool(
                state.agent_id is AgentIdentifier.RESEARCH
                and (state.reason_code or "").startswith("fallback")
            )
            category = _failure_category(
                state.agent_id,
                code=state.reason_code,
                fallback_failed=fallback_failed,
                attempts_exhausted=state.attempt_count >= spec.max_attempts and failed,
            )
            if not failed:
                category = "blocked_contact"
            items.append(
                FailureItem(
                    kind="stage" if failed else "contact",
                    category=category,
                    severity="critical" if failed and not state.retryable else "warning",
                    reason=state.reason_detail
                    or state.reason_code
                    or ("Stage failed." if failed else "Stage blocked."),
                    campaign_id=membership.campaign_id,
                    campaign_name=campaign_names.get(membership.campaign_id),
                    campaign_contact_id=membership.id,
                    contact_label=_contact_name(contact),
                    company_label=_company_label(contact),
                    agent_id=state.agent_id,
                    agent_name=spec.display_name,
                    job_id=state.latest_job_id,
                    attempt_count=state.attempt_count,
                    max_attempts=spec.max_attempts,
                    retryable=state.retryable,
                    latest_at=state.updated_at,
                    diagnosis_href=(
                        f"/admin/campaigns/{membership.campaign_id}/contacts/{membership.id}"
                    ),
                    action="retry_contact" if failed and state.retryable else None,
                    action_note=None
                    if failed and state.retryable
                    else (
                        "Needs an operator decision — the failure is not retryable."
                        if failed
                        else "Resolve the blocking condition; blocks cannot be released here."
                    ),
                )
            )

        # 2. Blocked memberships without a failed/blocked stage row.
        seen_memberships = {item.campaign_contact_id for item in items if item.campaign_contact_id}
        blocked_statement = (
            select(CampaignContact, Contact)
            .outerjoin(Contact, Contact.id == CampaignContact.contact_id)
            .where(CampaignContact.eligibility_status == CampaignContactEligibility.BLOCKED)
            .order_by(CampaignContact.updated_at.desc())
            .limit(FAILURES_CAP)
        )
        if campaign_filter is not None:
            blocked_statement = blocked_statement.where(
                CampaignContact.campaign_id == campaign_filter
            )
        for membership, contact in self._session.execute(blocked_statement).all():
            if membership.id in seen_memberships:
                continue
            detail = _first_blocking_detail(membership.blocking_reasons)
            items.append(
                FailureItem(
                    kind="contact",
                    category="blocked_contact",
                    severity="warning",
                    reason=detail or "Eligibility is blocked.",
                    campaign_id=membership.campaign_id,
                    campaign_name=campaign_names.get(membership.campaign_id),
                    campaign_contact_id=membership.id,
                    contact_label=_contact_name(contact),
                    company_label=_company_label(contact),
                    agent_id=None,
                    agent_name=None,
                    job_id=None,
                    attempt_count=None,
                    max_attempts=None,
                    retryable=False,
                    latest_at=membership.updated_at,
                    diagnosis_href=(
                        f"/admin/campaigns/{membership.campaign_id}/contacts/{membership.id}"
                    ),
                    action=None,
                    action_note=(
                        "Suppressions and domain blocks are authoritative; "
                        "they cannot be released from the Workbench."
                    ),
                )
            )

        # 3. Failed Agent Jobs not already represented by a failed stage row.
        #    (A stage row is the primary diagnosis surface; a failed job whose
        #    stage never transitioned — or that has no Campaign Contact at all —
        #    would otherwise be invisible.)
        represented_jobs = {item.job_id for item in items if item.job_id is not None}
        failed_jobs_statement = (
            select(AgentJob)
            .where(AgentJob.status == AgentJobStatus.FAILED)
            .order_by(AgentJob.updated_at.desc())
            .limit(FAILURES_CAP)
        )
        if campaign_filter is not None:
            failed_jobs_statement = failed_jobs_statement.where(
                AgentJob.campaign_id == campaign_filter
            )
        failed_jobs = [
            job
            for job in self._session.scalars(failed_jobs_statement).all()
            if job.id not in represented_jobs
        ]
        job_contact_labels = self._contact_labels(
            [job.contact_id for job in failed_jobs if job.contact_id is not None]
        )
        for job in failed_jobs:
            job_spec = AGENT_SPECS.get(job.agent_id)
            if job.campaign_id is not None and job.campaign_contact_id is not None:
                href = f"/admin/campaigns/{job.campaign_id}/contacts/{job.campaign_contact_id}"
            else:
                href = f"/admin/jobs/{job.id}"
            items.append(
                FailureItem(
                    kind="job",
                    category=_failure_category(
                        job.agent_id,
                        code=job.error_class,
                        attempts_exhausted=job.attempts >= job.max_attempts,
                    ),
                    severity="warning",
                    reason=job.error_class or "Job failed.",
                    campaign_id=job.campaign_id,
                    campaign_name=(
                        campaign_names.get(job.campaign_id) if job.campaign_id else None
                    ),
                    campaign_contact_id=job.campaign_contact_id,
                    contact_label=(
                        job_contact_labels.get(job.contact_id) if job.contact_id else None
                    ),
                    company_label=None,
                    agent_id=job.agent_id,
                    agent_name=job_spec.display_name if job_spec else job.agent_id.value,
                    job_id=job.id,
                    attempt_count=job.attempts,
                    max_attempts=job.max_attempts,
                    retryable=job.error_class not in ("terminal", "AgentTerminalError"),
                    latest_at=job.updated_at,
                    diagnosis_href=href,
                    action=None,
                )
            )

        # 4. Stale leases.
        stale_statement = (
            select(AgentJob)
            .where(*self._stale_lease_filter())
            .order_by(AgentJob.lease_expires_at.asc())
            .limit(FAILURES_CAP)
        )
        if campaign_filter is not None:
            stale_statement = stale_statement.where(AgentJob.campaign_id == campaign_filter)
        for job in self._session.scalars(stale_statement).all():
            job_spec = AGENT_SPECS.get(job.agent_id)
            expired = job.lease_expires_at
            age = int((now - expired).total_seconds()) if expired else None
            items.append(
                FailureItem(
                    kind="stale_lease",
                    category="stale_job",
                    severity="warning",
                    reason=(
                        f"Lease held by {job.lease_owner or 'an unknown worker'} expired"
                        + (f" {age} seconds ago." if age is not None else ".")
                    ),
                    campaign_id=job.campaign_id,
                    campaign_name=(
                        campaign_names.get(job.campaign_id) if job.campaign_id else None
                    ),
                    campaign_contact_id=job.campaign_contact_id,
                    contact_label=None,
                    company_label=None,
                    agent_id=job.agent_id,
                    agent_name=job_spec.display_name if job_spec else job.agent_id.value,
                    job_id=job.id,
                    attempt_count=job.attempts,
                    max_attempts=job.max_attempts,
                    retryable=False,
                    latest_at=job.lease_expires_at,
                    diagnosis_href=f"/admin/jobs/{job.id}",
                    action=None,
                    action_note=(
                        "Lease recovery runs through the orchestrator's recovery path, "
                        "not from this page."
                    ),
                )
            )

        # 5. Failed Company Intelligence jobs (company-scoped, outside the
        #    Campaign pipeline; hidden per-Campaign because they belong to no
        #    Campaign). Includes the automatically handed-off jobs, so a failed
        #    Research -> Intelligence handoff is as visible as any other failure.
        if campaign_filter is None:
            company_names = {
                company_id: name
                for company_id, name in self._session.execute(
                    select(Company.id, Company.name).where(
                        Company.id.in_(
                            select(CompanyIntelligenceJob.company_id).where(
                                CompanyIntelligenceJob.status == IntelligenceJobStatus.FAILED
                            )
                        )
                    )
                ).all()
            }
            for ci_job in self._session.scalars(
                select(CompanyIntelligenceJob)
                .where(CompanyIntelligenceJob.status == IntelligenceJobStatus.FAILED)
                .order_by(CompanyIntelligenceJob.finished_at.desc())
                .limit(FAILURES_CAP)
            ).all():
                automatic = ci_job.requested_by == RESEARCH_HANDOFF_ACTOR
                items.append(
                    FailureItem(
                        kind="ci_job",
                        category="company_intelligence",
                        severity="warning",
                        reason=ci_job.last_error or ci_job.error_class or "Job failed.",
                        campaign_id=None,
                        campaign_name=None,
                        campaign_contact_id=None,
                        contact_label=None,
                        company_label=company_names.get(ci_job.company_id),
                        agent_id=None,
                        agent_name="Company Intelligence",
                        job_id=ci_job.id,
                        attempt_count=ci_job.attempts,
                        max_attempts=ci_job.max_attempts,
                        retryable=False,
                        latest_at=ci_job.finished_at,
                        diagnosis_href=f"/admin/companies/{ci_job.company_id}",
                        action=None,
                        action_note=(
                            ("Queued automatically after Research. " if automatic else "")
                            + "Re-request it from the Company Intelligence pages "
                            "after addressing the recorded cause."
                        ),
                    )
                )

        items.sort(
            key=lambda item: item.latest_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return items

    # -- agent/stages --------------------------------------------------------

    def stages_index(self) -> tuple[StageOpsRow, ...]:
        phase2 = self._phase2.overview()
        cards = {card.agent_id: card for card in phase2.agents}

        override_counts: dict[AgentIdentifier, int] = {
            agent_id: int(count)
            for agent_id, count in self._session.execute(
                select(
                    CampaignAgentOverride.agent_id, func.count(CampaignAgentOverride.id)
                ).group_by(CampaignAgentOverride.agent_id)
            ).all()
        }
        stage_counts: dict[AgentIdentifier, dict[str, int]] = {}
        for agent_id, status_value, count in self._session.execute(
            select(
                CampaignContactAgentState.agent_id,
                CampaignContactAgentState.status,
                func.count(CampaignContactAgentState.campaign_contact_id),
            ).group_by(CampaignContactAgentState.agent_id, CampaignContactAgentState.status)
        ).all():
            stage_counts.setdefault(agent_id, {})[status_value.value] = int(count)

        durations: dict[AgentIdentifier, tuple[float, int]] = {}
        for agent_id, avg_seconds, sample in self._session.execute(
            select(
                AgentJob.agent_id,
                func.avg(
                    func.extract("epoch", AgentJob.finished_at)
                    - func.extract("epoch", AgentJob.started_at)
                ),
                func.count(AgentJob.id),
            )
            .where(
                AgentJob.status == AgentJobStatus.SUCCEEDED,
                AgentJob.started_at.is_not(None),
                AgentJob.finished_at.is_not(None),
                AgentJob.updated_at >= _utcnow() - timedelta(days=30),
            )
            .group_by(AgentJob.agent_id)
        ).all():
            if avg_seconds is not None:
                durations[agent_id] = (float(avg_seconds), int(sample))

        rows: list[StageOpsRow] = []
        for agent_id in PIPELINE_ORDER:
            spec = AGENT_SPECS[agent_id]
            card = cards.get(agent_id)
            if card is None:  # pragma: no cover - overview always covers registry
                continue
            duration = durations.get(agent_id)
            rows.append(
                StageOpsRow(
                    card=card,
                    implemented=spec.implemented,
                    skippable=spec.skippable,
                    dependencies=spec.dependencies,
                    workers=STAGE_WORKERS.get(agent_id, ()),
                    override_count=override_counts.get(agent_id, 0),
                    stage_status_counts=stage_counts.get(agent_id, {}),
                    avg_duration_seconds=duration[0] if duration else None,
                    duration_sample=duration[1] if duration else 0,
                )
            )
        return tuple(rows)

    def stage_detail(self, agent_id: AgentIdentifier) -> StageDetailView | None:
        rows = {row.agent_id: row for row in self.stages_index()}
        row = rows.get(agent_id)
        if row is None:
            return None
        detail = self._phase2.agent_detail(agent_id)
        if detail is None:  # pragma: no cover - registry agents always resolve
            return None
        campaign_names = self._campaign_names()
        overrides = tuple(
            StageOverrideRow(
                campaign_id=override.campaign_id,
                campaign_name=campaign_names.get(override.campaign_id, "(deleted)"),
                status=override.status,
                reason=override.reason,
                updated_at=override.updated_at,
            )
            for override in self._session.scalars(
                select(CampaignAgentOverride)
                .where(CampaignAgentOverride.agent_id == agent_id)
                .order_by(CampaignAgentOverride.updated_at.desc())
            ).all()
        )
        failures = [item for item in self._failure_items() if item.agent_id is agent_id][:10]

        stored_control = self._session.get(AgentControl, agent_id)
        config = stored_control.config if stored_control is not None else None
        studio_paths = {
            AgentIdentifier.RESEARCH: "/admin/agents/studio/research",
            AgentIdentifier.EMAIL: "/admin/agents/studio/email",
            AgentIdentifier.VERIFICATION: "/admin/agents/studio/verification",
            AgentIdentifier.PERSONALIZATION: "/admin/agents/studio/personalization",
            AgentIdentifier.CAPTURE: "/admin/agents/studio/capture",
            AgentIdentifier.COMPANY: "/admin/agents/studio/company",
            AgentIdentifier.INSIGHTS: "/admin/agents/studio/insights",
        }
        return StageDetailView(
            row=row,
            effective_global=detail.effective_control,
            overrides=overrides,
            open_jobs=detail.open_jobs,
            recent_failures=tuple(failures),
            recent_activity=detail.recent_activity,
            config_summary=dict(config) if isinstance(config, dict) else {},
            studio_href=studio_paths.get(agent_id),
        )

    # -- contacts ------------------------------------------------------------

    def contacts_index(
        self,
        *,
        query: str | None = None,
        campaign_id: uuid.UUID | None = None,
        page: int = 1,
    ) -> ContactsIndexView:
        statement = (
            select(Contact)
            .where(Contact.merged_into_id.is_(None))
            .order_by(Contact.updated_at.desc())
        )
        count_statement = select(func.count(Contact.id)).where(Contact.merged_into_id.is_(None))
        if query:
            needle = f"%{query.strip()}%"
            condition = or_(
                Contact.first_name.ilike(needle),
                Contact.last_name.ilike(needle),
                Contact.email.ilike(needle),
                Contact.company_name.ilike(needle),
                Contact.company_domain.ilike(needle),
            )
            statement = statement.where(condition)
            count_statement = count_statement.where(condition)
        if campaign_id is not None:
            membership_exists = (
                select(CampaignContact.id)
                .where(
                    CampaignContact.contact_id == Contact.id,
                    CampaignContact.campaign_id == campaign_id,
                )
                .exists()
            )
            statement = statement.where(membership_exists)
            count_statement = count_statement.where(membership_exists)

        total = int(self._session.scalar(count_statement) or 0)
        pages = max(1, -(-total // PAGE_SIZE))
        page = max(1, min(page, pages))
        contacts = self._session.scalars(
            statement.limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)
        ).all()

        memberships = self._memberships_for([contact.id for contact in contacts])
        rows = tuple(
            self._contact_ops_row(contact, memberships.get(contact.id, ())) for contact in contacts
        )
        return ContactsIndexView(
            rows=rows,
            total=total,
            page=page,
            pages=pages,
            query=query,
            campaign_filter=campaign_id,
            campaign_options=self._campaign_options(),
        )

    def _memberships_for(
        self, contact_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[MembershipRow, ...]]:
        if not contact_ids:
            return {}
        campaign_names = self._campaign_names()
        grouped: dict[uuid.UUID, list[MembershipRow]] = {}
        for membership in self._session.scalars(
            select(CampaignContact)
            .where(CampaignContact.contact_id.in_(set(contact_ids)))
            .order_by(CampaignContact.enrolled_at.desc())
        ).all():
            grouped.setdefault(membership.contact_id, []).append(
                MembershipRow(
                    campaign_contact_id=membership.id,
                    campaign_id=membership.campaign_id,
                    campaign_name=campaign_names.get(membership.campaign_id, "(deleted)"),
                    membership_status=membership.membership_status,
                    eligibility=membership.eligibility_status,
                    pipeline_status=membership.pipeline_status,
                    current_stage=membership.current_stage,
                    current_stage_name=(
                        AGENT_SPECS[membership.current_stage].display_name
                        if membership.current_stage
                        else None
                    ),
                    review_state=membership.review_state or "",
                    enrolled_at=membership.enrolled_at,
                    updated_at=membership.updated_at,
                )
            )
        return {contact_id: tuple(rows) for contact_id, rows in grouped.items()}

    def _contact_ops_row(
        self, contact: Contact, memberships: tuple[MembershipRow, ...]
    ) -> ContactOpsRow:
        suppressed = bool(self._active_suppressions(contact.email, contact.company_domain))
        return ContactOpsRow(
            contact_id=contact.id,
            name=_contact_name(contact),
            title=contact.title,
            company_label=_company_label(contact),
            company_id=contact.company_id,
            email=contact.email,
            suppressed=suppressed,
            membership_count=len(memberships),
            memberships=memberships,
            updated_at=contact.updated_at,
        )

    def _active_suppressions(self, email: str | None, domain: str | None) -> list[Suppression]:
        conditions = []
        if email:
            conditions.append(func.lower(Suppression.value) == email.lower())
        if domain:
            conditions.append(func.lower(Suppression.value) == domain.lower())
        if email and "@" in email:
            conditions.append(func.lower(Suppression.value) == email.split("@", 1)[1].lower())
        if not conditions:
            return []
        return list(
            self._session.scalars(
                select(Suppression).where(Suppression.is_active.is_(True), or_(*conditions))
            ).all()
        )

    def contact(self, contact_id: uuid.UUID) -> AdminContactView | None:
        contact = self._session.get(Contact, contact_id)
        if contact is None:
            return None
        memberships = self._memberships_for([contact.id]).get(contact.id, ())
        suppressions = tuple(
            SuppressionRow(
                suppression_type=row.suppression_type.value,
                value=row.value,
                reason=row.reason.value,
                active=row.is_active,
                created_at=row.created_at,
            )
            for row in self._active_suppressions(contact.email, contact.company_domain)
        )

        candidates = self._session.scalars(
            select(EmailCandidate)
            .where(EmailCandidate.contact_id == contact.id)
            .order_by(EmailCandidate.rank.asc())
        ).all()
        candidate_addresses = {candidate.email for candidate in candidates}
        addresses = set(candidate_addresses)
        if contact.email:
            addresses.add(contact.email)
        verifications: dict[str, ExactEmailVerification] = {}
        if addresses:
            for verification in self._session.scalars(
                select(ExactEmailVerification)
                .where(ExactEmailVerification.email.in_(addresses))
                .order_by(ExactEmailVerification.checked_at.desc())
            ).all():
                verifications.setdefault(verification.email, verification)

        # Which of these addresses a contact file supplied. One statement, so the
        # card can distinguish "no provider has answered yet" from "no provider
        # was ever asked" without a query per row.
        imported_addresses: set[str] = set()
        if addresses:
            imported_addresses = {
                value
                for value in self._session.scalars(
                    select(ImportedContactEmail.normalized_email).where(
                        ImportedContactEmail.contact_id == contact.id,
                        ImportedContactEmail.normalized_email.in_(addresses),
                        ImportedContactEmail.slot == ImportedEmailSlot.PRIMARY,
                        ImportedContactEmail.email_stage_outcome
                        == ImportedEmailStageOutcome.IMPORTED_EMAIL_ACCEPTED,
                    )
                ).all()
                if value
            }

        def _email_row(address: str, source: str) -> EmailStateRow:
            verification = verifications.get(address)
            return EmailStateRow(
                email=address,
                source=source,
                verification_result=verification.result.value if verification else None,
                verified_at=verification.checked_at if verification else None,
                imported=verification is None and address in imported_addresses,
            )

        emails: list[EmailStateRow] = []
        if contact.email and contact.email not in candidate_addresses:
            emails.append(_email_row(contact.email, "canonical"))
        emails.extend(
            _email_row(candidate.email, candidate.source.value) for candidate in candidates
        )

        captures = tuple(
            CaptureRow(
                capture_id=promotion.capture_id,
                kind="linkedin_capture_promotion",
                captured_at=promotion.created_at,
                outcome=promotion.contact_outcome.value,
                href=f"/contact-captures/{promotion.capture_id}",
            )
            for promotion in self._session.scalars(
                select(ContactCapturePromotion)
                .where(ContactCapturePromotion.promoted_contact_id == contact.id)
                .order_by(ContactCapturePromotion.created_at.desc())
                .limit(20)
            ).all()
        )

        campaign_names = self._campaign_names()
        draft_rows = self._session.execute(
            select(DraftVersion, DraftApproval)
            .outerjoin(DraftApproval, DraftApproval.draft_version_id == DraftVersion.id)
            .where(DraftVersion.contact_id == contact.id)
            .order_by(DraftVersion.created_at.desc())
            .limit(10)
        ).all()
        drafts = tuple(
            DraftSummaryRow(
                draft_version_id=draft.id,
                campaign_id=draft.campaign_id,
                campaign_name=campaign_names.get(draft.campaign_id),
                version_number=draft.version_number,
                subject=draft.subject,
                approval_status=approval.status.value if approval else None,
                created_at=draft.created_at,
            )
            for draft, approval in draft_rows
        )

        return AdminContactView(
            contact_id=contact.id,
            name=_contact_name(contact),
            title=contact.title,
            email=contact.email,
            company_label=_company_label(contact),
            company_id=contact.company_id,
            company_domain=contact.company_domain,
            linkedin_url=contact.linkedin_url,
            location=contact.location,
            country=contact.country,
            created_at=contact.created_at,
            updated_at=contact.updated_at,
            merged_into_id=contact.merged_into_id,
            suppressions=suppressions,
            memberships=memberships,
            emails=tuple(emails),
            captures=captures,
            drafts=drafts,
        )

    # -- companies -----------------------------------------------------------

    def companies_index(self, *, query: str | None = None, page: int = 1) -> CompaniesIndexView:
        contact_counts = {
            company_id: int(count)
            for company_id, count in self._session.execute(
                select(Contact.company_id, func.count(Contact.id))
                .where(Contact.company_id.is_not(None))
                .group_by(Contact.company_id)
            ).all()
        }
        dossier_counts = {
            company_id: int(count)
            for company_id, count in self._session.execute(
                select(
                    CompanyDossierVersion.company_id, func.count(CompanyDossierVersion.id)
                ).group_by(CompanyDossierVersion.company_id)
            ).all()
        }
        statement = select(Company).order_by(Company.updated_at.desc())
        count_statement = select(func.count(Company.id))
        if query:
            needle = f"%{query.strip()}%"
            condition = or_(Company.name.ilike(needle), Company.domain.ilike(needle))
            statement = statement.where(condition)
            count_statement = count_statement.where(condition)
        total = int(self._session.scalar(count_statement) or 0)
        pages = max(1, -(-total // PAGE_SIZE))
        page = max(1, min(page, pages))
        companies = self._session.scalars(
            statement.limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)
        ).all()
        rows = tuple(
            CompanyOpsRow(
                company_id=company.id,
                name=company.name,
                domain=company.domain,
                research_state=company.research_state.value,
                contact_count=contact_counts.get(company.id, 0),
                dossier_count=dossier_counts.get(company.id, 0),
                updated_at=company.updated_at,
            )
            for company in companies
        )
        return CompaniesIndexView(rows=rows, total=total, page=page, pages=pages, query=query)

    def company(self, company_id: uuid.UUID) -> AdminCompanyView | None:
        detail = company_detail_service.get_company_detail(self._session, company_id)
        if detail is None:
            return None
        company = detail.company

        linked = tuple(
            self._contact_ops_row(
                link.contact,
                self._memberships_for([link.contact.id]).get(link.contact.id, ()),
            )
            for link in detail.linked_contacts[:50]
        )
        campaign_names = self._campaign_names()
        campaign_ids = {
            campaign_id
            for (campaign_id,) in self._session.execute(
                select(CampaignContact.campaign_id)
                .join(Contact, Contact.id == CampaignContact.contact_id)
                .where(Contact.company_id == company.id)
                .distinct()
            ).all()
        }
        dossiers = tuple(
            DossierRow(
                dossier_id=summary.version.id,
                version_number=summary.version.version_number,
                is_current=summary.version.is_current,
                interpreter=summary.version.interpreter,
                interpreter_version=summary.version.interpreter_version,
                created_at=summary.version.created_at,
            )
            for summary in detail.dossier_versions
        )

        research_jobs = self._company_research_jobs(company.id, campaign_names)
        domain_state = (
            detail.domain_resolution.decision.state.value
            if detail.domain_resolution is not None
            else None
        )

        latest_ci_job = self._session.scalars(
            select(CompanyIntelligenceJob)
            .where(CompanyIntelligenceJob.company_id == company.id)
            .order_by(CompanyIntelligenceJob.created_at.desc())
            .limit(1)
        ).first()
        intelligence_job = (
            IntelligenceJobRow(
                job_id=latest_ci_job.id,
                status=latest_ci_job.status.value,
                requested_by=latest_ci_job.requested_by,
                automatic=latest_ci_job.requested_by == RESEARCH_HANDOFF_ACTOR,
                error_class=latest_ci_job.error_class,
                last_error=latest_ci_job.last_error,
                attempts=latest_ci_job.attempts,
                finished_at=latest_ci_job.finished_at,
                created_at=latest_ci_job.created_at,
            )
            if latest_ci_job is not None
            else None
        )
        intelligence_versions = int(
            self._session.scalar(
                select(func.count(CompanyIntelligenceVersion.id)).where(
                    CompanyIntelligenceVersion.company_id == company.id
                )
            )
            or 0
        )

        return AdminCompanyView(
            company_id=company.id,
            name=company.name,
            domain=company.domain,
            domain_state=str(domain_state) if domain_state else None,
            research_state=company.research_state.value,
            last_researched_at=company.last_researched_at,
            linkedin_company_url=(company.linkedin_company_url or detail.captured_linkedin_url),
            industry=company.industry,
            country=company.country,
            company_size=company.company_size,
            created_at=company.created_at,
            linked_contacts=linked,
            campaign_names=tuple(
                sorted(campaign_names.get(campaign_id, "(deleted)") for campaign_id in campaign_ids)
            ),
            dossiers=dossiers,
            research_jobs=research_jobs,
            conflicts=tuple(str(conflict.kind.value) for conflict in detail.conflicts),
            intelligence_available=self._settings.features.company_intelligence,
            intelligence_href=(
                f"/admin/companies/{company.id}/intelligence"
                if self._settings.features.company_intelligence
                else None
            ),
            intelligence_job=intelligence_job,
            intelligence_version_count=intelligence_versions,
        )

    def _company_research_jobs(
        self, company_id: uuid.UUID, campaign_names: dict[uuid.UUID, str]
    ) -> tuple[CompanyResearchJobRow, ...]:
        jobs = self._session.scalars(
            select(AgentJob)
            .where(
                AgentJob.agent_id == AgentIdentifier.RESEARCH,
                AgentJob.company_id == company_id,
            )
            .order_by(AgentJob.updated_at.desc())
            .limit(20)
        ).all()
        rows: list[CompanyResearchJobRow] = []
        for job in jobs:
            result = job.result if isinstance(job.result, dict) else {}
            fallback = result.get("fallback")
            has_lineage = isinstance(fallback, dict) or "dossier_basis" in result
            rows.append(
                CompanyResearchJobRow(
                    job_id=job.id,
                    status=public_status_for(job.status),
                    campaign_id=job.campaign_id,
                    campaign_name=(
                        campaign_names.get(job.campaign_id) if job.campaign_id else None
                    ),
                    campaign_contact_id=job.campaign_contact_id,
                    dossier_basis=(
                        str(result["dossier_basis"]) if result.get("dossier_basis") else None
                    ),
                    fallback_attempted=(
                        bool(fallback.get("attempted"))
                        if isinstance(fallback, dict)
                        else (False if has_lineage else None)
                    ),
                    fallback_status=(
                        str(fallback.get("status"))
                        if isinstance(fallback, dict) and fallback.get("status")
                        else None
                    ),
                    finished_at=job.finished_at,
                )
            )
        return tuple(rows)

    # -- review --------------------------------------------------------------

    def review(self, *, view: str = "awaiting") -> ReviewIndexView:
        available = self._settings.features.drafting or self._settings.features.email_generation
        if not available:
            available = bool(self._session.scalar(select(func.count(DraftVersion.id))))
        if not available:
            return ReviewIndexView(
                available=False, awaiting=0, approved=0, discarded=0, rows=(), view=view
            )
        counts = drafts_service.queue_counts(self._session)
        queue = drafts_service.list_queue(self._session, view=view, limit=PAGE_SIZE)
        rows = tuple(
            ReviewRow(
                draft_version_id=row.draft_version_id,
                campaign_id=row.campaign_id,
                campaign_name=row.campaign_name,
                contact_id=row.contact_id,
                campaign_contact_id=row.campaign_contact_id,
                contact_label=row.contact_name,
                version_number=row.version_number,
                subject=row.subject,
                approval_status=row.decision.value if row.decision else None,
                policy_version_id=None,
                created_at=row.created_at,
            )
            for row in queue.rows
        )
        return ReviewIndexView(
            available=True,
            awaiting=counts.awaiting,
            approved=counts.approved,
            discarded=counts.discarded,
            rows=rows,
            view=view,
        )

    # -- providers & usage ---------------------------------------------------

    def providers(self) -> ProvidersView:
        now = _utcnow()

        def _window(provider: str, days: int) -> ProviderUsageWindow:
            since = now - timedelta(days=days)
            row = self._session.execute(
                select(
                    func.count(UsageLedgerEntry.id),
                    func.coalesce(func.sum(UsageLedgerEntry.units), 0),
                    func.sum(UsageLedgerEntry.estimated_cost),
                    func.count(UsageLedgerEntry.id).filter(
                        UsageLedgerEntry.result.notin_(("ok", "success", "hit"))
                    ),
                    func.count(UsageLedgerEntry.id).filter(UsageLedgerEntry.cache_status == "hit"),
                ).where(
                    UsageLedgerEntry.provider == provider,
                    UsageLedgerEntry.attempted_at >= since,
                )
            ).one()
            calls, units, cost, failures, cache_hits = row
            currency = self._session.scalar(
                select(UsageLedgerEntry.currency)
                .where(UsageLedgerEntry.provider == provider)
                .order_by(UsageLedgerEntry.attempted_at.desc())
                .limit(1)
            )
            return ProviderUsageWindow(
                calls=int(calls or 0),
                units=float(units or 0),
                estimated_cost=float(cost) if cost is not None else None,
                currency=currency,
                failures=int(failures or 0),
                cache_hits=int(cache_hits or 0),
            )

        def _last(provider: str, *, failed: bool) -> tuple[datetime | None, str | None]:
            statement = (
                select(UsageLedgerEntry)
                .where(UsageLedgerEntry.provider == provider)
                .order_by(UsageLedgerEntry.attempted_at.desc())
                .limit(1)
            )
            if failed:
                statement = statement.where(
                    UsageLedgerEntry.result.notin_(("ok", "success", "hit"))
                )
            entry = self._session.scalars(statement).first()
            if entry is None:
                return None, None
            return entry.attempted_at, entry.result if failed else None

        settings = self._settings
        features = settings.features
        descriptors: list[ProviderStatusView] = []

        claude_features = tuple(
            name
            for name, enabled in (
                ("research_claude_fallback", features.research_claude_fallback),
                ("insights_research", features.insights_research),
                ("drafting", features.drafting),
                ("company_intelligence", features.company_intelligence),
                ("model_company_domain_lookup", features.model_company_domain_lookup),
            )
            if enabled
        )
        for provider_id, display, configured, note, flags in (
            (
                "claude_cli",
                "Claude CLI",
                bool(settings.claude_cli_path),
                (
                    f"Command: {settings.claude_cli_path} "
                    f"(version label {settings.claude_cli_version_label}). "
                    "Runs on the operator's subscription; no API key is stored."
                ),
                claude_features,
            ),
            (
                "millionverifier",
                "MillionVerifier",
                settings.has_millionverifier_key(),
                (
                    "API key configured."
                    if settings.has_millionverifier_key()
                    else "No API key configured — the deterministic simulator answers instead."
                ),
                tuple(name for name in ("millionverifier",) if features.millionverifier),
            ),
            (
                "logo_dev",
                "Logo.dev",
                settings.has_logo_dev_key(),
                (
                    "API key configured."
                    if settings.has_logo_dev_key()
                    else "No API key configured — domain lookups are unavailable."
                ),
                tuple(
                    name
                    for name in ("salesnav_domain_enrichment",)
                    if features.salesnav_domain_enrichment
                ),
            ),
            (
                "debounce",
                "DeBounce",
                False,
                "Registered in the provider registry; no credential path is configured yet.",
                (),
            ),
        ):
            last_used, _ = _last(provider_id, failed=False)
            last_failed, failure_reason = _last(provider_id, failed=True)
            descriptors.append(
                ProviderStatusView(
                    provider_id=provider_id,
                    display_name=display,
                    configured=configured,
                    configuration_note=note,
                    feature_flags=flags,
                    enabled=bool(flags),
                    usage_7d=_window(provider_id, 7),
                    usage_30d=_window(provider_id, 30),
                    last_used_at=last_used,
                    last_failure_at=last_failed,
                    last_failure_reason=failure_reason,
                )
            )

        recent = tuple(
            UsageEntryRow(
                provider=entry.provider,
                operation=entry.operation,
                result=entry.result,
                units=float(entry.units) if entry.units is not None else None,
                origin=entry.origin,
                campaign_id=entry.campaign_id,
                attempted_at=entry.attempted_at,
            )
            for entry in self._session.scalars(
                select(UsageLedgerEntry).order_by(UsageLedgerEntry.attempted_at.desc()).limit(25)
            ).all()
        )
        return ProvidersView(providers=tuple(descriptors), ledger_recent=recent)

    # -- configuration -------------------------------------------------------

    def configuration(self) -> ConfigurationView:
        settings = self._settings
        controls = tuple(
            self._phase2._control_view(agent_id, campaign=None, global_control=control)
            for agent_id, control in self._global_controls().items()
        )
        campaign_names = self._campaign_names()
        overrides = tuple(
            OverrideSummaryRow(
                campaign_id=override.campaign_id,
                campaign_name=campaign_names.get(override.campaign_id, "(deleted)"),
                agent_id=override.agent_id,
                agent_name=AGENT_SPECS[override.agent_id].display_name,
                status=override.status,
                reason=override.reason,
                updated_at=override.updated_at,
            )
            for override in self._session.scalars(
                select(CampaignAgentOverride).order_by(CampaignAgentOverride.updated_at.desc())
            ).all()
        )

        policies: list[PolicyStatusRow] = []
        active_personalization = personalization_policy.active_policy(self._session)
        personalization_count = len(personalization_policy.list_policy_versions(self._session))
        policies.append(
            PolicyStatusRow(
                family="personalization",
                label="Personalization policy",
                active_version=(
                    f"v{active_personalization.version_number}"
                    if active_personalization is not None
                    else None
                ),
                activated_at=None,
                version_count=personalization_count,
                manage_href="/admin/agents/studio/personalization",
            )
        )
        waterfall = verification_studio.active_waterfall(self._session)
        policies.append(
            PolicyStatusRow(
                family="verification_waterfall",
                label="Verification waterfall",
                active_version=f"v{waterfall.version_number}" if waterfall else None,
                activated_at=None,
                version_count=int(
                    self._session.scalar(select(func.count(VerificationWaterfallPolicyVersion.id)))
                    or 0
                ),
                manage_href="/admin/agents/studio/verification",
            )
        )
        pattern = verification_studio.active_pattern_policy(self._session)
        policies.append(
            PolicyStatusRow(
                family="email_pattern",
                label="Email pattern policy",
                active_version=f"v{pattern.version_number}" if pattern else None,
                activated_at=None,
                version_count=int(
                    self._session.scalar(select(func.count(EmailPatternPolicyVersion.id))) or 0
                ),
                manage_href="/admin/agents/studio/email",
                note=None if pattern else "No active pattern policy.",
            )
        )

        fallback_config: dict[str, Any] | None = None
        if settings.features.research_claude_fallback:
            fallback_config = {
                "timeout_seconds": settings.research_claude_fallback_timeout_seconds,
                "max_sources": settings.research_claude_fallback_max_sources,
                "max_evidence_items": settings.research_claude_fallback_max_evidence_items,
                "producer_version": settings.research_claude_fallback_producer_version,
                "allowed_tools": list(settings.research_claude_fallback_allowed_tools),
            }

        ttls = {
            "valid": settings.verification_ttl_valid_days,
            "invalid": settings.verification_ttl_invalid_days,
            "catch_all": settings.verification_ttl_catch_all_days,
            "unknown": settings.verification_ttl_unknown_days,
            "disposable": settings.verification_ttl_disposable_days,
        }

        return ConfigurationView(
            app_env=settings.app_env,
            dry_run=settings.dry_run,
            flags=tuple(
                (name, bool(getattr(settings.features, name)))
                for name in type(settings.features).model_fields
            ),
            controls=controls,
            overrides=overrides,
            policies=tuple(policies),
            fallback_config=fallback_config,
            verification_ttls=ttls,
        )

    def _global_controls(self) -> dict[AgentIdentifier, AgentControl | None]:
        stored = {
            control.agent_id: control
            for control in self._session.scalars(select(AgentControl)).all()
        }
        return {agent_id: stored.get(agent_id) for agent_id in PIPELINE_ORDER}

    # -- system --------------------------------------------------------------

    def system(self, *, job_query: str | None = None) -> SystemView:
        job_counts: dict[str, int] = {}
        for status_value, count in self._session.execute(
            select(AgentJob.status, func.count(AgentJob.id)).group_by(AgentJob.status)
        ).all():
            job_counts[status_value.value] = int(count)

        stale = tuple(
            view
            for view in (
                self._phase2.job(job.id)
                for job in self._session.scalars(
                    select(AgentJob)
                    .where(*self._stale_lease_filter())
                    .order_by(AgentJob.lease_expires_at.asc())
                    .limit(20)
                ).all()
            )
            if view is not None
        )

        oldest_open = self._session.scalar(
            select(func.min(AgentJob.created_at)).where(AgentJob.status.in_(_OPEN_JOB_STATUSES))
        )

        alembic_version: str | None = None
        database_ok = True
        try:
            # A savepoint, not a session rollback: this must never discard the
            # caller's transaction just because the table is absent.
            with self._session.begin_nested():
                alembic_version = self._session.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar()
        except Exception:  # noqa: BLE001 - table may not exist in a fresh DB
            alembic_version = None

        audit_tail = tuple(
            AuditRow(
                created_at=event.created_at,
                actor=event.actor,
                action=event.action,
                entity_type=event.entity_type,
                entity_id=event.entity_id,
                previous_state=event.previous_state,
                new_state=event.new_state,
                reason=event.reason,
                dry_run=event.dry_run,
            )
            for event in self._session.scalars(
                select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(50)
            ).all()
        )

        job_search_result: JobView | None = None
        job_search_error: str | None = None
        if job_query:
            try:
                job_search_result = self._phase2.job(uuid.UUID(job_query.strip()))
            except ValueError:
                job_search_error = "Enter a full Agent Job UUID."
            else:
                if job_search_result is None:
                    job_search_error = "No Agent Job with that identifier."

        warnings: list[str] = []
        if stale:
            warnings.append(
                f"{len(stale)} Agent Job lease(s) have expired without completion; "
                "the orchestrator recovers them on its next claim cycle."
            )

        try:
            app_version = importlib.metadata.version("vmr-outbound")
        except importlib.metadata.PackageNotFoundError:  # pragma: no cover
            app_version = "unknown"

        return SystemView(
            app_version=app_version,
            app_env=self._settings.app_env,
            database_ok=database_ok,
            alembic_version=alembic_version,
            job_counts={
                public_status_for(AgentJobStatus(status)): count
                for status, count in job_counts.items()
            },
            queue=self._phase2._queue_counts(),
            stale_leases=stale,
            oldest_open_job_at=oldest_open,
            audit_tail=audit_tail,
            features_enabled=tuple(self._settings.features.enabled()),
            job_search_result=job_search_result,
            job_search_query=job_query,
            job_search_error=job_search_error,
            warnings=tuple(warnings),
        )

    # -- convenience ---------------------------------------------------------

    def job(self, job_id: uuid.UUID) -> JobView | None:
        return self._phase2.job(job_id)

    def recent_activity(self, *, limit: int = 25) -> tuple[ActivityView, ...]:
        return self._phase2._activity(limit=limit)

    def queue(self) -> QueueCounts:
        return self._phase2._queue_counts()

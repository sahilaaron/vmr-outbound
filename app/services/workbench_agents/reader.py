"""The Workbench read model over the Phase 2 execution backbone.

One narrow port, one real implementation. The port exists so a page render can be
tested without a database and so a future deployment could serve the Workbench
from somewhere other than this process; it is not an execution abstraction and
carries no vocabulary of its own. Production wiring constructs
:class:`PhaseTwoWorkbenchReader` directly around the request's session — there is
no registration step and no environment switch, so the real backend is the only
thing production can load.

Everything here is read-only. Counts come from the same tables Phase 2 writes;
control precedence comes from :func:`app.services.agents.controls.effective_control`
rather than being recomputed; the pipeline order comes from the Phase 2 registry
rather than from a constant in the UI.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from app.models.agent import AgentControl, CampaignAgentOverride
from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.email_evidence import ExactEmailVerification
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    AgentJobStatus,
    CampaignContactEligibility,
    PipelineStageStatus,
)
from app.models.pipeline import PipelineEvent
from app.models.verification_job import AgentJob
from app.services.agents import controls as agent_controls
from app.services.agents import jobs as agent_jobs
from app.services.agents.registry import AGENT_SPECS, PIPELINE_ORDER, get_agent_spec
from app.services.pipeline import pipeline_snapshot
from app.services.verification.provider import SIMULATOR_PROVIDER_LABEL
from app.services.workbench_agents.sanitize import sanitize_mapping, sanitize_text
from app.services.workbench_agents.views import (
    ActivityView,
    AgentCardView,
    AgentDetailView,
    CampaignExecutionView,
    CampaignSummaryView,
    ContactExecutionView,
    ContactRowView,
    ControlView,
    JobListView,
    JobView,
    PipelineEventView,
    QueueCounts,
    StageView,
    VerificationEvidenceView,
    WorkbenchOverviewView,
)

#: Phase 2 event types that record a committed stage outcome. A stage counts as
#: completed only when one of these was appended — a succeeded job is never
#: enough on its own, because the committed domain outcome decides where the
#: Contact actually goes next.
COMMITTED_STAGE_EVENTS = frozenset(
    {
        "stage_completed",
        "stage_skipped",
    }
)


@runtime_checkable
class WorkbenchReader(Protocol):
    """Everything the Workbench pages read."""

    def overview(self) -> WorkbenchOverviewView: ...

    def agent_detail(
        self, agent_id: AgentIdentifier, *, campaign_id: uuid.UUID | None = None
    ) -> AgentDetailView | None: ...

    def campaign_execution(
        self,
        campaign_id: uuid.UUID,
        *,
        stage: AgentIdentifier | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> CampaignExecutionView | None: ...

    def contact_execution(
        self, campaign_id: uuid.UUID, campaign_contact_id: uuid.UUID
    ) -> ContactExecutionView | None: ...

    def jobs(
        self,
        *,
        agent_id: AgentIdentifier | None = None,
        campaign_id: uuid.UUID | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JobListView: ...

    def job(self, job_id: uuid.UUID) -> JobView | None: ...


def _contact_label(contact: Contact | None) -> str:
    if contact is None:
        return "(contact record missing)"
    name = " ".join(part for part in (contact.first_name, contact.last_name) if part)
    return name or contact.email or str(contact.id)


def _company_label(contact: Contact | None) -> str | None:
    if contact is None:
        return None
    parts = [part for part in (contact.company_name, contact.company_domain) if part]
    return " · ".join(parts) if parts else None


def _retryable_failure(job: AgentJob) -> bool:
    return bool((job.error or {}).get("retryable", False))


def _retry_refusal(job: AgentJob) -> str | None:
    """Why a retry is not offered, in the operator's language.

    Mirrors the Phase 2 guards in :func:`app.services.agents.jobs.retry_failed_job`
    exactly. The Workbench does not decide retry policy; it explains the decision
    before the operator spends a click discovering it.
    """

    if job.status is not AgentJobStatus.FAILED:
        return f"Only a failed job can be retried; this one is {agent_jobs.public_status(job)}."
    if not _retryable_failure(job):
        return "The failure is terminal. Phase 2 will not requeue it."
    if job.attempts >= job.max_attempts:
        return f"The job used its {job.max_attempts} permitted attempts."
    return None


class PhaseTwoWorkbenchReader:
    """The production read model: Phase 2 tables, projected for display."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # --- shared helpers --------------------------------------------------

    def _campaign_names(self) -> dict[uuid.UUID, str]:
        return {
            campaign_id: name
            for campaign_id, name in self._session.execute(select(Campaign.id, Campaign.name)).all()
        }

    def _queue_counts(self, filters: Sequence[ColumnElement[bool]] = ()) -> QueueCounts:
        """Job counts by public status, plus the terminal-failure split.

        The public label comes from Phase 2 (``jobs.public_status_for``) rather
        than a local map, so a status added to the queue appears here without
        anyone remembering to update the Workbench.
        """

        conditions = list(filters)
        base = select(AgentJob.status, func.count(AgentJob.id)).group_by(AgentJob.status)
        if conditions:
            base = base.where(*conditions)
        by_status: dict[str, int] = {}
        for stored, count in self._session.execute(base).all():
            public = agent_jobs.public_status_for(stored)
            by_status[public] = by_status.get(public, 0) + int(count)
        terminal_statement = select(func.count(AgentJob.id)).where(
            AgentJob.status == AgentJobStatus.FAILED,
            func.coalesce(AgentJob.error["retryable"].as_boolean(), False).is_(False),
        )
        if conditions:
            terminal_statement = terminal_statement.where(*conditions)
        terminal = int(self._session.scalar(terminal_statement) or 0)
        return QueueCounts(by_status=by_status, terminal_failures=terminal)

    def _control_view(
        self,
        agent_id: AgentIdentifier,
        *,
        campaign: Campaign | None,
        global_control: AgentControl | None,
    ) -> ControlView:
        spec = get_agent_spec(agent_id)
        global_status = global_control.status if global_control else spec.default_status
        if campaign is None:
            return ControlView(
                agent_id=agent_id,
                display_name=spec.display_name,
                position=spec.position,
                status=global_status,
                source="global" if global_control else "registry_default",
                reason=global_control.reason if global_control else None,
                implemented=spec.implemented,
                global_status=global_status,
                global_version=global_control.version if global_control else None,
                campaign_version=None,
                updated_by=global_control.updated_by if global_control else None,
                updated_at=global_control.updated_at if global_control else None,
            )
        effective = agent_controls.effective_control(
            self._session, campaign=campaign, agent_id=agent_id
        )
        return ControlView(
            agent_id=agent_id,
            display_name=spec.display_name,
            position=spec.position,
            status=effective.status,
            source=effective.source,
            reason=effective.reason,
            implemented=spec.implemented,
            global_status=global_status,
            global_version=effective.global_version,
            campaign_version=effective.campaign_version,
            updated_by=global_control.updated_by if global_control else None,
            updated_at=global_control.updated_at if global_control else None,
        )

    def _job_view(
        self,
        job: AgentJob,
        *,
        campaign_names: dict[uuid.UUID, str],
        contact_labels: dict[uuid.UUID, str],
    ) -> JobView:
        spec = get_agent_spec(job.agent_id)
        retryable = _retryable_failure(job)
        refusal = _retry_refusal(job)
        error = job.error or {}
        raw_message = job.last_error or (
            str(error.get("message")) if error.get("message") is not None else None
        )
        return JobView(
            job_id=job.id,
            agent_id=job.agent_id,
            agent_name=spec.display_name,
            public_status=agent_jobs.public_status(job),
            stored_status=job.status,
            task_kind=job.task_kind,
            idempotency_key=job.idempotency_key,
            attempt_count=job.attempts,
            max_attempts=job.max_attempts,
            priority=job.priority,
            campaign_id=job.campaign_id,
            campaign_name=campaign_names.get(job.campaign_id) if job.campaign_id else None,
            campaign_contact_id=job.campaign_contact_id,
            contact_id=job.contact_id,
            contact_label=contact_labels.get(job.contact_id) if job.contact_id else None,
            created_at=job.created_at,
            updated_at=job.updated_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            next_run_at=job.next_run_at,
            lease_owner=job.lease_owner,
            lease_expires_at=job.lease_expires_at,
            input_reference=sanitize_mapping(dict(job.input_reference or {})) or {},
            result=sanitize_mapping(dict(job.result)) if job.result is not None else None,
            error_class=job.error_class,
            error_message=sanitize_text(raw_message),
            error_detail=sanitize_mapping(
                {k: v for k, v in error.items() if k not in {"message", "class", "retryable"}}
            )
            or None,
            outcome_status=job.outcome_status,
            parent_job_id=job.parent_job_id,
            retryable_failure=retryable,
            retry_eligible=refusal is None,
            retry_refusal=refusal,
        )

    def _contact_labels(self, contact_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
        wanted = [cid for cid in contact_ids if cid is not None]
        if not wanted:
            return {}
        rows = self._session.scalars(select(Contact).where(Contact.id.in_(wanted))).all()
        return {contact.id: _contact_label(contact) for contact in rows}

    def _activity(
        self,
        *,
        campaign_id: uuid.UUID | None = None,
        agent_id: AgentIdentifier | None = None,
        limit: int = 25,
    ) -> tuple[ActivityView, ...]:
        statement = (
            select(PipelineEvent, CampaignContact, Campaign, Contact)
            .join(CampaignContact, CampaignContact.id == PipelineEvent.campaign_contact_id)
            .join(Campaign, Campaign.id == CampaignContact.campaign_id)
            .outerjoin(Contact, Contact.id == CampaignContact.contact_id)
            .order_by(PipelineEvent.occurred_at.desc(), PipelineEvent.id.desc())
            .limit(max(0, limit))
        )
        if campaign_id is not None:
            statement = statement.where(CampaignContact.campaign_id == campaign_id)
        if agent_id is not None:
            statement = statement.where(PipelineEvent.agent_id == agent_id)
        return tuple(
            ActivityView(
                event_id=event.id,
                occurred_at=event.occurred_at,
                event_type=event.event_type,
                agent_id=event.agent_id,
                campaign_id=campaign.id,
                campaign_name=campaign.name,
                campaign_contact_id=membership.id,
                contact_label=_contact_label(contact),
                job_id=event.job_id,
                from_status=event.from_status,
                to_status=event.to_status,
                reason_code=event.reason_code,
                reason_detail=sanitize_text(event.reason_detail),
                retryable=event.retryable,
                actor=event.actor,
            )
            for event, membership, campaign, contact in self._session.execute(statement).all()
        )

    # --- overview --------------------------------------------------------

    def overview(self) -> WorkbenchOverviewView:
        global_controls = {
            control.agent_id: control
            for control in self._session.scalars(select(AgentControl)).all()
        }
        campaign_names = self._campaign_names()

        overrides_by_agent: dict[
            AgentIdentifier, list[tuple[uuid.UUID, str, AgentControlStatus]]
        ] = {}
        for override in self._session.scalars(select(CampaignAgentOverride)).all():
            overrides_by_agent.setdefault(override.agent_id, []).append(
                (
                    override.campaign_id,
                    campaign_names.get(override.campaign_id, "(campaign missing)"),
                    override.status,
                )
            )

        per_agent_counts: dict[AgentIdentifier, dict[str, int]] = {}
        for stored_agent, stored_status, count in self._session.execute(
            select(AgentJob.agent_id, AgentJob.status, func.count(AgentJob.id)).group_by(
                AgentJob.agent_id, AgentJob.status
            )
        ).all():
            bucket = per_agent_counts.setdefault(stored_agent, {})
            public = agent_jobs.public_status_for(stored_status)
            bucket[public] = bucket.get(public, 0) + int(count)

        terminal_by_agent: dict[AgentIdentifier, int] = {
            agent: int(count)
            for agent, count in self._session.execute(
                select(AgentJob.agent_id, func.count(AgentJob.id))
                .where(
                    AgentJob.status == AgentJobStatus.FAILED,
                    func.coalesce(AgentJob.error["retryable"].as_boolean(), False).is_(False),
                )
                .group_by(AgentJob.agent_id)
            ).all()
        }

        latest_by_agent: dict[AgentIdentifier, tuple[datetime, str]] = {}
        for event in self._session.scalars(
            select(PipelineEvent)
            .where(PipelineEvent.agent_id.is_not(None))
            .order_by(PipelineEvent.occurred_at.desc())
            .limit(400)
        ).all():
            if event.agent_id is None or event.agent_id in latest_by_agent:
                continue
            latest_by_agent[event.agent_id] = (
                event.occurred_at,
                event.event_type.value.replace("_", " "),
            )

        cards: list[AgentCardView] = []
        for agent_id in PIPELINE_ORDER:
            spec = AGENT_SPECS[agent_id]
            control = self._control_view(
                agent_id, campaign=None, global_control=global_controls.get(agent_id)
            )
            latest = latest_by_agent.get(agent_id)
            cards.append(
                AgentCardView(
                    agent_id=agent_id,
                    display_name=spec.display_name,
                    position=spec.position,
                    control=control,
                    queue=QueueCounts(
                        by_status=per_agent_counts.get(agent_id, {}),
                        terminal_failures=terminal_by_agent.get(agent_id, 0),
                    ),
                    overriding_campaigns=tuple(sorted(overrides_by_agent.get(agent_id, []))),
                    latest_activity_at=latest[0] if latest else None,
                    latest_activity_summary=latest[1] if latest else None,
                    dependencies=spec.dependencies,
                    skippable=spec.skippable,
                    max_attempts=spec.max_attempts,
                )
            )

        campaigns = tuple(self._campaign_summaries(campaign_names, global_controls))
        eligibility_counts = {
            status: int(count)
            for status, count in self._session.execute(
                select(CampaignContact.eligibility_status, func.count(CampaignContact.id)).group_by(
                    CampaignContact.eligibility_status
                )
            ).all()
        }
        pipeline_counts = {
            status.value: int(count)
            for status, count in self._session.execute(
                select(CampaignContact.pipeline_status, func.count(CampaignContact.id)).group_by(
                    CampaignContact.pipeline_status
                )
            ).all()
        }
        sending_card = next(card for card in cards if card.agent_id is AgentIdentifier.SENDING)

        return WorkbenchOverviewView(
            generated_at=datetime.now(UTC),
            agents=tuple(cards),
            queue=self._queue_counts(),
            campaigns=campaigns,
            recent_activity=self._activity(limit=25),
            sending_control=sending_card.control,
            blocked_contacts=eligibility_counts.get(CampaignContactEligibility.BLOCKED, 0),
            suppressed_contacts=self._suppressed_count(),
            active_contacts=(
                pipeline_counts.get(PipelineStageStatus.RUNNING.value, 0)
                + pipeline_counts.get(PipelineStageStatus.RETRYING.value, 0)
            ),
            waiting_contacts=(
                pipeline_counts.get(PipelineStageStatus.WAITING.value, 0)
                + pipeline_counts.get(PipelineStageStatus.PAUSED.value, 0)
            ),
            completed_contacts=pipeline_counts.get(PipelineStageStatus.COMPLETED.value, 0),
        )

    def _suppressed_count(self, campaign_id: uuid.UUID | None = None) -> int:
        """Campaign Contacts whose blocking reasons include a suppression.

        Read from the durable ``blocking_reasons`` Phase 2 already computed —
        the Workbench never re-evaluates the suppression ledger itself.
        """

        statement = select(func.count(CampaignContact.id)).where(
            CampaignContact.blocking_reasons.contains([{"code": "suppression"}])
        )
        if campaign_id is not None:
            statement = statement.where(CampaignContact.campaign_id == campaign_id)
        return int(self._session.scalar(statement) or 0)

    def _campaign_summaries(
        self,
        campaign_names: dict[uuid.UUID, str],
        global_controls: dict[AgentIdentifier, AgentControl],
    ) -> list[CampaignSummaryView]:
        summaries: list[CampaignSummaryView] = []
        campaigns = self._session.scalars(
            select(Campaign).order_by(Campaign.created_at.desc())
        ).all()
        enrolled = {
            campaign_id: int(count)
            for campaign_id, count in self._session.execute(
                select(CampaignContact.campaign_id, func.count(CampaignContact.id)).group_by(
                    CampaignContact.campaign_id
                )
            ).all()
        }
        override_counts = {
            campaign_id: int(count)
            for campaign_id, count in self._session.execute(
                select(
                    CampaignAgentOverride.campaign_id, func.count(CampaignAgentOverride.id)
                ).group_by(CampaignAgentOverride.campaign_id)
            ).all()
        }
        blocked = {
            campaign_id: int(count)
            for campaign_id, count in self._session.execute(
                select(CampaignContact.campaign_id, func.count(CampaignContact.id))
                .where(CampaignContact.eligibility_status == CampaignContactEligibility.BLOCKED)
                .group_by(CampaignContact.campaign_id)
            ).all()
        }
        for campaign in campaigns:
            summaries.append(
                CampaignSummaryView(
                    campaign_id=campaign.id,
                    name=campaign.name,
                    status=campaign.status.value,
                    execution_enabled=campaign.execution_enabled,
                    settings_version=campaign.settings_version,
                    enrolled_contacts=enrolled.get(campaign.id, 0),
                    stage_counts=self._stage_counts(campaign.id),
                    pipeline_status_counts=self._pipeline_status_counts(campaign.id),
                    blocked_contacts=blocked.get(campaign.id, 0),
                    suppressed_contacts=self._suppressed_count(campaign.id),
                    override_count=override_counts.get(campaign.id, 0),
                    queue=self._queue_counts([AgentJob.campaign_id == campaign.id]),
                    sending_status=agent_controls.effective_control(
                        self._session, campaign=campaign, agent_id=AgentIdentifier.SENDING
                    ).status,
                    latest_activity_at=self._latest_activity_at(campaign.id),
                )
            )
        _ = campaign_names, global_controls
        return summaries

    def _stage_counts(self, campaign_id: uuid.UUID) -> dict[str, int]:
        """Campaign Contacts by the Agent stage they are currently on."""

        rows = self._session.execute(
            select(CampaignContact.current_stage, func.count(CampaignContact.id))
            .where(CampaignContact.campaign_id == campaign_id)
            .group_by(CampaignContact.current_stage)
        ).all()
        counts: dict[str, int] = {}
        for stage, count in rows:
            key = stage.value if stage is not None else "unassigned"
            counts[key] = counts.get(key, 0) + int(count)
        return counts

    def _pipeline_status_counts(self, campaign_id: uuid.UUID) -> dict[str, int]:
        return {
            status.value: int(count)
            for status, count in self._session.execute(
                select(CampaignContact.pipeline_status, func.count(CampaignContact.id))
                .where(CampaignContact.campaign_id == campaign_id)
                .group_by(CampaignContact.pipeline_status)
            ).all()
        }

    def _latest_activity_at(self, campaign_id: uuid.UUID) -> datetime | None:
        return self._session.scalar(
            select(func.max(PipelineEvent.occurred_at))
            .join(CampaignContact, CampaignContact.id == PipelineEvent.campaign_contact_id)
            .where(CampaignContact.campaign_id == campaign_id)
        )

    # --- agent detail ----------------------------------------------------

    def agent_detail(
        self, agent_id: AgentIdentifier, *, campaign_id: uuid.UUID | None = None
    ) -> AgentDetailView | None:
        if agent_id not in AGENT_SPECS:  # pragma: no cover - AgentIdentifier constrains callers
            return None
        campaign = self._session.get(Campaign, campaign_id) if campaign_id else None
        if campaign_id is not None and campaign is None:
            return None
        overview = self.overview()
        card = overview.agent(agent_id)
        if card is None:  # pragma: no cover - registry guarantees a card
            return None
        global_control = self._session.get(AgentControl, agent_id)
        effective = self._control_view(agent_id, campaign=campaign, global_control=global_control)
        campaign_names = self._campaign_names()
        open_statement = (
            select(AgentJob)
            .where(
                AgentJob.agent_id == agent_id,
                AgentJob.status.notin_(
                    (
                        AgentJobStatus.SUCCEEDED,
                        AgentJobStatus.CANCELLED,
                    )
                ),
            )
            .order_by(AgentJob.updated_at.desc())
            .limit(25)
        )
        if campaign_id is not None:
            open_statement = open_statement.where(AgentJob.campaign_id == campaign_id)
        open_jobs = list(self._session.scalars(open_statement).all())
        contact_labels = self._contact_labels(
            [job.contact_id for job in open_jobs if job.contact_id]
        )
        return AgentDetailView(
            card=card,
            campaign_id=campaign.id if campaign else None,
            campaign_name=campaign.name if campaign else None,
            effective_control=effective,
            open_jobs=tuple(
                self._job_view(job, campaign_names=campaign_names, contact_labels=contact_labels)
                for job in open_jobs
            ),
            recent_activity=self._activity(agent_id=agent_id, campaign_id=campaign_id, limit=25),
        )

    # --- campaign --------------------------------------------------------

    def campaign_execution(
        self,
        campaign_id: uuid.UUID,
        *,
        stage: AgentIdentifier | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> CampaignExecutionView | None:
        campaign = self._session.get(Campaign, campaign_id)
        if campaign is None:
            return None
        global_controls = {
            control.agent_id: control
            for control in self._session.scalars(select(AgentControl)).all()
        }
        controls_view = tuple(
            self._control_view(
                agent_id, campaign=campaign, global_control=global_controls.get(agent_id)
            )
            for agent_id in PIPELINE_ORDER
        )

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
        total = int(self._session.scalar(count_statement) or 0)
        rows = self._session.execute(
            membership_statement.order_by(CampaignContact.enrolled_at.desc())
            .limit(max(0, limit))
            .offset(max(0, offset))
        ).all()

        contacts = tuple(
            ContactRowView(
                campaign_contact_id=membership.id,
                contact_id=membership.contact_id,
                contact_label=_contact_label(contact),
                company_label=_company_label(contact),
                email=contact.email if contact else None,
                membership_status=membership.membership_status,
                eligibility=membership.eligibility_status,
                pipeline_status=membership.pipeline_status,
                current_stage=membership.current_stage,
                next_stage=membership.next_stage,
                latest_completed_stage=membership.latest_completed_stage,
                blocking_detail=_first_blocking_detail(membership.blocking_reasons),
                suppressed=_is_suppressed(membership.blocking_reasons),
                updated_at=membership.updated_at,
            )
            for membership, contact in rows
        )

        return CampaignExecutionView(
            campaign_id=campaign.id,
            name=campaign.name,
            status=campaign.status.value,
            execution_enabled=campaign.execution_enabled,
            settings_version=campaign.settings_version,
            disabled_reason=campaign.disabled_reason,
            enrolled_contacts=int(
                self._session.scalar(
                    select(func.count(CampaignContact.id)).where(
                        CampaignContact.campaign_id == campaign_id
                    )
                )
                or 0
            ),
            stage_counts=self._stage_counts(campaign_id),
            pipeline_status_counts=self._pipeline_status_counts(campaign_id),
            eligibility_counts={
                status.value: int(count)
                for status, count in self._session.execute(
                    select(CampaignContact.eligibility_status, func.count(CampaignContact.id))
                    .where(CampaignContact.campaign_id == campaign_id)
                    .group_by(CampaignContact.eligibility_status)
                ).all()
            },
            blocked_contacts=int(
                self._session.scalar(
                    select(func.count(CampaignContact.id)).where(
                        CampaignContact.campaign_id == campaign_id,
                        CampaignContact.eligibility_status == CampaignContactEligibility.BLOCKED,
                    )
                )
                or 0
            ),
            suppressed_contacts=self._suppressed_count(campaign_id),
            queue=self._queue_counts([AgentJob.campaign_id == campaign_id]),
            controls=controls_view,
            contacts=contacts,
            contact_total=total,
            recent_events=self._activity(campaign_id=campaign_id, limit=25),
            sending_control=next(
                control for control in controls_view if control.agent_id is AgentIdentifier.SENDING
            ),
        )

    # --- contact execution -----------------------------------------------

    def contact_execution(
        self, campaign_id: uuid.UUID, campaign_contact_id: uuid.UUID
    ) -> ContactExecutionView | None:
        snapshot = pipeline_snapshot(self._session, campaign_contact_id=campaign_contact_id)
        if snapshot is None or snapshot.membership.campaign_id != campaign_id:
            return None
        membership = snapshot.membership
        campaign = self._session.get(Campaign, membership.campaign_id)
        if campaign is None:  # pragma: no cover - protected by FK
            return None
        contact = self._session.get(Contact, membership.contact_id)
        global_controls = {
            control.agent_id: control
            for control in self._session.scalars(select(AgentControl)).all()
        }

        committed = {
            (event.agent_id, event.event_type.value)
            for event in snapshot.events
            if event.agent_id is not None
        }
        by_agent = {state.agent_id: state for state in snapshot.stages}
        stages: list[StageView] = []
        for agent_id in PIPELINE_ORDER:
            spec = AGENT_SPECS[agent_id]
            state = by_agent.get(agent_id)
            control = self._control_view(
                agent_id, campaign=campaign, global_control=global_controls.get(agent_id)
            )
            outcome_committed = any(
                (agent_id, event_type) in committed for event_type in COMMITTED_STAGE_EVENTS
            )
            stages.append(
                StageView(
                    agent_id=agent_id,
                    display_name=spec.display_name,
                    position=spec.position,
                    status=state.status if state else PipelineStageStatus.WAITING,
                    attempt_count=state.attempt_count if state else 0,
                    latest_job_id=state.latest_job_id if state else None,
                    reason_code=state.reason_code if state else None,
                    reason_detail=sanitize_text(state.reason_detail) if state else None,
                    retryable=state.retryable if state else False,
                    waiting_on_agent=state.waiting_on_agent if state else None,
                    output_reference=(
                        sanitize_mapping(dict(state.output_reference))
                        if state and state.output_reference
                        else None
                    ),
                    started_at=state.started_at if state else None,
                    completed_at=state.completed_at if state else None,
                    updated_at=state.updated_at if state else None,
                    control=control,
                    outcome_committed=outcome_committed,
                )
            )

        campaign_names = {campaign.id: campaign.name}
        contact_labels = {membership.contact_id: _contact_label(contact)}
        evidence: tuple[VerificationEvidenceView, ...] = ()
        if contact is not None and contact.email:
            evidence = tuple(
                VerificationEvidenceView(
                    email=row.email,
                    result=row.result.value,
                    provider=row.provider,
                    simulated=row.provider == SIMULATOR_PROVIDER_LABEL,
                    checked_at=row.checked_at,
                    policy_version=row.policy_version,
                )
                for row in self._session.scalars(
                    select(ExactEmailVerification)
                    .where(ExactEmailVerification.email == contact.email)
                    .order_by(ExactEmailVerification.checked_at.desc())
                    .limit(10)
                ).all()
            )

        return ContactExecutionView(
            campaign_contact_id=membership.id,
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            contact_id=membership.contact_id,
            contact_label=_contact_label(contact),
            contact_email=contact.email if contact else None,
            company_label=_company_label(contact),
            membership_status=membership.membership_status,
            eligibility=membership.eligibility_status,
            blocking_reasons=tuple(
                reason for reason in membership.blocking_reasons if isinstance(reason, dict)
            ),
            desired_stage=membership.desired_stage,
            current_stage=membership.current_stage,
            next_stage=membership.next_stage,
            latest_completed_stage=membership.latest_completed_stage,
            pipeline_status=membership.pipeline_status,
            next_action=snapshot.next_action,
            stages=tuple(stages),
            jobs=tuple(
                self._job_view(job, campaign_names=campaign_names, contact_labels=contact_labels)
                for job in snapshot.jobs
            ),
            events=tuple(
                PipelineEventView(
                    event_id=event.id,
                    occurred_at=event.occurred_at,
                    event_type=event.event_type,
                    agent_id=event.agent_id,
                    job_id=event.job_id,
                    from_status=event.from_status,
                    to_status=event.to_status,
                    reason_code=event.reason_code,
                    reason_detail=sanitize_text(event.reason_detail),
                    retryable=event.retryable,
                    actor=event.actor,
                    detail=sanitize_mapping(dict(event.detail)) or {},
                )
                for event in snapshot.events
            ),
            verification_evidence=evidence,
            enrolled_at=membership.enrolled_at,
            updated_at=membership.updated_at,
            review_state=membership.review_state,
            sending_state=membership.sending_state,
        )

    # --- jobs ------------------------------------------------------------

    def jobs(
        self,
        *,
        agent_id: AgentIdentifier | None = None,
        campaign_id: uuid.UUID | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JobListView:
        filters: list[Any] = []
        if agent_id is not None:
            filters.append(AgentJob.agent_id == agent_id)
        if campaign_id is not None:
            filters.append(AgentJob.campaign_id == campaign_id)
        stored = agent_jobs.stored_statuses_for_public(status) if status else ()
        statement = select(AgentJob)
        count_statement = select(func.count(AgentJob.id))
        if filters:
            statement = statement.where(*filters)
            count_statement = count_statement.where(*filters)
        if stored:
            statement = statement.where(AgentJob.status.in_(stored))
            count_statement = count_statement.where(AgentJob.status.in_(stored))
        total = int(self._session.scalar(count_statement) or 0)
        rows = list(
            self._session.scalars(
                statement.order_by(AgentJob.updated_at.desc())
                .limit(max(0, limit))
                .offset(max(0, offset))
            ).all()
        )
        campaign_names = self._campaign_names()
        contact_labels = self._contact_labels([job.contact_id for job in rows if job.contact_id])
        return JobListView(
            jobs=tuple(
                self._job_view(job, campaign_names=campaign_names, contact_labels=contact_labels)
                for job in rows
            ),
            total=total,
            queue=self._queue_counts(filters),
            agent_filter=agent_id,
            campaign_filter=campaign_id,
            status_filter=status if stored else None,
        )

    def job(self, job_id: uuid.UUID) -> JobView | None:
        job = self._session.get(AgentJob, job_id)
        if job is None:
            return None
        campaign_names = self._campaign_names()
        contact_labels = self._contact_labels([job.contact_id] if job.contact_id else [])
        return self._job_view(job, campaign_names=campaign_names, contact_labels=contact_labels)


def _first_blocking_detail(reasons: list[Any]) -> str | None:
    for reason in reasons:
        if isinstance(reason, dict) and reason.get("detail"):
            return sanitize_text(str(reason["detail"]))
    return None


def _is_suppressed(reasons: list[Any]) -> bool:
    return any(
        isinstance(reason, dict) and reason.get("code") == "suppression" for reason in reasons
    )

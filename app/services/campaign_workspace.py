"""The Campaign workspace projection: what one Campaign says about itself.

Every customer-facing Campaign screen — Overview, People, Setup, Activity and
the Campaign list — reads from here. It is a projection: it reads committed
state and writes nothing, and it speaks the customer's vocabulary only. Three
preparation outcomes (see :mod:`app.services.customer_status`), four lifecycle
words, and plain-language reasons. Agents, stages, jobs and reason codes stay in
Admin.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, and_, func, literal, or_, select
from sqlalchemy.orm import Session

from app.models.audit_event import AuditEvent
from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.email_sequence import EmailSequence
from app.models.enums import (
    AgentIdentifier,
    CampaignContactEligibility,
    CampaignStatus,
    ContactWorkflowState,
    PipelineStageStatus,
)
from app.models.import_batch import ImportBatch
from app.models.pipeline import CampaignContactAgentState
from app.services import customer_status
from app.services.customer_status import CustomerContactStatus
from app.services.imports.display import safe_text

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

LIFECYCLE_DRAFT = "draft"
LIFECYCLE_ACTIVE = "active"
LIFECYCLE_PAUSED = "paused"
LIFECYCLE_ARCHIVED = "archived"

LIFECYCLE_LABELS: dict[str, str] = {
    LIFECYCLE_DRAFT: "Draft",
    LIFECYCLE_ACTIVE: "Active",
    LIFECYCLE_PAUSED: "Paused",
    LIFECYCLE_ARCHIVED: "Archived",
}


def lifecycle(campaign: Campaign) -> str:
    """The customer's four lifecycle words, from the two stored facts.

    ``status`` and ``execution_enabled`` are separate columns; the customer sees
    one word. Draft is a Campaign that has never been started; Paused is one that
    was started and then stopped.
    """

    if campaign.status is CampaignStatus.ARCHIVED:
        return LIFECYCLE_ARCHIVED
    if campaign.status is CampaignStatus.DRAFT:
        return LIFECYCLE_DRAFT
    return LIFECYCLE_ACTIVE if campaign.execution_enabled else LIFECYCLE_PAUSED


# ---------------------------------------------------------------------------
# Plain-language reasons
# ---------------------------------------------------------------------------

#: What VMR is doing while a person rests on a stage. Present tense, no Agent.
_PROCESSING_AT: dict[AgentIdentifier, str] = {
    AgentIdentifier.CAPTURE: "Recording the captured details",
    AgentIdentifier.IDENTITY: "Matching this person to their permanent record",
    AgentIdentifier.COMPANY: "Confirming the company and its website",
    AgentIdentifier.RESEARCH: "Researching the company",
    AgentIdentifier.EMAIL: "Working out the email address",
    AgentIdentifier.VERIFICATION: "Confirming the email address",
    AgentIdentifier.INSIGHTS: "Choosing what to open with",
    AgentIdentifier.PERSONALIZATION: "Writing the seven emails",
    AgentIdentifier.SENDING: "Finishing up",
}

#: Why VMR stopped, by the stage it stopped on. Past tense, no Agent.
_STOPPED_AT: dict[AgentIdentifier, str] = {
    AgentIdentifier.CAPTURE: "The captured details could not be used",
    AgentIdentifier.IDENTITY: "This person could not be matched to one record",
    AgentIdentifier.COMPANY: "The company website could not be confirmed",
    AgentIdentifier.RESEARCH: "The company could not be researched",
    AgentIdentifier.EMAIL: "No email address could be found",
    AgentIdentifier.VERIFICATION: "No usable email address could be confirmed",
    AgentIdentifier.INSIGHTS: "Nothing usable was found to open with",
    AgentIdentifier.PERSONALIZATION: "The emails could not be written",
    AgentIdentifier.SENDING: "Preparation ended without a usable package",
}

REASON_SUPPRESSED = "On the suppression list — never contacted"
REASON_NOT_ELIGIBLE = "Not eligible for this Campaign"
REASON_NO_PACKAGE = "Preparation ended without a usable package"
REASON_STOPPED = "VMR stopped before the emails could be prepared"
DETAIL_READY = "Seven emails written"
DETAIL_WAITING = "Waiting its turn"


def _processing_detail(membership: CampaignContact) -> str:
    stage = membership.current_stage or membership.next_stage
    if membership.pipeline_status is PipelineStageStatus.PAUSED:
        return "Paused with the Campaign"
    if stage is None:
        return DETAIL_WAITING
    return _PROCESSING_AT.get(stage, DETAIL_WAITING)


def _stopped_detail(membership: CampaignContact, stopped_stage: AgentIdentifier | None) -> str:
    if _is_suppressed(membership.blocking_reasons):
        return REASON_SUPPRESSED
    if membership.eligibility_status is CampaignContactEligibility.BLOCKED or membership.state in (
        ContactWorkflowState.EXCLUDED,
        ContactWorkflowState.SUPPRESSED,
    ):
        detail = _first_blocking_detail(membership.blocking_reasons)
        return detail or REASON_NOT_ELIGIBLE
    if stopped_stage is not None:
        return _STOPPED_AT.get(stopped_stage, REASON_STOPPED)
    if membership.pipeline_status is PipelineStageStatus.BLOCKED:
        return _first_blocking_detail(membership.blocking_reasons) or REASON_STOPPED
    return REASON_NO_PACKAGE


def _is_suppressed(reasons: list[Any] | None) -> bool:
    return any(
        isinstance(reason, dict) and reason.get("code") == "suppression" for reason in reasons or ()
    )


def _first_blocking_detail(reasons: list[Any] | None) -> str | None:
    for reason in reasons or ():
        if isinstance(reason, dict) and reason.get("detail"):
            return safe_text(str(reason["detail"]))
    return None


def _person_name(contact: Contact | None) -> str:
    if contact is None:
        return "(person record missing)"
    name = " ".join(part for part in (contact.first_name, contact.last_name) if part)
    return safe_text(name) or contact.email or "Unnamed person"


def _company_name(contact: Contact | None) -> str | None:
    if contact is None:
        return None
    return safe_text(contact.company_name) if contact.company_name else None


# ---------------------------------------------------------------------------
# Header and counts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CampaignHeader:
    """What every Campaign tab shows above its content."""

    campaign: Campaign
    lifecycle: str
    progress: customer_status.CustomerProgress

    @property
    def lifecycle_label(self) -> str:
        return LIFECYCLE_LABELS[self.lifecycle]

    @property
    def people(self) -> int:
        return self.progress.total

    @property
    def is_paused(self) -> bool:
        return self.lifecycle == LIFECYCLE_PAUSED

    @property
    def is_archived(self) -> bool:
        return self.lifecycle == LIFECYCLE_ARCHIVED

    @property
    def is_draft(self) -> bool:
        return self.lifecycle == LIFECYCLE_DRAFT


def header(session: Session, campaign: Campaign) -> CampaignHeader:
    return CampaignHeader(
        campaign=campaign,
        lifecycle=lifecycle(campaign),
        progress=customer_status.progress(session, campaign_id=campaign.id),
    )


def happening_now(header_: CampaignHeader) -> str:
    """One plain sentence about the Campaign, said as fact."""

    progress = header_.progress
    if header_.is_archived:
        return "This Campaign is archived. Nothing more will be prepared."
    if progress.total == 0:
        return "Nobody has been added yet. VMR starts the moment somebody is."
    if header_.is_draft:
        return "This Campaign has not been started, so nothing is being prepared."
    if header_.is_paused:
        return "This Campaign is paused. Nothing new is being prepared."
    if progress.processing:
        word = "person" if progress.processing == 1 else "people"
        return f"VMR is preparing {progress.processing:,} {word}."
    if progress.ready_for_sending:
        return "Everyone who could be prepared is ready."
    return "Nothing is being prepared right now."


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------

OUTCOME_FILTERS: tuple[tuple[str, str], ...] = (
    ("all", "All"),
    (CustomerContactStatus.PROCESSING.value, "Processing"),
    (CustomerContactStatus.READY_FOR_SENDING.value, "Ready for Sending"),
    (CustomerContactStatus.COULD_NOT_PREPARE.value, "Could not prepare"),
)


@dataclass(frozen=True)
class PersonRow:
    """One Campaign member, in the customer's words."""

    membership_id: uuid.UUID
    contact_id: uuid.UUID
    name: str
    company: str | None
    email: str | None
    outcome: CustomerContactStatus
    detail: str
    updated_at: datetime | None
    added_at: datetime | None
    #: When the current seven-email package was written; ready people only.
    ready_at: datetime | None = None

    @property
    def outcome_label(self) -> str:
        return customer_status.STATUS_LABELS[self.outcome]

    @property
    def ready(self) -> bool:
        return self.outcome is CustomerContactStatus.READY_FOR_SENDING


def _stopped_stages(
    session: Session, membership_ids: list[uuid.UUID]
) -> dict[uuid.UUID, AgentIdentifier]:
    """The stage each stopped membership stopped on, from the stage ledger."""

    if not membership_ids:
        return {}
    rows = session.execute(
        select(
            CampaignContactAgentState.campaign_contact_id,
            CampaignContactAgentState.agent_id,
        )
        .where(
            CampaignContactAgentState.campaign_contact_id.in_(membership_ids),
            or_(
                and_(
                    CampaignContactAgentState.status == PipelineStageStatus.FAILED,
                    CampaignContactAgentState.retryable.is_(False),
                ),
                CampaignContactAgentState.status == PipelineStageStatus.BLOCKED,
            ),
        )
        .order_by(CampaignContactAgentState.updated_at.desc())
    ).all()
    stopped: dict[uuid.UUID, AgentIdentifier] = {}
    for membership_id, agent_id in rows:
        stopped.setdefault(membership_id, agent_id)
    return stopped


def _people_statement(
    campaign_id: uuid.UUID, *, outcome: str | None, search: str | None
) -> Select[Any]:
    expression = customer_status.status_expression()
    statement = (
        select(CampaignContact, Contact, expression.label("outcome"))
        .join(Contact, Contact.id == CampaignContact.contact_id)
        .where(CampaignContact.campaign_id == campaign_id)
    )
    if outcome and outcome != "all":
        statement = statement.where(expression == outcome)
    if search:
        needle = f"%{search.strip()}%"
        statement = statement.where(
            or_(
                Contact.first_name.ilike(needle),
                Contact.last_name.ilike(needle),
                Contact.company_name.ilike(needle),
                Contact.email.ilike(needle),
            )
        )
    return statement


def people(
    session: Session,
    *,
    campaign_id: uuid.UUID,
    outcome: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[PersonRow], int]:
    """A page of Campaign members with their outcome, plus the total."""

    statement = _people_statement(campaign_id, outcome=outcome, search=search)
    total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = session.execute(
        statement.order_by(CampaignContact.updated_at.desc(), CampaignContact.id)
        .limit(max(0, limit))
        .offset(max(0, offset))
    ).all()
    return _rows(session, rows), total


def ready_people(session: Session, *, campaign_id: uuid.UUID, limit: int = 200) -> list[PersonRow]:
    """Only people whose current package satisfies Ready for Sending."""

    rows, _total = people(
        session,
        campaign_id=campaign_id,
        outcome=CustomerContactStatus.READY_FOR_SENDING.value,
        limit=limit,
    )
    return rows


def _ready_since(session: Session, membership_ids: list[uuid.UUID]) -> dict[uuid.UUID, datetime]:
    if not membership_ids:
        return {}
    rows = session.execute(
        select(EmailSequence.campaign_contact_id, EmailSequence.created_at).where(
            EmailSequence.campaign_contact_id.in_(membership_ids),
            EmailSequence.superseded_at.is_(None),
        )
    ).all()
    return {membership_id: created_at for membership_id, created_at in rows}


def _rows(session: Session, rows: list[Any]) -> list[PersonRow]:
    stopped_ids = [
        membership.id
        for membership, _contact, outcome in rows
        if outcome == CustomerContactStatus.COULD_NOT_PREPARE.value
    ]
    stopped_at = _stopped_stages(session, stopped_ids)
    ready_ids = [
        membership.id
        for membership, _contact, outcome in rows
        if outcome == CustomerContactStatus.READY_FOR_SENDING.value
    ]
    ready_since = _ready_since(session, ready_ids)
    built: list[PersonRow] = []
    for membership, contact, raw_outcome in rows:
        outcome = CustomerContactStatus(raw_outcome)
        if outcome is CustomerContactStatus.READY_FOR_SENDING:
            detail = DETAIL_READY
        elif outcome is CustomerContactStatus.PROCESSING:
            detail = _processing_detail(membership)
        else:
            detail = _stopped_detail(membership, stopped_at.get(membership.id))
        built.append(
            PersonRow(
                membership_id=membership.id,
                contact_id=membership.contact_id,
                name=_person_name(contact),
                company=_company_name(contact),
                email=contact.email if contact is not None else None,
                outcome=outcome,
                detail=detail,
                updated_at=membership.updated_at,
                added_at=membership.enrolled_at,
                ready_at=ready_since.get(membership.id),
            )
        )
    return built


@dataclass(frozen=True)
class ReasonCount:
    reason: str
    count: int


def could_not_prepare_reasons(
    session: Session, *, campaign_id: uuid.UUID, limit: int = 3
) -> list[ReasonCount]:
    """The top plain-language reasons people could not be prepared."""

    rows, _total = people(
        session,
        campaign_id=campaign_id,
        outcome=CustomerContactStatus.COULD_NOT_PREPARE.value,
        limit=1000,
    )
    tally: dict[str, int] = {}
    for row in rows:
        tally[row.detail] = tally.get(row.detail, 0) + 1
    ranked = sorted(tally.items(), key=lambda item: (-item[1], item[0]))
    return [ReasonCount(reason=reason, count=count) for reason, count in ranked[:limit]]


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActivityLine:
    """One meaningful Campaign event, in customer language."""

    at: datetime
    text: str
    meta: str = ""


#: Audit actions worth telling the customer about, and how to say them.
_AUDIT_TEXT: dict[str, str] = {
    "campaign.created": "Campaign created",
    "campaign.updated": "Setup changed",
    "campaign.execution_enabled": "Preparation started",
    "campaign.execution_disabled": "Preparation paused",
    "campaign.status_changed": "Lifecycle changed",
    "campaign.archived": "Campaign archived",
    "campaign.offering_linked": "Offering attached",
    "campaign.offering_unlinked": "Offering removed",
    "campaign.user_assigned": "Access given",
    "campaign.user_unassigned": "Access removed",
}


def activity(session: Session, *, campaign_id: uuid.UUID, limit: int = 30) -> list[ActivityLine]:
    """Lifecycle, setup and access changes plus people added, newest first.

    Worker starts, queue transitions, retries and reason codes are deliberately
    absent: they are Admin diagnostics, not Campaign history.
    """

    lines: list[ActivityLine] = []

    audit_rows = session.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.entity_type == "campaign",
            AuditEvent.entity_id == str(campaign_id),
        )
        .order_by(AuditEvent.created_at.desc())
        .limit(limit)
    ).all()
    for event in audit_rows:
        text = _AUDIT_TEXT.get(event.action)
        if text is None:
            continue
        meta_parts = []
        if event.actor and event.actor not in ("system", "operator"):
            meta_parts.append(f"by {safe_text(event.actor)}")
        if event.reason and event.action not in ("campaign.execution_enabled",):
            meta_parts.append(safe_text(event.reason))
        lines.append(ActivityLine(at=event.created_at, text=text, meta=" · ".join(meta_parts)))

    added_day = func.date_trunc(literal("day"), CampaignContact.enrolled_at)
    added = session.execute(
        select(
            CampaignContact.source_kind,
            CampaignContact.source_batch_id,
            added_day,
            func.count(CampaignContact.id),
            func.max(CampaignContact.enrolled_at),
        )
        .where(CampaignContact.campaign_id == campaign_id)
        .group_by(
            CampaignContact.source_kind,
            CampaignContact.source_batch_id,
            added_day,
        )
        .order_by(func.max(CampaignContact.enrolled_at).desc())
        .limit(limit)
    ).all()
    for source_kind, _batch_id, _day, count, latest in added:
        word = "person" if count == 1 else "people"
        lines.append(
            ActivityLine(
                at=latest,
                text=f"{count:,} {word} added",
                meta=_source_label(source_kind),
            )
        )

    ready_day = func.date_trunc(literal("day"), EmailSequence.created_at)
    ready = session.execute(
        select(
            ready_day,
            func.count(EmailSequence.id),
            func.max(EmailSequence.created_at),
        )
        .where(
            EmailSequence.campaign_id == campaign_id,
            EmailSequence.superseded_at.is_(None),
        )
        .group_by(ready_day)
        .order_by(func.max(EmailSequence.created_at).desc())
        .limit(limit)
    ).all()
    for _day, count, latest in ready:
        word = "person" if count == 1 else "people"
        lines.append(ActivityLine(at=latest, text=f"Emails written for {count:,} {word}"))

    lines.sort(key=lambda line: line.at, reverse=True)
    return lines[:limit]


_SOURCE_LABELS: dict[str, str] = {
    "capture": "from the Chrome extension",
    "extension": "from the Chrome extension",
    "sheets": "from Google Sheets",
    "google_sheets": "from Google Sheets",
    "import": "from a file",
    "csv": "from a file",
    "file": "from a file",
    "manual": "chosen from People",
}


def _source_label(source_kind: str | None) -> str:
    if not source_kind:
        return ""
    return _SOURCE_LABELS.get(source_kind, f"from {source_kind.replace('_', ' ')}")


# ---------------------------------------------------------------------------
# Campaign list
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CampaignListRow:
    campaign: Campaign
    lifecycle: str
    progress: customer_status.CustomerProgress
    last_change: datetime | None

    @property
    def lifecycle_label(self) -> str:
        return LIFECYCLE_LABELS[self.lifecycle]


def list_rows(session: Session, campaigns: list[Campaign]) -> list[CampaignListRow]:
    if not campaigns:
        return []
    ids = [campaign.id for campaign in campaigns]
    latest = dict(
        session.execute(
            select(CampaignContact.campaign_id, func.max(CampaignContact.updated_at))
            .where(CampaignContact.campaign_id.in_(ids))
            .group_by(CampaignContact.campaign_id)
        ).all()
    )
    rows: list[CampaignListRow] = []
    for campaign in campaigns:
        changed = latest.get(campaign.id)
        last = max(filter(None, (changed, campaign.updated_at)), default=None)
        rows.append(
            CampaignListRow(
                campaign=campaign,
                lifecycle=lifecycle(campaign),
                progress=customer_status.progress(session, campaign_id=campaign.id),
                last_change=last,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Imports (Add people history)
# ---------------------------------------------------------------------------


def import_batches(
    session: Session, *, campaign_id: uuid.UUID, limit: int = 20
) -> list[ImportBatch]:
    return list(
        session.scalars(
            select(ImportBatch)
            .where(ImportBatch.campaign_id == campaign_id)
            .order_by(ImportBatch.created_at.desc())
            .limit(limit)
        ).all()
    )


__all__ = [
    "ActivityLine",
    "CampaignHeader",
    "CampaignListRow",
    "LIFECYCLE_ACTIVE",
    "LIFECYCLE_ARCHIVED",
    "LIFECYCLE_DRAFT",
    "LIFECYCLE_LABELS",
    "LIFECYCLE_PAUSED",
    "OUTCOME_FILTERS",
    "PersonRow",
    "ReasonCount",
    "activity",
    "could_not_prepare_reasons",
    "happening_now",
    "header",
    "import_batches",
    "lifecycle",
    "list_rows",
    "people",
    "ready_people",
]

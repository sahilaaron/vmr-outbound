"""Phase 2 Campaign, Collection, audience, Agent, and pipeline APIs.

Routes are intentionally thin transaction adapters. Domain validation,
idempotency, safety gates, state projection, and audit writes live in services.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.auth.csrf import require_csrf
from app.core.config import get_settings
from app.models.agent import AgentControl
from app.models.campaign import Campaign, CampaignContact
from app.models.collection import Collection
from app.models.contact import Contact
from app.models.email_discovery import EmailCandidateAttempt
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    CampaignContactEligibility,
    CampaignMembershipStatus,
    CampaignStatus,
    ContactWorkflowState,
    PipelineStageStatus,
)
from app.models.verification_job import AgentJob
from app.services import campaign_contacts, campaigns, collections, pipeline
from app.services.agents import controls, jobs
from app.services.agents.orchestrator import reconcile_agent_control
from app.services.agents.registry import AGENT_SPECS

# Every state-changing route on this router is refused unless the request
# carries the CSRF token bound to the caller's session. The check is declared
# once, here, rather than on ~100 individual handlers: a route added later is
# covered the moment it is registered. It is inert for safe methods and inert
# entirely when hosted authentication is disabled (local development).
router = APIRouter(prefix="/api", tags=["phase-2"], dependencies=[Depends(require_csrf)])
DbSession = Annotated[Session, Depends(get_db)]
PageLimit = Annotated[int, Query(ge=1, le=500)]
PageOffset = Annotated[int, Query(ge=0)]
EventLimit = Annotated[int, Query(ge=1, le=1_000)]


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    status: CampaignStatus = CampaignStatus.DRAFT
    sender_context: dict[str, Any] | None = None
    target_audience: dict[str, Any] | None = None
    messaging_direction: str | None = None
    primary_cta: str | None = None
    template_config: dict[str, Any] | None = None
    cadence_config: dict[str, Any] | None = None
    sending_settings: dict[str, Any] | None = None


class CampaignPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    status: CampaignStatus | None = None
    sender_context: dict[str, Any] | None = None
    target_audience: dict[str, Any] | None = None
    messaging_direction: str | None = None
    primary_cta: str | None = None
    template_config: dict[str, Any] | None = None
    cadence_config: dict[str, Any] | None = None
    sending_settings: dict[str, Any] | None = None
    reason: str | None = Field(default=None, max_length=2_000)


class ExecutionUpdate(BaseModel):
    enabled: bool
    reason: str | None = Field(default=None, max_length=2_000)


class CollectionWrite(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=2_000)


class CollectionAssociation(BaseModel):
    role: str = Field(default="audience", min_length=1, max_length=32)


class EnrollmentWrite(BaseModel):
    source_type: str = Field(default="manual", min_length=1, max_length=64)
    source_reference: str | None = Field(default=None, max_length=512)
    source_context: dict[str, Any] | None = None
    capture_id: uuid.UUID | None = None
    import_batch_id: uuid.UUID | None = None
    collection_id: uuid.UUID | None = None
    idempotency_key: str | None = Field(default=None, max_length=512)
    desired_stage: AgentIdentifier = AgentIdentifier.SENDING


class ActionReason(BaseModel):
    reason: str | None = Field(default=None, max_length=2_000)


class RequiredActionReason(BaseModel):
    reason: str = Field(min_length=1, max_length=2_000)


class AgentControlWrite(BaseModel):
    status: AgentControlStatus
    config: dict[str, Any] | None = None
    reason: str | None = Field(default=None, max_length=2_000)


def _raise_service_error(exc: Exception) -> None:
    not_found = isinstance(
        exc,
        (
            campaigns.CampaignNotFound,
            campaign_contacts.CampaignContactNotFound,
            collections.CollectionNotFound,
            jobs.AgentJobNotFound,
        ),
    )
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND if not_found else status.HTTP_409_CONFLICT,
        detail=str(exc),
    ) from exc


def _campaign(campaign: Campaign) -> dict[str, Any]:
    return {
        "id": campaign.id,
        "name": campaign.name,
        "description": campaign.description,
        "status": campaign.status.value,
        "execution_enabled": campaign.execution_enabled,
        "settings_version": campaign.settings_version,
        "sender_context": campaign.sender_context,
        "target_audience": campaign.target_audience,
        "messaging_direction": campaign.messaging_direction,
        "primary_cta": campaign.primary_cta,
        "template_config": campaign.template_config,
        "cadence_config": campaign.cadence_config,
        "sending_settings": campaign.sending_settings,
        "enabled_at": campaign.enabled_at,
        "disabled_at": campaign.disabled_at,
        "disabled_reason": campaign.disabled_reason,
        "created_at": campaign.created_at,
        "updated_at": campaign.updated_at,
    }


def _collection(collection: Collection) -> dict[str, Any]:
    return {
        "id": collection.id,
        "slug": collection.slug,
        "name": collection.name,
        "description": collection.description,
        "created_by": collection.created_by,
        "created_at": collection.created_at,
        "updated_at": collection.updated_at,
    }


def _membership(
    membership: CampaignContact,
    contact: Contact | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": membership.id,
        "campaign_id": membership.campaign_id,
        "contact_id": membership.contact_id,
        "membership_status": membership.membership_status.value,
        "legacy_workflow_state": membership.state.value,
        "eligibility_status": membership.eligibility_status.value,
        "blocking_reasons": membership.blocking_reasons,
        "qualification_state": membership.qualification_state,
        "review_state": membership.review_state,
        "sending_state": membership.sending_state,
        "provider_state": membership.provider_state,
        "desired_stage": membership.desired_stage.value,
        "current_stage": membership.current_stage.value if membership.current_stage else None,
        "latest_completed_stage": (
            membership.latest_completed_stage.value if membership.latest_completed_stage else None
        ),
        "next_stage": membership.next_stage.value if membership.next_stage else None,
        "pipeline_status": membership.pipeline_status.value,
        "source_kind": membership.source_kind,
        "source_reference": membership.source_reference,
        "source_capture_id": membership.source_capture_id,
        "source_batch_id": membership.source_batch_id,
        "enrolled_by": membership.enrolled_by,
        "enrolled_at": membership.enrolled_at,
        "archived_at": membership.archived_at,
        "created_at": membership.created_at,
        "updated_at": membership.updated_at,
    }
    if contact is not None:
        value["contact"] = {
            "id": contact.id,
            "first_name": contact.first_name,
            "last_name": contact.last_name,
            "title": contact.title,
            "company_name": contact.company_name,
            "company_domain": contact.company_domain,
            "email": contact.email,
        }
    return value


def _job(job: AgentJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "agent_id": job.agent_id.value,
        "task_kind": job.task_kind,
        "status": jobs.public_status(job),
        "stored_status": job.status.value,
        "priority": job.priority,
        "campaign_id": job.campaign_id,
        "campaign_contact_id": job.campaign_contact_id,
        "contact_id": job.contact_id,
        "company_id": job.company_id,
        "capture_id": job.capture_id,
        "entity_type": job.entity_type,
        "entity_id": job.entity_id,
        "email": job.email,
        "policy_version": job.policy_version,
        "idempotency_key": job.idempotency_key,
        "attempt_count": job.attempts,
        "max_attempts": job.max_attempts,
        "next_run_at": job.next_run_at,
        "lease_owner": job.lease_owner,
        "lease_expires_at": job.lease_expires_at,
        "parent_job_id": job.parent_job_id,
        "input_reference": job.input_reference,
        "result": job.result,
        "error": job.error,
        "error_class": job.error_class,
        "last_error": job.last_error,
        "outcome_status": job.outcome_status,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "updated_at": job.updated_at,
    }


def _email_attempt(attempt: EmailCandidateAttempt) -> dict[str, Any]:
    """Provider-neutral Email attempt projection for API and Workbench readers."""

    return {
        "id": attempt.id,
        "email_job_id": attempt.email_job_id,
        "candidate_id": attempt.candidate_id,
        "contact_id": attempt.contact_id,
        "company_id": attempt.company_id,
        "campaign_id": attempt.campaign_id,
        "campaign_contact_id": attempt.campaign_contact_id,
        "candidate_index": attempt.candidate_index,
        "candidate_format": attempt.candidate_format,
        "normalized_email": attempt.normalized_email,
        "normalized_domain": attempt.normalized_domain,
        "policy_identifier": attempt.policy_identifier,
        "policy_version": attempt.policy_version,
        "employee_count_class": attempt.employee_count_class,
        "employee_evidence_id": attempt.employee_evidence_id,
        "employee_evidence_reference": attempt.employee_evidence_reference,
        "employee_evidence_at": attempt.employee_evidence_at,
        "employee_evidence_freshness": attempt.employee_evidence_freshness,
        "force_refresh": attempt.force_refresh,
        "refresh_scope": attempt.refresh_scope,
        "status": attempt.status,
        "verification_job_id": attempt.verification_job_id,
        "verification_id": attempt.verification_id,
        "verification_decision": attempt.verification_decision,
        "verification_result": attempt.verification_result,
        "refusal_reason": attempt.refusal_reason,
        "verification_queued_at": attempt.verification_queued_at,
        "resolved_at": attempt.resolved_at,
        "created_at": attempt.created_at,
        "updated_at": attempt.updated_at,
    }


@router.get("/campaigns")
def list_campaigns(
    request: Request,
    response: Response,
    db: DbSession,
    include_archived: bool = False,
) -> dict[str, Any]:
    """List Campaigns for both the operating API and the local extension.

    Browser-origin requests are treated as the extension's local selector and
    retain its feature, environment, and CORS safety gates. Server-side/no-origin
    requests use the canonical Phase 2 operating API without those acquisition
    gates. One route therefore owns the resource and OpenAPI contract.
    """

    origin = request.headers.get("origin")
    extension_request = origin is not None
    if origin is not None:
        settings = get_settings()
        parsed_origin = urlsplit(origin)
        allowed_origin = parsed_origin.scheme == "chrome-extension" or (
            parsed_origin.scheme in {"http", "https"}
            and parsed_origin.hostname in {"127.0.0.1", "localhost", "::1"}
        )
        if not (settings.features.salesnav_intake or settings.features.contact_capture_intake):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
        if settings.app_env.lower() != "local" or not allowed_origin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="unauthorized")
        response.headers["Access-Control-Allow-Origin"] = origin

    overviews = campaigns.list_campaigns(db)
    rows = []
    for overview in overviews:
        campaign = overview.campaign
        if extension_request and campaign.status not in (
            CampaignStatus.DRAFT,
            CampaignStatus.ACTIVE,
        ):
            continue
        if (
            not extension_request
            and not include_archived
            and campaign.status is CampaignStatus.ARCHIVED
        ):
            continue
        rows.append(
            {
                **_campaign(campaign),
                "contact_count": overview.contact_count,
                "import_count": overview.import_count,
            }
        )
    return {"campaigns": rows}


@router.post("/campaigns", status_code=status.HTTP_201_CREATED)
def create_campaign(payload: CampaignCreate, db: DbSession) -> dict[str, Any]:
    try:
        campaign = campaigns.create_campaign(db, **payload.model_dump())
    except campaigns.CampaignError as exc:
        _raise_service_error(exc)
    return _campaign(campaign)


@router.patch("/campaigns/{campaign_id}")
def update_campaign(
    campaign_id: uuid.UUID,
    payload: CampaignPatch,
    db: DbSession,
) -> dict[str, Any]:
    fields = payload.model_fields_set - {"reason"}
    changes = {name: getattr(payload, name) for name in fields}
    if changes.get("name") is None and "name" in fields:
        raise HTTPException(status_code=422, detail="name cannot be null")
    if changes.get("status") is None and "status" in fields:
        raise HTTPException(status_code=422, detail="status cannot be null")
    try:
        campaign = campaigns.update_campaign(
            db,
            campaign_id,
            **changes,
            reason=payload.reason,
        )
    except campaigns.CampaignError as exc:
        _raise_service_error(exc)
    return _campaign(campaign)


@router.post("/campaigns/{campaign_id}/execution")
def update_campaign_execution(
    campaign_id: uuid.UUID,
    payload: ExecutionUpdate,
    db: DbSession,
) -> dict[str, Any]:
    try:
        campaign = campaigns.apply_campaign_execution(
            db,
            campaign_id,
            enabled=payload.enabled,
            reason=payload.reason,
        )
    except campaigns.CampaignError as exc:
        _raise_service_error(exc)
    return _campaign(campaign)


@router.get("/campaigns/{campaign_id}/operating-state")
def campaign_operating_state(
    campaign_id: uuid.UUID,
    db: DbSession,
) -> dict[str, Any]:
    state = campaigns.campaign_operating_state(db, campaign_id)
    if state is None:
        raise HTTPException(status_code=404, detail="campaign does not exist")
    overview = campaigns.get_campaign_overview(db, campaign_id)
    assert overview is not None
    return {
        "campaign": _campaign(state.campaign),
        "offering_ids": state.offering_ids,
        "agent_controls": state.agent_controls,
        "audience": {
            "contact_count": overview.contact_count,
            "import_count": overview.import_count,
            "legacy_state_counts": overview.state_counts,
            "pipeline_counts": overview.pipeline_counts,
        },
    }


@router.get("/collections")
def list_collections(
    db: DbSession,
    campaign_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    summaries = collections.list_collections(db, campaign_id=campaign_id)
    return {
        "collections": [
            {
                **_collection(summary.collection),
                "contact_count": summary.contact_count,
                "pending_capture_count": summary.pending_capture_count,
                "campaign_ids": summary.campaign_ids,
            }
            for summary in summaries
        ]
    }


@router.post("/collections")
def create_collection(
    payload: CollectionWrite,
    db: DbSession,
) -> dict[str, Any]:
    try:
        collection, created = collections.create_collection(db, **payload.model_dump())
    except collections.CollectionError as exc:
        _raise_service_error(exc)
    return {"collection": _collection(collection), "created": created}


@router.patch("/collections/{collection_id}")
def update_collection(
    collection_id: uuid.UUID,
    payload: CollectionWrite,
    db: DbSession,
) -> dict[str, Any]:
    try:
        collection = collections.rename_collection(
            db,
            collection_id,
            **payload.model_dump(),
        )
    except collections.CollectionError as exc:
        _raise_service_error(exc)
    return _collection(collection)


@router.put("/collections/{collection_id}/contacts/{contact_id}")
def add_collection_contact(
    collection_id: uuid.UUID,
    contact_id: uuid.UUID,
    db: DbSession,
) -> dict[str, Any]:
    try:
        membership, created = collections.assign_contact(
            db,
            collection_id=collection_id,
            contact_id=contact_id,
        )
    except collections.CollectionError as exc:
        _raise_service_error(exc)
    return {"membership_id": membership.id, "created": created}


@router.delete("/collections/{collection_id}/contacts/{contact_id}")
def remove_collection_contact(
    collection_id: uuid.UUID,
    contact_id: uuid.UUID,
    db: DbSession,
) -> dict[str, Any]:
    return {
        "removed": collections.remove_contact(
            db,
            collection_id=collection_id,
            contact_id=contact_id,
        )
    }


@router.put("/campaigns/{campaign_id}/collections/{collection_id}")
def associate_campaign_collection(
    campaign_id: uuid.UUID,
    collection_id: uuid.UUID,
    payload: CollectionAssociation,
    db: DbSession,
) -> dict[str, Any]:
    try:
        link, created = collections.associate_campaign(
            db,
            campaign_id=campaign_id,
            collection_id=collection_id,
            role=payload.role,
        )
    except collections.CollectionError as exc:
        _raise_service_error(exc)
    return {
        "association_id": link.id,
        "campaign_id": link.campaign_id,
        "collection_id": link.collection_id,
        "role": link.association_role,
        "created": created,
    }


@router.delete("/campaigns/{campaign_id}/collections/{collection_id}")
def dissociate_campaign_collection(
    campaign_id: uuid.UUID,
    collection_id: uuid.UUID,
    db: DbSession,
) -> dict[str, Any]:
    return {
        "removed": collections.dissociate_campaign(
            db,
            campaign_id=campaign_id,
            collection_id=collection_id,
        )
    }


@router.post("/campaigns/{campaign_id}/contacts/{contact_id}")
def enrol_campaign_contact(
    campaign_id: uuid.UUID,
    contact_id: uuid.UUID,
    payload: EnrollmentWrite,
    db: DbSession,
) -> dict[str, Any]:
    try:
        result = campaign_contacts.enrol_contact(
            db,
            campaign_id=campaign_id,
            contact_id=contact_id,
            **payload.model_dump(),
        )
    except campaign_contacts.CampaignContactError as exc:
        _raise_service_error(exc)
    return {
        "campaign_contact": _membership(result.membership),
        "created": result.created,
        "source_created": result.source_created,
        "queued_job": _job(result.queued_job) if result.queued_job else None,
    }


@router.get("/campaigns/{campaign_id}/contacts")
def list_campaign_contacts(
    campaign_id: uuid.UUID,
    db: DbSession,
    membership_status: CampaignMembershipStatus | None = None,
    pipeline_status: PipelineStageStatus | None = None,
    eligibility_status: CampaignContactEligibility | None = None,
    legacy_state: ContactWorkflowState | None = None,
    limit: PageLimit = 50,
    offset: PageOffset = 0,
) -> dict[str, Any]:
    if campaigns.get_campaign(db, campaign_id) is None:
        raise HTTPException(status_code=404, detail="campaign does not exist")
    rows, total = campaigns.campaign_members(
        db,
        campaign_id,
        state=legacy_state,
        membership_status=membership_status,
        pipeline_status=pipeline_status,
        eligibility_status=eligibility_status,
        limit=limit,
        offset=offset,
    )
    return {
        "campaign_id": campaign_id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "contacts": [_membership(membership, contact) for membership, contact in rows],
    }


@router.get("/campaign-contacts/{campaign_contact_id}")
def get_campaign_contact(
    campaign_contact_id: uuid.UUID,
    db: DbSession,
) -> dict[str, Any]:
    membership = db.get(CampaignContact, campaign_contact_id)
    if membership is None:
        raise HTTPException(status_code=404, detail="campaign contact does not exist")
    return _membership(membership, db.get(Contact, membership.contact_id))


@router.post("/campaign-contacts/{campaign_contact_id}/pause")
def pause_campaign_contact(
    campaign_contact_id: uuid.UUID,
    payload: ActionReason,
    db: DbSession,
) -> dict[str, Any]:
    try:
        membership = campaign_contacts.pause_membership(
            db,
            campaign_contact_id=campaign_contact_id,
            reason=payload.reason or "paused by operator",
        )
    except campaign_contacts.CampaignContactError as exc:
        _raise_service_error(exc)
    return _membership(membership)


@router.post("/campaign-contacts/{campaign_contact_id}/resume")
def resume_campaign_contact(
    campaign_contact_id: uuid.UUID,
    payload: ActionReason,
    db: DbSession,
) -> dict[str, Any]:
    try:
        membership = campaign_contacts.resume_membership(
            db,
            campaign_contact_id=campaign_contact_id,
            reason=payload.reason or "resumed by operator",
        )
    except campaign_contacts.CampaignContactError as exc:
        _raise_service_error(exc)
    return _membership(membership)


@router.post("/campaign-contacts/{campaign_contact_id}/archive")
def archive_campaign_contact(
    campaign_contact_id: uuid.UUID,
    payload: ActionReason,
    db: DbSession,
) -> dict[str, Any]:
    try:
        membership = campaign_contacts.archive_membership(
            db,
            campaign_contact_id=campaign_contact_id,
            reason=payload.reason or "removed from Campaign by operator",
        )
    except campaign_contacts.CampaignContactError as exc:
        _raise_service_error(exc)
    return _membership(membership)


@router.get("/campaign-contacts/{campaign_contact_id}/pipeline")
def get_campaign_contact_pipeline(
    campaign_contact_id: uuid.UUID,
    db: DbSession,
    event_limit: EventLimit = 200,
) -> dict[str, Any]:
    snapshot = pipeline.pipeline_snapshot(
        db,
        campaign_contact_id=campaign_contact_id,
        event_limit=event_limit,
    )
    if snapshot is None:
        raise HTTPException(status_code=404, detail="campaign contact does not exist")
    return {
        "campaign_contact": _membership(snapshot.membership),
        "next_action": snapshot.next_action,
        "stages": [
            {
                "agent_id": stage.agent_id.value,
                "status": stage.status.value,
                "attempt_count": stage.attempt_count,
                "latest_job_id": stage.latest_job_id,
                "reason_code": stage.reason_code,
                "reason_detail": stage.reason_detail,
                "retryable": stage.retryable,
                "waiting_on_agent": (
                    stage.waiting_on_agent.value if stage.waiting_on_agent else None
                ),
                "output_reference": stage.output_reference,
                "started_at": stage.started_at,
                "completed_at": stage.completed_at,
                "updated_at": stage.updated_at,
            }
            for stage in snapshot.stages
        ],
        "active_and_recent_jobs": [_job(job) for job in snapshot.jobs],
        "events": [
            {
                "id": event.id,
                "agent_id": event.agent_id.value if event.agent_id else None,
                "job_id": event.job_id,
                "event_type": event.event_type.value,
                "from_status": event.from_status.value if event.from_status else None,
                "to_status": event.to_status.value if event.to_status else None,
                "reason_code": event.reason_code,
                "reason_detail": event.reason_detail,
                "retryable": event.retryable,
                "detail": event.detail,
                "actor": event.actor,
                "occurred_at": event.occurred_at,
            }
            for event in snapshot.events
        ],
    }


@router.post("/campaign-contacts/{campaign_contact_id}/retry")
def retry_campaign_contact(
    campaign_contact_id: uuid.UUID,
    payload: ActionReason,
    db: DbSession,
) -> dict[str, Any]:
    try:
        job = campaign_contacts.retry_processing(
            db,
            campaign_contact_id=campaign_contact_id,
            reason=payload.reason or "operator requested retry",
        )
    except campaign_contacts.CampaignContactError as exc:
        _raise_service_error(exc)
    membership = db.get(CampaignContact, campaign_contact_id)
    assert membership is not None
    return {
        "campaign_contact": _membership(membership),
        "queued_job": _job(job) if job else None,
    }


@router.post("/campaign-contacts/{campaign_contact_id}/stages/{agent_id}/skip")
def skip_campaign_contact_stage(
    campaign_contact_id: uuid.UUID,
    agent_id: AgentIdentifier,
    payload: RequiredActionReason,
    db: DbSession,
) -> dict[str, Any]:
    membership = db.get(CampaignContact, campaign_contact_id)
    if membership is None:
        raise HTTPException(status_code=404, detail="campaign contact does not exist")
    try:
        stage = pipeline.skip_current_stage(
            db,
            membership=membership,
            agent_id=agent_id,
            reason=payload.reason,
        )
    except pipeline.PipelineStateError as exc:
        _raise_service_error(exc)
    return {
        "campaign_contact": _membership(membership),
        "stage": {
            "agent_id": stage.agent_id.value,
            "status": stage.status.value,
            "reason_code": stage.reason_code,
            "reason_detail": stage.reason_detail,
        },
    }


@router.get("/agents")
def list_agents(db: DbSession) -> dict[str, Any]:
    global_controls = {
        control.agent_id: control for control in db.scalars(select(AgentControl)).all()
    }
    return {
        "agents": [
            {
                "agent_id": spec.identifier.value,
                "display_name": spec.display_name,
                "position": spec.position,
                "dependencies": [dependency.value for dependency in spec.dependencies],
                "registry_default_status": spec.default_status.value,
                "implemented": spec.implemented,
                "skippable": spec.skippable,
                "max_attempts": spec.max_attempts,
                "global_control": (
                    {
                        "status": global_controls[spec.identifier].status.value,
                        "config": global_controls[spec.identifier].config,
                        "reason": global_controls[spec.identifier].reason,
                        "version": global_controls[spec.identifier].version,
                        "updated_by": global_controls[spec.identifier].updated_by,
                        "updated_at": global_controls[spec.identifier].updated_at,
                    }
                    if spec.identifier in global_controls
                    else None
                ),
                "configured_status": (
                    global_controls[spec.identifier].status.value
                    if spec.identifier in global_controls
                    else spec.default_status.value
                ),
            }
            for spec in AGENT_SPECS.values()
        ]
    }


@router.put("/agents/{agent_id}/control")
def update_global_agent_control(
    agent_id: AgentIdentifier,
    payload: AgentControlWrite,
    db: DbSession,
) -> dict[str, Any]:
    try:
        control = controls.set_global_control(
            db,
            agent_id=agent_id,
            **payload.model_dump(),
        )
        reconciled_jobs = reconcile_agent_control(db, agent_id=agent_id, actor="operator")
    except controls.AgentControlError as exc:
        _raise_service_error(exc)
    return {
        "agent_id": control.agent_id.value,
        "status": control.status.value,
        "config": control.config,
        "reason": control.reason,
        "version": control.version,
        "reconciled_jobs": reconciled_jobs,
    }


@router.put("/campaigns/{campaign_id}/agents/{agent_id}/override")
def update_campaign_agent_override(
    campaign_id: uuid.UUID,
    agent_id: AgentIdentifier,
    payload: AgentControlWrite,
    db: DbSession,
) -> dict[str, Any]:
    try:
        override = controls.set_campaign_override(
            db,
            campaign_id=campaign_id,
            agent_id=agent_id,
            **payload.model_dump(),
        )
        reconciled_jobs = reconcile_agent_control(
            db,
            campaign_id=campaign_id,
            agent_id=agent_id,
            actor="operator",
        )
    except controls.AgentControlError as exc:
        _raise_service_error(exc)
    return {
        "campaign_id": campaign_id,
        "agent_id": override.agent_id.value,
        "status": override.status.value,
        "config": override.config,
        "reason": override.reason,
        "version": override.version,
        "reconciled_jobs": reconciled_jobs,
    }


@router.delete("/campaigns/{campaign_id}/agents/{agent_id}/override")
def delete_campaign_agent_override(
    campaign_id: uuid.UUID,
    agent_id: AgentIdentifier,
    db: DbSession,
) -> dict[str, Any]:
    removed = controls.clear_campaign_override(
        db,
        campaign_id=campaign_id,
        agent_id=agent_id,
    )
    if removed:
        reconcile_agent_control(
            db,
            campaign_id=campaign_id,
            agent_id=agent_id,
            actor="operator",
        )
    return {"removed": removed}


@router.get("/agent-jobs/{job_id}")
def get_agent_job(job_id: uuid.UUID, db: DbSession) -> dict[str, Any]:
    job = db.get(AgentJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Agent job does not exist")
    return _job(job)


@router.get("/agent-jobs/{job_id}/email-attempts")
def get_email_agent_attempts(job_id: uuid.UUID, db: DbSession) -> dict[str, Any]:
    job = db.get(AgentJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Agent job does not exist")
    if job.agent_id is not AgentIdentifier.EMAIL:
        raise HTTPException(status_code=409, detail="Agent job is not an Email Agent execution")
    rows = list(
        db.scalars(
            select(EmailCandidateAttempt)
            .where(EmailCandidateAttempt.email_job_id == job.id)
            .order_by(EmailCandidateAttempt.candidate_index)
        ).all()
    )
    return {
        "job": _job(job),
        "attempts": [_email_attempt(row) for row in rows],
    }


@router.post("/agent-jobs/{job_id}/retry")
def retry_agent_job(job_id: uuid.UUID, db: DbSession) -> dict[str, Any]:
    try:
        job = jobs.retry_failed_job(db, job_id=job_id)
    except jobs.AgentJobError as exc:
        _raise_service_error(exc)
    return _job(job)

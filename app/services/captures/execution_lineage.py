"""Bounded durable lineage for synchronous Capture Agent executions.

Capture intake, import validation, promotion and Campaign filing are existing
authoritative write paths.  They do not need a second worker or workflow.  This
module records the outcome those paths already committed in the common Agent Job
envelope, using a small versioned projection instead of copying raw payloads.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignContact
from app.models.capture_promotion import ContactCapturePromotion
from app.models.collection import Collection, CollectionMembership
from app.models.contact import Contact
from app.models.contact_capture import ContactCaptureNote
from app.models.contact_field_value import ContactFieldValue
from app.models.enums import (
    AgentIdentifier,
    AgentJobStatus,
    ImportRowOutcome,
    LinkedInSnapshotOutcome,
)
from app.models.import_batch import ImportBatch, ImportRow, ImportRowError, ImportRowValidation
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.models.pipeline import (
    CampaignContactAgentState,
    CampaignContactSource,
    CaptureCampaignFiling,
)
from app.models.suppression import Suppression
from app.models.verification_job import AgentJob
from app.services.agent_studio.research_report import _safe_text, _safe_url
from app.services.agents import jobs as agent_jobs
from app.services.captures import promotion as capture_promotion
from app.services.imports import mapping as import_mapping
from app.services.imports import normalization as norm

SCHEMA_VERSION = "capture-agent-report/1"
INPUT_VERSION = "capture-agent-input/1"

_IMPORT_FIELDS = import_mapping.SYSTEM_FIELDS
_TEXT_LIMIT = 1_000
_NOTE_LIMIT = 500
_LIST_LIMIT = 50
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _text(value: object, *, limit: int = _TEXT_LIMIT) -> str | None:
    if not isinstance(value, str):
        return None
    safe = _safe_text(value, limit=limit)
    clean = " ".join(safe.split()) if safe else ""
    return clean[:limit] if clean else None


def _url(value: object) -> str | None:
    return _safe_url(value if isinstance(value, str) else None)


def _note_text(value: object) -> str | None:
    safe = _text(value, limit=_NOTE_LIMIT * 2)
    if safe is None:
        return None
    without_url_secrets = _URL_IN_TEXT.sub(
        lambda match: _url(match.group(0)) or "[unsafe URL]",
        safe,
    )
    return without_url_secrets[:_NOTE_LIMIT]


def _import_field(field: str, value: object) -> str | None:
    if field == "linkedin_url":
        return _url(value)
    if field == "source_reference" and isinstance(value, str):
        return (
            _url(value)
            if value.lstrip().lower().startswith(("http://", "https://"))
            else _text(value)
        )
    return _text(value)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _uuid(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None


def _safe_list(value: object, *, limit: int = 64) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    result: list[str] = []
    for item in value[:_LIST_LIMIT]:
        safe = _text(item, limit=limit)
        if safe:
            result.append(safe)
    return result


def _contact_snapshot(contact: Contact | None) -> dict[str, object]:
    if contact is None:
        return {
            "contact_id": None,
            "first_name": None,
            "last_name": None,
            "title": None,
            "company_name": None,
            "company_domain": None,
            "linkedin_url": None,
            "email": None,
            "company_id": None,
            "merged_into_id": None,
            "created_at": None,
        }
    return {
        "contact_id": str(contact.id),
        "first_name": _text(contact.first_name, limit=255),
        "last_name": _text(contact.last_name, limit=255),
        "title": _text(contact.title, limit=512),
        "company_name": _text(contact.company_name, limit=512),
        "company_domain": _text(contact.company_domain, limit=255),
        "linkedin_url": _url(contact.linkedin_url),
        "email": _text(contact.email, limit=320),
        "company_id": _uuid(contact.company_id),
        "merged_into_id": _uuid(contact.merged_into_id),
        "created_at": _iso(contact.created_at),
    }


def _label_names(
    session: Session, *, contact_id: uuid.UUID | None, capture_id: uuid.UUID | None
) -> list[str]:
    conditions = []
    if contact_id is not None:
        conditions.append(CollectionMembership.contact_id == contact_id)
    if capture_id is not None:
        conditions.append(CollectionMembership.capture_id == capture_id)
    if not conditions:
        return []
    rows = session.execute(
        select(Collection.name)
        .join(CollectionMembership, CollectionMembership.collection_id == Collection.id)
        .where(or_(*conditions))
        .order_by(Collection.name.asc())
        .limit(_LIST_LIMIT)
    ).scalars()
    return [name for name in (_text(item, limit=64) for item in rows) if name]


def _notes(session: Session, capture_id: uuid.UUID) -> dict[str, object]:
    rows = session.scalars(
        select(ContactCaptureNote)
        .where(ContactCaptureNote.capture_id == capture_id)
        .order_by(ContactCaptureNote.created_at.asc(), ContactCaptureNote.id.asc())
        .limit(20)
    ).all()
    latest = rows[-1] if rows else None
    return {
        "present": bool(rows),
        "count": len(rows),
        "scope": _text(latest.scope, limit=16) if latest else None,
        "content": _note_text(latest.note_text) if latest else None,
        "author": _text(latest.author, limit=128) if latest else None,
        "created_at": _iso(latest.created_at) if latest else None,
        "bounded": True,
    }


def _field_provenance(
    session: Session,
    *,
    contact_id: uuid.UUID | None,
    capture_id: uuid.UUID | None = None,
    import_row_id: uuid.UUID | None = None,
) -> list[dict[str, object]]:
    if contact_id is None:
        return []
    statement = select(ContactFieldValue).where(ContactFieldValue.contact_id == contact_id)
    if import_row_id is not None:
        statement = statement.where(ContactFieldValue.import_row_id == import_row_id)
    elif capture_id is not None:
        statement = statement.where(ContactFieldValue.source_reference == str(capture_id))
    else:
        return []
    rows = session.scalars(
        statement.order_by(ContactFieldValue.ingested_at.asc(), ContactFieldValue.id.asc()).limit(
            50
        )
    ).all()
    return [
        {
            "observation_id": str(row.id),
            "field": row.field_name,
            "value": _text(row.value, limit=1_000),
            "source_name": _text(row.source_name, limit=256),
            "source_reference": _text(row.source_reference, limit=512),
            "observed_at": _iso(row.observed_at),
            "ingested_at": _iso(row.ingested_at),
            "policy_version": _text(row.policy_version, limit=50),
            "winner_at_execution": row.is_current_winner,
        }
        for row in rows
    ]


def _identity_handoff(session: Session, membership: CampaignContact | None) -> dict[str, object]:
    if membership is None:
        return {"identity_job_id": None, "status": None, "reason": "no_campaign_membership"}
    state = session.scalars(
        select(CampaignContactAgentState).where(
            CampaignContactAgentState.campaign_contact_id == membership.id,
            CampaignContactAgentState.agent_id == AgentIdentifier.IDENTITY,
        )
    ).one_or_none()
    job = session.get(AgentJob, state.latest_job_id) if state and state.latest_job_id else None
    if job is not None and (
        job.agent_id is not AgentIdentifier.IDENTITY
        or job.campaign_contact_id != membership.id
        or job.campaign_id != membership.campaign_id
        or job.contact_id != membership.contact_id
    ):
        job = None
    return {
        "identity_job_id": _uuid(job.id) if job else None,
        "status": agent_jobs.public_status(job) if job else None,
        "reason": (
            "identity_is_next_person_resolution_authority"
            if job
            else "identity_state_or_job_not_persisted"
            if state is None
            else "identity_not_enqueued_or_membership_blocked"
        ),
    }


def _membership_source(
    session: Session,
    *,
    membership: CampaignContact | None,
    capture_id: uuid.UUID | None = None,
    import_batch_id: uuid.UUID | None = None,
) -> CampaignContactSource | None:
    if membership is None:
        return None
    statement = select(CampaignContactSource).where(
        CampaignContactSource.campaign_contact_id == membership.id
    )
    if capture_id is not None:
        statement = statement.where(CampaignContactSource.capture_id == capture_id)
    elif import_batch_id is not None:
        statement = statement.where(CampaignContactSource.import_batch_id == import_batch_id)
    return session.scalars(
        statement.order_by(CampaignContactSource.recorded_at.asc(), CampaignContactSource.id.asc())
    ).first()


def _membership_for(
    session: Session,
    *,
    campaign_id: uuid.UUID | None,
    contact_id: uuid.UUID | None,
    campaign_contact_id: uuid.UUID | None = None,
) -> CampaignContact | None:
    if campaign_contact_id is not None:
        row = session.get(CampaignContact, campaign_contact_id)
        if row is None:
            return None
        if campaign_id is not None and row.campaign_id != campaign_id:
            return None
        if contact_id is not None and row.contact_id != contact_id:
            return None
        return row
    if campaign_id is None or contact_id is None:
        return None
    return session.scalars(
        select(CampaignContact).where(
            CampaignContact.campaign_id == campaign_id,
            CampaignContact.contact_id == contact_id,
        )
    ).one_or_none()


def _filing_projection(
    session: Session,
    *,
    filing: CaptureCampaignFiling | None,
    membership: CampaignContact | None,
    source: CampaignContactSource | None,
    membership_created: bool | None = None,
) -> dict[str, object]:
    if filing is None:
        return {
            "requested": False,
            "requested_campaign_id": None,
            "campaign_id": None,
            "status": "not_requested",
            "reason": None,
            "attempts": 0,
            "attempted_at": None,
            "campaign_contact_id": None,
            "membership_created": False,
            "membership_reused": False,
            "membership_status": None,
            "eligibility_status": None,
            "pipeline_status": None,
            "next_stage": None,
            "source_record_id": None,
            "pipeline_enrolled": False,
        }
    if membership_created is None and membership is not None:
        membership_created = membership.source_capture_id == filing.capture_id
    return {
        "requested": True,
        "requested_campaign_id": str(filing.requested_campaign_id),
        "campaign_id": _uuid(filing.campaign_id),
        "status": filing.status.value,
        "reason": _text(filing.error_code, limit=96),
        "attempts": filing.attempts,
        "attempted_at": _iso(filing.applied_at or filing.updated_at),
        "campaign_contact_id": _uuid(filing.campaign_contact_id),
        "membership_created": membership_created,
        "membership_reused": (
            (not membership_created) if membership_created is not None and membership else None
        ),
        "membership_status": membership.membership_status.value if membership else None,
        "eligibility_status": membership.eligibility_status.value if membership else None,
        "pipeline_status": membership.pipeline_status.value if membership else None,
        "next_stage": membership.next_stage.value if membership and membership.next_stage else None,
        "source_record_id": _uuid(source.id) if source else None,
        "pipeline_enrolled": membership is not None,
    }


def _capture_suppression(
    session: Session,
    *,
    snapshot: LinkedInProfileSnapshot,
    contact: Contact | None,
    promotion: ContactCapturePromotion | None,
    filing: CaptureCampaignFiling | None,
    membership: CampaignContact | None,
) -> dict[str, object]:
    applied = snapshot.outcome is LinkedInSnapshotOutcome.SUPPRESSED
    detail = (
        promotion.detail if promotion is not None and isinstance(promotion.detail, dict) else {}
    )
    if promotion is not None and promotion.contact_outcome.value == "suppressed":
        applied = True
    hit = None
    if applied:
        from app.services.suppressions import find_active_suppression

        hit = find_active_suppression(
            session,
            email=contact.email if contact else None,
            domain=(
                promotion.resolved_domain
                if promotion is not None and promotion.resolved_domain
                else contact.company_domain
                if contact
                else None
            ),
        )
    reason = (
        hit.reason.value
        if hit is not None
        else _text(detail.get("suppression_reason"), limit=100)
        or _text((snapshot.refresh_summary or {}).get("suppression_reason"), limit=100)
    )
    return {
        "applied": applied,
        "dimension": hit.suppression_type.value if hit is not None else None,
        "reason": reason,
        "suppression_id": _uuid(hit.id) if hit is not None else None,
        "blocked_promotion": applied,
        "blocked_filing": applied and filing is not None and membership is None,
    }


def _capture_projection(
    session: Session,
    *,
    snapshot: LinkedInProfileSnapshot,
    actor: str,
) -> tuple[dict[str, object], Contact | None, CampaignContact | None, uuid.UUID | None]:
    promotion = capture_promotion.get_promotion(session, snapshot.id)
    contact_id = (
        promotion.promoted_contact_id
        if promotion is not None and promotion.promoted_contact_id is not None
        else snapshot.matched_contact_id
    )
    contact = session.get(Contact, contact_id) if contact_id else None
    filing = session.scalars(
        select(CaptureCampaignFiling).where(CaptureCampaignFiling.capture_id == snapshot.id)
    ).one_or_none()
    campaign_id = filing.campaign_id if filing else snapshot.campaign_id
    membership = (
        _membership_for(
            session,
            campaign_id=campaign_id,
            contact_id=contact_id,
            campaign_contact_id=filing.campaign_contact_id,
        )
        if filing is not None and filing.campaign_contact_id is not None
        else None
    )
    source = _membership_source(session, membership=membership, capture_id=snapshot.id)
    person = capture_promotion.person_identity(snapshot)
    employer = capture_promotion.company_hints(snapshot)
    refresh = snapshot.refresh_summary or {}
    promotion_detail = (
        promotion.detail if promotion is not None and isinstance(promotion.detail, dict) else {}
    )
    candidates = []
    for item in (snapshot.review_candidates or [])[:_LIST_LIMIT]:
        if isinstance(item, Mapping):
            candidate = item.get("contact_id")
            if isinstance(candidate, str):
                candidates.append(candidate)
    candidates.extend(_safe_list(promotion_detail.get("ambiguous_contact_ids"), limit=36))
    validation_outcome = {
        LinkedInSnapshotOutcome.AMBIGUOUS_REVIEW: "ambiguous",
        LinkedInSnapshotOutcome.DUPLICATE_IN_SUBMISSION: "duplicate",
        LinkedInSnapshotOutcome.SUPPRESSED: "suppressed",
        LinkedInSnapshotOutcome.UNMATCHED_STAGED: "pending",
        LinkedInSnapshotOutcome.STORED: "pending",
    }.get(snapshot.outcome, "accepted")
    if promotion is not None and promotion.contact_outcome.value == "contact_identity_ambiguous":
        validation_outcome = "ambiguous"
    source_duplicate = snapshot.outcome is LinkedInSnapshotOutcome.DUPLICATE_IN_SUBMISSION
    if promotion is not None:
        promotion_outcome = promotion.contact_outcome.value
        promoted = promotion.promoted_contact_id is not None
        created = promotion.contact_outcome.value == "contact_created"
        reused = promotion.contact_outcome.value in {
            "contact_exact_match_linked",
            "already_promoted",
        }
        promotion_reason = promotion.blocked_reason
        match_kind = _text(promotion_detail.get("match_kind"), limit=100)
        dedup_key = _text(promotion_detail.get("natural_key"), limit=512)
    else:
        promoted = contact is not None
        created = snapshot.outcome is LinkedInSnapshotOutcome.CONTACT_CREATED
        reused = contact is not None and not created
        promotion_outcome = (
            "contact_created" if created else "contact_reused" if reused else "not_promoted"
        )
        promotion_reason = _text(refresh.get("skipped_fields"), limit=1_000)
        match_kind = "exact_capture_identity" if reused else None
        dedup_key = None
    duplicate = (
        source_duplicate
        or reused
        or snapshot.outcome
        in {
            LinkedInSnapshotOutcome.EXACT_MATCH_REFRESHED,
            LinkedInSnapshotOutcome.EXACT_MATCH_UNCHANGED,
        }
    )
    if duplicate and contact_id is not None:
        candidates.append(str(contact_id))
    candidates = list(dict.fromkeys(candidates))[:_LIST_LIMIT]
    duplicate_match_type = (
        "in_submission_identity_key"
        if source_duplicate
        else match_kind or "exact_linkedin_identity"
        if duplicate
        else None
    )
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "lineage_complete": True,
        "source": {
            "type": "extension",
            "system": _text(snapshot.source, limit=128),
            "source_record_id": str(snapshot.id),
            "snapshot_id": str(snapshot.id),
            "submission_id": _uuid(snapshot.submission_id),
            "schema_version": _text(snapshot.schema_version, limit=64),
            "captured_at": _iso(snapshot.captured_at or snapshot.ingested_at),
            "source_url": _url(snapshot.source_url),
            "linkedin_profile_url": _url(snapshot.normalized_profile_url),
            "linkedin_public_identifier": _text(snapshot.public_identifier, limit=255),
            "linkedin_member_id": _text(snapshot.salesnav_member_id, limit=255),
            "linkedin_handle": _text(snapshot.public_identifier, limit=255),
            "capture_mode": _text(snapshot.capture_mode, limit=64),
            "source_surface": _text(snapshot.source_surface, limit=128),
            "actor": _text(actor, limit=128),
            "immutable": True,
        },
        "captured": {
            "person": {
                "first_name": _text(person.first_name, limit=255),
                "last_name": _text(person.last_name, limit=255),
                "full_name": _text(person.full_name, limit=512),
                "title": _text(person.title, limit=512),
                "email": None,
                "linkedin_url": _url(person.normalized_profile_url),
                "location": _text(
                    (snapshot.profile_fields or {}).get("displayed_location"), limit=512
                ),
            },
            "employer": {
                "name": _text(employer.name, limit=512),
                "domain": None,
                "linkedin_url": _url(employer.linkedin_url),
                "linkedin_id": _text(employer.linkedin_id, limit=255),
                "location": _text(employer.location, limit=512),
            },
            "normalized": {
                "profile_url": _url(snapshot.normalized_profile_url),
                "public_identifier": _text(snapshot.public_identifier, limit=255),
                "salesnav_member_id": _text(snapshot.salesnav_member_id, limit=255),
            },
            "labels": _label_names(session, contact_id=contact_id, capture_id=snapshot.id),
            "requested_labels": _safe_list(snapshot.operator_labels),
            "note": _notes(session, snapshot.id),
            "field_provenance": _field_provenance(
                session, contact_id=contact_id, capture_id=snapshot.id
            ),
        },
        "validation": {
            "outcome": validation_outcome,
            "stage": "extension_capture_intake",
            "reason_code": snapshot.outcome.value,
            "rejected_field": None,
            "retry_possible": validation_outcome in {"pending", "ambiguous"},
            "source_preserved": True,
        },
        "duplicate": {
            "applied": duplicate,
            "match_type": duplicate_match_type,
            "selected_contact_id": _uuid(contact_id) if duplicate else None,
            "candidate_contact_ids": candidates,
            "reason": (
                "same_person_already_captured_in_submission"
                if source_duplicate
                else "existing_permanent_contact_reused"
                if duplicate
                else None
            ),
            "no_new_contact": duplicate,
            "duplicate_of_capture_id": _uuid(snapshot.duplicate_of_id),
        },
        "suppression": _capture_suppression(
            session,
            snapshot=snapshot,
            contact=contact,
            promotion=promotion,
            filing=filing,
            membership=membership,
        ),
        "promotion": {
            "outcome": promotion_outcome,
            "promoted": promoted,
            "contact_created": created,
            "contact_reused": reused,
            "contact_id": _uuid(contact_id),
            "reason": _text(promotion_reason, limit=1_000),
            "idempotency_key": f"capture-promotion:{snapshot.id}",
            "deduplication_key": dedup_key,
            "match_type": match_kind,
            "source_to_contact": f"capture:{snapshot.id}",
            "decided_at": _iso(promotion.promoted_at if promotion else snapshot.reconciled_at),
            "contact_at_execution": _contact_snapshot(contact),
        },
        "filing": _filing_projection(session, filing=filing, membership=membership, source=source),
        "handoff": _identity_handoff(session, membership),
    }
    return payload, contact, membership, campaign_id


def _mapped_raw(batch: ImportBatch, row: ImportRow) -> dict[str, object]:
    raw = row.raw_data if isinstance(row.raw_data, dict) else {}
    if batch.column_mapping:
        mapped = {
            target: raw.get(column)
            for column, target in batch.column_mapping.items()
            if target in _IMPORT_FIELDS
        }
    else:
        lookup = {str(key).strip().lower(): value for key, value in raw.items()}
        mapped = {field: lookup.get(field) for field in _IMPORT_FIELDS}
    return {field: _import_field(field, mapped.get(field)) for field in _IMPORT_FIELDS}


def _import_projection(
    session: Session,
    *,
    batch: ImportBatch,
    row: ImportRow,
    validation: ImportRowValidation,
    actor: str,
    membership_created: bool | None,
) -> tuple[dict[str, object], Contact | None, CampaignContact | None]:
    contact = session.get(Contact, validation.contact_id) if validation.contact_id else None
    membership = _membership_for(
        session, campaign_id=batch.campaign_id, contact_id=validation.contact_id
    )
    source = _membership_source(session, membership=membership, import_batch_id=batch.id)
    suppression = (
        session.get(Suppression, validation.suppression_id) if validation.suppression_id else None
    )
    raw = _mapped_raw(batch, row)
    normalized = validation.normalized_data if isinstance(validation.normalized_data, dict) else {}
    errors = session.scalars(
        select(ImportRowError)
        .where(ImportRowError.import_row_id == row.id)
        .order_by(ImportRowError.created_at.asc(), ImportRowError.id.asc())
        .limit(20)
    ).all()
    rejected_field = next((_text(error.column_name, limit=255) for error in errors), None)
    reason_code = (
        _text(errors[0].code, limit=64)
        if errors
        else _text(validation.note, limit=200) or validation.outcome.value
    )
    accepted = validation.outcome is ImportRowOutcome.ACCEPTED
    duplicate = validation.outcome is ImportRowOutcome.DUPLICATE
    suppressed = validation.outcome is ImportRowOutcome.SUPPRESSED
    campaign = session.get(Campaign, batch.campaign_id)
    filing_status = "applied" if membership is not None else "failed"
    filing_reason = None
    if validation.outcome is ImportRowOutcome.REJECTED:
        filing_reason = "validation_rejected"
    elif validation.outcome is ImportRowOutcome.AMBIGUOUS:
        filing_reason = "identity_ambiguous"
    elif suppressed:
        filing_reason = (
            "suppression_blocked_pipeline"
            if membership is not None
            else "suppression_blocked_filing"
        )
    filing: dict[str, object] = {
        "requested": True,
        "requested_campaign_id": str(batch.campaign_id),
        "campaign_id": str(batch.campaign_id),
        "status": filing_status,
        "reason": filing_reason,
        "attempts": 1,
        "attempted_at": _iso(validation.created_at),
        "campaign_contact_id": _uuid(membership.id) if membership else None,
        "membership_created": membership_created,
        "membership_reused": (
            not membership_created if membership_created is not None and membership else None
        ),
        "membership_status": membership.membership_status.value if membership else None,
        "eligibility_status": membership.eligibility_status.value if membership else None,
        "pipeline_status": membership.pipeline_status.value if membership else None,
        "next_stage": membership.next_stage.value if membership and membership.next_stage else None,
        "source_record_id": _uuid(source.id) if source else None,
        "pipeline_enrolled": membership is not None,
    }
    natural_key = None
    if all(normalized.get(key) for key in ("first_name", "last_name", "company_domain")):
        natural_key = norm.build_natural_key(
            str(normalized["first_name"]),
            str(normalized["last_name"]),
            str(normalized["company_domain"]),
        )
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "lineage_complete": True,
        "source": {
            "type": "import",
            "system": batch.source_format.value,
            "source_record_id": str(row.id),
            "snapshot_id": None,
            "import_batch_id": str(batch.id),
            "import_row_id": str(row.id),
            "row_number": row.row_number,
            "sheet_index": row.sheet_index,
            "sheet_name": _text(row.sheet_name, limit=255),
            "schema_version": _text(batch.mapper_version or batch.parser_version, limit=64),
            "captured_at": _iso(row.created_at),
            "source_url": _url(batch.source_reference),
            "capture_mode": batch.source_format.value,
            "source_surface": "import",
            "actor": _text(actor, limit=128),
            "immutable": True,
            "import_source_name": raw.get("source_name") or _text(batch.source_name, limit=512),
            "import_source_reference": raw.get("source_reference")
            or _import_field("source_reference", batch.source_reference),
            "import_exported_by": raw.get("exported_by") or _text(batch.exported_by, limit=255),
            "import_exported_at": raw.get("exported_at")
            or (batch.exported_at.isoformat() if batch.exported_at else None),
        },
        "captured": {
            "person": {
                "first_name": raw.get("first_name"),
                "last_name": raw.get("last_name"),
                "full_name": None,
                "title": raw.get("title"),
                "email": raw.get("email"),
                "linkedin_url": _url(raw.get("linkedin_url")),
                "location": raw.get("country"),
            },
            "employer": {
                "name": raw.get("company_name"),
                "domain": _text(raw.get("company_domain"), limit=255),
                "linkedin_url": None,
                "linkedin_id": None,
                "location": raw.get("country"),
            },
            "normalized": {key: _import_field(key, normalized.get(key)) for key in _IMPORT_FIELDS},
            "labels": _label_names(session, contact_id=validation.contact_id, capture_id=None),
            "requested_labels": [],
            "note": {
                "present": False,
                "count": 0,
                "scope": None,
                "content": None,
                "author": None,
                "created_at": None,
                "bounded": True,
            },
            "field_provenance": _field_provenance(
                session,
                contact_id=validation.contact_id,
                import_row_id=row.id,
            ),
        },
        "validation": {
            "outcome": validation.outcome.value,
            "stage": "import_row_validation",
            "reason_code": reason_code,
            "rejected_field": rejected_field,
            "retry_possible": validation.outcome
            in {ImportRowOutcome.REJECTED, ImportRowOutcome.AMBIGUOUS},
            "source_preserved": True,
            "error_codes": [error.code for error in errors],
        },
        "duplicate": {
            "applied": duplicate,
            "match_type": validation.match_type.value if validation.match_type else None,
            "selected_contact_id": _uuid(validation.contact_id) if duplicate else None,
            "candidate_contact_ids": (
                None
                if validation.outcome is ImportRowOutcome.AMBIGUOUS
                else [str(validation.contact_id)]
                if duplicate and validation.contact_id is not None
                else []
            ),
            "reason": _text(validation.note, limit=1_000) if duplicate else None,
            "no_new_contact": duplicate,
            "duplicate_of_capture_id": None,
        },
        "suppression": {
            "applied": suppressed,
            "dimension": suppression.suppression_type.value if suppression else None,
            "reason": suppression.reason.value if suppression else None,
            "suppression_id": _uuid(suppression.id) if suppression else None,
            "blocked_promotion": suppressed,
            "blocked_filing": suppressed and membership is None,
        },
        "promotion": {
            "outcome": (
                "contact_created" if accepted else "contact_reused" if duplicate else "not_promoted"
            ),
            "promoted": accepted or duplicate,
            "contact_created": accepted,
            "contact_reused": duplicate,
            "contact_id": _uuid(validation.contact_id),
            "reason": _text(validation.note, limit=1_000),
            "idempotency_key": f"import-row:{row.id}",
            "deduplication_key": natural_key,
            "match_type": validation.match_type.value if validation.match_type else None,
            "source_to_contact": f"import-row:{row.id}",
            "decided_at": _iso(validation.created_at),
            "contact_at_execution": _contact_snapshot(contact),
        },
        "filing": filing,
        "handoff": _identity_handoff(session, membership),
        "campaign_at_execution": {
            "campaign_id": str(batch.campaign_id),
            "campaign_name": _text(campaign.name, limit=512) if campaign else None,
        },
    }
    return payload, contact, membership


def _manual_projection(
    session: Session,
    *,
    source: CampaignContactSource,
    membership: CampaignContact,
    contact: Contact,
    actor: str,
    membership_created: bool,
) -> dict[str, object]:
    campaign = session.get(Campaign, membership.campaign_id)
    source_type = source.source_type.strip().lower()
    discriminator = "manual" if source_type == "manual" else "api"
    suppressed = membership.state.value == "suppressed"
    suppression = None
    if suppressed:
        from app.services.suppressions import find_active_suppression

        suppression = find_active_suppression(
            session,
            email=contact.email,
            domain=contact.company_domain,
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "lineage_complete": True,
        "source": {
            "type": discriminator,
            "system": source_type,
            "source_record_id": str(source.id),
            "snapshot_id": None,
            "schema_version": INPUT_VERSION,
            "captured_at": _iso(source.recorded_at),
            "source_url": None,
            "capture_mode": (
                "operator_enrollment" if discriminator == "manual" else "api_enrollment"
            ),
            "source_surface": (
                "operator_enrollment" if discriminator == "manual" else "api_enrollment"
            ),
            "actor": _text(source.recorded_by or actor, limit=128),
            "immutable": True,
        },
        "captured": {
            "person": {
                "first_name": _text(contact.first_name, limit=255),
                "last_name": _text(contact.last_name, limit=255),
                "full_name": None,
                "title": _text(contact.title, limit=512),
                "email": _text(contact.email, limit=320),
                "linkedin_url": _url(contact.linkedin_url),
                "location": _text(contact.location, limit=512),
            },
            "employer": {
                "name": _text(contact.company_name, limit=512),
                "domain": _text(contact.company_domain, limit=255),
                "linkedin_url": None,
                "linkedin_id": None,
                "location": None,
            },
            "normalized": {},
            "labels": _label_names(session, contact_id=contact.id, capture_id=None),
            "requested_labels": [],
            "note": {
                "present": False,
                "count": 0,
                "scope": None,
                "content": None,
                "author": None,
                "created_at": None,
                "bounded": True,
            },
            "field_provenance": [],
        },
        "validation": {
            "outcome": "suppressed" if suppressed else "accepted",
            "stage": "campaign_enrollment",
            "reason_code": "suppression" if suppressed else "permanent_contact_selected",
            "rejected_field": None,
            "retry_possible": False,
            "source_preserved": True,
        },
        "duplicate": {
            "applied": False,
            "match_type": None,
            "selected_contact_id": None,
            "candidate_contact_ids": [],
            "reason": None,
            "no_new_contact": True,
            "duplicate_of_capture_id": None,
        },
        "suppression": {
            "applied": suppressed,
            "dimension": suppression.suppression_type.value if suppression else None,
            "reason": (
                suppression.reason.value
                if suppression
                else _text(
                    (membership.blocking_reasons or [{}])[0].get("code")
                    if membership.blocking_reasons
                    and isinstance(membership.blocking_reasons[0], dict)
                    else None,
                    limit=100,
                )
            ),
            "suppression_id": _uuid(suppression.id) if suppression else None,
            "blocked_promotion": False,
            "blocked_filing": False,
        },
        "promotion": {
            "outcome": "contact_reused",
            "promoted": True,
            "contact_created": False,
            "contact_reused": True,
            "contact_id": str(contact.id),
            "reason": "Selected an existing permanent Contact; Capture did not resolve identity.",
            "idempotency_key": _text(source.idempotency_key, limit=512),
            "deduplication_key": _text(contact.natural_key, limit=512),
            "match_type": "operator_selected_contact",
            "source_to_contact": f"campaign-contact-source:{source.id}",
            "decided_at": _iso(source.recorded_at),
            "contact_at_execution": _contact_snapshot(contact),
        },
        "filing": {
            "requested": True,
            "requested_campaign_id": str(membership.campaign_id),
            "campaign_id": str(membership.campaign_id),
            "status": "applied",
            "reason": "suppression_blocked_pipeline" if suppressed else None,
            "attempts": 1,
            "attempted_at": _iso(source.recorded_at),
            "campaign_contact_id": str(membership.id),
            "membership_created": membership_created,
            "membership_reused": not membership_created,
            "membership_status": membership.membership_status.value,
            "eligibility_status": membership.eligibility_status.value,
            "pipeline_status": membership.pipeline_status.value,
            "next_stage": membership.next_stage.value if membership.next_stage else None,
            "source_record_id": str(source.id),
            "pipeline_enrolled": True,
        },
        "handoff": _identity_handoff(session, membership),
        "campaign_at_execution": {
            "campaign_id": str(membership.campaign_id),
            "campaign_name": _text(campaign.name, limit=512) if campaign else None,
        },
    }


def _result_fingerprint(payload: Mapping[str, object]) -> str:
    source_value = payload.get("source")
    validation_value = payload.get("validation")
    duplicate_value = payload.get("duplicate")
    suppression_value = payload.get("suppression")
    promotion_value = payload.get("promotion")
    filing_value = payload.get("filing")
    source: Mapping[str, object] = source_value if isinstance(source_value, Mapping) else {}
    validation: Mapping[str, object] = (
        validation_value if isinstance(validation_value, Mapping) else {}
    )
    duplicate: Mapping[str, object] = (
        duplicate_value if isinstance(duplicate_value, Mapping) else {}
    )
    suppression: Mapping[str, object] = (
        suppression_value if isinstance(suppression_value, Mapping) else {}
    )
    promotion: Mapping[str, object] = (
        promotion_value if isinstance(promotion_value, Mapping) else {}
    )
    filing: Mapping[str, object] = filing_value if isinstance(filing_value, Mapping) else {}
    raw = "|".join(
        str(item or "")
        for item in (
            source.get("source_record_id"),
            validation.get("outcome"),
            duplicate.get("candidate_contact_ids"),
            suppression.get("suppression_id"),
            promotion.get("outcome"),
            promotion.get("contact_id"),
            promotion.get("match_type"),
            filing.get("status"),
            filing.get("campaign_contact_id"),
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _record(
    session: Session,
    *,
    payload: dict[str, object],
    execution_kind: str,
    source_key: str,
    contact: Contact | None,
    membership: CampaignContact | None,
    campaign_id: uuid.UUID | None = None,
    capture_id: uuid.UUID | None,
    entity_type: str,
    entity_id: uuid.UUID,
    actor: str,
    include_fingerprint: bool = False,
) -> AgentJob:
    effective_campaign_id = membership.campaign_id if membership else campaign_id
    campaign_contact_id = membership.id if membership else None
    suffix = f":{_result_fingerprint(payload)}" if include_fingerprint else ""
    key = f"capture-execution:{execution_kind}:{source_key}{suffix}:v1"
    source_value = payload.get("source")
    source_type = source_value.get("type") if isinstance(source_value, Mapping) else None
    job = session.scalars(select(AgentJob).where(AgentJob.idempotency_key == key)).one_or_none()
    created = False
    if job is not None:
        if (
            job.agent_id is not AgentIdentifier.CAPTURE
            or job.task_kind != execution_kind
            or job.entity_type != entity_type
            or job.entity_id != entity_id
        ):
            raise agent_jobs.JobIdempotencyConflict(
                "Capture execution key was reused for a different source intent"
            )
    else:
        job, created = agent_jobs.enqueue_job(
            session,
            agent_id=AgentIdentifier.CAPTURE,
            idempotency_key=key,
            task_kind=execution_kind,
            max_attempts=1,
            campaign_id=effective_campaign_id,
            campaign_contact_id=campaign_contact_id,
            contact_id=contact.id if contact else None,
            company_id=contact.company_id if contact else None,
            capture_id=capture_id,
            entity_type=entity_type,
            entity_id=entity_id,
            input_reference={
                "schema_version": INPUT_VERSION,
                "source_type": source_type,
                "source_record_id": source_key,
            },
            actor=actor,
        )
    if created:
        now = datetime.now(UTC)
        job.status = AgentJobStatus.SUCCEEDED
        job.attempts = 1
        job.started_at = now
        job.finished_at = now
        job.result = payload
        job.next_run_at = now
        session.flush()
    if membership is not None:
        state = session.scalars(
            select(CampaignContactAgentState).where(
                CampaignContactAgentState.campaign_contact_id == membership.id,
                CampaignContactAgentState.agent_id == AgentIdentifier.CAPTURE,
            )
        ).one_or_none()
        if state is not None and (created or state.latest_job_id is None):
            state.latest_job_id = job.id
            state.attempt_count = max(state.attempt_count, job.attempts)
            state.output_reference = {
                "schema_version": SCHEMA_VERSION,
                "capture_job_id": str(job.id),
                "contact_id": _uuid(job.contact_id),
                "source_record_id": source_key,
            }
            session.flush()
    return job


def record_snapshot_execution(
    session: Session,
    *,
    snapshot: LinkedInProfileSnapshot,
    actor: str,
    execution_kind: str = "capture_intake",
    material_outcome_generation: bool = False,
) -> AgentJob:
    payload, contact, membership, campaign_id = _capture_projection(
        session, snapshot=snapshot, actor=actor
    )
    return _record(
        session,
        payload=payload,
        execution_kind=execution_kind,
        source_key=str(snapshot.id),
        contact=contact,
        membership=membership,
        campaign_id=campaign_id,
        capture_id=snapshot.id,
        entity_type="linkedin_profile_snapshot",
        entity_id=snapshot.id,
        actor=actor,
        include_fingerprint=material_outcome_generation,
    )


def record_import_row_execution(
    session: Session,
    *,
    batch: ImportBatch,
    row: ImportRow,
    validation: ImportRowValidation,
    actor: str,
    membership_created: bool | None,
) -> AgentJob:
    payload, contact, membership = _import_projection(
        session,
        batch=batch,
        row=row,
        validation=validation,
        actor=actor,
        membership_created=membership_created,
    )
    return _record(
        session,
        payload=payload,
        execution_kind="capture_import_row",
        source_key=str(row.id),
        contact=contact,
        membership=membership,
        campaign_id=batch.campaign_id,
        capture_id=None,
        entity_type="import_row",
        entity_id=row.id,
        actor=actor,
    )


def record_enrollment_execution(
    session: Session,
    *,
    source: CampaignContactSource,
    membership: CampaignContact,
    contact: Contact,
    actor: str,
    membership_created: bool,
) -> AgentJob:
    payload = _manual_projection(
        session,
        source=source,
        membership=membership,
        contact=contact,
        actor=actor,
        membership_created=membership_created,
    )
    return _record(
        session,
        payload=payload,
        execution_kind="capture_enrollment",
        source_key=str(source.id),
        contact=contact,
        membership=membership,
        capture_id=source.capture_id,
        entity_type="campaign_contact_source",
        entity_id=source.id,
        actor=actor,
    )

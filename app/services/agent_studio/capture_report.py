"""Typed, query-only report for one durable Capture Agent execution.

The job result is the historical execution snapshot.  Immutable source records
may safely fill source evidence for legacy jobs, but current Contact, label,
membership and suppression state is always presented separately and never used
to repair missing historical decisions.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignContact
from app.models.collection import Collection, CollectionMembership
from app.models.company import Company
from app.models.contact import Contact
from app.models.enums import AgentIdentifier
from app.models.import_batch import ImportBatch, ImportRow
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.models.pipeline import CampaignContactSource, PipelineEvent
from app.models.verification_job import AgentJob
from app.services.agent_studio.research_report import (
    _job_error,
    _mapping,
    _retryable_error,
    _safe_domain,
    _safe_text,
    _safe_url,
    _uuid,
)
from app.services.agents import jobs as agent_jobs
from app.services.captures.execution_lineage import SCHEMA_VERSION
from app.services.captures.promotion import company_hints, person_identity
from app.services.imports import mapping as import_mapping
from app.services.suppressions import find_active_suppression


class CaptureReportState(enum.StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class CaptureSourceType(enum.StrEnum):
    EXTENSION = "extension"
    IMPORT = "import"
    MANUAL = "manual"
    API = "api"
    UNKNOWN = "unknown"


class CaptureValidationOutcome(enum.StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    SUPPRESSED = "suppressed"
    AMBIGUOUS = "ambiguous"
    PENDING = "pending"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class CaptureSourceReport:
    source_type: CaptureSourceType
    source_system: str | None
    source_record_id: uuid.UUID | None
    snapshot_id: uuid.UUID | None
    submission_id: uuid.UUID | None
    import_batch_id: uuid.UUID | None
    import_row_id: uuid.UUID | None
    row_number: int | None
    sheet_name: str | None
    schema_version: str | None
    captured_at: datetime | None
    source_url: str | None
    linkedin_profile_url: str | None
    linkedin_public_identifier: str | None
    linkedin_member_id: str | None
    linkedin_handle: str | None
    capture_mode: str | None
    source_surface: str | None
    actor: str | None
    immutable: bool | None
    import_source_name: str | None
    import_source_reference: str | None
    import_exported_by: str | None
    import_exported_at: str | None


@dataclass(frozen=True)
class CapturedPersonReport:
    first_name: str | None
    last_name: str | None
    full_name: str | None
    title: str | None
    email: str | None
    linkedin_url: str | None
    location: str | None


@dataclass(frozen=True)
class CapturedEmployerReport:
    name: str | None
    domain: str | None
    linkedin_url: str | None
    linkedin_id: str | None
    location: str | None


@dataclass(frozen=True)
class CaptureFieldProvenanceReport:
    observation_id: uuid.UUID | None
    field: str
    value: str | None
    source_name: str | None
    source_reference: str | None
    observed_at: datetime | None
    ingested_at: datetime | None
    policy_version: str | None
    winner_at_execution: bool | None


@dataclass(frozen=True)
class CaptureNoteReport:
    present: bool
    count: int
    scope: str | None
    content: str | None
    author: str | None
    created_at: datetime | None
    bounded: bool


@dataclass(frozen=True)
class CaptureEvidenceReport:
    person: CapturedPersonReport
    employer: CapturedEmployerReport
    normalized_fields: tuple[tuple[str, str | None], ...]
    labels: tuple[str, ...]
    requested_labels: tuple[str, ...]
    note: CaptureNoteReport
    field_provenance: tuple[CaptureFieldProvenanceReport, ...]


@dataclass(frozen=True)
class CaptureValidationReport:
    outcome: CaptureValidationOutcome
    stage: str | None
    reason_code: str | None
    rejected_field: str | None
    retry_possible: bool | None
    source_preserved: bool | None
    error_codes: tuple[str, ...]


@dataclass(frozen=True)
class CaptureDuplicateReport:
    applied: bool | None
    match_type: str | None
    selected_contact_id: uuid.UUID | None
    candidate_contact_ids: tuple[uuid.UUID, ...] | None
    reason: str | None
    no_new_contact: bool | None
    duplicate_of_capture_id: uuid.UUID | None


@dataclass(frozen=True)
class CaptureSuppressionReport:
    applied: bool | None
    dimension: str | None
    reason: str | None
    suppression_id: uuid.UUID | None
    blocked_promotion: bool | None
    blocked_filing: bool | None


@dataclass(frozen=True)
class ContactTruthReport:
    contact_id: uuid.UUID | None
    first_name: str | None
    last_name: str | None
    title: str | None
    company_name: str | None
    company_domain: str | None
    linkedin_url: str | None
    email: str | None
    company_id: uuid.UUID | None
    merged_into_id: uuid.UUID | None
    created_at: datetime | None


@dataclass(frozen=True)
class CapturePromotionReport:
    outcome: str | None
    promoted: bool | None
    contact_created: bool | None
    contact_reused: bool | None
    contact_id: uuid.UUID | None
    reason: str | None
    idempotency_key: str | None
    deduplication_key: str | None
    match_type: str | None
    source_to_contact: str | None
    decided_at: datetime | None
    contact_at_execution: ContactTruthReport


@dataclass(frozen=True)
class CaptureFilingReport:
    requested: bool | None
    requested_campaign_id: uuid.UUID | None
    campaign_id: uuid.UUID | None
    status: str | None
    reason: str | None
    attempts: int | None
    attempted_at: datetime | None
    campaign_contact_id: uuid.UUID | None
    membership_created: bool | None
    membership_reused: bool | None
    membership_status: str | None
    eligibility_status: str | None
    pipeline_status: str | None
    next_stage: str | None
    source_record_id: uuid.UUID | None
    pipeline_enrolled: bool | None


@dataclass(frozen=True)
class IdentityHandoffReport:
    identity_job_id: uuid.UUID | None
    status: str | None
    reason: str | None


@dataclass(frozen=True)
class CurrentCampaignMembershipReport:
    campaign_contact_id: uuid.UUID
    campaign_id: uuid.UUID
    campaign_name: str | None
    membership_status: str
    eligibility_status: str
    pipeline_status: str
    current_stage: str | None
    next_stage: str | None


@dataclass(frozen=True)
class CurrentCaptureTruthReport:
    historical_contact_record: ContactTruthReport
    current_survivor: ContactTruthReport
    current_company_name: str | None
    current_labels: tuple[str, ...]
    current_campaign_contact_ids: tuple[uuid.UUID, ...]
    current_campaign_memberships: tuple[CurrentCampaignMembershipReport, ...]
    exact_campaign_membership_status: str | None
    suppression_applied: bool
    suppression_dimension: str | None
    suppression_reason: str | None
    suppression_id: uuid.UUID | None


@dataclass(frozen=True)
class RelatedCaptureExecution:
    job_id: uuid.UUID
    execution_kind: str
    status: str
    attempts: int
    max_attempts: int
    generation: int
    created_at: datetime
    selected: bool


@dataclass(frozen=True)
class CaptureJobEventReport:
    event_type: str
    from_status: str | None
    to_status: str | None
    reason_code: str | None
    reason_detail: str | None
    retryable: bool
    occurred_at: datetime


@dataclass(frozen=True)
class CaptureExecutionReport:
    report_state: CaptureReportState
    report_reason: str
    job_id: uuid.UUID
    execution_kind: str
    job_status: str
    attempts: int
    max_attempts: int
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    next_run_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    error_type: str | None
    error_detail: str | None
    retryable_error: bool | None
    customer_account: str | None
    campaign_id: uuid.UUID | None
    campaign_name: str | None
    campaign_contact_id: uuid.UUID | None
    contact_id: uuid.UUID | None
    source: CaptureSourceReport
    captured: CaptureEvidenceReport
    validation: CaptureValidationReport
    duplicate: CaptureDuplicateReport
    suppression: CaptureSuppressionReport
    promotion: CapturePromotionReport
    filing: CaptureFilingReport
    identity_handoff: IdentityHandoffReport
    current: CurrentCaptureTruthReport
    related_executions: tuple[RelatedCaptureExecution, ...]
    job_events: tuple[CaptureJobEventReport, ...]
    unavailable: tuple[str, ...]


def _bool(mapping: Mapping[str, object], key: str) -> bool | None:
    value = mapping.get(key)
    return value if isinstance(value, bool) else None


def _int(mapping: Mapping[str, object], key: str) -> int | None:
    value = mapping.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(mapping: Mapping[str, object], key: str, *, limit: int = 1_000) -> str | None:
    value = mapping.get(key)
    return _safe_text(value if isinstance(value, str) else None, limit=limit)


def _url_str(mapping: Mapping[str, object], key: str, *, limit: int = 2_000) -> str | None:
    """Sanitize a bounded URL before generic path redaction can alter it."""

    value = mapping.get(key)
    if not isinstance(value, str) or len(value.strip()) > limit:
        return None
    return _safe_url(value)


def _datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _strings(value: object, *, limit: int = 200) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(
        safe
        for item in value[:50]
        if isinstance(item, str) and (safe := _safe_text(item, limit=limit))
    )


def _uuid_list(value: object) -> tuple[uuid.UUID, ...] | None:
    if value is None:
        return None
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    return tuple(parsed for item in value[:50] if (parsed := _uuid(item)) is not None)


def _outcome(value: object) -> CaptureValidationOutcome:
    if isinstance(value, str):
        try:
            return CaptureValidationOutcome(value)
        except ValueError:
            pass
    return CaptureValidationOutcome.UNAVAILABLE


def _source_type(value: object) -> CaptureSourceType:
    if isinstance(value, str):
        try:
            return CaptureSourceType(value)
        except ValueError:
            pass
    return CaptureSourceType.UNKNOWN


def _safe_reference(value: object, *, limit: int = 1_000) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if stripped.lower().startswith(("http://", "https://")):
        return _safe_url(stripped) if len(stripped) <= limit else None
    return _safe_text(value, limit=limit)


def _empty_note() -> CaptureNoteReport:
    return CaptureNoteReport(
        present=False,
        count=0,
        scope=None,
        content=None,
        author=None,
        created_at=None,
        bounded=True,
    )


def _empty_contact(contact_id: uuid.UUID | None = None) -> ContactTruthReport:
    return ContactTruthReport(
        contact_id=contact_id,
        first_name=None,
        last_name=None,
        title=None,
        company_name=None,
        company_domain=None,
        linkedin_url=None,
        email=None,
        company_id=None,
        merged_into_id=None,
        created_at=None,
    )


class DurableCaptureReportReader:
    """Project one persisted Capture execution without issuing writes."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def read_job(self, job_id: uuid.UUID) -> CaptureExecutionReport | None:
        with self._session.no_autoflush:
            return self._read_job(job_id)

    def _read_job(self, job_id: uuid.UUID) -> CaptureExecutionReport | None:
        job = self._session.get(AgentJob, job_id)
        if job is None or job.agent_id is not AgentIdentifier.CAPTURE:
            return None
        context = self._context(job)
        if context is None:
            return None
        campaign, membership, source_record, snapshot, batch, import_row = context
        payload = self._execution_payload(job)
        source_map = _mapping(payload.get("source"))
        source = self._source(
            source_map=source_map,
            snapshot=snapshot,
            batch=batch,
            import_row=import_row,
            source_record=source_record,
        )
        source_available, source_consistent = self._source_integrity(
            source=source,
            snapshot=snapshot,
            batch=batch,
            import_row=import_row,
            source_record=source_record,
        )
        captured = self._captured(payload, snapshot, batch, import_row)
        validation = self._validation(payload)
        duplicate = self._duplicate(payload)
        suppression = self._suppression(payload)
        promotion = self._promotion(payload, job)
        filing = self._filing(payload)
        if not self._lineage_matches_job(
            job=job,
            membership=membership,
            promotion=promotion,
            filing=filing,
        ):
            return None
        handoff = self._handoff(payload, membership)
        current = self._current(
            promotion,
            membership,
            authorized_contact_id=job.contact_id,
        )
        unavailable = self._unavailable(
            job=job,
            payload=payload,
            source=source,
            captured=captured,
            validation=validation,
            duplicate=duplicate,
            suppression=suppression,
            promotion=promotion,
            filing=filing,
            handoff=handoff,
            source_available=source_available,
            source_consistent=source_consistent,
        )
        state, reason = self._state(
            job,
            payload,
            source,
            validation,
            duplicate,
            suppression,
            promotion,
            filing,
            source_available=source_available,
            source_consistent=source_consistent,
        )
        error_type, error_detail = _job_error(job)
        return CaptureExecutionReport(
            report_state=state,
            report_reason=reason,
            job_id=job.id,
            execution_kind=job.task_kind,
            job_status=agent_jobs.public_status(job),
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            queued_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            next_run_at=job.next_run_at,
            lease_owner=_safe_text(job.lease_owner, limit=100),
            lease_expires_at=job.lease_expires_at,
            error_type=error_type,
            error_detail=error_detail,
            retryable_error=_retryable_error(job),
            customer_account=None,
            campaign_id=campaign.id if campaign else None,
            campaign_name=_safe_text(campaign.name, limit=512) if campaign else None,
            campaign_contact_id=membership.id if membership else None,
            contact_id=promotion.contact_id or job.contact_id,
            source=source,
            captured=captured,
            validation=validation,
            duplicate=duplicate,
            suppression=suppression,
            promotion=promotion,
            filing=filing,
            identity_handoff=handoff,
            current=current,
            related_executions=self._related(job),
            job_events=self._events(job, membership),
            unavailable=unavailable,
        )

    @staticmethod
    def _execution_payload(job: AgentJob) -> Mapping[str, object]:
        result = _mapping(job.result)
        if result:
            return result
        return _mapping(_mapping(job.error).get("detail"))

    def _context(
        self, job: AgentJob
    ) -> (
        tuple[
            Campaign | None,
            CampaignContact | None,
            CampaignContactSource | None,
            LinkedInProfileSnapshot | None,
            ImportBatch | None,
            ImportRow | None,
        ]
        | None
    ):
        campaign = self._session.get(Campaign, job.campaign_id) if job.campaign_id else None
        if job.campaign_id is not None and campaign is None:
            return None
        membership = (
            self._session.get(CampaignContact, job.campaign_contact_id)
            if job.campaign_contact_id
            else None
        )
        if job.campaign_contact_id is not None and membership is None:
            return None
        if membership is not None and (
            (job.campaign_id is not None and membership.campaign_id != job.campaign_id)
            or (job.contact_id is not None and membership.contact_id != job.contact_id)
        ):
            return None
        snapshot = (
            self._session.get(LinkedInProfileSnapshot, job.capture_id) if job.capture_id else None
        )
        source_record = (
            self._session.get(CampaignContactSource, job.entity_id)
            if job.entity_type == "campaign_contact_source" and job.entity_id
            else None
        )
        import_row = (
            self._session.get(ImportRow, job.entity_id)
            if job.entity_type == "import_row" and job.entity_id
            else None
        )
        batch = self._session.get(ImportBatch, import_row.batch_id) if import_row else None
        if import_row is not None and (
            batch is None or (job.campaign_id is not None and batch.campaign_id != job.campaign_id)
        ):
            return None
        if source_record is not None and membership is not None:
            if source_record.campaign_contact_id != membership.id:
                return None
        if source_record is not None and membership is None:
            return None
        if (
            job.entity_type == "linkedin_profile_snapshot"
            and job.capture_id is not None
            and job.entity_id != job.capture_id
        ):
            return None
        return campaign, membership, source_record, snapshot, batch, import_row

    @staticmethod
    def _source_integrity(
        *,
        source: CaptureSourceReport,
        snapshot: LinkedInProfileSnapshot | None,
        batch: ImportBatch | None,
        import_row: ImportRow | None,
        source_record: CampaignContactSource | None,
    ) -> tuple[bool, bool]:
        if source.source_type is CaptureSourceType.EXTENSION:
            available = snapshot is not None
            consistent = bool(
                snapshot is not None
                and source.source_record_id == snapshot.id
                and source.snapshot_id == snapshot.id
                and (
                    source.schema_version is None
                    or source.schema_version == snapshot.schema_version
                )
            )
            return available, consistent
        if source.source_type is CaptureSourceType.IMPORT:
            available = batch is not None and import_row is not None
            consistent = bool(
                batch is not None
                and import_row is not None
                and source.source_record_id == import_row.id
                and source.import_row_id == import_row.id
                and source.import_batch_id == batch.id
                and (
                    source.schema_version is None
                    or source.schema_version == batch.mapper_version
                    or source.schema_version == batch.parser_version
                )
            )
            return available, consistent
        if source.source_type in {CaptureSourceType.MANUAL, CaptureSourceType.API}:
            available = source_record is not None
            return available, bool(
                source_record is not None and source.source_record_id == source_record.id
            )
        return False, False

    @staticmethod
    def _lineage_matches_job(
        *,
        job: AgentJob,
        membership: CampaignContact | None,
        promotion: CapturePromotionReport,
        filing: CaptureFilingReport,
    ) -> bool:
        if (
            job.contact_id is not None
            and promotion.contact_id is not None
            and job.contact_id != promotion.contact_id
        ):
            return False
        if (
            job.campaign_id is not None
            and filing.campaign_id is not None
            and job.campaign_id != filing.campaign_id
        ):
            return False
        if (
            membership is not None
            and filing.campaign_contact_id is not None
            and membership.id != filing.campaign_contact_id
        ):
            return False
        return True

    def _source(
        self,
        *,
        source_map: Mapping[str, object],
        snapshot: LinkedInProfileSnapshot | None,
        batch: ImportBatch | None,
        import_row: ImportRow | None,
        source_record: CampaignContactSource | None,
    ) -> CaptureSourceReport:
        if source_map:
            return CaptureSourceReport(
                source_type=_source_type(source_map.get("type")),
                source_system=_str(source_map, "system", limit=128),
                source_record_id=_uuid(source_map.get("source_record_id")),
                snapshot_id=_uuid(source_map.get("snapshot_id")),
                submission_id=_uuid(source_map.get("submission_id")),
                import_batch_id=_uuid(source_map.get("import_batch_id")),
                import_row_id=_uuid(source_map.get("import_row_id")),
                row_number=_int(source_map, "row_number"),
                sheet_name=_str(source_map, "sheet_name", limit=255),
                schema_version=_str(source_map, "schema_version", limit=64),
                captured_at=_datetime(source_map.get("captured_at")),
                source_url=_url_str(source_map, "source_url"),
                linkedin_profile_url=_url_str(source_map, "linkedin_profile_url"),
                linkedin_public_identifier=_str(
                    source_map, "linkedin_public_identifier", limit=255
                ),
                linkedin_member_id=_str(source_map, "linkedin_member_id", limit=255),
                linkedin_handle=_str(source_map, "linkedin_handle", limit=255),
                capture_mode=_str(source_map, "capture_mode", limit=64),
                source_surface=_str(source_map, "source_surface", limit=512),
                actor=_str(source_map, "actor", limit=128),
                immutable=_bool(source_map, "immutable"),
                import_source_name=_str(source_map, "import_source_name", limit=512),
                import_source_reference=_safe_reference(source_map.get("import_source_reference")),
                import_exported_by=_str(source_map, "import_exported_by", limit=255),
                import_exported_at=_str(source_map, "import_exported_at", limit=100),
            )
        if snapshot is not None:
            return CaptureSourceReport(
                source_type=CaptureSourceType.EXTENSION,
                source_system=_safe_text(snapshot.source, limit=128),
                source_record_id=snapshot.id,
                snapshot_id=snapshot.id,
                submission_id=snapshot.submission_id,
                import_batch_id=None,
                import_row_id=None,
                row_number=None,
                sheet_name=None,
                schema_version=_safe_text(snapshot.schema_version, limit=64),
                captured_at=snapshot.captured_at or snapshot.ingested_at,
                source_url=_safe_url(snapshot.source_url),
                linkedin_profile_url=_safe_url(snapshot.normalized_profile_url),
                linkedin_public_identifier=_safe_text(snapshot.public_identifier, limit=255),
                linkedin_member_id=_safe_text(snapshot.salesnav_member_id, limit=255),
                linkedin_handle=_safe_text(snapshot.public_identifier, limit=255),
                capture_mode=_safe_text(snapshot.capture_mode, limit=64),
                source_surface=_safe_text(snapshot.source_surface, limit=512),
                actor=None,
                immutable=True,
                import_source_name=None,
                import_source_reference=None,
                import_exported_by=None,
                import_exported_at=None,
            )
        if batch is not None and import_row is not None:
            return CaptureSourceReport(
                source_type=CaptureSourceType.IMPORT,
                source_system=batch.source_format.value,
                source_record_id=import_row.id,
                snapshot_id=None,
                submission_id=None,
                import_batch_id=batch.id,
                import_row_id=import_row.id,
                row_number=import_row.row_number,
                sheet_name=_safe_text(import_row.sheet_name, limit=255),
                schema_version=_safe_text(batch.mapper_version or batch.parser_version, limit=64),
                captured_at=import_row.created_at,
                source_url=_safe_url(batch.source_reference),
                linkedin_profile_url=None,
                linkedin_public_identifier=None,
                linkedin_member_id=None,
                linkedin_handle=None,
                capture_mode=batch.source_format.value,
                source_surface="import",
                actor=None,
                immutable=True,
                import_source_name=_safe_text(batch.source_name, limit=512),
                import_source_reference=_safe_reference(batch.source_reference),
                import_exported_by=_safe_text(batch.exported_by, limit=255),
                import_exported_at=(batch.exported_at.isoformat() if batch.exported_at else None),
            )
        if source_record is not None:
            kind = (
                CaptureSourceType.MANUAL
                if source_record.source_type == "manual"
                else CaptureSourceType.API
            )
            return CaptureSourceReport(
                source_type=kind,
                source_system=_safe_text(source_record.source_type, limit=64),
                source_record_id=source_record.id,
                snapshot_id=source_record.capture_id,
                submission_id=None,
                import_batch_id=source_record.import_batch_id,
                import_row_id=None,
                row_number=None,
                sheet_name=None,
                schema_version=None,
                captured_at=source_record.recorded_at,
                source_url=None,
                linkedin_profile_url=None,
                linkedin_public_identifier=None,
                linkedin_member_id=None,
                linkedin_handle=None,
                capture_mode="campaign_enrollment",
                source_surface=_safe_text(source_record.source_reference, limit=512),
                actor=_safe_text(source_record.recorded_by, limit=128),
                immutable=True,
                import_source_name=None,
                import_source_reference=None,
                import_exported_by=None,
                import_exported_at=None,
            )
        return CaptureSourceReport(
            source_type=CaptureSourceType.UNKNOWN,
            source_system=None,
            source_record_id=None,
            snapshot_id=None,
            submission_id=None,
            import_batch_id=None,
            import_row_id=None,
            row_number=None,
            sheet_name=None,
            schema_version=None,
            captured_at=None,
            source_url=None,
            linkedin_profile_url=None,
            linkedin_public_identifier=None,
            linkedin_member_id=None,
            linkedin_handle=None,
            capture_mode=None,
            source_surface=None,
            actor=None,
            immutable=None,
            import_source_name=None,
            import_source_reference=None,
            import_exported_by=None,
            import_exported_at=None,
        )

    def _captured(
        self,
        payload: Mapping[str, object],
        snapshot: LinkedInProfileSnapshot | None,
        batch: ImportBatch | None,
        import_row: ImportRow | None,
    ) -> CaptureEvidenceReport:
        captured = _mapping(payload.get("captured"))
        person = _mapping(captured.get("person"))
        employer = _mapping(captured.get("employer"))
        if not captured and snapshot is not None:
            identity = person_identity(snapshot)
            hints = company_hints(snapshot)
            person = {
                "first_name": identity.first_name,
                "last_name": identity.last_name,
                "full_name": identity.full_name,
                "title": identity.title,
                "linkedin_url": identity.normalized_profile_url,
                "location": (snapshot.profile_fields or {}).get("displayed_location"),
            }
            employer = {
                "name": hints.name,
                "linkedin_url": hints.linkedin_url,
                "linkedin_id": hints.linkedin_id,
                "location": hints.location,
            }
        elif not captured and batch is not None and import_row is not None:
            raw = self._import_raw(batch, import_row)
            person = {
                "first_name": raw.get("first_name"),
                "last_name": raw.get("last_name"),
                "title": raw.get("title"),
                "email": raw.get("email"),
                "linkedin_url": raw.get("linkedin_url"),
                "location": raw.get("country"),
            }
            employer = {
                "name": raw.get("company_name"),
                "domain": raw.get("company_domain"),
                "location": raw.get("country"),
            }
        normalized = _mapping(captured.get("normalized"))
        note = _mapping(captured.get("note"))
        provenance: list[CaptureFieldProvenanceReport] = []
        raw_provenance = captured.get("field_provenance")
        if isinstance(raw_provenance, Sequence) and not isinstance(raw_provenance, (str, bytes)):
            for item in raw_provenance[:50]:
                row = _mapping(item)
                field = _str(row, "field", limit=64)
                if not field:
                    continue
                provenance.append(
                    CaptureFieldProvenanceReport(
                        observation_id=_uuid(row.get("observation_id")),
                        field=field,
                        value=_str(row, "value"),
                        source_name=_str(row, "source_name", limit=256),
                        source_reference=_safe_reference(row.get("source_reference"), limit=512),
                        observed_at=_datetime(row.get("observed_at")),
                        ingested_at=_datetime(row.get("ingested_at")),
                        policy_version=_str(row, "policy_version", limit=50),
                        winner_at_execution=_bool(row, "winner_at_execution"),
                    )
                )
        return CaptureEvidenceReport(
            person=CapturedPersonReport(
                first_name=_str(person, "first_name", limit=255),
                last_name=_str(person, "last_name", limit=255),
                full_name=_str(person, "full_name", limit=512),
                title=_str(person, "title", limit=512),
                email=_str(person, "email", limit=320),
                linkedin_url=_url_str(person, "linkedin_url"),
                location=_str(person, "location", limit=512),
            ),
            employer=CapturedEmployerReport(
                name=_str(employer, "name", limit=512),
                domain=_safe_domain(employer.get("domain")),
                linkedin_url=_url_str(employer, "linkedin_url"),
                linkedin_id=_str(employer, "linkedin_id", limit=255),
                location=_str(employer, "location", limit=512),
            ),
            normalized_fields=tuple(
                (key, _safe_text(value, limit=1_000) if isinstance(value, str) else None)
                for key, value in sorted(normalized.items())
                if isinstance(key, str)
                and key
                in import_mapping.SYSTEM_FIELDS
                + (
                    "profile_url",
                    "public_identifier",
                    "salesnav_member_id",
                )
            ),
            labels=_strings(captured.get("labels"), limit=64),
            requested_labels=_strings(captured.get("requested_labels"), limit=64),
            note=(
                CaptureNoteReport(
                    present=_bool(note, "present") is True,
                    count=_int(note, "count") or 0,
                    scope=_str(note, "scope", limit=16),
                    content=_str(note, "content", limit=500),
                    author=_str(note, "author", limit=128),
                    created_at=_datetime(note.get("created_at")),
                    bounded=_bool(note, "bounded") is not False,
                )
                if note
                else _empty_note()
            ),
            field_provenance=tuple(provenance),
        )

    @staticmethod
    def _import_raw(batch: ImportBatch, row: ImportRow) -> Mapping[str, object]:
        raw = row.raw_data if isinstance(row.raw_data, dict) else {}
        if batch.column_mapping:
            return {
                target: raw.get(column)
                for column, target in batch.column_mapping.items()
                if target in import_mapping.SYSTEM_FIELDS
            }
        lower = {str(key).strip().lower(): value for key, value in raw.items()}
        return {key: lower.get(key) for key in import_mapping.SYSTEM_FIELDS}

    @staticmethod
    def _validation(payload: Mapping[str, object]) -> CaptureValidationReport:
        value = _mapping(payload.get("validation"))
        return CaptureValidationReport(
            outcome=_outcome(value.get("outcome")),
            stage=_str(value, "stage", limit=100),
            reason_code=_str(value, "reason_code", limit=200),
            rejected_field=_str(value, "rejected_field", limit=255),
            retry_possible=_bool(value, "retry_possible"),
            source_preserved=_bool(value, "source_preserved"),
            error_codes=_strings(value.get("error_codes"), limit=64),
        )

    @staticmethod
    def _duplicate(payload: Mapping[str, object]) -> CaptureDuplicateReport:
        value = _mapping(payload.get("duplicate"))
        return CaptureDuplicateReport(
            applied=_bool(value, "applied"),
            match_type=_str(value, "match_type", limit=100),
            selected_contact_id=_uuid(value.get("selected_contact_id")),
            candidate_contact_ids=_uuid_list(value.get("candidate_contact_ids")),
            reason=_str(value, "reason"),
            no_new_contact=_bool(value, "no_new_contact"),
            duplicate_of_capture_id=_uuid(value.get("duplicate_of_capture_id")),
        )

    @staticmethod
    def _suppression(payload: Mapping[str, object]) -> CaptureSuppressionReport:
        value = _mapping(payload.get("suppression"))
        return CaptureSuppressionReport(
            applied=_bool(value, "applied"),
            dimension=_str(value, "dimension", limit=50),
            reason=_str(value, "reason", limit=200),
            suppression_id=_uuid(value.get("suppression_id")),
            blocked_promotion=_bool(value, "blocked_promotion"),
            blocked_filing=_bool(value, "blocked_filing"),
        )

    @staticmethod
    def _contact(
        value: Mapping[str, object], fallback: uuid.UUID | None = None
    ) -> ContactTruthReport:
        return ContactTruthReport(
            contact_id=_uuid(value.get("contact_id")) or fallback,
            first_name=_str(value, "first_name", limit=255),
            last_name=_str(value, "last_name", limit=255),
            title=_str(value, "title", limit=512),
            company_name=_str(value, "company_name", limit=512),
            company_domain=_safe_domain(value.get("company_domain")),
            linkedin_url=_url_str(value, "linkedin_url"),
            email=_str(value, "email", limit=320),
            company_id=_uuid(value.get("company_id")),
            merged_into_id=_uuid(value.get("merged_into_id")),
            created_at=_datetime(value.get("created_at")),
        )

    def _promotion(self, payload: Mapping[str, object], job: AgentJob) -> CapturePromotionReport:
        value = _mapping(payload.get("promotion"))
        contact_id = _uuid(value.get("contact_id")) or job.contact_id
        historical = _mapping(value.get("contact_at_execution"))
        return CapturePromotionReport(
            outcome=_str(value, "outcome", limit=100),
            promoted=_bool(value, "promoted"),
            contact_created=_bool(value, "contact_created"),
            contact_reused=_bool(value, "contact_reused"),
            contact_id=contact_id,
            reason=_str(value, "reason"),
            idempotency_key=_str(value, "idempotency_key", limit=512),
            deduplication_key=_str(value, "deduplication_key", limit=512),
            match_type=_str(value, "match_type", limit=100),
            source_to_contact=_str(value, "source_to_contact", limit=512),
            decided_at=_datetime(value.get("decided_at")),
            contact_at_execution=(
                self._contact(historical, contact_id) if historical else _empty_contact(contact_id)
            ),
        )

    @staticmethod
    def _filing(payload: Mapping[str, object]) -> CaptureFilingReport:
        value = _mapping(payload.get("filing"))
        return CaptureFilingReport(
            requested=_bool(value, "requested"),
            requested_campaign_id=_uuid(value.get("requested_campaign_id")),
            campaign_id=_uuid(value.get("campaign_id")),
            status=_str(value, "status", limit=50),
            reason=_str(value, "reason", limit=200),
            attempts=_int(value, "attempts"),
            attempted_at=_datetime(value.get("attempted_at")),
            campaign_contact_id=_uuid(value.get("campaign_contact_id")),
            membership_created=_bool(value, "membership_created"),
            membership_reused=_bool(value, "membership_reused"),
            membership_status=_str(value, "membership_status", limit=50),
            eligibility_status=_str(value, "eligibility_status", limit=50),
            pipeline_status=_str(value, "pipeline_status", limit=50),
            next_stage=_str(value, "next_stage", limit=50),
            source_record_id=_uuid(value.get("source_record_id")),
            pipeline_enrolled=_bool(value, "pipeline_enrolled"),
        )

    def _handoff(
        self, payload: Mapping[str, object], membership: CampaignContact | None
    ) -> IdentityHandoffReport:
        value = _mapping(payload.get("handoff"))
        job_id = _uuid(value.get("identity_job_id"))
        job = self._session.get(AgentJob, job_id) if job_id else None
        if job is not None and (
            job.agent_id is not AgentIdentifier.IDENTITY
            or membership is None
            or job.campaign_contact_id != membership.id
            or job.campaign_id != membership.campaign_id
            or job.contact_id != membership.contact_id
        ):
            job = None
            job_id = None
        return IdentityHandoffReport(
            identity_job_id=job_id,
            status=agent_jobs.public_status(job) if job else _str(value, "status", limit=50),
            reason=_str(value, "reason", limit=200),
        )

    def _current(
        self,
        promotion: CapturePromotionReport,
        membership: CampaignContact | None,
        *,
        authorized_contact_id: uuid.UUID | None,
    ) -> CurrentCaptureTruthReport:
        historical_record = (
            self._session.get(Contact, authorized_contact_id)
            if authorized_contact_id is not None
            and promotion.contact_id in {None, authorized_contact_id}
            else None
        )
        survivor = self._survivor(historical_record)
        company = (
            self._session.get(Company, survivor.company_id)
            if survivor and survivor.company_id
            else None
        )
        exact_membership = membership
        memberships = (
            self._session.scalars(
                select(CampaignContact)
                .where(CampaignContact.contact_id == survivor.id)
                .order_by(CampaignContact.created_at.asc(), CampaignContact.id.asc())
                .limit(50)
            ).all()
            if survivor
            else []
        )
        campaign_names = {
            row.id: _safe_text(row.name, limit=512)
            for row in self._session.scalars(
                select(Campaign).where(Campaign.id.in_({row.campaign_id for row in memberships}))
            ).all()
        }
        labels: Sequence[str] = (
            tuple(
                self._session.execute(
                    select(Collection.name)
                    .join(CollectionMembership, CollectionMembership.collection_id == Collection.id)
                    .where(CollectionMembership.contact_id == survivor.id)
                    .order_by(Collection.name.asc())
                    .limit(50)
                )
                .scalars()
                .all()
            )
            if survivor
            else ()
        )
        suppression = (
            find_active_suppression(
                self._session,
                email=survivor.email,
                domain=survivor.company_domain,
            )
            if survivor
            else None
        )
        return CurrentCaptureTruthReport(
            historical_contact_record=self._contact_row(historical_record),
            current_survivor=self._contact_row(survivor),
            current_company_name=_safe_text(company.name, limit=512) if company else None,
            current_labels=tuple(item for name in labels if (item := _safe_text(name, limit=64))),
            current_campaign_contact_ids=tuple(row.id for row in memberships),
            current_campaign_memberships=tuple(
                CurrentCampaignMembershipReport(
                    campaign_contact_id=row.id,
                    campaign_id=row.campaign_id,
                    campaign_name=campaign_names.get(row.campaign_id),
                    membership_status=row.membership_status.value,
                    eligibility_status=row.eligibility_status.value,
                    pipeline_status=row.pipeline_status.value,
                    current_stage=row.current_stage.value if row.current_stage else None,
                    next_stage=row.next_stage.value if row.next_stage else None,
                )
                for row in memberships
            ),
            exact_campaign_membership_status=(
                exact_membership.membership_status.value if exact_membership else None
            ),
            suppression_applied=suppression is not None,
            suppression_dimension=(suppression.suppression_type.value if suppression else None),
            suppression_reason=suppression.reason.value if suppression else None,
            suppression_id=suppression.id if suppression else None,
        )

    def _survivor(self, contact: Contact | None) -> Contact | None:
        seen: set[uuid.UUID] = set()
        current = contact
        while current is not None and current.merged_into_id is not None and len(seen) < 20:
            if current.id in seen:
                return current
            seen.add(current.id)
            current = self._session.get(Contact, current.merged_into_id)
        return current

    @staticmethod
    def _contact_row(contact: Contact | None) -> ContactTruthReport:
        if contact is None:
            return _empty_contact()
        return ContactTruthReport(
            contact_id=contact.id,
            first_name=_safe_text(contact.first_name, limit=255),
            last_name=_safe_text(contact.last_name, limit=255),
            title=_safe_text(contact.title, limit=512),
            company_name=_safe_text(contact.company_name, limit=512),
            company_domain=_safe_domain(contact.company_domain),
            linkedin_url=_safe_url(contact.linkedin_url),
            email=_safe_text(contact.email, limit=320),
            company_id=contact.company_id,
            merged_into_id=contact.merged_into_id,
            created_at=contact.created_at,
        )

    def _related(self, job: AgentJob) -> tuple[RelatedCaptureExecution, ...]:
        conditions = []
        if job.capture_id is not None:
            conditions.append(AgentJob.capture_id == job.capture_id)
        if job.entity_type is not None and job.entity_id is not None:
            conditions.append(
                (AgentJob.entity_type == job.entity_type) & (AgentJob.entity_id == job.entity_id)
            )
        if job.campaign_contact_id is not None:
            conditions.append(AgentJob.campaign_contact_id == job.campaign_contact_id)
        statement = select(AgentJob).where(AgentJob.agent_id == AgentIdentifier.CAPTURE)
        if conditions:
            statement = statement.where(or_(*conditions))
        else:
            statement = statement.where(AgentJob.id == job.id)
        rows = self._session.scalars(
            statement.order_by(AgentJob.created_at.asc(), AgentJob.id.asc()).limit(100)
        ).all()
        return tuple(
            RelatedCaptureExecution(
                job_id=row.id,
                execution_kind=row.task_kind,
                status=agent_jobs.public_status(row),
                attempts=row.attempts,
                max_attempts=row.max_attempts,
                generation=index,
                created_at=row.created_at,
                selected=row.id == job.id,
            )
            for index, row in enumerate(rows, start=1)
        )

    def _events(
        self, job: AgentJob, membership: CampaignContact | None
    ) -> tuple[CaptureJobEventReport, ...]:
        if membership is None:
            return ()
        rows = self._session.scalars(
            select(PipelineEvent)
            .where(
                PipelineEvent.job_id == job.id,
                PipelineEvent.campaign_contact_id == membership.id,
                PipelineEvent.agent_id == AgentIdentifier.CAPTURE,
            )
            .order_by(PipelineEvent.occurred_at.asc(), PipelineEvent.id.asc())
        ).all()
        return tuple(
            CaptureJobEventReport(
                event_type=row.event_type.value,
                from_status=row.from_status.value if row.from_status else None,
                to_status=row.to_status.value if row.to_status else None,
                reason_code=_safe_text(row.reason_code, limit=96),
                reason_detail=_safe_text(row.reason_detail, limit=1_000),
                retryable=row.retryable,
                occurred_at=row.occurred_at,
            )
            for row in rows
        )

    @staticmethod
    def _unavailable(
        *,
        job: AgentJob,
        payload: Mapping[str, object],
        source: CaptureSourceReport,
        captured: CaptureEvidenceReport,
        validation: CaptureValidationReport,
        duplicate: CaptureDuplicateReport,
        suppression: CaptureSuppressionReport,
        promotion: CapturePromotionReport,
        filing: CaptureFilingReport,
        handoff: IdentityHandoffReport,
        source_available: bool,
        source_consistent: bool,
    ) -> tuple[str, ...]:
        missing = ["Authoritative customer/account ownership is not persisted in this context."]
        if source.source_type is CaptureSourceType.UNKNOWN:
            missing.append("The Capture source type and source record are unavailable.")
        if source.snapshot_id is None and source.source_type is CaptureSourceType.EXTENSION:
            missing.append("The exact immutable extension snapshot is unavailable.")
        if not source_available and source.source_type is not CaptureSourceType.UNKNOWN:
            missing.append("The exact referenced Capture source record is unavailable.")
        elif source_available and not source_consistent:
            missing.append(
                "The source identifier or source-schema version does not match its immutable "
                "record."
            )
        if not any(
            (
                captured.person.first_name,
                captured.person.last_name,
                captured.person.full_name,
                captured.person.linkedin_url,
            )
        ):
            missing.append("Captured person fields are unavailable for this execution.")
        if validation.outcome is CaptureValidationOutcome.UNAVAILABLE:
            missing.append("The exact staged validation outcome was not persisted.")
        if duplicate.applied is None:
            missing.append("The historical duplicate decision was not persisted.")
        if duplicate.applied and duplicate.candidate_contact_ids is None:
            missing.append("The historical duplicate candidate ledger was not persisted.")
        if (
            validation.outcome is CaptureValidationOutcome.AMBIGUOUS
            and duplicate.candidate_contact_ids is None
        ):
            missing.append("The historical ambiguity candidate ledger was not persisted.")
        if suppression.applied is None:
            missing.append("The historical suppression decision was not persisted.")
        if promotion.outcome is None:
            missing.append("The historical promotion outcome was not persisted.")
        if promotion.contact_id is not None and promotion.contact_at_execution.first_name is None:
            missing.append("Contact state at execution was not persisted.")
        if filing.requested is None:
            missing.append("The historical Campaign filing intent was not persisted.")
        if filing.requested and filing.status is None:
            missing.append("The historical Campaign filing outcome was not persisted.")
        if (
            filing.requested
            and filing.campaign_contact_id is None
            and (filing.pipeline_enrolled is True or filing.status == "applied")
        ):
            missing.append("The exact historical Campaign Contact lineage is unavailable.")
        if handoff.identity_job_id is None and handoff.reason in {
            None,
            "identity_state_or_job_not_persisted",
        }:
            missing.append("No exact next Identity Agent Job was durably linked.")
        if payload.get("schema_version") != SCHEMA_VERSION:
            missing.append("This execution predates the CAP-002 Capture report contract.")
        missing.extend(
            (
                "Unpersisted fuzzy or retrospective duplicate candidates are unavailable.",
                "Rejected intake payloads that never produced a source record have no Capture job.",
                "Private raw snapshot/import payloads are intentionally unavailable from "
                "this report.",
            )
        )
        if job.attempts > 1:
            missing.append(
                "Per-attempt Capture decision deltas are unavailable; attempts share this "
                "job result."
            )
        return tuple(dict.fromkeys(missing))

    @staticmethod
    def _state(
        job: AgentJob,
        payload: Mapping[str, object],
        source: CaptureSourceReport,
        validation: CaptureValidationReport,
        duplicate: CaptureDuplicateReport,
        suppression: CaptureSuppressionReport,
        promotion: CapturePromotionReport,
        filing: CaptureFilingReport,
        *,
        source_available: bool,
        source_consistent: bool,
    ) -> tuple[CaptureReportState, str]:
        if (
            payload.get("schema_version") == SCHEMA_VERSION
            and payload.get("lineage_complete") is True
            and source.source_type is not CaptureSourceType.UNKNOWN
            and source_available
            and source_consistent
            and validation.outcome is not CaptureValidationOutcome.UNAVAILABLE
            and duplicate.applied is not None
            and suppression.applied is not None
            and promotion.outcome is not None
            and filing.requested is not None
            and (promotion.contact_id is None or promotion.contact_id == job.contact_id)
            and (filing.campaign_id is None or filing.campaign_id == job.campaign_id)
            and (
                filing.campaign_contact_id is None
                or filing.campaign_contact_id == job.campaign_contact_id
            )
            and not (
                (duplicate.applied or validation.outcome is CaptureValidationOutcome.AMBIGUOUS)
                and duplicate.candidate_contact_ids is None
            )
            and not (suppression.applied and suppression.suppression_id is None)
            and not (
                (promotion.promoted or promotion.contact_created or promotion.contact_reused)
                and promotion.contact_id is None
            )
            and not (
                filing.requested
                and filing.status == "applied"
                and filing.campaign_contact_id is None
            )
        ):
            return (
                CaptureReportState.COMPLETE,
                "Exact CAP-002 bounded execution lineage is available.",
            )
        if source.source_type is not CaptureSourceType.UNKNOWN or job.result or job.error:
            return (
                CaptureReportState.PARTIAL,
                "Only part of this Capture execution's historical lineage is durable.",
            )
        return (
            CaptureReportState.UNAVAILABLE,
            "No durable Capture execution outcome or supported source record is available.",
        )

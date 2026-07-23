"""Sales Navigator capture -> staged import intake (DAT-009).

Backend adapter for the operator-driven Sales Navigator capture extension. It
receives one authorized JSON batch and **stages** it: it creates exactly one
:class:`~app.models.import_batch.ImportBatch` (``source_format`` =
``sales_navigator``, ``status`` = ``pending``) plus one immutable
:class:`~app.models.import_batch.ImportRow` per captured record — and nothing
else.

It deliberately does NOT create, normalize authoritatively, deduplicate against
the database, suppress, verify, score, or otherwise process contacts. Those are
downstream operator-workbench steps that run only after the operator previews and
confirms the staged batch (GOAL.md, AGENTS.md, and the extension's
``BACKEND_CONTRACT.md``). Every captured record is written verbatim to the
write-once ``import_rows`` table, so all extension-supplied values, warnings,
exclusions, timestamps, and URLs are preserved for that later review (DAT-003 /
DAT-005 provenance).

This reuses the SAME staged-import tables and raw-capture discipline as the
CSV/XLSX importer (:mod:`app.services.imports.importer`); it does not introduce a
second import pipeline. The one honest difference is the source: records arrive
as a validated JSON batch rather than a spreadsheet, so raw capture maps each
record onto one immutable raw row instead of parsing a file.

The request body is validated against the extension's committed contract schema
(``extensions/salesnav-capture/docs/intake.schema.json``, contract version
``salesnav-capture/1.0.0``) — the single source of truth for the wire shape.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.campaign import Campaign
from app.models.enums import CampaignStatus, ImportBatchStatus, ImportSourceFormat
from app.models.import_batch import ImportBatch, ImportRow
from app.services.audit import record_audit_event

# --- Contract constants ------------------------------------------------------

# The single source of truth for the accepted contract version is the extension
# (mirrored here for the MAJOR-version gate). See BACKEND_CONTRACT.md.
CONTRACT_NAMESPACE = "salesnav-capture"
SUPPORTED_MAJOR = 1
SCHEMA_VERSION = f"{CONTRACT_NAMESPACE}/1.0.0"

# A staged batch is advertised to the extension as expiring after this window
# (the un-committed batch is meant to be previewed and confirmed promptly). The
# value is advisory in the response; nothing is deleted automatically.
STAGED_BATCH_TTL_HOURS = 24

_SOURCE_ACTOR = "salesnav-capture"
_VERSION_RE = re.compile(rf"^{re.escape(CONTRACT_NAMESPACE)}/(\d+)\.(\d+)\.(\d+)$")

# The committed contract schema lives with the extension so the wire shape has
# exactly one definition. Resolve it from the repository root:
# app/services/imports/salesnav_intake.py -> parents[3] == repo root.
_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3]
    / "extensions"
    / "salesnav-capture"
    / "docs"
    / "intake.schema.json"
)

# Fields that establish a record's minimum identity. A record with none of these
# carries no useful signal to stage (the contract rejects an empty record).
_IDENTITY_FIELDS = (
    "firstName",
    "lastName",
    "rawFullName",
    "linkedinProfileUrl",
    "salesNavLeadUrl",
)


# --- Error hierarchy ---------------------------------------------------------


class SalesNavIntakeError(Exception):
    """Base class for deterministic, client-facing intake failures.

    Each subclass carries the stable ``error`` code and HTTP status the contract
    defines, plus an optional ``details`` list. The route renders these verbatim;
    no stack trace or internal detail leaks to the client.
    """

    error_code: str = "internal_error"
    http_status: int = 500

    def __init__(self, message: str, *, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or []

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": self.error_code, "status": self.http_status}
        if self.details:
            body["details"] = self.details
        return body


class InvalidJsonError(SalesNavIntakeError):
    """The request body was not valid JSON."""

    error_code = "invalid_json"
    http_status = 400


class ValidationFailedError(SalesNavIntakeError):
    """The request body failed contract-schema or semantic validation."""

    error_code = "validation_failed"
    http_status = 422


class UnsupportedVersionError(ValidationFailedError):
    """The batch declares an unsupported contract MAJOR version.

    Per the contract's versioning rules an unknown MAJOR is rejected as
    ``422 validation_failed``; this subclass keeps that code while making the
    reason explicit and separately testable.
    """


class CampaignInvalidError(SalesNavIntakeError):
    """The selected campaign is missing, unknown, or not available for staging."""

    error_code = "campaign_invalid"
    http_status = 409


class IdempotencyConflictError(SalesNavIntakeError):
    """The ``client_batch_id`` was already staged with a different payload.

    A retry of the *same* batch is idempotent (it returns the original result).
    Reusing the same id for *different* content is a client error and is refused
    rather than silently overwriting or duplicating the staged batch.
    """

    error_code = "client_batch_id_conflict"
    http_status = 409


class PayloadTooLargeError(SalesNavIntakeError):
    """The request body exceeded the configured intake limit."""

    error_code = "payload_too_large"
    http_status = 413


class UnauthorizedError(SalesNavIntakeError):
    """The request came from a disallowed origin or non-local environment."""

    error_code = "unauthorized"
    http_status = 403


# --- Result ------------------------------------------------------------------


@dataclass
class StagingResult:
    """The outcome of staging (or idempotently replaying) one capture batch."""

    staging_id: str
    client_batch_id: str
    record_count: int
    warnings: list[dict[str, Any]]
    received_at: str
    expires_at: str
    operator_workbench_url: str
    already_received: bool
    http_status: int

    def to_body(self) -> dict[str, Any]:
        """Render the contract response body (``intake.response.schema.json``)."""

        return {
            "staging_id": self.staging_id,
            "client_batch_id": self.client_batch_id,
            "record_count": self.record_count,
            "warnings": self.warnings,
            "received_at": self.received_at,
            "expires_at": self.expires_at,
            "operator_workbench_url": self.operator_workbench_url,
            "already_received": self.already_received,
        }


# --- Validation helpers ------------------------------------------------------


@lru_cache(maxsize=1)
def _request_validator() -> Draft202012Validator:
    """Load and cache the committed contract validator (single source of truth)."""

    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _json_pointer(path: Any) -> str:
    """Render a jsonschema error path as ``records[3].salesNavLeadUrl``."""

    parts: list[str] = []
    for item in path:
        if isinstance(item, int):
            parts.append(f"[{item}]")
        elif parts:
            parts.append(f".{item}")
        else:
            parts.append(str(item))
    return "".join(parts) or "<root>"


def _check_version(payload: dict[str, Any]) -> None:
    """Reject an unsupported contract MAJOR before full schema validation.

    A same-MAJOR but not-yet-implemented MINOR/PATCH still falls through to the
    schema's ``const`` check (which currently pins the exact 1.0.0 shape), so an
    unimplemented version is never silently accepted.
    """

    version = payload.get("schema_version")
    if not isinstance(version, str):
        return  # schema validation reports the missing/invalid version
    match = _VERSION_RE.match(version)
    if match is None:
        return  # schema ``const`` check reports the malformed namespace/version
    major = int(match.group(1))
    if major != SUPPORTED_MAJOR:
        raise UnsupportedVersionError(
            f"unsupported contract version {version!r}",
            details=[
                f"schema_version {version!r} declares MAJOR {major}; this backend "
                f"supports {CONTRACT_NAMESPACE}/{SUPPORTED_MAJOR}.x"
            ],
        )


def _validate_schema(payload: dict[str, Any]) -> None:
    """Validate the body against the committed contract schema."""

    errors = sorted(_request_validator().iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        details = [f"{_json_pointer(err.path)}: {err.message}" for err in errors]
        raise ValidationFailedError("request body failed schema validation", details=details)


def _record_has_identity(record: dict[str, Any]) -> bool:
    return any(_non_empty(record.get(field)) for field in _IDENTITY_FIELDS)


def _non_empty(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _check_records_not_empty(records: list[dict[str, Any]]) -> None:
    """Reject records that carry no name or URL (contract: no empty records)."""

    empties = [
        f"records[{index}] has no name or URL (empty record not allowed)"
        for index, record in enumerate(records)
        if not _record_has_identity(record)
    ]
    if empties:
        raise ValidationFailedError("request contains empty records", details=empties)


def _resolve_campaign(session: Session, campaign_id: Any) -> Campaign:
    """Resolve the target campaign or raise ``campaign_invalid``.

    Staging requires a real, available campaign so the operator workbench has a
    home for the batch. ``null`` (allowed by the wire schema for the pre-selection
    dev case) is refused here: a batch cannot be staged into the backend without a
    chosen campaign. An archived campaign is treated as unavailable.
    """

    if campaign_id is None or (isinstance(campaign_id, str) and campaign_id.strip() == ""):
        raise CampaignInvalidError(
            "a campaign must be selected before a Sales Navigator batch can be staged"
        )
    try:
        campaign_uuid = uuid.UUID(str(campaign_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise CampaignInvalidError(
            f"campaign_id {campaign_id!r} is not a valid campaign id"
        ) from exc
    campaign = session.get(Campaign, campaign_uuid)
    if campaign is None:
        raise CampaignInvalidError(f"campaign {campaign_id!r} does not exist")
    if campaign.status == CampaignStatus.ARCHIVED:
        raise CampaignInvalidError(
            f"campaign {campaign_id!r} is archived and cannot receive a staged batch"
        )
    return campaign


# --- Persistence helpers -----------------------------------------------------


def _content_hash(payload: dict[str, Any]) -> str:
    """Stable hash of the whole batch, used to detect a reused-id/changed-body."""

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _batch_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Verbatim batch-level provenance preserved from the capture extension."""

    return {
        "schema_version": payload.get("schema_version"),
        "source": payload.get("source"),
        "captured_at": payload.get("captured_at"),
        "current_search_url": payload.get("current_search_url"),
        "extraction_metadata": payload.get("extraction_metadata"),
        "client_batch_id": payload.get("client_batch_id"),
        "record_count": len(payload.get("records") or []),
    }


def _audit_context(batch: ImportBatch, payload: dict[str, Any]) -> dict[str, Any]:
    """Safe audit context: identifiers and counts only, never raw records/secrets."""

    return {
        "campaign_id": str(batch.campaign_id),
        "client_batch_id": batch.client_batch_id,
        "record_count": batch.total_rows,
        "schema_version": payload.get("schema_version"),
        "source": payload.get("source"),
        "source_format": batch.source_format.value,
    }


def _workbench_url(operator_base_url: str, batch_id: uuid.UUID) -> str:
    return f"{operator_base_url.rstrip('/')}/imports/{batch_id}"


def _result_from_batch(
    batch: ImportBatch,
    *,
    already_received: bool,
    http_status: int,
    operator_base_url: str,
    received_at: datetime | None = None,
) -> StagingResult:
    received = received_at or batch.created_at or datetime.now(UTC)
    received = received.astimezone(UTC)
    return StagingResult(
        staging_id=str(batch.id),
        client_batch_id=batch.client_batch_id or "",
        record_count=batch.total_rows,
        warnings=[],
        received_at=received.isoformat(),
        expires_at=(received + timedelta(hours=STAGED_BATCH_TTL_HOURS)).isoformat(),
        operator_workbench_url=_workbench_url(operator_base_url, batch.id),
        already_received=already_received,
        http_status=http_status,
    )


def _find_by_client_batch_id(session: Session, client_batch_id: str) -> ImportBatch | None:
    return session.scalars(
        select(ImportBatch).where(ImportBatch.client_batch_id == client_batch_id)
    ).first()


# --- Entry point -------------------------------------------------------------


def stage_salesnav_batch(
    session: Session,
    *,
    payload: dict[str, Any],
    operator_base_url: str,
    actor: str = _SOURCE_ACTOR,
    _fault: Any = None,
) -> StagingResult:
    """Validate and stage one authorized Sales Navigator capture batch.

    Creates exactly one ``ImportBatch`` and its immutable raw ``ImportRow`` rows;
    creates zero contacts, companies, memberships, suppressions, scores, or
    outreach actions. Idempotent on ``client_batch_id``. Raises a
    :class:`SalesNavIntakeError` subclass for every deterministic failure.

    ``_fault`` is a test-only hook invoked after rows are written but before the
    staging transaction commits, used to prove that a mid-write failure rolls back
    to zero staged rows.
    """

    # --- Deterministic validation (no writes) --------------------------------
    _check_version(payload)
    _validate_schema(payload)
    records: list[dict[str, Any]] = payload["records"]
    _check_records_not_empty(records)
    campaign = _resolve_campaign(session, payload.get("campaign_id"))

    client_batch_id = str(payload["client_batch_id"])
    content_hash = _content_hash(payload)

    # --- Idempotency: same id + same body replays; changed body conflicts -----
    existing = _find_by_client_batch_id(session, client_batch_id)
    if existing is not None:
        if existing.content_hash == content_hash:
            return _result_from_batch(
                existing,
                already_received=True,
                http_status=200,
                operator_base_url=operator_base_url,
            )
        raise IdempotencyConflictError(
            f"client_batch_id {client_batch_id!r} was already staged with a different payload",
            details=[
                "reusing a client_batch_id requires an identical payload; clear the "
                "batch in the extension to stage new content"
            ],
        )

    # --- Stage: batch + immutable raw rows, atomically -----------------------
    received = datetime.now(UTC)
    try:
        batch = ImportBatch(
            campaign_id=campaign.id,
            client_batch_id=client_batch_id,
            content_hash=content_hash,
            status=ImportBatchStatus.PENDING,
            source_format=ImportSourceFormat.SALES_NAVIGATOR,
            mime_type="application/json",
            # The contract version doubles as the parser/interpreter version for
            # this source, so a staged batch records exactly how it was read.
            parser_version=str(payload.get("schema_version")),
            source_name=(payload.get("source") or None),
            source_reference=(payload.get("current_search_url") or None),
            source_metadata=_batch_metadata(payload),
            total_rows=len(records),
        )
        session.add(batch)
        session.flush()

        for index, record in enumerate(records, start=1):
            session.add(
                ImportRow(
                    batch_id=batch.id,
                    row_number=index,
                    sheet_index=0,
                    sheet_name="sales_navigator",
                    # Verbatim capture: the entire record, including its warnings,
                    # source page/position, timestamps, and URLs, exactly as sent.
                    raw_data=record,
                )
            )
        session.flush()

        record_audit_event(
            session,
            actor=actor,
            action="import.salesnav_staged",
            entity_type="import_batch",
            entity_id=str(batch.id),
            new_state=ImportBatchStatus.PENDING.value,
            reason="operator-authorized Sales Navigator capture staged for preview",
            context=_audit_context(batch, payload),
        )

        if _fault is not None:
            _fault()

        session.commit()
        return _result_from_batch(
            batch,
            already_received=False,
            http_status=201,
            operator_base_url=operator_base_url,
            received_at=received,
        )
    except IntegrityError:
        # A concurrent submission won the unique client_batch_id. Recover the
        # winner and reconcile: identical body replays, changed body conflicts.
        session.rollback()
        winner = _find_by_client_batch_id(session, client_batch_id)
        if winner is not None:
            if winner.content_hash == content_hash:
                return _result_from_batch(
                    winner,
                    already_received=True,
                    http_status=200,
                    operator_base_url=operator_base_url,
                )
            raise IdempotencyConflictError(
                f"client_batch_id {client_batch_id!r} was already staged with a different payload"
            ) from None
        raise
    except SalesNavIntakeError:
        raise
    except Exception:
        # Any mid-staging failure leaves nothing behind: no batch, no rows.
        session.rollback()
        raise

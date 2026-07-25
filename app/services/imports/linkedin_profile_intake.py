"""LinkedIn person-profile capture -> immutable snapshot intake (DAT-012D).

Backend adapter for the operator-driven LinkedIn profile capture mode of the
extension. It receives one reviewed, operator-approved snapshot of a manually
opened MAIN profile page and **persists it immutably**: one
:class:`~app.models.linkedin_profile.LinkedInProfileSnapshot` (verbatim payload
+ normalized identity URL + provenance) plus one
:class:`~app.models.linkedin_profile.LinkedInProfileExperienceObservation` per
nested experience entry — and nothing else.

It deliberately does NOT match identities, refresh contacts, resolve freshness,
enforce or bypass suppression, verify, score, or schedule anything. Canonical
reconciliation is a separate service (DAT-012E) that runs on stored snapshots
and records a truthful outcome back onto the snapshot row. The baseline ingest
outcome here is always ``stored``.

The request body is validated against the extension's committed contract schema
(``extensions/salesnav-capture/docs/profile-intake.schema.json``, contract
version ``linkedin-profile-capture/1.0.0``) — the single source of truth for
the wire shape. Mirrors the Sales Navigator intake's discipline: local-only,
idempotent on the client-minted capture id, deterministic typed errors, timeout
bounded, PII-free audit trail.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CampaignStatus, LinkedInSnapshotOutcome
from app.models.linkedin_profile import (
    LinkedInProfileExperienceObservation,
    LinkedInProfileSnapshot,
)
from app.services.audit import record_audit_event
from app.services.imports.normalization import normalize_linkedin_profile_url

# --- Contract constants ------------------------------------------------------

CONTRACT_NAMESPACE = "linkedin-profile-capture"
SUPPORTED_MAJOR = 1
SCHEMA_VERSION = f"{CONTRACT_NAMESPACE}/1.0.0"

_SOURCE_ACTOR = "linkedin-profile-capture"
_VERSION_RE = re.compile(rf"^{re.escape(CONTRACT_NAMESPACE)}/(\d+)\.(\d+)\.(\d+)$")

INTAKE_ROUTE = "/api/intake/linkedin-profile/stage"
SUCCESS_AUDIT_ACTION = "intake.linkedin_profile_staged"
FAILURE_AUDIT_ACTION = "intake.linkedin_profile_stage_failed"
INTAKE_ENTITY_TYPE = "linkedin_profile_intake"
INTAKE_SOURCE_ID = "linkedin_profile_intake"

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3]
    / "extensions"
    / "salesnav-capture"
    / "docs"
    / "profile-intake.schema.json"
)


# --- Error hierarchy ---------------------------------------------------------


class ProfileIntakeError(Exception):
    """Base class for deterministic, client-facing profile-intake failures."""

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


class InvalidJsonError(ProfileIntakeError):
    error_code = "invalid_json"
    http_status = 400


class ValidationFailedError(ProfileIntakeError):
    error_code = "validation_failed"
    http_status = 422


class UnsupportedVersionError(ValidationFailedError):
    """The payload declares an unsupported contract MAJOR version."""


class CampaignInvalidError(ProfileIntakeError):
    error_code = "campaign_invalid"
    http_status = 409


class IdempotencyConflictError(ProfileIntakeError):
    """The ``client_capture_id`` was already staged with a different payload."""

    error_code = "client_capture_id_conflict"
    http_status = 409


class PayloadTooLargeError(ProfileIntakeError):
    error_code = "payload_too_large"
    http_status = 413


class UnauthorizedError(ProfileIntakeError):
    error_code = "unauthorized"
    http_status = 403


class IntakeTimeoutError(ProfileIntakeError):
    error_code = "timeout"
    http_status = 504


# --- Result ------------------------------------------------------------------


@dataclass
class SnapshotResult:
    """The outcome of staging (or idempotently replaying) one profile capture."""

    snapshot_id: str
    client_capture_id: str
    outcome: str
    warnings: list[dict[str, Any]]
    received_at: str
    operator_workbench_url: str
    already_received: bool
    http_status: int

    def to_body(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "client_capture_id": self.client_capture_id,
            "outcome": self.outcome,
            "warnings": self.warnings,
            "received_at": self.received_at,
            "operator_workbench_url": self.operator_workbench_url,
            "already_received": self.already_received,
        }


# --- Validation helpers ------------------------------------------------------


@lru_cache(maxsize=1)
def _request_validator() -> Draft202012Validator:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _json_pointer(path: Any) -> str:
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
    version = payload.get("schema_version")
    if not isinstance(version, str):
        return
    match = _VERSION_RE.match(version)
    if match is None:
        return
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
    errors = sorted(_request_validator().iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        details = [f"{_json_pointer(err.path)}: {err.message}" for err in errors]
        raise ValidationFailedError("request body failed schema validation", details=details)


def _require_identity_url(payload: dict[str, Any]) -> str:
    """The one non-negotiable semantic rule: a usable normalized profile URL.

    The wire schema already requires a ``linkedin.com/in/`` shaped string; here
    the backend applies its authoritative normalization and refuses a snapshot
    whose URL does not normalize to a MAIN profile identity — without an exact
    identity key the snapshot could never be matched or reviewed meaningfully.
    """

    profile = payload.get("profile") or {}
    normalized = normalize_linkedin_profile_url(profile.get("linkedin_profile_url"))
    if normalized is None:
        raise ValidationFailedError(
            "profile.linkedin_profile_url does not normalize to a main profile URL",
            details=[
                "profile.linkedin_profile_url must normalize to "
                "https://www.linkedin.com/in/<public-identifier>"
            ],
        )
    return normalized


def _resolve_campaign(session: Session, campaign_id: Any) -> Campaign | None:
    """Resolve the optional campaign or raise ``campaign_invalid``.

    Unlike a Sales Navigator batch (which stages into a campaign's import
    workbench), a profile snapshot is contact evidence: a campaign selection is
    optional context. ``null`` is accepted; a NON-null id must exist and not be
    archived — a dangling reference is refused rather than silently dropped.
    """

    if campaign_id is None or (isinstance(campaign_id, str) and campaign_id.strip() == ""):
        return None
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
            f"campaign {campaign_id!r} is archived and cannot receive capture evidence"
        )
    return campaign


# --- Persistence helpers -----------------------------------------------------


def _content_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _date_part(value: Any, key: str) -> int | None:
    if isinstance(value, dict):
        part = value.get(key)
        if isinstance(part, int):
            return part
    return None


def _audit_context(snapshot: LinkedInProfileSnapshot, payload: dict[str, Any]) -> dict[str, Any]:
    """Safe audit context: identifiers, counts and status only — never captured
    names, headlines, URLs' query context, or any raw page content."""

    return {
        "snapshot_id": str(snapshot.id),
        "client_capture_id": snapshot.client_capture_id,
        "schema_version": payload.get("schema_version"),
        "source": payload.get("source"),
        "extraction_status": snapshot.extraction_status,
        "experience_count": len(payload.get("experiences") or []),
        "outcome": snapshot.outcome.value,
        "campaign_id": str(snapshot.campaign_id) if snapshot.campaign_id else None,
    }


# --- Failure auditing --------------------------------------------------------


def _capture_id_fingerprint(payload: Any) -> tuple[bool, str | None]:
    if isinstance(payload, dict):
        cid = payload.get("client_capture_id")
        if isinstance(cid, str) and cid.strip():
            return True, hashlib.sha256(cid.encode("utf-8")).hexdigest()[:16]
    return False, None


def build_failure_context(*, error_code: str, http_status: int, payload: Any) -> dict[str, Any]:
    """Deterministic, PII-free audit context for a rejected profile intake."""

    present, fingerprint = _capture_id_fingerprint(payload)
    context: dict[str, Any] = {
        "error_code": error_code,
        "http_status": http_status,
        "route": INTAKE_ROUTE,
        "source": INTAKE_SOURCE_ID,
        "client_capture_id_present": present,
    }
    if fingerprint is not None:
        context["client_capture_id_fingerprint"] = fingerprint
    if isinstance(payload, dict) and isinstance(payload.get("experiences"), list):
        context["experience_count"] = len(payload["experiences"])
    return context


def record_intake_failure(
    session: Session, *, error: ProfileIntakeError, payload: Any = None
) -> None:
    """Best-effort, safe audit of a rejected intake (fail-open, mirrors DAT-009)."""

    try:
        session.rollback()
        record_audit_event(
            session,
            actor=_SOURCE_ACTOR,
            action=FAILURE_AUDIT_ACTION,
            entity_type=INTAKE_ENTITY_TYPE,
            entity_id=None,
            new_state="rejected",
            reason=f"linkedin profile intake rejected: {error.error_code}",
            context=build_failure_context(
                error_code=error.error_code,
                http_status=error.http_status,
                payload=payload,
            ),
        )
        session.commit()
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass


# --- Deadline enforcement ----------------------------------------------------

_CLOCK_OVERRIDE: Callable[[], float] | None = None


def _now() -> float:
    return (_CLOCK_OVERRIDE or time.monotonic)()


class _Deadline:
    def __init__(self, timeout_seconds: float) -> None:
        self._end = _now() + timeout_seconds

    def check(self) -> None:
        if _now() >= self._end:
            raise IntakeTimeoutError("linkedin profile intake exceeded its time budget")


def _is_query_canceled(exc: OperationalError) -> bool:
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    return sqlstate == "57014"


def _apply_statement_timeout(session: Session, timeout_seconds: float) -> None:
    if timeout_seconds > 0:
        millis = max(1, int(timeout_seconds * 1000))
        session.execute(text(f"SET LOCAL statement_timeout = {millis}"))


def _workbench_url(operator_base_url: str, snapshot_id: uuid.UUID) -> str:
    return f"{operator_base_url.rstrip('/')}/profiles/{snapshot_id}"


def _result_from_snapshot(
    snapshot: LinkedInProfileSnapshot,
    *,
    already_received: bool,
    http_status: int,
    operator_base_url: str,
    received_at: datetime | None = None,
) -> SnapshotResult:
    received = received_at or snapshot.ingested_at or datetime.now(UTC)
    received = received.astimezone(UTC)
    return SnapshotResult(
        snapshot_id=str(snapshot.id),
        client_capture_id=snapshot.client_capture_id,
        outcome=snapshot.outcome.value,
        warnings=[],
        received_at=received.isoformat(),
        operator_workbench_url=_workbench_url(operator_base_url, snapshot.id),
        already_received=already_received,
        http_status=http_status,
    )


def _find_by_capture_id(session: Session, client_capture_id: str) -> LinkedInProfileSnapshot | None:
    return session.scalars(
        select(LinkedInProfileSnapshot).where(
            LinkedInProfileSnapshot.client_capture_id == client_capture_id
        )
    ).first()


# --- Entry point -------------------------------------------------------------


def stage_profile_snapshot(
    session: Session,
    *,
    payload: dict[str, Any],
    operator_base_url: str,
    timeout_seconds: float = 15.0,
    actor: str = _SOURCE_ACTOR,
    reconcile: bool = False,
    _fault: Any = None,
) -> SnapshotResult:
    """Validate and persist one reviewed LinkedIn profile capture, immutably.

    Creates exactly one snapshot row and its nested experience observations.
    Idempotent on ``client_capture_id`` (same id + same content replays; same
    id + different content conflicts). Raises a :class:`ProfileIntakeError`
    subclass for every deterministic failure. ``_fault`` is a test-only hook
    proving mid-write rollback.

    With ``reconcile=False`` (the default and the DAT-012D baseline) nothing
    else happens and the outcome is ``stored``. With ``reconcile=True``
    (DAT-012E, gated by the ``linkedin_profile_refresh`` feature switch) the
    stored snapshot is immediately reconciled in the same transaction: exact
    normalized-URL matching, DAT-005 freshness refresh, DAT-006 suppression
    enforcement, review-only weak candidates, and the QA-policy evaluation for
    a matched contact. Reconciliation still never creates/merges contacts on
    weak evidence and never touches suppression, verification, approval, or
    scheduling state.
    """

    deadline = _Deadline(timeout_seconds)

    # --- Deterministic validation (no writes) --------------------------------
    _check_version(payload)
    _validate_schema(payload)
    normalized_url = _require_identity_url(payload)
    campaign = _resolve_campaign(session, payload.get("campaign_id"))

    client_capture_id = str(payload["client_capture_id"])
    content_hash = _content_hash(payload)

    # --- Idempotency ----------------------------------------------------------
    existing = _find_by_capture_id(session, client_capture_id)
    if existing is not None:
        if existing.content_hash == content_hash:
            return _result_from_snapshot(
                existing,
                already_received=True,
                http_status=200,
                operator_base_url=operator_base_url,
            )
        raise IdempotencyConflictError(
            f"client_capture_id {client_capture_id!r} was already staged with a different payload",
            details=[
                "reusing a client_capture_id requires an identical payload; re-capture "
                "the profile in the extension to stage new content"
            ],
        )

    # --- Persist: snapshot + nested observations, atomically ------------------
    received = datetime.now(UTC)
    profile = payload["profile"]
    extraction = payload["extraction"]
    experiences: list[dict[str, Any]] = payload.get("experiences") or []
    try:
        _apply_statement_timeout(session, timeout_seconds)
        deadline.check()

        snapshot = LinkedInProfileSnapshot(
            client_capture_id=client_capture_id,
            content_hash=content_hash,
            schema_version=str(payload.get("schema_version")),
            source=str(payload.get("source")),
            source_url=payload.get("source_url"),
            normalized_profile_url=normalized_url,
            public_identifier=profile.get("public_identifier"),
            campaign_id=campaign.id if campaign is not None else None,
            captured_at=_parse_dt(payload.get("captured_at")),
            extraction_status=str(extraction.get("status")),
            adapter_version=extraction.get("adapter_version"),
            missing_sections=extraction.get("missing_sections"),
            page_warnings=extraction.get("page_warnings"),
            payload=payload,
            profile_fields=profile,
            outcome=LinkedInSnapshotOutcome.STORED,
        )
        session.add(snapshot)
        session.flush()

        for entry in experiences:
            session.add(
                LinkedInProfileExperienceObservation(
                    snapshot_id=snapshot.id,
                    position_index=int(entry["position_index"]),
                    layout=str(entry["layout"]),
                    company_name=entry.get("company_name"),
                    company_linkedin_url=entry.get("company_linkedin_url"),
                    company_linkedin_id=entry.get("company_linkedin_id"),
                    job_title=entry.get("job_title"),
                    timeline_text=entry.get("timeline_text"),
                    duration_text=entry.get("duration_text"),
                    start_year=_date_part(entry.get("start_date"), "year"),
                    start_month=_date_part(entry.get("start_date"), "month"),
                    end_year=_date_part(entry.get("end_date"), "year"),
                    end_month=_date_part(entry.get("end_date"), "month"),
                    dates_reliable=bool(entry.get("dates_reliable")),
                    employment_type=entry.get("employment_type"),
                    role_location=entry.get("role_location"),
                    workplace_type=entry.get("workplace_type"),
                    is_current=entry.get("is_current"),
                    observed_at=_parse_dt(entry.get("observed_at")),
                    raw_lines=entry.get("raw_lines"),
                    warnings=entry.get("warnings"),
                )
            )
        session.flush()

        deadline.check()

        record_audit_event(
            session,
            actor=actor,
            action=SUCCESS_AUDIT_ACTION,
            entity_type="linkedin_profile_snapshot",
            entity_id=str(snapshot.id),
            new_state=LinkedInSnapshotOutcome.STORED.value,
            reason="operator-authorized LinkedIn profile capture stored immutably",
            context=_audit_context(snapshot, payload),
        )

        if reconcile:
            # DAT-012E: reconcile in the same transaction so the response's
            # outcome is truthful and a failure rolls the whole intake back.
            from app.services.profiles.refresh import reconcile_snapshot
            from app.services.qa.policy import evaluate_contact_snapshot

            deadline.check()
            reconcile_result = reconcile_snapshot(session, snapshot)
            if snapshot.matched_contact_id is not None:
                matched = session.get(Contact, snapshot.matched_contact_id)
                if matched is not None:
                    evaluate_contact_snapshot(session, snapshot=snapshot, contact=matched)
            del reconcile_result  # outcome now lives on the snapshot row

        if _fault is not None:
            _fault()

        deadline.check()
        session.commit()
        return _result_from_snapshot(
            snapshot,
            already_received=False,
            http_status=201,
            operator_base_url=operator_base_url,
            received_at=received,
        )
    except IntegrityError:
        session.rollback()
        winner = _find_by_capture_id(session, client_capture_id)
        if winner is not None:
            if winner.content_hash == content_hash:
                return _result_from_snapshot(
                    winner,
                    already_received=True,
                    http_status=200,
                    operator_base_url=operator_base_url,
                )
            raise IdempotencyConflictError(
                f"client_capture_id {client_capture_id!r} was already staged with a "
                "different payload"
            ) from None
        raise
    except OperationalError as exc:
        session.rollback()
        if _is_query_canceled(exc):
            raise IntakeTimeoutError("linkedin profile intake exceeded its time budget") from exc
        raise
    except ProfileIntakeError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise

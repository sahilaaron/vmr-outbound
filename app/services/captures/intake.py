"""Contact-first capture intake (DAT-013).

The backend adapter for the contact-acquisition edge of the system. It receives
ONE operator-reviewed submission — ``linkedin-contact-capture/2.1.0`` — carrying
one or more people the operator deliberately opened or selected on LinkedIn or
Sales Navigator, and persists each of them as permanent, immutable capture
evidence.

Acquisition never requires a Campaign. Version 2.1 adds an optional filing
shortcut: capture still commits permanent evidence first, while Campaign Contact
enrolment is an idempotent, isolated action that cannot erase the capture.

What one accepted submission does:

* creates one :class:`~app.models.contact_capture.ContactCaptureSubmission`
  (the idempotency anchor and the operator's labels/note, verbatim);
* creates one immutable
  :class:`~app.models.linkedin_profile.LinkedInProfileSnapshot` per captured
  person, plus its nested experience observations;
* refreshes a permanent :class:`~app.models.contact.Contact` only when an
  exact existing LinkedIn identity is already known; otherwise the capture stays
  staged until domain resolution and promotion create the Contact;
* reconciles identifiers through the existing exact LinkedIn identity rules,
  while weaker similarity remains review-only and never merges people;
* applies the operator's labels to a matched, unsuppressed contact;
* appends operator notes (never overwrites an earlier one).

What it deliberately never does: require Campaign selection, qualify, discover
or verify an email, research a company, approve or schedule outreach, remove a
suppression, or make any contact sending-eligible. An unmatched capture remains
staged until the existing resolution and promotion path can establish a Contact.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.contact import Contact
from app.models.contact_capture import (
    NOTE_SCOPE_CONTACT,
    NOTE_SCOPE_SUBMISSION,
    ContactCaptureNote,
    ContactCaptureSubmission,
)
from app.models.enums import LinkedInIdentifierKind, LinkedInSnapshotOutcome
from app.models.linkedin_profile import (
    LinkedInProfileExperienceObservation,
    LinkedInProfileSnapshot,
)
from app.services import identity_links
from app.services.audit import record_audit_event
from app.services.captures import campaign_filing
from app.services.captures import labels as labels_service
from app.services.imports.normalization import (
    normalize_linkedin_profile_url,
    normalize_linkedin_url,
    normalize_name,
    normalize_text,
)
from app.services.profiles import refresh as refresh_service
from app.services.provenance import service as provenance
from app.services.suppressions import evaluate_suppression

# --- Contract constants ------------------------------------------------------

CONTRACT_NAMESPACE = "linkedin-contact-capture"
SUPPORTED_MAJOR = 2
SCHEMA_VERSION = f"{CONTRACT_NAMESPACE}/2.1.0"
SOURCE_IDENTIFIER = "chrome-extension:linkedin-contact-capture"

CAPTURE_MODE_PROFILE = "linkedin_profile"
CAPTURE_MODE_SALESNAV = "salesnav_people_search"

INTAKE_ROUTE = "/api/intake/contact-captures"
SUCCESS_AUDIT_ACTION = "intake.contact_capture_submitted"
FAILURE_AUDIT_ACTION = "intake.contact_capture_rejected"
INTAKE_ENTITY_TYPE = "contact_capture_intake"
INTAKE_SOURCE_ID = "contact_capture_intake"

_SOURCE_ACTOR = "linkedin-contact-capture"
_VERSION_RE = re.compile(rf"^{re.escape(CONTRACT_NAMESPACE)}/(\d+)\.(\d+)\.(\d+)$")

# The legacy, campaign-era contracts. A payload declaring one of these is
# refused here with a pointer to its own route — never silently reinterpreted as
# a contact-first submission.
LEGACY_CONTRACT_ROUTES = {
    "linkedin-profile-capture": "/api/intake/linkedin-profile/stage",
    "salesnav-capture": "/api/intake/sales-navigator/stage",
    "linkedin-company-capture": "/api/intake/linkedin-company/stage",
}

#: Why a newly captured Contact may remain downstream-blocked.
CANONICAL_CREATION_NOTE = (
    "capture preserves evidence and refreshes exact existing identities; unmatched "
    "people stay staged until promotion can create a Contact without guessing"
)

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3]
    / "extensions"
    / "salesnav-capture"
    / "docs"
    / "contact-capture.schema.json"
)


# --- Error hierarchy ---------------------------------------------------------


class ContactCaptureError(Exception):
    """Base class for deterministic, client-facing contact-capture failures."""

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


class InvalidJsonError(ContactCaptureError):
    error_code = "invalid_json"
    http_status = 400


class ValidationFailedError(ContactCaptureError):
    error_code = "validation_failed"
    http_status = 422


class UnsupportedContractError(ValidationFailedError):
    """The payload declares a contract this endpoint does not accept."""

    error_code = "unsupported_contract"


class IdempotencyConflictError(ContactCaptureError):
    """A client-minted id was reused with different content."""

    error_code = "client_submission_id_conflict"
    http_status = 409


class CaptureIdConflictError(ContactCaptureError):
    """A ``client_capture_id`` already belongs to a different submission."""

    error_code = "client_capture_id_conflict"
    http_status = 409


class PayloadTooLargeError(ContactCaptureError):
    error_code = "payload_too_large"
    http_status = 413


class UnauthorizedError(ContactCaptureError):
    error_code = "unauthorized"
    http_status = 403


class IntakeTimeoutError(ContactCaptureError):
    error_code = "timeout"
    http_status = 504


# --- Results -----------------------------------------------------------------

# Bounds on the optional automatic-resolution pass inside an intake request.
# A submission's whole budget belongs to staging; resolution may use a share of
# whatever is left and no more. The provider cap matters independently of the
# clock: a fast provider should not turn one submission into 500 outbound lookups.
_RESOLUTION_BUDGET_SHARE = 0.4
_RESOLUTION_MAX_SECONDS = 15.0
_RESOLUTION_MAX_PROVIDER_CALLS = 10

_COUNT_KEYS = (
    "submitted",
    "created",
    "refreshed_exact_match",
    "exact_match_unchanged",
    "staged_unmatched",
    "staged_ambiguous",
    "duplicate_in_submission",
    "suppressed",
    "labels_applied",
    "notes_recorded",
    "campaign_filings_applied",
    "campaign_filings_pending",
    "campaign_filings_failed",
    # Captures the automatic company-domain policy finished without an operator.
    "auto_resolved",
)

# Truthful outcome -> the response counter it increments.
_OUTCOME_COUNTER = {
    LinkedInSnapshotOutcome.CONTACT_CREATED: "created",
    LinkedInSnapshotOutcome.EXACT_MATCH_REFRESHED: "refreshed_exact_match",
    LinkedInSnapshotOutcome.EXACT_MATCH_UNCHANGED: "exact_match_unchanged",
    LinkedInSnapshotOutcome.UNMATCHED_STAGED: "staged_unmatched",
    LinkedInSnapshotOutcome.AMBIGUOUS_REVIEW: "staged_ambiguous",
    LinkedInSnapshotOutcome.DUPLICATE_IN_SUBMISSION: "duplicate_in_submission",
    LinkedInSnapshotOutcome.SUPPRESSED: "suppressed",
    LinkedInSnapshotOutcome.STORED: "staged_unmatched",
}


@dataclass
class CaptureOutcome:
    """The truthful per-person outcome of one capture."""

    client_capture_id: str
    capture_id: str | None
    outcome: str
    matched_contact_id: str | None = None
    contact_url: str | None = None
    capture_url: str | None = None
    review_candidate_count: int = 0
    labels_applied: list[str] = field(default_factory=list)
    campaign_filing: dict[str, Any] | None = None
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_body(self) -> dict[str, Any]:
        return {
            "client_capture_id": self.client_capture_id,
            "capture_id": self.capture_id,
            "outcome": self.outcome,
            "matched_contact_id": self.matched_contact_id,
            "contact_url": self.contact_url,
            "capture_url": self.capture_url,
            "review_candidate_count": self.review_candidate_count,
            "labels_applied": self.labels_applied,
            "campaign_filing": self.campaign_filing,
            "warnings": self.warnings,
        }


@dataclass
class SubmissionResult:
    """The outcome of accepting (or replaying) one contact-capture submission."""

    submission_id: str
    client_submission_id: str
    received_at: str
    already_received: bool
    counts: dict[str, int]
    results: list[CaptureOutcome]
    operator_workbench_url: str | None
    http_status: int

    def to_body(self) -> dict[str, Any]:
        return {
            "submission_id": self.submission_id,
            "client_submission_id": self.client_submission_id,
            "received_at": self.received_at,
            "already_received": self.already_received,
            "counts": self.counts,
            "results": [r.to_body() for r in self.results],
            "operator_workbench_url": self.operator_workbench_url,
        }


# --- Validation --------------------------------------------------------------


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


def _check_contract(payload: dict[str, Any]) -> None:
    """Reject a foreign or unsupported contract before schema validation.

    A legacy campaign-era payload gets a pointer to its own route rather than a
    confusing field-by-field schema failure — the transition is explicit, and no
    legacy body is ever reinterpreted as a contact-first submission.
    """

    version = payload.get("schema_version")
    if not isinstance(version, str):
        return  # schema validation reports the missing/invalid version
    namespace = version.split("/", 1)[0]
    if namespace in LEGACY_CONTRACT_ROUTES:
        raise UnsupportedContractError(
            f"contract {version!r} is not accepted by the contact-capture endpoint",
            details=[
                f"{version!r} is the legacy campaign-era contract; post it to "
                f"{LEGACY_CONTRACT_ROUTES[namespace]} or upgrade the extension to "
                f"{SCHEMA_VERSION}"
            ],
        )
    match = _VERSION_RE.match(version)
    if match is None:
        return  # the schema's ``const`` check reports the malformed version
    major = int(match.group(1))
    if major != SUPPORTED_MAJOR:
        raise UnsupportedContractError(
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


def _non_empty(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _check_identity_signals(contacts: list[dict[str, Any]]) -> None:
    """Every capture must carry at least one visible identity signal.

    A profile URL, a Sales Navigator lead URL, or a name. A capture with none of
    those is an empty record: it can never be reviewed or matched, so it is
    refused rather than stored as noise.
    """

    problems: list[str] = []
    for index, capture in enumerate(contacts):
        person = capture.get("person") or {}
        if any(
            _non_empty(person.get(f))
            for f in ("linkedin_profile_url", "salesnav_lead_url", "full_name")
        ):
            continue
        problems.append(
            f"contacts[{index}] has no profile URL, lead URL, or name (empty capture not allowed)"
        )
    if problems:
        raise ValidationFailedError("submission contains empty captures", details=problems)


def _check_capture_ids_unique(contacts: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    duplicates: list[str] = []
    for index, capture in enumerate(contacts):
        cid = str(capture.get("client_capture_id"))
        if cid in seen:
            duplicates.append(f"contacts[{index}].client_capture_id {cid!r} is repeated")
        seen.add(cid)
    if duplicates:
        raise ValidationFailedError(
            "client_capture_id must be unique within a submission", details=duplicates
        )


# --- Identity ----------------------------------------------------------------


def identity_key(person: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return ``(normalized_profile_url, in_submission_dedup_key)``.

    Only a normalized MAIN profile URL is a canonical identity. A Sales
    Navigator lead URL is enough to recognise the same row twice inside one
    submission, but it can never match a stored contact — the uncertainty is
    preserved, not repaired.
    """

    normalized = normalize_linkedin_profile_url(person.get("linkedin_profile_url"))
    if normalized is not None:
        return normalized, normalized
    lead = normalize_linkedin_url(person.get("salesnav_lead_url"))
    if lead is not None:
        return None, f"lead:{lead}"
    return None, None


# --- Projections --------------------------------------------------------------


# The canonical projection stored on ``profile_fields``. Downstream services
# (freshness refresh, the QA policy, the capture pages) read these keys for
# every capture regardless of the surface it came from.
def _profile_projection(person: dict[str, Any], normalized_url: str | None) -> dict[str, Any]:
    return {
        "linkedin_profile_url": normalized_url or person.get("linkedin_profile_url"),
        "public_identifier": person.get("linkedin_public_identifier"),
        "salesnav_lead_url": person.get("salesnav_lead_url"),
        "salesnav_member_id": person.get("salesnav_member_id"),
        # DAT-020A. Derived navigation alias, projected under its own key so a
        # reader of profile_fields cannot mistake it for the observed handle.
        "salesnav_alias_url": person.get("salesnav_alias_url"),
        "full_name": person.get("full_name"),
        "first_name": person.get("first_name"),
        "last_name": person.get("last_name"),
        "headline": person.get("headline"),
        "displayed_location": person.get("location"),
        "connection_count": person.get("connection_count"),
        "connection_count_raw": person.get("connection_count_raw"),
        "open_to_work": person.get("open_to_work_visible"),
        "about_text": person.get("about_text"),
        "raw_lines": person.get("raw_lines") or [],
        "warnings": person.get("warnings") or [],
    }


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


# --- Failure auditing --------------------------------------------------------


def build_failure_context(*, error_code: str, http_status: int, payload: Any) -> dict[str, Any]:
    """Deterministic, PII-free audit context for a rejected submission."""

    present = False
    fingerprint: str | None = None
    if isinstance(payload, dict):
        sid = payload.get("client_submission_id")
        if isinstance(sid, str) and sid.strip():
            present = True
            fingerprint = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:16]
    context: dict[str, Any] = {
        "error_code": error_code,
        "http_status": http_status,
        "route": INTAKE_ROUTE,
        "source": INTAKE_SOURCE_ID,
        "client_submission_id_present": present,
    }
    if fingerprint is not None:
        context["client_submission_id_fingerprint"] = fingerprint
    if isinstance(payload, dict):
        if isinstance(payload.get("contacts"), list):
            context["contact_count"] = len(payload["contacts"])
        if isinstance(payload.get("capture_mode"), str):
            context["capture_mode"] = payload["capture_mode"]
    return context


def record_intake_failure(
    session: Session, *, error: ContactCaptureError, payload: Any = None
) -> None:
    """Best-effort, safe audit of a rejected submission (fail-open)."""

    try:
        session.rollback()
        record_audit_event(
            session,
            actor=_SOURCE_ACTOR,
            action=FAILURE_AUDIT_ACTION,
            entity_type=INTAKE_ENTITY_TYPE,
            entity_id=None,
            new_state="rejected",
            reason=f"contact capture submission rejected: {error.error_code}",
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


# --- Deadline ----------------------------------------------------------------

_CLOCK_OVERRIDE: Callable[[], float] | None = None


def _now() -> float:
    return (_CLOCK_OVERRIDE or time.monotonic)()


class _Deadline:
    def __init__(self, timeout_seconds: float) -> None:
        self._end = _now() + timeout_seconds

    def check(self) -> None:
        if _now() >= self._end:
            raise IntakeTimeoutError("contact capture intake exceeded its time budget")

    def remaining_seconds(self) -> float:
        """How much budget is left, for work that must yield rather than fail.

        Staging is the valuable part of a submission and it is finished by the time
        anything optional runs. Optional work therefore asks how much time it has
        and stops, instead of calling :meth:`check` and converting "we ran out of
        time on an extra" into "discard everything that already succeeded".
        """

        return max(0.0, self._end - _now())


def _is_query_canceled(exc: OperationalError) -> bool:
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    return sqlstate == "57014"


def _apply_statement_timeout(session: Session, timeout_seconds: float) -> None:
    if timeout_seconds > 0:
        millis = max(1, int(timeout_seconds * 1000))
        session.execute(text(f"SET LOCAL statement_timeout = {millis}"))


# --- URLs --------------------------------------------------------------------


def capture_url(operator_base_url: str, capture_id: uuid.UUID | str) -> str:
    return f"{operator_base_url.rstrip('/')}/contact-captures/{capture_id}"


def submission_url(operator_base_url: str, submission_id: uuid.UUID | str) -> str:
    return f"{operator_base_url.rstrip('/')}/contact-captures/submissions/{submission_id}"


def contact_url(operator_base_url: str, contact_id: uuid.UUID | str) -> str:
    return f"{operator_base_url.rstrip('/')}/contacts/{contact_id}"


# --- Persistence helpers ------------------------------------------------------


def _build_snapshot(
    *,
    capture: dict[str, Any],
    payload: dict[str, Any],
    submission: ContactCaptureSubmission,
    normalized_url: str | None,
    requested_labels: list[str],
) -> LinkedInProfileSnapshot:
    person = capture.get("person") or {}
    extraction = capture.get("extraction") or {}
    source = capture.get("source") or {}
    return LinkedInProfileSnapshot(
        client_capture_id=str(capture["client_capture_id"]),
        content_hash=_content_hash(capture),
        schema_version=str(payload.get("schema_version")),
        source=str(payload.get("source")),
        source_url=source.get("url"),
        normalized_profile_url=normalized_url,
        public_identifier=person.get("linkedin_public_identifier"),
        campaign_id=None,
        submission_id=submission.id,
        capture_mode=str(payload.get("capture_mode")),
        source_surface=source.get("surface"),
        salesnav_lead_url=person.get("salesnav_lead_url"),
        # DAT-019: stored verbatim. The member id is case-sensitive and must not
        # travel through the URL normalizer, which lowercases slugs.
        salesnav_member_id=person.get("salesnav_member_id"),
        # DAT-020A: stored as evidence only. Deliberately NOT fed to
        # ``normalized_profile_url`` above — that field takes a directly
        # observed handle or stays null, and a derived alias is neither.
        salesnav_alias_url=person.get("salesnav_alias_url"),
        # Only a link actually on the page counts as observed. The extension no
        # longer synthesises one, so a null URL here is honest uncertainty.
        profile_url_source="observed" if normalized_url else None,
        operator_labels=requested_labels or None,
        captured_at=_parse_dt(capture.get("captured_at")),
        extraction_status=str(extraction.get("status")),
        adapter_version=extraction.get("adapter_version"),
        missing_sections=extraction.get("missing_sections"),
        page_warnings=extraction.get("page_warnings"),
        payload=capture,
        profile_fields=_profile_projection(person, normalized_url),
        outcome=LinkedInSnapshotOutcome.STORED,
    )


def _add_experience_rows(session: Session, snapshot: LinkedInProfileSnapshot, capture: Any) -> None:
    for entry in capture.get("experience_observations") or []:
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


def _stage_without_identity(
    session: Session, snapshot: LinkedInProfileSnapshot
) -> list[dict[str, Any]]:
    """Outcome for a capture with no canonical profile URL (a results row).

    The person is preserved permanently, and any same-name contacts are surfaced
    as review candidates — but nothing matches, merges, or refreshes. Uncertain
    identity stays uncertain.
    """

    candidates = refresh_service.find_review_candidates(session, snapshot)
    snapshot.outcome = LinkedInSnapshotOutcome.UNMATCHED_STAGED
    snapshot.reconciled_at = datetime.now(UTC)
    snapshot.review_candidates = candidates or None
    snapshot.refresh_summary = {
        "outcome": LinkedInSnapshotOutcome.UNMATCHED_STAGED.value,
        "matched_contact_id": None,
        "refreshed_fields": [],
        "unchanged_fields": [],
        "skipped_fields": {
            "*": "no canonical LinkedIn profile URL was visible; identity stays uncertain"
        },
        "review_candidate_count": len(candidates),
        "suppression_reason": None,
    }
    session.flush()
    return candidates


def _capture_contact_values(capture: dict[str, Any]) -> dict[str, str | None]:
    """Project only directly observed person/employment values onto Contact."""

    person = capture.get("person") or {}
    current = next(
        (
            item
            for item in (capture.get("experience_observations") or [])
            if item.get("is_current") is True
        ),
        None,
    )
    hint = current or capture.get("current_employment_hint") or {}
    return {
        "first_name": normalize_name(person.get("first_name")),
        "last_name": normalize_name(person.get("last_name")),
        "company_name": normalize_name(hint.get("company_name")),
        "title": normalize_text(
            hint.get("job_title") or hint.get("title") or person.get("headline")
        ),
        "linkedin_url": normalize_linkedin_profile_url(person.get("linkedin_profile_url")),
    }


def _record_capture_observations(
    session: Session,
    *,
    contact: Contact,
    snapshot: LinkedInProfileSnapshot,
    values: dict[str, str | None],
    actor: str,
) -> list[str]:
    """Append descriptive observations and fill missing observed name parts."""

    changed: list[str] = []
    for field_name in ("first_name", "last_name"):
        value = values[field_name]
        if getattr(contact, field_name) is None and value is not None:
            setattr(contact, field_name, value)
            changed.append(field_name)
    observed_at = snapshot.captured_at or snapshot.ingested_at or datetime.now(UTC)
    for field_name in ("title", "company_name", "linkedin_url"):
        value = values[field_name]
        if value is None:
            continue
        before = getattr(contact, field_name)
        provenance.record_observation(
            session,
            contact_id=contact.id,
            field_name=field_name,
            value=value,
            source_name="linkedin-contact-capture",
            source_reference=str(snapshot.id),
            observed_at=observed_at,
            created_by=actor,
        )
        provenance.reconcile_field(
            session,
            contact=contact,
            field_name=field_name,
            actor=actor,
        )
        if getattr(contact, field_name) != before:
            changed.append(field_name)
    session.flush()
    return changed


def _record_capture_identifiers(
    session: Session,
    *,
    contact: Contact,
    snapshot: LinkedInProfileSnapshot,
    actor: str,
) -> list[str]:
    """Record only identifiers directly observed on this one capture."""

    observed_url = (
        snapshot.normalized_profile_url if snapshot.profile_url_source == "observed" else None
    )
    outcomes: list[identity_links.LinkOutcome] = []
    if snapshot.salesnav_member_id and observed_url:
        bridge = identity_links.bridge_observed_pair(
            session,
            contact=contact,
            member_id=snapshot.salesnav_member_id,
            vanity_url=observed_url,
            decided_by=actor,
            capture_id=snapshot.id,
            source_surface=snapshot.source_surface,
        )
        outcomes.extend(item for item in (bridge.member, bridge.vanity) if item is not None)
    else:
        for kind, value in (
            (LinkedInIdentifierKind.SALESNAV_MEMBER_ID, snapshot.salesnav_member_id),
            (LinkedInIdentifierKind.PUBLIC_VANITY_URL, observed_url),
        ):
            if value:
                outcomes.append(
                    identity_links.record_observed(
                        session,
                        contact=contact,
                        kind=kind,
                        value=value,
                        decided_by=actor,
                        capture_id=snapshot.id,
                        source_surface=snapshot.source_surface,
                    )
                )
    session.flush()
    return sorted(
        {
            str(outcome.conflicting_contact_id)
            for outcome in outcomes
            if outcome.conflicting_contact_id is not None
        }
    )


def _link_existing_contact(
    session: Session,
    *,
    snapshot: LinkedInProfileSnapshot,
    capture: dict[str, Any],
    contact: Contact,
    actor: str,
    review_candidates: list[dict[str, Any]] | None = None,
) -> tuple[Contact, list[dict[str, Any]]]:
    """Refresh one exact identity owner without bypassing suppression."""

    candidates = review_candidates or []
    snapshot.matched_contact_id = contact.id
    decision = evaluate_suppression(
        session,
        email=contact.email,
        domain=contact.company_domain,
    )
    if decision.blocked:
        snapshot.outcome = LinkedInSnapshotOutcome.SUPPRESSED
        snapshot.reconciled_at = datetime.now(UTC)
        snapshot.refresh_summary = {
            "outcome": snapshot.outcome.value,
            "matched_contact_id": str(contact.id),
            "refreshed_fields": [],
            "unchanged_fields": [],
            "skipped_fields": {
                "*": (
                    f"contact is suppressed ({decision.blocked_reason}); evidence linked, "
                    "canonical fields untouched"
                )
            },
            "review_candidate_count": len(candidates),
            "suppression_reason": decision.blocked_reason,
        }
        session.flush()
        return contact, candidates

    values = _capture_contact_values(capture)
    changed = _record_capture_observations(
        session,
        contact=contact,
        snapshot=snapshot,
        values=values,
        actor=actor,
    )
    conflicts = _record_capture_identifiers(
        session,
        contact=contact,
        snapshot=snapshot,
        actor=actor,
    )
    snapshot.outcome = (
        LinkedInSnapshotOutcome.EXACT_MATCH_REFRESHED
        if changed
        else LinkedInSnapshotOutcome.EXACT_MATCH_UNCHANGED
    )
    snapshot.reconciled_at = datetime.now(UTC)
    snapshot.review_candidates = candidates or None
    snapshot.refresh_summary = {
        "outcome": snapshot.outcome.value,
        "matched_contact_id": str(contact.id),
        "refreshed_fields": changed,
        "unchanged_fields": [],
        "skipped_fields": {},
        "review_candidate_count": len(candidates),
        "suppression_reason": None,
        "identity_link_conflicts": conflicts,
    }
    session.flush()
    return contact, candidates


def _resolve_contact(
    session: Session,
    *,
    snapshot: LinkedInProfileSnapshot,
    capture: dict[str, Any],
    actor: str,
) -> tuple[Contact | None, list[dict[str, Any]]]:
    """Resolve only exact existing identity owners; otherwise keep evidence staged.

    Capture is an evidence-acquisition step, not canonical-person creation.  A
    permanent Contact is created later by the existing promotion service once
    company identity and suppression checks have converged.  This preserves the
    DAT-014/DAT-017A safety boundary while still allowing exact existing
    identities to refresh immediately.
    """

    observed_url = (
        snapshot.normalized_profile_url if snapshot.profile_url_source == "observed" else None
    )
    by_url = identity_links.lookup_contact(
        session,
        LinkedInIdentifierKind.PUBLIC_VANITY_URL,
        observed_url,
    )
    by_member = identity_links.lookup_contact(
        session,
        LinkedInIdentifierKind.SALESNAV_MEMBER_ID,
        snapshot.salesnav_member_id,
    )
    if by_url is not None and by_member is not None and by_url.id != by_member.id:
        conflict_candidates = [
            {
                "contact_id": str(contact.id),
                "match_basis": ["conflicting_exact_linkedin_identifier"],
                "auto_merge": False,
            }
            for contact in (by_url, by_member)
        ]
        snapshot.outcome = LinkedInSnapshotOutcome.AMBIGUOUS_REVIEW
        snapshot.reconciled_at = datetime.now(UTC)
        snapshot.review_candidates = conflict_candidates
        snapshot.refresh_summary = {
            "outcome": snapshot.outcome.value,
            "matched_contact_id": None,
            "refreshed_fields": [],
            "unchanged_fields": [],
            "skipped_fields": {"*": "observed LinkedIn identifiers belong to different Contacts"},
            "review_candidate_count": len(conflict_candidates),
            "suppression_reason": None,
        }
        session.flush()
        return None, conflict_candidates

    owner = by_url or by_member
    if owner is not None:
        return _link_existing_contact(
            session,
            snapshot=snapshot,
            capture=capture,
            contact=owner,
            actor=actor,
        )

    if snapshot.normalized_profile_url is not None:
        refreshed = refresh_service.reconcile_snapshot(session, snapshot, actor=actor)
        candidates = refreshed.review_candidates
        if refreshed.matched_contact_id is not None:
            contact = session.get(Contact, uuid.UUID(refreshed.matched_contact_id))
            assert contact is not None
            _record_capture_identifiers(
                session,
                contact=contact,
                snapshot=snapshot,
                actor=actor,
            )
            return contact, candidates
        return None, candidates

    return None, _stage_without_identity(session, snapshot)


def _record_note(
    session: Session,
    *,
    snapshot: LinkedInProfileSnapshot,
    submission: ContactCaptureSubmission,
    capture_note: str | None,
    submission_note: str | None,
    actor: str,
) -> bool:
    """Append the effective operator note for one capture. Never overwrites."""

    scope = NOTE_SCOPE_CONTACT if capture_note else NOTE_SCOPE_SUBMISSION
    note_text = capture_note or submission_note
    if not note_text:
        return False
    session.add(
        ContactCaptureNote(
            capture_id=snapshot.id,
            submission_id=submission.id,
            contact_id=snapshot.matched_contact_id,
            scope=scope,
            note_text=note_text,
            author=actor,
        )
    )
    return True


# --- Entry point --------------------------------------------------------------


def _find_submission(
    session: Session, client_submission_id: str
) -> ContactCaptureSubmission | None:
    return session.scalars(
        select(ContactCaptureSubmission).where(
            ContactCaptureSubmission.client_submission_id == client_submission_id
        )
    ).first()


def _replay(submission: ContactCaptureSubmission) -> SubmissionResult:
    body = dict(submission.response_body or {})
    return SubmissionResult(
        submission_id=str(submission.id),
        client_submission_id=submission.client_submission_id,
        received_at=str(body.get("received_at") or submission.received_at.isoformat()),
        already_received=True,
        counts={key: int((body.get("counts") or {}).get(key, 0)) for key in _COUNT_KEYS},
        results=[
            CaptureOutcome(
                client_capture_id=str(r.get("client_capture_id")),
                capture_id=r.get("capture_id"),
                outcome=str(r.get("outcome")),
                matched_contact_id=r.get("matched_contact_id"),
                contact_url=r.get("contact_url"),
                capture_url=r.get("capture_url"),
                review_candidate_count=int(r.get("review_candidate_count") or 0),
                labels_applied=list(r.get("labels_applied") or []),
                campaign_filing=(
                    dict(r["campaign_filing"])
                    if isinstance(r.get("campaign_filing"), dict)
                    else None
                ),
                warnings=list(r.get("warnings") or []),
            )
            for r in (body.get("results") or [])
        ],
        operator_workbench_url=body.get("operator_workbench_url"),
        http_status=200,
    )


def _auto_resolve_captures(
    session: Session,
    *,
    snapshots: list[LinkedInProfileSnapshot],
    actor: str,
    deadline: Any,
) -> int:
    """Let the company-domain policy finish the captures it can, unattended.

    Every capture arriving without a permanent Contact used to wait for an
    operator to open it and press "resolve automatically" — a button that carried
    no judgement, because the policy behind it decides on evidence and the
    operator could neither see nor improve on that evidence. For a batch of a
    hundred that was a hundred clicks standing between a saved person and the
    Contact they already implied.

    So it runs here. What the policy cannot decide it still leaves alone: an
    ambiguous, conflicting or unresolvable company produces ``UNRESOLVED``, no
    Contact, and the manual controls the operator genuinely needs. Nothing is
    loosened — ``resolve()`` applies exactly the rules it always did, including
    the guards that keep an uncorroborated guess out of the approved-mapping
    store.

    Each capture is isolated in its own SAVEPOINT. A provider failure, a policy
    error or an operator-correction conflict on one person must not roll back the
    submission that saved all of them; the capture stays staged and resolvable by
    hand, which is exactly where it would have been anyway.
    """

    settings = get_settings()
    if not (
        settings.features.contact_capture_promotion
        and settings.features.automatic_company_domain_resolution
    ):
        return 0

    # Imported here rather than at module scope: intake is the staging boundary
    # and must not acquire a hard dependency on the resolution package for the
    # common case where the switch is off.
    from app.services.resolution import service as resolution_service

    usable = settings.features.salesnav_domain_enrichment and settings.has_logo_dev_key()
    access = resolution_service.ProviderAccess(
        api_key=settings.logo_dev_api_key if usable else None,
        search_url=settings.logo_dev_search_url,
        timeout=settings.logo_dev_timeout_seconds,
        max_candidates=settings.logo_dev_max_candidates,
    )
    # Without a usable provider, an attempt here cannot decide anything it could
    # not decide later — and it is actively harmful. The policy would record
    # UNRESOLVED for the reason "the provider lookup was not run", which is not a
    # decision but the absence of one; and because a recorded decision is not
    # recalculated without an explicit force, that non-decision would stop the
    # capture ever being resolved automatically again. So the capture stays staged,
    # exactly as it did before this automation existed.
    #
    # Nothing valuable is lost. With a key configured, established evidence — an
    # approved mapping from an earlier confirmation at the same company — is still
    # checked first and still resolves without a provider call.
    if not access.available:
        return 0

    # Bounded, and it yields rather than failing. Both halves were learned the
    # hard way: a 100-capture submission spent one logo.dev lookup per unresolved
    # company, blew the submission's 60s budget, and the IntakeTimeoutError that
    # raised is a ContactCaptureError — whose handler rolls the transaction back.
    # So an *optional improvement* destroyed 100 successfully staged people.
    #
    # Two rules follow. Optional work never calls deadline.check(), because that
    # converts "ran out of time on an extra" into "discard everything". And it
    # gets a hard share of the remaining budget, so a slow provider cannot reach
    # the submission's limit at all.
    #
    # What is left unresolved is left *untouched* — no decision recorded, so the
    # capture stays exactly as resolvable as it was. The agent worker finishes
    # them on its next pass, where time is not bounded by an HTTP request.
    budget = min(deadline.remaining_seconds() * _RESOLUTION_BUDGET_SHARE, _RESOLUTION_MAX_SECONDS)
    started = _now()
    resolved = 0
    provider_calls = 0

    for snapshot in snapshots:
        if provider_calls >= _RESOLUTION_MAX_PROVIDER_CALLS:
            break
        if _now() - started >= budget:
            break
        try:
            with session.begin_nested():
                outcome = resolution_service.resolve(
                    session,
                    snapshot=snapshot,
                    access=access,
                    actor=actor,
                    # Never force: an operator correction already on this capture
                    # is a decision, and recalculating over it would discard it.
                    force=False,
                )
        except Exception:  # noqa: BLE001 - one capture must not fail the batch
            continue
        if outcome.provider_call_made:
            provider_calls += 1
        if outcome.auto_promoted:
            resolved += 1
    return resolved


def stage_contact_captures(
    session: Session,
    *,
    payload: dict[str, Any],
    operator_base_url: str,
    timeout_seconds: float = 15.0,
    actor: str = _SOURCE_ACTOR,
    run_qa_policy: bool = True,
    _fault: Any = None,
) -> SubmissionResult:
    """Validate, persist and reconcile one contact-first capture submission.

    Idempotent on ``client_submission_id``: the same id with identical content
    replays the original truthful response; the same id with different content
    is a conflict. Every deterministic failure raises a
    :class:`ContactCaptureError` subclass and leaves nothing behind.
    """

    deadline = _Deadline(timeout_seconds)

    # --- Deterministic validation (no writes) --------------------------------
    _check_contract(payload)
    _validate_schema(payload)
    contacts: list[dict[str, Any]] = payload["contacts"]
    _check_identity_signals(contacts)
    _check_capture_ids_unique(contacts)

    client_submission_id = str(payload["client_submission_id"])
    content_hash = _content_hash(payload)

    existing = _find_submission(session, client_submission_id)
    if existing is not None:
        if existing.content_hash == content_hash:
            return _replay(existing)
        raise IdempotencyConflictError(
            f"client_submission_id {client_submission_id!r} was already accepted with "
            "different content",
            details=[
                "reusing a client_submission_id requires an identical body; re-capture "
                "in the extension to submit new content"
            ],
        )

    # A capture id that already belongs to another submission is a client error:
    # replaying it here would silently split one person's evidence in two.
    incoming_ids = [str(c["client_capture_id"]) for c in contacts]
    taken = list(
        session.scalars(
            select(LinkedInProfileSnapshot.client_capture_id).where(
                LinkedInProfileSnapshot.client_capture_id.in_(incoming_ids)
            )
        )
    )
    if taken:
        raise CaptureIdConflictError(
            "one or more client_capture_id values were already accepted",
            details=[f"client_capture_id {cid!r} already exists" for cid in sorted(taken)],
        )

    submission_meta = payload.get("operator_metadata") or {}
    requested_labels = labels_service.normalize_requested_labels(submission_meta.get("labels"))
    try:
        requested_campaign_id = (
            uuid.UUID(str(payload["campaign_id"])) if payload.get("campaign_id") else None
        )
    except ValueError as exc:
        raise ValidationFailedError(
            "request body failed schema validation",
            details=["campaign_id: must be a UUID string or null"],
        ) from exc
    submission_note = submission_meta.get("note") or None
    capture_mode = str(payload.get("capture_mode"))
    received = datetime.now(UTC)

    try:
        _apply_statement_timeout(session, timeout_seconds)
        deadline.check()

        submission = ContactCaptureSubmission(
            client_submission_id=client_submission_id,
            content_hash=content_hash,
            schema_version=str(payload.get("schema_version")),
            source=str(payload.get("source")),
            capture_mode=capture_mode,
            extension_version=payload.get("extension_version"),
            submitted_at=_parse_dt(payload.get("submitted_at")),
            received_at=received,
            contact_count=len(contacts),
            requested_labels=requested_labels or None,
            operator_note=submission_note,
            response_body={},
        )
        session.add(submission)
        session.flush()

        counts = dict.fromkeys(_COUNT_KEYS, 0)
        counts["submitted"] = len(contacts)
        results: list[CaptureOutcome] = []
        seen_keys: dict[str, uuid.UUID] = {}
        # Captures that reached no permanent Contact. These are the ones automatic
        # company-domain resolution can finish, and it runs after the loop rather
        # than inside it so a slow provider call cannot lengthen the transaction
        # that is holding the staged rows.
        awaiting_company: list[LinkedInProfileSnapshot] = []

        for capture in contacts:
            deadline.check()
            person = capture.get("person") or {}
            capture_meta = capture.get("operator_metadata") or {}
            capture_labels = labels_service.normalize_requested_labels(capture_meta.get("labels"))
            effective_labels = requested_labels + [
                name for name in capture_labels if name not in requested_labels
            ]
            normalized_url, dedup_key = identity_key(person)

            snapshot = _build_snapshot(
                capture=capture,
                payload=payload,
                submission=submission,
                normalized_url=normalized_url,
                requested_labels=effective_labels,
            )
            session.add(snapshot)
            session.flush()
            filing = (
                campaign_filing.ensure_filing(
                    session,
                    snapshot=snapshot,
                    requested_campaign_id=requested_campaign_id,
                )
                if requested_campaign_id is not None
                else None
            )
            if filing is not None and filing.campaign_id is not None:
                snapshot.campaign_id = filing.campaign_id
            _add_experience_rows(session, snapshot, capture)
            session.flush()

            duplicate_of = seen_keys.get(dedup_key) if dedup_key else None
            if duplicate_of is not None:
                original = session.get(LinkedInProfileSnapshot, duplicate_of)
                snapshot.outcome = LinkedInSnapshotOutcome.DUPLICATE_IN_SUBMISSION
                snapshot.duplicate_of_id = duplicate_of
                snapshot.matched_contact_id = (
                    original.matched_contact_id if original is not None else None
                )
                snapshot.reconciled_at = datetime.now(UTC)
                snapshot.refresh_summary = {
                    "outcome": LinkedInSnapshotOutcome.DUPLICATE_IN_SUBMISSION.value,
                    "duplicate_of_capture_id": str(duplicate_of),
                    "matched_contact_id": (
                        str(snapshot.matched_contact_id) if snapshot.matched_contact_id else None
                    ),
                    "refreshed_fields": [],
                    "unchanged_fields": [],
                    "skipped_fields": {
                        "*": "the same person was already captured in this submission"
                    },
                    "review_candidate_count": 0,
                    "suppression_reason": None,
                }
                session.flush()
                candidates: list[dict[str, Any]] = []
            else:
                if dedup_key:
                    seen_keys[dedup_key] = snapshot.id
                matched_contact, candidates = _resolve_contact(
                    session,
                    snapshot=snapshot,
                    capture=capture,
                    actor=actor,
                )

            if duplicate_of is not None:
                matched_contact = (
                    session.get(Contact, snapshot.matched_contact_id)
                    if snapshot.matched_contact_id is not None
                    else None
                )

            # Labels apply to the permanent Contact unless suppression blocks
            # mutation. Ambiguous identifier conflicts remain capture-scoped.
            applied: list[str] = []
            if (
                matched_contact is not None
                and effective_labels
                and snapshot.outcome != LinkedInSnapshotOutcome.SUPPRESSED
            ):
                resolved = labels_service.resolve_labels(session, effective_labels)
                applied = labels_service.assign_labels(
                    session,
                    contact_id=matched_contact.id,
                    labels=resolved.labels,
                    capture_id=snapshot.id,
                )
            elif effective_labels:
                # Register the label so it is reusable, without assigning it.
                labels_service.resolve_labels(session, effective_labels)

            filing_body: dict[str, Any] | None = None
            if filing is not None:
                filing_result = (
                    campaign_filing.apply_filing(
                        session,
                        filing=filing,
                        snapshot=snapshot,
                        contact=matched_contact,
                        actor=actor,
                    )
                    if matched_contact is not None
                    else campaign_filing.FilingResult(filing=filing, applied=False)
                )
                filing_body = filing_result.to_dict()
                counts[f"campaign_filings_{filing.status.value}"] += 1

            if _record_note(
                session,
                snapshot=snapshot,
                submission=submission,
                capture_note=capture_meta.get("note") or None,
                submission_note=submission_note,
                actor=actor,
            ):
                counts["notes_recorded"] += 1

            if (
                run_qa_policy
                and capture_mode == CAPTURE_MODE_PROFILE
                and matched_contact is not None
                and snapshot.outcome
                in (
                    LinkedInSnapshotOutcome.EXACT_MATCH_REFRESHED,
                    LinkedInSnapshotOutcome.EXACT_MATCH_UNCHANGED,
                )
            ):
                from app.services.qa.policy import evaluate_contact_snapshot

                evaluate_contact_snapshot(session, snapshot=snapshot, contact=matched_contact)

            if matched_contact is None and duplicate_of is None:
                awaiting_company.append(snapshot)

            counts[_OUTCOME_COUNTER[snapshot.outcome]] += 1
            counts["labels_applied"] += len(applied)
            results.append(
                CaptureOutcome(
                    client_capture_id=snapshot.client_capture_id,
                    capture_id=str(snapshot.id),
                    outcome=snapshot.outcome.value,
                    matched_contact_id=(
                        str(snapshot.matched_contact_id) if snapshot.matched_contact_id else None
                    ),
                    contact_url=(
                        contact_url(operator_base_url, snapshot.matched_contact_id)
                        if snapshot.matched_contact_id
                        else None
                    ),
                    capture_url=capture_url(operator_base_url, snapshot.id),
                    review_candidate_count=len(candidates),
                    labels_applied=applied,
                    campaign_filing=filing_body,
                    warnings=list(person.get("warnings") or []),
                )
            )

        counts["auto_resolved"] = _auto_resolve_captures(
            session, snapshots=awaiting_company, actor=actor, deadline=deadline
        )

        result = SubmissionResult(
            submission_id=str(submission.id),
            client_submission_id=client_submission_id,
            received_at=received.isoformat(),
            already_received=False,
            counts=counts,
            results=results,
            operator_workbench_url=submission_url(operator_base_url, submission.id),
            http_status=201,
        )
        submission.response_body = result.to_body()
        session.flush()

        record_audit_event(
            session,
            actor=actor,
            action=SUCCESS_AUDIT_ACTION,
            entity_type="contact_capture_submission",
            entity_id=str(submission.id),
            new_state="accepted",
            reason="operator-authorized contact capture submission accepted",
            context={
                "submission_id": str(submission.id),
                "schema_version": submission.schema_version,
                "capture_mode": capture_mode,
                "counts": counts,
                "label_count": len(requested_labels),
                "note_present": bool(submission_note),
                "campaign_requested": requested_campaign_id is not None,
                "canonical_creation": CANONICAL_CREATION_NOTE,
            },
        )

        if _fault is not None:
            _fault()

        deadline.check()
        session.commit()
        return result
    except IntegrityError:
        session.rollback()
        winner = _find_submission(session, client_submission_id)
        if winner is not None:
            if winner.content_hash == content_hash:
                return _replay(winner)
            raise IdempotencyConflictError(
                f"client_submission_id {client_submission_id!r} was already accepted with "
                "different content"
            ) from None
        raise
    except OperationalError as exc:
        session.rollback()
        if _is_query_canceled(exc):
            raise IntakeTimeoutError("contact capture intake exceeded its time budget") from exc
        raise
    except ContactCaptureError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise

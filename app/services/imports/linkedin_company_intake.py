"""LinkedIn company-page capture -> immutable firmographic evidence (DAT-012G).

Mirrors the person-profile intake discipline for operator-opened company pages:
validate against the committed contract schema
(``extensions/salesnav-capture/docs/company-intake.schema.json``, version
``linkedin-company-capture/1.0.0``), persist one immutable snapshot, stay
idempotent on the client-minted capture id, and audit safely.

Matching is evidence-linking only, applied at ingest:

* **Exact LinkedIn company URL/ID lineage** — a previous company snapshot with
  the same normalized company URL that is already linked to a company links
  this one to the same company.
* **Exact unique website domain** — the displayed website's normalized domain
  equals exactly one existing company's unique ``domain``.
* Anything weaker (name similarity, ambiguous domains) becomes **review
  candidates** on the snapshot. Nothing is merged and no canonical
  :class:`~app.models.company.Company` field is ever rewritten here.

Headquarters is parsed into city/region/country ONLY when the displayed text
splits deterministically into exactly three comma-separated parts; otherwise
the displayed value stands alone. Person/role/employee locations are never
used as headquarters.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.campaign import Campaign
from app.models.company import Company
from app.models.enums import CampaignStatus, LinkedInSnapshotOutcome
from app.models.linkedin_company import LinkedInCompanySnapshot
from app.services.audit import record_audit_event
from app.services.imports.normalization import (
    normalize_domain,
    normalize_linkedin_company_url,
)

# --- Contract constants ------------------------------------------------------

CONTRACT_NAMESPACE = "linkedin-company-capture"
SUPPORTED_MAJOR = 1
SCHEMA_VERSION = f"{CONTRACT_NAMESPACE}/1.0.0"

_SOURCE_ACTOR = "linkedin-company-capture"
_VERSION_RE = re.compile(rf"^{re.escape(CONTRACT_NAMESPACE)}/(\d+)\.(\d+)\.(\d+)$")

INTAKE_ROUTE = "/api/intake/linkedin-company/stage"
SUCCESS_AUDIT_ACTION = "intake.linkedin_company_staged"
FAILURE_AUDIT_ACTION = "intake.linkedin_company_stage_failed"
INTAKE_ENTITY_TYPE = "linkedin_company_intake"

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3]
    / "extensions"
    / "salesnav-capture"
    / "docs"
    / "company-intake.schema.json"
)

_MAX_REVIEW_CANDIDATES = 10

# Reuse the profile intake's error hierarchy verbatim: the wire contract shares
# its shape and codes (except the namespace-specific schema), so a second
# hierarchy would only duplicate strings.
from app.services.imports.linkedin_profile_intake import (  # noqa: E402
    CampaignInvalidError,
    IdempotencyConflictError,
    IntakeTimeoutError,
    InvalidJsonError,
    PayloadTooLargeError,
    ProfileIntakeError,
    UnauthorizedError,
    UnsupportedVersionError,
    ValidationFailedError,
    build_failure_context,
)


@dataclass
class CompanySnapshotResult:
    """The outcome of staging (or replaying) one company capture."""

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


def _check_version(payload: dict[str, Any]) -> None:
    version = payload.get("schema_version")
    if not isinstance(version, str):
        return
    match = _VERSION_RE.match(version)
    if match is None:
        return
    if int(match.group(1)) != SUPPORTED_MAJOR:
        raise UnsupportedVersionError(
            f"unsupported contract version {version!r}",
            details=[
                f"schema_version {version!r} declares MAJOR {match.group(1)}; this "
                f"backend supports {CONTRACT_NAMESPACE}/{SUPPORTED_MAJOR}.x"
            ],
        )


def _validate_schema(payload: dict[str, Any]) -> None:
    errors = sorted(_request_validator().iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        details = [f"{_json_pointer(err.path)}: {err.message}" for err in errors]
        raise ValidationFailedError("request body failed schema validation", details=details)


def _require_company_url(payload: dict[str, Any]) -> str:
    company = payload.get("company") or {}
    normalized = normalize_linkedin_company_url(company.get("company_linkedin_url"))
    if normalized is None:
        raise ValidationFailedError(
            "company.company_linkedin_url does not normalize to a company URL",
            details=[
                "company.company_linkedin_url must normalize to "
                "https://www.linkedin.com/company/<identifier>"
            ],
        )
    return normalized


def _resolve_campaign(session: Session, campaign_id: Any) -> Campaign | None:
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


# --- Deterministic parsing ----------------------------------------------------


def parse_headquarters(displayed: str | None) -> tuple[str | None, str | None, str | None]:
    """Split "City, Region, Country" ONLY when exactly three parts appear.

    Two parts ("Austin, Texas") are ambiguous — the second may be a region or a
    country — so nothing is parsed and the displayed value stands alone. Never
    guesses, never geocodes.
    """

    if not displayed:
        return None, None, None
    parts = [p.strip() for p in displayed.split(",") if p.strip()]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    return None, None, None


def _token_overlap(a: str, b: str) -> float:
    tokens = [t for t in a.casefold().split() if t]
    if not tokens:
        return 0.0
    other = b.casefold()
    return sum(1 for t in tokens if t in other) / len(tokens)


def _names_similar(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return _token_overlap(a, b) > 0.5 or _token_overlap(b, a) > 0.5


# --- Matching (evidence-linking only) -----------------------------------------


def _match_company(
    session: Session, *, normalized_url: str, website_domain: str | None, name: str | None
) -> tuple[LinkedInSnapshotOutcome, uuid.UUID | None, list[dict[str, Any]]]:
    """Link this snapshot to an existing company, or surface review candidates.

    Primary: exact LinkedIn company URL lineage via previously matched
    snapshots. Secondary: exact unique website-domain equality (``domain`` is
    unique on companies). Weak name similarity only ever produces review
    candidates; ambiguity (URL lineage pointing at several companies) is
    surfaced for review, never resolved silently.
    """

    # Exact URL lineage: prior snapshots of the same company page.
    linked_ids = {
        row
        for row in session.scalars(
            select(LinkedInCompanySnapshot.matched_company_id).where(
                LinkedInCompanySnapshot.normalized_company_url == normalized_url,
                LinkedInCompanySnapshot.matched_company_id.is_not(None),
            )
        )
    }
    if len(linked_ids) == 1:
        return LinkedInSnapshotOutcome.EXACT_MATCH_UNCHANGED, next(iter(linked_ids)), []
    if len(linked_ids) > 1:
        candidates = [
            {
                "company_id": str(cid),
                "match_basis": ["linkedin_url_lineage_conflict"],
                "auto_merge": False,
            }
            for cid in sorted(linked_ids, key=str)
        ]
        return LinkedInSnapshotOutcome.AMBIGUOUS_REVIEW, None, candidates

    # Exact unique domain.
    if website_domain:
        domain_matches = list(
            session.scalars(select(Company).where(Company.domain == website_domain))
        )
        if len(domain_matches) == 1:
            return (
                LinkedInSnapshotOutcome.EXACT_MATCH_UNCHANGED,
                domain_matches[0].id,
                [],
            )

    # Weak: name similarity -> review candidates only.
    candidates = []
    if name:
        for company in session.scalars(select(Company)):
            if _names_similar(name, company.name):
                candidates.append(
                    {
                        "company_id": str(company.id),
                        "company_name": company.name,
                        "company_domain": company.domain,
                        "match_basis": ["name_similarity"],
                        "auto_merge": False,
                    }
                )
                if len(candidates) >= _MAX_REVIEW_CANDIDATES:
                    break
    return LinkedInSnapshotOutcome.UNMATCHED_STAGED, None, candidates


# --- Helpers ------------------------------------------------------------------


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
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _workbench_url(operator_base_url: str, snapshot_id: uuid.UUID) -> str:
    return f"{operator_base_url.rstrip('/')}/company-profiles/{snapshot_id}"


def _result(
    snapshot: LinkedInCompanySnapshot,
    *,
    already_received: bool,
    http_status: int,
    operator_base_url: str,
    received_at: datetime | None = None,
) -> CompanySnapshotResult:
    received = (received_at or snapshot.ingested_at or datetime.now(UTC)).astimezone(UTC)
    return CompanySnapshotResult(
        snapshot_id=str(snapshot.id),
        client_capture_id=snapshot.client_capture_id,
        outcome=snapshot.outcome.value,
        warnings=[],
        received_at=received.isoformat(),
        operator_workbench_url=_workbench_url(operator_base_url, snapshot.id),
        already_received=already_received,
        http_status=http_status,
    )


def _find_by_capture_id(session: Session, client_capture_id: str) -> LinkedInCompanySnapshot | None:
    return session.scalars(
        select(LinkedInCompanySnapshot).where(
            LinkedInCompanySnapshot.client_capture_id == client_capture_id
        )
    ).first()


def record_intake_failure(
    session: Session, *, error: ProfileIntakeError, payload: Any = None
) -> None:
    """Best-effort, PII-free failure audit (same fail-open contract as DAT-009)."""

    try:
        session.rollback()
        context = build_failure_context(
            error_code=error.error_code, http_status=error.http_status, payload=payload
        )
        context["route"] = INTAKE_ROUTE
        context["source"] = INTAKE_ENTITY_TYPE
        record_audit_event(
            session,
            actor=_SOURCE_ACTOR,
            action=FAILURE_AUDIT_ACTION,
            entity_type=INTAKE_ENTITY_TYPE,
            entity_id=None,
            new_state="rejected",
            reason=f"linkedin company intake rejected: {error.error_code}",
            context=context,
        )
        session.commit()
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass


# --- Entry point --------------------------------------------------------------


def stage_company_snapshot(
    session: Session,
    *,
    payload: dict[str, Any],
    operator_base_url: str,
    actor: str = _SOURCE_ACTOR,
) -> CompanySnapshotResult:
    """Validate, persist, and evidence-link one reviewed company capture.

    Creates exactly one immutable snapshot row; creates or rewrites zero
    canonical companies, contacts, suppressions, or outreach state. Idempotent
    on ``client_capture_id``.
    """

    _check_version(payload)
    _validate_schema(payload)
    normalized_url = _require_company_url(payload)
    campaign = _resolve_campaign(session, payload.get("campaign_id"))

    client_capture_id = str(payload["client_capture_id"])
    content_hash = _content_hash(payload)

    existing = _find_by_capture_id(session, client_capture_id)
    if existing is not None:
        if existing.content_hash == content_hash:
            return _result(
                existing,
                already_received=True,
                http_status=200,
                operator_base_url=operator_base_url,
            )
        raise IdempotencyConflictError(
            f"client_capture_id {client_capture_id!r} was already staged with a different payload"
        )

    received = datetime.now(UTC)
    company = payload["company"]
    extraction = payload["extraction"]
    website_domain = normalize_domain(company.get("website"))
    hq_city, hq_region, hq_country = parse_headquarters(company.get("headquarters_text"))
    outcome, matched_company_id, candidates = _match_company(
        session,
        normalized_url=normalized_url,
        website_domain=website_domain,
        name=company.get("name"),
    )

    try:
        snapshot = LinkedInCompanySnapshot(
            client_capture_id=client_capture_id,
            content_hash=content_hash,
            schema_version=str(payload.get("schema_version")),
            source=str(payload.get("source")),
            source_url=payload.get("source_url"),
            normalized_company_url=normalized_url,
            company_linkedin_id=company.get("company_linkedin_id"),
            website_domain=website_domain,
            campaign_id=campaign.id if campaign is not None else None,
            captured_at=_parse_dt(payload.get("captured_at")),
            extraction_status=str(extraction.get("status")),
            adapter_version=extraction.get("adapter_version"),
            missing_sections=extraction.get("missing_sections"),
            page_warnings=extraction.get("page_warnings"),
            payload=payload,
            company_fields=company,
            hq_city=hq_city,
            hq_region=hq_region,
            hq_country=hq_country,
            outcome=outcome,
            matched_company_id=matched_company_id,
            review_candidates=candidates or None,
            reconciled_at=received,
        )
        session.add(snapshot)
        session.flush()

        record_audit_event(
            session,
            actor=actor,
            action=SUCCESS_AUDIT_ACTION,
            entity_type="linkedin_company_snapshot",
            entity_id=str(snapshot.id),
            new_state=outcome.value,
            reason="operator-authorized LinkedIn company capture stored immutably",
            context={
                "snapshot_id": str(snapshot.id),
                "client_capture_id": client_capture_id,
                "schema_version": payload.get("schema_version"),
                "outcome": outcome.value,
                "matched_company_id": str(matched_company_id) if matched_company_id else None,
                "review_candidate_count": len(candidates),
            },
        )
        session.commit()
        return _result(
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
                return _result(
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
    except ProfileIntakeError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise


__all__ = [
    "CampaignInvalidError",
    "CompanySnapshotResult",
    "IdempotencyConflictError",
    "IntakeTimeoutError",
    "InvalidJsonError",
    "PayloadTooLargeError",
    "ProfileIntakeError",
    "UnauthorizedError",
    "ValidationFailedError",
    "parse_headquarters",
    "record_intake_failure",
    "stage_company_snapshot",
]

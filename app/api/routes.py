"""Phase 1 API routes: campaign creation and staged CSV import.

These are intentionally thin adapters over the services. All business rules live
in the service layer (AGENTS.md: the dashboard/API "must not contain business
rules"). Both import behaviours are gated by the ``csv_import`` feature switch.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import get_settings
from app.models.enums import CampaignStatus
from app.services.campaigns import CampaignError, create_campaign
from app.services.imports.importer import (
    BatchProvenance,
    CampaignNotFound,
    FeatureDisabledError,
    run_import,
)
from app.services.imports.salesnav_intake import (
    InvalidJsonError,
    PayloadTooLargeError,
    SalesNavIntakeError,
    UnauthorizedError,
    record_intake_failure,
    stage_salesnav_batch,
)

router = APIRouter()

# --- Sales Navigator capture intake (DAT-009) --------------------------------

SALESNAV_INTAKE_PATH = "/api/intake/sales-navigator/stage"

# Loopback hosts the capture extension is allowed to talk to. The endpoint is
# local-only; a request from any non-loopback web origin is refused.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_CORS_ALLOW_METHODS = "POST, GET, OPTIONS"
_CORS_ALLOW_HEADERS = "Content-Type, Idempotency-Key, X-Client-Batch-Id"


def _origin_allowed(origin: str | None) -> bool:
    """Allow no-origin (curl/service-worker), the extension, and loopback only."""

    if origin is None:
        return True
    parsed = urlsplit(origin)
    if parsed.scheme == "chrome-extension":
        return True
    if parsed.scheme in {"http", "https"} and parsed.hostname in _LOOPBACK_HOSTS:
        return True
    return False


def _cors_headers(origin: str | None) -> dict[str, str]:
    """CORS headers reflecting an allowed origin (empty when there is none)."""

    if origin is None:
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": _CORS_ALLOW_METHODS,
        "Access-Control-Allow-Headers": _CORS_ALLOW_HEADERS,
        "Vary": "Origin",
    }


def _intake_error_response(exc: SalesNavIntakeError, origin: str | None) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status, content=exc.to_body(), headers=_cors_headers(origin)
    )


def _fail_intake(
    db: Session, exc: SalesNavIntakeError, origin: str | None, payload: object = None
) -> JSONResponse:
    """Record a safe failure audit (best-effort) and return the error response.

    Auditing is deterministic and PII-free (see ``record_intake_failure``); it
    never changes the client-facing status or body, and a failed audit write is
    swallowed so a client error is never masked by an unrelated 500.
    """

    record_intake_failure(db, error=exc, payload=payload)
    return _intake_error_response(exc, origin)


@router.options(SALESNAV_INTAKE_PATH, include_in_schema=False)
async def salesnav_stage_preflight(request: Request) -> Response:
    """CORS preflight for the capture extension. Reflects an allowed origin only."""

    origin = request.headers.get("origin")
    if not _origin_allowed(origin):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"error": "unauthorized", "status": 403},
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers=_cors_headers(origin))


@router.post(SALESNAV_INTAKE_PATH)
async def salesnav_stage_route(request: Request, db: Session = Depends(get_db)) -> Response:
    """Stage one authorized Sales Navigator capture batch (DAT-009).

    Thin adapter: enforces the endpoint's local/private-access, origin, size, and
    JSON boundaries, then delegates all staging logic to the service. A success
    creates only the staged import batch and its immutable raw rows — never a
    contact, company, membership, score, or outreach action.
    """

    settings = get_settings()
    origin = request.headers.get("origin")

    # 1. Feature gate: the endpoint does not exist until deliberately enabled.
    if not settings.features.salesnav_intake:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": "not_found", "status": 404},
        )

    # 2. Local-only guard: the endpoint has no authentication and must never
    #    serve a non-local environment (same rule as the operator workbench).
    if settings.app_env.lower() != "local":
        return _fail_intake(
            db,
            UnauthorizedError("the Sales Navigator intake endpoint is available only locally"),
            origin,
        )

    # 3. Origin guard: only the extension or a loopback origin may post here.
    if not _origin_allowed(origin):
        return _fail_intake(db, UnauthorizedError(f"origin {origin!r} is not allowed"), origin)

    # 4. Payload-size guard: reject an oversized body before reading/parsing it.
    limit = settings.salesnav_intake_max_bytes
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        return _fail_intake(
            db, PayloadTooLargeError(f"request body exceeds the {limit}-byte intake limit"), origin
        )
    body = await request.body()
    if len(body) > limit:
        return _fail_intake(
            db, PayloadTooLargeError(f"request body exceeds the {limit}-byte intake limit"), origin
        )

    # 5. JSON parse: a malformed body is a deterministic 400.
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _fail_intake(db, InvalidJsonError("request body was not valid JSON"), origin)
    if not isinstance(payload, dict):
        return _fail_intake(db, InvalidJsonError("request body must be a JSON object"), origin)

    # 6. Stage. All contract validation and persistence live in the service.
    try:
        result = stage_salesnav_batch(
            db,
            payload=payload,
            operator_base_url=settings.operator_base_url,
            timeout_seconds=settings.salesnav_intake_timeout_seconds,
        )
    except SalesNavIntakeError as exc:
        return _fail_intake(db, exc, origin, payload)

    return JSONResponse(
        status_code=result.http_status, content=result.to_body(), headers=_cors_headers(origin)
    )


class CampaignCreate(BaseModel):
    """Request body for creating a campaign."""

    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    status: CampaignStatus = CampaignStatus.DRAFT


class CampaignOut(BaseModel):
    """Campaign representation returned to the client."""

    id: uuid.UUID
    name: str
    description: str | None
    status: CampaignStatus


class ImportSummaryOut(BaseModel):
    """Import result returned to the client."""

    batch_id: uuid.UUID
    status: str
    total_rows: int
    accepted_rows: int
    rejected_rows: int
    duplicate_rows: int
    suppressed_rows: int
    ambiguous_rows: int
    contacts_created: int
    reused_existing_batch: bool
    error_detail: str | None = None


@router.post("/campaigns", response_model=CampaignOut, status_code=status.HTTP_201_CREATED)
def create_campaign_route(payload: CampaignCreate, db: Session = Depends(get_db)) -> CampaignOut:
    """Create a campaign shell that can receive an authorized import."""

    try:
        campaign = create_campaign(
            db,
            name=payload.name,
            description=payload.description,
            status=payload.status,
        )
    except CampaignError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return CampaignOut(
        id=campaign.id,
        name=campaign.name,
        description=campaign.description,
        status=campaign.status,
    )


@router.post("/campaigns/{campaign_id}/imports", response_model=ImportSummaryOut)
async def import_contacts_route(
    campaign_id: uuid.UUID,
    request: Request,
    source_name: str | None = None,
    source_reference: str | None = None,
    exported_by: str | None = None,
    exported_at: date | None = None,
    filename: str | None = None,
    db: Session = Depends(get_db),
) -> ImportSummaryOut:
    """Import an authorized CSV (raw request body) into a campaign.

    The feature switch is checked first so the capability stays disabled until
    Phase 1 is verified. Provenance is supplied as query parameters and captured
    once for the whole batch.
    """

    if not get_settings().features.csv_import:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="CSV import is not enabled.",
        )

    content = await request.body()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body is empty; expected CSV content.",
        )

    provenance = BatchProvenance(
        source_name=source_name,
        source_reference=source_reference,
        exported_by=exported_by,
        exported_at=exported_at,
    )
    try:
        summary = run_import(
            db,
            campaign_id=campaign_id,
            content=content,
            filename=filename,
            provenance=provenance,
        )
    except FeatureDisabledError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except CampaignNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return ImportSummaryOut(
        batch_id=summary.batch_id,
        status=summary.status.value,
        total_rows=summary.total_rows,
        accepted_rows=summary.accepted_rows,
        rejected_rows=summary.rejected_rows,
        duplicate_rows=summary.duplicate_rows,
        suppressed_rows=summary.suppressed_rows,
        ambiguous_rows=summary.ambiguous_rows,
        contacts_created=summary.contacts_created,
        reused_existing_batch=summary.reused_existing_batch,
        error_detail=summary.error_detail,
    )

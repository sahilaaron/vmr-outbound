"""Operator-workbench page routes (server-rendered, Jinja2).

These routes are deliberately thin adapters: every business rule lives in the
service layer (AGENTS.md — the dashboard "must not contain business rules").
The pages are gated behind the ``workbench`` feature switch (the router is only
mounted when the switch is on) and perform no outreach action of any kind.

Flash messages travel as ``ok``/``err`` query parameters on redirects, so the
pages stay stateless (no sessions, no cookies).
"""

from __future__ import annotations

import json
import uuid
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.auth.csrf import register_csrf, require_csrf
from app.core.config import Settings, get_settings
from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.contact_capture import ContactCaptureNote, ContactCaptureSubmission
from app.models.email_verification_studio import (
    EmailPatternPolicyVersion,
    ProviderTestRun,
    VerificationWaterfallPolicyVersion,
)
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    EnrichmentConfirmationSource,
    IdentityResolutionType,
    ImportBatchStatus,
    ImportRowOutcome,
    ImportSourceFormat,
    ResearchState,
    VerificationUsageEventType,
)
from app.models.linkedin_company import LinkedInCompanySnapshot
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.models.personalization_policy import PersonalizationPolicyVersion
from app.models.verification_job import AgentJob
from app.services import agent_studio as agent_studio_service
from app.services import (
    devtools,
    identity,
    workbench,
    workbench_agents,
)
from app.services.agent_studio.capture_report import DurableCaptureReportReader
from app.services.agent_studio.company_report import DurableCompanyReportReader
from app.services.agent_studio.email_verification_report import EmailVerificationReportReader
from app.services.agent_studio.insights_report import DurableInsightsReportReader
from app.services.agent_studio.research_report import (
    DurableResearchReportReader,
    ResearchReportReader,
)
from app.services.agents.registry import AGENT_SPECS
from app.services.campaign_access import (
    actor_from_request,
    require_campaign_path_access,
)
from app.services.campaigns import (
    get_campaign_overview,
    list_campaigns,
)
from app.services.captures import promotion as capture_promotion
from app.services.crm import annotations as crm_annotations
from app.services.crm import detail as crm_detail
from app.services.enrichment import companies as enrichment
from app.services.imports import display, parsing, staging, validation
from app.services.imports import mapping as mapping_service
from app.services.imports.importer import (
    BatchNotProcessable,
    BatchProvenance,
    CampaignNotFound,
    FeatureDisabledError,
    process_pending_batch,
    run_import,
)
from app.services.imports.preview import preview_import, preview_pending_batch
from app.services.operations import settings as operational
from app.services.personalization import generation as personalization_generation
from app.services.personalization import policy as personalization_policy
from app.services.resolution import pending as resolution_pending
from app.services.resolution import service as resolution_service
from app.services.seller.common import OPERATOR_ACTOR
from app.services.thinking.claude_cli import ClaudeCliThinker
from app.services.thinking.contracts import ThinkingError
from app.services.verification import console as verification_console
from app.services.verification import queue as verification_queue
from app.services.verification import service as verification_service
from app.services.verification import studio as verification_studio
from app.services.verification import usage as verification_usage
from app.services.verification.provider import VerificationProvider
from app.services.verification.provider_registry import PROVIDERS
from app.services.workbench_agents import views as workbench_views

# Every state-changing route on this router is refused unless the request
# carries the CSRF token bound to the caller's session. The check is declared
# once, here, rather than on ~100 individual handlers: a route added later is
# covered the moment it is registered. It is inert for safe methods and inert
# entirely when hosted authentication is disabled (local development).
router = APIRouter(
    include_in_schema=False,
    dependencies=[Depends(require_csrf), Depends(require_campaign_path_access)],
)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
# The shared spreadsheet-neutralization boundary, so every environment that
# can render imported text has it under the same name.
display.register_neutralize(templates.env)
register_csrf(templates.env)

#: Seconds between auto-refreshes on the Agent monitor pages.
#:
#: Only these pages opt in. They are the ones whose whole purpose is a queue that is
#: moving, where a page that can only be correct at the moment it was requested is
#: not much use — an operator was reloading by hand to learn what changed. Every
#: other page keeps the no-JavaScript convention and renders without a script tag.
LIVE_REFRESH_SECONDS = 5

PAGE_SIZE = 50
PREVIEW_ROWS_SHOWN = 50
SAMPLE_ROWS_SHOWN = 5
# Uploads are read in bounded chunks so an oversized file is rejected without
# ever being held fully in memory.
_UPLOAD_CHUNK_BYTES = 1024 * 1024


def _fmt_dt(value: datetime | date | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return value.isoformat()


def _pretty_json(value: object) -> str:
    """Render stored JSON for reading, not for machines.

    Dossier sections are JSONB written by a research producer, and until now the
    page only reported whether a section was *present* — which told an operator a
    section existed and nothing about what it said. This is what makes the content
    visible.

    ``sort_keys`` is deliberate: a stable key order means two versions of the same
    section can be compared by eye. ``default=str`` keeps dates and UUIDs
    readable rather than raising. An empty container renders as ``{}`` / ``[]``
    rather than as nothing, because "looked and found nothing" is a real answer
    and must not read as "did not look".
    """

    try:
        return json.dumps(value, indent=2, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(value)


templates.env.filters["dt"] = _fmt_dt
templates.env.filters["pretty_json"] = _pretty_json


def _database_ok(db: Session) -> bool:
    try:
        db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _render(
    request: Request,
    db: Session,
    template: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """Render a page with the shared shell context merged in."""

    settings = get_settings()
    try:
        open_reviews = identity.count_open_reviews(db)
    except Exception:
        open_reviews = 0
    shared: dict[str, Any] = {
        "app_env": settings.app_env,
        "dry_run": settings.dry_run,
        # The effective controls, not the environment's defaults: every page's
        # shell reports what an administrator has actually switched on.
        "features_enabled": operational.effective_flags(db, settings).enabled(),
        "local_env": settings.app_env.lower() == "local",
        "database_ok": _database_ok(db),
        "open_reviews": open_reviews,
        "flash_ok": request.query_params.get("ok"),
        "flash_err": request.query_params.get("err"),
    }
    shared.update(context)
    return templates.TemplateResponse(
        request=request, name=template, context=shared, status_code=status_code
    )


def _redirect(url: str, *, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    params = {}
    if ok:
        params["ok"] = ok
    if err:
        params["err"] = err
    if params:
        url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
    return RedirectResponse(url, status_code=303)


def _not_found(request: Request, db: Session, message: str) -> HTMLResponse:
    return _render(
        request, db, "not_found.html", {"message": message, "active_nav": ""}, status_code=404
    )


def _tri_state(raw: str | None) -> bool | None:
    """Map a yes/no query parameter to a tri-state filter.

    Anything unrecognised — including an empty string from a "no preference"
    select — means no filter rather than False, so a blank dropdown does not
    silently exclude half the list.
    """

    return {"yes": True, "no": False}.get((raw or "").strip().lower())


def _research_state(raw: str | None) -> ResearchState | None:
    """A research-state filter from a query parameter, or None.

    Unrecognised values mean no filter. A hand-edited URL should widen the list
    rather than produce an error page or silently show nothing.
    """

    try:
        return ResearchState((raw or "").strip().lower())
    except ValueError:
        return None


def _positive_int(raw: str | None) -> int | None:
    """A positive integer from a query parameter, or None.

    Operator-editable input: a negative or non-numeric value is ignored rather
    than raising, because a hand-edited URL should not produce an error page.
    """

    try:
        value = int((raw or "").strip())
    except ValueError:
        return None
    return value if value > 0 else None


def _page_number(request: Request) -> int:
    try:
        return max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        return 1


def _pages(total: int) -> int:
    return max(1, -(-total // PAGE_SIZE))


def _parse_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


# --- Overview ----------------------------------------------------------------


# The redesigned Admin Workbench now owns `/admin` (see
# `app.web.admin_workbench`, mounted before this router). The original
# import-centric overview stays reachable at an explicit legacy address, linked
# from Advanced Diagnostics, so nothing an operator bookmarked disappears.
@router.get("/admin/legacy/overview", response_class=HTMLResponse)
def overview_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    stats = workbench.load_overview(db)
    return _render(
        request,
        db,
        "overview.html",
        {"stats": stats, "active_nav": "overview", "page_title": "Overview"},
    )


# --- Imports: list + upload --------------------------------------------------


@router.get("/imports", response_class=HTMLResponse)
def imports_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    page = _page_number(request)
    batches, total = workbench.list_batches(db, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)

    staged_dir = get_settings().staged_uploads_dir
    staged_entries = staging.list_staged_uploads(staged_dir)
    campaign_names = {
        str(row.campaign.id): row.campaign.name
        for row in list_campaigns(db, actor=actor_from_request(request))
    }
    staged_with_names = [(entry, campaign_names.get(entry.campaign_id)) for entry in staged_entries]

    return _render(
        request,
        db,
        "imports.html",
        {
            "batches": batches,
            "total": total,
            "page": page,
            "pages": _pages(total),
            "staged": staged_with_names,
            "staged_ttl_hours": staging.STAGED_UPLOAD_TTL_HOURS,
            "active_nav": "imports",
            "page_title": "Imports",
        },
    )


@router.get("/imports/new", response_class=HTMLResponse)
def import_new_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return _render(
        request,
        db,
        "import_new.html",
        {
            "campaigns": list_campaigns(db, actor=actor_from_request(request)),
            "preselect_campaign": request.query_params.get("campaign_id"),
            "max_upload_bytes": get_settings().max_upload_bytes,
            "active_nav": "imports",
            "page_title": "New import",
        },
    )


@router.post("/imports/upload")
async def import_upload(
    request: Request, file: UploadFile, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    campaign_id = _parse_uuid(str(form.get("campaign_id", "")))
    if campaign_id is None or get_campaign_overview(db, campaign_id) is None:
        return _redirect("/imports/new", err="Choose an existing campaign to import into.")

    filename = (file.filename or "").strip()

    # Size gate FIRST — before any parsing or staging. The upload is read in
    # bounded chunks and abandoned as soon as it exceeds the configured limit,
    # so an oversized file is never held fully in memory, parsed, or staged.
    limit_bytes = get_settings().max_upload_bytes
    chunks: list[bytes] = []
    received = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        received += len(chunk)
        if received > limit_bytes:
            try:
                staging.enforce_upload_size(received, limit_bytes, filename=filename)
            except staging.UploadTooLargeError as exc:
                return _redirect(f"/imports/new?campaign_id={campaign_id}", err=str(exc))
        chunks.append(chunk)
    content = b"".join(chunks)

    try:
        file_format = parsing.detect_format(filename)
        parsed = parsing.parse_file(content, filename)
    except (parsing.UnsupportedFormatError, parsing.MalformedFileError) as exc:
        return _redirect(f"/imports/new?campaign_id={campaign_id}", err=str(exc))

    provenance: dict[str, str | None] = {
        "source_name": str(form.get("source_name", "")).strip() or None,
        "source_reference": str(form.get("source_reference", "")).strip() or None,
        "exported_by": str(form.get("exported_by", "")).strip() or None,
        "exported_at": str(form.get("exported_at", "")).strip() or None,
    }
    staged = staging.create_staged_upload(
        get_settings().staged_uploads_dir,
        filename=filename,
        campaign_id=str(campaign_id),
        content=content,
        source_format=file_format,
        provenance=provenance,
    )
    if file_format == "xlsx":
        message = (
            f"“{filename}” staged — {len(parsed.sheets)} sheet(s) found. Nothing is imported yet."
        )
        return _redirect(f"/imports/staged/{staged.id}/sheets", ok=message)
    message = (
        f"“{filename}” staged with {parsed.sheets[0].data_row_count} data row(s). "
        "Nothing is imported yet."
    )
    return _redirect(f"/imports/staged/{staged.id}/mapping", ok=message)


# --- Imports: staged wizard --------------------------------------------------


def _load_staged(staged_id: str) -> tuple[staging.StagedUpload, parsing.ParsedFile] | None:
    staged_dir = get_settings().staged_uploads_dir
    try:
        staged = staging.load_staged_upload(staged_dir, staged_id)
        content = staging.read_staged_content(staged_dir, staged_id)
        parsed = parsing.parse_file(content, staged.filename)
    except (
        staging.StagedUploadNotFound,
        parsing.UnsupportedFormatError,
        parsing.MalformedFileError,
    ):
        return None
    return staged, parsed


def _selected_header(parsed: parsing.ParsedFile, selection: list[int] | None) -> list[str]:
    """Order-preserving union of headers across the selected sheets."""

    header: list[str] = []
    seen: set[str] = set()
    for sheet in parsed.sheets:
        if selection is not None and sheet.index not in selection:
            continue
        for column in sheet.header:
            if column not in seen:
                seen.add(column)
                header.append(column)
    return header


@router.get("/imports/staged/{staged_id}/sheets", response_class=HTMLResponse)
def staged_sheets_page(
    request: Request, staged_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    loaded = _load_staged(staged_id)
    if loaded is None:
        return _not_found(request, db, "That staged upload no longer exists (it may have expired).")
    staged, parsed = loaded
    if staged.confirmed_batch_id:
        return _not_found(request, db, "This staged upload was already imported.")
    return _render(
        request,
        db,
        "staged_sheets.html",
        {
            "staged": staged,
            "sheets": parsed.sheets,
            "active_nav": "imports",
            "page_title": f"Inspect {staged.filename}",
        },
    )


@router.post("/imports/staged/{staged_id}/sheets")
async def staged_sheets_select(request: Request, staged_id: str) -> Response:
    loaded = _load_staged(staged_id)
    if loaded is None:
        return _redirect("/imports", err="That staged upload no longer exists.")
    staged, parsed = loaded

    form = await request.form()
    selection: list[int] = []
    for value in form.getlist("sheet"):
        try:
            index = int(str(value))
        except ValueError:
            continue
        sheet = parsed.sheet(index)
        if sheet is not None and sheet.header:
            selection.append(index)
    if not selection:
        return _redirect(
            f"/imports/staged/{staged_id}/sheets",
            err="Select at least one sheet that has a header row.",
        )
    staged.sheet_selection = sorted(selection)
    staging.update_staged_upload(get_settings().staged_uploads_dir, staged)
    return _redirect(f"/imports/staged/{staged_id}/mapping")


@router.get("/imports/staged/{staged_id}/mapping", response_class=HTMLResponse)
def staged_mapping_page(
    request: Request, staged_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    loaded = _load_staged(staged_id)
    if loaded is None:
        return _not_found(request, db, "That staged upload no longer exists (it may have expired).")
    staged, parsed = loaded
    if staged.confirmed_batch_id:
        return _not_found(request, db, "This staged upload was already imported.")

    header = _selected_header(parsed, staged.sheet_selection)
    current = staged.column_mapping or mapping_service.suggest_mapping(header)
    sample = parsed.rows_for_sheets(staged.sheet_selection)[:SAMPLE_ROWS_SHOWN]
    return _render(
        request,
        db,
        "staged_mapping.html",
        {
            "staged": staged,
            "header": header,
            "current_mapping": current,
            "system_fields": list(mapping_service.SYSTEM_FIELDS),
            "required_fields": set(validation.REQUIRED_COLUMNS),
            "sample_rows": sample,
            "mapping_problems": [],
            "active_nav": "imports",
            "page_title": f"Map columns — {staged.filename}",
        },
    )


@router.post("/imports/staged/{staged_id}/mapping")
async def staged_mapping_save(
    request: Request, staged_id: str, db: Session = Depends(get_db)
) -> Response:
    loaded = _load_staged(staged_id)
    if loaded is None:
        return _redirect("/imports", err="That staged upload no longer exists.")
    staged, parsed = loaded

    form = await request.form()
    header = _selected_header(parsed, staged.sheet_selection)
    mapping: dict[str, str] = {}
    for key, value in form.multi_items():
        if key.startswith("map__") and str(value):
            mapping[key[len("map__") :]] = str(value)

    check = mapping_service.check_mapping(mapping, header)
    if not check.is_valid:
        sample = parsed.rows_for_sheets(staged.sheet_selection)[:SAMPLE_ROWS_SHOWN]
        return _render(
            request,
            db,
            "staged_mapping.html",
            {
                "staged": staged,
                "header": header,
                "current_mapping": mapping,
                "system_fields": list(mapping_service.SYSTEM_FIELDS),
                "required_fields": set(validation.REQUIRED_COLUMNS),
                "sample_rows": sample,
                "mapping_problems": check.problems,
                "active_nav": "imports",
                "page_title": f"Map columns — {staged.filename}",
            },
            status_code=400,
        )

    staged.column_mapping = mapping
    staging.update_staged_upload(get_settings().staged_uploads_dir, staged)
    return _redirect(f"/imports/staged/{staged_id}/preview")


@router.get("/imports/staged/{staged_id}/preview", response_class=HTMLResponse)
def staged_preview_page(
    request: Request, staged_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    loaded = _load_staged(staged_id)
    if loaded is None:
        return _not_found(request, db, "That staged upload no longer exists (it may have expired).")
    staged, parsed = loaded
    if staged.confirmed_batch_id:
        return _not_found(request, db, "This staged upload was already imported.")

    result = preview_import(
        db,
        parsed=parsed,
        sheet_selection=staged.sheet_selection,
        column_mapping=staged.column_mapping,
    )
    campaign_uuid = _parse_uuid(staged.campaign_id)
    campaign = get_campaign_overview(db, campaign_uuid) if campaign_uuid else None
    return _render(
        request,
        db,
        "staged_preview.html",
        {
            "staged": staged,
            "preview": result,
            "shown_rows": result.rows[:PREVIEW_ROWS_SHOWN],
            "campaign_name": campaign.campaign.name if campaign else "(unknown campaign)",
            "active_nav": "imports",
            "page_title": f"Preview — {staged.filename}",
        },
    )


@router.post("/imports/staged/{staged_id}/confirm")
def staged_confirm(request: Request, staged_id: str, db: Session = Depends(get_db)) -> Response:
    staged_dir = get_settings().staged_uploads_dir
    try:
        staged = staging.load_staged_upload(staged_dir, staged_id)
    except staging.StagedUploadNotFound:
        return _redirect(
            "/imports", err="That staged upload no longer exists (it may have expired)."
        )

    # Repeated confirmation of the same staged file returns the existing batch.
    if staged.confirmed_batch_id:
        return _redirect(
            f"/imports/{staged.confirmed_batch_id}",
            ok="This staged upload was already imported; showing the existing batch.",
        )

    campaign_uuid = _parse_uuid(staged.campaign_id)
    if campaign_uuid is None:
        return _redirect("/imports", err="The staged upload's campaign reference is invalid.")

    content = staging.read_staged_content(staged_dir, staged_id)
    exported_at_raw = staged.provenance.get("exported_at")
    exported_at: date | None = None
    if exported_at_raw:
        try:
            exported_at = date.fromisoformat(exported_at_raw)
        except ValueError:
            exported_at = None
    provenance = BatchProvenance(
        source_name=staged.provenance.get("source_name"),
        source_reference=staged.provenance.get("source_reference"),
        exported_by=staged.provenance.get("exported_by"),
        exported_at=exported_at,
    )
    mime = (
        "text/csv"
        if staged.source_format == "csv"
        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    try:
        summary = run_import(
            db,
            campaign_id=campaign_uuid,
            content=content,
            filename=staged.filename,
            provenance=provenance,
            sheet_selection=staged.sheet_selection,
            column_mapping=staged.column_mapping,
            mime_type=mime,
            actor="workbench",
        )
    except FeatureDisabledError:
        return _redirect(
            f"/imports/staged/{staged_id}/preview",
            err="Imports are disabled: set FEATURES__CSV_IMPORT=true and restart the app.",
        )
    except CampaignNotFound:
        return _redirect("/imports", err="The target campaign no longer exists.")

    staged.confirmed_batch_id = str(summary.batch_id)
    staging.update_staged_upload(staged_dir, staged)

    if summary.status.value == "failed":
        return _redirect(
            f"/imports/{summary.batch_id}",
            err="The import could not be completed — see the failure reason on the batch.",
        )
    message = (
        f"Import complete: {summary.accepted_rows} accepted, {summary.rejected_rows} rejected, "
        f"{summary.duplicate_rows} duplicate, {summary.ambiguous_rows} ambiguous, "
        f"{summary.suppressed_rows} suppressed."
    )
    if summary.reused_existing_batch:
        message = "This exact file and mapping were already imported; showing the existing batch."
    return _redirect(f"/imports/{summary.batch_id}", ok=message)


@router.post("/imports/staged/{staged_id}/discard")
def staged_discard(staged_id: str) -> Response:
    try:
        staging.delete_staged_upload(get_settings().staged_uploads_dir, staged_id)
    except staging.StagedUploadNotFound:
        pass
    return _redirect("/imports", ok="Staged upload discarded. Nothing was imported.")


# --- Imports: batch + row detail ---------------------------------------------


@router.get("/imports/{batch_id}", response_class=HTMLResponse)
def batch_detail_page(
    request: Request, batch_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    parsed_id = _parse_uuid(batch_id)
    found = workbench.get_batch(db, parsed_id) if parsed_id else None
    if found is None:
        return _not_found(request, db, "That import batch does not exist.")
    batch, campaign = found

    outcome_filter = request.query_params.get("outcome") or None
    outcome = None
    if outcome_filter:
        try:
            outcome = ImportRowOutcome(outcome_filter)
        except ValueError:
            outcome_filter = None
    page = _page_number(request)
    rows, total_rows = workbench.list_batch_rows(
        db, batch.id, outcome=outcome, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE
    )
    is_salesnav = batch.source_format == ImportSourceFormat.SALES_NAVIGATOR
    is_pending = batch.status == ImportBatchStatus.PENDING
    return _render(
        request,
        db,
        "batch_detail.html",
        {
            "batch": batch,
            "campaign": campaign,
            "rows": rows,
            "total_rows": total_rows,
            "page": page,
            "pages": _pages(total_rows),
            "outcome_filter": outcome_filter,
            "is_salesnav": is_salesnav,
            "is_pending": is_pending,
            "enrich_enabled": is_salesnav and _enrichment_enabled(db),
            "sn_meta": batch.source_metadata if is_salesnav else None,
            "csv_import_enabled": operational.enabled(db, "csv_import"),
            "active_nav": "imports",
            "page_title": batch.filename or "Import batch",
        },
    )


# --- Pending staged batch: map -> preview -> confirm (Sales Navigator, UI-010) -
#
# A Sales Navigator capture (DAT-009) is staged as a PENDING ImportBatch whose
# raw rows already exist. These routes let the operator drive that exact batch
# through the SAME mapping, non-committing preview, and explicit confirmation the
# spreadsheet importer uses — processing the rows in place, never a second
# pipeline, never auto-confirmed.


def _load_pending_batch(db: Session, batch_id: str) -> tuple[Any, Any, list[Any]] | None:
    """Return (batch, campaign, raw_rows) for a PENDING batch, else None."""

    parsed_id = _parse_uuid(batch_id)
    found = workbench.get_batch(db, parsed_id) if parsed_id else None
    if found is None:
        return None
    batch, campaign = found
    if batch.status != ImportBatchStatus.PENDING:
        return None
    rows = workbench.list_import_rows(db, batch.id)
    return batch, campaign, rows


def _mapping_blocking_problems(problems: list[Any]) -> list[Any]:
    """Structural mapping problems that must block progress.

    A capture may legitimately lack a source for a required field (Sales
    Navigator never exposes ``company_domain``). ``missing_required`` is therefore
    surfaced as a non-blocking warning rather than a hard block: the rows that
    lack the field are still truthfully rejected by validation at preview/confirm,
    so no validation rule is bypassed. Structural errors (unknown column, unknown
    field, duplicate target) still block.
    """

    return [p for p in problems if p.code != "missing_required"]


@router.get("/imports/{batch_id}/map", response_class=HTMLResponse)
def batch_map_page(request: Request, batch_id: str, db: Session = Depends(get_db)) -> HTMLResponse:
    loaded = _load_pending_batch(db, batch_id)
    if loaded is None:
        return _not_found(
            request, db, "That staged batch does not exist or has already been processed."
        )
    batch, campaign, rows = loaded
    header = workbench.raw_row_header(rows)
    current = batch.column_mapping or mapping_service.suggest_mapping(header)
    check = mapping_service.check_mapping(current, header) if current else None
    warnings = _mapping_warnings(check)
    return _render(
        request,
        db,
        "batch_map.html",
        {
            "batch": batch,
            "campaign": campaign,
            "header": header,
            "current_mapping": current,
            "system_fields": list(mapping_service.SYSTEM_FIELDS),
            "required_fields": set(validation.REQUIRED_COLUMNS),
            "sample_rows": [dict(r.raw_data) for r in rows[:SAMPLE_ROWS_SHOWN]],
            "mapping_problems": [],
            "mapping_warnings": warnings,
            "active_nav": "imports",
            "page_title": f"Map columns — staged batch {batch.id}",
        },
    )


def _mapping_warnings(check: Any) -> list[str]:
    if check is None:
        return []
    return [p.message for p in check.problems if p.code == "missing_required"]


@router.post("/imports/{batch_id}/map")
async def batch_map_save(
    request: Request, batch_id: str, db: Session = Depends(get_db)
) -> Response:
    loaded = _load_pending_batch(db, batch_id)
    if loaded is None:
        return _redirect(
            "/imports", err="That staged batch does not exist or was already processed."
        )
    batch, campaign, rows = loaded
    header = workbench.raw_row_header(rows)

    form = await request.form()
    mapping: dict[str, str] = {}
    for key, value in form.multi_items():
        if key.startswith("map__") and str(value):
            mapping[key[len("map__") :]] = str(value)

    check = mapping_service.check_mapping(mapping, header)
    blocking = _mapping_blocking_problems(check.problems)
    if blocking:
        return _render(
            request,
            db,
            "batch_map.html",
            {
                "batch": batch,
                "campaign": campaign,
                "header": header,
                "current_mapping": mapping,
                "system_fields": list(mapping_service.SYSTEM_FIELDS),
                "required_fields": set(validation.REQUIRED_COLUMNS),
                "sample_rows": [dict(r.raw_data) for r in rows[:SAMPLE_ROWS_SHOWN]],
                "mapping_problems": blocking,
                "mapping_warnings": _mapping_warnings(check),
                "active_nav": "imports",
                "page_title": f"Map columns — staged batch {batch.id}",
            },
            status_code=400,
        )

    batch.column_mapping = mapping
    batch.mapper_version = mapping_service.MAPPER_VERSION
    db.commit()
    # A Sales Navigator capture has no company_domain source, so when the
    # domain-enrichment feature is on the operator resolves domains next; other
    # batches go straight to the preview (unchanged behaviour).
    if batch.source_format == ImportSourceFormat.SALES_NAVIGATOR and operational.enabled(
        db, "salesnav_domain_enrichment"
    ):
        return _redirect(f"/imports/{batch.id}/enrich")
    return _redirect(f"/imports/{batch.id}/preview")


# --- Sales Navigator company-domain enrichment (DAT-010) ---------------------
#
# A Sales Navigator capture carries no company_domain, so its rows reject until
# a domain is supplied. These routes let the operator look each unique company up
# through the official logo.dev Search Brands API and EXPLICITLY confirm one
# domain per company (a candidate, a manual override, or "unresolved"); the
# confirmed domain is overlaid onto matching rows at preview/confirm — the raw
# capture is never mutated, and nothing is ever auto-accepted.


def _enrichment_enabled(db: Session) -> bool:
    """Whether the logo.dev lookup may be offered on this request.

    ``db`` is a parameter because the switch is an administrator's durable
    setting rather than an environment variable, so answering it is a read
    against the database.
    """

    return operational.enabled(db, "salesnav_domain_enrichment")


def _render_enrich(
    request: Request, db: Session, batch: Any, campaign: Any, rows: list[Any]
) -> HTMLResponse:
    settings = get_settings()
    view = enrichment.build_view(db, batch=batch, rows=rows, column_mapping=batch.column_mapping)
    db.commit()  # persist any NOT_STARTED records ensure_records created
    return _render(
        request,
        db,
        "batch_enrich.html",
        {
            "batch": batch,
            "campaign": campaign,
            "view": view,
            "has_mapping": bool(batch.column_mapping),
            "api_key_configured": settings.has_logo_dev_key(),
            "active_nav": "imports",
            "page_title": f"Enrich domains — staged batch {batch.id}",
        },
    )


@router.get("/imports/{batch_id}/enrich", response_class=HTMLResponse)
def batch_enrich_page(
    request: Request, batch_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    refused = operational.refusal(db, "salesnav_domain_enrichment")
    if refused is not None:
        return _not_found(request, db, refused)
    loaded = _load_pending_batch(db, batch_id)
    if loaded is None:
        return _not_found(
            request, db, "That staged batch does not exist or has already been processed."
        )
    batch, campaign, rows = loaded
    return _render_enrich(request, db, batch, campaign, rows)


@router.post("/imports/{batch_id}/enrich/lookup")
def batch_enrich_lookup(request: Request, batch_id: str, db: Session = Depends(get_db)) -> Response:
    """Look up every not-yet-looked-up company once (idempotent, explicit)."""

    refused = operational.refusal(db, "salesnav_domain_enrichment")
    if refused is not None:
        # The specific cause, not a generic "not enabled". A missing provider
        # credential and an administrator's decision to switch the lookup off are
        # different problems with different fixes, and the operator needs to know
        # which one they have.
        return _redirect("/imports", err=refused)
    loaded = _load_pending_batch(db, batch_id)
    if loaded is None:
        return _redirect("/imports", err="That staged batch does not exist or was processed.")
    batch, _campaign, rows = loaded
    settings = get_settings()
    if not settings.has_logo_dev_key():
        return _redirect(
            f"/imports/{batch.id}/enrich",
            err="logo.dev API key is not configured (set LOGO_DEV_API_KEY). No lookup ran.",
        )
    try:
        summary = enrichment.run_pending_lookups(
            db,
            batch=batch,
            rows=rows,
            column_mapping=batch.column_mapping,
            api_key=settings.logo_dev_api_key or "",
            search_url=settings.logo_dev_search_url,
            timeout=settings.logo_dev_timeout_seconds,
            max_candidates=settings.logo_dev_max_candidates,
            actor="workbench",
        )
    except enrichment.ApiKeyMissing:
        db.rollback()
        return _redirect(
            f"/imports/{batch.id}/enrich",
            err="logo.dev API key is not configured (set LOGO_DEV_API_KEY). No lookup ran.",
        )
    db.commit()
    return _redirect(
        f"/imports/{batch.id}/enrich",
        ok=(
            f"Looked up {summary.looked_up} compan{'y' if summary.looked_up == 1 else 'ies'}"
            f"{f' (skipped {summary.skipped} already looked up)' if summary.skipped else ''}."
        ),
    )


@router.post("/imports/{batch_id}/enrich/refresh")
async def batch_enrich_refresh(
    request: Request, batch_id: str, db: Session = Depends(get_db)
) -> Response:
    """Explicitly re-look-up one company (the only path that re-calls logo.dev)."""

    refused = operational.refusal(db, "salesnav_domain_enrichment")
    if refused is not None:
        # The specific cause, not a generic "not enabled". A missing provider
        # credential and an administrator's decision to switch the lookup off are
        # different problems with different fixes, and the operator needs to know
        # which one they have.
        return _redirect("/imports", err=refused)
    loaded = _load_pending_batch(db, batch_id)
    if loaded is None:
        return _redirect("/imports", err="That staged batch does not exist or was processed.")
    batch, _campaign, rows = loaded
    settings = get_settings()
    form = await request.form()
    key = str(form.get("company_key", ""))
    if not settings.has_logo_dev_key():
        return _redirect(
            f"/imports/{batch.id}/enrich",
            err="logo.dev API key is not configured (set LOGO_DEV_API_KEY). No lookup ran.",
        )
    enrichment.ensure_records(db, batch=batch, rows=rows, column_mapping=batch.column_mapping)
    record = next(
        (
            r
            for r in enrichment.build_view(
                db, batch=batch, rows=rows, column_mapping=batch.column_mapping
            ).companies
            if r.record.company_key == key
        ),
        None,
    )
    if record is None:
        return _redirect(f"/imports/{batch.id}/enrich", err="Unknown company for this batch.")
    try:
        enrichment.run_lookup(
            db,
            record=record.record,
            api_key=settings.logo_dev_api_key or "",
            search_url=settings.logo_dev_search_url,
            timeout=settings.logo_dev_timeout_seconds,
            max_candidates=settings.logo_dev_max_candidates,
            actor="workbench",
            force=True,
        )
    except enrichment.ApiKeyMissing:
        db.rollback()
        return _redirect(
            f"/imports/{batch.id}/enrich",
            err="logo.dev API key is not configured (set LOGO_DEV_API_KEY). No lookup ran.",
        )
    db.commit()
    return _redirect(f"/imports/{batch.id}/enrich", ok="Re-looked-up the company.")


@router.post("/imports/{batch_id}/enrich/confirm")
async def batch_enrich_confirm(
    request: Request, batch_id: str, db: Session = Depends(get_db)
) -> Response:
    """Apply the operator's explicit domain decision for one company."""

    refused = operational.refusal(db, "salesnav_domain_enrichment")
    if refused is not None:
        # The specific cause, not a generic "not enabled". A missing provider
        # credential and an administrator's decision to switch the lookup off are
        # different problems with different fixes, and the operator needs to know
        # which one they have.
        return _redirect("/imports", err=refused)
    loaded = _load_pending_batch(db, batch_id)
    if loaded is None:
        return _redirect("/imports", err="That staged batch does not exist or was processed.")
    batch, _campaign, rows = loaded
    # Ensure records exist (mapping-consistent) before applying a decision.
    enrichment.ensure_records(db, batch=batch, rows=rows, column_mapping=batch.column_mapping)

    form = await request.form()
    key = str(form.get("company_key", ""))
    action_raw = str(form.get("action", ""))
    try:
        source = EnrichmentConfirmationSource(action_raw)
    except ValueError:
        return _redirect(f"/imports/{batch.id}/enrich", err="Choose select, manual, or unresolved.")
    if source is EnrichmentConfirmationSource.CANDIDATE:
        domain: str | None = str(form.get("candidate_domain", "")).strip() or None
    elif source is EnrichmentConfirmationSource.MANUAL:
        domain = str(form.get("manual_domain", "")).strip() or None
    else:
        domain = None
    note = str(form.get("note", "")).strip() or None

    try:
        record = enrichment.confirm_company(
            db,
            batch=batch,
            company_key_value=key,
            source=source,
            domain=domain,
            actor="workbench",
            note=note,
        )
    except enrichment.EnrichmentError as exc:
        db.rollback()
        return _redirect(f"/imports/{batch.id}/enrich", err=str(exc))
    db.commit()
    if record.confirmation_status.value == "confirmed":
        msg = (
            f"“{record.company_name}” → {record.confirmed_domain} "
            f"applied to {record.row_count} row(s)."
        )
    else:
        msg = f"“{record.company_name}” left unresolved; its rows stay rejected."
    return _redirect(f"/imports/{batch.id}/enrich", ok=msg)


@router.get("/imports/{batch_id}/preview", response_class=HTMLResponse)
def batch_preview_page(
    request: Request, batch_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    loaded = _load_pending_batch(db, batch_id)
    if loaded is None:
        return _not_found(
            request, db, "That staged batch does not exist or has already been processed."
        )
    batch, campaign, rows = loaded
    overlay = enrichment.domain_overlay(db, batch.id)
    result = preview_pending_batch(
        db, rows=rows, column_mapping=batch.column_mapping, domain_overlay=overlay
    )
    return _render(
        request,
        db,
        "batch_preview.html",
        {
            "batch": batch,
            "campaign": campaign,
            "preview": result,
            "shown_rows": result.rows[:PREVIEW_ROWS_SHOWN],
            "has_mapping": bool(batch.column_mapping),
            "csv_import_enabled": operational.enabled(db, "csv_import"),
            "enrich_enabled": (
                batch.source_format == ImportSourceFormat.SALES_NAVIGATOR
                and _enrichment_enabled(db)
            ),
            "active_nav": "imports",
            "page_title": f"Preview — staged batch {batch.id}",
        },
    )


@router.post("/imports/{batch_id}/confirm")
def batch_confirm(request: Request, batch_id: str, db: Session = Depends(get_db)) -> Response:
    parsed_id = _parse_uuid(batch_id)
    found = workbench.get_batch(db, parsed_id) if parsed_id else None
    if found is None:
        return _redirect("/imports", err="That staged batch does not exist.")
    batch, _campaign = found

    if batch.status == ImportBatchStatus.COMPLETED:
        return _redirect(
            f"/imports/{batch.id}", ok="This staged batch was already imported; showing outcomes."
        )
    if batch.status != ImportBatchStatus.PENDING:
        return _redirect(
            f"/imports/{batch.id}",
            err="This staged batch cannot be processed in its current state.",
        )

    overlay = enrichment.domain_overlay(db, batch.id)
    try:
        summary = process_pending_batch(
            db, batch=batch, column_mapping=batch.column_mapping, domain_overlay=overlay
        )
    except FeatureDisabledError:
        return _redirect(
            f"/imports/{batch.id}/preview",
            err="Imports are disabled: set FEATURES__CSV_IMPORT=true and restart the app.",
        )
    except (CampaignNotFound, BatchNotProcessable) as exc:
        return _redirect(f"/imports/{batch.id}", err=str(exc))

    if summary.status.value == "failed":
        return _redirect(
            f"/imports/{batch.id}",
            err="The import could not be completed — see the failure reason on the batch.",
        )
    message = (
        f"Import complete: {summary.accepted_rows} accepted, {summary.rejected_rows} rejected, "
        f"{summary.duplicate_rows} duplicate, {summary.ambiguous_rows} ambiguous, "
        f"{summary.suppressed_rows} suppressed."
    )
    return _redirect(f"/imports/{batch.id}", ok=message)


_COMPARE_FIELDS: tuple[str, ...] = (
    validation.REQUIRED_COLUMNS + validation.RECOMMENDED_COLUMNS + validation.PROVENANCE_COLUMNS
)


@router.get("/imports/{batch_id}/rows/{row_id}", response_class=HTMLResponse)
def row_detail_page(
    request: Request, batch_id: str, row_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    parsed_batch = _parse_uuid(batch_id)
    parsed_row = _parse_uuid(row_id)
    found = workbench.get_batch(db, parsed_batch) if parsed_batch else None
    if found is None:
        return _not_found(request, db, "That import batch does not exist.")
    batch, _campaign = found
    row = workbench.get_batch_row(db, batch.id, parsed_row) if parsed_row else None
    if row is None:
        return _not_found(request, db, "That import row does not exist in this batch.")

    # Original vs normalized, respecting the batch's confirmed column mapping.
    raw = dict(row.row.raw_data)
    mapping = batch.column_mapping or {}
    reverse = {target: source for source, target in mapping.items()}
    normalized = row.validation.normalized_data if row.validation else None

    comparison: list[tuple[str, str | None, str | None, bool]] = []
    for field in _COMPARE_FIELDS:
        source_column = reverse.get(field, field)
        original = raw.get(source_column)
        if original is None and not mapping:
            # Unmapped CSVs matched headers case-insensitively; look again.
            for key, value in raw.items():
                if isinstance(key, str) and key.strip().lower() == field:
                    original = value
                    break
        norm_value = normalized.get(field) if normalized else None
        if original in (None, "") and norm_value in (None, ""):
            continue
        changed = (
            original is not None
            and norm_value is not None
            and str(original).strip() != str(norm_value)
        )
        comparison.append((field, original, norm_value, changed))

    mapped_sources = (
        set(mapping.keys())
        if mapping
        else {key for key in raw if isinstance(key, str) and key.strip().lower() in _COMPARE_FIELDS}
    )
    unmapped = [(key, value) for key, value in raw.items() if key not in mapped_sources]

    return _render(
        request,
        db,
        "row_detail.html",
        {
            "batch": batch,
            "row": row,
            "comparison": comparison,
            "unmapped_columns": unmapped,
            "active_nav": "imports",
            "page_title": f"Row {row.row.row_number}",
        },
    )


# --- Ambiguity review & identity resolution (DAT-004) ------------------------

_ROW_ACTIONS = {
    IdentityResolutionType.ASSIGN_EXISTING,
    IdentityResolutionType.CREATE_NEW,
    IdentityResolutionType.MARK_SEPARATE,
}


def _parse_action(value: str | None) -> IdentityResolutionType | None:
    if not value:
        return None
    try:
        return IdentityResolutionType(value)
    except ValueError:
        return None


def _idempotency_key(
    row_id: uuid.UUID,
    action: IdentityResolutionType,
    target: uuid.UUID | None,
    loser: uuid.UUID | None = None,
) -> str:
    """A deterministic key so a repeated confirm of the same decision is a no-op."""

    return f"row:{row_id}:{action.value}:{target or '-'}:{loser or '-'}"


@router.get("/review", response_class=HTMLResponse)
def review_queue_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    page = _page_number(request)
    items, total = identity.list_review_queue(db, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    return _render(
        request,
        db,
        "review_queue.html",
        {
            "items": items,
            "total": total,
            "page": page,
            "pages": _pages(total),
            "active_nav": "review",
            "page_title": "Ambiguity review",
        },
    )


@router.get("/review/rows/{row_id}", response_class=HTMLResponse)
def review_detail_page(
    request: Request, row_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    parsed = _parse_uuid(row_id)
    review = identity.get_row_review(db, parsed) if parsed else None
    if review is None:
        return _not_found(
            request, db, "That ambiguous row does not exist, or it has already been resolved."
        )
    return _render(
        request,
        db,
        "review_detail.html",
        {
            "review": review,
            "active_nav": "review",
            "page_title": f"Resolve row {review.row.row_number}",
        },
    )


@router.post("/review/rows/{row_id}/preview", response_class=HTMLResponse)
async def review_preview(request: Request, row_id: str, db: Session = Depends(get_db)) -> Response:
    parsed = _parse_uuid(row_id)
    review = identity.get_row_review(db, parsed) if parsed else None
    if review is None or parsed is None:
        return _redirect("/review", err="That ambiguous row does not exist or was resolved.")

    form = await request.form()
    action = _parse_action(str(form.get("action", "")))
    if action is None:
        return _redirect(f"/review/rows/{row_id}", err="Choose a resolution action.")
    target = _parse_uuid(str(form.get("target_contact_id", "")) or None)
    loser = _parse_uuid(str(form.get("merged_contact_id", "")) or None)

    try:
        preview = identity.preview_row_resolution(
            db,
            import_row_id=parsed,
            action=action,
            target_contact_id=target,
            merged_contact_id=loser,
        )
    except identity.ResolutionError as exc:
        return _redirect(f"/review/rows/{row_id}", err=str(exc))

    if not preview.ok:
        return _redirect(f"/review/rows/{row_id}", err=preview.blocked_reason or "Cannot resolve.")

    return _render(
        request,
        db,
        "review_confirm.html",
        {
            "review": review,
            "preview": preview,
            "action": action,
            "target_contact_id": target,
            "merged_contact_id": loser,
            "active_nav": "review",
            "page_title": f"Confirm — row {review.row.row_number}",
        },
    )


@router.post("/review/rows/{row_id}/resolve")
async def review_resolve(request: Request, row_id: str, db: Session = Depends(get_db)) -> Response:
    parsed = _parse_uuid(row_id)
    if parsed is None:
        return _redirect("/review", err="That ambiguous row reference is invalid.")

    form = await request.form()
    action = _parse_action(str(form.get("action", "")))
    if action is None:
        return _redirect(f"/review/rows/{row_id}", err="Choose a resolution action.")
    target = _parse_uuid(str(form.get("target_contact_id", "")) or None)
    loser = _parse_uuid(str(form.get("merged_contact_id", "")) or None)
    reason = str(form.get("reason", "")).strip() or None

    try:
        if action is IdentityResolutionType.MERGE:
            key = _idempotency_key(parsed, action, target, loser)
            result = identity.merge_contacts(
                db,
                survivor_id=target,  # type: ignore[arg-type]
                loser_id=loser,  # type: ignore[arg-type]
                idempotency_key=key,
                actor="workbench",
                reason=reason,
                import_row_id=parsed,
            )
        elif action in _ROW_ACTIONS:
            key = _idempotency_key(parsed, action, target)
            result = identity.resolve_row(
                db,
                import_row_id=parsed,
                action=action,
                idempotency_key=key,
                actor="workbench",
                reason=reason,
                target_contact_id=target,
                merged_contact_id=loser,
            )
        else:
            return _redirect(f"/review/rows/{row_id}", err="Unknown resolution action.")
    except identity.ResolutionError as exc:
        return _redirect(f"/review/rows/{row_id}", err=str(exc))
    except Exception:
        db.rollback()
        return _redirect(
            f"/review/rows/{row_id}",
            err="The resolution could not be completed and was rolled back. Nothing changed.",
        )

    contact_id = result.resolution.target_contact_id
    if result.reused:
        note = "This decision was already recorded; nothing changed."
    else:
        note = f"Resolved by {action.value.replace('_', ' ')}."
    if contact_id is not None:
        return _redirect(f"/contacts/{contact_id}", ok=note)
    return _redirect("/review", ok=note)


@router.get("/captures/{capture_id}", response_class=HTMLResponse)
def capture_detail_page(
    request: Request, capture_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """A saved person who is not a canonical contact yet.

    Separate from the contact page on purpose: this record has no email, no
    company domain and no campaign history, and a page that borrowed the contact
    layout would imply data that does not exist.
    """

    parsed_id = _parse_uuid(capture_id)
    detail = crm_detail.get_capture_detail(db, parsed_id) if parsed_id else None
    if detail is None:
        return _not_found(request, db, "That capture does not exist.")

    return _render(
        request,
        db,
        "capture_detail.html",
        {
            "detail": detail,
            "all_labels": crm_annotations.all_labels(db),
            "active_nav": "contacts",
            "page_title": detail.full_name,
        },
    )


# --- Labels and notes (contact or pending capture) ---------------------------


def _annotation_subject(
    db: Session, *, contact_id: str | None = None, capture_id: str | None = None
) -> tuple[crm_annotations.Subject | None, str]:
    """Resolve the annotation subject and the URL to return the operator to."""

    if contact_id is not None:
        parsed = _parse_uuid(contact_id)
        back = f"/contacts/{contact_id}"
        if parsed is None:
            return None, back
        try:
            return crm_annotations.resolve_subject(db, contact_id=parsed), back
        except crm_annotations.AnnotationError:
            return None, back

    parsed = _parse_uuid(capture_id or "")
    back = f"/captures/{capture_id}"
    if parsed is None:
        return None, back
    try:
        return crm_annotations.resolve_subject(db, capture_id=parsed), back
    except crm_annotations.AnnotationError:
        return None, back


def _apply_label(db: Session, subject: crm_annotations.Subject, name: str, back: str) -> Response:
    try:
        label, applied = crm_annotations.add_label(db, subject, name=name)
    except crm_annotations.AnnotationError as exc:
        return _redirect(back, err=str(exc))
    db.commit()
    if not applied:
        return _redirect(back, ok=f"{label.name} was already applied.")
    return _redirect(back, ok=f"Applied {label.name}.")


@router.post("/captures/{capture_id}/labels")
async def capture_add_label(
    request: Request, capture_id: str, db: Session = Depends(get_db)
) -> Response:
    subject, back = _annotation_subject(db, capture_id=capture_id)
    if subject is None:
        return _redirect("/contacts", err="That capture does not exist.")
    form = await request.form()
    return _apply_label(db, subject, str(form.get("label") or ""), back)


@router.post("/captures/{capture_id}/labels/{slug}/remove")
def capture_remove_label(capture_id: str, slug: str, db: Session = Depends(get_db)) -> Response:
    subject, back = _annotation_subject(db, capture_id=capture_id)
    if subject is None:
        return _redirect("/contacts", err="That capture does not exist.")
    removed = crm_annotations.remove_label(db, subject, slug=slug)
    db.commit()
    return _redirect(back, ok="Label removed." if removed else "That label was not applied.")


def _append_note(db: Session, subject: crm_annotations.Subject, text: str, back: str) -> Response:
    try:
        crm_annotations.add_note(db, subject, text=text)
    except crm_annotations.AnnotationError as exc:
        return _redirect(back, err=str(exc))
    db.commit()
    return _redirect(back, ok="Note added.")


@router.post("/captures/{capture_id}/notes")
async def capture_add_note(
    request: Request, capture_id: str, db: Session = Depends(get_db)
) -> Response:
    subject, back = _annotation_subject(db, capture_id=capture_id)
    if subject is None:
        return _redirect("/contacts", err="That capture does not exist.")
    form = await request.form()
    return _append_note(db, subject, str(form.get("note") or ""), back)


# --- Phase 2: email verification --------------------------------------------


def _verification_available(db: Session) -> bool:
    """Whether either half of email verification is switched on.

    ``db`` is a parameter because both switches are administrators' durable
    settings rather than environment variables, and reading them is a query.
    """

    features = operational.effective_flags(db)
    return features.email_generation or features.millionverifier


WORKER_ID = "workbench-local"


@router.get("/verification", response_class=HTMLResponse, name="verification")
def verification_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    settings = get_settings()
    if not _verification_available(db):
        return _render(
            request,
            db,
            "unavailable.html",
            {
                "section_title": "Email Verification",
                "active_nav": "verification",
                "page_title": "Email Verification",
            },
        )
    features = operational.effective_flags(db, settings)
    console = verification_console.load_console(db)
    return _render(
        request,
        db,
        "verification.html",
        {
            "console": console,
            "generation_enabled": features.email_generation,
            "millionverifier_enabled": features.millionverifier,
            "active_nav": "verification",
            "page_title": "Email Verification",
        },
    )


def _verification_provider(settings: Settings) -> VerificationProvider:
    """The provider the workbench should actually use.

    These three routes asked for ``get_provider(settings)``, whose default is
    ``live=False`` — so every verification an operator triggered from the
    workbench was simulated, while the Verification Agent verified the same
    addresses for real. Two paths quietly disagreeing about whether a result came
    from MillionVerifier is worse than either answer on its own: it makes the
    evidence table untrustworthy without looking wrong.

    Asking for live is safe by construction. ``build_provider`` still returns the
    simulator unless a real, non-test key is configured, so an installation
    without credentials keeps behaving exactly as before.
    """

    return verification_service.get_provider(settings, live=True)


@router.post("/verification/run")
def verification_run(db: Session = Depends(get_db)) -> Response:
    settings = get_settings()
    if not operational.enabled(db, "millionverifier", settings):
        return _redirect("/verification", err="MillionVerifier is disabled.")
    provider = _verification_provider(settings)
    processed = verification_service.run_worker(
        db, provider=provider, settings=settings, worker_id=WORKER_ID
    )
    db.commit()
    label = "simulator" if provider.simulated else provider.name
    return _redirect("/verification", ok=f"Processed {len(processed)} job(s) via {label}.")


@router.post("/verification/recover")
def verification_recover(db: Session = Depends(get_db)) -> Response:
    if not _verification_available(db):
        return _redirect("/verification", err="Email verification is disabled.")
    reclaimed = verification_queue.recover_stale_jobs(db)
    for job in reclaimed:
        verification_usage.record_usage(
            db,
            event_type=VerificationUsageEventType.RECOVERED,
            provider="millionverifier",
            email=job.email,
            contact_id=job.contact_id,
            job_id=job.id,
            reason="recovered by explicit operator sweep",
        )
    db.commit()
    return _redirect(
        "/verification", ok=f"Recovered {len(reclaimed)} interrupted job(s) back to pending."
    )


@router.post("/verification/bulk")
async def verification_bulk(request: Request, db: Session = Depends(get_db)) -> Response:
    settings = get_settings()
    if not operational.enabled(db, "millionverifier", settings):
        return _redirect("/verification", err="MillionVerifier is disabled.")
    form = await request.form()
    campaign_id = _parse_uuid(str(form.get("campaign_id", "")))
    query = select(Contact)
    if campaign_id is not None:
        from app.models.campaign import CampaignContact

        query = query.join(CampaignContact, CampaignContact.contact_id == Contact.id).where(
            CampaignContact.campaign_id == campaign_id
        )
    contacts = list(db.scalars(query.limit(500)).all())
    enqueued = 0
    reused = 0
    review = 0
    for contact in contacts:
        outcome = verification_service.prepare_and_enqueue_contact(
            db, contact, settings=settings, campaign_id=campaign_id
        )
        if outcome.needs_review:
            review += 1
        elif outcome.reused_evidence is not None:
            reused += 1
        elif outcome.job is not None:
            enqueued += 1
    provider = _verification_provider(settings)
    verification_service.run_worker(
        db, provider=provider, settings=settings, max_jobs=1000, worker_id=WORKER_ID
    )
    db.commit()
    return _redirect(
        "/verification",
        ok=(
            f"Bulk verification: {enqueued} enqueued, {reused} reused from cache, "
            f"{review} routed to review."
        ),
    )


# --- Workbench Agent monitor and controls (MVP-01B) --------------------------
#
# The operator control room over the Phase 2 execution backbone. These routes are
# the same thin adapters as everything else on this page router: they resolve the
# reader or the command surface, hand typed view models to a template, and turn
# one form post into one Phase 2 service call.
#
# No route here writes a row, computes a count, or decides what a state means.
# The Agent order comes from the Phase 2 registry, the control precedence from
# ``agents.controls.effective_control``, and every command from a Phase 2
# service. A page that cannot answer a question truthfully says so instead.


def _agent_workbench_available(db: Session) -> bool:
    """Whether the Agent Workbench routes may answer on this request.

    ``db`` is a parameter because the switch is an administrator's durable
    setting rather than an environment variable, so reading it needs a query.
    """

    return operational.enabled(db, "agent_workbench")


def _reader(db: Session) -> workbench_agents.PhaseTwoWorkbenchReader:
    """The production read model, constructed directly around this request.

    No registry, no environment switch, no transport to register: production has
    exactly one backend and it is the real Phase 2 one. Tests that want a
    deterministic read model override this function's caller through the normal
    FastAPI dependency mechanism.
    """

    return workbench_agents.PhaseTwoWorkbenchReader(db)


def _commands(db: Session) -> workbench_agents.WorkbenchCommands:
    return workbench_agents.WorkbenchCommands(db, actor=OPERATOR_ACTOR)


def _agent_workbench_unavailable(request: Request, db: Session) -> HTMLResponse:
    return _render(
        request,
        db,
        "unavailable.html",
        {
            "section_title": "Workbench",
            "active_nav": "agent-workbench",
            "page_title": "Workbench",
        },
    )


def _agent_labels() -> dict[str, str]:
    return {spec.identifier.value: spec.display_name for spec in AGENT_SPECS.values()}


def _parse_agent_id(raw: str) -> AgentIdentifier | None:
    try:
        return AgentIdentifier(raw)
    except ValueError:
        return None


def _parse_control_status(raw: str | None) -> AgentControlStatus | None:
    try:
        return AgentControlStatus((raw or "").strip().lower())
    except ValueError:
        return None


def _expected_version(form: Any, field: str = "expected_version") -> int | None:
    """The control version the page was rendered with.

    An absent or blank field means "the page saw no stored control". That is a
    real claim and is compared as one, so a control created after the page
    rendered is a conflict rather than a silent overwrite.
    """

    raw = str(form.get(field, "")).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _command_redirect(url: str, outcome: workbench_agents.CommandOutcome) -> Response:
    """Report the outcome Phase 2 returned, never the operator's intention."""

    if outcome.accepted:
        return _redirect(url, ok=outcome.summary)
    reason = outcome.refusal_reason
    return _redirect(url, err=f"{outcome.message}{(' ' + reason) if reason else ''}")


@router.get("/workbench", response_class=HTMLResponse)
def agent_workbench_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if not _agent_workbench_available(db):
        return _agent_workbench_unavailable(request, db)
    return _render(
        request,
        db,
        "agent_workbench.html",
        {
            "live_seconds": LIVE_REFRESH_SECONDS,
            "overview": _reader(db).overview(),
            "agent_labels": _agent_labels(),
            "active_nav": "agent-workbench",
            "page_title": "Workbench",
        },
    )


@router.get("/workbench/jobs", response_class=HTMLResponse)
def agent_jobs_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if not _agent_workbench_available(db):
        return _agent_workbench_unavailable(request, db)
    agent_raw = request.query_params.get("agent") or None
    status_raw = request.query_params.get("status") or None
    campaign_raw = request.query_params.get("campaign") or None
    agent_id = _parse_agent_id(agent_raw) if agent_raw else None
    campaign_id = _parse_uuid(campaign_raw) if campaign_raw else None
    page = _page_number(request)
    listing = _reader(db).jobs(
        agent_id=agent_id,
        campaign_id=campaign_id,
        status=status_raw,
        limit=PAGE_SIZE,
        offset=(page - 1) * PAGE_SIZE,
    )

    def _query(**overrides: str | None) -> str:
        params: dict[str, str | None] = {
            "agent": agent_id.value if agent_id else None,
            "campaign": str(campaign_id) if campaign_id else None,
            "status": listing.status_filter,
        }
        params.update(overrides)
        pairs = {key: value for key, value in params.items() if value}
        return f"/workbench/jobs{'?' + urlencode(pairs) if pairs else ''}"

    return _render(
        request,
        db,
        "agent_jobs.html",
        {
            "live_seconds": LIVE_REFRESH_SECONDS,
            "listing": listing,
            "agent_specs": list(AGENT_SPECS.values()),
            "job_states": list(workbench_views.PUBLIC_JOB_STATES),
            "agent_labels": _agent_labels(),
            "base_url": _query(),
            "base_url_without_status": _query(status=None),
            "base_url_without_agent": _query(agent=None),
            "page": page,
            "pages": _pages(listing.total),
            "active_nav": "agent-workbench",
            "page_title": "Agent jobs",
        },
    )


@router.get("/workbench/jobs/{job_id}", response_class=HTMLResponse)
def agent_job_detail_page(
    request: Request, job_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    if not _agent_workbench_available(db):
        return _agent_workbench_unavailable(request, db)
    parsed = _parse_uuid(job_id)
    job = _reader(db).job(parsed) if parsed else None
    if job is None:
        return _not_found(request, db, "That Agent job does not exist.")
    return _render(
        request,
        db,
        "agent_job_detail.html",
        {
            "job": job,
            "agent_labels": _agent_labels(),
            "active_nav": "agent-workbench",
            "page_title": f"Job {job.job_id}",
        },
    )


@router.post("/workbench/jobs/{job_id}/retry")
async def agent_job_retry(request: Request, job_id: str, db: Session = Depends(get_db)) -> Response:
    if not _agent_workbench_available(db):
        return _redirect("/", err="The Workbench Agent monitor is disabled.")
    parsed = _parse_uuid(job_id)
    if parsed is None:
        return _redirect("/workbench/jobs", err="That is not a valid job id.")
    form = await request.form()
    reason = str(form.get("reason", "")).strip() or None
    back = str(form.get("back", "")).strip() or f"/workbench/jobs/{job_id}"
    try:
        outcome = _commands(db).retry_job(parsed, reason=reason)
    except workbench_agents.WorkbenchCommandError as exc:
        db.rollback()
        return _redirect("/workbench/jobs", err=str(exc))
    db.commit()
    return _command_redirect(back, outcome)


@router.get("/workbench/agents/{agent_id}", response_class=HTMLResponse)
def agent_detail_page(
    request: Request, agent_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    if not _agent_workbench_available(db):
        return _agent_workbench_unavailable(request, db)
    parsed = _parse_agent_id(agent_id)
    if parsed is None:
        return _not_found(request, db, "That Agent is not in the registry.")
    campaign_id = _parse_uuid(request.query_params.get("campaign"))
    detail = _reader(db).agent_detail(parsed, campaign_id=campaign_id)
    if detail is None:
        return _not_found(request, db, "That Campaign does not exist.")
    return _render(
        request,
        db,
        "agent_detail.html",
        {
            "detail": detail,
            "agent_labels": _agent_labels(),
            "active_nav": "agent-workbench",
            "page_title": detail.display_name,
        },
    )


@router.post("/workbench/agents/{agent_id}/control")
async def agent_set_control(
    request: Request, agent_id: str, db: Session = Depends(get_db)
) -> Response:
    if not _agent_workbench_available(db):
        return _redirect("/", err="The Workbench Agent monitor is disabled.")
    parsed = _parse_agent_id(agent_id)
    if parsed is None:
        return _redirect("/workbench", err="That Agent is not in the registry.")
    back = f"/workbench/agents/{parsed.value}"
    form = await request.form()
    status = _parse_control_status(str(form.get("status", "")))
    if status is None:
        return _redirect(back, err="Choose enabled, paused, or disabled. Nothing changed.")
    outcome = _commands(db).set_global_agent_status(
        parsed,
        status,
        expected_version=_expected_version(form),
        reason=str(form.get("reason", "")).strip() or None,
    )
    db.commit()
    return _command_redirect(back, outcome)


@router.post("/workbench/agents/sending/stop")
async def agent_sending_stop(request: Request, db: Session = Depends(get_db)) -> Response:
    """Stop new Sending work everywhere.

    Typed-confirmation guarded, like the destructive Local Tools actions: it is
    the one control on these pages that changes what the whole system will do
    next.
    """

    back = f"/workbench/agents/{AgentIdentifier.SENDING.value}"
    if not _agent_workbench_available(db):
        return _redirect("/", err="The Workbench Agent monitor is disabled.")
    form = await request.form()
    if str(form.get("confirm", "")).strip().upper() != "STOP":
        return _redirect(back, err="Type STOP to confirm. Nothing changed.")
    outcome = _commands(db).stop_sending(
        expected_version=_expected_version(form),
        reason=str(form.get("reason", "")).strip() or None,
    )
    db.commit()
    return _command_redirect(back, outcome)


@router.post("/workbench/agents/sending/resume")
async def agent_sending_resume(request: Request, db: Session = Depends(get_db)) -> Response:
    """Ask Phase 2 to allow Sending again, through its own safety checks."""

    back = f"/workbench/agents/{AgentIdentifier.SENDING.value}"
    if not _agent_workbench_available(db):
        return _redirect("/", err="The Workbench Agent monitor is disabled.")
    form = await request.form()
    if str(form.get("confirm", "")).strip().upper() != "RESUME SENDING":
        return _redirect(back, err="Type RESUME SENDING to confirm. Nothing changed.")
    outcome = _commands(db).resume_sending(
        expected_version=_expected_version(form),
        reason=str(form.get("reason", "")).strip() or None,
    )
    db.commit()
    return _command_redirect(back, outcome)


@router.get("/workbench/campaigns/{campaign_id}", response_class=HTMLResponse)
def agent_campaign_page(
    request: Request, campaign_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    if not _agent_workbench_available(db):
        return _agent_workbench_unavailable(request, db)
    parsed = _parse_uuid(campaign_id)
    stage = (
        _parse_agent_id(request.query_params.get("stage") or "")
        if request.query_params.get("stage")
        else None
    )
    page = _page_number(request)
    execution = (
        _reader(db).campaign_execution(
            parsed, stage=stage, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE
        )
        if parsed
        else None
    )
    if execution is None:
        return _not_found(request, db, "That Campaign does not exist.")
    return _render(
        request,
        db,
        "agent_campaign.html",
        {
            "live_seconds": LIVE_REFRESH_SECONDS,
            "execution": execution,
            "agent_specs": list(AGENT_SPECS.values()),
            "stage_filter": stage.value if stage else None,
            "agent_labels": _agent_labels(),
            "page": page,
            "pages": _pages(execution.contact_total),
            "active_nav": "agent-workbench",
            "page_title": execution.name,
        },
    )


@router.post("/workbench/campaigns/{campaign_id}/agents/{agent_id}/override")
async def agent_campaign_override(
    request: Request, campaign_id: str, agent_id: str, db: Session = Depends(get_db)
) -> Response:
    if not _agent_workbench_available(db):
        return _redirect("/", err="The Workbench Agent monitor is disabled.")
    parsed_campaign = _parse_uuid(campaign_id)
    parsed_agent = _parse_agent_id(agent_id)
    if parsed_campaign is None or parsed_agent is None:
        return _redirect("/workbench", err="That Campaign or Agent does not exist.")
    back_default = f"/workbench/campaigns/{campaign_id}"
    form = await request.form()
    back = str(form.get("back", "")).strip() or back_default
    status = _parse_control_status(str(form.get("status", "")))
    if status is None:
        return _redirect(back, err="Choose enabled, paused, or disabled. Nothing changed.")
    try:
        outcome = _commands(db).set_campaign_override(
            parsed_campaign,
            parsed_agent,
            status,
            expected_version=_expected_version(form),
            reason=str(form.get("reason", "")).strip() or None,
        )
    except workbench_agents.WorkbenchCommandError as exc:
        db.rollback()
        return _redirect("/workbench", err=str(exc))
    db.commit()
    return _command_redirect(back, outcome)


@router.post("/workbench/campaigns/{campaign_id}/agents/{agent_id}/override/clear")
async def agent_campaign_override_clear(
    request: Request, campaign_id: str, agent_id: str, db: Session = Depends(get_db)
) -> Response:
    if not _agent_workbench_available(db):
        return _redirect("/", err="The Workbench Agent monitor is disabled.")
    parsed_campaign = _parse_uuid(campaign_id)
    parsed_agent = _parse_agent_id(agent_id)
    if parsed_campaign is None or parsed_agent is None:
        return _redirect("/workbench", err="That Campaign or Agent does not exist.")
    back_default = f"/workbench/campaigns/{campaign_id}"
    form = await request.form()
    back = str(form.get("back", "")).strip() or back_default
    try:
        outcome = _commands(db).clear_campaign_override(
            parsed_campaign, parsed_agent, expected_version=_expected_version(form)
        )
    except workbench_agents.WorkbenchCommandError as exc:
        db.rollback()
        return _redirect("/workbench", err=str(exc))
    db.commit()
    return _command_redirect(back, outcome)


@router.get(
    "/workbench/campaigns/{campaign_id}/contacts/{campaign_contact_id}",
    response_class=HTMLResponse,
)
def agent_contact_execution_page(
    request: Request,
    campaign_id: str,
    campaign_contact_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if not _agent_workbench_available(db):
        return _agent_workbench_unavailable(request, db)
    parsed_campaign = _parse_uuid(campaign_id)
    parsed_membership = _parse_uuid(campaign_contact_id)
    execution = (
        _reader(db).contact_execution(parsed_campaign, parsed_membership)
        if parsed_campaign and parsed_membership
        else None
    )
    if execution is None:
        return _not_found(request, db, "That Campaign Contact does not exist.")
    return _render(
        request,
        db,
        "agent_contact_execution.html",
        {
            "live_seconds": LIVE_REFRESH_SECONDS,
            "execution": execution,
            "agent_labels": _agent_labels(),
            "active_nav": "agent-workbench",
            "page_title": execution.contact_label,
        },
    )


@router.post("/workbench/campaigns/{campaign_id}/contacts/{campaign_contact_id}/{command}")
async def agent_contact_command(
    request: Request,
    campaign_id: str,
    campaign_contact_id: str,
    command: str,
    db: Session = Depends(get_db),
) -> Response:
    """Pause, resume, retry, or skip the current stage for one Campaign Contact.

    One route, four named commands, because they share a target and a redirect
    and differ only in which Phase 2 service they call. An unrecognised command
    is refused rather than guessed.
    """

    if not _agent_workbench_available(db):
        return _redirect("/", err="The Workbench Agent monitor is disabled.")
    back = f"/workbench/campaigns/{campaign_id}/contacts/{campaign_contact_id}"
    parsed = _parse_uuid(campaign_contact_id)
    if parsed is None or _parse_uuid(campaign_id) is None:
        return _redirect("/workbench", err="That Campaign Contact does not exist.")
    form = await request.form()
    reason = str(form.get("reason", "")).strip()
    commands = _commands(db)
    try:
        if command == "pause":
            outcome = commands.pause_contact(parsed, reason=reason or "paused by operator")
        elif command == "resume":
            outcome = commands.resume_contact(parsed)
        elif command == "retry":
            outcome = commands.retry_contact(parsed, reason=reason or "operator requested retry")
        elif command == "skip-stage":
            if not reason:
                return _redirect(back, err="A reason is required to skip a stage. Nothing changed.")
            outcome = commands.skip_stage(parsed, reason=reason)
        else:
            return _redirect(back, err="That command is not available.")
    except workbench_agents.WorkbenchCommandError as exc:
        db.rollback()
        return _redirect("/workbench", err=str(exc))
    db.commit()
    return _command_redirect(back, outcome)


# --- Local-only tools --------------------------------------------------------


def _local_tools_available() -> bool:
    return get_settings().app_env.lower() == "local"


@router.get("/local-tools", response_class=HTMLResponse)
def local_tools_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if not _local_tools_available():
        return _not_found(request, db, "Local tools are only available in local development.")
    return _render(
        request,
        db,
        "local_tools.html",
        {"active_nav": "local-tools", "page_title": "Local Tools"},
    )


@router.post("/local-tools/load-csv")
def local_tools_load_csv(db: Session = Depends(get_db)) -> Response:
    if not _local_tools_available():
        return _redirect("/", err="Local tools are only available in local development.")
    try:
        result = devtools.load_csv_fixture(db)
    except (devtools.LocalOnlyViolation, FeatureDisabledError) as exc:
        return _redirect("/local-tools", err=str(exc))
    s = result.summary
    return _redirect(
        f"/imports/{s.batch_id}",
        ok=(
            f"CSV fixture loaded into “{result.campaign_name}”: {s.accepted_rows} accepted, "
            f"{s.rejected_rows} rejected, {s.duplicate_rows} duplicate, "
            f"{s.suppressed_rows} suppressed."
        ),
    )


@router.post("/local-tools/load-xlsx")
def local_tools_load_xlsx(db: Session = Depends(get_db)) -> Response:
    if not _local_tools_available():
        return _redirect("/", err="Local tools are only available in local development.")
    try:
        result = devtools.load_xlsx_fixture(db)
    except (devtools.LocalOnlyViolation, FeatureDisabledError) as exc:
        return _redirect("/local-tools", err=str(exc))
    s = result.summary
    return _redirect(
        f"/imports/{s.batch_id}",
        ok=(
            f"XLSX fixture loaded into “{result.campaign_name}”: {s.accepted_rows} accepted "
            f"across the selected sheets."
        ),
    )


async def _confirmed(request: Request) -> bool:
    form = await request.form()
    return str(form.get("confirm", "")).strip().upper() == "RESET"


@router.post("/local-tools/clear")
async def local_tools_clear(request: Request, db: Session = Depends(get_db)) -> Response:
    if not _local_tools_available():
        return _redirect("/", err="Local tools are only available in local development.")
    if not await _confirmed(request):
        return _redirect("/local-tools", err="Type RESET in the confirmation box to clear data.")
    try:
        tables = devtools.clear_local_data(db)
    except devtools.LocalOnlyViolation as exc:
        return _redirect("/local-tools", err=str(exc))
    return _redirect(
        "/local-tools", ok=f"Local data cleared ({len(tables)} tables). The reset was audited."
    )


@router.post("/local-tools/demo-reset")
async def local_tools_demo_reset(request: Request, db: Session = Depends(get_db)) -> Response:
    if not _local_tools_available():
        return _redirect("/", err="Local tools are only available in local development.")
    if not await _confirmed(request):
        return _redirect("/local-tools", err="Type RESET in the confirmation box to reset.")
    try:
        results = devtools.reset_to_demo_state(db)
    except (devtools.LocalOnlyViolation, FeatureDisabledError) as exc:
        return _redirect("/local-tools", err=str(exc))
    loaded = " and ".join(f"“{r.campaign_name}”" for r in results)
    return _redirect("/", ok=f"Demo state ready: cleared local data and loaded {loaded}.")


# --- LinkedIn profile snapshots (DAT-012D, read-only) ------------------------


@router.get("/profiles/{snapshot_id}", response_class=HTMLResponse)
def profile_snapshot_page(
    request: Request, snapshot_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Read-only view of one immutable LinkedIn profile capture snapshot.

    This is the record the capture extension links to after staging. It renders
    evidence only — there is no action here that changes a contact, suppression,
    verification, or approval.
    """

    parsed_id = _parse_uuid(snapshot_id)
    snapshot = db.get(LinkedInProfileSnapshot, parsed_id) if parsed_id else None
    if snapshot is None:
        return _not_found(request, db, "That profile snapshot does not exist.")
    fields = snapshot.profile_fields or {}
    profile_rows = [
        ("full_name", fields.get("full_name")),
        ("headline", fields.get("headline")),
        ("displayed_location", fields.get("displayed_location")),
        ("connection_count", fields.get("connection_count")),
        ("open_to_work", fields.get("open_to_work")),
        ("warnings", len(fields.get("warnings") or [])),
    ]
    return _render(
        request,
        db,
        "profile_snapshot.html",
        {"snapshot": snapshot, "profile_rows": profile_rows, "page_title": "Profile snapshot"},
    )


@router.get("/company-profiles/{snapshot_id}", response_class=HTMLResponse)
def company_snapshot_page(
    request: Request, snapshot_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Read-only view of one immutable LinkedIn company capture snapshot."""

    parsed_id = _parse_uuid(snapshot_id)
    snapshot = db.get(LinkedInCompanySnapshot, parsed_id) if parsed_id else None
    if snapshot is None:
        return _not_found(request, db, "That company snapshot does not exist.")
    fields = snapshot.company_fields or {}
    company_rows = [
        ("name", fields.get("name")),
        ("website", fields.get("website")),
        ("industry", fields.get("industry")),
        ("size_range", fields.get("size_range")),
        ("employee_count_raw", fields.get("employee_count_raw")),
        ("headquarters_text", fields.get("headquarters_text")),
        ("founded_raw", fields.get("founded_raw")),
        ("specialties", fields.get("specialties")),
        ("warnings", len(fields.get("warnings") or [])),
    ]
    return _render(
        request,
        db,
        "company_snapshot.html",
        {"snapshot": snapshot, "company_rows": company_rows, "page_title": "Company snapshot"},
    )


# --- Contact-first captures (DAT-013, read-only) ------------------------------


def _capture_profile_rows(fields: dict[str, Any]) -> list[tuple[str, Any]]:
    return [
        ("full_name", fields.get("full_name")),
        ("headline", fields.get("headline")),
        ("displayed_location", fields.get("displayed_location")),
        ("connection_count", fields.get("connection_count")),
        ("open_to_work", fields.get("open_to_work")),
        ("about_text", fields.get("about_text")),
        ("warnings", len(fields.get("warnings") or [])),
    ]


@router.get("/contact-captures/submissions/{submission_id}", response_class=HTMLResponse)
def contact_capture_submission_page(
    request: Request, submission_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Read-only view of one contact-capture submission and its per-person outcomes."""

    parsed_id = _parse_uuid(submission_id)
    submission = db.get(ContactCaptureSubmission, parsed_id) if parsed_id else None
    if submission is None:
        return _not_found(request, db, "That contact capture submission does not exist.")
    captures = list(
        db.scalars(
            select(LinkedInProfileSnapshot)
            .where(LinkedInProfileSnapshot.submission_id == submission.id)
            .order_by(LinkedInProfileSnapshot.ingested_at)
        )
    )
    counts = sorted(((submission.response_body or {}).get("counts") or {}).items())
    return _render(
        request,
        db,
        "contact_capture_submission.html",
        {
            "submission": submission,
            "captures": captures,
            "counts": counts,
            "page_title": "Contact capture submission",
        },
    )


# --- Capture promotion (DAT-014) ----------------------------------------------


def _promotion_enabled() -> bool:
    return get_settings().features.contact_capture_promotion


def _auto_resolution_enabled() -> bool:
    """Whether the workbench may run automatic company-domain resolution.

    Both switches, because automatic resolution is a way of settling a capture's
    promotion: with promotion off there is nothing for a decision to feed.

    """

    features = get_settings().features
    return features.contact_capture_promotion and features.automatic_company_domain_resolution


def _provider_access(db: Session) -> resolution_service.ProviderAccess:
    """How the provider may be reached, or an access with no key at all.

    A missing switch or missing key yields an unusable access rather than an
    error: the policy then decides from stored evidence and reports the provider
    truthfully as not run, instead of the page pretending it asked and heard
    nothing.

    ``db`` is a parameter because the switch is an administrator's durable
    setting rather than an environment variable, so reading it is a query.
    """

    settings = get_settings()
    usable = (
        operational.enabled(db, "salesnav_domain_enrichment", settings)
        and settings.has_logo_dev_key()
    )
    return resolution_service.ProviderAccess(
        api_key=settings.logo_dev_api_key if usable else None,
        search_url=settings.logo_dev_search_url,
        timeout=settings.logo_dev_timeout_seconds,
        max_candidates=settings.logo_dev_max_candidates,
    )


def _load_capture(db: Session, capture_id: str) -> LinkedInProfileSnapshot | None:
    parsed_id = _parse_uuid(capture_id)
    return db.get(LinkedInProfileSnapshot, parsed_id) if parsed_id else None


@router.get("/contact-captures/pending", response_class=HTMLResponse)
def contact_captures_pending_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Captures waiting for a company domain before they can become contacts."""

    if not _promotion_enabled():
        return _not_found(request, db, "Capture promotion is not enabled for this workbench.")
    captures = capture_promotion.pending_captures(db)
    rows = []
    for snapshot in captures:
        view = capture_promotion.build_view(db, snapshot)
        rows.append(view)
    db.commit()  # persist the promotion/enrichment records build_view ensured
    return _render(
        request,
        db,
        "contact_captures_pending.html",
        {
            "rows": rows,
            "lookup_available": _enrichment_enabled(db) and get_settings().has_logo_dev_key(),
            "page_title": "Captures awaiting promotion",
        },
    )


@router.post("/contact-captures/{capture_id}/company/lookup")
def capture_company_lookup(
    request: Request, capture_id: str, db: Session = Depends(get_db)
) -> Response:
    """Ask logo.dev for domain candidates for this capture's company."""

    if not _promotion_enabled():
        return _redirect("/", err="Capture promotion is not enabled.")
    snapshot = _load_capture(db, capture_id)
    if snapshot is None:
        return _redirect("/contact-captures/pending", err="That capture does not exist.")
    settings = get_settings()
    target = f"/contact-captures/{snapshot.id}"
    if not operational.enabled(db, "salesnav_domain_enrichment", settings):
        return _redirect(target, err="Company-domain enrichment is not enabled. No lookup ran.")
    if not settings.has_logo_dev_key():
        return _redirect(
            target,
            err="logo.dev API key is not configured (set LOGO_DEV_API_KEY). No lookup ran.",
        )
    try:
        _promotion, record = capture_promotion.run_lookup(
            db,
            snapshot=snapshot,
            api_key=settings.logo_dev_api_key or "",
            search_url=settings.logo_dev_search_url,
            timeout=settings.logo_dev_timeout_seconds,
            max_candidates=settings.logo_dev_max_candidates,
            actor="workbench",
            force=True,
        )
    except enrichment.ApiKeyMissing:
        db.rollback()
        return _redirect(target, err="logo.dev API key is not configured. No lookup ran.")
    db.commit()
    if record is None:
        return _redirect(
            target, err="This capture showed no company name, so nothing was looked up."
        )
    return _redirect(
        target,
        ok=(
            f"Lookup finished: {record.lookup_status.value} · "
            f"{len(record.candidates or [])} candidate(s) awaiting your decision."
        ),
    )


@router.post("/contact-captures/{capture_id}/company/confirm")
async def capture_company_confirm(
    request: Request, capture_id: str, db: Session = Depends(get_db)
) -> Response:
    """Record the operator's explicit domain decision for this capture."""

    if not _promotion_enabled():
        return _redirect("/", err="Capture promotion is not enabled.")
    snapshot = _load_capture(db, capture_id)
    if snapshot is None:
        return _redirect("/contact-captures/pending", err="That capture does not exist.")
    target = f"/contact-captures/{snapshot.id}"
    form = await request.form()
    decision = str(form.get("decision", "")).strip()
    domain = str(form.get("domain", "")).strip() or None
    note = str(form.get("note", "")).strip() or None

    sources = {
        "candidate": EnrichmentConfirmationSource.CANDIDATE,
        "manual": EnrichmentConfirmationSource.MANUAL,
        "unresolved": EnrichmentConfirmationSource.UNRESOLVED,
    }
    source = sources.get(decision)
    if source is None:
        return _redirect(target, err="Choose a candidate, enter a domain, or leave it unresolved.")
    try:
        promotion = capture_promotion.confirm_domain(
            db, snapshot=snapshot, source=source, domain=domain, actor="workbench", note=note
        )
    except capture_promotion.PromotionError as exc:
        db.rollback()
        return _redirect(target, err=str(exc))
    resolved_domain = promotion.resolved_domain
    db.commit()
    if source is EnrichmentConfirmationSource.UNRESOLVED:
        return _redirect(target, ok="Recorded as deliberately unresolved. Nothing was promoted.")
    # Say which decision was actually recorded. A manual override and a
    # confirmed provider candidate share one outcome value, so naming the
    # outcome here would credit the provider for a domain the operator typed.
    how = "Entered" if source is EnrichmentConfirmationSource.MANUAL else "Confirmed the candidate"
    return _redirect(
        target,
        ok=f"{how} {resolved_domain or domain}. You can promote this capture now.",
    )


@router.post("/contact-captures/{capture_id}/company/reject")
async def capture_company_reject(
    request: Request, capture_id: str, db: Session = Depends(get_db)
) -> Response:
    """Reject one candidate, preserving it as a recorded decision."""

    if not _promotion_enabled():
        return _redirect("/", err="Capture promotion is not enabled.")
    snapshot = _load_capture(db, capture_id)
    if snapshot is None:
        return _redirect("/contact-captures/pending", err="That capture does not exist.")
    target = f"/contact-captures/{snapshot.id}"
    form = await request.form()
    domain = str(form.get("domain", "")).strip()
    reason = str(form.get("reason", "")).strip() or None
    try:
        capture_promotion.reject_candidate(
            db, snapshot=snapshot, domain=domain, actor="workbench", reason=reason
        )
    except capture_promotion.PromotionError as exc:
        db.rollback()
        return _redirect(target, err=str(exc))
    db.commit()
    return _redirect(target, ok=f"Rejected {domain}. The decision is kept with the candidates.")


@router.post("/contact-captures/{capture_id}/company/resolve")
def capture_company_resolve(
    request: Request, capture_id: str, db: Session = Depends(get_db)
) -> Response:
    """Run automatic company-domain resolution for this capture (DAT-017A)."""

    if not _promotion_enabled():
        return _redirect("/", err="Capture promotion is not enabled.")
    snapshot = _load_capture(db, capture_id)
    if snapshot is None:
        return _redirect("/contact-captures/pending", err="That capture does not exist.")
    target = f"/contact-captures/{snapshot.id}"
    if not _auto_resolution_enabled():
        return _redirect(target, err="Automatic company-domain resolution is not enabled.")
    try:
        # ``force`` because this button is an explicit operator request to
        # re-evaluate. Without it a capture that already has a decision would
        # silently do nothing, which would read as a broken button rather than
        # as the idempotence it actually is.
        outcome = resolution_service.resolve(
            db,
            snapshot=snapshot,
            access=_provider_access(db),
            actor="workbench",
            force=True,
        )
    except resolution_service.ResolutionError as exc:
        db.rollback()
        return _redirect(target, err=str(exc))
    except enrichment.ApiKeyMissing:
        db.rollback()
        return _redirect(target, err="logo.dev API key is not configured. No lookup ran.")
    db.commit()

    view = resolution_service.build_decision_view(outcome.decision)
    headline = view.headline if view else outcome.state.value
    if not outcome.created:
        return _redirect(
            target, ok=f"{headline}. Nothing changed — the evidence still says the same thing."
        )
    spent = "one provider lookup was used" if outcome.provider_call_made else "no provider call"
    if outcome.selected_domain:
        return _redirect(target, ok=f"{headline}: {outcome.selected_domain} · {spent}.")
    return _redirect(target, err=f"{headline}. No domain was selected · {spent}.")


@router.post("/contact-captures/{capture_id}/company/correct")
async def capture_company_correct(
    request: Request, capture_id: str, db: Session = Depends(get_db)
) -> Response:
    """Record an operator's correction of a resolution decision (DAT-017A)."""

    if not _promotion_enabled():
        return _redirect("/", err="Capture promotion is not enabled.")
    snapshot = _load_capture(db, capture_id)
    if snapshot is None:
        return _redirect("/contact-captures/pending", err="That capture does not exist.")
    target = f"/contact-captures/{snapshot.id}"
    form = await request.form()
    note = str(form.get("note", "")).strip() or None
    raw_domain = str(form.get("domain", "")).strip()
    to_unresolved = str(form.get("decision", "")).strip() == "unresolved"

    if not to_unresolved and not raw_domain:
        return _redirect(target, err="Enter the correct domain, or correct it to unresolved.")
    try:
        outcome = resolution_service.correct(
            db,
            snapshot=snapshot,
            domain=None if to_unresolved else raw_domain,
            actor="workbench",
            note=note,
        )
    except resolution_service.ResolutionError as exc:
        db.rollback()
        return _redirect(target, err=str(exc))
    db.commit()
    if outcome.selected_domain:
        return _redirect(
            target,
            ok=(
                f"Corrected to {outcome.selected_domain}. The earlier decision is kept as "
                f"decision #{outcome.decision.decision_number - 1}."
            ),
        )
    return _redirect(
        target,
        ok="Corrected to unresolved. The earlier decision and its candidates are kept.",
    )


@router.post("/contact-captures/{capture_id}/promote")
def capture_promote(request: Request, capture_id: str, db: Session = Depends(get_db)) -> Response:
    """Promote a resolved capture into a canonical contact."""

    if not _promotion_enabled():
        return _redirect("/", err="Capture promotion is not enabled.")
    snapshot = _load_capture(db, capture_id)
    if snapshot is None:
        return _redirect("/contact-captures/pending", err="That capture does not exist.")
    target = f"/contact-captures/{snapshot.id}"
    try:
        result = capture_promotion.promote(db, snapshot=snapshot, actor="workbench")
    except capture_promotion.PromotionError as exc:
        db.rollback()
        return _redirect(target, err=str(exc))
    company_phrase = capture_promotion.company_outcome_phrase(
        result.company_outcome, record=capture_promotion.get_enrichment(db, snapshot.id)
    )
    db.commit()
    if result.promoted:
        return _redirect(
            target,
            ok=(f"{result.contact_outcome.value.replace('_', ' ')} · company {company_phrase}."),
        )
    return _redirect(target, err=result.blocked_reason or "This capture could not be promoted.")


@router.get("/contact-captures/{capture_id}", response_class=HTMLResponse)
def contact_capture_page(
    request: Request, capture_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """One permanent contact capture, plus its company-resolution state.

    The capture evidence itself is read-only and always will be. The promotion
    controls shown alongside it act on the separate promotion record, never on
    the captured payload.
    """

    snapshot = _load_capture(db, capture_id)
    if snapshot is None:
        return _not_found(request, db, "That contact capture does not exist.")
    notes = list(
        db.scalars(
            select(ContactCaptureNote)
            .where(ContactCaptureNote.capture_id == snapshot.id)
            .order_by(ContactCaptureNote.created_at)
        )
    )
    view = None
    if _promotion_enabled():
        view = capture_promotion.build_view(db, snapshot)
        db.commit()
    settings = get_settings()
    return _render(
        request,
        db,
        "contact_capture.html",
        {
            "snapshot": snapshot,
            "notes": notes,
            "profile_rows": _capture_profile_rows(snapshot.profile_fields or {}),
            "resolution": view,
            "lookup_available": (
                operational.enabled(db, "salesnav_domain_enrichment", settings)
                and settings.has_logo_dev_key()
            ),
            # So "not_started · 0 attempt(s)" can say *why* nothing was attempted.
            # A status with no explanation reads as a broken pipeline when the truth
            # is usually one unset switch.
            "readiness": resolution_pending.lookup_readiness(db, settings),
            # Decisions are shown whenever they exist, even with the switch since
            # turned off: a decision that produced a live company link must stay
            # explainable regardless of the current configuration.
            "auto_available": _auto_resolution_enabled(),
            "decision": resolution_service.capture_view(db, snapshot.id),
            "history": resolution_service.history_view(db, snapshot.id),
            "page_title": "Contact capture",
        },
    )


# --- Retired customer twins ----------------------------------------------------
#
# The operator product owns these records now (People, Companies, Campaigns,
# Library). Old links and bookmarks resolve into it; nothing renders here.


@router.get("/contacts")
def _legacy_contacts(request: Request) -> RedirectResponse:
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(f"/app/people{query}", status_code=308)


@router.get("/contacts/{contact_id}")
def _legacy_contact(contact_id: str) -> RedirectResponse:
    return RedirectResponse(f"/app/people/{contact_id}", status_code=308)


@router.get("/companies")
def _legacy_companies(request: Request) -> RedirectResponse:
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(f"/app/companies{query}", status_code=308)


@router.get("/companies/{company_id}")
def _legacy_company(company_id: str) -> RedirectResponse:
    return RedirectResponse(f"/app/companies/{company_id}", status_code=308)


@router.get("/campaigns")
def _legacy_campaigns() -> RedirectResponse:
    return RedirectResponse("/app/campaigns", status_code=308)


@router.get("/campaigns/{campaign_id}")
def _legacy_campaign(campaign_id: str) -> RedirectResponse:
    return RedirectResponse(f"/app/campaigns/{campaign_id}", status_code=308)


@router.get("/knowledge-base")
@router.get("/knowledge-base/{section}")
def _legacy_knowledge_base(section: str = "") -> RedirectResponse:
    suffix = f"/{section}" if section else ""
    return RedirectResponse(f"/app/library{suffix}", status_code=308)


# --- Admin Agent Studio ------------------------------------------------------
#
# Agent Studio is mounted only inside this already local-only Admin router.  It
# reuses the Phase 2 registry, control reader and job queue; no execution switch
# or Campaign override is implemented here.


def _studio_campaign_id(request: Request) -> uuid.UUID | None:
    return _parse_uuid(request.query_params.get("campaign"))


def _campaign_contact_options(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        select(CampaignContact, Campaign, Contact)
        .join(Campaign, Campaign.id == CampaignContact.campaign_id)
        .join(Contact, Contact.id == CampaignContact.contact_id)
        .order_by(Campaign.name, Contact.last_name, Contact.first_name)
        .limit(250)
    ).all()
    return [
        {"membership": membership, "campaign": campaign, "contact": contact}
        for membership, campaign, contact in rows
    ]


@router.get("/admin/agents/studio", response_class=HTMLResponse)
def agent_studio_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if not _agent_workbench_available(db):
        return _agent_workbench_unavailable(request, db)
    return _render(
        request,
        db,
        "agent_studio.html",
        {
            "studio": agent_studio_service.load_studio(
                db, campaign_id=_studio_campaign_id(request)
            ),
            "active_nav": "agent-studio",
            "page_title": "Agent Studio",
        },
    )


def _temperament_values(
    config: personalization_policy.PolicyConfig | None,
) -> dict[str, int]:
    if config is None:
        return {}
    fields = (
        "company_context_usage",
        "question_first_preference",
        "commercial_directness",
        "personalization_depth",
        "evidence_confidence_tolerance",
        "role_led_emphasis",
        "seller_introduction_timing",
        "assertive_tone",
    )
    return {field: int(getattr(config.temperament, field)) for field in fields}


def _policy_comparison(
    left: personalization_policy.PolicyConfig,
    right: personalization_policy.PolicyConfig,
) -> dict[str, Any]:
    left_standards = {item.identifier: item for item in left.standards}
    right_standards = {item.identifier: item for item in right.standards}
    standard_changes = {
        identifier: {
            "from": {
                "strength": right_standards[identifier].strength.value,
                "state": right_standards[identifier].state.value,
                "wording": right_standards[identifier].wording,
            },
            "to": {
                "strength": left_standards[identifier].strength.value,
                "state": left_standards[identifier].state.value,
                "wording": left_standards[identifier].wording,
            },
        }
        for identifier in left_standards.keys() & right_standards.keys()
        if left_standards[identifier] != right_standards[identifier]
    }
    left_temperament = _temperament_values(left)
    right_temperament = _temperament_values(right)
    temperament_changes = {
        key: {"from": right_temperament[key], "to": left_temperament[key]}
        for key in left_temperament
        if left_temperament[key] != right_temperament[key]
    }
    left_strategies = {item.identifier: item.enabled for item in left.strategies}
    right_strategies = {item.identifier: item.enabled for item in right.strategies}
    strategy_changes = {
        key: {"from": right_strategies.get(key), "to": left_strategies.get(key)}
        for key in left_strategies.keys() | right_strategies.keys()
        if left_strategies.get(key) != right_strategies.get(key)
    }
    return {
        "standards": standard_changes,
        "temperament": temperament_changes,
        "strategies": strategy_changes,
        "examples": {"from": len(right.examples), "to": len(left.examples)},
        "maximum_evidence_age_days": {
            "from": right.evidence.maximum_age_days,
            "to": left.evidence.maximum_age_days,
        },
    }


def _personalization_context(
    request: Request,
    db: Session,
    *,
    preview: personalization_generation.GeneratedPersonalization | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    versions = personalization_policy.list_policy_versions(db)
    active = personalization_policy.active_policy(db)
    requested_id = _parse_uuid(request.query_params.get("version"))
    selected = db.get(PersonalizationPolicyVersion, requested_id) if requested_id else active
    if selected is None and versions:
        selected = versions[0]
    selected_config = (
        personalization_policy.PolicyConfig.from_dict(dict(selected.configuration))
        if selected
        else None
    )
    comparison = None
    compare_id = _parse_uuid(request.query_params.get("compare"))
    compared = db.get(PersonalizationPolicyVersion, compare_id) if compare_id else None
    if selected_config and compared:
        comparison = _policy_comparison(
            selected_config,
            personalization_policy.PolicyConfig.from_dict(dict(compared.configuration)),
        )
    return {
        "versions": versions,
        "activation_history": personalization_policy.activation_history(db),
        "active_policy": active,
        "selected_policy": selected,
        "selected_config": selected_config,
        "temperament_values": _temperament_values(selected_config),
        "campaign_contacts": _campaign_contact_options(db),
        "preview": preview,
        "comparison": comparison,
        "flash_err": error or request.query_params.get("err"),
        "active_nav": "agent-studio",
        "page_title": "Personalization Policy Studio",
    }


@router.get("/admin/agents/studio/personalization", response_class=HTMLResponse)
def personalization_policy_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if not _agent_workbench_available(db):
        return _agent_workbench_unavailable(request, db)
    return _render(
        request,
        db,
        "personalization_policy_studio.html",
        _personalization_context(request, db),
    )


def _parse_examples(raw: str) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split("|", 2)]
        if len(parts) < 2:
            raise personalization_policy.PolicyError(
                "Every example line must use: category | text | optional note."
            )
        examples.append(
            {
                "category": parts[0],
                "content": parts[1],
                "note": parts[2] if len(parts) == 3 and parts[2] else None,
            }
        )
    return examples


@router.post("/admin/agents/studio/personalization/policies")
async def personalization_policy_create(
    request: Request, db: Session = Depends(get_db)
) -> Response:
    if not _agent_workbench_available(db):
        return _redirect("/admin", err="Agent Studio is disabled.")
    form = await request.form()
    base_id = _parse_uuid(str(form.get("based_on_version_id", "")))
    base = db.get(PersonalizationPolicyVersion, base_id) if base_id else None
    if base is None:
        return _redirect(
            "/admin/agents/studio/personalization", err="Choose an existing base version."
        )
    raw = deepcopy(dict(base.configuration))
    wording_revision = str(form.get("edit_mode", "")) == "wording"
    for standard in raw.get("standards", []):
        if not isinstance(standard, dict) or not isinstance(standard.get("id"), str):
            continue
        identifier = standard["id"]
        if wording_revision:
            standard["description"] = str(
                form.get(f"standard_{identifier}_description", standard.get("description", ""))
            )
            standard["wording"] = str(
                form.get(f"standard_{identifier}_wording", standard.get("wording", ""))
            )
            continue
        standard["strength"] = str(
            form.get(f"standard_{identifier}_strength", standard.get("strength", "required"))
        )
        standard["state"] = (
            "enabled"
            if identifier in personalization_policy.CORE_STANDARD_IDS
            or f"standard_{identifier}_enabled" in form
            else "unavailable"
        )
    if not wording_revision:
        for strategy in raw.get("strategies", []):
            if isinstance(strategy, dict) and isinstance(strategy.get("id"), str):
                strategy["enabled"] = f"strategy_{strategy['id']}_enabled" in form
        temperament = raw.get("temperament")
        if isinstance(temperament, dict):
            for field in tuple(temperament):
                try:
                    temperament[field] = int(
                        str(form.get(f"temperament_{field}", temperament[field]))
                    )
                except ValueError:
                    temperament[field] = -1
        try:
            age = int(str(form.get("maximum_age_days", "365")))
        except ValueError:
            age = -1
        raw["evidence"] = {"maximum_age_days": age}
    try:
        if not wording_revision:
            raw["examples"] = _parse_examples(str(form.get("examples", "")))
        config = personalization_policy.PolicyConfig.from_dict(raw)
        version = personalization_policy.create_policy_version(
            db,
            configuration=config,
            name=str(form.get("name", "")),
            actor=OPERATOR_ACTOR,
            based_on_version_id=base.id,
            change_note=str(form.get("change_note", "")),
        )
    except personalization_policy.PolicyError as exc:
        db.rollback()
        return _redirect("/admin/agents/studio/personalization", err=str(exc))
    db.commit()
    return _redirect(
        f"/admin/agents/studio/personalization?version={version.id}",
        ok=f"Policy v{version.version_number} saved as an inactive immutable version.",
    )


@router.post("/admin/agents/studio/personalization/policies/{policy_version_id}/activate")
async def personalization_policy_activate(
    request: Request, policy_version_id: str, db: Session = Depends(get_db)
) -> Response:
    if not _agent_workbench_available(db):
        return _redirect("/admin", err="Agent Studio is disabled.")
    parsed = _parse_uuid(policy_version_id)
    if parsed is None:
        return _redirect("/admin/agents/studio/personalization", err="Invalid policy version.")
    form = await request.form()
    try:
        activation = personalization_policy.activate_policy(
            db,
            policy_version_id=parsed,
            actor=OPERATOR_ACTOR,
            reason=str(form.get("reason", "")) or None,
        )
    except personalization_policy.PolicyError as exc:
        db.rollback()
        return _redirect("/admin/agents/studio/personalization", err=str(exc))
    db.commit()
    version = db.get(PersonalizationPolicyVersion, activation.policy_version_id)
    return _redirect(
        f"/admin/agents/studio/personalization?version={parsed}",
        ok=f"Policy v{version.version_number if version else '?'} is active.",
    )


def _personalization_thinker() -> ClaudeCliThinker:
    return ClaudeCliThinker(settings=get_settings())


@router.post("/admin/agents/studio/personalization/preview", response_class=HTMLResponse)
async def personalization_preview(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if not _agent_workbench_available(db):
        return _agent_workbench_unavailable(request, db)
    form = await request.form()
    membership_id = _parse_uuid(str(form.get("campaign_contact_id", "")))
    policy_id = _parse_uuid(str(form.get("policy_version_id", "")))
    membership = db.get(CampaignContact, membership_id) if membership_id else None
    policy = db.get(PersonalizationPolicyVersion, policy_id) if policy_id else None
    generated = None
    error = None
    if membership is None or policy is None:
        error = "Choose a persisted Campaign Contact and policy version."
    else:
        try:
            generated = personalization_generation.generate(
                db,
                membership=membership,
                policy=policy,
                thinker=_personalization_thinker(),
            )
        except (personalization_generation.PreviewError, ThinkingError) as exc:
            error = str(exc)
    # Intentionally no commit. ``generate`` is read-only and no route action
    # creates a job, pipeline event, DraftVersion, approval or send.
    return _render(
        request,
        db,
        "personalization_policy_studio.html",
        _personalization_context(request, db, preview=generated, error=error),
    )


def _research_report_reader(db: Session) -> ResearchReportReader:
    return DurableResearchReportReader(db)


def _capture_report_reader(db: Session) -> DurableCaptureReportReader:
    return DurableCaptureReportReader(db)


def _company_report_reader(db: Session) -> DurableCompanyReportReader:
    return DurableCompanyReportReader(db)


def _insights_report_reader(db: Session) -> DurableInsightsReportReader:
    return DurableInsightsReportReader(db)


def _email_verification_context(
    request: Request,
    db: Session,
    *,
    agent_id: AgentIdentifier,
    test_run: ProviderTestRun | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    jobs = list(
        db.scalars(
            select(AgentJob)
            .where(AgentJob.agent_id == agent_id)
            .order_by(AgentJob.created_at.desc())
            .limit(100)
        ).all()
    )
    selected_id = _parse_uuid(request.query_params.get("job"))
    report = EmailVerificationReportReader(db).read(selected_id, agent_id) if selected_id else None
    context: dict[str, Any] = {
        "agent_id": agent_id,
        "jobs": jobs,
        "report": report,
        "test_run": test_run,
        "flash_err": error or request.query_params.get("err"),
        "active_nav": "agent-studio",
        "page_title": f"{agent_id.value.title()} Agent Studio",
    }
    if agent_id is AgentIdentifier.VERIFICATION:
        context.update(
            {
                "provider_cards": verification_studio.provider_cards(db),
                "provider_usage": verification_studio.provider_usage_summaries(db, get_settings()),
                "waterfall": verification_studio.active_waterfall(db),
                "waterfall_versions": list(
                    db.scalars(
                        select(VerificationWaterfallPolicyVersion).order_by(
                            VerificationWaterfallPolicyVersion.version_number.desc()
                        )
                    ).all()
                ),
                "usage_origins": verification_studio.usage_by_origin(db),
                "providers": PROVIDERS,
            }
        )
    else:
        context.update(
            {
                "pattern_policy": verification_studio.active_pattern_policy(db),
                "pattern_versions": list(
                    db.scalars(
                        select(EmailPatternPolicyVersion).order_by(
                            EmailPatternPolicyVersion.version_number.desc()
                        )
                    ).all()
                ),
                "allowed_patterns": verification_studio.ALLOWED_PATTERNS,
            }
        )
    return context


@router.get("/admin/agents/studio/email", response_class=HTMLResponse)
def email_agent_studio_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if not _agent_workbench_available(db):
        return _agent_workbench_unavailable(request, db)
    return _render(
        request,
        db,
        "email_agent_studio.html",
        _email_verification_context(request, db, agent_id=AgentIdentifier.EMAIL),
    )


@router.get("/admin/agents/studio/verification", response_class=HTMLResponse)
def verification_agent_studio_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if not _agent_workbench_available(db):
        return _agent_workbench_unavailable(request, db)
    return _render(
        request,
        db,
        "verification_agent_studio.html",
        _email_verification_context(request, db, agent_id=AgentIdentifier.VERIFICATION),
    )


@router.post("/admin/agents/studio/verification/credentials/{provider_id}")
async def verification_credential_rotate(
    request: Request, provider_id: str, db: Session = Depends(get_db)
) -> Response:
    if not _agent_workbench_available(db) or provider_id not in PROVIDERS:
        return _redirect("/admin/agents/studio/verification", err="Provider unavailable.")
    form = await request.form()
    try:
        verification_studio.rotate_credential(
            db,
            provider_id=provider_id,
            secret=str(form.get("secret", "")),
            label=str(form.get("label", "")),
            actor=OPERATOR_ACTOR,
            reason=str(form.get("reason", "")) or None,
            settings=get_settings(),
        )
        db.commit()
    except verification_studio.StudioConfigurationError as exc:
        db.rollback()
        return _redirect("/admin/agents/studio/verification", err=str(exc))
    return _redirect(
        "/admin/agents/studio/verification",
        ok=(
            f"{PROVIDERS[provider_id].display_name} credential rotated; "
            "its value will not be shown."
        ),
    )


@router.post("/admin/agents/studio/verification/test", response_class=HTMLResponse)
async def verification_provider_test(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    if not _agent_workbench_available(db):
        return _agent_workbench_unavailable(request, db)
    form = await request.form()
    run = None
    error = None
    try:
        run = verification_studio.provider_test(
            db,
            provider_id=str(form.get("provider_id", "")),
            email=str(form.get("email", "")),
            live=str(form.get("mode", "simulated")) == "live",
            actor=OPERATOR_ACTOR,
            settings=get_settings(),
        )
        db.commit()
    except verification_studio.StudioConfigurationError as exc:
        db.rollback()
        error = str(exc)
    return _render(
        request,
        db,
        "verification_agent_studio.html",
        _email_verification_context(
            request, db, agent_id=AgentIdentifier.VERIFICATION, test_run=run, error=error
        ),
    )


@router.post("/admin/agents/studio/verification/waterfalls")
async def verification_waterfall_create(
    request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    order = [
        item.strip() for item in str(form.get("provider_order", "")).split(",") if item.strip()
    ]
    try:
        row = verification_studio.create_waterfall_version(
            db,
            configuration={"providers": [{"id": item, "enabled": True} for item in order]},
            name=str(form.get("name", "")),
            actor=OPERATOR_ACTOR,
            based_on_version_id=_parse_uuid(str(form.get("based_on_version_id", ""))),
            change_note=str(form.get("change_note", "")) or None,
        )
        db.commit()
    except verification_studio.StudioConfigurationError as exc:
        db.rollback()
        return _redirect("/admin/agents/studio/verification", err=str(exc))
    return _redirect(
        "/admin/agents/studio/verification", ok=f"Waterfall v{row.version_number} saved inactive."
    )


@router.post("/admin/agents/studio/verification/waterfalls/{policy_id}/activate")
async def verification_waterfall_activate(
    request: Request, policy_id: str, db: Session = Depends(get_db)
) -> Response:
    parsed = _parse_uuid(policy_id)
    if parsed is None:
        return _redirect("/admin/agents/studio/verification", err="Invalid policy version.")
    form = await request.form()
    try:
        verification_studio.activate_waterfall(
            db,
            policy_version_id=parsed,
            actor=OPERATOR_ACTOR,
            reason=str(form.get("reason", "")) or None,
        )
        db.commit()
    except verification_studio.StudioConfigurationError as exc:
        db.rollback()
        return _redirect("/admin/agents/studio/verification", err=str(exc))
    return _redirect("/admin/agents/studio/verification", ok="Waterfall policy activated.")


@router.post("/admin/agents/studio/email/pattern-policies")
async def email_pattern_policy_create(request: Request, db: Session = Depends(get_db)) -> Response:
    form = await request.form()
    order = [item.strip() for item in str(form.get("pattern_order", "")).split(",") if item.strip()]
    try:
        maximum = int(str(form.get("max_candidates", "8")))
    except ValueError:
        maximum = -1
    try:
        row = verification_studio.create_pattern_version(
            db,
            configuration={
                "patterns": [{"id": item, "enabled": True} for item in order],
                "learned_formats_first": "learned_formats_first" in form,
                "max_candidates": maximum,
            },
            name=str(form.get("name", "")),
            actor=OPERATOR_ACTOR,
            based_on_version_id=_parse_uuid(str(form.get("based_on_version_id", ""))),
            change_note=str(form.get("change_note", "")) or None,
        )
        db.commit()
    except verification_studio.StudioConfigurationError as exc:
        db.rollback()
        return _redirect("/admin/agents/studio/email", err=str(exc))
    return _redirect(
        "/admin/agents/studio/email",
        ok=f"Email pattern policy v{row.version_number} saved inactive.",
    )


@router.post("/admin/agents/studio/email/pattern-policies/{policy_id}/activate")
async def email_pattern_policy_activate(
    request: Request, policy_id: str, db: Session = Depends(get_db)
) -> Response:
    parsed = _parse_uuid(policy_id)
    if parsed is None:
        return _redirect("/admin/agents/studio/email", err="Invalid policy version.")
    form = await request.form()
    try:
        verification_studio.activate_pattern_policy(
            db,
            policy_version_id=parsed,
            actor=OPERATOR_ACTOR,
            reason=str(form.get("reason", "")) or None,
        )
        db.commit()
    except verification_studio.StudioConfigurationError as exc:
        db.rollback()
        return _redirect("/admin/agents/studio/email", err=str(exc))
    return _redirect("/admin/agents/studio/email", ok="Email pattern policy activated.")


def _agent_execution_report_response(
    job_id: str, expected: AgentIdentifier, db: Session
) -> JSONResponse:
    if not _agent_workbench_available(db):
        return JSONResponse(status_code=404, content={"detail": "Not found."})
    parsed = _parse_uuid(job_id)
    report = EmailVerificationReportReader(db).read(parsed, expected) if parsed else None
    if report is None:
        return JSONResponse(status_code=404, content={"detail": "Not found."})
    return JSONResponse(content=jsonable_encoder(report))


@router.get("/api/admin/agent-studio/email/jobs/{job_id}/report")
def email_agent_report_api(job_id: str, db: Session = Depends(get_db)) -> JSONResponse:
    return _agent_execution_report_response(job_id, AgentIdentifier.EMAIL, db)


@router.get("/api/admin/agent-studio/verification/jobs/{job_id}/report")
def verification_agent_report_api(job_id: str, db: Session = Depends(get_db)) -> JSONResponse:
    return _agent_execution_report_response(job_id, AgentIdentifier.VERIFICATION, db)


@router.get("/admin/agents/studio/research", response_class=HTMLResponse)
def research_agent_report_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if not _agent_workbench_available(db):
        return _agent_workbench_unavailable(request, db)
    selected = _parse_uuid(request.query_params.get("campaign_contact"))
    report = _research_report_reader(db).read(selected) if selected else None
    if selected and report is None:
        return _not_found(request, db, "That Campaign Contact does not exist.")
    return _render(
        request,
        db,
        "research_agent_report.html",
        {
            "report": report,
            "campaign_contacts": _campaign_contact_options(db),
            "active_nav": "agent-studio",
            "page_title": "Company Research report",
        },
    )


@router.get("/api/admin/agent-studio/research/jobs/{agent_job_id}/report")
def research_agent_report_api(agent_job_id: str, db: Session = Depends(get_db)) -> JSONResponse:
    """Return the exact typed report used by the Admin HTML surface.

    This router is mounted only with the local-only operator Workbench.  A
    missing, malformed, non-Research or cross-owner job receives the same
    generic response so the endpoint never leaks another Agent's existence.
    """

    if not _agent_workbench_available(db):
        return JSONResponse(status_code=404, content={"detail": "Not found."})
    parsed = _parse_uuid(agent_job_id)
    report = _research_report_reader(db).read_job(parsed) if parsed else None
    if report is None:
        return JSONResponse(status_code=404, content={"detail": "Not found."})
    return JSONResponse(content=jsonable_encoder(report))


@router.get("/admin/agents/studio/capture", response_class=HTMLResponse)
def capture_agent_report_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if not _agent_workbench_available(db):
        return _agent_workbench_unavailable(request, db)
    raw_selected = request.query_params.get("job")
    selected = _parse_uuid(raw_selected)
    if raw_selected and selected is None:
        return _not_found(request, db, "That Capture report is unavailable.")
    reader = _capture_report_reader(db)
    report = reader.read_job(selected) if selected else None
    if selected and report is None:
        return _not_found(request, db, "That Capture report is unavailable.")
    studio = agent_studio_service.load_studio(db, campaign_id=_studio_campaign_id(request))
    item = next(entry for entry in studio.agents if entry.card.agent_id is AgentIdentifier.CAPTURE)
    recent_jobs = db.scalars(
        select(AgentJob)
        .where(AgentJob.agent_id == AgentIdentifier.CAPTURE)
        .order_by(AgentJob.created_at.desc(), AgentJob.id.desc())
        .limit(100)
    ).all()
    reports = {job.id: reader.read_job(job.id) for job in recent_jobs[:25]}
    return _render(
        request,
        db,
        "capture_agent_studio.html",
        {
            "item": item,
            "jobs": recent_jobs,
            "reports": reports,
            "report": report,
            "active_nav": "agent-studio",
            "page_title": "Capture Agent Studio",
        },
    )


@router.get("/api/admin/agent-studio/capture/jobs/{agent_job_id}/report")
def capture_agent_report_api(agent_job_id: str, db: Session = Depends(get_db)) -> JSONResponse:
    """Return the shared exact-job Capture report, or one generic safe 404."""

    if not _agent_workbench_available(db):
        return JSONResponse(status_code=404, content={"detail": "Not found."})
    parsed = _parse_uuid(agent_job_id)
    report = _capture_report_reader(db).read_job(parsed) if parsed else None
    if report is None:
        return JSONResponse(status_code=404, content={"detail": "Not found."})
    return JSONResponse(content=jsonable_encoder(report))


@router.get("/admin/agents/studio/company", response_class=HTMLResponse)
def company_agent_report_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if not _agent_workbench_available(db):
        return _agent_workbench_unavailable(request, db)
    raw_selected = request.query_params.get("job")
    selected = _parse_uuid(raw_selected)
    if raw_selected and selected is None:
        return _not_found(request, db, "That Company report is unavailable.")
    report = _company_report_reader(db).read_job(selected) if selected else None
    if selected and report is None:
        return _not_found(request, db, "That Company report is unavailable.")
    studio = agent_studio_service.load_studio(db, campaign_id=_studio_campaign_id(request))
    item = next(entry for entry in studio.agents if entry.card.agent_id is AgentIdentifier.COMPANY)
    recent_jobs = db.scalars(
        select(AgentJob)
        .where(AgentJob.agent_id == AgentIdentifier.COMPANY)
        .order_by(AgentJob.created_at.desc(), AgentJob.id.desc())
        .limit(100)
    ).all()
    reports = {job.id: _company_report_reader(db).read_job(job.id) for job in recent_jobs[:25]}
    return _render(
        request,
        db,
        "company_agent_studio.html",
        {
            "item": item,
            "jobs": recent_jobs,
            "reports": reports,
            "report": report,
            "active_nav": "agent-studio",
            "page_title": "Company Agent Studio",
        },
    )


@router.get("/api/admin/agent-studio/company/jobs/{agent_job_id}/report")
def company_agent_report_api(agent_job_id: str, db: Session = Depends(get_db)) -> JSONResponse:
    """Return the shared exact-job Company report, or one generic safe 404."""

    if not _agent_workbench_available(db):
        return JSONResponse(status_code=404, content={"detail": "Not found."})
    parsed = _parse_uuid(agent_job_id)
    report = _company_report_reader(db).read_job(parsed) if parsed else None
    if report is None:
        return JSONResponse(status_code=404, content={"detail": "Not found."})
    return JSONResponse(content=jsonable_encoder(report))


@router.get("/admin/agents/studio/insights", response_class=HTMLResponse)
def insights_agent_report_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if not _agent_workbench_available(db):
        return _agent_workbench_unavailable(request, db)
    selected = _parse_uuid(request.query_params.get("job"))
    report = _insights_report_reader(db).read_job(selected) if selected else None
    if selected and report is None:
        return _not_found(request, db, "That Insights report is unavailable.")
    studio = agent_studio_service.load_studio(db, campaign_id=_studio_campaign_id(request))
    item = next(entry for entry in studio.agents if entry.card.agent_id is AgentIdentifier.INSIGHTS)
    jobs = db.scalars(
        select(AgentJob)
        .where(AgentJob.agent_id == AgentIdentifier.INSIGHTS)
        .order_by(AgentJob.created_at.desc(), AgentJob.id.desc())
        .limit(100)
    ).all()
    return _render(
        request,
        db,
        "insights_agent_studio.html",
        {
            "item": item,
            "jobs": jobs,
            "report": report,
            "active_nav": "agent-studio",
            "page_title": "Insights Agent Studio",
        },
    )


@router.get("/api/admin/agent-studio/insights/jobs/{agent_job_id}/report")
def insights_agent_report_api(agent_job_id: str, db: Session = Depends(get_db)) -> JSONResponse:
    """Return the same exact-job read model as the operator HTML surface."""

    if not _agent_workbench_available(db):
        return JSONResponse(status_code=404, content={"detail": "Not found."})
    parsed = _parse_uuid(agent_job_id)
    report = _insights_report_reader(db).read_job(parsed) if parsed else None
    if report is None:
        return JSONResponse(status_code=404, content={"detail": "Not found."})
    return JSONResponse(content=jsonable_encoder(report))


@router.get("/admin/agents/studio/{agent_id}", response_class=HTMLResponse)
def agent_studio_agent_page(
    request: Request, agent_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    if not _agent_workbench_available(db):
        return _agent_workbench_unavailable(request, db)
    parsed = _parse_agent_id(agent_id)
    if parsed is None:
        return _not_found(request, db, "That Agent is not registered.")
    studio = agent_studio_service.load_studio(db, campaign_id=_studio_campaign_id(request))
    item = next(entry for entry in studio.agents if entry.card.agent_id is parsed)
    return _render(
        request,
        db,
        "agent_studio_agent.html",
        {
            "item": item,
            "active_nav": "agent-studio",
            "page_title": item.card.display_name,
        },
    )

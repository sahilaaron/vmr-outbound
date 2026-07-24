"""Operator-workbench page routes (server-rendered, Jinja2).

These routes are deliberately thin adapters: every business rule lives in the
service layer (AGENTS.md — the dashboard "must not contain business rules").
The pages are gated behind the ``workbench`` feature switch (the router is only
mounted when the switch is on) and perform no outreach action of any kind.

Flash messages travel as ``ok``/``err`` query parameters on redirects, so the
pages stay stateless (no sessions, no cookies).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import get_settings
from app.models.enums import (
    CampaignStatus,
    ContactWorkflowState,
    EnrichmentConfirmationSource,
    IdentityResolutionType,
    ImportBatchStatus,
    ImportRowOutcome,
    ImportSourceFormat,
)
from app.services import devtools, identity, workbench
from app.services.campaigns import (
    CampaignError,
    campaign_imports,
    campaign_members,
    create_campaign,
    get_campaign_overview,
    list_campaigns,
)
from app.services.enrichment import companies as enrichment
from app.services.imports import mapping as mapping_service
from app.services.imports import parsing, staging, validation
from app.services.imports.importer import (
    BatchNotProcessable,
    BatchProvenance,
    CampaignNotFound,
    FeatureDisabledError,
    process_pending_batch,
    run_import,
)
from app.services.imports.preview import preview_import, preview_pending_batch

router = APIRouter(include_in_schema=False)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

PAGE_SIZE = 50
PREVIEW_ROWS_SHOWN = 50
SAMPLE_ROWS_SHOWN = 5
# Uploads are read in bounded chunks so an oversized file is rejected without
# ever being held fully in memory.
_UPLOAD_CHUNK_BYTES = 1024 * 1024

_UNAVAILABLE_SECTIONS: dict[str, str] = {
    "verification": "Email Verification",
    "scoring": "Scoring",
    "research": "Research",
    "drafts": "Drafts & Approval",
    "sequences": "Sequences",
    "activity": "Activity",
    "settings": "Settings",
}


def _fmt_dt(value: datetime | date | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return value.isoformat()


templates.env.filters["dt"] = _fmt_dt


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
        "features_enabled": settings.features.enabled(),
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


@router.get("/", response_class=HTMLResponse)
def overview_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    stats = workbench.load_overview(db)
    return _render(
        request,
        db,
        "overview.html",
        {"stats": stats, "active_nav": "overview", "page_title": "Overview"},
    )


# --- Campaigns ---------------------------------------------------------------


@router.get("/campaigns", response_class=HTMLResponse)
def campaigns_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return _render(
        request,
        db,
        "campaigns.html",
        {
            "campaigns": list_campaigns(db),
            "active_nav": "campaigns",
            "page_title": "Campaigns",
        },
    )


# Note: POST /campaigns belongs to the JSON API (app/api/routes.py); the HTML
# form posts to its own path so the two adapters cannot shadow each other.
@router.post("/campaigns/create")
async def campaigns_create(request: Request, db: Session = Depends(get_db)) -> Response:
    form = await request.form()
    name = str(form.get("name", "")).strip()
    description = str(form.get("description", "")).strip() or None
    status_raw = str(form.get("status", "draft"))
    try:
        status = CampaignStatus(status_raw)
    except ValueError:
        status = CampaignStatus.DRAFT
    try:
        campaign = create_campaign(db, name=name, description=description, status=status)
    except CampaignError as exc:
        return _redirect("/campaigns", err=str(exc))
    except Exception:
        db.rollback()
        return _redirect(
            "/campaigns",
            err=f"A campaign named “{name}” already exists. Campaign names must be unique.",
        )
    db.commit()
    return _redirect(f"/campaigns/{campaign.id}", ok=f"Campaign “{campaign.name}” created.")


@router.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
def campaign_detail_page(
    request: Request, campaign_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    parsed_id = _parse_uuid(campaign_id)
    overview = get_campaign_overview(db, parsed_id) if parsed_id else None
    if overview is None:
        return _not_found(request, db, "That campaign does not exist.")

    state_filter = request.query_params.get("state") or None
    state = None
    if state_filter:
        try:
            state = ContactWorkflowState(state_filter)
        except ValueError:
            state_filter = None
    page = _page_number(request)
    members, total_members = campaign_members(
        db,
        overview.campaign.id,
        state=state,
        limit=PAGE_SIZE,
        offset=(page - 1) * PAGE_SIZE,
    )
    return _render(
        request,
        db,
        "campaign_detail.html",
        {
            "overview": overview,
            "imports": campaign_imports(db, overview.campaign.id),
            "members": members,
            "total_members": total_members,
            "page": page,
            "pages": _pages(total_members),
            "state_filter": state_filter,
            "workflow_states": list(ContactWorkflowState),
            "active_nav": "campaigns",
            "page_title": overview.campaign.name,
        },
    )


# --- Imports: list + upload --------------------------------------------------


@router.get("/imports", response_class=HTMLResponse)
def imports_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    page = _page_number(request)
    batches, total = workbench.list_batches(db, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)

    staged_dir = get_settings().staged_uploads_dir
    staged_entries = staging.list_staged_uploads(staged_dir)
    campaign_names = {str(row.campaign.id): row.campaign.name for row in list_campaigns(db)}
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
            "campaigns": list_campaigns(db),
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
            "enrich_enabled": is_salesnav and _enrichment_enabled(),
            "sn_meta": batch.source_metadata if is_salesnav else None,
            "csv_import_enabled": get_settings().features.csv_import,
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
    if (
        batch.source_format == ImportSourceFormat.SALES_NAVIGATOR
        and get_settings().features.salesnav_domain_enrichment
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


def _enrichment_enabled() -> bool:
    return get_settings().features.salesnav_domain_enrichment


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
    if not _enrichment_enabled():
        return _not_found(
            request, db, "Company-domain enrichment is not enabled for this workbench."
        )
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

    if not _enrichment_enabled():
        return _redirect("/imports", err="Company-domain enrichment is not enabled.")
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

    if not _enrichment_enabled():
        return _redirect("/imports", err="Company-domain enrichment is not enabled.")
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

    if not _enrichment_enabled():
        return _redirect("/imports", err="Company-domain enrichment is not enabled.")
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
            "csv_import_enabled": get_settings().features.csv_import,
            "enrich_enabled": (
                batch.source_format == ImportSourceFormat.SALES_NAVIGATOR and _enrichment_enabled()
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


# --- Contacts ----------------------------------------------------------------


@router.get("/contacts", response_class=HTMLResponse)
def contacts_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    q = request.query_params.get("q") or None
    campaign_filter = request.query_params.get("campaign_id") or None
    state_filter = request.query_params.get("state") or None
    has_email_filter = request.query_params.get("has_email") or None

    campaign_uuid = _parse_uuid(campaign_filter)
    if campaign_filter and campaign_uuid is None:
        campaign_filter = None
    state = None
    if state_filter:
        try:
            state = ContactWorkflowState(state_filter)
        except ValueError:
            state_filter = None
    has_email = {"yes": True, "no": False}.get(has_email_filter or "")

    page = _page_number(request)
    contacts, total = workbench.list_contacts(
        db,
        search=q,
        campaign_id=campaign_uuid,
        state=state,
        has_email=has_email,
        limit=PAGE_SIZE,
        offset=(page - 1) * PAGE_SIZE,
    )

    filter_params = {
        key: value
        for key, value in (
            ("q", q),
            ("campaign_id", campaign_filter),
            ("state", state_filter),
            ("has_email", has_email_filter),
        )
        if value
    }
    filter_url = "/contacts" + (f"?{urlencode(filter_params)}" if filter_params else "")

    return _render(
        request,
        db,
        "contacts.html",
        {
            "contacts": contacts,
            "total": total,
            "page": page,
            "pages": _pages(total),
            "q": q,
            "campaign_filter": campaign_filter,
            "state_filter": state_filter,
            "has_email_filter": has_email_filter,
            "filter_url": filter_url,
            "campaigns": list_campaigns(db),
            "workflow_states": list(ContactWorkflowState),
            "active_nav": "contacts",
            "page_title": "Contacts",
        },
    )


@router.get("/contacts/{contact_id}", response_class=HTMLResponse)
def contact_detail_page(
    request: Request, contact_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    parsed_id = _parse_uuid(contact_id)
    detail = workbench.get_contact_detail(db, parsed_id) if parsed_id else None
    if detail is None:
        return _not_found(request, db, "That contact does not exist.")
    return _render(
        request,
        db,
        "contact_detail.html",
        {
            "detail": detail,
            "active_nav": "contacts",
            "page_title": f"{detail.contact.first_name} {detail.contact.last_name}",
        },
    )


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


# --- Later-phase sections: one clean unavailable state -----------------------


def _make_unavailable_route(slug: str, title: str) -> None:
    @router.get(f"/{slug}", response_class=HTMLResponse, name=f"unavailable_{slug}")
    def unavailable_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
        return _render(
            request,
            db,
            "unavailable.html",
            {"section_title": title, "active_nav": slug, "page_title": title},
        )


for _slug, _title in _UNAVAILABLE_SECTIONS.items():
    _make_unavailable_route(_slug, _title)

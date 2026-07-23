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
from app.models.enums import CampaignStatus, ContactWorkflowState, ImportRowOutcome
from app.services import devtools, workbench
from app.services.campaigns import (
    CampaignError,
    campaign_imports,
    campaign_members,
    create_campaign,
    get_campaign_overview,
    list_campaigns,
)
from app.services.imports import mapping as mapping_service
from app.services.imports import parsing, staging, validation
from app.services.imports.importer import (
    BatchProvenance,
    CampaignNotFound,
    FeatureDisabledError,
    run_import,
)
from app.services.imports.preview import preview_import

router = APIRouter(include_in_schema=False)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

PAGE_SIZE = 50
PREVIEW_ROWS_SHOWN = 50
SAMPLE_ROWS_SHOWN = 5

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
    shared: dict[str, Any] = {
        "app_env": settings.app_env,
        "dry_run": settings.dry_run,
        "features_enabled": settings.features.enabled(),
        "local_env": settings.app_env.lower() == "local",
        "database_ok": _database_ok(db),
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
    content = await file.read()
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
            "active_nav": "imports",
            "page_title": batch.filename or "Import batch",
        },
    )


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

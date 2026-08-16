"""Add people from a file: upload -> preview -> confirm, bound to one Campaign.

The Campaign is in the URL of every step and re-checked at each of them, so a
staged upload can only ever be confirmed into the Campaign it was uploaded for.
The preview writes nothing; confirmation is the first durable mutation.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import get_settings
from app.models.enums import AgentIdentifier, CampaignStatus
from app.services import campaigns as campaign_service
from app.services import drafts as draft_service
from app.services.agents import readiness as agent_readiness
from app.services.imports import apollo, campaign_import, staging
from app.services.personalization.cadence import campaign_opted_in
from app.web.v2 import shell

router = shell.router

#: How many planned rows the import preview renders.
PREVIEW_ROWS_SHOWN = 50


def _sheet_index(value: str | None) -> int | None:
    """Read a worksheet selection from a form field, or None."""

    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def _campaign_or_none(db: Session, campaign_id: str) -> tuple[uuid.UUID, Any] | None:
    identifier = shell.uuid_or_none(campaign_id)
    if identifier is None:
        return None
    campaign = campaign_service.get_campaign(db, identifier)
    if campaign is None:
        return None
    return identifier, campaign


def _staging_dir() -> str:
    return get_settings().staged_uploads_dir


def _load_campaign_staged(campaign_id: uuid.UUID, staged_id: str) -> Any | None:
    """Load a staged upload only if it belongs to *campaign_id*.

    The ownership check is the point. Without it a staged upload id — which is
    guessable only in the sense that anything is, but is also copied into URLs
    and browser history — would let a file uploaded for one Campaign be confirmed
    into another, and the Campaign a contact was imported into is exactly the
    fact this whole flow exists to fix in place.
    """

    try:
        staged = staging.load_staged_upload(_staging_dir(), staged_id)
    except staging.StagedUploadNotFound:
        return None
    if staged.campaign_id != str(campaign_id):
        return None
    return staged


@router.get("/campaigns/{campaign_id}/imports")
def campaign_imports_page(
    campaign_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Upload a contact file into this Campaign, and see what has been uploaded."""

    found = _campaign_or_none(db, campaign_id)
    if found is None:
        return shell.not_found(request, db, "That campaign does not exist.")
    identifier, campaign = found
    settings = get_settings()
    return shell.render(
        request,
        db,
        "campaign_imports.html",
        {
            "active_nav": "campaigns",
            "page_title": f"Import contacts — {campaign.name}",
            "campaign": campaign,
            "import_on": shell.import_on(db, settings),
            "max_upload_mb": round(settings.max_upload_bytes / (1024 * 1024), 1),
            "max_rows": campaign_import.MAX_DATA_ROWS,
            "batches": campaign_import.campaign_batches(db, identifier),
            "archived": campaign.status is CampaignStatus.ARCHIVED,
        },
    )


@router.post("/campaigns/{campaign_id}/imports")
async def campaign_import_upload(
    campaign_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Stage an uploaded file. Nothing is imported and no Contact is created."""

    found = _campaign_or_none(db, campaign_id)
    if found is None:
        return shell.redirect("/app/campaigns", err="That campaign does not exist.")
    identifier, campaign = found
    base = f"/app/campaigns/{identifier}/imports"
    settings = get_settings()
    if not shell.import_on(db, settings):
        return shell.redirect(
            base,
            err="Contact file import is switched off. Set FEATURES__CSV_IMPORT=true and restart.",
        )
    if campaign.status is CampaignStatus.ARCHIVED:
        return shell.redirect(base, err="An archived campaign cannot receive contacts.")

    form = await request.form()
    upload = form.get("file")
    filename = getattr(upload, "filename", None)
    if upload is None or not filename:
        return shell.redirect(base, err="Choose a .csv or .xlsx file to upload.")
    # Declared size first, so an oversized upload is refused before its bytes are
    # buffered into this process. This is a best-effort improvement, not complete
    # streaming protection: Content-Length is client-supplied and absent from a
    # chunked request, so the authoritative ceiling for an untrusted client
    # remains the reverse proxy's own body limit. The check below still runs on
    # what actually arrived.
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit():
        try:
            staging.enforce_upload_size(
                int(declared), settings.max_upload_bytes, filename=str(filename)
            )
        except staging.UploadTooLargeError as exc:
            return shell.redirect(base, err=str(exc))

    content = await upload.read()  # type: ignore[union-attr]

    try:
        staging.enforce_upload_size(len(content), settings.max_upload_bytes, filename=str(filename))
    except staging.UploadTooLargeError as exc:
        return shell.redirect(base, err=str(exc))

    # Parsed once here so an unreadable or unrecognized file is refused before a
    # single byte is written to the staging area.
    try:
        inspection = campaign_import.inspect(content, str(filename))
    except campaign_import.CampaignImportError as exc:
        return shell.redirect(base, err=str(exc))
    if not inspection.importable_sheets:
        detection = inspection.sheets[0].detection if inspection.sheets else None
        return shell.redirect(
            base,
            err=(
                apollo.missing_header_message(detection)
                if detection is not None
                else "No worksheet in this file carries a recognizable contact header row."
            ),
        )

    staged = staging.create_staged_upload(
        _staging_dir(),
        filename=campaign_import.sanitize_filename(str(filename)),
        campaign_id=str(identifier),
        content=content,
        source_format=inspection.source_format,
        provenance={
            "source_name": campaign_import.source_name_for(
                inspection.importable_sheets[0].detection
            )
        },
    )
    return shell.redirect(f"{base}/staged/{staged.id}")


@router.get("/campaigns/{campaign_id}/imports/staged/{staged_id}")
def campaign_import_preview_page(
    campaign_id: str,
    staged_id: str,
    request: Request,
    db: Session = Depends(get_db),
    sheet: int | None = None,
) -> HTMLResponse:
    """Show exactly what confirming would do. Performs no writes."""

    found = _campaign_or_none(db, campaign_id)
    if found is None:
        return shell.not_found(request, db, "That campaign does not exist.")
    identifier, campaign = found
    staged = _load_campaign_staged(identifier, staged_id)
    if staged is None:
        return shell.not_found(
            request,
            db,
            "That upload is not available for this campaign. It may have expired, or it "
            "may belong to a different campaign.",
        )
    if staged.confirmed_batch_id:
        return shell.redirect(  # type: ignore[return-value]
            f"/app/campaigns/{identifier}/imports/{staged.confirmed_batch_id}",
            ok="This upload was already imported; showing the batch it produced.",
        )

    content = staging.read_staged_content(_staging_dir(), staged_id)
    try:
        inspection = campaign_import.inspect(content, staged.filename)
        preview = campaign_import.preview(
            db,
            campaign_id=identifier,
            content=content,
            filename=staged.filename,
            sheet_index=sheet,
        )
    except campaign_import.CampaignImportError as exc:
        return shell.render(
            request,
            db,
            "campaign_import_preview.html",
            {
                "active_nav": "campaigns",
                "page_title": f"Preview — {staged.filename}",
                "campaign": campaign,
                "staged": staged,
                "inspection": None,
                "preview": None,
                "fatal_error": str(exc),
            },
        )

    return shell.render(
        request,
        db,
        "campaign_import_preview.html",
        {
            "active_nav": "campaigns",
            "page_title": f"Preview — {staged.filename}",
            "campaign": campaign,
            "staged": staged,
            "inspection": inspection,
            "preview": preview,
            "shown_rows": preview.rows[:PREVIEW_ROWS_SHOWN],
            "fatal_error": None,
            "import_on": shell.import_on(db, get_settings()),
        },
    )


@router.post("/campaigns/{campaign_id}/imports/staged/{staged_id}/confirm")
def campaign_import_confirm(
    campaign_id: str,
    staged_id: str,
    request: Request,
    db: Session = Depends(get_db),
    sheet: str = Form(""),
) -> RedirectResponse:
    """Import the staged file. The first point anything durable is written."""

    found = _campaign_or_none(db, campaign_id)
    if found is None:
        return shell.redirect("/app/campaigns", err="That campaign does not exist.")
    identifier, campaign = found
    base = f"/app/campaigns/{identifier}/imports"
    staged = _load_campaign_staged(identifier, staged_id)
    if staged is None:
        return shell.redirect(
            base,
            err=(
                "That upload is not available for this campaign. It may have expired, or "
                "it may belong to a different campaign."
            ),
        )
    if staged.confirmed_batch_id:
        return shell.redirect(
            f"{base}/{staged.confirmed_batch_id}",
            ok="This upload was already imported; showing the batch it produced.",
        )
    if campaign.status is CampaignStatus.ARCHIVED:
        return shell.redirect(base, err="An archived campaign cannot receive contacts.")
    # Asked here as well as in the enrolment service, and not as belt and braces.
    # `campaign_import.confirm` writes a batch and then commits each row in its
    # own SAVEPOINT, catching only `SQLAlchemyError`; a refusal raised from
    # enrolment part-way down the file would escape as a 500 with a batch already
    # written and some rows already through. Asking before the first write makes
    # the refusal what the operator needs it to be — whole, with the file still
    # staged and nothing imported — and says it on the page they uploaded from.
    if campaign.execution_enabled and campaign_opted_in(campaign):
        readiness = agent_readiness.execution_readiness(
            db, campaign=campaign, prospective_stage=AgentIdentifier.SENDING
        )
        if not readiness.runnable:
            return shell.redirect(
                f"{base}/staged/{staged_id}", err=readiness.enrolment_refusal_message()
            )

    content = staging.read_staged_content(_staging_dir(), staged_id)
    try:
        result = campaign_import.confirm(
            db,
            campaign_id=identifier,
            content=content,
            filename=staged.filename,
            sheet_index=_sheet_index(sheet),
            uploaded_by=draft_service.OPERATOR_ACTOR,
        )
    except campaign_import.CampaignImportError as exc:
        return shell.redirect(f"{base}/staged/{staged_id}", err=str(exc))

    staged.confirmed_batch_id = str(result.batch_id)
    staging.update_staged_upload(_staging_dir(), staged)

    if result.reused_existing_batch:
        return shell.redirect(
            f"{base}/{result.batch_id}",
            ok="This exact file and worksheet were already imported; showing the existing batch.",
        )
    return shell.redirect(
        f"{base}/{result.batch_id}",
        ok=(
            f"{result.imported} imported, {result.matched_existing} matched an existing "
            f"contact, {result.already_in_campaign} already in this campaign, "
            f"{result.skipped_duplicate} skipped as duplicates, "
            f"{result.review_required} need review, {result.suppressed} suppressed, "
            f"{result.failed} failed."
        ),
    )


@router.post("/campaigns/{campaign_id}/imports/staged/{staged_id}/discard")
def campaign_import_discard(
    campaign_id: str, staged_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Throw the staged upload away. Nothing was ever imported from it."""

    found = _campaign_or_none(db, campaign_id)
    if found is None:
        return shell.redirect("/app/campaigns", err="That campaign does not exist.")
    identifier, _campaign = found
    base = f"/app/campaigns/{identifier}/imports"
    if _load_campaign_staged(identifier, staged_id) is None:
        return shell.redirect(base, err="That upload is not available for this campaign.")
    try:
        staging.delete_staged_upload(_staging_dir(), staged_id)
    except staging.StagedUploadNotFound:
        pass
    return shell.redirect(base, ok="Upload discarded. Nothing was imported.")


@router.get("/campaigns/{campaign_id}/imports/{batch_id}")
def campaign_import_batch_page(
    campaign_id: str,
    batch_id: str,
    request: Request,
    db: Session = Depends(get_db),
    page: int = 1,
) -> HTMLResponse:
    """The result of one confirmed import, row by row."""

    found = _campaign_or_none(db, campaign_id)
    if found is None:
        return shell.not_found(request, db, "That campaign does not exist.")
    identifier, campaign = found
    parsed = shell.uuid_or_none(batch_id)
    batch = campaign_import.get_batch(db, parsed) if parsed else None
    # A batch belonging to another campaign is not merely the wrong page: showing
    # it would disclose another campaign's contacts and their addresses.
    if batch is None or batch.campaign_id != identifier:
        return shell.not_found(request, db, "That import does not exist in this campaign.")

    current = max(1, page)
    rows, total = campaign_import.batch_rows(
        db, batch_id=batch.id, limit=shell.PAGE_SIZE, offset=(current - 1) * shell.PAGE_SIZE
    )
    return shell.render(
        request,
        db,
        "campaign_import_batch.html",
        {
            "active_nav": "campaigns",
            "page_title": f"Import — {batch.sanitized_filename or batch.filename}",
            "campaign": campaign,
            "batch": batch,
            "counts": campaign_import.batch_counts(batch),
            "rows": rows,
            "total_rows": total,
            "page": current,
            "pages": shell.pages(total),
            "base_url": f"/app/campaigns/{identifier}/imports/{batch.id}",
        },
    )


__all__ = ["router"]

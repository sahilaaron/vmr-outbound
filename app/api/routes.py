"""Phase 1 API routes: campaign creation and staged CSV import.

These are intentionally thin adapters over the services. All business rules live
in the service layer (AGENTS.md: the dashboard/API "must not contain business
rules"). Both import behaviours are gated by the ``csv_import`` feature switch.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, status
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

router = APIRouter()


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
        contacts_created=summary.contacts_created,
        reused_existing_batch=summary.reused_existing_batch,
        error_detail=summary.error_detail,
    )

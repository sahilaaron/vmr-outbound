"""Where the retired customer destinations go now.

Emails and Review are not places; Capture is one of three ways to add people;
the future-feature stubs (sending, replies, sequences, analytics) are gone.
"""

from __future__ import annotations

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.campaign import Campaign
from app.services import campaign_access
from app.services.campaign_access import actor_from_request
from app.services.sequences import read as sequence_read
from app.web.v2 import shell

router = shell.router


@router.get("/review")
def review_redirect(
    request: Request,
    db: Session = Depends(get_db),
    campaign: str | None = None,
    sequence: str | None = None,
) -> RedirectResponse:
    """A legacy Emails/Review link resolves to its Campaign — or to the list."""

    actor = actor_from_request(request)
    sequence_id = shell.uuid_or_none(sequence) if sequence else None
    if sequence_id is not None:
        record = sequence_read.get_sequence(db, sequence_id)
        if record is not None and campaign_access.may_access_campaign(
            db, record.campaign_id, actor
        ):
            return RedirectResponse(
                f"/app/people/{record.contact_id}?campaign={record.campaign_id}#emails",
                status_code=308,
            )
    campaign_id = shell.uuid_or_none(campaign) if campaign else None
    if campaign_id is not None and db.get(Campaign, campaign_id) is not None:
        if campaign_access.may_access_campaign(db, campaign_id, actor):
            return RedirectResponse(f"/app/campaigns/{campaign_id}#ready", status_code=308)
    return RedirectResponse("/app/campaigns", status_code=308)


@router.get("/capture")
def capture_redirect() -> RedirectResponse:
    return RedirectResponse("/app/add-people", status_code=308)


@router.get("/sending")
@router.get("/replies")
@router.get("/sequences")
@router.get("/analytics")
def retired_redirect() -> RedirectResponse:
    return RedirectResponse("/app", status_code=308)


__all__ = ["router"]

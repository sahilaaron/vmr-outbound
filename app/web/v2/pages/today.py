"""Today: what is due now, and where to continue.

The return surface. It brings the user back to the relevant Campaign; it is not
a notification centre and it counts no backlog.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.campaign import Campaign
from app.models.company import Company
from app.models.contact import Contact
from app.services import campaigns as campaign_service
from app.services import customer_status
from app.services.campaign_access import actor_from_request
from app.web.v2 import shell

router = shell.router


@router.get("")
@router.get("/")
def today_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """A compact operational overview. Not an inbox.

    This page used to open with "110 things want you" over a card headed
    "Decisions only you can make", summing drafts nobody had read, contacts the
    eligibility rules had blocked, stages that had failed, captures whose domain
    lookup had not resolved and identity matches nothing could settle. Four of
    those five are machine outcomes, the same contact could be counted in more
    than one of them, and none of them was work the customer had been asked to
    do. VMR Outbound is autonomous until Ready for Sending, so the page now
    reports where contacts stand and leaves it there.

    Nothing here manufactures urgency. "Could not prepare" carries no alarm tone
    and no call to action, because it is an outcome the system reached, not an
    obligation the customer incurred.
    """

    overviews = campaign_service.list_campaigns(db, actor=actor_from_request(request))
    contacts_total = db.scalar(select(func.count(Contact.id))) or 0
    companies_total = db.scalar(select(func.count(Company.id))) or 0
    confirmed = db.scalar(select(func.count(Contact.id)).where(Contact.email.is_not(None))) or 0

    campaign_rows: list[dict[str, Any]] = []
    for overview in overviews:
        campaign = overview.campaign
        campaign_progress = customer_status.progress(db, campaign_id=campaign.id)
        campaign_rows.append(
            {
                "campaign": campaign,
                "contacts": overview.contact_count,
                "progress": campaign_progress,
                "state": _campaign_progress_sentence(campaign, campaign_progress),
            }
        )

    # Summed from the same projection the campaign rows use, so the header and
    # the table can never disagree.
    overall = customer_status.CustomerProgress(
        total=sum(row["progress"].total for row in campaign_rows),
        processing=sum(row["progress"].processing for row in campaign_rows),
        ready_for_sending=sum(row["progress"].ready_for_sending for row in campaign_rows),
        could_not_prepare=sum(row["progress"].could_not_prepare for row in campaign_rows),
    )

    return shell.render(
        request,
        db,
        "today.html",
        {
            "active_nav": "today",
            "page_title": "Today",
            "today": datetime.now(UTC),
            "progress": overall,
            "status_labels": customer_status.STATUS_LABELS,
            "status_notes": customer_status.STATUS_NOTES,
            "campaign_rows": campaign_rows,
            "contacts_total": contacts_total,
            "companies_total": companies_total,
            "confirmed_addresses": confirmed,
        },
    )


def _campaign_progress_sentence(
    campaign: Campaign, progress: customer_status.CustomerProgress
) -> str:
    """What a campaign row says about itself — a fact, never an instruction.

    The sentence this replaced was assembled from "N drafts waiting for you",
    "N contacts held" and "N stages stopped". All three read as arrears.
    """

    if progress.total == 0:
        return "No contacts enrolled yet."
    if not campaign.execution_enabled:
        return "Paused — nothing new is being prepared."
    if progress.processing:
        return f"VMR is preparing {shell._plural(progress.processing, 'contact')}."
    if progress.ready_for_sending:
        return "Every contact that could be prepared is ready."
    return "Nothing is in progress."


__all__ = ["router"]

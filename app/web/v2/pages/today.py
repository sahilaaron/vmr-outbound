"""Today: what is due now, and where to continue.

The return surface. It brings the user back to the relevant Campaign; it is not
a notification centre and it counts no backlog.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.auth.accounts import session_account_id
from app.core.auth.context import current_operator
from app.core.config import get_settings
from app.services import campaign_access, customer_status, email_progress, today
from app.services import campaigns as campaign_service
from app.services.campaign_access import actor_from_request
from app.web.v2 import shell

router = shell.router


@router.get("")
@router.get("/")
def today_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    settings = get_settings()
    campaigns = [
        overview.campaign
        for overview in campaign_service.list_campaigns(db, actor=actor_from_request(request))
    ]
    user_id = session_account_id(current_operator())
    view = today.build(db, campaigns=campaigns, user_id=user_id, kb_on=shell.kb_on(db, settings))
    return shell.render(
        request,
        db,
        "today.html",
        {
            "active_nav": "today",
            "page_title": "Today",
            "today": datetime.now(UTC).astimezone(email_progress.local_zone()),
            "view": view,
            "can_dismiss": user_id is not None,
            "status_labels": customer_status.STATUS_LABELS,
            "has_campaigns": bool(campaigns),
        },
    )


@router.post("/today/dismiss")
def today_dismiss(
    request: Request, db: Session = Depends(get_db), campaign_id: str = Form("")
) -> RedirectResponse:
    """Hide one Campaign's due card for me until tomorrow. Changes no shared state."""

    user_id = session_account_id(current_operator())
    identifier = shell.uuid_or_none(campaign_id)
    if user_id is None:
        return shell.redirect("/app", err="Dismissing needs a signed-in account.")
    if identifier is None:
        return shell.redirect("/app", err="That Campaign could not be found.")
    campaign_access.require_campaign_access(db, identifier, actor_from_request(request))
    today.dismiss(db, user_id=user_id, campaign_id=identifier, day=email_progress.local_today())
    db.commit()
    return shell.redirect("/app", ok="Hidden for today. Nothing about the emails changed.")


__all__ = ["router"]

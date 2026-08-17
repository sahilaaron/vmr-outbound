"""Account: the signed-in person's own connections.

Gmail lives here because a mailbox belongs to a person, not to a Campaign.
Connecting it is invoked contextually from Create Gmail draft as well.
"""

from __future__ import annotations

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import get_settings
from app.web.v2 import shell

router = shell.router


@router.get("/account/connections")
def connections_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    settings = get_settings()
    return shell.render(
        request,
        db,
        "account_connections.html",
        {
            "active_nav": "",
            "page_title": "Connections",
            "gmail_drafts_on": shell.gmail_drafts_on(db, settings),
            "mailbox": shell.mailbox_state(db, settings),
        },
    )


__all__ = ["router"]

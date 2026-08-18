"""Account: the signed-in person's own connections.

Gmail lives here because a mailbox belongs to a person, not to a Campaign.
Connecting it is invoked contextually from Create Gmail draft as well.
"""

from __future__ import annotations

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import Settings, get_settings
from app.core.http import CSP_FORM_ACTION_STATE_KEY, csp_form_action_source_permitted
from app.web.v2 import shell

router = shell.router


def _allow_connect_gmail_to_reach_the_consent_screen(request: Request, settings: Settings) -> None:
    """Let the Connect Gmail form's own redirect survive the application's CSP.

    ``form-action`` is checked against the submission **and against every
    redirect it follows**, using the policy of the page holding the form. The
    application policy is ``form-action 'self'``, so pressing *Connect Gmail*
    posted to ``/gmail/connect``, the route answered ``303`` to Google's consent
    screen, and the browser refused to navigate there. Staging showed the defect
    in its purest form: nine ``POST /gmail/connect 303`` in the access log,
    every one of them correct, and not a single ``GET /gmail/callback`` in the
    deployment's whole history. The customer saw a button that did nothing.

    Google sign-in was never affected and that is what disguised this: it starts
    from ``<a href="/auth/google/start">``, a plain navigation, which
    ``form-action`` does not govern at all.

    What this widens is one directive, on one page, by one source: the origin of
    the configured Gmail authorization endpoint, which is the origin
    ``/gmail/connect`` builds its ``Location`` from. ``app/core/http.py``
    re-validates the shape before it uses it, so a settings value naming
    anything other than Google's consent origin leaves the policy exactly as it
    was rather than widening it to somewhere new.
    """

    origin = settings.gmail.authorization_origin()
    if csp_form_action_source_permitted(origin):
        request.scope.setdefault("state", {})[CSP_FORM_ACTION_STATE_KEY] = origin


@router.get("/account/connections")
def connections_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    settings = get_settings()
    drafts_on = shell.gmail_drafts_on(db, settings)
    mailbox = shell.mailbox_state(db, settings)
    # Only when this render will actually carry a Connect (or Reconnect) form,
    # which is exactly the template's own condition. A page showing the
    # Disconnect form posts to `/gmail/disconnect`, which redirects back into
    # `/app`, so that render needs nothing beyond `'self'`.
    if drafts_on and mailbox.available and not mailbox.connected:
        _allow_connect_gmail_to_reach_the_consent_screen(request, settings)
    return shell.render(
        request,
        db,
        "account_connections.html",
        {
            "active_nav": "",
            "page_title": "Connections",
            "gmail_drafts_on": drafts_on,
            "mailbox": mailbox,
        },
    )


__all__ = ["router"]

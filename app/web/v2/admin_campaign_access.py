"""The administrator's campaign-assignment surface.

Two routes, both writes, both refused for anybody who is not an active
administrator — and refused twice, which is deliberate rather than redundant:
``require_admin`` sits on this router, and
:func:`app.services.campaign_access.assign_user` refuses a non-administrator
actor again inside the service. The router guard is what stops the request; the
service guard is what stops a *future* caller that reaches the service another
way.

Why a separate router rather than two more handlers on the campaign page
------------------------------------------------------------------------
The same reason ``admin_users.py`` gives, and it applies more strongly here:
every other route on the v2 router is reachable by a normal operator, so a
handler added there would be administrator-only by decoration rather than by
construction. Under ``/app/admin`` it is administrator-only by *path*, which is
also what ``app/core/auth/policy.py`` already enforces at the middleware, so the
two layers agree without either having to know about the other.

The screens themselves live on the campaign pages, not here. An administrator
manages who can use a campaign while looking at that campaign, so the panel is
rendered by ``app/web/v2/routes.py`` and posts back to these two paths.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.auth.admin import require_admin
from app.core.auth.context import current_operator
from app.core.auth.csrf import require_csrf
from app.models.campaign import Campaign
from app.services import campaign_access

# Imported rather than rebuilt, for the reason `admin_users.py` records: one page
# shell and one redirect convention for the whole customer-facing interface.
from app.web.v2.shell import redirect as _redirect

router = APIRouter(
    prefix="/app/admin",
    include_in_schema=False,
    dependencies=[Depends(require_csrf), Depends(require_admin)],
)


def _actor_label() -> str:
    """Who is making this change, for the audit trail.

    The signed-in administrator's address when authentication is enabled, and a
    plainly-labelled local marker when it is not. Never a form field: an audit
    trail whose actor the caller chooses is not an audit trail.
    """

    session = current_operator()
    if session is not None and session.email:
        return session.email
    return "local:unauthenticated-development"


def _campaign_or_redirect(db: Session, campaign_id: str) -> Campaign | Response:
    try:
        identifier = uuid.UUID(campaign_id)
    except ValueError:
        return _redirect("/app/campaigns", err="That is not a campaign id.")
    campaign = db.get(Campaign, identifier)
    if campaign is None:
        return _redirect("/app/campaigns", err="That campaign does not exist.")
    return campaign


@router.post("/campaigns/{campaign_id}/assign")
def assign_campaign_user(
    request: Request,
    campaign_id: str,
    db: Session = Depends(get_db),
    user_id: str = Form(""),
    back: str = Form(""),
) -> Response:
    """Grant one existing account access to this campaign."""

    campaign = _campaign_or_redirect(db, campaign_id)
    if isinstance(campaign, Response):
        return campaign
    destination = back or f"/app/campaigns/{campaign.id}"

    try:
        target = uuid.UUID(user_id)
    except ValueError:
        return _redirect(destination, err="Choose somebody to assign this campaign to.")

    try:
        campaign_access.assign_user(
            db,
            campaign=campaign,
            user_id=target,
            actor=campaign_access.actor_from_request(request),
            actor_label=_actor_label(),
        )
    except campaign_access.CampaignAssignmentError as exc:
        db.rollback()
        return _redirect(destination, err=str(exc))
    db.commit()
    return _redirect(destination, ok="They can now open this campaign.")


@router.post("/campaigns/{campaign_id}/unassign")
def unassign_campaign_user(
    request: Request,
    campaign_id: str,
    db: Session = Depends(get_db),
    user_id: str = Form(""),
    back: str = Form(""),
) -> Response:
    """Revoke one account's assignment to this campaign."""

    campaign = _campaign_or_redirect(db, campaign_id)
    if isinstance(campaign, Response):
        return campaign
    destination = back or f"/app/campaigns/{campaign.id}"

    try:
        target = uuid.UUID(user_id)
    except ValueError:
        return _redirect(destination, err="That is not an account id.")

    removed = campaign_access.unassign_user(
        db,
        campaign=campaign,
        user_id=target,
        actor=campaign_access.actor_from_request(request),
        actor_label=_actor_label(),
    )
    db.commit()
    if not removed:
        # Not an error: the operator's intent is already satisfied. Saying so
        # plainly is better than an error page for a double-submitted form.
        return _redirect(destination, ok="They were not assigned to this campaign.")
    return _redirect(
        destination,
        ok=(
            "Access revoked. It takes effect on their next request — they do not have to sign out."
        ),
    )

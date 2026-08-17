"""Campaigns: the list, creation, and the four-tab Campaign workspace.

Overview · People · Setup · Activity. The Campaign owns the operating journey:
create it, add people, watch outcomes and act on ready emails without leaving
it. Every rule lives in the service layer; these handlers project and redirect.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import get_settings
from app.models.campaign import Campaign
from app.models.enums import AgentIdentifier, CampaignStatus
from app.services import campaign_access, campaign_workspace, customer_status
from app.services import campaigns as campaign_service
from app.services import drafts as draft_service
from app.services.agents import controls as agent_controls
from app.services.agents import readiness as agent_readiness
from app.services.campaign_access import actor_from_request
from app.services.campaigns import CampaignError
from app.services.personalization.cadence import (
    DEFAULT_ELAPSED_DAYS,
    campaign_opted_in,
    with_campaign_opt_in,
)
from app.services.seller import campaign_offerings as seller_campaign_offerings
from app.services.seller import records as seller_records
from app.web.v2 import shell

router = shell.router

TABS: tuple[tuple[str, str], ...] = (
    ("overview", "Overview"),
    ("people", "People"),
    ("setup", "Setup"),
    ("activity", "Activity"),
)

READY_TABLE_LIMIT = 200
ACTIVITY_PREVIEW = 6
PEOPLE_PAGE_SIZE = 50
CADENCE_TEXT = ", ".join(str(day) for day in DEFAULT_ELAPSED_DAYS)


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


def _campaign(db: Session, campaign_id: str) -> Campaign | None:
    identifier = shell.uuid_or_none(campaign_id)
    if identifier is None:
        return None
    return campaign_service.get_campaign(db, identifier)


def _workspace_context(
    request: Request, db: Session, campaign: Campaign, tab: str
) -> dict[str, Any]:
    """What every Campaign tab renders: the header, the tabs and the counts."""

    header = campaign_workspace.header(db, campaign)
    return {
        "active_nav": "campaigns",
        "page_title": campaign.name,
        "campaign": campaign,
        "header": header,
        "answer": _setup_answer(db, campaign, header),
        "tab": tab,
        "tabs": TABS,
        "base_href": f"/app/campaigns/{campaign.id}",
        "add_people_href": f"/app/campaigns/{campaign.id}/add-people",
        "status_labels": customer_status.STATUS_LABELS,
    }


def _setup_answer(
    db: Session, campaign: Campaign, header: campaign_workspace.CampaignHeader
) -> dict[str, Any]:
    """The one computed sentence Setup ends with, and Overview repeats when needed.

    Said before any button is pressed: a Campaign held by an administrator
    setting is told so here, in the customer's words, rather than being offered
    a Start that would be refused. Which Agents are off is Admin's to see, on
    the diagnostics page.

    **Order is the semantics, not a detail**, and the ordering below is not the
    obvious one, so it is written down.

    A Campaign the customer paused reports the pause, and offers Resume, even
    when an Agent is switched off and would independently hold preparation. The
    pause is the customer's own act on their own Campaign; answering it with
    "held by an administrator setting" tells them something false about a state
    they created, and withholds the one control that is theirs. So
    ``is_paused`` is checked *before* execution readiness — the defect this
    ordering fixes.

    A draft is not the same case and is deliberately not treated the same. It
    is the absence of a customer act rather than one, and there the
    administrator sentence is the whole point: ``set_campaign_execution``
    refuses the first enable of an opted-in Campaign whose Agents are not
    ready, so a Start offered here would be refused on click. Saying so on the
    page the customer decides from, rather than after they have decided, is a
    property with its own coverage in
    ``tests/test_campaign_execution_readiness.py`` and is not this repair's to
    drop. Readiness therefore sits between paused and draft.

    The asymmetry is real and worth naming: Resume is refused by the same
    preflight that refuses Start, so a paused, held Campaign now offers a
    Resume that comes back as an error rather than a warning. That is the
    accepted trade — a customer who paused is told the truth about their own
    Campaign first — and the refusal message the route surfaces is explicit
    about the administrator cause when they press it.
    """

    if header.is_archived:
        return {"ready": False, "text": "Archived. Nothing more will be prepared."}
    if header.is_paused:
        return {
            "ready": False,
            "text": "Paused. Resume the Campaign to carry on preparing people.",
            "action": "resume",
        }
    if campaign_opted_in(campaign):
        readiness = agent_readiness.execution_readiness(db, campaign=campaign)
        if not readiness.runnable:
            return {
                "ready": False,
                "text": "Preparation is being held by an administrator setting.",
                "admin": True,
            }
    if header.is_draft:
        return {
            "ready": False,
            "text": "Not started. Start the Campaign and VMR prepares everyone in it.",
            "action": "start",
        }
    return {"ready": True, "text": "Ready to prepare people."}


# ---------------------------------------------------------------------------
# List and create
# ---------------------------------------------------------------------------


@router.get("/campaigns")
def campaigns_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    overviews = campaign_service.list_campaigns(db, actor=actor_from_request(request))
    rows = campaign_workspace.list_rows(db, [overview.campaign for overview in overviews])
    return shell.render(
        request,
        db,
        "campaigns.html",
        {
            "active_nav": "campaigns",
            "page_title": "Campaigns",
            "rows": rows,
            "status_labels": customer_status.STATUS_LABELS,
        },
    )


@router.get("/campaigns/new")
def campaign_new_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    settings = get_settings()
    kb_on = shell.kb_on(db, settings)
    offerings = seller_records.list_offerings(db, include_archived=False) if kb_on else []
    return shell.render(
        request,
        db,
        "campaign_new.html",
        {
            "active_nav": "campaigns",
            "page_title": "New Campaign",
            "offerings": offerings,
            "kb_on": kb_on,
            "cadence_text": CADENCE_TEXT,
        },
    )


@router.post("/campaigns/new")
def campaign_create(
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    description: str = Form(""),
    offering_id: str = Form(""),
    messaging_direction: str = Form(""),
) -> RedirectResponse:
    """Create the Campaign and start it. It prepares people the moment they arrive."""

    try:
        campaign = campaign_service.create_campaign(
            db,
            name=name,
            description=description or None,
            messaging_direction=messaging_direction or None,
            status=CampaignStatus.DRAFT,
            allow_provisional_domains=True,
            actor=draft_service.OPERATOR_ACTOR,
            created_by_user_id=actor_from_request(request).user_id,
        )
    except CampaignError as exc:
        return shell.redirect("/app/campaigns/new", err=str(exc))

    settings = get_settings()
    chosen = shell.uuid_or_none(offering_id) if offering_id else None
    if chosen is not None and shell.kb_on(db, settings):
        try:
            seller_campaign_offerings.associate(
                db, campaign=campaign, offering_id=chosen, actor=draft_service.OPERATOR_ACTOR
            )
        except Exception as exc:  # pragma: no cover - surfaced to the operator
            db.commit()
            return shell.redirect(
                f"/app/campaigns/{campaign.id}/setup",
                err=f"The Campaign was created, but the offering was not attached: {exc}",
            )
    db.commit()

    try:
        campaign_service.apply_campaign_execution(
            db,
            campaign.id,
            enabled=True,
            actor=draft_service.OPERATOR_ACTOR,
            reason="started on creation",
        )
    except CampaignError as exc:
        return shell.redirect(
            f"/app/campaigns/{campaign.id}/setup",
            err=f"{campaign.name} was created but could not be started: {exc}",
        )
    return shell.redirect(
        f"/app/campaigns/{campaign.id}",
        ok=f"{campaign.name} is ready. Add people and VMR prepares their emails.",
    )


# ---------------------------------------------------------------------------
# Workspace tabs
# ---------------------------------------------------------------------------


@router.get("/campaigns/{campaign_id}")
def campaign_overview(
    campaign_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    campaign = _campaign(db, campaign_id)
    if campaign is None:
        return shell.not_found(request, db, "That Campaign does not exist.")
    context = _workspace_context(request, db, campaign, "overview")
    header = context["header"]
    settings = get_settings()

    offerings = (
        seller_campaign_offerings.offerings_for_campaign(db, campaign.id)
        if shell.kb_on(db, settings)
        else []
    )
    context.update(
        {
            "ready": campaign_workspace.ready_people(
                db, campaign_id=campaign.id, limit=READY_TABLE_LIMIT
            ),
            "ready_limit": READY_TABLE_LIMIT,
            "happening": campaign_workspace.happening_now(header),
            "reasons": campaign_workspace.could_not_prepare_reasons(db, campaign_id=campaign.id),
            "offerings": offerings,
            "research_allowed": _research_allowed(db, campaign),
            "sequence_on": campaign_opted_in(campaign),
            "cadence_text": CADENCE_TEXT,
            "access_people": campaign_access.campaign_people(db, campaign),
            "activity": campaign_workspace.activity(
                db, campaign_id=campaign.id, limit=ACTIVITY_PREVIEW
            ),
            "live_seconds": 30 if header.progress.processing else None,
        }
    )
    return shell.render(request, db, "campaign_overview.html", context)


@router.get("/campaigns/{campaign_id}/people")
def campaign_people(
    campaign_id: str,
    request: Request,
    db: Session = Depends(get_db),
    outcome: str = "all",
    q: str | None = None,
    page: int = 1,
) -> HTMLResponse:
    campaign = _campaign(db, campaign_id)
    if campaign is None:
        return shell.not_found(request, db, "That Campaign does not exist.")
    context = _workspace_context(request, db, campaign, "people")
    keys = {key for key, _label in campaign_workspace.OUTCOME_FILTERS}
    chosen = outcome if outcome in keys else "all"
    current = max(1, page)
    rows, total = campaign_workspace.people(
        db,
        campaign_id=campaign.id,
        outcome=chosen,
        search=q,
        limit=PEOPLE_PAGE_SIZE,
        offset=(current - 1) * PEOPLE_PAGE_SIZE,
    )
    base = f"/app/campaigns/{campaign.id}/people?outcome={chosen}"
    if q:
        base += f"&q={quote_plus(q)}"
    context.update(
        {
            "rows": rows,
            "total": total,
            "page": current,
            "pages": shell.pages(total, PEOPLE_PAGE_SIZE),
            "outcome": chosen,
            "filters": campaign_workspace.OUTCOME_FILTERS,
            "q": q or "",
            "base_url": base,
            "status_tones": customer_status.STATUS_TONES,
        }
    )
    return shell.render(request, db, "campaign_people.html", context)


@router.get("/campaigns/{campaign_id}/activity")
def campaign_activity(
    campaign_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    campaign = _campaign(db, campaign_id)
    if campaign is None:
        return shell.not_found(request, db, "That Campaign does not exist.")
    context = _workspace_context(request, db, campaign, "activity")
    context["activity"] = campaign_workspace.activity(db, campaign_id=campaign.id, limit=100)
    context["batches"] = campaign_workspace.import_batches(db, campaign_id=campaign.id)
    return shell.render(request, db, "campaign_activity.html", context)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def _research_allowed(db: Session, campaign: Campaign) -> bool:
    try:
        return agent_controls.campaign_live_opt_in(
            db, campaign=campaign, agent_id=AgentIdentifier.RESEARCH
        )
    except Exception:
        return False


@router.get("/campaigns/{campaign_id}/setup")
def campaign_setup(
    campaign_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    campaign = _campaign(db, campaign_id)
    if campaign is None:
        return shell.not_found(request, db, "That Campaign does not exist.")
    context = _workspace_context(request, db, campaign, "setup")
    settings = get_settings()
    kb_on = shell.kb_on(db, settings)
    linked = seller_campaign_offerings.offerings_for_campaign(db, campaign.id) if kb_on else []
    context.update(
        {
            "kb_on": kb_on,
            "offerings": seller_records.list_offerings(db, include_archived=False) if kb_on else [],
            "linked_offering": linked[0] if linked else None,
            "linked_offerings": linked,
            "sequences_on": shell.sequences_on(db, settings),
            "sequence_on": campaign_opted_in(campaign),
            "cadence_text": CADENCE_TEXT,
            "research_allowed": _research_allowed(db, campaign),
            "access_owner": campaign_access.campaign_owner(db, campaign),
            "access_people": campaign_access.campaign_people(db, campaign),
            "assignable_users": campaign_access.assignable_users(db, campaign),
        }
    )
    return shell.render(request, db, "campaign_setup.html", context)


@router.post("/campaigns/{campaign_id}/setup")
def campaign_setup_save(
    campaign_id: str,
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    description: str = Form(""),
    messaging_direction: str = Form(""),
    primary_cta: str = Form(""),
    offering_id: str = Form(""),
    allow_provisional_domains: str = Form(""),
    sequence_enabled: str = Form(""),
) -> RedirectResponse:
    campaign = _campaign(db, campaign_id)
    if campaign is None:
        return shell.redirect("/app/campaigns", err="That Campaign does not exist.")
    back = f"/app/campaigns/{campaign.id}/setup"
    settings = get_settings()

    opted_in = (
        shell.checkbox(sequence_enabled)
        if shell.sequences_on(db, settings)
        else campaign_opted_in(campaign)
    )
    try:
        campaign = campaign_service.update_campaign(
            db,
            campaign.id,
            name=name,
            description=description or None,
            messaging_direction=messaging_direction or None,
            primary_cta=primary_cta or None,
            allow_provisional_domains=shell.checkbox(allow_provisional_domains),
            cadence_config=with_campaign_opt_in(campaign, enabled=opted_in),
            actor=draft_service.OPERATOR_ACTOR,
            reason="setup changed",
        )
    except CampaignError as exc:
        db.rollback()
        return shell.redirect(back, err=str(exc))

    if shell.kb_on(db, settings):
        wanted = shell.uuid_or_none(offering_id) if offering_id else None
        current = seller_campaign_offerings.offerings_for_campaign(db, campaign.id)
        try:
            for offering in current:
                if wanted is None or offering.id != wanted:
                    seller_campaign_offerings.dissociate(
                        db,
                        campaign=campaign,
                        offering_id=offering.id,
                        actor=draft_service.OPERATOR_ACTOR,
                    )
            if wanted is not None and all(offering.id != wanted for offering in current):
                seller_campaign_offerings.associate(
                    db, campaign=campaign, offering_id=wanted, actor=draft_service.OPERATOR_ACTOR
                )
        except Exception as exc:
            db.rollback()
            return shell.redirect(back, err=f"The offering could not be changed: {exc}")

    db.commit()
    return shell.redirect(back, ok="Setup saved.")


@router.post("/campaigns/{campaign_id}/setup/research")
def campaign_setup_research(
    campaign_id: str,
    request: Request,
    db: Session = Depends(get_db),
    allowed: str = Form(""),
) -> RedirectResponse:
    """Allow or stop website research for this Campaign. Administrators only."""

    campaign = _campaign(db, campaign_id)
    if campaign is None:
        return shell.redirect("/app/campaigns", err="That Campaign does not exist.")
    back = f"/app/campaigns/{campaign.id}/setup"
    if not actor_from_request(request).is_admin:
        return shell.redirect(back, err="Only an administrator can change website research.")
    try:
        agent_controls.set_campaign_live_opt_in(
            db,
            campaign_id=campaign.id,
            agent_id=AgentIdentifier.RESEARCH,
            live=shell.checkbox(allowed),
            actor=draft_service.OPERATOR_ACTOR,
            reason="changed from Campaign Setup",
        )
    except agent_controls.AgentControlError as exc:
        db.rollback()
        return shell.redirect(back, err=str(exc))
    db.commit()
    return shell.redirect(
        back,
        ok=(
            "Website research is allowed for this Campaign."
            if shell.checkbox(allowed)
            else "Website research is off for this Campaign."
        ),
    )


LIFECYCLE_ACTIONS = ("start", "resume", "pause", "archive")


@router.post("/campaigns/{campaign_id}/lifecycle")
def campaign_lifecycle(
    campaign_id: str,
    request: Request,
    db: Session = Depends(get_db),
    action: str = Form(...),
    back: str = Form(""),
) -> RedirectResponse:
    """Start, resume, pause or archive. One route, four verbs, no other state."""

    campaign = _campaign(db, campaign_id)
    if campaign is None:
        return shell.redirect("/app/campaigns", err="That Campaign does not exist.")
    target = shell.in_app_path(back, f"/app/campaigns/{campaign.id}/setup")
    verb = action.strip().lower()
    if verb not in LIFECYCLE_ACTIONS:
        return shell.redirect(target, err="That is not a Campaign action.")
    try:
        if verb in ("start", "resume"):
            campaign_service.apply_campaign_execution(
                db,
                campaign.id,
                enabled=True,
                actor=draft_service.OPERATOR_ACTOR,
                reason="started from Setup" if verb == "start" else "resumed from Setup",
            )
            message = (
                f"{campaign.name} started. VMR prepares everyone in it."
                if verb == "start"
                else f"{campaign.name} resumed."
            )
        elif verb == "pause":
            campaign_service.apply_campaign_execution(
                db,
                campaign.id,
                enabled=False,
                actor=draft_service.OPERATOR_ACTOR,
                reason="paused from Setup",
            )
            message = f"{campaign.name} paused. Nothing new is prepared until it is resumed."
        else:
            campaign_service.apply_campaign_execution(
                db,
                campaign.id,
                enabled=False,
                actor=draft_service.OPERATOR_ACTOR,
                reason="archived",
            )
            campaign_service.update_campaign(
                db,
                campaign.id,
                status=CampaignStatus.ARCHIVED,
                actor=draft_service.OPERATOR_ACTOR,
                reason="archived from Setup",
            )
            db.commit()
            return shell.redirect("/app/campaigns", ok=f"{campaign.name} archived.")
    except CampaignError as exc:
        db.rollback()
        return shell.redirect(target, err=str(exc))
    return shell.redirect(target, ok=message)


# ---------------------------------------------------------------------------
# Add people
# ---------------------------------------------------------------------------


def _add_people_context(request: Request, db: Session, campaign: Campaign | None) -> dict[str, Any]:
    settings = get_settings()
    campaigns = [
        overview.campaign
        for overview in campaign_service.list_campaigns(db, actor=actor_from_request(request))
        if overview.campaign.status is not CampaignStatus.ARCHIVED
    ]
    return {
        "active_nav": "campaigns",
        "page_title": "Add people",
        "campaign": campaign,
        "campaigns": campaigns,
        "capture_on": shell.capture_on(db, settings),
        "sheets_on": shell.sheets_on(db, settings),
        "import_on": shell.import_on(db, settings),
    }


@router.get("/add-people")
def add_people_page(
    request: Request, db: Session = Depends(get_db), campaign: str | None = None
) -> HTMLResponse:
    chosen = None
    identifier = shell.uuid_or_none(campaign) if campaign else None
    if identifier is not None:
        campaign_access.require_campaign_access(db, identifier, actor_from_request(request))
        chosen = campaign_service.get_campaign(db, identifier)
    return shell.render(request, db, "add_people.html", _add_people_context(request, db, chosen))


@router.get("/campaigns/{campaign_id}/add-people")
def campaign_add_people_page(
    campaign_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    campaign = _campaign(db, campaign_id)
    if campaign is None:
        return shell.not_found(request, db, "That Campaign does not exist.")
    context = _add_people_context(request, db, campaign)
    context["page_title"] = f"Add people to {campaign.name}"
    return shell.render(request, db, "add_people.html", context)


# ---------------------------------------------------------------------------
# Legacy Campaign URLs
# ---------------------------------------------------------------------------


@router.get("/campaigns/{campaign_id}/edit")
def campaign_edit_redirect(campaign_id: str) -> RedirectResponse:
    return RedirectResponse(f"/app/campaigns/{campaign_id}/setup", status_code=308)


__all__ = ["router", "TABS"]

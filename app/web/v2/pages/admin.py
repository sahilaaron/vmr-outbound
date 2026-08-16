"""Admin inside the product: the machinery room, in the same shell.

A landing page, the Agent settings & logs, per-Campaign diagnostics (stopped
people, re-runs, live opt-ins), and the suppression list. Every route here is
administrator-only by the ``/app/admin`` prefix in ``app/core/auth/policy.py``.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import get_settings
from app.models.campaign import Campaign
from app.models.enums import AgentControlStatus, AgentIdentifier
from app.models.suppression import Suppression
from app.services import campaign_workspace, customer_status, workbench_agents
from app.services import campaigns as campaign_service
from app.services import drafts as draft_service
from app.services.admin_workbench.reader import AdminWorkbenchReader
from app.services.agents import rerun as agent_rerun
from app.services.agents.registry import AGENT_SPECS, PIPELINE_ORDER
from app.services.campaign_access import actor_from_request
from app.services.imports import display
from app.services.operations import settings as operational
from app.web.v2 import shell

router = shell.router


def _reader(db: Session) -> workbench_agents.PhaseTwoWorkbenchReader:
    return workbench_agents.PhaseTwoWorkbenchReader(db)


#: The three phases the design groups the nine Agents into. Grouping, not authority
#: — the order and membership come from ``PIPELINE_ORDER``.
PHASES: dict[AgentIdentifier, str] = {
    AgentIdentifier.CAPTURE: "find",
    AgentIdentifier.IDENTITY: "find",
    AgentIdentifier.COMPANY: "find",
    AgentIdentifier.RESEARCH: "learn",
    AgentIdentifier.EMAIL: "learn",
    AgentIdentifier.VERIFICATION: "learn",
    AgentIdentifier.INSIGHTS: "write",
    AgentIdentifier.PERSONALIZATION: "write",
    AgentIdentifier.SENDING: "write",
}

#: What each Agent is for, in the customer's language rather than the queue's.
#: Descriptive copy about behaviour that already exists — no capability is claimed
#: here that the adapter does not have.
AGENT_BLURBS: dict[AgentIdentifier, str] = {
    AgentIdentifier.CAPTURE: (
        "Pulls in everyone from a Sales Navigator or LinkedIn page you opened. Never "
        "navigates on its own. Capturing happens in the extension, so this stage is "
        "already complete by the time a contact is enrolled — its number is how many "
        "arrived."
    ),
    AgentIdentifier.IDENTITY: (
        "Ties each capture to one permanent person record, so nobody is duplicated "
        "across campaigns. Two candidates means it stops and asks you."
    ),
    AgentIdentifier.COMPANY: (
        "Ties each person to a company, and that company to a real website — resolved "
        "once and reused for everyone who works there."
    ),
    AgentIdentifier.RESEARCH: (
        "Collects public facts about the company, each stored with its source and its "
        "date. A thin result stays thin rather than being filled in."
    ),
    AgentIdentifier.EMAIL: (
        "Works out the address format a company uses, builds up to three candidates per "
        "person in one fixed order, and stops at the first that validates."
    ),
    AgentIdentifier.VERIFICATION: (
        "Confirms with the receiving mail server that this exact mailbox will accept "
        "delivery. A catch-all domain is reported as unconfirmed, never as valid."
    ),
    AgentIdentifier.INSIGHTS: (
        "Picks the sourced facts worth opening an email with — or records that there are "
        "none. A claim with no usable source is dropped, not downgraded."
    ),
    AgentIdentifier.PERSONALIZATION: (
        "Writes the finished email inside your Knowledge Base guardrails, and refuses to "
        "write at all for someone who has no confirmed mailbox."
    ),
    AgentIdentifier.SENDING: (
        "Would hand approved messages to a sending service. No adapter is registered, so "
        "it cannot be enabled and nothing is ever sent."
    ),
}


# ---------------------------------------------------------------------------
# Landing
# ---------------------------------------------------------------------------


def _campaign_rows(request: Request, db: Session) -> list[campaign_workspace.CampaignListRow]:
    return campaign_workspace.list_rows(
        db,
        [
            overview.campaign
            for overview in campaign_service.list_campaigns(db, actor=actor_from_request(request))
        ],
    )


@router.get("/admin")
def admin_home(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Admin — Home / Health: what is running, what needs an administrator."""

    settings = get_settings()
    overview = None
    failed_jobs: dict[Any, int] = {}
    if shell.agent_workbench_on(db, settings):
        try:
            overview = AdminWorkbenchReader(db, settings=settings).overview()
            failed_jobs = {row.campaign_id: row.failed_jobs for row in overview.campaigns}
        except Exception:  # pragma: no cover - a health page must render on a sick system
            overview = None
    return shell.render(
        request,
        db,
        "admin_home.html",
        {
            "active_nav": "admin",
            "page_title": "Admin",
            "overview": overview,
            "failed_jobs": failed_jobs,
            "campaign_rows": _campaign_rows(request, db),
            "release_id": settings.release_id,
        },
    )


@router.get("/admin/data-tools")
def admin_data_tools(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return shell.render(
        request,
        db,
        "admin_data_tools.html",
        {
            "active_nav": "admin",
            "page_title": "Data tools",
            "local_env": get_settings().app_env == "local",
        },
    )


@router.get("/admin/diagnostics")
def admin_diagnostics(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return shell.render(
        request,
        db,
        "admin_diagnostics.html",
        {
            "active_nav": "admin",
            "page_title": "Diagnostics",
            "campaign_rows": _campaign_rows(request, db),
        },
    )


@router.get("/agents")
def agents_redirect(request: Request) -> RedirectResponse:
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(f"/app/admin/agents{query}", status_code=308)


@router.get("/suppressions")
def suppressions_redirect() -> RedirectResponse:
    return RedirectResponse("/app/admin/suppressions", status_code=308)


# ---------------------------------------------------------------------------
# Campaign diagnostics
# ---------------------------------------------------------------------------


@router.get("/admin/campaigns/{campaign_id}/diagnostics")
def campaign_diagnostics(
    campaign_id: str,
    request: Request,
    db: Session = Depends(get_db),
    stage: str | None = None,
) -> HTMLResponse:
    """Where people stopped, per Agent, and the recovery controls for one Campaign."""

    identifier = shell.uuid_or_none(campaign_id)
    campaign = db.get(Campaign, identifier) if identifier is not None else None
    if campaign is None:
        return shell.not_found(request, db, "That Campaign does not exist.")
    settings = get_settings()
    if not shell.agent_workbench_on(db, settings):
        return shell.render(
            request,
            db,
            "agents_disabled.html",
            {"active_nav": "admin", "page_title": "Diagnostics"},
        )

    selected: AgentIdentifier | None = None
    if stage:
        try:
            selected = AgentIdentifier(stage)
        except ValueError:
            selected = None

    execution = _reader(db).campaign_execution(campaign.id, stage=selected, limit=0)
    failures = agent_rerun.failure_counts(db, campaign.id)
    stage_rows: list[dict[str, Any]] = []
    controls = {control.agent_id: control for control in execution.controls} if execution else {}
    for position, agent_id in enumerate(PIPELINE_ORDER, start=1):
        spec = AGENT_SPECS[agent_id]
        control = controls.get(agent_id)
        stage_rows.append(
            {
                "agent_id": agent_id.value,
                "index": position,
                "label": spec.display_name,
                "resting": int(execution.stage_counts.get(agent_id.value, 0)) if execution else 0,
                "failed": failures.get(agent_id.value, 0),
                "control": control,
                "selected": selected is agent_id,
                "href": f"/app/admin/campaigns/{campaign.id}/diagnostics?stage={agent_id.value}",
            }
        )
    rerun_candidates: tuple[agent_rerun.RerunCandidate, ...] = ()
    if selected is not None:
        rerun_candidates = agent_rerun.candidates(db, campaign_id=campaign.id, agent_id=selected)
    selected_control = controls.get(selected) if selected is not None else None

    return shell.render(
        request,
        db,
        "admin_campaign_diagnostics.html",
        {
            "active_nav": "admin",
            "page_title": f"Diagnostics — {campaign.name}",
            "campaign": campaign,
            "header": campaign_workspace.header(db, campaign),
            "stage_rows": stage_rows,
            "selected": selected.value if selected else None,
            "selected_label": AGENT_SPECS[selected].display_name if selected else None,
            "selected_control": selected_control,
            "rerun_candidates": rerun_candidates,
            "rerun_spends": (selected in agent_rerun.SPENDS_PER_CONTACT) if selected else False,
            "rerun_ceiling": agent_rerun.MAX_PER_RERUN,
            "activity": execution.recent_events if execution else (),
            "status_labels": customer_status.STATUS_LABELS,
        },
    )


@router.post("/admin/campaigns/{campaign_id}/agents/{agent_id}/rerun")
def campaign_agent_rerun(
    campaign_id: str,
    agent_id: str,
    request: Request,
    db: Session = Depends(get_db),
    reason: str = Form(""),
    campaign_contact_id: str = Form(""),
    back: str = Form(""),
) -> RedirectResponse:
    """Run one Agent again for the contacts it has stopped on.

    The whole campaign by default, or one contact when ``campaign_contact_id`` is
    given. Both go through the same guards, so the single-contact button cannot do
    anything the bulk one would refuse.

    Refusals are carried back as a flash rather than raised: an operator who presses
    "run again" and sees nothing happen has been told less than nothing.
    """

    identifier = shell.uuid_or_none(campaign_id)
    if identifier is None:
        return shell.redirect("/app/campaigns", err="That is not a campaign id.")
    try:
        target = AgentIdentifier(agent_id)
    except ValueError:
        return shell.redirect(
            f"/app/admin/campaigns/{identifier}/diagnostics", err="That is not an Agent."
        )

    destination = back or f"/app/admin/campaigns/{identifier}/diagnostics?stage={target.value}"
    try:
        outcome = agent_rerun.rerun_stage(
            db,
            campaign_id=identifier,
            agent_id=target,
            actor=draft_service.OPERATOR_ACTOR,
            reason=reason or None,
            campaign_contact_id=shell.uuid_or_none(campaign_contact_id)
            if campaign_contact_id
            else None,
        )
    except agent_rerun.RerunError as exc:
        return shell.redirect(destination, err=str(exc))

    db.commit()
    message = outcome.message()
    if outcome.refusals:
        # Name the first few rather than a bare count: "3 were not re-run" sends the
        # operator hunting, and the reason is already in hand.
        shown = "; ".join(
            f"{display.safe_text(refusal.contact_label)} — {refusal.reason}"
            for refusal in outcome.refusals[:3]
        )
        remaining = len(outcome.refusals) - 3
        if remaining > 0:
            shown += f"; and {remaining} more"
        message = f"{message} {shown}"
    if not outcome.accepted:
        return shell.redirect(destination, err=message)
    return shell.redirect(destination, ok=message)


@router.post("/admin/campaigns/{campaign_id}/agents/{agent_id}/live")
def campaign_agent_live_opt_in(
    campaign_id: str,
    agent_id: str,
    request: Request,
    db: Session = Depends(get_db),
    live: str = Form(""),
    expected_version: str = Form(""),
    reason: str = Form(""),
    back: str = Form(""),
) -> RedirectResponse:
    """Let this campaign's Agent do real, outside work — or stop letting it.

    Four Agents refuse to execute until the campaign's effective configuration
    carries ``{"live": true}``: Research, Verification, Insights and
    Personalization. Until this route existed the product had no way to say it.
    The switch was reachable only through the Phase 2 API or the database, so a
    campaign could show every Agent enabled while every job it claimed came back
    ``research_not_live`` — which is what held 18 Campaign Contacts at Research.

    It is deliberately *not* the status control. Status decides whether an Agent
    may claim work; this decides whether the work it claims may reach a provider,
    another organisation's website or a metered model. Both are versioned, both
    are campaign-scoped, and neither implies the other — see
    ``WorkbenchCommands.set_campaign_live_opt_in``.

    ``expected_version`` travels through untouched for the reason the status
    control gives: two people on the same campaign must not be able to overwrite
    each other silently.
    """

    identifier = shell.uuid_or_none(campaign_id)
    if identifier is None:
        return shell.redirect("/app/campaigns", err="That is not a campaign id.")
    try:
        target = AgentIdentifier(agent_id)
    except ValueError:
        return shell.redirect(
            f"/app/admin/campaigns/{identifier}/diagnostics", err="That is not an Agent."
        )

    destination = back or f"/app/admin/campaigns/{identifier}/diagnostics?stage={target.value}"
    # A blank version means the page saw no campaign override at all, which is a
    # claim about the world rather than a missing value. Coercing it to 0 would
    # turn a conflict into a silent overwrite.
    raw_version = expected_version.strip()
    version: int | None
    if not raw_version:
        version = None
    else:
        try:
            version = int(raw_version)
        except ValueError:
            return shell.redirect(destination, err="That control version is not a number.")

    wanted = live.lower() in {"1", "true", "on", "yes"}
    try:
        outcome = workbench_agents.WorkbenchCommands(db).set_campaign_live_opt_in(
            identifier,
            target,
            live=wanted,
            expected_version=version,
            reason=reason or None,
        )
    except workbench_agents.WorkbenchCommandError as exc:
        db.rollback()
        return shell.redirect(f"/app/admin/campaigns/{identifier}/diagnostics", err=str(exc))
    # Committed either way. A refusal writes exactly one thing — the audit event
    # recording that somebody asked and was told no — and that record is the
    # point: "who tried to make this campaign spend, and when" is a question the
    # trail has to be able to answer, not only "who succeeded".
    db.commit()
    if not outcome.accepted:
        return shell.redirect(destination, err=outcome.summary)
    return shell.redirect(destination, ok=outcome.summary)


@router.get("/admin/agents")
def agents_page(
    request: Request,
    db: Session = Depends(get_db),
    agent: str | None = None,
    campaign: str | None = None,
) -> HTMLResponse:
    """One workshop per Agent: what it may do, and a log of what it did.

    The design's per-Agent numeric settings (concurrency, spend caps, retry counts)
    are not settings in this product — retry limits are registry facts and
    concurrency is the worker pool's, not the Agent's. So the workshop shows the
    controls that genuinely exist: the enabled/paused/disabled switch with its
    optimistic-concurrency version, the precedence that produced the current state,
    and the registry facts as facts.
    """

    settings = get_settings()
    if not shell.agent_workbench_on(db, settings):
        return shell.render(
            request,
            db,
            "agents_disabled.html",
            {"active_nav": "admin", "page_title": "Agent settings"},
        )

    selected: AgentIdentifier = AgentIdentifier.PERSONALIZATION
    if agent:
        try:
            selected = AgentIdentifier(agent)
        except ValueError:
            selected = AgentIdentifier.PERSONALIZATION

    campaign_id = shell.uuid_or_none(campaign) if campaign else None
    reader = _reader(db)
    overview = reader.overview()
    detail = reader.agent_detail(selected, campaign_id=campaign_id)
    jobs = reader.jobs(agent_id=selected, campaign_id=campaign_id, limit=25)

    return shell.render(
        request,
        db,
        "agents.html",
        {
            "active_nav": "admin",
            "page_title": "Agent settings & logs",
            "overview": overview,
            "detail": detail,
            "jobs": jobs,
            "selected": selected,
            "spec": AGENT_SPECS[selected],
            "blurb": AGENT_BLURBS[selected],
            "phases": PHASES,
            "blurbs": AGENT_BLURBS,
            "campaigns": campaign_service.list_campaigns(db, actor=actor_from_request(request)),
            "campaign_id": campaign_id,
        },
    )


@router.post("/admin/agents/{agent_id}/control")
def agent_control(
    agent_id: str,
    request: Request,
    db: Session = Depends(get_db),
    status: str = Form(...),
    expected_version: str = Form(""),
    reason: str = Form(""),
    campaign_id: str = Form(""),
) -> RedirectResponse:
    """Change what an Agent may claim.

    Carries ``expected_version`` through untouched: the control row is versioned
    precisely so two operators cannot silently overwrite each other, and dropping it
    here would remove that protection.
    """

    try:
        target = AgentIdentifier(agent_id)
    except ValueError:
        return shell.redirect("/app/admin/agents", err="That is not an Agent.")
    try:
        wanted = AgentControlStatus(status)
    except ValueError:
        return shell.redirect(
            f"/app/admin/agents?agent={agent_id}", err="That is not a control state."
        )
    # A blank field means the page saw no stored control at all. That is a claim
    # about the world, not a missing value: if a control exists now, someone created
    # it after the page rendered and the operator has not seen it. Coercing it to 0
    # would turn that conflict into a silent overwrite.
    raw_version = expected_version.strip()
    version: int | None
    if not raw_version:
        version = None
    else:
        try:
            version = int(raw_version)
        except ValueError:
            return shell.redirect(
                f"/app/admin/agents?agent={agent_id}", err="That control version is not a number."
            )

    commands = workbench_agents.WorkbenchCommands(db)
    scope = shell.uuid_or_none(campaign_id) if campaign_id else None
    back = f"/app/admin/agents?agent={agent_id}" + (f"&campaign={scope}" if scope else "")
    if scope is not None:
        outcome = commands.set_campaign_override(
            scope, target, wanted, expected_version=version, reason=reason or None
        )
    else:
        outcome = commands.set_global_agent_status(
            target, wanted, expected_version=version, reason=reason or None
        )
    if not outcome.accepted:
        return shell.redirect(back, err=outcome.message)
    db.commit()
    return shell.redirect(back, ok=outcome.message)


@router.get("/admin/suppressions")
def suppressions_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Who is never contacted, and why.

    Read-only. Suppression wins over every other decision including your approval,
    so adding to it stays a deliberate act on the admin surface rather than a click
    on a browsing screen.
    """

    active = list(
        db.scalars(
            select(Suppression)
            .where(Suppression.is_active.is_(True))
            .order_by(Suppression.created_at.desc())
            .limit(200)
        ).all()
    )
    total = (
        db.scalar(select(func.count(Suppression.id)).where(Suppression.is_active.is_(True))) or 0
    )
    return shell.render(
        request,
        db,
        "suppressions.html",
        {
            "active_nav": "admin",
            "page_title": "Suppression list",
            "rows": active,
            "total": total,
            "enabled": operational.enabled(db, "suppressions"),
        },
    )


__all__ = ["router"]

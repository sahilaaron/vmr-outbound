"""Admin Workbench page routes (server-rendered, Jinja2).

The Admin Workbench is the primary operator surface: the authoritative control
and diagnosis centre for the whole application, organised around the operator's
mental model —

    Campaign -> Contacts -> Agent/Stage progress -> worker -> Agent Job
    -> attempt -> evidence, output, failure and available corrective action.

Routes here are thin adapters (AGENTS.md: the dashboard must not contain
business rules). Every page reads through
:class:`app.services.admin_workbench.reader.AdminWorkbenchReader`; every
mutation goes through the existing authoritative command surface
(:class:`app.services.workbench_agents.commands.WorkbenchCommands`). No route
or template writes a row or derives a state the services did not commit.

The router is mounted only when the ``workbench`` feature switch is on (and the
application is locked to ``APP_ENV=local`` for that). Read-only pages work with
that alone; the contact/job corrective actions additionally require the
``agent_workbench`` switch, exactly like the legacy monitor they share their
command surface with.

Flash messages travel as ``ok``/``err`` query parameters on 303 redirects; the
pages stay stateless (no sessions, no cookies).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import get_settings
from app.models.campaign import CampaignContact
from app.models.enums import (
    AgentIdentifier,
    AgentJobStatus,
    CampaignContactEligibility,
)
from app.models.verification_job import AgentJob
from app.services import workbench_agents
from app.services.admin_workbench.reader import PAGE_SIZE, AdminWorkbenchReader
from app.services.admin_workbench.views import (
    FAILURE_CATEGORIES,
    DiagnosticLink,
    DiagnosticsView,
)
from app.services.agents.registry import AGENT_SPECS, PIPELINE_ORDER
from app.services.seller.common import OPERATOR_ACTOR

router = APIRouter()

_TEMPLATES_DIR = "app/web/templates"
templates = Jinja2Templates(directory=_TEMPLATES_DIR)


def _fmt_dt(value: datetime | date | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return value.isoformat()


def _fmt_ago(value: datetime | None) -> str:
    """A compact age, from a committed timestamp; never a fabricated one."""

    if value is None:
        return "—"
    now = datetime.now(UTC)
    moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    seconds = int((now - moment).total_seconds())
    if seconds < 0:
        return _fmt_dt(value)
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _fmt_duration(value: float | None) -> str:
    if value is None:
        return "—"
    seconds = int(value)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def _pretty_json(value: object) -> str:
    try:
        return json.dumps(value, indent=2, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(value)


templates.env.filters["dt"] = _fmt_dt
templates.env.filters["ago"] = _fmt_ago
templates.env.filters["duration"] = _fmt_duration
templates.env.filters["pretty_json"] = _pretty_json


def _database_ok(db: Session) -> bool:
    try:
        db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _reader(db: Session) -> AdminWorkbenchReader:
    return AdminWorkbenchReader(db, settings=get_settings())


def _commands(db: Session) -> workbench_agents.WorkbenchCommands:
    return workbench_agents.WorkbenchCommands(db, actor=OPERATOR_ACTOR)


def _actions_available() -> bool:
    return get_settings().features.agent_workbench


def _attention_counts(db: Session) -> dict[str, int]:
    """Two cheap counts for the persistent navigation badges."""

    try:
        failed_jobs = int(
            db.scalar(
                select(func.count(AgentJob.id)).where(AgentJob.status == AgentJobStatus.FAILED)
            )
            or 0
        )
        blocked = int(
            db.scalar(
                select(func.count(CampaignContact.id)).where(
                    CampaignContact.eligibility_status == CampaignContactEligibility.BLOCKED
                )
            )
            or 0
        )
    except Exception:
        return {"failures": 0}
    return {"failures": failed_jobs + blocked}


def _render(
    request: Request,
    db: Session,
    template: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    settings = get_settings()
    shared: dict[str, Any] = {
        "app_env": settings.app_env,
        "dry_run": settings.dry_run,
        "features_enabled": settings.features.enabled(),
        "local_env": settings.app_env.lower() == "local",
        "database_ok": _database_ok(db),
        "actions_available": _actions_available(),
        "nav_badges": _attention_counts(db),
        "flash_ok": request.query_params.get("ok"),
        "flash_err": request.query_params.get("err"),
    }
    shared.update(context)
    return templates.TemplateResponse(
        request=request, name=template, context=shared, status_code=status_code
    )


def _redirect(url: str, *, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    params = {}
    if ok:
        params["ok"] = ok
    if err:
        params["err"] = err
    if params:
        url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
    return RedirectResponse(url, status_code=303)


def _command_redirect(url: str, outcome: workbench_agents.CommandOutcome) -> Response:
    """Report the outcome the command surface returned, never the intention."""

    if outcome.accepted:
        return _redirect(url, ok=outcome.summary)
    reason = outcome.refusal_reason
    return _redirect(url, err=f"{outcome.message}{(' ' + reason) if reason else ''}")


def _not_found(request: Request, db: Session, message: str) -> HTMLResponse:
    return _render(
        request,
        db,
        "admin/not_found.html",
        {"message": message, "active_nav": ""},
        status_code=404,
    )


def _parse_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _agent_from(value: str | None) -> AgentIdentifier | None:
    if not value:
        return None
    try:
        return AgentIdentifier(value.strip().lower())
    except ValueError:
        return None


def _page_number(request: Request) -> int:
    try:
        return max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        return 1


# --- Overview ----------------------------------------------------------------


@router.get("/admin", response_class=HTMLResponse)
def admin_overview_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    view = _reader(db).overview()
    return _render(
        request,
        db,
        "admin/overview.html",
        {"view": view, "active_nav": "overview", "page_title": "Overview", "live_seconds": 5},
    )


# --- Campaigns ---------------------------------------------------------------


@router.get("/admin/campaigns", response_class=HTMLResponse)
def admin_campaigns_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    status = request.query_params.get("status") or None
    health = request.query_params.get("health") or None
    view = _reader(db).campaigns_index(status=status, health=health)
    return _render(
        request,
        db,
        "admin/campaigns.html",
        {"view": view, "active_nav": "campaigns", "page_title": "Campaigns"},
    )


@router.get("/admin/campaigns/{campaign_id}", response_class=HTMLResponse)
def admin_campaign_detail_page(
    request: Request, campaign_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    parsed = _parse_uuid(campaign_id)
    if parsed is None:
        return _not_found(request, db, "That Campaign does not exist.")
    page = _page_number(request)
    view = _reader(db).campaign_detail(
        parsed,
        stage=_agent_from(request.query_params.get("stage")),
        status=request.query_params.get("status") or None,
        attention=request.query_params.get("attention") == "1",
        limit=PAGE_SIZE,
        offset=(page - 1) * PAGE_SIZE,
    )
    if view is None:
        return _not_found(request, db, "That Campaign does not exist.")
    pages = max(1, -(-view.contact_total // PAGE_SIZE))
    return _render(
        request,
        db,
        "admin/campaign_detail.html",
        {
            "view": view,
            "active_nav": "campaigns",
            "page_title": view.name,
            "page": page,
            "pages": pages,
            "pipeline_order": PIPELINE_ORDER,
            "agent_specs": AGENT_SPECS,
        },
    )


# --- Contact diagnosis -------------------------------------------------------


@router.get(
    "/admin/campaigns/{campaign_id}/contacts/{campaign_contact_id}",
    response_class=HTMLResponse,
)
def admin_contact_diagnosis_page(
    request: Request,
    campaign_id: str,
    campaign_contact_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    campaign_uuid = _parse_uuid(campaign_id)
    membership_uuid = _parse_uuid(campaign_contact_id)
    if campaign_uuid is None or membership_uuid is None:
        return _not_found(request, db, "That Campaign Contact does not exist.")
    reader = _reader(db)
    view = reader.contact_diagnosis(campaign_uuid, membership_uuid)
    if view is None:
        return _not_found(request, db, "That Campaign Contact does not exist.")
    research_report = (
        reader.research_lineage(membership_uuid) if view.research_lineage_available else None
    )
    return _render(
        request,
        db,
        "admin/contact_diagnosis.html",
        {
            "view": view,
            "execution": view.execution,
            "research_report": research_report,
            "active_nav": "campaigns",
            "page_title": view.execution.contact_label,
        },
    )


@router.post("/admin/campaigns/{campaign_id}/contacts/{campaign_contact_id}/actions/{command}")
async def admin_contact_action(
    request: Request,
    campaign_id: str,
    campaign_contact_id: str,
    command: str,
    db: Session = Depends(get_db),
) -> Response:
    """Pause, resume, retry, or skip the current stage for one Campaign Contact.

    The four commands share the authoritative Phase 2 command surface with the
    legacy monitor; nothing here implements a second retry or control system.
    """

    back = f"/admin/campaigns/{campaign_id}/contacts/{campaign_contact_id}"
    if not _actions_available():
        return _redirect(back, err="Corrective actions require the agent_workbench feature switch.")
    membership_uuid = _parse_uuid(campaign_contact_id)
    if membership_uuid is None or _parse_uuid(campaign_id) is None:
        return _redirect("/admin/campaigns", err="That Campaign Contact does not exist.")
    form = await request.form()
    reason = str(form.get("reason", "")).strip()
    commands = _commands(db)
    try:
        if command == "pause":
            outcome = commands.pause_contact(membership_uuid, reason=reason or "paused by operator")
        elif command == "resume":
            outcome = commands.resume_contact(membership_uuid)
        elif command == "retry":
            outcome = commands.retry_contact(
                membership_uuid, reason=reason or "operator requested retry"
            )
        elif command == "skip-stage":
            if not reason:
                return _redirect(back, err="A reason is required to skip a stage. Nothing changed.")
            outcome = commands.skip_stage(membership_uuid, reason=reason)
        else:
            return _redirect(back, err="That command is not available.")
    except workbench_agents.WorkbenchCommandError as exc:
        db.rollback()
        return _redirect(back, err=str(exc))
    db.commit()
    return _command_redirect(back, outcome)


# --- Agent Jobs --------------------------------------------------------------


@router.get("/admin/jobs/{job_id}", response_class=HTMLResponse)
def admin_job_detail_page(
    request: Request, job_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    parsed = _parse_uuid(job_id)
    if parsed is None:
        return _not_found(request, db, "That Agent Job does not exist.")
    view = _reader(db).job(parsed)
    if view is None:
        return _not_found(request, db, "That Agent Job does not exist.")
    return _render(
        request,
        db,
        "admin/job_detail.html",
        {
            "job": view,
            "active_nav": "system",
            "page_title": f"Agent Job · {view.agent_name}",
        },
    )


@router.post("/admin/jobs/{job_id}/retry")
async def admin_job_retry(request: Request, job_id: str, db: Session = Depends(get_db)) -> Response:
    back = f"/admin/jobs/{job_id}"
    if not _actions_available():
        return _redirect(back, err="Corrective actions require the agent_workbench feature switch.")
    parsed = _parse_uuid(job_id)
    if parsed is None:
        return _redirect("/admin/failures", err="That is not a valid Agent Job id.")
    form = await request.form()
    reason = str(form.get("reason", "")).strip() or None
    redirect_to = str(form.get("back", "")).strip() or back
    try:
        outcome = _commands(db).retry_job(parsed, reason=reason)
    except workbench_agents.WorkbenchCommandError as exc:
        db.rollback()
        return _redirect(back, err=str(exc))
    db.commit()
    return _command_redirect(redirect_to, outcome)


# --- Failures ----------------------------------------------------------------


@router.get("/admin/failures", response_class=HTMLResponse)
def admin_failures_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    view = _reader(db).failures(
        category=request.query_params.get("category") or None,
        campaign_id=_parse_uuid(request.query_params.get("campaign")),
        agent=_agent_from(request.query_params.get("agent")),
    )
    return _render(
        request,
        db,
        "admin/failures.html",
        {
            "view": view,
            "categories": FAILURE_CATEGORIES,
            "active_nav": "failures",
            "page_title": "Failures",
            "live_seconds": 5,
        },
    )


# --- Agent/Stages ------------------------------------------------------------


@router.get("/admin/stages", response_class=HTMLResponse)
def admin_stages_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    rows = _reader(db).stages_index()
    return _render(
        request,
        db,
        "admin/stages.html",
        {"rows": rows, "active_nav": "stages", "page_title": "Agent/Stages"},
    )


@router.get("/admin/stages/{agent_id}", response_class=HTMLResponse)
def admin_stage_detail_page(
    request: Request, agent_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    agent = _agent_from(agent_id)
    if agent is None:
        return _not_found(request, db, "That Agent/Stage does not exist.")
    view = _reader(db).stage_detail(agent)
    if view is None:
        return _not_found(request, db, "That Agent/Stage does not exist.")
    return _render(
        request,
        db,
        "admin/stage_detail.html",
        {"view": view, "active_nav": "stages", "page_title": view.row.display_name},
    )


# --- Contacts ----------------------------------------------------------------


@router.get("/admin/contacts", response_class=HTMLResponse)
def admin_contacts_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    view = _reader(db).contacts_index(
        query=request.query_params.get("q") or None,
        campaign_id=_parse_uuid(request.query_params.get("campaign")),
        page=_page_number(request),
    )
    return _render(
        request,
        db,
        "admin/contacts.html",
        {"view": view, "active_nav": "contacts", "page_title": "Contacts"},
    )


@router.get("/admin/contacts/{contact_id}", response_class=HTMLResponse)
def admin_contact_page(
    request: Request, contact_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    parsed = _parse_uuid(contact_id)
    if parsed is None:
        return _not_found(request, db, "That Contact does not exist.")
    view = _reader(db).contact(parsed)
    if view is None:
        return _not_found(request, db, "That Contact does not exist.")
    return _render(
        request,
        db,
        "admin/contact_detail.html",
        {"view": view, "active_nav": "contacts", "page_title": view.name},
    )


# --- Companies ---------------------------------------------------------------


@router.get("/admin/companies", response_class=HTMLResponse)
def admin_companies_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    view = _reader(db).companies_index(
        query=request.query_params.get("q") or None, page=_page_number(request)
    )
    return _render(
        request,
        db,
        "admin/companies.html",
        {"view": view, "active_nav": "companies", "page_title": "Companies"},
    )


@router.get("/admin/companies/{company_id}", response_class=HTMLResponse)
def admin_company_page(
    request: Request, company_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    parsed = _parse_uuid(company_id)
    if parsed is None:
        return _not_found(request, db, "That Company does not exist.")
    view = _reader(db).company(parsed)
    if view is None:
        return _not_found(request, db, "That Company does not exist.")
    return _render(
        request,
        db,
        "admin/company_detail.html",
        {"view": view, "active_nav": "companies", "page_title": view.name},
    )


# --- Review ------------------------------------------------------------------


@router.get("/admin/review", response_class=HTMLResponse)
def admin_review_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    requested = request.query_params.get("view", "awaiting")
    view = _reader(db).review(view=requested)
    return _render(
        request,
        db,
        "admin/review.html",
        {"view": view, "active_nav": "review", "page_title": "Review"},
    )


# --- Providers & Usage -------------------------------------------------------


@router.get("/admin/providers", response_class=HTMLResponse)
def admin_providers_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    view = _reader(db).providers()
    return _render(
        request,
        db,
        "admin/providers.html",
        {"view": view, "active_nav": "providers", "page_title": "Providers & Usage"},
    )


# --- Configuration -----------------------------------------------------------


@router.get("/admin/configuration", response_class=HTMLResponse)
def admin_configuration_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    view = _reader(db).configuration()
    return _render(
        request,
        db,
        "admin/configuration.html",
        {"view": view, "active_nav": "configuration", "page_title": "Configuration"},
    )


# --- System ------------------------------------------------------------------


@router.get("/admin/system", response_class=HTMLResponse)
def admin_system_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    view = _reader(db).system(job_query=request.query_params.get("job") or None)
    return _render(
        request,
        db,
        "admin/system.html",
        {"view": view, "active_nav": "system", "page_title": "System"},
    )


# --- Advanced Diagnostics ----------------------------------------------------


@router.get("/admin/diagnostics", response_class=HTMLResponse)
def admin_diagnostics_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    features = get_settings().features
    agent_on = features.agent_workbench

    def _link(
        title: str, href: str, description: str, *, available: bool = True, note: str | None = None
    ) -> DiagnosticLink:
        return DiagnosticLink(
            title=title, href=href, description=description, available=available, note=note
        )

    groups: tuple[tuple[str, tuple[DiagnosticLink, ...]], ...] = (
        (
            "Agent Studio",
            (
                _link(
                    "Agent Studio",
                    "/admin/agents/studio",
                    "Per-Agent capability cards, configuration boundaries and previews.",
                    available=agent_on,
                    note=None if agent_on else "Requires the agent_workbench feature switch.",
                ),
                _link(
                    "Research Agent report",
                    "/admin/agents/studio/research",
                    "Raw durable Research execution reports, including fallback lineage.",
                    available=agent_on,
                ),
                _link(
                    "Verification Agent studio",
                    "/admin/agents/studio/verification",
                    "Provider credentials, waterfall policies and live provider tests.",
                    available=agent_on,
                ),
                _link(
                    "Personalization policy studio",
                    "/admin/agents/studio/personalization",
                    "Immutable policy versions, activation history and bounded previews.",
                    available=agent_on,
                ),
            ),
        ),
        (
            "Legacy monitor",
            (
                _link(
                    "Workbench monitor",
                    "/workbench",
                    "The original Agent monitor this Workbench supersedes.",
                    available=agent_on,
                ),
                _link(
                    "Agent Job list (legacy)",
                    "/workbench/jobs",
                    "Raw Agent Job queue with public-status filters.",
                    available=agent_on,
                ),
                _link(
                    "Legacy import overview",
                    "/admin/legacy/overview",
                    "The original import-centric admin overview page.",
                ),
            ),
        ),
        (
            "Data workflows",
            (
                _link(
                    "Imports",
                    "/imports",
                    "Spreadsheet staging: upload, map, enrich, preview, confirm.",
                ),
                _link(
                    "Identity review",
                    "/review",
                    "Ambiguous import rows awaiting an identity decision.",
                ),
                _link(
                    "Pending captures",
                    "/contact-captures/pending",
                    "Captured profiles awaiting company resolution or promotion.",
                ),
                _link(
                    "Email verification console",
                    "/verification",
                    "Verification queue, evidence and provider operations.",
                ),
                _link(
                    "Company Intelligence",
                    "/admin/company-intelligence",
                    "Model-produced company classifications awaiting operator decisions.",
                    available=features.company_intelligence,
                ),
                _link(
                    "Seller Knowledge Base",
                    "/knowledge-base",
                    "Offerings, proof points, restricted claims and personas.",
                    available=features.seller_knowledge_base,
                ),
                _link(
                    "Local tools",
                    "/local-tools",
                    "Local-environment fixtures and resets (typed confirmation).",
                ),
            ),
        ),
    )
    return _render(
        request,
        db,
        "admin/diagnostics.html",
        {
            "view": DiagnosticsView(groups=groups),
            "active_nav": "diagnostics",
            "page_title": "Advanced Diagnostics",
        },
    )

"""Customer-facing page routes for the v2 interface.

Thin adapters, exactly like the admin routes: every rule lives in the service
layer. What is different is the audience. The admin Workbench answers "what is the
machine doing"; these pages answer "what should I do next, and why does the system
believe what it believes". So the same services are read, but projected around the
operator's decisions rather than around the queue.

Three rules held throughout:

* **Nothing is invented.** Where the design shows a figure this product does not
  produce — a send, a reply, a bounce, a confidence score, an auto-send threshold —
  the page renders the design's slot with an explicit "not built yet" marker. The
  layout survives; the claim is never made.
* **Future screens are shape only.** Sending, replies, sequences and analytics
  render the design's structure with no data and no action.
* **The admin Workbench is untouched.** This router shares services and models with
  it and no code, no templates and no stylesheet.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import Settings, get_settings
from app.models.campaign import CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    CampaignStatus,
    DossierSection,
    PipelineStageStatus,
    ResearchState,
)
from app.models.pipeline import CampaignContactAgentState
from app.models.suppression import Suppression
from app.services import campaigns as campaign_service
from app.services import drafts as draft_service
from app.services import workbench_agents
from app.services.agents import rerun as agent_rerun
from app.services.agents.registry import AGENT_SPECS, PIPELINE_ORDER
from app.services.campaigns import CampaignError
from app.services.captures import labels as capture_labels
from app.services.captures import promotion as capture_promotion
from app.services.companies import detail as company_detail
from app.services.companies import records as company_records
from app.services.crm import detail as crm_detail
from app.services.crm import records as crm_records
from app.services.imports import apollo, campaign_import, staging
from app.services.resolution import service as resolution_service
from app.services.seller import campaign_offerings as seller_campaign_offerings
from app.services.seller import profile as seller_profile
from app.services.seller import readiness as seller_readiness
from app.services.seller import records as seller_records
from app.services.verification import console as verification_console
from app.services.workbench_agents import views as agent_views
from app.web.v2 import context as shell

router = APIRouter(prefix="/app", include_in_schema=False)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

PAGE_SIZE = 25
#: How many planned rows the import preview renders. The preview's job is to make
#: the file's *shape* legible, not to be a spreadsheet viewer; the counts above it
#: are computed over every row, and the full row-by-row result is on the batch
#: page after confirmation.
PREVIEW_ROWS_SHOWN = 50
#: The campaign screen is a monitor: its whole purpose is a queue that is moving.
#: Same mechanism and same interval as the admin monitor pages.
LIVE_REFRESH_SECONDS = 5

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

#: Future-feature surfaces. The design shows them; the backend has nothing behind
#: them, so they render as shape with no data.
SOON_SECTIONS: dict[str, dict[str, Any]] = {
    "sending": {
        "title": "Sending accounts",
        "nav": "sending",
        "lede": (
            "Connecting a mailbox, a daily cap, a send window and the delivery record for "
            "every message will live here. None of it is built: the Sending Agent has no "
            "adapter registered, so it cannot be enabled and this product cannot send an "
            "email. Approving a draft in Review records your decision and stops there."
        ),
        "tiles": [
            ("Mailboxes connected", "No mailbox can be connected yet."),
            ("Sent today", "Nothing has ever been sent from here."),
            ("Daily cap", "There is no sending to cap."),
            ("Send window", "There is no schedule to keep."),
        ],
        "note": (
            "Until this exists, an approved draft is a decision on record and nothing "
            "more. That is deliberate — a queue that looks like it is sending and is not "
            "would be the most expensive kind of wrong."
        ),
    },
    "replies": {
        "title": "Replies",
        "nav": "replies",
        "lede": (
            "Replies, bounces and the rule that pulls someone out of a campaign the moment "
            "they answer will live here. Nothing has been sent, so there is nothing to "
            "reply to, and no inbound channel is connected."
        ),
        "tiles": [
            ("Replies", "No inbound channel is connected."),
            ("Bounces", "Nothing has been sent."),
            ("Pulled from campaigns", "No reply rule runs yet."),
        ],
        "note": (
            "The suppression ledger already exists and already wins over everything else, "
            "so when replies arrive they will have somewhere authoritative to land."
        ),
    },
    "sequences": {
        "title": "Sequences",
        "nav": "sequences",
        "lede": (
            "A follow-up after a first message, and the rules for when to stop, will live "
            "here. There is no first message yet, so there is no sequence."
        ),
        "tiles": [
            ("Steps", "No sequence engine exists."),
            ("In flight", "Nothing is in flight."),
        ],
        "note": "",
    },
    "analytics": {
        "title": "Analytics",
        "nav": "analytics",
        "lede": (
            "Open rates, reply rates and per-offering performance will live here. Every one "
            "of those measures something that has been sent, and nothing has been sent."
        ),
        "tiles": [
            ("Reply rate", "Requires sending."),
            ("Open rate", "Requires sending."),
            ("Best offering", "Requires replies."),
        ],
        "note": (
            "What can be measured today — how far contacts get through the pipeline, what "
            "is holding them, and what each Agent actually did — is on Campaigns and on "
            "Agent settings."
        ),
    },
}

KB_SECTIONS: tuple[tuple[str, str], ...] = (
    ("overview", "Overview"),
    ("company", "Company"),
    ("offerings", "Offerings"),
    ("proof-points", "Proof Points"),
    ("restricted-claims", "Restricted Claims"),
    ("personas", "Personas"),
)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _fmt_dt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%d %b %Y %H:%M")
    return str(value)


def _fmt_time(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%H:%M")
    return "—"


def _fmt_day(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%d %b")
    return "—"


def _ago(value: Any) -> str:
    """A relative age, in the design's compact vocabulary (4m, 3h, 2d)."""

    if not isinstance(value, datetime):
        return "—"
    moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    seconds = max(0, int((datetime.now(UTC) - moment).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _thousands(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def _titlecase(value: Any) -> str:
    return str(value).replace("_", " ").replace("-", " ").strip().capitalize()


def _initials(value: Any) -> str:
    """Up to two initials for a record avatar.

    Word initials where there is more than one word ("Nordic Med" -> NM), the first
    two letters otherwise ("Northwind" -> NO reads as a country code, "N" does not).
    """

    words = [word for word in str(value).replace("-", " ").split() if word]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    if words:
        return words[0][:2].capitalize()
    return "—"


def _plural(count: Any, singular: str, plural: str | None = None) -> str:
    """ "1 company" / "3 companies" — a count and its noun agreeing."""

    try:
        number = int(count)
    except (TypeError, ValueError):
        number = 0
    word = singular if number == 1 else (plural or f"{singular}s")
    return f"{number:,} {word}"


def _neutralize(value: Any) -> str:
    """Render a spreadsheet cell so it can never travel onward as a formula.

    Jinja's autoescaping already makes an operator's file safe as *HTML*. This
    handles the other direction: a value beginning with ``=``, ``+``, ``-`` or
    ``@`` copied off this page into a spreadsheet would be evaluated there, so it
    is prefixed on the way out. Nothing is ever evaluated here — the workbook
    reader is opened with cached values only — and the original text is preserved
    verbatim in the immutable raw row.
    """

    if value is None:
        return ""
    return apollo.neutralize_formula(str(value)) or ""


templates.env.filters["neutralize"] = _neutralize
templates.env.filters["dt"] = _fmt_dt
templates.env.filters["clock"] = _fmt_time
templates.env.filters["day"] = _fmt_day
templates.env.filters["ago"] = _ago
templates.env.filters["thousands"] = _thousands
templates.env.filters["nice"] = _titlecase
templates.env.filters["initials"] = _initials
templates.env.globals["plural"] = _plural


def _database_ok(db: Session) -> bool:
    try:
        db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _render(
    request: Request,
    db: Session,
    template: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    settings = get_settings()
    counts = shell.attention_counts(db)
    name, email, initials = shell.operator_identity(db, settings)
    shared: dict[str, Any] = {
        "app_env": settings.app_env,
        "features_enabled": settings.features.enabled(),
        "database_ok": _database_ok(db),
        "attention": counts,
        "nav_groups": shell.nav_groups(counts),
        "operator_name": name,
        "operator_email": email,
        "operator_initials": initials,
        "capture_ready": "contact_capture_intake" in settings.features.enabled(),
        "flash_ok": request.query_params.get("ok"),
        "flash_err": request.query_params.get("err"),
    }
    shared.update(context)
    return templates.TemplateResponse(
        request=request, name=template, context=shared, status_code=status_code
    )


def _redirect(url: str, *, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    """Redirect with a flash message, appended correctly.

    Several of these targets already carry a query string — the Review screen sends
    the operator back to the exact draft and filter they were on — so the separator
    has to be chosen rather than assumed. Appending a second ``?`` produced a URL
    where the flash became part of the previous parameter's value and never showed.
    """

    params = {key: value for key, value in (("ok", ok), ("err", err)) if value}
    if not params:
        return RedirectResponse(url, status_code=303)
    separator = "&" if "?" in url else "?"
    return RedirectResponse(f"{url}{separator}{urlencode(params)}", status_code=303)


def _not_found(request: Request, db: Session, message: str) -> HTMLResponse:
    return _render(
        request,
        db,
        "not_found.html",
        {"message": message, "active_nav": "", "page_title": "Not found"},
        status_code=404,
    )


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


def _sheet_index(value: str | None) -> int | None:
    """Read a worksheet selection from a form field, or None.

    One parse, no separate guard. The guard and the parse used to be different
    predicates — ``lstrip("-").isdigit()`` accepts ``"--5"`` because it strips
    every leading dash, and ``str.isdigit()`` accepts superscript digits, both of
    which then raised ``ValueError`` inside a handler that catches only
    ``CampaignImportError``. Two ordinary form values returned a bare 500.

    A negative index is returned as-is rather than rejected: the caller matches
    it against the sheets that exist and gets a clean structure error, which is a
    better message than anything invented here.
    """

    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def _pages(total: int, size: int = PAGE_SIZE) -> int:
    return max(1, (total + size - 1) // size)


def _reader(db: Session) -> workbench_agents.PhaseTwoWorkbenchReader:
    return workbench_agents.PhaseTwoWorkbenchReader(db)


def _agent_workbench_on(settings: Settings) -> bool:
    return "agent_workbench" in settings.features.enabled()


def _kb_on(settings: Settings) -> bool:
    return "seller_knowledge_base" in settings.features.enabled()


# ---------------------------------------------------------------------------
# Projections shared by more than one screen
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StageTile:
    """One Agent in the pipeline strip.

    Two different quantities, kept apart because conflating them is what made this
    row unreadable:

    * ``through`` — how many contacts have *got past* this Agent. This is the big
      number, and it is what makes the row a funnel: it only ever descends, and it
      answers "did my 50 arrive, and how far did they get".
    * ``resting`` / ``moving`` / ``held`` — where contacts are *right now*. These are
      the live detail underneath.

    The strip originally showed only ``resting``. That reads as a throughput counter
    and is not one: Capture completes the moment a contact is enrolled, and Identity
    and Company finish in under a second, so all three permanently showed 0 while 50
    contacts sat failing at Research. The operator's reasonable conclusion — that
    nothing had been captured and nothing had passed through — was wrong, and the
    screen was the reason.
    """

    agent_id: str
    index: int
    label: str
    suffix: str
    phase: str
    #: Completed plus skipped: a skipped stage was passed, not failed.
    through: int
    completed: int
    skipped: int
    resting: int
    moving: int
    held: int
    blurb: str
    href: str
    selected: bool
    control_status: str
    implemented: bool

    @property
    def waiting(self) -> int:
        """Resting here, but neither in flight nor held.

        Derived rather than queried because that is what it is: the remainder of
        ``resting`` once the two states that have their own note are taken out. Shown
        independently of them — an early version only showed it when nothing was
        moving or held, so a stage with five contacts waiting and one failure reported
        the one failure and hid the five.
        """

        return max(0, self.resting - self.moving - self.held)

    @property
    def quiet(self) -> bool:
        """Nothing to report: nobody here now, and nothing was skipped."""

        return not (self.moving or self.held or self.resting or self.skipped)


def _stage_tiles(
    execution: agent_views.CampaignExecutionView,
    *,
    selected: AgentIdentifier | None,
    base_href: str,
    open_counts: dict[str, tuple[int, int]],
    progress: dict[str, tuple[int, int]],
) -> tuple[StageTile, ...]:
    """The nine-stage strip. Every number is a Phase 2 count, none derived."""

    controls = {control.agent_id: control for control in execution.controls}
    tiles: list[StageTile] = []
    for position, agent_id in enumerate(PIPELINE_ORDER, start=1):
        spec = AGENT_SPECS[agent_id]
        control = controls.get(agent_id)
        completed, skipped = progress.get(agent_id.value, (0, 0))
        moving, held = open_counts.get(agent_id.value, (0, 0))
        tiles.append(
            StageTile(
                agent_id=agent_id.value,
                index=position,
                label=spec.display_name.replace(" Agent", ""),
                suffix="Agent",
                phase=PHASES[agent_id],
                through=completed + skipped,
                completed=completed,
                skipped=skipped,
                resting=int(execution.stage_counts.get(agent_id.value, 0)),
                moving=moving,
                held=held,
                blurb=AGENT_BLURBS[agent_id],
                href=f"{base_href}?stage={agent_id.value}",
                selected=selected is agent_id,
                control_status=(control.status.value if control else spec.default_status.value),
                implemented=spec.implemented,
            )
        )
    return tuple(tiles)


def _stage_progress(db: Session, campaign_id: uuid.UUID) -> dict[str, tuple[int, int]]:
    """Per Agent, how many contacts completed it and how many skipped it.

    Read from the durable per-stage ledger (`campaign_contact_agent_states`), which
    is the only place that remembers a stage a contact has already left. The
    membership's ``current_stage`` cannot answer this — it says where someone is now,
    not where they have been.

    Completed and skipped are counted separately and only summed for the headline: a
    skipped stage was passed (an Agent that is off and skippable auto-skips), but an
    operator reading "50 through Research" deserves to know if 50 of those were
    skips.
    """

    rows = db.execute(
        select(
            CampaignContactAgentState.agent_id,
            CampaignContactAgentState.status,
            func.count(CampaignContactAgentState.id),
        )
        .join(
            CampaignContact,
            CampaignContact.id == CampaignContactAgentState.campaign_contact_id,
        )
        .where(CampaignContact.campaign_id == campaign_id)
        .group_by(CampaignContactAgentState.agent_id, CampaignContactAgentState.status)
    ).all()
    built: dict[str, tuple[int, int]] = {}
    for agent_id, status, count in rows:
        completed, skipped = built.get(agent_id.value, (0, 0))
        if status is PipelineStageStatus.COMPLETED:
            completed += int(count)
        elif status is PipelineStageStatus.SKIPPED:
            skipped += int(count)
        built[agent_id.value] = (completed, skipped)
    return built


def _agent_open_counts(db: Session, campaign_id: uuid.UUID) -> dict[str, tuple[int, int]]:
    """Per Agent, how many contacts are in flight and how many are held.

    One grouped query answers it for all nine stages. Computed per request and
    handed to the strip rather than cached on the module: a cached count would go
    stale on a page whose entire purpose is being current.
    """

    rows = db.execute(
        select(
            CampaignContact.current_stage,
            CampaignContact.pipeline_status,
            func.count(CampaignContact.id),
        )
        .where(CampaignContact.campaign_id == campaign_id)
        .group_by(CampaignContact.current_stage, CampaignContact.pipeline_status)
    ).all()
    built: dict[str, tuple[int, int]] = {}
    for stage, status, count in rows:
        if stage is None:
            continue
        moving, stuck = built.get(stage.value, (0, 0))
        if status in (PipelineStageStatus.RUNNING, PipelineStageStatus.RETRYING):
            moving += int(count)
        elif status in (
            PipelineStageStatus.FAILED,
            PipelineStageStatus.BLOCKED,
            PipelineStageStatus.PAUSED,
        ):
            stuck += int(count)
        built[stage.value] = (moving, stuck)
    return built


@dataclass(frozen=True)
class DecisionGroup:
    """One thing only a person can settle."""

    count: int
    title: str
    detail: str
    primary_label: str
    primary_href: str
    secondary_label: str | None = None
    secondary_href: str | None = None


def _decision_groups(
    db: Session, counts: shell.AttentionCounts, *, campaign_id: uuid.UUID | None
) -> tuple[DecisionGroup, ...]:
    """The design's "Needs a decision from you" panel, from real backlogs."""

    groups: list[DecisionGroup] = []
    scope = f"?campaign={campaign_id}" if campaign_id else ""
    if counts.drafts_awaiting:
        groups.append(
            DecisionGroup(
                count=counts.drafts_awaiting,
                title="Drafts waiting for your read",
                detail=(
                    "Written, checked against your Knowledge Base limits, and held. Nothing "
                    "is sent on its own — every draft waits for you."
                ),
                primary_label="Start reviewing",
                primary_href=f"/app/review{scope}",
            )
        )
    if counts.blocked_contacts:
        groups.append(
            DecisionGroup(
                count=counts.blocked_contacts,
                title="Contacts the pipeline will not carry",
                detail=(
                    "Each carries a recorded blocking reason — suppression, a missing "
                    "company, or an identity nothing could resolve. They are held rather "
                    "than guessed at."
                ),
                primary_label="See what is holding them",
                primary_href=(
                    f"/app/campaigns/{campaign_id}?eligibility=blocked"
                    if campaign_id
                    else "/app/contacts?view=all"
                ),
            )
        )
    if counts.failed_stages:
        groups.append(
            DecisionGroup(
                count=counts.failed_stages,
                title="Stages that stopped",
                detail=(
                    "A stage that failed terminally will fail the same way on a retry, so "
                    "nothing retries it automatically. The cause is recorded on each one."
                ),
                primary_label="Open the pipeline",
                primary_href=(f"/app/campaigns/{campaign_id}" if campaign_id else "/app/campaigns"),
            )
        )
    if counts.unresolved_domains:
        groups.append(
            DecisionGroup(
                count=counts.unresolved_domains,
                title="No website could be found",
                detail=(
                    "Every candidate domain was rejected with a reason. Without a website "
                    "there is no format to build an address from, so these people cannot be "
                    "emailed at all until one is entered."
                ),
                primary_label="Resolve them",
                primary_href="/contact-captures/pending",
                secondary_label="See the companies",
                secondary_href="/app/companies?view=unresolved_domain",
            )
        )
    if counts.ambiguous_imports:
        groups.append(
            DecisionGroup(
                count=counts.ambiguous_imports,
                title="Two people could be the same person",
                detail=(
                    "Nothing was merged. Merging the wrong two records is not reversible by "
                    "a retry, so it is always yours to confirm."
                ),
                primary_label="Review the matches",
                primary_href="/review",
            )
        )
    return tuple(groups)


def _activity_lines(events: Sequence[agent_views.ActivityView]) -> list[dict[str, Any]]:
    """Pipeline events as the design's "What changed" feed.

    The sentence is assembled from the event's own committed fields — who, which
    Agent, which transition, and the reason code it carried. Nothing is narrated
    that the event does not say.
    """

    lines: list[dict[str, Any]] = []
    for event in events:
        agent = AGENT_SPECS[event.agent_id].display_name if event.agent_id else "Pipeline"
        who = event.contact_label or "A contact"
        verb = event.event_type.value.replace("_", " ")
        text_parts = [f"{who} — {verb}"]
        if event.to_status is not None:
            text_parts.append(f"now {event.to_status.value}")
        meta_parts = [agent]
        if event.reason_code:
            meta_parts.append(event.reason_code.replace("_", " "))
        if event.reason_detail:
            meta_parts.append(event.reason_detail)
        if event.actor:
            meta_parts.append(f"by {event.actor}")
        lines.append(
            {
                "time": event.occurred_at,
                "text": " · ".join(text_parts),
                "meta": " · ".join(meta_parts),
                "is_failure": event.is_failure,
                "campaign_contact_id": event.campaign_contact_id,
                "campaign_id": event.campaign_id,
            }
        )
    return lines


# ---------------------------------------------------------------------------
# Today
# ---------------------------------------------------------------------------


@router.get("")
@router.get("/")
def today_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """What happened, and what wants you.

    The design's stat strip counts overnight sends, replies and bounces. None of
    those exist, so the strip carries what the pipeline genuinely did — contacts
    captured, mailboxes confirmed, drafts written — and marks the sending measures
    unavailable rather than filling them in.
    """

    counts = shell.attention_counts(db)
    overviews = campaign_service.list_campaigns(db)
    draft_counts = draft_service.queue_counts(db)
    settings = get_settings()

    reader = _reader(db) if _agent_workbench_on(settings) else None
    queue = reader.overview().queue if reader is not None else None

    contacts_total = db.scalar(select(func.count(Contact.id))) or 0
    companies_total = db.scalar(select(func.count(Company.id))) or 0
    confirmed = db.scalar(select(func.count(Contact.id)).where(Contact.email.is_not(None))) or 0

    campaign_rows: list[dict[str, Any]] = []
    for overview in overviews:
        campaign = overview.campaign
        campaign_counts = shell.attention_counts(db, campaign_id=campaign.id)
        campaign_rows.append(
            {
                "campaign": campaign,
                "contacts": overview.contact_count,
                "drafts_awaiting": campaign_counts.drafts_awaiting,
                "blocked": campaign_counts.blocked_contacts,
                "failed": campaign_counts.failed_stages,
                "pipeline_counts": overview.pipeline_counts,
                "needs": _campaign_needs_sentence(campaign_counts, overview),
            }
        )

    cards: list[dict[str, Any]] = []
    if draft_counts.awaiting:
        cards.append(
            {
                "tone": "",
                "count": draft_counts.awaiting,
                "title": "Drafts waiting for your read",
                "detail": (
                    "Each one was written inside your Knowledge Base limits and held. There "
                    "is no auto-send in this product: a draft goes nowhere until you "
                    "approve it, and approving it records your decision."
                ),
                "cta": "Start reviewing",
                "href": "/app/review",
            }
        )
    decisions_now = counts.total - counts.drafts_awaiting
    if decisions_now:
        cards.append(
            {
                "tone": "warn",
                "count": decisions_now,
                "title": "Decisions only you can make",
                "detail": (
                    "Blocked contacts, stopped stages, companies with no website and "
                    "identities that matched two people. Everything the system could settle "
                    "safely, it already did."
                ),
                "cta": "See them",
                "href": "/app/campaigns",
            }
        )
    if not cards:
        cards.append(
            {
                "tone": "quiet",
                "count": 0,
                "title": "Nothing is waiting on you",
                "detail": (
                    "No draft is held for a read and no contact is blocked. Capture more "
                    "people, or let the Agents work through what is already enrolled."
                ),
                "cta": "Open capture",
                "href": "/app/capture",
            }
        )

    return _render(
        request,
        db,
        "today.html",
        {
            "active_nav": "today",
            "page_title": "Today",
            "today": datetime.now(UTC),
            "cards": cards,
            "campaign_rows": campaign_rows,
            "draft_counts": draft_counts,
            "contacts_total": contacts_total,
            "companies_total": companies_total,
            "confirmed_addresses": confirmed,
            "queue": queue,
            "agent_workbench_on": _agent_workbench_on(settings),
        },
    )


def _campaign_needs_sentence(
    counts: shell.AttentionCounts, overview: campaign_service.CampaignOverview
) -> str:
    parts: list[str] = []
    if counts.drafts_awaiting:
        parts.append(f"{_plural(counts.drafts_awaiting, 'draft')} waiting for you")
    if counts.blocked_contacts:
        parts.append(f"{_plural(counts.blocked_contacts, 'contact')} held")
    if counts.failed_stages:
        parts.append(f"{_plural(counts.failed_stages, 'stage')} stopped")
    if not parts:
        if not overview.campaign.execution_enabled:
            return "Execution is off, so no Agent will claim work for this campaign."
        if overview.contact_count == 0:
            return "No contacts enrolled yet."
        return "Nothing is waiting on you."
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------


@router.get("/campaigns")
def campaigns_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    overviews = campaign_service.list_campaigns(db)
    rows: list[dict[str, Any]] = []
    for overview in overviews:
        campaign_counts = shell.attention_counts(db, campaign_id=overview.campaign.id)
        rows.append(
            {
                "campaign": overview.campaign,
                "contacts": overview.contact_count,
                "pipeline_counts": overview.pipeline_counts,
                "state_counts": overview.state_counts,
                "counts": campaign_counts,
                "needs": _campaign_needs_sentence(campaign_counts, overview),
            }
        )
    return _render(
        request,
        db,
        "campaigns.html",
        {"active_nav": "campaigns", "page_title": "Campaigns", "rows": rows},
    )


@router.get("/campaigns/new")
def campaign_new_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Create a campaign.

    The design's five-step wizard asks for an audience rule set, sending accounts
    and an auto-send threshold. None of those exist in this product, so the steps
    that do exist are real fields and the rest are shown as the later steps they
    are — visible, so the shape of the finished product is legible, and explicitly
    not yet available.
    """

    settings = get_settings()
    offerings = (
        seller_records.list_offerings(db, include_archived=False) if _kb_on(settings) else []
    )
    return _render(
        request,
        db,
        "campaign_new.html",
        {
            "active_nav": "campaigns",
            "page_title": "New campaign",
            "offerings": offerings,
            "kb_on": _kb_on(settings),
        },
    )


@router.post("/campaigns/new")
def campaign_create(
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    description: str = Form(""),
    offering_id: str = Form(""),
    allow_provisional_domains: str = Form(""),
) -> RedirectResponse:
    try:
        campaign = campaign_service.create_campaign(
            db,
            name=name,
            description=description or None,
            status=CampaignStatus.DRAFT,
            allow_provisional_domains=bool(allow_provisional_domains),
            actor=draft_service.OPERATOR_ACTOR,
        )
    except CampaignError as exc:
        return _redirect("/app/campaigns/new", err=str(exc))

    settings = get_settings()
    chosen = _uuid(offering_id)
    if chosen is not None and _kb_on(settings):
        try:
            seller_campaign_offerings.associate(
                db, campaign=campaign, offering_id=chosen, actor=draft_service.OPERATOR_ACTOR
            )
        except Exception as exc:  # pragma: no cover - surfaced to the operator
            db.commit()
            return _redirect(
                f"/app/campaigns/{campaign.id}",
                err=f"The campaign was created, but the offering was not attached: {exc}",
            )
    db.commit()
    return _redirect(
        f"/app/campaigns/{campaign.id}",
        ok=(
            f"{campaign.name} created as a draft. Enrol contacts from Contacts, then turn "
            "execution on when you want the Agents to start."
        ),
    )


@router.get("/campaigns/{campaign_id}/edit")
def campaign_edit_page(
    campaign_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    identifier = _uuid(campaign_id)
    if identifier is None:
        return _not_found(request, db, "That is not a campaign id.")
    campaign = campaign_service.get_campaign(db, identifier)
    if campaign is None:
        return _not_found(request, db, "That campaign does not exist.")
    return _render(
        request,
        db,
        "campaign_edit.html",
        {"active_nav": "campaigns", "page_title": f"Edit {campaign.name}", "campaign": campaign},
    )


@router.post("/campaigns/{campaign_id}/edit")
def campaign_edit_submit(
    campaign_id: str,
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    description: str = Form(""),
    allow_provisional_domains: str = Form(""),
) -> RedirectResponse:
    identifier = _uuid(campaign_id)
    if identifier is None:
        return _redirect("/app/campaigns", err="That is not a campaign id.")
    try:
        campaign = campaign_service.update_campaign(
            db,
            identifier,
            name=name,
            description=description or None,
            allow_provisional_domains=bool(allow_provisional_domains),
            actor=draft_service.OPERATOR_ACTOR,
            reason="campaign edited",
        )
    except CampaignError as exc:
        return _redirect(f"/app/campaigns/{identifier}/edit", err=str(exc))
    db.commit()
    return _redirect(f"/app/campaigns/{campaign.id}", ok=f"{campaign.name} updated.")


@router.post("/campaigns/{campaign_id}/archive")
def campaign_archive(
    campaign_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Retire a campaign from the list. One-way: an archived campaign cannot reopen.

    There is no hard delete — enrolled contacts, drafts and audit history stay in
    place; archiving only turns execution off for good, the same guard already
    applied to any Campaign reaching ``ARCHIVED``.

    Execution is turned off through :func:`apply_campaign_execution` rather than
    left to ``update_campaign``'s own reconciliation — the same deadlock-safe,
    batched control path the execution toggle uses, so archiving a Campaign with
    many enrolled Contacts cannot deadlock or block a worker's lease the way an
    unbatched reconcile could. ``apply_campaign_execution`` commits on its own;
    the status transition that follows is a second, separate commit.
    """

    identifier = _uuid(campaign_id)
    if identifier is None:
        return _redirect("/app/campaigns", err="That is not a campaign id.")
    try:
        campaign_service.apply_campaign_execution(
            db,
            identifier,
            enabled=False,
            actor=draft_service.OPERATOR_ACTOR,
            reason="archived from the campaigns list",
        )
        campaign = campaign_service.update_campaign(
            db,
            identifier,
            status=CampaignStatus.ARCHIVED,
            actor=draft_service.OPERATOR_ACTOR,
            reason="archived from the campaigns list",
        )
    except CampaignError as exc:
        return _redirect("/app/campaigns", err=str(exc))
    db.commit()
    return _redirect("/app/campaigns", ok=f"{campaign.name} archived.")


@router.get("/campaigns/{campaign_id}")
def campaign_page(
    campaign_id: str,
    request: Request,
    db: Session = Depends(get_db),
    stage: str | None = None,
    page: int = 1,
) -> HTMLResponse:
    """The pipeline screen — the heart of the design.

    Nine stages with the counts resting on each, the contacts held by the stage in
    focus, what needs a decision, and what changed. Every number is a Phase 2 count.
    """

    identifier = _uuid(campaign_id)
    if identifier is None:
        return _not_found(request, db, "That is not a campaign id.")

    settings = get_settings()
    if not _agent_workbench_on(settings):
        overview = campaign_service.get_campaign_overview(db, identifier)
        if overview is None:
            return _not_found(request, db, "That campaign does not exist.")
        return _render(
            request,
            db,
            "campaign_unavailable.html",
            {
                "active_nav": "campaigns",
                "page_title": overview.campaign.name,
                "overview": overview,
            },
        )

    selected: AgentIdentifier | None = None
    if stage:
        try:
            selected = AgentIdentifier(stage)
        except ValueError:
            selected = None

    reader = _reader(db)
    current = max(1, page)
    execution = reader.campaign_execution(
        identifier,
        stage=selected,
        limit=PAGE_SIZE,
        offset=(current - 1) * PAGE_SIZE,
    )
    if execution is None:
        return _not_found(request, db, "That campaign does not exist.")

    tiles = _stage_tiles(
        execution,
        selected=selected,
        base_href=f"/app/campaigns/{identifier}",
        open_counts=_agent_open_counts(db, identifier),
        progress=_stage_progress(db, identifier),
    )
    counts = shell.attention_counts(db, campaign_id=identifier)
    selected_tile = next((tile for tile in tiles if tile.selected), None)

    offerings = (
        seller_campaign_offerings.offerings_for_campaign(db, identifier) if _kb_on(settings) else []
    )
    readiness = None
    if _kb_on(settings):
        campaign = campaign_service.get_campaign(db, identifier)
        if campaign is not None:
            readiness = seller_readiness.campaign_report(db, campaign)

    # Two different questions, so two different numbers. The strip hint asks "is a
    # re-run likely to help here?" across all nine Agents, and answers it from
    # failures, cheaply. The panel asks "what exactly has stopped on the Agent I am
    # looking at?" — and since it needs the list anyway, it reads the list rather than
    # gating on the approximate count. Gating on the count hid a stage whose only
    # stopped contact was blocked, which is precisely the contact worth explaining.
    failures = agent_rerun.failure_counts(db, identifier)
    rerun_candidates: tuple[agent_rerun.RerunCandidate, ...] = ()
    if selected is not None:
        rerun_candidates = agent_rerun.candidates(db, campaign_id=identifier, agent_id=selected)

    return _render(
        request,
        db,
        "campaign.html",
        {
            "active_nav": "campaigns",
            "page_title": execution.name,
            "live_seconds": LIVE_REFRESH_SECONDS,
            "execution": execution,
            "tiles": tiles,
            "selected_tile": selected_tile,
            "selected_stage": selected.value if selected else None,
            "failure_counts": failures,
            "rerun_candidates": rerun_candidates,
            "rerun_spends": (selected in agent_rerun.SPENDS_PER_CONTACT) if selected else False,
            "rerun_ceiling": agent_rerun.MAX_PER_RERUN,
            "decisions": _decision_groups(db, counts, campaign_id=identifier),
            "attention_here": counts,
            "activity": _activity_lines(execution.recent_events),
            "page": current,
            "pages": _pages(execution.contact_total),
            "base_url": (
                f"/app/campaigns/{identifier}?stage={selected.value}"
                if selected
                else f"/app/campaigns/{identifier}"
            ),
            "campaigns": campaign_service.list_campaigns(db),
            "offerings": offerings,
            "readiness": readiness,
            "kb_on": _kb_on(settings),
        },
    )


@router.post("/campaigns/{campaign_id}/execution")
def campaign_execution_toggle(
    campaign_id: str,
    request: Request,
    db: Session = Depends(get_db),
    enabled: str = Form(""),
    reason: str = Form(""),
) -> RedirectResponse:
    """Turn a campaign's execution on or off.

    This is the design's "Pause campaign". It is the existing campaign switch, not a
    new one: with execution off every Agent except Capture is forced to DISABLED, so
    nothing new is claimed and nothing in flight is discarded.
    """

    identifier = _uuid(campaign_id)
    if identifier is None:
        return _redirect("/app/campaigns", err="That is not a campaign id.")
    want = enabled.lower() in {"1", "true", "on", "yes"}
    try:
        campaign_service.apply_campaign_execution(
            db,
            identifier,
            enabled=want,
            actor=draft_service.OPERATOR_ACTOR,
            reason=reason or None,
        )
    except CampaignError as exc:
        return _redirect(f"/app/campaigns/{identifier}", err=str(exc))
    message = (
        "Execution is on. Agents may claim work for this campaign again."
        if want
        else (
            "Execution is off. No Agent will claim new work for this campaign, and nothing "
            "already in flight was discarded."
        )
    )
    return _redirect(f"/app/campaigns/{identifier}", ok=message)


@router.post("/campaigns/{campaign_id}/agents/{agent_id}/rerun")
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

    identifier = _uuid(campaign_id)
    if identifier is None:
        return _redirect("/app/campaigns", err="That is not a campaign id.")
    try:
        target = AgentIdentifier(agent_id)
    except ValueError:
        return _redirect(f"/app/campaigns/{identifier}", err="That is not an Agent.")

    destination = back or f"/app/campaigns/{identifier}?stage={target.value}"
    try:
        outcome = agent_rerun.rerun_stage(
            db,
            campaign_id=identifier,
            agent_id=target,
            actor=draft_service.OPERATOR_ACTOR,
            reason=reason or None,
            campaign_contact_id=_uuid(campaign_contact_id) if campaign_contact_id else None,
        )
    except agent_rerun.RerunError as exc:
        return _redirect(destination, err=str(exc))

    db.commit()
    message = outcome.message()
    if outcome.refusals:
        # Name the first few rather than a bare count: "3 were not re-run" sends the
        # operator hunting, and the reason is already in hand.
        shown = "; ".join(
            f"{refusal.contact_label} — {refusal.reason}" for refusal in outcome.refusals[:3]
        )
        remaining = len(outcome.refusals) - 3
        if remaining > 0:
            shown += f"; and {remaining} more"
        message = f"{message} {shown}"
    if not outcome.accepted:
        return _redirect(destination, err=message)
    return _redirect(destination, ok=message)


# ---------------------------------------------------------------------------
# Campaign contact file import (IMP-001)
# ---------------------------------------------------------------------------
#
# Upload -> preview -> confirm, bound to one Campaign throughout. The Campaign is
# in the URL of every step and is re-checked at each of them, so a staged upload
# can only ever be confirmed into the Campaign it was uploaded for. That is also
# the whole of the authorization boundary this application has today: it is
# single-operator, there are no accounts, and the limitation is stated on the
# page rather than implied by an absent login form.
#
# The preview writes nothing. Confirmation is the first durable mutation, and it
# is a POST, so a refreshed preview cannot import anything.


def _import_on(settings: Settings) -> bool:
    return settings.features.csv_import


def _campaign_or_none(db: Session, campaign_id: str) -> tuple[uuid.UUID, Any] | None:
    identifier = _uuid(campaign_id)
    if identifier is None:
        return None
    campaign = campaign_service.get_campaign(db, identifier)
    if campaign is None:
        return None
    return identifier, campaign


def _staging_dir() -> str:
    return get_settings().staged_uploads_dir


def _load_campaign_staged(campaign_id: uuid.UUID, staged_id: str) -> Any | None:
    """Load a staged upload only if it belongs to *campaign_id*.

    The ownership check is the point. Without it a staged upload id — which is
    guessable only in the sense that anything is, but is also copied into URLs
    and browser history — would let a file uploaded for one Campaign be confirmed
    into another, and the Campaign a contact was imported into is exactly the
    fact this whole flow exists to fix in place.
    """

    try:
        staged = staging.load_staged_upload(_staging_dir(), staged_id)
    except staging.StagedUploadNotFound:
        return None
    if staged.campaign_id != str(campaign_id):
        return None
    return staged


@router.get("/campaigns/{campaign_id}/imports")
def campaign_imports_page(
    campaign_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Upload a contact file into this Campaign, and see what has been uploaded."""

    found = _campaign_or_none(db, campaign_id)
    if found is None:
        return _not_found(request, db, "That campaign does not exist.")
    identifier, campaign = found
    settings = get_settings()
    return _render(
        request,
        db,
        "campaign_imports.html",
        {
            "active_nav": "campaigns",
            "page_title": f"Import contacts — {campaign.name}",
            "campaign": campaign,
            "import_on": _import_on(settings),
            "max_upload_mb": round(settings.max_upload_bytes / (1024 * 1024), 1),
            "max_rows": campaign_import.MAX_DATA_ROWS,
            "batches": campaign_import.campaign_batches(db, identifier),
            "archived": campaign.status is CampaignStatus.ARCHIVED,
        },
    )


@router.post("/campaigns/{campaign_id}/imports")
async def campaign_import_upload(
    campaign_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Stage an uploaded file. Nothing is imported and no Contact is created."""

    found = _campaign_or_none(db, campaign_id)
    if found is None:
        return _redirect("/app/campaigns", err="That campaign does not exist.")
    identifier, campaign = found
    base = f"/app/campaigns/{identifier}/imports"
    settings = get_settings()
    if not _import_on(settings):
        return _redirect(
            base,
            err="Contact file import is switched off. Set FEATURES__CSV_IMPORT=true and restart.",
        )
    if campaign.status is CampaignStatus.ARCHIVED:
        return _redirect(base, err="An archived campaign cannot receive contacts.")

    form = await request.form()
    upload = form.get("file")
    filename = getattr(upload, "filename", None)
    if upload is None or not filename:
        return _redirect(base, err="Choose a .csv or .xlsx file to upload.")
    # Declared size first, so an oversized upload is refused before its bytes are
    # buffered into this process. This is a best-effort improvement, not complete
    # streaming protection: Content-Length is client-supplied and absent from a
    # chunked request, so the authoritative ceiling for an untrusted client
    # remains the reverse proxy's own body limit. The check below still runs on
    # what actually arrived.
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit():
        try:
            staging.enforce_upload_size(
                int(declared), settings.max_upload_bytes, filename=str(filename)
            )
        except staging.UploadTooLargeError as exc:
            return _redirect(base, err=str(exc))

    content = await upload.read()  # type: ignore[union-attr]

    try:
        staging.enforce_upload_size(len(content), settings.max_upload_bytes, filename=str(filename))
    except staging.UploadTooLargeError as exc:
        return _redirect(base, err=str(exc))

    # Parsed once here so an unreadable or unrecognized file is refused before a
    # single byte is written to the staging area.
    try:
        inspection = campaign_import.inspect(content, str(filename))
    except campaign_import.CampaignImportError as exc:
        return _redirect(base, err=str(exc))
    if not inspection.importable_sheets:
        detection = inspection.sheets[0].detection if inspection.sheets else None
        return _redirect(
            base,
            err=(
                apollo.missing_header_message(detection)
                if detection is not None
                else "No worksheet in this file carries a recognizable contact header row."
            ),
        )

    staged = staging.create_staged_upload(
        _staging_dir(),
        filename=campaign_import.sanitize_filename(str(filename)),
        campaign_id=str(identifier),
        content=content,
        source_format=inspection.source_format,
        provenance={
            "source_name": campaign_import.source_name_for(
                inspection.importable_sheets[0].detection
            )
        },
    )
    return _redirect(f"{base}/staged/{staged.id}")


@router.get("/campaigns/{campaign_id}/imports/staged/{staged_id}")
def campaign_import_preview_page(
    campaign_id: str,
    staged_id: str,
    request: Request,
    db: Session = Depends(get_db),
    sheet: int | None = None,
) -> HTMLResponse:
    """Show exactly what confirming would do. Performs no writes."""

    found = _campaign_or_none(db, campaign_id)
    if found is None:
        return _not_found(request, db, "That campaign does not exist.")
    identifier, campaign = found
    staged = _load_campaign_staged(identifier, staged_id)
    if staged is None:
        return _not_found(
            request,
            db,
            "That upload is not available for this campaign. It may have expired, or it "
            "may belong to a different campaign.",
        )
    if staged.confirmed_batch_id:
        return _redirect(  # type: ignore[return-value]
            f"/app/campaigns/{identifier}/imports/{staged.confirmed_batch_id}",
            ok="This upload was already imported; showing the batch it produced.",
        )

    content = staging.read_staged_content(_staging_dir(), staged_id)
    try:
        inspection = campaign_import.inspect(content, staged.filename)
        preview = campaign_import.preview(
            db,
            campaign_id=identifier,
            content=content,
            filename=staged.filename,
            sheet_index=sheet,
        )
    except campaign_import.CampaignImportError as exc:
        return _render(
            request,
            db,
            "campaign_import_preview.html",
            {
                "active_nav": "campaigns",
                "page_title": f"Preview — {staged.filename}",
                "campaign": campaign,
                "staged": staged,
                "inspection": None,
                "preview": None,
                "fatal_error": str(exc),
            },
        )

    return _render(
        request,
        db,
        "campaign_import_preview.html",
        {
            "active_nav": "campaigns",
            "page_title": f"Preview — {staged.filename}",
            "campaign": campaign,
            "staged": staged,
            "inspection": inspection,
            "preview": preview,
            "shown_rows": preview.rows[:PREVIEW_ROWS_SHOWN],
            "fatal_error": None,
            "import_on": _import_on(get_settings()),
        },
    )


@router.post("/campaigns/{campaign_id}/imports/staged/{staged_id}/confirm")
def campaign_import_confirm(
    campaign_id: str,
    staged_id: str,
    request: Request,
    db: Session = Depends(get_db),
    sheet: str = Form(""),
) -> RedirectResponse:
    """Import the staged file. The first point anything durable is written."""

    found = _campaign_or_none(db, campaign_id)
    if found is None:
        return _redirect("/app/campaigns", err="That campaign does not exist.")
    identifier, campaign = found
    base = f"/app/campaigns/{identifier}/imports"
    staged = _load_campaign_staged(identifier, staged_id)
    if staged is None:
        return _redirect(
            base,
            err=(
                "That upload is not available for this campaign. It may have expired, or "
                "it may belong to a different campaign."
            ),
        )
    if staged.confirmed_batch_id:
        return _redirect(
            f"{base}/{staged.confirmed_batch_id}",
            ok="This upload was already imported; showing the batch it produced.",
        )
    if campaign.status is CampaignStatus.ARCHIVED:
        return _redirect(base, err="An archived campaign cannot receive contacts.")

    content = staging.read_staged_content(_staging_dir(), staged_id)
    try:
        result = campaign_import.confirm(
            db,
            campaign_id=identifier,
            content=content,
            filename=staged.filename,
            sheet_index=_sheet_index(sheet),
            uploaded_by=draft_service.OPERATOR_ACTOR,
        )
    except campaign_import.CampaignImportError as exc:
        return _redirect(f"{base}/staged/{staged_id}", err=str(exc))

    staged.confirmed_batch_id = str(result.batch_id)
    staging.update_staged_upload(_staging_dir(), staged)

    if result.reused_existing_batch:
        return _redirect(
            f"{base}/{result.batch_id}",
            ok="This exact file and worksheet were already imported; showing the existing batch.",
        )
    return _redirect(
        f"{base}/{result.batch_id}",
        ok=(
            f"{result.imported} imported, {result.matched_existing} matched an existing "
            f"contact, {result.already_in_campaign} already in this campaign, "
            f"{result.skipped_duplicate} skipped as duplicates, "
            f"{result.review_required} need review, {result.suppressed} suppressed, "
            f"{result.failed} failed."
        ),
    )


@router.post("/campaigns/{campaign_id}/imports/staged/{staged_id}/discard")
def campaign_import_discard(
    campaign_id: str, staged_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Throw the staged upload away. Nothing was ever imported from it."""

    found = _campaign_or_none(db, campaign_id)
    if found is None:
        return _redirect("/app/campaigns", err="That campaign does not exist.")
    identifier, _campaign = found
    base = f"/app/campaigns/{identifier}/imports"
    if _load_campaign_staged(identifier, staged_id) is None:
        return _redirect(base, err="That upload is not available for this campaign.")
    try:
        staging.delete_staged_upload(_staging_dir(), staged_id)
    except staging.StagedUploadNotFound:
        pass
    return _redirect(base, ok="Upload discarded. Nothing was imported.")


@router.get("/campaigns/{campaign_id}/imports/{batch_id}")
def campaign_import_batch_page(
    campaign_id: str,
    batch_id: str,
    request: Request,
    db: Session = Depends(get_db),
    page: int = 1,
) -> HTMLResponse:
    """The result of one confirmed import, row by row."""

    found = _campaign_or_none(db, campaign_id)
    if found is None:
        return _not_found(request, db, "That campaign does not exist.")
    identifier, campaign = found
    parsed = _uuid(batch_id)
    batch = campaign_import.get_batch(db, parsed) if parsed else None
    # A batch belonging to another campaign is not merely the wrong page: showing
    # it would disclose another campaign's contacts and their addresses.
    if batch is None or batch.campaign_id != identifier:
        return _not_found(request, db, "That import does not exist in this campaign.")

    current = max(1, page)
    rows, total = campaign_import.batch_rows(
        db, batch_id=batch.id, limit=PAGE_SIZE, offset=(current - 1) * PAGE_SIZE
    )
    return _render(
        request,
        db,
        "campaign_import_batch.html",
        {
            "active_nav": "campaigns",
            "page_title": f"Import — {batch.sanitized_filename or batch.filename}",
            "campaign": campaign,
            "batch": batch,
            "counts": campaign_import.batch_counts(batch),
            "rows": rows,
            "total_rows": total,
            "page": current,
            "pages": _pages(total),
            "base_url": f"/app/campaigns/{identifier}/imports/{batch.id}",
        },
    )


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


@router.get("/review")
def review_page(
    request: Request,
    db: Session = Depends(get_db),
    campaign: str | None = None,
    view: str = draft_service.VIEW_AWAITING,
    draft: str | None = None,
) -> HTMLResponse:
    """Read a draft, and decide.

    The design shows a confidence score and an auto-send threshold. Neither exists:
    there is no scoring service and no sending, so nothing here is scored and
    nothing goes out without this decision. What the panel *can* show is real and
    more useful — the sourced claims the draft was allowed to use, the verification
    evidence for the address, and what the Knowledge Base authorised.
    """

    campaign_id = _uuid(campaign) if campaign else None
    queue = draft_service.list_queue(db, campaign_id=campaign_id, view=view, limit=100)

    selected: draft_service.DraftRow | None = None
    requested = _uuid(draft) if draft else None
    if requested is not None:
        selected = draft_service.get_draft(db, requested)
    if selected is None:
        selected = (
            queue.rows[0]
            if queue.rows
            else draft_service.first_awaiting(db, campaign_id=campaign_id)
        )

    settings = get_settings()
    execution = None
    if (
        selected is not None
        and selected.campaign_contact_id is not None
        and _agent_workbench_on(settings)
    ):
        execution = _reader(db).contact_execution(
            selected.campaign_id, selected.campaign_contact_id
        )

    evidence = _draft_evidence(db, selected, execution)
    return _render(
        request,
        db,
        "review.html",
        {
            "active_nav": "review",
            "page_title": "Review",
            "queue": queue,
            "views": draft_service.VIEWS,
            "selected": selected,
            "execution": execution,
            "evidence": evidence,
            "campaigns": campaign_service.list_campaigns(db),
            "campaign_id": campaign_id,
            "agent_workbench_on": _agent_workbench_on(settings),
        },
    )


@dataclass(frozen=True)
class EvidenceItem:
    kind: str
    fact: str
    source: str
    quote: str | None = None


def _draft_evidence(
    db: Session,
    row: draft_service.DraftRow | None,
    execution: agent_views.ContactExecutionView | None,
) -> tuple[EvidenceItem, ...]:
    """Why this email says what it says.

    Assembled only from what was committed: the Insights claims that survived the
    evidence gate (with their source URLs), the Verification decision for the exact
    address, the Research dossier the claims came out of, and the Knowledge Base
    offerings the campaign named. A draft with thin evidence looks thin here.
    """

    if row is None:
        return ()

    items: list[EvidenceItem] = []

    if execution is not None and execution.insights is not None:
        for claim in execution.insights.claims:
            items.append(
                EvidenceItem(
                    kind="Sourced claim it was allowed to use",
                    fact=claim.claim,
                    source=claim.source_url or "No source URL was recorded with this claim.",
                )
            )
        if execution.insights.claims_dropped:
            items.append(
                EvidenceItem(
                    kind="Claims dropped",
                    fact=(
                        f"{execution.insights.claims_dropped} claim(s) were dropped for having "
                        "no usable source. They were not weakened and re-used — they were "
                        "removed."
                    ),
                    source="Insights Agent · evidence gate",
                )
            )
        if execution.insights.unknowns_recorded:
            items.append(
                EvidenceItem(
                    kind="Named gaps",
                    fact=(
                        f"{execution.insights.unknowns_recorded} thing(s) were recorded as "
                        "unknown rather than guessed at."
                    ),
                    source="Insights Agent",
                )
            )

    if execution is not None and execution.verification is not None:
        ver = execution.verification
        if ver.accepted:
            fact = (
                f"{row.email} — the receiving mail server accepted this exact mailbox, so an "
                "email sent to it will land."
            )
        elif ver.simulated:
            fact = (
                f"{row.email} — the recorded result is from the built-in simulator, not from a "
                "real mail server. It is a normalized outcome, not external verification."
            )
        elif ver.decided:
            fact = f"{row.email} — verification decided: {ver.decision}. {ver.reason or ''}".strip()
        else:
            fact = f"{row.email} — no verification decision has been committed for this address."
        source_parts = [p for p in ("Verification Agent", ver.provider, ver.policy_version) if p]
        if ver.paid_calls:
            source_parts.append(f"{ver.paid_calls} paid call(s)")
        items.append(EvidenceItem(kind="The address", fact=fact, source=" · ".join(source_parts)))
    elif row.email:
        items.append(
            EvidenceItem(
                kind="The address",
                fact=f"{row.email} — no verification evidence is linked to this draft.",
                source="Nothing was committed by the Verification Agent for this contact.",
            )
        )

    if execution is not None and execution.research is not None:
        res = execution.research
        addressed = ", ".join(res.sections_present) or "none"
        items.append(
            EvidenceItem(
                kind="The research underneath",
                fact=(
                    f"Dossier v{res.dossier_version or '—'} addressed {len(res.sections_present)} "
                    f"of 9 sections ({addressed}) and named {res.unknown_count} gap(s) across "
                    f"{res.source_count} source(s)."
                ),
                source=res.producer or "Research Agent",
                quote=res.summary,
            )
        )

    offerings = []
    try:
        offerings = list(seller_campaign_offerings.offerings_for_campaign(db, row.campaign_id))
    except Exception:
        offerings = []
    if offerings:
        items.append(
            EvidenceItem(
                kind="Why the ask is this ask",
                fact="; ".join(offering.name for offering in offerings),
                source="Knowledge Base · Offerings named by this campaign",
            )
        )

    if row.rationale:
        items.append(
            EvidenceItem(
                kind="What the Agent said it did",
                fact=row.rationale,
                source="Personalization Agent · recorded with the draft",
            )
        )

    return tuple(items)


@router.post("/review/{draft_id}/approve")
def review_approve(
    draft_id: str,
    request: Request,
    db: Session = Depends(get_db),
    reason: str = Form(""),
    back: str = Form("/app/review"),
) -> RedirectResponse:
    identifier = _uuid(draft_id)
    if identifier is None:
        return _redirect("/app/review", err="That is not a draft id.")
    try:
        draft_service.approve(
            db,
            draft_version_id=identifier,
            actor=draft_service.OPERATOR_ACTOR,
            reason=reason or None,
        )
    except draft_service.DraftReviewError as exc:
        return _redirect(back, err=str(exc))
    db.commit()
    return _redirect(
        back,
        ok=(
            "Approved, and recorded against this exact version. Nothing was sent: no sending "
            "adapter is registered, so an approved draft waits for one."
        ),
    )


@router.post("/review/{draft_id}/discard")
def review_discard(
    draft_id: str,
    request: Request,
    db: Session = Depends(get_db),
    reason: str = Form(""),
    back: str = Form("/app/review"),
) -> RedirectResponse:
    identifier = _uuid(draft_id)
    if identifier is None:
        return _redirect("/app/review", err="That is not a draft id.")
    try:
        draft_service.discard(
            db,
            draft_version_id=identifier,
            actor=draft_service.OPERATOR_ACTOR,
            reason=reason or None,
        )
    except draft_service.DraftReviewError as exc:
        return _redirect(back, err=str(exc))
    db.commit()
    return _redirect(back, ok="Discarded. The draft itself is kept, with your decision on it.")


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


@router.get("/contacts")
def contacts_page(
    request: Request,
    db: Session = Depends(get_db),
    view: str = crm_records.VIEW_ALL,
    q: str | None = None,
    label: str | None = None,
    company: str | None = None,
    source: str | None = None,
    has_email: str | None = None,
    has_linkedin: str | None = None,
    sort: str = crm_records.SORT_RECENT,
    page: int = 1,
) -> HTMLResponse:
    """Everyone captured. A person exists once, whatever campaigns they are in."""

    filters = crm_records.CrmFilters(
        view=view,
        search=q,
        label_slug=label,
        company=company,
        source=source,
        has_email=_tri(has_email),
        has_linkedin=_tri(has_linkedin),
        sort=sort,
    ).normalized()
    current = max(1, page)
    rows, total = crm_records.list_crm_rows(
        db, filters=filters, limit=PAGE_SIZE, offset=(current - 1) * PAGE_SIZE
    )
    labels = capture_labels.list_labels(db)
    return _render(
        request,
        db,
        "contacts.html",
        {
            "active_nav": "contacts",
            "page_title": "Contacts",
            "rows": rows,
            "total": total,
            "page": current,
            "pages": _pages(total),
            "filters": filters,
            "filter_url": _filter_url("/app/contacts", filters),
            "labels": labels,
            "views": (
                (crm_records.VIEW_ALL, "All"),
                (crm_records.VIEW_AWAITING_COMPANY, "Awaiting a company"),
                (crm_records.VIEW_AMBIGUOUS, "Identity unresolved"),
                (crm_records.VIEW_SUPPRESSED, "Suppressed"),
            ),
            "sorts": (
                (crm_records.SORT_RECENT, "Last change"),
                (crm_records.SORT_NAME, "Name"),
                (crm_records.SORT_COMPANY, "Company"),
            ),
        },
    )


def _tri(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    return value.lower() in {"1", "true", "yes", "on"}


def _filter_url(base: str, filters: Any) -> str:
    params: dict[str, str] = {}
    for key in (
        "view",
        "search",
        "label_slug",
        "company",
        "source",
        "sort",
        "research_state",
    ):
        value = getattr(filters, key, None)
        if value in (None, ""):
            continue
        name = {"search": "q", "label_slug": "label", "research_state": "research"}.get(key, key)
        params[name] = value.value if hasattr(value, "value") else str(value)
    for key, name in (("has_email", "has_email"), ("has_linkedin", "has_linkedin")):
        value = getattr(filters, key, None)
        if value is not None:
            params[name] = "1" if value else "0"
    return f"{base}?{urlencode(params)}" if params else base


@router.get("/contacts/{contact_id}")
def contact_page(
    contact_id: str,
    request: Request,
    db: Session = Depends(get_db),
    campaign: str | None = None,
) -> HTMLResponse:
    """One person, and every Agent that touched them.

    The nine-step story the design shows is the Phase 2 stage ledger for one
    membership: what each Agent did, when, and why. It is per campaign because
    execution is per campaign, so the page names which one it is showing.
    """

    identifier = _uuid(contact_id)
    if identifier is None:
        return _not_found(request, db, "That is not a contact id.")
    detail = crm_detail.get_contact_detail(db, identifier)
    if detail is None:
        return _not_found(request, db, "That contact does not exist.")

    settings = get_settings()
    memberships = detail.memberships
    chosen = _uuid(campaign) if campaign else None
    membership = None
    for candidate, _campaign in memberships:
        if chosen is None or candidate.campaign_id == chosen:
            membership = candidate
            break

    execution = None
    if membership is not None and _agent_workbench_on(settings):
        execution = _reader(db).contact_execution(membership.campaign_id, membership.id)

    intel = verification_console.contact_email_intel(db, detail.contact)
    steps = _contact_steps(execution)
    latest_draft = None
    if membership is not None:
        page = draft_service.list_queue(
            db, campaign_id=membership.campaign_id, view=draft_service.VIEW_ALL, limit=100
        )
        latest_draft = next(
            (row for row in page.rows if row.contact_id == identifier and row.is_current), None
        )

    return _render(
        request,
        db,
        "contact.html",
        {
            "active_nav": "contacts",
            "page_title": detail.full_name,
            "detail": detail,
            "intel": intel,
            "execution": execution,
            "steps": steps,
            "membership": membership,
            "latest_draft": latest_draft,
            "agent_workbench_on": _agent_workbench_on(settings),
        },
    )


@dataclass(frozen=True)
class ContactStep:
    index: int
    label: str
    blurb: str
    status: str
    dot: str
    tag_text: str
    tag_tone: str
    detail: str
    at: datetime | None
    inset: str | None


def _contact_steps(
    execution: agent_views.ContactExecutionView | None,
) -> tuple[ContactStep, ...]:
    """The nine Agents as a story, from the committed stage ledger.

    Every sentence is the stage's own ``reason_code``/``reason_detail`` or the
    absence of one. Where a stage has recorded nothing, the step says nothing has
    run — which is different from saying it found nothing.
    """

    if execution is None:
        return ()
    by_agent = {stage.agent_id: stage for stage in execution.stages}
    steps: list[ContactStep] = []
    for position, agent_id in enumerate(PIPELINE_ORDER, start=1):
        spec = AGENT_SPECS[agent_id]
        stage = by_agent.get(agent_id)
        if stage is None:
            steps.append(
                ContactStep(
                    index=position,
                    label=spec.display_name,
                    blurb=AGENT_BLURBS[agent_id],
                    status="todo",
                    dot="",
                    tag_text="not reached",
                    tag_tone="",
                    detail="Nothing has run for this Agent yet.",
                    at=None,
                    inset=None,
                )
            )
            continue

        status = stage.status.value
        dot, tag_text, tag_tone = {
            "completed": ("done", "done", "ok"),
            "running": ("now", "running", "info"),
            "retrying": ("held", "retrying", "warn"),
            "paused": ("held", "held", "warn"),
            "failed": ("stuck", "stopped", "err"),
            "blocked": ("stuck", "blocked", "err"),
            "skipped": ("held", "skipped", ""),
            "disabled": ("", "agent off", ""),
        }.get(status, ("", "waiting", ""))

        detail_parts: list[str] = []
        if stage.reason_detail:
            detail_parts.append(stage.reason_detail)
        elif stage.reason_code:
            detail_parts.append(stage.reason_code.replace("_", " "))
        elif status == "completed":
            detail_parts.append("Completed, and the outcome was committed.")
        elif status == "waiting":
            detail_parts.append(
                "Waiting for its turn."
                if stage.waiting_on_agent is None
                else f"Waiting on the {AGENT_SPECS[stage.waiting_on_agent].display_name}."
            )
        elif status == "disabled":
            detail_parts.append(
                "This Agent is switched off, so nothing was claimed for this contact."
            )
        else:
            detail_parts.append("No reason was recorded.")

        inset = None
        if stage.attempt_count > 1:
            inset = (
                f"{stage.attempt_count} attempts. Phase 2 retries only what it marked "
                f"retryable, so an attempt count above one means the earlier attempt failed "
                f"in a way that could be retried."
            )
        if not stage.outcome_committed and status == "completed":
            inset = (
                "The job finished, but no pipeline event committed the outcome — so this is "
                "not counted as a completed stage."
            )

        steps.append(
            ContactStep(
                index=position,
                label=spec.display_name,
                blurb=AGENT_BLURBS[agent_id],
                status=status,
                dot=dot,
                tag_text=tag_text,
                tag_tone=tag_tone,
                detail=" ".join(detail_parts),
                at=stage.completed_at or stage.started_at or stage.updated_at,
                inset=inset,
            )
        )
    return tuple(steps)


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------


@router.get("/companies")
def companies_page(
    request: Request,
    db: Session = Depends(get_db),
    view: str = company_records.VIEW_ALL,
    q: str | None = None,
    research: str | None = None,
    has_linkedin: str | None = None,
    sort: str = company_records.SORT_RECENT,
    page: int = 1,
) -> HTMLResponse:
    """A website is resolved once and reused for everyone who works there."""

    state: ResearchState | None = None
    if research:
        try:
            state = ResearchState(research)
        except ValueError:
            state = None
    filters = company_records.CompanyFilters(
        view=view,
        search=q,
        research_state=state,
        has_linkedin=_tri(has_linkedin),
        sort=sort,
    ).normalized()
    current = max(1, page)
    rows, total = company_records.list_company_rows(
        db, filters=filters, limit=PAGE_SIZE, offset=(current - 1) * PAGE_SIZE
    )
    return _render(
        request,
        db,
        "companies.html",
        {
            "active_nav": "companies",
            "page_title": "Companies",
            "rows": rows,
            "total": total,
            "page": current,
            "pages": _pages(total),
            "filters": filters,
            "filter_url": _filter_url("/app/companies", filters),
            "provisional": shell.provisional_domain_count(db),
            "views": (
                (company_records.VIEW_ALL, "All"),
                (company_records.VIEW_WITH_CONTACTS, "With contacts"),
                (company_records.VIEW_UNRESOLVED_DOMAIN, "No website"),
                (company_records.VIEW_RESEARCHED, "Researched"),
                (company_records.VIEW_CONFLICTED, "Records disagree"),
            ),
            "sorts": (
                (company_records.SORT_RECENT, "Last change"),
                (company_records.SORT_NAME, "Name"),
                (company_records.SORT_CONTACTS, "Contacts"),
            ),
        },
    )


@router.get("/companies/{company_id}")
def company_page(company_id: str, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """The permanent company record: identity, the domain decision, the dossier."""

    identifier = _uuid(company_id)
    if identifier is None:
        return _not_found(request, db, "That is not a company id.")
    detail = company_detail.get_company_detail(db, identifier)
    if detail is None:
        return _not_found(request, db, "That company does not exist.")
    return _render(
        request,
        db,
        "company.html",
        {
            "active_nav": "companies",
            "page_title": detail.company.name,
            "detail": detail,
            "sections": list(_dossier_sections(detail)),
        },
    )


def _dossier_sections(detail: company_detail.CompanyDetailView) -> list[dict[str, Any]]:
    """The nine dossier sections, in order, with presence read honestly.

    A section that was never addressed is *unknown*, not empty — the model stores
    ``None`` for one and ``{}`` for the other, and the two are shown differently
    because "we looked and found nothing" is a real answer.
    """

    current = detail.current_dossier
    rows: list[dict[str, Any]] = []
    for position, section in enumerate(DossierSection, start=1):
        value = getattr(current.version, section.value, None) if current is not None else None
        addressed = value is not None
        fields: list[tuple[str, list[str]]] = []
        if isinstance(value, dict):
            for key, raw in value.items():
                fields.append((key.replace("_", " "), _dossier_lines(raw)))
        elif isinstance(value, list):
            fields.append((section.value.replace("_", " "), _dossier_lines(value)))
        rows.append(
            {
                "n": position,
                "key": section.value,
                "name": section.value.replace("_", " "),
                "addressed": addressed,
                "field_count": len(fields),
                "fields": fields,
                "raw": value,
            }
        )
    return rows


def _dossier_lines(value: Any) -> list[str]:
    if value is None:
        return ["not recorded"]
    if isinstance(value, list):
        lines: list[str] = []
        for item in value:
            if isinstance(item, dict):
                lines.append(
                    " · ".join(
                        f"{k.replace('_', ' ')}: "
                        f"{', '.join(str(x) for x in v) if isinstance(v, list) else v}"
                        for k, v in item.items()
                    )
                )
            else:
                lines.append(str(item))
        return lines or ["none recorded"]
    if isinstance(value, dict):
        return [f"{k.replace('_', ' ')}: {v}" for k, v in value.items()] or ["none recorded"]
    return [str(value)]


# ---------------------------------------------------------------------------
# Knowledge Base
# ---------------------------------------------------------------------------


@router.get("/knowledge")
@router.get("/knowledge/{section}")
def knowledge_page(
    request: Request,
    db: Session = Depends(get_db),
    section: str = "overview",
    offering: str | None = None,
) -> HTMLResponse:
    """What we sell, what we may say about it, and who we say it to.

    Read-only here. Every one of these records is the authorisation for a claim in a
    draft, so editing stays on the admin surface where the create/update/archive
    forms and their validation already live — a second set of forms would be a
    second place for the rules to drift.
    """

    settings = get_settings()
    if not _kb_on(settings):
        return _render(
            request,
            db,
            "knowledge_disabled.html",
            {"active_nav": "knowledge", "page_title": "Knowledge Base"},
        )

    if section not in {key for key, _ in KB_SECTIONS}:
        section = "overview"

    counts = seller_records.counts(db)
    ctx: dict[str, Any] = {
        "active_nav": "knowledge",
        "page_title": "Knowledge Base",
        "kb_section": section,
        "kb_sections": KB_SECTIONS,
        "kb_counts": counts,
        "profile": seller_profile.get_profile(db),
    }

    if section == "overview":
        ctx["report"] = seller_readiness.seller_report(db)
        ctx["draft_counts"] = draft_service.queue_counts(db)
    elif section == "offerings":
        offerings = seller_records.list_offerings(db, include_archived=True)
        chosen = _uuid(offering) if offering else None
        default = offerings[0] if offerings else None
        selected = next((o for o in offerings if o.id == chosen), default)
        ctx["offerings"] = offerings
        ctx["selected"] = selected
        if selected is not None:
            ctx["proof_points"] = seller_records.proof_points_for_offering(db, selected.id)
            ctx["claims"] = seller_records.restricted_claims_for_offering(db, selected.id)
            ctx["personas"] = seller_records.personas_for_offering(db, selected.id)
            ctx["campaigns"] = seller_campaign_offerings.campaigns_for_offering(db, selected.id)
    elif section == "proof-points":
        proof_points = seller_records.list_proof_points(db, include_archived=True)
        ctx["records"] = proof_points
        ctx["offerings_by_record"] = seller_records.offerings_by_record(
            db, kind="proof_point", record_ids=[record.id for record in proof_points]
        )
    elif section == "restricted-claims":
        claims = seller_records.list_restricted_claims(db, include_archived=True)
        ctx["records"] = claims
        ctx["offerings_by_record"] = seller_records.offerings_by_record(
            db, kind="restricted_claim", record_ids=[record.id for record in claims]
        )
    elif section == "personas":
        personas = seller_records.list_personas(db, include_archived=True)
        ctx["records"] = personas
        ctx["offerings_by_record"] = seller_records.offerings_by_record(
            db, kind="persona", record_ids=[record.id for record in personas]
        )

    return _render(request, db, f"kb_{section.replace('-', '_')}.html", ctx)


# ---------------------------------------------------------------------------
# Agent settings & logs
# ---------------------------------------------------------------------------


@router.get("/agents")
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
    if not _agent_workbench_on(settings):
        return _render(
            request,
            db,
            "agents_disabled.html",
            {"active_nav": "agents", "page_title": "Agent settings"},
        )

    selected: AgentIdentifier = AgentIdentifier.PERSONALIZATION
    if agent:
        try:
            selected = AgentIdentifier(agent)
        except ValueError:
            selected = AgentIdentifier.PERSONALIZATION

    campaign_id = _uuid(campaign) if campaign else None
    reader = _reader(db)
    overview = reader.overview()
    detail = reader.agent_detail(selected, campaign_id=campaign_id)
    jobs = reader.jobs(agent_id=selected, campaign_id=campaign_id, limit=25)

    return _render(
        request,
        db,
        "agents.html",
        {
            "active_nav": "agents",
            "page_title": "Agent settings & logs",
            "overview": overview,
            "detail": detail,
            "jobs": jobs,
            "selected": selected,
            "spec": AGENT_SPECS[selected],
            "blurb": AGENT_BLURBS[selected],
            "phases": PHASES,
            "blurbs": AGENT_BLURBS,
            "campaigns": campaign_service.list_campaigns(db),
            "campaign_id": campaign_id,
        },
    )


@router.post("/agents/{agent_id}/control")
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
        return _redirect("/app/agents", err="That is not an Agent.")
    try:
        wanted = AgentControlStatus(status)
    except ValueError:
        return _redirect(f"/app/agents?agent={agent_id}", err="That is not a control state.")
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
            return _redirect(
                f"/app/agents?agent={agent_id}", err="That control version is not a number."
            )

    commands = workbench_agents.WorkbenchCommands(db)
    scope = _uuid(campaign_id) if campaign_id else None
    back = f"/app/agents?agent={agent_id}" + (f"&campaign={scope}" if scope else "")
    if scope is not None:
        outcome = commands.set_campaign_override(
            scope, target, wanted, expected_version=version, reason=reason or None
        )
    else:
        outcome = commands.set_global_agent_status(
            target, wanted, expected_version=version, reason=reason or None
        )
    if not outcome.accepted:
        return _redirect(back, err=outcome.message)
    db.commit()
    return _redirect(back, ok=outcome.message)


# ---------------------------------------------------------------------------
# Capture, suppressions, future surfaces
# ---------------------------------------------------------------------------


@router.get("/capture")
def capture_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """What the capture extension does, and what it has actually captured."""

    settings = get_settings()
    enabled = settings.features.enabled()
    pending: list[Any] = []
    try:
        pending = list(capture_promotion.pending_captures(db, limit=25))
    except Exception:
        pending = []
    labels = capture_labels.list_labels(db)
    unresolved = 0
    try:
        unresolved = len(resolution_service.unresolved_captures(db, limit=500))
    except Exception:
        unresolved = 0
    return _render(
        request,
        db,
        "capture.html",
        {
            "active_nav": "capture",
            "page_title": "Capture",
            "intake_on": "contact_capture_intake" in enabled,
            "promotion_on": "contact_capture_promotion" in enabled,
            "auto_domain_on": "automatic_company_domain_resolution" in enabled,
            "pending": pending,
            "labels": labels,
            "unresolved": unresolved,
            "campaigns": campaign_service.list_campaigns(db),
        },
    )


@router.get("/suppressions")
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
    return _render(
        request,
        db,
        "suppressions.html",
        {
            "active_nav": "suppressions",
            "page_title": "Suppression list",
            "rows": active,
            "total": total,
            "enabled": "suppressions" in get_settings().features.enabled(),
        },
    )


@router.get("/sending")
@router.get("/replies")
@router.get("/sequences")
@router.get("/analytics")
def soon_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """A future-feature surface: the design's shape, and no data at all."""

    key = request.url.path.rsplit("/", 1)[-1]
    spec = SOON_SECTIONS.get(key, SOON_SECTIONS["sending"])
    return _render(
        request,
        db,
        "soon.html",
        {
            "active_nav": spec["nav"],
            "page_title": spec["title"],
            "spec": spec,
        },
    )


__all__ = ["router"]

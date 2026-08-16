"""People and Companies: the permanent records, independent of any Campaign.

People · Companies is one destination with a local switch. Preparation belongs
to Campaign membership, not to the person, so these pages show who somebody is
and where the facts came from, and link into the Campaign for their emails.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import get_settings
from app.models.campaign import Campaign
from app.models.enums import AgentIdentifier, DossierSection, ResearchState
from app.services import campaign_access, customer_status, workbench_agents
from app.services import drafts as draft_service
from app.services.agents.registry import AGENT_SPECS, PIPELINE_ORDER
from app.services.campaign_access import actor_from_request
from app.services.captures import labels as capture_labels
from app.services.companies import detail as company_detail
from app.services.companies import records as company_records
from app.services.crm import detail as crm_detail
from app.services.crm import records as crm_records
from app.services.sequences import read as sequence_read
from app.services.verification import console as verification_console
from app.services.workbench_agents import views as agent_views
from app.web.v2 import shell
from app.web.v2.pages.emails import (
    SEQUENCE_STATE_FEATURE_OFF,
    SequenceAvailability,
    gmail_draft_rows,
)
from app.web.v2.pages.emails import sequence_availability as resolve_sequence_availability

router = shell.router


def _reader(db: Session) -> workbench_agents.PhaseTwoWorkbenchReader:
    return workbench_agents.PhaseTwoWorkbenchReader(db)


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


@router.get("/people")
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
        db, filters=filters, limit=shell.PAGE_SIZE, offset=(current - 1) * shell.PAGE_SIZE
    )
    labels = capture_labels.list_labels(db)
    return shell.render(
        request,
        db,
        "contacts.html",
        {
            "active_nav": "people",
            "page_title": "People",
            "rows": rows,
            "total": total,
            "page": current,
            "pages": shell.pages(total),
            "filters": filters,
            "filter_url": _filter_url("/app/people", filters),
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


@router.get("/people/{contact_id}")
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

    identifier = shell.uuid_or_none(contact_id)
    if identifier is None:
        return shell.not_found(request, db, "That is not a contact id.")
    detail = crm_detail.get_contact_detail(db, identifier)
    if detail is None:
        return shell.not_found(request, db, "That contact does not exist.")

    settings = get_settings()
    # A Contact is a permanent record and is not owned by a campaign — the same
    # person can legitimately appear in several. What *is* campaign-scoped is the
    # membership: its pipeline, its drafts, its sequence, its Agent history. So
    # the page keeps showing the contact and narrows the memberships to the
    # campaigns this account may open, rather than refusing the whole person
    # because one of their memberships belongs to somebody else's campaign.
    actor = actor_from_request(request)
    allowed = campaign_access.accessible_campaign_ids(db, actor)
    if allowed is not None:
        # Narrow the view object itself, not a local copy: the template renders
        # `detail.memberships` directly, so filtering only a local list would
        # have scoped the code and not the page.
        detail.memberships = [
            (candidate, candidate_campaign)
            for candidate, candidate_campaign in detail.memberships
            if candidate.campaign_id in allowed
        ]
    memberships = detail.memberships
    chosen = shell.uuid_or_none(campaign) if campaign else None
    if chosen is not None:
        campaign_access.require_campaign_access(db, chosen, actor)
    membership = None
    for candidate, _campaign in memberships:
        if chosen is None or candidate.campaign_id == chosen:
            membership = candidate
            break

    execution = None
    if membership is not None and shell.agent_workbench_on(db, settings):
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

    sequence_summary = None
    sequence_rows: tuple[sequence_read.MessageRow, ...] = ()
    sequence_details: tuple[sequence_read.MessageDetail, ...] = ()
    sequence_record = None
    sequence_availability = SequenceAvailability(state=SEQUENCE_STATE_FEATURE_OFF)
    # The one word this page leads with, in the customer's vocabulary. The nine
    # per-Agent steps below it stay exactly as they were: they are observability,
    # and a person who wants to know where their contact got to should be able to
    # look. They are not a checklist anybody is expected to work through.
    contact_state = (
        customer_status.status_for_membership(db, campaign_contact_id=membership.id)
        if membership is not None
        else None
    )
    if membership is not None:
        # Looked up regardless of the switches: an existing sequence is shown
        # and explained, never hidden.
        sequence_record = sequence_read.sequence_for_membership(
            db, campaign_contact_id=membership.id
        )
        sequence_availability = resolve_sequence_availability(
            db,
            settings,
            campaign=db.get(Campaign, membership.campaign_id),
            sequence=sequence_record,
        )
        if sequence_record is not None:
            sequence_summary = sequence_read.summary(db, sequence=sequence_record)
            sequence_rows = sequence_read.message_rows(db, sequence=sequence_record)
            # All seven bodies, in one query. This is the page an operator came
            # to read, copy and edit the sequence on, so paging through it one
            # message at a time cost six extra loads and bought nothing. The
            # Review queue keeps the no-bodies rule, because it lists forty
            # contacts rather than one.
            sequence_details = sequence_read.message_details(db, sequence=sequence_record)

    return shell.render(
        request,
        db,
        "contact.html",
        {
            "active_nav": "people",
            "page_title": detail.full_name,
            "detail": detail,
            "intel": intel,
            "execution": execution,
            "steps": steps,
            "membership": membership,
            "contact_state": contact_state,
            "status_labels": customer_status.STATUS_LABELS,
            "status_notes": customer_status.STATUS_NOTES,
            "status_tones": customer_status.STATUS_TONES,
            "latest_draft": latest_draft,
            "agent_workbench_on": shell.agent_workbench_on(db, settings),
            "sequences_on": shell.sequences_on(db, settings) or sequence_record is not None,
            "sequence_section_visible": shell.sequences_on(db, settings)
            or sequence_record is not None,
            "sequence_generation_on": shell.sequences_on(db, settings),
            "sequence_availability": sequence_availability,
            "sequence": sequence_record,
            "sequence_summary": sequence_summary,
            "sequence_rows": sequence_rows,
            "sequence_details": sequence_details,
            # #267. Resolved on every render rather than cached: a mailbox can
            # be revoked at Google between two page loads, and the page an
            # operator is about to click Create Gmail drafts on is exactly the
            # place that must not be showing a stale "connected".
            "gmail_drafts_on": shell.gmail_drafts_on(db, settings),
            "mailbox": shell.mailbox_state(db, settings),
            # Scoped to this operator's own connected mailbox: a sequence
            # belongs to a Campaign Contact rather than to an operator, so an
            # unscoped read would show one operator the address of another
            # operator's mailbox.
            "gmail_draft_rows": gmail_draft_rows(db, settings, sequence=sequence_record),
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
        db, filters=filters, limit=shell.PAGE_SIZE, offset=(current - 1) * shell.PAGE_SIZE
    )
    return shell.render(
        request,
        db,
        "companies.html",
        {
            "active_nav": "people",
            "page_title": "Companies",
            "rows": rows,
            "total": total,
            "page": current,
            "pages": shell.pages(total),
            "filters": filters,
            "filter_url": _filter_url("/app/companies", filters),
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

    identifier = shell.uuid_or_none(company_id)
    if identifier is None:
        return shell.not_found(request, db, "That is not a company id.")
    detail = company_detail.get_company_detail(db, identifier)
    if detail is None:
        return shell.not_found(request, db, "That company does not exist.")
    return shell.render(
        request,
        db,
        "company.html",
        {
            "active_nav": "people",
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
# Legacy URLs
# ---------------------------------------------------------------------------


@router.get("/contacts")
def contacts_redirect(request: Request) -> RedirectResponse:
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(f"/app/people{query}", status_code=308)


@router.get("/contacts/{contact_id}")
def contact_redirect(contact_id: str, request: Request) -> RedirectResponse:
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(f"/app/people/{contact_id}{query}", status_code=308)


__all__ = ["router"]

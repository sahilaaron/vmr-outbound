"""People and Companies: the permanent records, independent of any Campaign.

People · Companies is one destination with a local switch. Preparation belongs
to Campaign membership, not to the person, so these pages show who somebody is
and where the facts came from, and link into the Campaign for their emails.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.enums import CampaignStatus, DossierSection, ResearchState
from app.models.insight import Insight
from app.services import campaign_access, campaign_contacts, people_workspace
from app.services import campaigns as campaign_service
from app.services.campaign_access import actor_from_request
from app.services.campaign_contacts import CampaignContactError
from app.services.captures import labels as capture_labels
from app.services.companies import detail as company_detail
from app.services.companies import records as company_records
from app.services.company_intelligence import read as intelligence_read
from app.services.crm import detail as crm_detail
from app.services.crm import records as crm_records
from app.services.resolution import service as resolution_service
from app.web.v2 import shell

router = shell.router


@dataclass(frozen=True)
class SourceLine:
    label: str
    detail: str | None = None
    when: datetime | None = None
    href: str | None = None


@dataclass(frozen=True)
class ActivityLine:
    at: datetime
    text: str
    meta: str = ""


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


def _open_campaigns(request: Request, db: Session) -> list[Any]:
    return [
        overview.campaign
        for overview in campaign_service.list_campaigns(db, actor=actor_from_request(request))
        if overview.campaign.status is not CampaignStatus.ARCHIVED
    ]


@router.get("/people")
def people_page(
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
    campaign: str | None = None,
) -> HTMLResponse:
    """Everyone on file. A person exists once, whatever Campaigns they are in."""

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
    contact_ids = [row.contact_id for row in rows if row.contact_id]
    counts = people_workspace.campaign_counts_by_contact(db, contact_ids)
    return shell.render(
        request,
        db,
        "people.html",
        {
            "active_nav": "people",
            "page_title": "People",
            "rows": rows,
            "total": total,
            "page": current,
            "pages": shell.pages(total),
            "filters": filters,
            "filter_url": _filter_url("/app/people", filters),
            "labels": capture_labels.list_labels(db),
            "campaign_summaries": {
                contact_id: people_workspace.campaign_summary(counts.get(contact_id))
                for contact_id in contact_ids
            },
            "campaigns": _open_campaigns(request, db),
            "preselected_campaign": shell.uuid_or_none(campaign) if campaign else None,
            "views": (
                (crm_records.VIEW_ALL, "All"),
                (crm_records.VIEW_AWAITING_COMPANY, "Awaiting a company"),
                (crm_records.VIEW_AMBIGUOUS, "Identity unresolved"),
                (crm_records.VIEW_SUPPRESSED, "Never contacted"),
            ),
            "sorts": (
                (crm_records.SORT_RECENT, "Last change"),
                (crm_records.SORT_NAME, "Name"),
                (crm_records.SORT_COMPANY, "Company"),
            ),
        },
    )


@router.post("/people/add-to-campaign")
async def people_add_to_campaign(
    request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Add existing people to one Campaign — the fourth Add people source."""

    form = await request.form()
    back = shell.in_app_path(str(form.get("back") or ""), "/app/people")
    campaign_id = shell.uuid_or_none(str(form.get("campaign_id", "")))
    if campaign_id is None:
        return shell.redirect(back, err="Choose a Campaign to add the selected people to.")
    campaign_access.require_campaign_access(db, campaign_id, actor_from_request(request))
    selected: list[uuid.UUID] = []
    for raw in form.getlist("contact_ids"):
        parsed = shell.uuid_or_none(str(raw))
        if parsed is not None:
            selected.append(parsed)
    if not selected:
        return shell.redirect(back, err="Tick at least one person first.")
    try:
        outcome = campaign_contacts.enrol_contacts(
            db,
            campaign_id=campaign_id,
            contact_ids=selected,
            source_type="manual",
            source_reference="people-page-selection",
        )
    except CampaignContactError as exc:
        db.rollback()
        return shell.redirect(back, err=str(exc))
    db.commit()
    message = outcome.summary
    if outcome.refused:
        message = f"{message} First refusal: {outcome.refused[0][1]}"
    return shell.redirect(back, ok=message)


@router.get("/people/{contact_id}")
def person_page(
    contact_id: str,
    request: Request,
    db: Session = Depends(get_db),
    campaign: str | None = None,
) -> HTMLResponse:
    """Who this person is, where the facts came from, and their Campaign state."""

    identifier = shell.uuid_or_none(contact_id)
    if identifier is None:
        return shell.not_found(request, db, "That is not a person id.")
    detail = crm_detail.get_contact_detail(db, identifier)
    if detail is None:
        return shell.not_found(request, db, "That person does not exist.")

    actor = actor_from_request(request)
    allowed = campaign_access.accessible_campaign_ids(db, actor)
    memberships = [
        row
        for row in people_workspace.memberships_for(db, detail.contact)
        if allowed is None or row.campaign.id in allowed
    ]
    joined = {row.campaign.id for row in memberships}
    addable = [item for item in _open_campaigns(request, db) if item.id not in joined]

    insights = list(
        db.scalars(
            select(Insight)
            .where(Insight.contact_id == detail.contact.id)
            .order_by(Insight.created_at.desc())
            .limit(5)
        ).all()
    )

    sources: list[SourceLine] = []
    for capture in detail.captures:
        sources.append(
            SourceLine(
                label="Chrome extension"
                if "linkedin" in capture.source.lower() or "sales" in capture.source.lower()
                else capture.source.replace("_", " ").capitalize(),
                detail=capture.source_url,
                when=capture.captured_at or capture.ingested_at,
                href=getattr(capture, "detail_url", None),
            )
        )
    for row in memberships:
        kind = (row.membership.source_kind or "").lower()
        if kind in ("sheets", "google_sheets"):
            sources.append(SourceLine("Google Sheets", when=row.membership.enrolled_at))
        elif kind in ("import", "csv", "file"):
            sources.append(SourceLine("Imported file", when=row.membership.enrolled_at))
        elif kind == "manual":
            sources.append(SourceLine("Added from People", when=row.membership.enrolled_at))
    activity: list[ActivityLine] = [
        ActivityLine(
            at=row.membership.enrolled_at,
            text=f"Added to {row.campaign.name}",
            meta=row.membership.enrolled_by or "",
        )
        for row in memberships
        if row.membership.enrolled_at is not None
    ]
    for capture in detail.captures:
        activity.append(
            ActivityLine(
                at=capture.ingested_at, text="Captured", meta=capture.source.replace("_", " ")
            )
        )
    activity.sort(key=lambda line: line.at, reverse=True)

    return shell.render(
        request,
        db,
        "person.html",
        {
            "active_nav": "people",
            "page_title": detail.full_name,
            "detail": detail,
            "memberships": memberships,
            "addable_campaigns": addable,
            "insights": insights,
            "sources": sources,
            "activity": activity[:20],
        },
    )


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
    companies = [row.company for row in rows]
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
            "website_labels": people_workspace.website_labels(db, companies),
            "active_campaigns": people_workspace.active_campaign_counts(
                db, [company.id for company in companies]
            ),
            "views": (
                (company_records.VIEW_ALL, "All"),
                (company_records.VIEW_WITH_CONTACTS, "With people"),
                (company_records.VIEW_UNRESOLVED_DOMAIN, "No website"),
                (company_records.VIEW_RESEARCHED, "Researched"),
            ),
            "sorts": (
                (company_records.SORT_RECENT, "Last change"),
                (company_records.SORT_NAME, "Name"),
                (company_records.SORT_CONTACTS, "People"),
            ),
        },
    )


@router.get("/companies/{company_id}")
def company_page(company_id: str, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """What we know about this Company, and where that knowledge is used."""

    identifier = shell.uuid_or_none(company_id)
    if identifier is None:
        return shell.not_found(request, db, "That is not a company id.")
    detail = company_detail.get_company_detail(db, identifier)
    if detail is None:
        return shell.not_found(request, db, "That company does not exist.")
    decision = detail.domain_resolution or resolution_service.company_view(db, identifier)
    website = people_workspace.website_label(
        detail.company, decision.decision.state if decision else None
    )
    try:
        intelligence = intelligence_read.get_company_intelligence(db, company_id=identifier)
    except Exception:
        intelligence = None
    facts = people_workspace.what_we_know(
        db,
        company=detail.company,
        field_provenance=detail.field_provenance,
        dossier_sections=_dossier_sections(detail),
        intelligence=intelligence,
    )
    return shell.render(
        request,
        db,
        "company.html",
        {
            "active_nav": "people",
            "page_title": detail.company.name,
            "detail": detail,
            "website": website,
            "facts": facts,
            "impacts": people_workspace.campaign_impact(db, company_id=identifier),
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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

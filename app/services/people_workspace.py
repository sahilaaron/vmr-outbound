"""People and Companies as the customer sees them: permanent records, plainly.

Preparation belongs to Campaign membership, not to the person, so this module
projects "which Campaigns is this person in and how far did each get" and
"what do we know about this Company and where did it come from" — never
Agents, dossier versions, provider candidates or classification queues.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.company_domain_resolution import CompanyDomainResolution
from app.models.contact import Contact
from app.models.enums import CampaignStatus, CompanyFieldSource, DomainResolutionState
from app.services import campaign_workspace, customer_status, email_progress
from app.services.customer_status import CustomerContactStatus
from app.services.imports.display import safe_text

# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


def campaign_counts_by_contact(
    session: Session, contact_ids: list[uuid.UUID]
) -> dict[uuid.UUID, customer_status.CustomerProgress]:
    """Per person, how many of their Campaign memberships stand in each outcome."""

    if not contact_ids:
        return {}
    expression = customer_status.status_expression()
    rows = session.execute(
        select(CampaignContact.contact_id, expression, func.count(CampaignContact.id))
        .join(Contact, Contact.id == CampaignContact.contact_id)
        .join(Campaign, Campaign.id == CampaignContact.campaign_id)
        .where(
            CampaignContact.contact_id.in_(contact_ids),
            Campaign.status != CampaignStatus.ARCHIVED,
        )
        .group_by(CampaignContact.contact_id, expression)
    ).all()
    tally: dict[uuid.UUID, dict[str, int]] = {}
    for contact_id, bucket, count in rows:
        tally.setdefault(contact_id, {})[str(bucket)] = int(count)
    out: dict[uuid.UUID, customer_status.CustomerProgress] = {}
    for contact_id, buckets in tally.items():
        processing = buckets.get(CustomerContactStatus.PROCESSING.value, 0)
        ready = buckets.get(CustomerContactStatus.READY_FOR_SENDING.value, 0)
        stopped = buckets.get(CustomerContactStatus.COULD_NOT_PREPARE.value, 0)
        out[contact_id] = customer_status.CustomerProgress(
            total=processing + ready + stopped,
            processing=processing,
            ready_for_sending=ready,
            could_not_prepare=stopped,
        )
    return out


def campaign_summary(progress: customer_status.CustomerProgress | None) -> str:
    """'1 ready · 2 processing' — clearly Campaign counts, or 'Not in a Campaign'."""

    if progress is None or progress.total == 0:
        return "Not in a Campaign"
    parts: list[str] = []
    if progress.ready_for_sending:
        parts.append(f"{progress.ready_for_sending} ready")
    if progress.processing:
        parts.append(f"{progress.processing} processing")
    if progress.could_not_prepare:
        parts.append(f"{progress.could_not_prepare} could not prepare")
    return " · ".join(parts)


@dataclass(frozen=True)
class MembershipRow:
    """One Campaign this person belongs to, in customer words."""

    campaign: Campaign
    membership: CampaignContact
    outcome: CustomerContactStatus
    detail: str
    lifecycle: str
    progress: email_progress.PersonProgress | None = None

    @property
    def outcome_label(self) -> str:
        return customer_status.STATUS_LABELS[self.outcome]

    @property
    def ready(self) -> bool:
        return self.outcome is CustomerContactStatus.READY_FOR_SENDING

    @property
    def open_url(self) -> str:
        base = f"/app/campaigns/{self.campaign.id}"
        if self.ready:
            return f"{base}?section=all&person={self.membership.id}#ready"
        return f"{base}/people"

    @property
    def next_line(self) -> str:
        if self.progress is None:
            return ""
        p = self.progress
        return f"{p.next_label} · {p.due_label} · {p.progress_label}"


def memberships_for(session: Session, contact: Contact) -> list[MembershipRow]:
    """Every Campaign membership of one person, newest first."""

    rows = session.execute(
        select(CampaignContact, Campaign)
        .join(Campaign, Campaign.id == CampaignContact.campaign_id)
        .where(CampaignContact.contact_id == contact.id)
        .order_by(CampaignContact.enrolled_at.desc())
    ).all()
    if not rows:
        return []
    membership_ids = [membership.id for membership, _campaign in rows]
    people_rows: dict[uuid.UUID, campaign_workspace.PersonRow] = {}
    for campaign_id in {campaign.id for _membership, campaign in rows}:
        found, _total = campaign_workspace.people(
            session, campaign_id=campaign_id, search=None, limit=1000
        )
        for row in found:
            if row.membership_id in membership_ids:
                people_rows[row.membership_id] = row
    progress = email_progress.progress_for_memberships(
        session,
        [mid for mid, row in people_rows.items() if row.ready],
    )
    built: list[MembershipRow] = []
    for membership, campaign in rows:
        person = people_rows.get(membership.id)
        outcome = person.outcome if person else CustomerContactStatus.PROCESSING
        built.append(
            MembershipRow(
                campaign=campaign,
                membership=membership,
                outcome=outcome,
                detail=person.detail if person else "",
                lifecycle=campaign_workspace.lifecycle(campaign),
                progress=progress.get(membership.id),
            )
        )
    return built


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------

WEBSITE_CONFIRMED = "Confirmed"
WEBSITE_BEST = "Best available"
WEBSITE_MISSING = "Missing"


def website_label(company: Company, decision_state: DomainResolutionState | None) -> str:
    """Confirmed / Best available / Missing — the customer's three words for a website."""

    if not company.domain:
        return WEBSITE_MISSING
    if decision_state is DomainResolutionState.PROVISIONAL:
        return WEBSITE_BEST
    if decision_state is DomainResolutionState.UNRESOLVED:
        return WEBSITE_BEST
    return WEBSITE_CONFIRMED


_STATE_RANK: dict[DomainResolutionState, int] = {
    DomainResolutionState.CONFIRMED: 3,
    DomainResolutionState.PROVISIONAL: 2,
    DomainResolutionState.UNRESOLVED: 1,
}


def website_labels(session: Session, companies: list[Company]) -> dict[uuid.UUID, str]:
    """Confirmed / Best available / Missing per Company, in one query."""

    if not companies:
        return {}
    ids = [company.id for company in companies]
    rows = session.execute(
        select(CompanyDomainResolution.resolved_company_id, CompanyDomainResolution.state).where(
            CompanyDomainResolution.resolved_company_id.in_(ids),
            CompanyDomainResolution.is_current.is_(True),
        )
    ).all()
    strongest: dict[uuid.UUID, DomainResolutionState] = {}
    for company_id, state in rows:
        current = strongest.get(company_id)
        if current is None or _STATE_RANK.get(state, 0) > _STATE_RANK.get(current, 0):
            strongest[company_id] = state
    return {company.id: website_label(company, strongest.get(company.id)) for company in companies}


@dataclass(frozen=True)
class Fact:
    """One thing we know about a Company, with where it came from."""

    subject: str
    text: str
    provenance: str  # Researched evidence · Classification · Captured profile · Manual
    detail: str | None = None
    updated_at: datetime | None = None


PROVENANCE_RESEARCH = "Researched evidence"
PROVENANCE_CLASSIFICATION = "Classification"
PROVENANCE_CAPTURED = "Captured profile"
PROVENANCE_MANUAL = "Manual"


def _source_label(kind: CompanyFieldSource | None) -> str:
    if kind is None:
        return PROVENANCE_CAPTURED
    if kind is CompanyFieldSource.MANUAL:
        return PROVENANCE_MANUAL
    if "DOSSIER" in kind.name or "RESEARCH" in kind.name:
        return PROVENANCE_RESEARCH
    return PROVENANCE_CAPTURED


def what_we_know(
    session: Session,
    *,
    company: Company,
    field_provenance: list[Any],
    dossier_sections: list[dict[str, Any]],
    intelligence: Any | None,
) -> list[Fact]:
    """One surface: canonical fields, researched sections and classifications."""

    facts: list[Fact] = []
    for view in field_provenance:
        value = getattr(view, "current_value", None)
        if not value:
            continue
        winner = getattr(view, "winner", None)
        kind = getattr(winner, "source_kind", None)
        facts.append(
            Fact(
                subject=str(getattr(view, "field_name", "")).replace("_", " ").capitalize(),
                text=safe_text(value),
                provenance=_source_label(kind),
                updated_at=getattr(winner, "observed_at", None),
            )
        )
    classifications = getattr(intelligence, "classifications", None) or ()
    if intelligence is not None:
        for classification in classifications:
            if getattr(classification, "operator_only", False):
                continue
            state = getattr(classification, "state", None)
            if state is not None and getattr(state, "value", "") in {"rejected", "excluded"}:
                continue
            facts.append(
                Fact(
                    subject=str(classification.dimension.value).replace("_", " ").capitalize(),
                    text=safe_text(classification.display_value),
                    provenance=PROVENANCE_CLASSIFICATION,
                    detail=(
                        f"{classification.confidence_band.value.replace('_', ' ')} confidence"
                        if getattr(classification, "confidence_band", None)
                        else None
                    ),
                )
            )
    for section in dossier_sections:
        if not section.get("addressed"):
            continue
        claims = _dossier_claims(section.get("raw"))
        if not claims:
            continue
        sources = {source for _text, source in claims if source}
        facts.append(
            Fact(
                subject=str(section.get("name", "")).capitalize(),
                text=" ".join(text for text, _source in claims[:3]),
                provenance=PROVENANCE_RESEARCH,
                detail=_claims_detail(len(claims), len(sources)),
            )
        )
    return facts


def _dossier_claims(raw: Any) -> list[tuple[str, str | None]]:
    """The human-readable claims in one dossier section, with their source URLs.

    A dossier stores each observation as a small record (value, worker,
    confidence, source, extraction method). The customer gets the value and,
    on expansion, the source; the machinery stays in Admin.
    """

    claims: list[tuple[str, str | None]] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            value = item.get("value") or item.get("text") or item.get("claim")
            if isinstance(value, str) and value.strip():
                claims.append((safe_text(value.strip()), item.get("source_url") or item.get("url")))
                return
            for nested in item.values():
                if isinstance(nested, (dict, list)):
                    visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)
        elif isinstance(item, str) and item.strip():
            claims.append((safe_text(item.strip()), None))

    visit(raw)
    return claims


def _claims_detail(count: int, sources: int) -> str | None:
    if count <= 3 and sources <= 1:
        return None
    parts = []
    if count > 3:
        parts.append(f"{count} recorded facts")
    if sources:
        parts.append(f"{sources} source{'s' if sources != 1 else ''}")
    return " · ".join(parts)


@dataclass(frozen=True)
class CampaignImpact:
    campaign: Campaign
    progress: customer_status.CustomerProgress
    lifecycle: str


def campaign_impact(session: Session, *, company_id: uuid.UUID) -> list[CampaignImpact]:
    """Which Campaigns include people from this Company, with three-state counts."""

    expression = customer_status.status_expression()
    rows = session.execute(
        select(Campaign, expression, func.count(CampaignContact.id))
        .select_from(CampaignContact)
        .join(Contact, Contact.id == CampaignContact.contact_id)
        .join(Campaign, Campaign.id == CampaignContact.campaign_id)
        .where(Contact.company_id == company_id)
        .group_by(Campaign.id, expression)
    ).all()
    tally: dict[uuid.UUID, tuple[Campaign, dict[str, int]]] = {}
    for campaign, bucket, count in rows:
        entry = tally.setdefault(campaign.id, (campaign, {}))
        entry[1][str(bucket)] = int(count)
    impacts: list[CampaignImpact] = []
    for campaign, buckets in tally.values():
        processing = buckets.get(CustomerContactStatus.PROCESSING.value, 0)
        ready = buckets.get(CustomerContactStatus.READY_FOR_SENDING.value, 0)
        stopped = buckets.get(CustomerContactStatus.COULD_NOT_PREPARE.value, 0)
        impacts.append(
            CampaignImpact(
                campaign=campaign,
                progress=customer_status.CustomerProgress(
                    total=processing + ready + stopped,
                    processing=processing,
                    ready_for_sending=ready,
                    could_not_prepare=stopped,
                ),
                lifecycle=campaign_workspace.lifecycle(campaign),
            )
        )
    impacts.sort(key=lambda item: item.campaign.name.lower())
    return impacts


def active_campaign_counts(session: Session, company_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not company_ids:
        return {}
    rows = session.execute(
        select(Contact.company_id, func.count(func.distinct(CampaignContact.campaign_id)))
        .select_from(CampaignContact)
        .join(Contact, Contact.id == CampaignContact.contact_id)
        .join(Campaign, Campaign.id == CampaignContact.campaign_id)
        .where(Contact.company_id.in_(company_ids), Campaign.status != CampaignStatus.ARCHIVED)
        .group_by(Contact.company_id)
    ).all()
    return {company_id: int(count) for company_id, count in rows}


__all__ = [
    "CampaignImpact",
    "Fact",
    "MembershipRow",
    "WEBSITE_BEST",
    "WEBSITE_CONFIRMED",
    "WEBSITE_MISSING",
    "active_campaign_counts",
    "campaign_counts_by_contact",
    "campaign_impact",
    "campaign_summary",
    "memberships_for",
    "website_label",
    "website_labels",
    "what_we_know",
]

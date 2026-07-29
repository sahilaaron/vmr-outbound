"""The company detail workspace read model (APP-003).

Assembles everything ``/companies/{id}`` shows, in one place, so the template
renders and does not decide. Mirrors :mod:`app.services.crm.detail`.

Two things this module is careful about:

* **Linked contacts come from the permanent edge.** Contacts reachable only by
  matching the domain string are listed too, but in their own group and clearly
  labelled transitional — because "we have not linked this person" and "this
  person works here" are different claims and merging the lists would assert the
  second while only knowing the first.
* **A dossier is shown as a claim.** Sections are displayed with their source
  submission and their warnings, never merged into the canonical fields. What
  won a canonical field is a separate question answered by the provenance
  ledger, and both answers appear on the page.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.contact import Contact
from app.models.enums import ResearchState
from app.services.companies import conflicts as company_conflicts
from app.services.companies import dossiers as company_dossiers
from app.services.companies import provenance as company_provenance
from app.services.resolution import gates as resolution_gates
from app.services.resolution import service as resolution_service

# Shown where a research engine would report, so the page never implies one
# exists. APP-004 owns building it.
RESEARCH_NOT_BUILT = (
    "No research engine exists yet. A dossier can be submitted and interpreted; "
    "nothing produces one automatically."
)

TRANSITIONAL_LINK_NOTE = (
    "Matched by company domain string only, with no permanent link. This is the "
    "pre-APP-003 path and is shown so unresolved people stay visible; it is not "
    "evidence that they work here."
)


@dataclass(frozen=True)
class LinkedContact:
    """One person at this company, and how we know."""

    contact: Contact
    is_permanent_link: bool

    @property
    def display_name(self) -> str:
        return (
            " ".join(part for part in (self.contact.first_name, self.contact.last_name) if part)
            or "(name not captured)"
        )


@dataclass(frozen=True)
class CompanyDetailView:
    """Everything the workspace displays about one company."""

    company: Company
    linked_contacts: list[LinkedContact]
    transitional_contacts: list[LinkedContact]
    field_provenance: list[company_provenance.CompanyFieldProvenanceView]
    dossier_versions: list[company_dossiers.DossierSummary]
    current_dossier: company_dossiers.DossierSummary | None
    conflicts: list[company_conflicts.CompanyConflict]
    research_note: str
    # How this company's domain was decided (DAT-017A), and whether that is
    # settled enough to research. ``domain_resolution`` is None for a company
    # whose domain never came from automatic resolution — an import, an
    # operator, a pre-DAT-017A promotion — which is a different statement from
    # "resolved and uncertain" and is shown as one.
    domain_resolution: resolution_service.DecisionView | None = None
    research_readiness: resolution_gates.ResearchReadiness | None = None

    @property
    def linked_count(self) -> int:
        return len(self.linked_contacts)

    @property
    def is_researched(self) -> bool:
        return self.company.research_state in (
            ResearchState.COMPLETED,
            ResearchState.COMPLETED_WITH_WARNINGS,
        )

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)


def _linked(session: Session, company: Company, *, limit: int = 200) -> list[LinkedContact]:
    rows = session.scalars(
        select(Contact)
        .where(Contact.company_id == company.id, Contact.merged_into_id.is_(None))
        .order_by(func.lower(Contact.last_name), func.lower(Contact.first_name), Contact.id)
        .limit(limit)
    )
    return [LinkedContact(contact=c, is_permanent_link=True) for c in rows]


def _transitional(session: Session, company: Company, *, limit: int = 200) -> list[LinkedContact]:
    """Contacts carrying this domain that nothing has linked.

    Kept separate rather than folded into the linked list. The backfill declined
    to guess at these, and presenting a guess in the UI would undo that decision
    in the one place an operator would believe it.
    """

    if not company.domain:
        return []
    rows = session.scalars(
        select(Contact)
        .where(
            Contact.company_domain == company.domain,
            Contact.company_id.is_(None),
            Contact.merged_into_id.is_(None),
        )
        .order_by(func.lower(Contact.last_name), func.lower(Contact.first_name), Contact.id)
        .limit(limit)
    )
    return [LinkedContact(contact=c, is_permanent_link=False) for c in rows]


def get_company_detail(session: Session, company_id: uuid.UUID) -> CompanyDetailView | None:
    """The full workspace view, or None when the company does not exist."""

    company = session.get(Company, company_id)
    if company is None:
        return None

    versions = company_dossiers.list_versions(session, company_id=company.id)
    current = next((s for s in versions if s.version.is_current), None)

    return CompanyDetailView(
        company=company,
        linked_contacts=_linked(session, company),
        transitional_contacts=_transitional(session, company),
        field_provenance=company_provenance.explain_all(session, company=company),
        dossier_versions=versions,
        current_dossier=current,
        conflicts=company_conflicts.for_company(session, company=company),
        research_note=RESEARCH_NOT_BUILT,
        domain_resolution=resolution_service.company_view(session, company.id),
        research_readiness=resolution_gates.research_readiness(
            session, company_id=company.id, domain=company.domain
        ),
    )

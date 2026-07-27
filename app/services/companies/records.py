"""The permanent company list (APP-003).

A read model for ``/companies``. Mirrors the shape of
:mod:`app.services.crm.records` so the two lists behave the same way and a saved
audience can later reuse these predicates rather than re-derive them from query
strings.

No filter is a campaign, and there is no campaign column to filter on. Companies
exist independently of whether anyone has ever run outreach at them.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Subquery

from app.models.company import Company
from app.models.company_dossier import CompanyDossierVersion
from app.models.contact import Contact
from app.models.enums import ResearchState
from app.services.companies import conflicts as company_conflicts

VIEW_ALL = "all"
VIEW_WITH_CONTACTS = "with_contacts"
VIEW_UNRESOLVED_DOMAIN = "unresolved_domain"
VIEW_RESEARCHED = "researched"
VIEW_CONFLICTED = "conflicted"
VIEWS: tuple[str, ...] = (
    VIEW_ALL,
    VIEW_WITH_CONTACTS,
    VIEW_UNRESOLVED_DOMAIN,
    VIEW_RESEARCHED,
    VIEW_CONFLICTED,
)

SORT_RECENT = "recent"
SORT_NAME = "name"
SORT_CONTACTS = "contacts"
SORTS: tuple[str, ...] = (SORT_RECENT, SORT_NAME, SORT_CONTACTS)


@dataclass(frozen=True)
class CompanyRow:
    """One company as the list shows it."""

    company: Company
    contact_count: int
    dossier_count: int
    conflict_count: int

    @property
    def has_domain(self) -> bool:
        return bool(self.company.domain)

    @property
    def is_researched(self) -> bool:
        return self.company.research_state in (
            ResearchState.COMPLETED,
            ResearchState.COMPLETED_WITH_WARNINGS,
        )


@dataclass(frozen=True)
class CompanyFilters:
    """Every filter the list supports. None of them is a campaign."""

    view: str = VIEW_ALL
    search: str | None = None
    research_state: ResearchState | None = None
    has_linkedin: bool | None = None
    sort: str = SORT_RECENT

    def normalized(self) -> CompanyFilters:
        """Coerce anything unrecognised back to a safe default.

        Query strings are operator input and get edited by hand; an unknown view
        should show the default working set rather than an error page.
        """

        return CompanyFilters(
            view=self.view if self.view in VIEWS else VIEW_ALL,
            search=(self.search or "").strip() or None,
            research_state=self.research_state,
            has_linkedin=self.has_linkedin,
            sort=self.sort if self.sort in SORTS else SORT_RECENT,
        )


def _contact_count_subquery() -> Subquery:
    """Live contacts per company, by the permanent edge only.

    Merged tombstones are excluded: a duplicate that was merged away is not a
    second person at the company.

    Contacts linked only by domain string are NOT counted here. They are real and
    they are reported — as a conflict, by
    :mod:`app.services.companies.conflicts` — but counting them as linked would
    hide the fact that nothing has actually resolved them.
    """

    return (
        select(Contact.company_id.label("company_id"), func.count().label("n"))
        .where(Contact.company_id.is_not(None), Contact.merged_into_id.is_(None))
        .group_by(Contact.company_id)
        .subquery()
    )


def _dossier_count_subquery() -> Subquery:
    return (
        select(CompanyDossierVersion.company_id.label("company_id"), func.count().label("n"))
        .group_by(CompanyDossierVersion.company_id)
        .subquery()
    )


def list_company_rows(
    session: Session,
    *,
    filters: CompanyFilters,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[CompanyRow], int]:
    """One page of companies plus the total matching the filters."""

    f = filters.normalized()
    contacts = _contact_count_subquery()
    dossiers = _dossier_count_subquery()

    contact_n = func.coalesce(contacts.c.n, 0)
    dossier_n = func.coalesce(dossiers.c.n, 0)

    stmt = (
        select(Company, contact_n.label("contact_count"), dossier_n.label("dossier_count"))
        .outerjoin(contacts, contacts.c.company_id == Company.id)
        .outerjoin(dossiers, dossiers.c.company_id == Company.id)
    )

    if f.view == VIEW_WITH_CONTACTS:
        stmt = stmt.where(contact_n > 0)
    elif f.view == VIEW_UNRESOLVED_DOMAIN:
        stmt = stmt.where(or_(Company.domain.is_(None), Company.domain == ""))
    elif f.view == VIEW_RESEARCHED:
        stmt = stmt.where(dossier_n > 0)
    elif f.view == VIEW_CONFLICTED:
        # The cheap, indexable half of "conflicted": no canonical domain, or a
        # linked contact that captured a different one. The full derivation is
        # per company and lives in the conflicts module; using it as a SQL
        # filter would mean loading every company to paginate.
        mismatched = (
            select(Contact.company_id)
            .where(
                Contact.company_id.is_not(None),
                Contact.merged_into_id.is_(None),
                Contact.company_domain != func.coalesce(Company.domain, ""),
            )
            .correlate(Company)
            .exists()
        )
        stmt = stmt.where(or_(Company.domain.is_(None), Company.domain == "", mismatched))

    if f.search:
        pattern = f"%{f.search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Company.name).like(pattern),
                func.lower(func.coalesce(Company.domain, "")).like(pattern),
            )
        )
    if f.research_state is not None:
        stmt = stmt.where(Company.research_state == f.research_state)
    if f.has_linkedin is True:
        stmt = stmt.where(Company.linkedin_company_url.is_not(None))
    elif f.has_linkedin is False:
        stmt = stmt.where(Company.linkedin_company_url.is_(None))

    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    if f.sort == SORT_NAME:
        stmt = stmt.order_by(func.lower(Company.name), Company.id)
    elif f.sort == SORT_CONTACTS:
        stmt = stmt.order_by(contact_n.desc(), func.lower(Company.name), Company.id)
    else:
        stmt = stmt.order_by(Company.updated_at.desc(), Company.id)

    results = list(session.execute(stmt.limit(limit).offset(offset)))
    company_ids = [company.id for company, _c, _d in results]
    conflict_counts = company_conflicts.count_for_companies(session, company_ids=company_ids)

    rows = [
        CompanyRow(
            company=company,
            contact_count=int(contact_count or 0),
            dossier_count=int(dossier_count or 0),
            conflict_count=conflict_counts.get(company.id, 0),
        )
        for company, contact_count, dossier_count in results
    ]
    return rows, int(total)

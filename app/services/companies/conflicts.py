"""Company identity disagreements, made visible (APP-003).

Every conflict here is **derived** from rows that already exist. There is no
conflict table and no second review queue, and that is a decision rather than an
omission:

* A stored queue needs someone to close its rows. A derived view stops reporting
  a conflict the instant the underlying records agree, which is the behaviour an
  operator actually wants from "is this still a problem?".
* The repository already has one review queue, bound to import rows. A second
  architecture beside it would be two places to look and two places to forget.
* A conflict that cannot be re-derived from the data was never really evidence.

None of these block anything. A company whose sources disagree about its domain
is a fact worth showing, not an error worth swallowing, and the operator decides
what to do about it. This module reports; it does not resolve, merge or reject.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.contact import Contact
from app.models.enums import CompanyConflictKind
from app.models.linkedin_company import LinkedInCompanySnapshot

# Wording shown to the operator. Kept beside the detection so a new conflict
# kind cannot be added without saying, in words, what it means and why it
# matters.
CONFLICT_TITLES: dict[CompanyConflictKind, str] = {
    CompanyConflictKind.CONTACT_DOMAIN_MISMATCH: "Linked contacts captured a different domain",
    CompanyConflictKind.CONTACT_LINK_UNRESOLVED: "Contacts matching this domain are not linked",
    CompanyConflictKind.LINKEDIN_ID_SHARED: "Another company claims this LinkedIn identifier",
    CompanyConflictKind.SNAPSHOT_DOMAIN_MISMATCH: "A captured company page states another domain",
    CompanyConflictKind.NO_CANONICAL_DOMAIN: "No canonical domain",
}


@dataclass(frozen=True)
class CompanyConflict:
    """One disagreement, with enough context to act on it."""

    kind: CompanyConflictKind
    title: str
    detail: str
    count: int
    # Short, non-identifying pointers for the operator to follow. Never a
    # person's name — a conflict list is not a contact list.
    references: tuple[str, ...] = ()


def _contact_domain_mismatch(session: Session, company: Company) -> CompanyConflict | None:
    """Linked contacts whose captured domain is not this company's domain.

    Usually means the company's canonical domain was corrected after the contact
    was created. The contact is still linked — ``company_id`` is the edge — but
    the evidence it was created from now says something else, and that is worth
    an operator's eyes rather than a silent rewrite of captured evidence.
    """

    rows = list(
        session.scalars(
            select(Contact.company_domain)
            .where(
                Contact.company_id == company.id,
                Contact.merged_into_id.is_(None),
                Contact.company_domain != (company.domain or ""),
            )
            .distinct()
            .limit(10)
        )
    )
    if not rows:
        return None
    total = (
        session.scalar(
            select(func.count())
            .select_from(Contact)
            .where(
                Contact.company_id == company.id,
                Contact.merged_into_id.is_(None),
                Contact.company_domain != (company.domain or ""),
            )
        )
        or 0
    )
    return CompanyConflict(
        kind=CompanyConflictKind.CONTACT_DOMAIN_MISMATCH,
        title=CONFLICT_TITLES[CompanyConflictKind.CONTACT_DOMAIN_MISMATCH],
        detail=(
            "These contacts are linked to this company, but the domain captured with them "
            "is not this company's canonical domain. The captured value is evidence and is "
            "not rewritten."
        ),
        count=total,
        references=tuple(sorted(row for row in rows if row is not None)),
    )


def _contact_link_unresolved(session: Session, company: Company) -> CompanyConflict | None:
    """Contacts carrying this domain that are not linked to this company.

    Either legacy rows the backfill declined to guess at, or rows created while
    two companies shared a domain. Reported so the transitional domain-string
    path never becomes invisible: an unlinked contact is a link nobody has made,
    not a link that does not exist.
    """

    if not company.domain:
        return None
    total = (
        session.scalar(
            select(func.count())
            .select_from(Contact)
            .where(
                Contact.company_domain == company.domain,
                Contact.company_id.is_(None),
                Contact.merged_into_id.is_(None),
            )
        )
        or 0
    )
    if not total:
        return None
    return CompanyConflict(
        kind=CompanyConflictKind.CONTACT_LINK_UNRESOLVED,
        title=CONFLICT_TITLES[CompanyConflictKind.CONTACT_LINK_UNRESOLVED],
        detail=(
            "These contacts carry this company's domain but have no company link. They are "
            "reachable through the transitional domain match only, which is why they are "
            "listed here rather than counted as linked."
        ),
        count=total,
    )


def _linkedin_id_shared(session: Session, company: Company) -> CompanyConflict | None:
    """Another company row claiming the same LinkedIn company identifier."""

    if not company.linkedin_company_id:
        return None
    others = list(
        session.scalars(
            select(Company.domain)
            .where(
                Company.linkedin_company_id == company.linkedin_company_id,
                Company.id != company.id,
            )
            .limit(10)
        )
    )
    if not others:
        return None
    return CompanyConflict(
        kind=CompanyConflictKind.LINKEDIN_ID_SHARED,
        title=CONFLICT_TITLES[CompanyConflictKind.LINKEDIN_ID_SHARED],
        detail=(
            "More than one company record carries this LinkedIn identifier. One of them is "
            "wrong, or two records describe one organisation. Nothing is merged automatically."
        ),
        count=len(others),
        references=tuple(sorted(d or "(no domain)" for d in others)),
    )


def _snapshot_domain_mismatch(session: Session, company: Company) -> CompanyConflict | None:
    """A captured LinkedIn company page matched here that states another domain.

    The strongest signal available, because it is one source naming an
    organisation and a website together. When it disagrees with the canonical
    domain, one of the two is wrong and only an operator can say which.
    """

    rows = list(
        session.scalars(
            select(LinkedInCompanySnapshot.website_domain)
            .where(
                LinkedInCompanySnapshot.matched_company_id == company.id,
                LinkedInCompanySnapshot.website_domain.is_not(None),
                LinkedInCompanySnapshot.website_domain != (company.domain or ""),
            )
            .distinct()
            .limit(10)
        )
    )
    if not rows:
        return None
    return CompanyConflict(
        kind=CompanyConflictKind.SNAPSHOT_DOMAIN_MISMATCH,
        title=CONFLICT_TITLES[CompanyConflictKind.SNAPSHOT_DOMAIN_MISMATCH],
        detail=(
            "A company page captured for this company displays a website domain that is not "
            "the canonical one. The snapshot is immutable evidence and is never rewritten."
        ),
        count=len(rows),
        references=tuple(sorted(d for d in rows if d)),
    )


def _no_canonical_domain(session: Session, company: Company) -> CompanyConflict | None:
    """No domain at all, so domain identity cannot apply.

    Not an error. A company can be perfectly real and not yet resolved. It is
    listed because every domain-based check above silently does nothing here, and
    an operator should know that rather than read empty results as agreement.
    """

    if company.domain:
        return None
    linked = (
        session.scalar(
            select(func.count())
            .select_from(Contact)
            .where(Contact.company_id == company.id, Contact.merged_into_id.is_(None))
        )
        or 0
    )
    return CompanyConflict(
        kind=CompanyConflictKind.NO_CANONICAL_DOMAIN,
        title=CONFLICT_TITLES[CompanyConflictKind.NO_CANONICAL_DOMAIN],
        detail=(
            "This company has no canonical domain, so every domain-based identity check is "
            "silent rather than satisfied. Resolving the domain is the DAT-014 capture "
            "confirmation path."
        ),
        count=linked,
    )


_DETECTORS = (
    _no_canonical_domain,
    _snapshot_domain_mismatch,
    _linkedin_id_shared,
    _contact_domain_mismatch,
    _contact_link_unresolved,
)


def for_company(session: Session, *, company: Company) -> list[CompanyConflict]:
    """Every current disagreement about this company's identity."""

    found: list[CompanyConflict] = []
    for detect in _DETECTORS:
        conflict = detect(session, company)
        if conflict is not None:
            found.append(conflict)
    return found


def count_for_companies(session: Session, *, company_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Conflict counts for a page of companies, for the list view.

    Computed with three aggregate queries rather than one per company, so
    listing fifty companies does not mean two hundred round trips.
    """

    if not company_ids:
        return {}
    counts: dict[uuid.UUID, int] = dict.fromkeys(company_ids, 0)

    # Companies with no domain, or whose linked contacts captured another one.
    for company_id, domain in session.execute(
        select(Company.id, Company.domain).where(Company.id.in_(company_ids))
    ):
        if not domain:
            counts[company_id] += 1

    mismatched = session.execute(
        select(Contact.company_id, func.count())
        .join(Company, Company.id == Contact.company_id)
        .where(
            Contact.company_id.in_(company_ids),
            Contact.merged_into_id.is_(None),
            Contact.company_domain != func.coalesce(Company.domain, ""),
        )
        .group_by(Contact.company_id)
    )
    for company_id, _count in mismatched:
        if company_id is not None:
            counts[company_id] += 1

    shared = session.execute(
        select(LinkedInCompanySnapshot.matched_company_id, func.count())
        .join(Company, Company.id == LinkedInCompanySnapshot.matched_company_id)
        .where(
            LinkedInCompanySnapshot.matched_company_id.in_(company_ids),
            LinkedInCompanySnapshot.website_domain.is_not(None),
            LinkedInCompanySnapshot.website_domain != func.coalesce(Company.domain, ""),
        )
        .group_by(LinkedInCompanySnapshot.matched_company_id)
    )
    for company_id, _count in shared:
        if company_id is not None:
            counts[company_id] += 1

    return counts

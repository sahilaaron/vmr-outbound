"""Which company an imported row is about (IMP-001 §10).

The rule this module exists to enforce, stated once:

    ``Company Name`` is not evidence of company identity.

An Apollo export routinely carries a name that disagrees with everything else in
the row — the worked example from the specification has ``AGILENT TECHNOLOGIES``
next to ``twnoyes@llbean.com``, ``https://llbean.com`` and L.L.Bean's LinkedIn
page. Three independent signals agree there and the name is the outlier, so the
row resolves to L.L.Bean and the supplied name is kept as evidence with a
warning. A resolver that trusted the name would have filed a Bean employee under
Agilent and nothing downstream could have noticed.

Resolution is in two halves on purpose. :func:`plan` is a pure read: it looks
things up, decides, and writes nothing, so the preview screen can show exactly
what confirmation will do without creating a single row. :func:`apply` takes a
plan and materializes it. The preview and the commit therefore cannot drift,
because the commit has no decision logic of its own to drift with.

Ambiguity is never resolved by ranking. When two identity signals point at two
different permanent Companies, that is a fact about the data, and the row is held
for an operator rather than filed under whichever signal this module happened to
check first.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.imported_email import ImportSourceIdentifier
from app.services.audit import record_audit_event
from app.services.imports import normalization as norm
from app.services.imports.apollo import ApolloRow, is_public_email_domain

APOLLO_SYSTEM = "apollo"
ACCOUNT_ID_KIND = "account_id"


class CompanyAction(enum.StrEnum):
    """What confirming this row would do about its company."""

    MATCH_EXISTING = "match_existing"
    CREATE = "create"
    REVIEW_REQUIRED = "review_required"


class CompanyBasis(enum.StrEnum):
    """Which evidence decided the company, in the IMP-001 §10 hierarchy order."""

    APOLLO_ACCOUNT_ID = "apollo_account_id"
    WEBSITE_DOMAIN = "website_domain"
    COMPANY_LINKEDIN = "company_linkedin"
    EMAIL_DOMAIN_AGREEMENT = "email_domain_agreement"
    EMAIL_DOMAIN = "email_domain"
    NAME_AND_DOMAIN_AGREEMENT = "name_and_domain_agreement"


#: Strongest first. Used only to *report* which evidence carried a decision that
#: the signals already agreed on — never to break a disagreement between them.
_BASIS_PRIORITY: tuple[CompanyBasis, ...] = (
    CompanyBasis.APOLLO_ACCOUNT_ID,
    CompanyBasis.WEBSITE_DOMAIN,
    CompanyBasis.COMPANY_LINKEDIN,
    CompanyBasis.EMAIL_DOMAIN_AGREEMENT,
    CompanyBasis.EMAIL_DOMAIN,
    CompanyBasis.NAME_AND_DOMAIN_AGREEMENT,
)


@dataclass
class CompanyPlan:
    """What :func:`apply` will do, decided without writing anything."""

    action: CompanyAction
    basis: CompanyBasis | None = None
    company_id: uuid.UUID | None = None
    company_name: str | None = None
    #: The domain the row resolves to, or the existing company's domain.
    domain: str | None = None
    linkedin_url: str | None = None
    linkedin_id: str | None = None
    #: Set only when the action is REVIEW_REQUIRED.
    review_code: str | None = None
    review_detail: str | None = None
    warnings: list[tuple[str, str]] = field(default_factory=list)
    #: Every signal that pointed at a permanent Company, for the preview.
    evidence: dict[str, str] = field(default_factory=dict)

    @property
    def needs_review(self) -> bool:
        return self.action is CompanyAction.REVIEW_REQUIRED


def _linkedin_slug(identity_url: str | None) -> str | None:
    if identity_url is None:
        return None
    return identity_url.rstrip("/").rpartition("/")[2] or None


def _registrable_label(domain: str | None) -> str | None:
    """The leading label of a hostname — ``llbean`` from ``llbean.com``.

    A deliberately naive reading, used for one purpose only: deciding whether to
    show the operator a warning that the supplied company name looks unrelated to
    the domain the row resolved to. It never decides anything. A proper public
    suffix list would make the warning marginally better worded and would not
    change a single import outcome.
    """

    if not domain:
        return None
    return domain.split(".", 1)[0] or None


def _name_tokens(name: str | None) -> set[str]:
    if not name:
        return set()
    cleaned = "".join(ch if ch.isalnum() else " " for ch in name.casefold())
    #: Corporate suffixes carry no identity: every second company is an "inc".
    noise = {"inc", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "plc", "gmbh"}
    return {token for token in cleaned.split() if len(token) > 2 and token not in noise}


def name_agrees_with_domain(name: str | None, domain: str | None) -> bool:
    """Whether a company name and a domain plausibly describe the same company.

    Used only to raise or withhold a warning. ``True`` when the domain's label
    contains a name token or a name token contains the label — so ``L.L.Bean`` /
    ``llbean.com`` agrees and ``Agilent Technologies`` / ``llbean.com`` does not.
    """

    label = _registrable_label(domain)
    if label is None:
        return True  # nothing to disagree with
    tokens = _name_tokens(name)
    if not tokens:
        return True  # nothing usable to compare; do not manufacture a conflict
    if any(token in label or label in token for token in tokens):
        return True
    # A domain that runs the name together — "logmein.com" for "Log Me In".
    return label in "".join(sorted(tokens))


def _company_by_account_id(session: Session, account_id: str | None) -> Company | None:
    if not account_id:
        return None
    identifier = session.scalars(
        select(ImportSourceIdentifier).where(
            ImportSourceIdentifier.system == APOLLO_SYSTEM,
            ImportSourceIdentifier.identifier_kind == ACCOUNT_ID_KIND,
            ImportSourceIdentifier.identifier_value == account_id,
            ImportSourceIdentifier.company_id.is_not(None),
        )
    ).first()
    if identifier is None or identifier.company_id is None:
        return None
    return session.get(Company, identifier.company_id)


def _company_by_domain(session: Session, domain: str | None) -> Company | None:
    if not domain:
        return None
    return session.scalars(select(Company).where(Company.domain == domain)).first()


def _company_by_linkedin(session: Session, identity_url: str | None) -> Company | None:
    if not identity_url:
        return None
    slug = _linkedin_slug(identity_url)
    conditions = [Company.linkedin_company_url == identity_url]
    if slug:
        conditions.append(Company.linkedin_company_id == slug)
    return session.scalars(select(Company).where(or_(*conditions))).first()


def _canonical_domain(row: ApolloRow, plan: CompanyPlan) -> tuple[str | None, CompanyBasis | None]:
    """Choose the domain a NEW company would be created with, or refuse to.

    The refusals are the interesting part. A website and a non-public email
    domain that disagree are two claims about where this person works, and
    nothing in the row settles which is right — so the row waits for a human. A
    row whose only address is at a public mailbox provider has no company signal
    in its address at all, which is a different problem with the same answer.
    """

    website = row.website_domain
    email_domain = row.primary.domain if row.primary is not None else None
    public_email = is_public_email_domain(email_domain)

    if website and email_domain and not public_email:
        if website == email_domain:
            return website, CompanyBasis.EMAIL_DOMAIN_AGREEMENT
        plan.action = CompanyAction.REVIEW_REQUIRED
        plan.review_code = "company_domain_conflict"
        plan.review_detail = (
            f"The Website ({website}) and the email domain ({email_domain}) name two "
            "different companies and nothing in the row settles which is right."
        )
        return None, None

    if website:
        if public_email:
            plan.warnings.append(
                (
                    "public_email_domain",
                    f"The primary address is at the public mailbox provider {email_domain}, "
                    "which establishes nothing about the company. The Website was used "
                    "for company identity instead.",
                )
            )
        return website, CompanyBasis.WEBSITE_DOMAIN

    if email_domain and not public_email:
        return email_domain, CompanyBasis.EMAIL_DOMAIN

    plan.action = CompanyAction.REVIEW_REQUIRED
    if public_email:
        plan.review_code = "public_email_no_company_signal"
        plan.review_detail = (
            f"The only address is at the public mailbox provider {email_domain}, and the "
            "row carries no Website, Company LinkedIn URL or Apollo Account Id that "
            "matches a known company. A public mailbox cannot establish an employer."
        )
    else:
        plan.review_code = "company_domain_unavailable"
        plan.review_detail = (
            "The row carries no Website and no usable email domain, so there is no "
            "company domain to create this company with."
        )
    return None, None


def plan(session: Session, row: ApolloRow) -> CompanyPlan:
    """Decide the row's company without writing anything."""

    result = CompanyPlan(action=CompanyAction.CREATE)

    if not row.company_name and not row.website_domain and not row.company_linkedin_identity:
        result.action = CompanyAction.REVIEW_REQUIRED
        result.review_code = "company_identity_missing"
        result.review_detail = (
            "The row names no company: no Company Name, no Website and no Company LinkedIn URL."
        )
        return result

    email_domain = row.primary.domain if row.primary is not None else None
    lookup_email_domain = None if is_public_email_domain(email_domain) else email_domain

    # --- Every signal that already names a permanent Company ------------------
    matches: list[tuple[CompanyBasis, Company]] = []
    for basis, company in (
        (CompanyBasis.APOLLO_ACCOUNT_ID, _company_by_account_id(session, row.apollo_account_id)),
        (CompanyBasis.WEBSITE_DOMAIN, _company_by_domain(session, row.website_domain)),
        (
            CompanyBasis.COMPANY_LINKEDIN,
            _company_by_linkedin(session, row.company_linkedin_identity),
        ),
        (CompanyBasis.EMAIL_DOMAIN, _company_by_domain(session, lookup_email_domain)),
    ):
        if company is not None:
            matches.append((basis, company))
            result.evidence[basis.value] = str(company.id)

    distinct = {company.id for _basis, company in matches}
    if len(distinct) > 1:
        result.action = CompanyAction.REVIEW_REQUIRED
        result.review_code = "company_identity_ambiguous"
        named = ", ".join(
            sorted({f"{company.name} ({company.domain or 'no domain'})" for _b, company in matches})
        )
        result.review_detail = (
            f"The row's company signals point at {len(distinct)} different permanent "
            f"companies: {named}. Merging them on this evidence would be a guess."
        )
        return result

    if distinct:
        basis, company = next((b, c) for pref in _BASIS_PRIORITY for b, c in matches if b is pref)
        result.action = CompanyAction.MATCH_EXISTING
        result.basis = basis
        result.company_id = company.id
        result.company_name = company.name
        result.domain = company.domain
        result.linkedin_url = company.linkedin_company_url
        _warn_on_matched_company(result, row, company)
        return result

    # --- Nothing exists yet: decide what creating one would mean --------------
    domain, create_basis = _canonical_domain(row, result)
    if result.needs_review:
        return result
    if not row.company_name:
        result.action = CompanyAction.REVIEW_REQUIRED
        result.review_code = "company_name_missing"
        result.review_detail = (
            "A new company cannot be created without a Company Name, and no existing "
            "company matched this row's identity signals."
        )
        return result

    result.action = CompanyAction.CREATE
    result.basis = create_basis
    result.company_name = row.company_name
    result.domain = domain
    result.linkedin_url = row.company_linkedin_identity or row.company_linkedin_url
    result.linkedin_id = _linkedin_slug(row.company_linkedin_identity)
    if not name_agrees_with_domain(row.company_name, domain):
        result.warnings.append(
            (
                "supplied_company_name_conflict",
                f"The supplied Company Name {row.company_name!r} does not look related to "
                f"{domain}, which the Website and email domain agree on. The domain "
                "evidence was used; the supplied name was kept as source evidence.",
            )
        )
    return result


def _warn_on_matched_company(result: CompanyPlan, row: ApolloRow, company: Company) -> None:
    """Note where the row disagrees with the company it matched.

    None of these change the match. A permanent Company that several signals
    already name is the right answer even when one supplied cell disagrees; what
    the operator needs is to be told which cell.
    """

    email_domain = row.primary.domain if row.primary is not None else None
    if row.website_domain and company.domain and row.website_domain != company.domain:
        result.warnings.append(
            (
                "matched_company_domain_mismatch",
                f"The row's Website ({row.website_domain}) differs from the matched "
                f"company's domain ({company.domain}). The existing company was kept.",
            )
        )
    if is_public_email_domain(email_domain):
        result.warnings.append(
            (
                "public_email_domain",
                f"The primary address is at the public mailbox provider {email_domain}. "
                f"Company identity came from {result.basis.value if result.basis else 'other'} "
                "evidence instead.",
            )
        )
    elif email_domain and company.domain and email_domain != company.domain:
        result.warnings.append(
            (
                "matched_company_email_domain_mismatch",
                f"The primary address is at {email_domain}, which is not the matched "
                f"company's domain ({company.domain}).",
            )
        )
    if row.company_name and not name_agrees_with_domain(row.company_name, company.domain):
        result.warnings.append(
            (
                "supplied_company_name_conflict",
                f"The supplied Company Name {row.company_name!r} does not look related to "
                f"the matched company {company.name!r} ({company.domain}). The identity "
                "evidence was used; the supplied name was kept as source evidence.",
            )
        )


def apply(
    session: Session,
    *,
    company_plan: CompanyPlan,
    row: ApolloRow,
    batch_id: uuid.UUID,
    actor: str,
) -> Company | None:
    """Materialize *company_plan*. Returns ``None`` for a review-required plan."""

    if company_plan.action is CompanyAction.REVIEW_REQUIRED:
        return None
    if company_plan.action is CompanyAction.MATCH_EXISTING:
        company = session.get(Company, company_plan.company_id)
        if company is not None:
            _record_account_id(session, company=company, row=row, batch_id=batch_id, actor=actor)
        return company

    assert company_plan.company_name is not None
    company = Company(
        name=company_plan.company_name,
        domain=company_plan.domain,
        industry=row.industry,
        country=row.company_country,
        company_size=row.employee_count,
        linkedin_company_url=company_plan.linkedin_url,
        linkedin_company_id=company_plan.linkedin_id,
    )
    try:
        with session.begin_nested():
            session.add(company)
            session.flush()
    except IntegrityError:
        # A concurrent row in the same batch created it first. The partial unique
        # index on ``domain`` is what made that safe to attempt; reuse the winner
        # rather than failing a perfectly valid row.
        existing = _company_by_domain(session, company_plan.domain)
        if existing is None:  # pragma: no cover - defensive
            raise
        company_plan.action = CompanyAction.MATCH_EXISTING
        company_plan.company_id = existing.id
        _record_account_id(session, company=existing, row=row, batch_id=batch_id, actor=actor)
        return existing

    company_plan.company_id = company.id
    record_audit_event(
        session,
        actor=actor,
        action="company.created",
        entity_type="company",
        entity_id=str(company.id),
        new_state=company.domain or "no domain",
        reason="company created from a campaign-bound contact file import",
        context={
            "import_batch_id": str(batch_id),
            "source_row_number": row.row_number,
            "identity_basis": company_plan.basis.value if company_plan.basis else None,
        },
    )
    _record_account_id(session, company=company, row=row, batch_id=batch_id, actor=actor)
    return company


def _record_account_id(
    session: Session,
    *,
    company: Company,
    row: ApolloRow,
    batch_id: uuid.UUID,
    actor: str,
) -> None:
    """Persist the export's Account Id against the company it resolved to.

    Idempotent, and it never re-points an identifier another company already
    holds: a vendor key claimed by two companies is a conflict to surface, not a
    reassignment to perform silently.
    """

    if not row.apollo_account_id:
        return
    existing = session.scalars(
        select(ImportSourceIdentifier).where(
            ImportSourceIdentifier.system == APOLLO_SYSTEM,
            ImportSourceIdentifier.identifier_kind == ACCOUNT_ID_KIND,
            ImportSourceIdentifier.identifier_value == row.apollo_account_id,
        )
    ).first()
    if existing is not None:
        return
    identifier = ImportSourceIdentifier(
        company_id=company.id,
        system=APOLLO_SYSTEM,
        identifier_kind=ACCOUNT_ID_KIND,
        identifier_value=row.apollo_account_id,
        first_seen_batch_id=batch_id,
        recorded_by=actor,
    )
    try:
        with session.begin_nested():
            session.add(identifier)
            session.flush()
    except IntegrityError:
        # Another row in this batch recorded it first. Same value, same company:
        # nothing to reconcile.
        pass


def normalized_domain(value: str | None) -> str | None:
    """Re-exported for callers that only import this module."""

    return norm.normalize_domain(value)

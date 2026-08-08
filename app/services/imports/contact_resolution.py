"""Which person an imported row is about (IMP-001 §11).

Same two-phase shape as :mod:`app.services.imports.company_resolution`, for the
same reason: :func:`plan` writes nothing so the preview can be honest, and
:func:`apply` has no decision logic of its own so the two cannot drift.

The matching rule is deliberately unlike a merge heuristic. Three exact signals
are consulted — the export's own contact key, the normalized address, and the
normalized LinkedIn profile URL — and they must **agree**. Any one of them alone
identifies a person; two of them naming two different permanent Contacts is a
contradiction, and a contradiction is held for an operator rather than resolved
by preferring whichever signal is listed first. Nothing fuzzy participates: a
matching name, a matching employer, or a matching title never contributes,
because the population these files describe is full of people who share all
three.

One Contact may of course be in many Campaigns. Enrolment is a separate record
with its own execution history (:mod:`app.services.campaign_contacts`), so
importing the same person into a second campaign creates a membership and never
a second person.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.contact import Contact
from app.models.enums import LinkedInIdentifierKind
from app.models.imported_email import ImportSourceIdentifier
from app.services import identity_links
from app.services.audit import record_audit_event
from app.services.imports import normalization as norm
from app.services.imports.apollo import ApolloRow

APOLLO_SYSTEM = "apollo"
CONTACT_ID_KIND = "contact_id"
RECORD_ID_KIND = "record_id"


class ContactAction(enum.StrEnum):
    """What confirming this row would do about its person."""

    MATCH_EXISTING = "match_existing"
    CREATE = "create"
    REVIEW_REQUIRED = "review_required"


class ContactBasis(enum.StrEnum):
    """Which exact signal identified the person."""

    APOLLO_CONTACT_ID = "apollo_contact_id"
    NORMALIZED_EMAIL = "normalized_email"
    LINKEDIN_PROFILE_URL = "linkedin_profile_url"
    CREATED = "created"


_BASIS_PRIORITY: tuple[ContactBasis, ...] = (
    ContactBasis.APOLLO_CONTACT_ID,
    ContactBasis.NORMALIZED_EMAIL,
    ContactBasis.LINKEDIN_PROFILE_URL,
)


@dataclass
class ContactPlan:
    """What :func:`apply` will do, decided without writing anything."""

    action: ContactAction
    basis: ContactBasis | None = None
    contact_id: uuid.UUID | None = None
    display_name: str | None = None
    review_code: str | None = None
    review_detail: str | None = None
    warnings: list[tuple[str, str]] = field(default_factory=list)
    evidence: dict[str, str] = field(default_factory=dict)

    @property
    def needs_review(self) -> bool:
        return self.action is ContactAction.REVIEW_REQUIRED


def _contact_by_apollo_id(session: Session, contact_key: str | None) -> Contact | None:
    if not contact_key:
        return None
    identifier = session.scalars(
        select(ImportSourceIdentifier).where(
            ImportSourceIdentifier.system == APOLLO_SYSTEM,
            ImportSourceIdentifier.identifier_kind == CONTACT_ID_KIND,
            ImportSourceIdentifier.identifier_value == contact_key,
            ImportSourceIdentifier.contact_id.is_not(None),
        )
    ).first()
    if identifier is None or identifier.contact_id is None:
        return None
    contact = session.get(Contact, identifier.contact_id)
    return None if contact is None or contact.merged_into_id is not None else contact


def _contact_by_email(session: Session, email: str | None) -> Contact | None:
    if not email:
        return None
    contact = session.scalars(
        select(Contact).where(Contact.email == email, Contact.merged_into_id.is_(None))
    ).first()
    return contact


def plan(session: Session, row: ApolloRow, *, company_domain: str | None) -> ContactPlan:
    """Decide the row's person without writing anything.

    ``company_domain`` is the domain the company resolution settled on. It is
    used only to build the repository's existing email-less ``natural_key`` for a
    newly created Contact, never to match one: a shared name and employer is the
    single most common way two different people look identical in these files.
    """

    result = ContactPlan(action=ContactAction.CREATE)

    if not row.has_person_identity:
        result.action = ContactAction.REVIEW_REQUIRED
        result.review_code = "person_identity_missing"
        result.review_detail = (
            "The row does not name a person: both First Name and Last Name are required."
        )
        return result

    primary = row.primary
    email = primary.normalized if primary is not None and primary.is_valid_syntax else None

    matches: list[tuple[ContactBasis, Contact]] = []
    for basis, contact in (
        (ContactBasis.APOLLO_CONTACT_ID, _contact_by_apollo_id(session, row.apollo_contact_id)),
        (ContactBasis.NORMALIZED_EMAIL, _contact_by_email(session, email)),
        (
            ContactBasis.LINKEDIN_PROFILE_URL,
            identity_links.lookup_contact(
                session,
                LinkedInIdentifierKind.PUBLIC_VANITY_URL,
                row.person_linkedin_identity,
            ),
        ),
    ):
        if contact is not None:
            matches.append((basis, contact))
            result.evidence[basis.value] = str(contact.id)

    distinct = {contact.id for _basis, contact in matches}
    if len(distinct) > 1:
        result.action = ContactAction.REVIEW_REQUIRED
        result.review_code = "contact_identity_ambiguous"
        signals = ", ".join(sorted(basis.value for basis, _c in matches))
        result.review_detail = (
            f"This row's identity signals ({signals}) name {len(distinct)} different "
            "permanent Contacts. They were kept separate rather than merged on this "
            "evidence."
        )
        return result

    if distinct:
        basis, contact = next((b, c) for pref in _BASIS_PRIORITY for b, c in matches if b is pref)
        result.action = ContactAction.MATCH_EXISTING
        result.basis = basis
        result.contact_id = contact.id
        result.display_name = " ".join(
            part for part in (contact.first_name, contact.last_name) if part
        )
        # The imported address becomes this campaign's address for the person, so
        # a matched Contact already carrying a DIFFERENT address is a genuine
        # conflict. Overwriting would discard an address something else
        # established; keeping both silently would leave two answers to "what is
        # this person's address" with nothing recording which won.
        if email and contact.email and norm.normalize_email(contact.email) != email:
            result.action = ContactAction.REVIEW_REQUIRED
            result.review_code = "contact_email_conflict"
            result.review_detail = (
                f"The row supplies {email}, but the matched Contact already has a "
                f"different address on record. Choosing between them is an operator "
                f"decision."
            )
            return result
        return result

    if email is None:
        result.action = ContactAction.REVIEW_REQUIRED
        result.review_code = "primary_email_unusable"
        result.review_detail = (
            "The row's primary Email is missing or not a valid address, and no existing "
            "Contact matched. This import path exists to carry a supplied address, so a "
            "row without a usable one cannot be created through it."
        )
        return result

    result.action = ContactAction.CREATE
    result.basis = ContactBasis.CREATED
    result.display_name = f"{row.first_name} {row.last_name}"
    if company_domain is None:
        result.warnings.append(
            (
                "natural_key_unavailable",
                "No company domain was resolved, so this Contact has no name+domain "
                "dedup fingerprint. Its address remains its identity.",
            )
        )
    return result


def apply(
    session: Session,
    *,
    contact_plan: ContactPlan,
    row: ApolloRow,
    company_id: uuid.UUID | None,
    company_domain: str | None,
    batch_id: uuid.UUID,
    actor: str,
) -> Contact | None:
    """Materialize *contact_plan*. Returns ``None`` for a review-required plan."""

    if contact_plan.action is ContactAction.REVIEW_REQUIRED:
        return None

    if contact_plan.action is ContactAction.MATCH_EXISTING:
        contact = session.get(Contact, contact_plan.contact_id)
        if contact is None:  # pragma: no cover - defensive
            return None
        _enrich_existing(session, contact=contact, row=row, company_id=company_id)
    else:
        contact = _create(
            session,
            row=row,
            company_id=company_id,
            company_domain=company_domain,
            batch_id=batch_id,
            actor=actor,
        )

    _record_identifiers(session, contact=contact, row=row, batch_id=batch_id, actor=actor)
    _record_linkedin(session, contact=contact, row=row, contact_plan=contact_plan)
    return contact


def _create(
    session: Session,
    *,
    row: ApolloRow,
    company_id: uuid.UUID | None,
    company_domain: str | None,
    batch_id: uuid.UUID,
    actor: str,
) -> Contact:
    assert row.first_name is not None and row.last_name is not None
    assert row.primary is not None and row.primary.normalized is not None
    natural_key = (
        norm.build_natural_key(row.first_name, row.last_name, company_domain)
        if company_domain
        else None
    )
    contact = Contact(
        first_name=row.first_name,
        last_name=row.last_name,
        company_name=row.company_name,
        company_domain=company_domain,
        company_id=company_id,
        email=row.primary.normalized,
        title=row.title,
        linkedin_url=row.person_linkedin_identity or row.person_linkedin_url,
        country=row.country,
        location=_location(row),
        industry=row.industry,
        company_size=row.employee_count,
        natural_key=natural_key,
    )
    session.add(contact)
    session.flush()
    record_audit_event(
        session,
        actor=actor,
        action="contact.created",
        entity_type="contact",
        entity_id=str(contact.id),
        new_state="imported",
        reason="contact created from a campaign-bound contact file import",
        context={
            "import_batch_id": str(batch_id),
            "source_row_number": row.row_number,
            "company_id": str(company_id) if company_id else None,
        },
    )
    return contact


def _location(row: ApolloRow) -> str | None:
    parts = [part for part in (row.city, row.state, row.country) if part]
    return ", ".join(parts)[:255] if parts else None


def _enrich_existing(
    session: Session,
    *,
    contact: Contact,
    row: ApolloRow,
    company_id: uuid.UUID | None,
) -> None:
    """Fill in what the permanent record does not have yet — and nothing else.

    An import is one more observation, not an authority. It may complete a NULL,
    because "unknown" is not a claim this can contradict. It may not replace a
    value something else already established, because an older export would then
    be able to overwrite a newer, better-sourced fact simply by being uploaded
    later. The address in particular is never touched here: a matched Contact
    whose address disagrees was already held for review by :func:`plan`.
    """

    if contact.company_id is None and company_id is not None:
        contact.company_id = company_id
    for attribute, value in (
        ("first_name", row.first_name),
        ("last_name", row.last_name),
        ("company_name", row.company_name),
        ("title", row.title),
        ("country", row.country),
        ("industry", row.industry),
        ("company_size", row.employee_count),
        ("location", _location(row)),
        ("linkedin_url", row.person_linkedin_identity or row.person_linkedin_url),
        ("email", row.primary.normalized if row.primary is not None else None),
    ):
        if value is not None and getattr(contact, attribute) is None:
            setattr(contact, attribute, value)
    session.flush()


def _record_identifiers(
    session: Session,
    *,
    contact: Contact,
    row: ApolloRow,
    batch_id: uuid.UUID,
    actor: str,
) -> None:
    """Persist the export's own person keys, without ever re-pointing one."""

    for kind, value in (
        (CONTACT_ID_KIND, row.apollo_contact_id),
        (RECORD_ID_KIND, row.apollo_record_id),
    ):
        if not value:
            continue
        existing = session.scalars(
            select(ImportSourceIdentifier).where(
                ImportSourceIdentifier.system == APOLLO_SYSTEM,
                ImportSourceIdentifier.identifier_kind == kind,
                ImportSourceIdentifier.identifier_value == value,
            )
        ).first()
        if existing is not None:
            continue
        identifier = ImportSourceIdentifier(
            contact_id=contact.id,
            system=APOLLO_SYSTEM,
            identifier_kind=kind,
            identifier_value=value,
            first_seen_batch_id=batch_id,
            recorded_by=actor,
        )
        try:
            with session.begin_nested():
                session.add(identifier)
                session.flush()
        except IntegrityError:
            continue


def _record_linkedin(
    session: Session,
    *,
    contact: Contact,
    row: ApolloRow,
    contact_plan: ContactPlan,
) -> None:
    """Record the observed LinkedIn profile URL through the existing identity store.

    Reuses :mod:`app.services.identity_links` rather than writing the column
    directly, so an identifier another Contact already holds produces the same
    ``NEEDS_REVIEW`` outcome it would from any other source. The import does not
    fail on that: the person is still imported, and the disputed identifier is
    left for the existing review path with a warning here.
    """

    if not row.person_linkedin_identity:
        return
    outcome = identity_links.record_observed(
        session,
        contact=contact,
        kind=LinkedInIdentifierKind.PUBLIC_VANITY_URL,
        value=row.person_linkedin_identity,
        decided_by="campaign-file-import",
    )
    if outcome.state.value == "needs_review":
        contact_plan.warnings.append(
            (
                "linkedin_identifier_disputed",
                "The supplied Person LinkedIn Url is already held by another Contact, so "
                "it was not attached to this one. Both records survive for review.",
            )
        )

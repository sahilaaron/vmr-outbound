"""Promote a contact capture into a canonical Contact (DAT-014).

DAT-013 deliberately stops at permanent capture evidence: a canonical
:class:`~app.models.contact.Contact` requires a company **domain**, a LinkedIn
page never shows one, and inferring a domain from a company name would be
fabricated evidence. This module is the bridge the product always intended:

    unmatched capture
    → captured company name + LinkedIn company hints
    → DAT-010 logo.dev candidate lookup
    → operator reviews, confirms or rejects
    → canonical Company matched or created
    → canonical Contact created or safely matched
    → the capture is permanently linked

Everything it needs already exists, and it reuses those parts rather than
growing a second copy of any of them:

* candidate lookup, candidate storage, confirmation and rejection —
  :mod:`app.services.enrichment.companies` and :mod:`.logodev` (DAT-010);
* exact LinkedIn-URL person matching — :mod:`app.services.profiles.refresh`
  (DAT-012E);
* deterministic person deduplication — :mod:`app.services.imports.dedup`
  (DAT-004);
* suppression authority — :mod:`app.services.suppressions` (DAT-006);
* field provenance and freshness — :mod:`app.services.provenance` (DAT-005);
* labels and append-only notes — :mod:`app.services.captures.labels` (DAT-013).

Two rules shape every decision here. **Never fabricate a domain**: the provider
returns candidates, and a candidate becomes truth only when an operator says so
(or when an operator already said so for the same company). **Never merge on
weak evidence**: an ambiguous company or an ambiguous person blocks the
promotion and waits for a human.

Promotion creates identity, not permission. It never produces a campaign
membership, an email candidate, a verification, a score, a draft, or an
approval, and it never weakens a suppression.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.capture_promotion import ContactCapturePromotion
from app.models.company import Company
from app.models.contact import Contact
from app.models.contact_capture import ContactCaptureNote
from app.models.enums import (
    CompanyResolutionOutcome,
    ContactPromotionOutcome,
    DomainResolutionState,
    EnrichmentConfirmationSource,
    EnrichmentConfirmationStatus,
    EnrichmentLookupStatus,
)
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.models.salesnav_enrichment import SalesNavCompanyEnrichment
from app.services.audit import record_audit_event
from app.services.captures import labels as labels_service
from app.services.enrichment import companies as enrichment
from app.services.enrichment import logodev
from app.services.imports import dedup
from app.services.imports import normalization as norm
from app.services.profiles import refresh as refresh_service
from app.services.provenance import service as provenance
from app.services.resolution import store as resolution_store
from app.services.suppressions import evaluate_suppression

PROMOTION_ACTOR = "capture-promotion"
_ENTITY_TYPE = "contact_capture_promotion"

LOOKUP_AUDIT_ACTION = "capture.company_lookup"
CONFIRM_AUDIT_ACTION = "capture.company_confirmed"
PROMOTE_AUDIT_ACTION = "capture.promoted"

# Snapshot-derived fields proposed to the DAT-005 freshness policy after a
# promotion. Everything else the capture observed stays snapshot evidence only.
_PROVENANCE_FIELDS = ("title", "company_name", "linkedin_url")

# Lookup statuses that mean "ask again later", as opposed to "asked, and the
# provider genuinely has nothing".
_RETRYABLE_LOOKUP_STATUSES = frozenset(
    {
        EnrichmentLookupStatus.API_UNAVAILABLE,
        EnrichmentLookupStatus.RATE_LIMITED,
        EnrichmentLookupStatus.MALFORMED,
        EnrichmentLookupStatus.ERROR,
    }
)

# The company outcomes that authorize a promotion attempt.
#
# DAT-017A adds the third. A provisional domain is allowed to create identity —
# the permanent Company and the Contact link — because that is what company
# research needs to exist at all. It is NOT allowed to authorize anything after
# research; that boundary is enforced by app.services.resolution.gates, not by
# withholding the promotion.
_RESOLVED_COMPANY_OUTCOMES = frozenset(
    {
        CompanyResolutionOutcome.EXISTING_COMPANY_RESOLVED,
        CompanyResolutionOutcome.DOMAIN_CANDIDATE_CONFIRMED,
        CompanyResolutionOutcome.DOMAIN_PROVISIONAL,
    }
)


class PromotionError(Exception):
    """A deterministic, operator-facing promotion failure (bad input or state)."""


# --- Reading the capture ------------------------------------------------------


@dataclass(frozen=True)
class CompanyHints:
    """What a capture actually showed about the person's employer.

    Every field is a hint. None of them resolves a domain on its own, and the
    absence of all of them means there is nothing to look up.
    """

    name: str | None
    linkedin_url: str | None
    linkedin_id: str | None
    location: str | None

    @property
    def key(self) -> str:
        return enrichment.company_key(self.name)

    @property
    def has_company(self) -> bool:
        return bool(self.key)


@dataclass(frozen=True)
class PersonIdentity:
    """The person a capture observed, as far as it can be trusted."""

    first_name: str | None
    last_name: str | None
    full_name: str | None
    title: str | None
    normalized_profile_url: str | None

    @property
    def is_nameable(self) -> bool:
        return bool(self.first_name and self.last_name)


def _current_role(snapshot: LinkedInProfileSnapshot) -> dict[str, Any]:
    """The capture's current role: a current experience entry, else the hint."""

    for entry in refresh_service.snapshot_experiences(snapshot):
        if entry.get("is_current") is True:
            return entry
    payload = snapshot.payload or {}
    hint = payload.get("current_employment_hint")
    return hint if isinstance(hint, dict) else {}


def company_hints(snapshot: LinkedInProfileSnapshot) -> CompanyHints:
    """Extract the employer hints from a capture, without inferring anything."""

    role = _current_role(snapshot)
    fields = snapshot.profile_fields or {}
    linkedin_url = norm.normalize_linkedin_company_url(role.get("company_linkedin_url"))
    linkedin_id = role.get("company_linkedin_id")
    location = role.get("role_location") or fields.get("displayed_location")
    return CompanyHints(
        name=norm.collapse_whitespace(role.get("company_name")),
        linkedin_url=linkedin_url,
        linkedin_id=linkedin_id if isinstance(linkedin_id, str) and linkedin_id else None,
        location=norm.collapse_whitespace(location if isinstance(location, str) else None),
    )


def person_identity(snapshot: LinkedInProfileSnapshot) -> PersonIdentity:
    """Extract the person, using only what the capture recorded.

    First/last names come from the capture's own split when it has one; a
    single-token name yields no last name, which blocks promotion rather than
    guessing a surname.
    """

    fields = snapshot.profile_fields or {}
    role = _current_role(snapshot)
    first = norm.normalize_name(fields.get("first_name"))
    last = norm.normalize_name(fields.get("last_name"))
    full = norm.collapse_whitespace(fields.get("full_name"))
    if (not first or not last) and full:
        parts = full.split(" ", 1)
        if len(parts) == 2:
            first = first or norm.normalize_name(parts[0])
            last = last or norm.normalize_name(parts[1])
    title = role.get("job_title") or role.get("title")
    return PersonIdentity(
        first_name=first,
        last_name=last,
        full_name=full,
        title=norm.collapse_whitespace(title if isinstance(title, str) else None),
        normalized_profile_url=snapshot.normalized_profile_url,
    )


# --- Records ------------------------------------------------------------------


def get_promotion(session: Session, capture_id: uuid.UUID) -> ContactCapturePromotion | None:
    return session.scalars(
        select(ContactCapturePromotion).where(ContactCapturePromotion.capture_id == capture_id)
    ).first()


def get_enrichment(session: Session, capture_id: uuid.UUID) -> SalesNavCompanyEnrichment | None:
    return session.scalars(
        select(SalesNavCompanyEnrichment).where(SalesNavCompanyEnrichment.capture_id == capture_id)
    ).first()


def ensure_records(
    session: Session, snapshot: LinkedInProfileSnapshot
) -> tuple[ContactCapturePromotion, SalesNavCompanyEnrichment | None]:
    """Create the promotion and capture-scoped enrichment records, once.

    Idempotent, and it never calls the provider. A capture whose page showed no
    company name gets a promotion record but no enrichment record: there is
    nothing to look up, and inventing a query would be the first step toward
    inventing a domain.
    """

    hints = company_hints(snapshot)
    promotion = get_promotion(session, snapshot.id)
    if promotion is None:
        promotion = ContactCapturePromotion(
            capture_id=snapshot.id,
            company_outcome=CompanyResolutionOutcome.PENDING_LOOKUP,
            contact_outcome=ContactPromotionOutcome.PENDING,
        )
        session.add(promotion)
        session.flush()

    record = get_enrichment(session, snapshot.id)
    if record is None and hints.has_company:
        record = SalesNavCompanyEnrichment(
            capture_id=snapshot.id,
            company_key=hints.key,
            company_name=hints.name or "",
            row_count=1,
            company_linkedin_url=hints.linkedin_url,
            company_linkedin_id=hints.linkedin_id,
            location_hint=hints.location,
            lookup_status=EnrichmentLookupStatus.NOT_STARTED,
            confirmation_status=EnrichmentConfirmationStatus.UNCONFIRMED,
            lookup_attempts=0,
        )
        session.add(record)
        session.flush()
    elif record is not None:
        # Keep the hints current without disturbing the lookup or the decision.
        record.company_linkedin_url = hints.linkedin_url
        record.company_linkedin_id = hints.linkedin_id
        record.location_hint = hints.location

    if promotion.enrichment_id is None and record is not None:
        promotion.enrichment_id = record.id
    session.flush()
    return promotion, record


# --- Company resolution -------------------------------------------------------


def prior_confirmed_domains(
    session: Session,
    *,
    company_key_value: str,
    company_linkedin_id: str | None,
    exclude_record_id: uuid.UUID | None = None,
) -> set[str]:
    """Domains an operator has ALREADY confirmed for this same company.

    This is the one deterministic auto-confirmation input, and it is not an
    exception to operator control — it replays a decision the operator made
    earlier, for the same normalized company name (and, when the capture showed
    one, the same LinkedIn company identifier). A provider's top-ranked result
    is never consulted here.

    Returning more than one domain means two earlier confirmations disagree,
    which is company ambiguity: the caller must not choose between them.
    """

    if not company_key_value:
        return set()
    query = select(SalesNavCompanyEnrichment).where(
        SalesNavCompanyEnrichment.company_key == company_key_value,
        SalesNavCompanyEnrichment.confirmation_status == EnrichmentConfirmationStatus.CONFIRMED,
        SalesNavCompanyEnrichment.confirmed_domain.is_not(None),
    )
    if exclude_record_id is not None:
        query = query.where(SalesNavCompanyEnrichment.id != exclude_record_id)
    domains: set[str] = set()
    for record in session.scalars(query):
        # When both sides know the LinkedIn company identifier they must agree;
        # a mismatch means two different companies share a display name.
        if (
            company_linkedin_id
            and record.company_linkedin_id
            and record.company_linkedin_id != company_linkedin_id
        ):
            continue
        if record.confirmed_domain:
            domains.add(record.confirmed_domain)
    return domains


def evaluate_company(
    session: Session,
    *,
    promotion: ContactCapturePromotion,
    record: SalesNavCompanyEnrichment | None,
    hints: CompanyHints,
    actor: str = PROMOTION_ACTOR,
    allow_auto_confirm: bool = True,
) -> CompanyResolutionOutcome:
    """Determine (and store) the truthful company-resolution state.

    Pure state evaluation plus, at most, one deterministic auto-confirmation
    from a prior operator decision. It never calls the provider and never picks
    a candidate.
    """

    if not hints.has_company or record is None:
        outcome = CompanyResolutionOutcome.NO_CANDIDATE
        promotion.company_outcome = outcome
        promotion.blocked_reason = (
            "the captured page showed no company name, so there is nothing to look up"
        )
        session.flush()
        return outcome

    if record.confirmation_status is EnrichmentConfirmationStatus.CONFIRMED:
        # PRIOR_MAPPING and AUTOMATIC_POLICY both mean "a domain already on
        # record named this company". AUTOMATIC_POLICY (DAT-017A) only ever
        # confirms from an approved mapping or an existing permanent Company —
        # never from a provider candidate — so reporting it as an existing
        # company resolution is exact rather than generous.
        outcome = (
            CompanyResolutionOutcome.EXISTING_COMPANY_RESOLVED
            if record.confirmation_source
            in (
                EnrichmentConfirmationSource.PRIOR_MAPPING,
                EnrichmentConfirmationSource.AUTOMATIC_POLICY,
            )
            else CompanyResolutionOutcome.DOMAIN_CANDIDATE_CONFIRMED
        )
        promotion.company_outcome = outcome
        promotion.resolved_domain = record.confirmed_domain
        # The confirmation is what the operator came here to do, so whatever
        # refused promotion before it — candidates awaiting review, no domain,
        # a failed lookup — has just stopped being true. Leaving the old reason
        # on the row makes the page say promotion is available and blocked at
        # the same time, and the row is what the page reads.
        promotion.blocked_reason = None
        session.flush()
        return outcome

    if record.confirmation_status is EnrichmentConfirmationStatus.UNRESOLVED:
        promotion.company_outcome = CompanyResolutionOutcome.LEFT_UNRESOLVED
        promotion.blocked_reason = "the operator left this company deliberately unresolved"
        session.flush()
        return CompanyResolutionOutcome.LEFT_UNRESOLVED

    # Unconfirmed, but a DAT-017A decision may already have settled it at the
    # provisional level. A provisional domain deliberately writes no confirmation
    # onto the enrichment record — that is what stops it becoming a reusable
    # approved mapping — so it has to be read from the decision itself, or every
    # later view of this capture would quietly report it back as "awaiting your
    # confirmation" and undo the resolution.
    live = resolution_store.current_decision(session, promotion.capture_id)
    if (
        live is not None
        and live.state is DomainResolutionState.PROVISIONAL
        and live.selected_domain
    ):
        promotion.company_outcome = CompanyResolutionOutcome.DOMAIN_PROVISIONAL
        promotion.resolved_domain = live.selected_domain
        promotion.blocked_reason = None
        session.flush()
        return CompanyResolutionOutcome.DOMAIN_PROVISIONAL

    # Unconfirmed: can an earlier operator decision settle it deterministically?
    if allow_auto_confirm:
        prior = prior_confirmed_domains(
            session,
            company_key_value=record.company_key,
            company_linkedin_id=record.company_linkedin_id,
            exclude_record_id=record.id,
        )
        if len(prior) > 1:
            promotion.company_outcome = CompanyResolutionOutcome.COMPANY_IDENTITY_AMBIGUOUS
            promotion.blocked_reason = (
                "this company name has been confirmed with more than one domain before; "
                "confirm the right one for this capture"
            )
            promotion.detail = {"prior_confirmed_domains": sorted(prior)}
            session.flush()
            return CompanyResolutionOutcome.COMPANY_IDENTITY_AMBIGUOUS
        if len(prior) == 1:
            enrichment.confirm_record(
                session,
                record=record,
                source=EnrichmentConfirmationSource.PRIOR_MAPPING,
                domain=next(iter(prior)),
                actor=actor,
                note="reused a domain this operator already confirmed for the same company",
            )
            promotion.company_outcome = CompanyResolutionOutcome.EXISTING_COMPANY_RESOLVED
            promotion.resolved_domain = record.confirmed_domain
            promotion.blocked_reason = None
            session.flush()
            return CompanyResolutionOutcome.EXISTING_COMPANY_RESOLVED

    outcome = _outcome_from_lookup(record)
    promotion.company_outcome = outcome
    promotion.blocked_reason = _company_blocked_reason(outcome, record=record)
    session.flush()
    return outcome


def _outcome_from_lookup(record: SalesNavCompanyEnrichment) -> CompanyResolutionOutcome:
    if record.lookup_status is EnrichmentLookupStatus.NOT_STARTED:
        return CompanyResolutionOutcome.PENDING_LOOKUP
    if record.lookup_status in _RETRYABLE_LOOKUP_STATUSES:
        return CompanyResolutionOutcome.LOOKUP_UNAVAILABLE
    remaining = len(record.candidates or [])
    if remaining == 0:
        return CompanyResolutionOutcome.NO_CANDIDATE
    if remaining == 1:
        return CompanyResolutionOutcome.CANDIDATE_REVIEW_REQUIRED
    return CompanyResolutionOutcome.MULTIPLE_CANDIDATES_REVIEW_REQUIRED


def _company_blocked_reason(
    outcome: CompanyResolutionOutcome,
    *,
    record: SalesNavCompanyEnrichment | None = None,
) -> str | None:
    """Why promotion is refused right now, counted from the current candidates.

    "Several" survives a rejection unchanged and so quietly outlives the set it
    described. The count is read from ``record.candidates``, which a rejection
    shrinks, so the sentence is re-derived rather than remembered.
    """

    if outcome is CompanyResolutionOutcome.MULTIPLE_CANDIDATES_REVIEW_REQUIRED:
        waiting = len(record.candidates or []) if record is not None else 0
        return f"{waiting} domain candidates are waiting for your confirmation"
    return {
        CompanyResolutionOutcome.PENDING_LOOKUP: "run the company-domain lookup first",
        CompanyResolutionOutcome.LOOKUP_UNAVAILABLE: (
            "the domain provider could not be reached or returned an unusable answer; retry"
        ),
        CompanyResolutionOutcome.NO_CANDIDATE: (
            "no usable domain candidate — enter a domain manually or leave this capture pending"
        ),
        CompanyResolutionOutcome.CANDIDATE_REVIEW_REQUIRED: (
            "1 domain candidate is waiting for your confirmation"
        ),
    }.get(outcome)


#: How a resolved company outcome should be described to the operator. The
#: outcome enum alone cannot say it: a manual override and a confirmed provider
#: candidate both store ``DOMAIN_CANDIDATE_CONFIRMED``, so reporting the enum
#: name credits the provider for a domain the operator typed.
_CONFIRMATION_PHRASES = {
    EnrichmentConfirmationSource.CANDIDATE: "domain candidate confirmed",
    EnrichmentConfirmationSource.MANUAL: "domain entered manually",
    EnrichmentConfirmationSource.PRIOR_MAPPING: "domain reused from an earlier confirmation",
    EnrichmentConfirmationSource.AUTOMATIC_POLICY: "domain confirmed automatically from evidence",
    EnrichmentConfirmationSource.UNRESOLVED: "deliberately left unresolved",
}


def company_outcome_phrase(
    outcome: CompanyResolutionOutcome,
    *,
    record: SalesNavCompanyEnrichment | None = None,
) -> str:
    """A truthful operator-facing phrase for a company-resolution outcome.

    Falls back to the outcome name whenever the record cannot narrow it —
    a provisional domain has no confirmation source by design, and saying
    "provisional" is already exact.
    """

    if (
        outcome is CompanyResolutionOutcome.DOMAIN_CANDIDATE_CONFIRMED
        and record is not None
        and record.confirmation_source is not None
    ):
        phrase = _CONFIRMATION_PHRASES.get(record.confirmation_source)
        if phrase is not None:
            return phrase
    return outcome.value.replace("_", " ")


def run_lookup(
    session: Session,
    *,
    snapshot: LinkedInProfileSnapshot,
    api_key: str,
    search_url: str,
    timeout: float,
    max_candidates: int,
    actor: str = PROMOTION_ACTOR,
    force: bool = False,
    transport: logodev.Transport | None = None,
) -> tuple[ContactCapturePromotion, SalesNavCompanyEnrichment | None]:
    """Ask the provider for domain candidates for this capture's company.

    Delegates to the DAT-010 lookup unchanged, so there is one provider client,
    one candidate shape, and one place a lookup can happen. Idempotent unless
    ``force``; a capture with no company name never calls out.
    """

    promotion, record = ensure_records(session, snapshot)
    hints = company_hints(snapshot)
    if record is None:
        evaluate_company(session, promotion=promotion, record=None, hints=hints, actor=actor)
        return promotion, None

    enrichment.run_lookup(
        session,
        record=record,
        api_key=api_key,
        search_url=search_url,
        timeout=timeout,
        max_candidates=max_candidates,
        actor=actor,
        force=force,
        transport=transport,
    )
    evaluate_company(session, promotion=promotion, record=record, hints=hints, actor=actor)
    return promotion, record


def confirm_domain(
    session: Session,
    *,
    snapshot: LinkedInProfileSnapshot,
    source: EnrichmentConfirmationSource,
    domain: str | None,
    actor: str,
    note: str | None = None,
) -> ContactCapturePromotion:
    """Record the operator's explicit domain decision for this capture."""

    promotion, record = ensure_records(session, snapshot)
    if record is None:
        raise PromotionError("this capture showed no company name, so there is nothing to confirm")
    if source is EnrichmentConfirmationSource.PRIOR_MAPPING:
        raise PromotionError("prior_mapping is derived by the backend, not chosen by hand")
    try:
        enrichment.confirm_record(
            session, record=record, source=source, domain=domain, actor=actor, note=note
        )
    except enrichment.EnrichmentError as exc:
        raise PromotionError(str(exc)) from exc
    # An explicit decision is final for this capture: never auto-override it.
    evaluate_company(
        session,
        promotion=promotion,
        record=record,
        hints=company_hints(snapshot),
        actor=actor,
        allow_auto_confirm=False,
    )
    return promotion


def reject_candidate(
    session: Session,
    *,
    snapshot: LinkedInProfileSnapshot,
    domain: str,
    actor: str,
    reason: str | None = None,
) -> ContactCapturePromotion:
    """Reject one provider candidate, preserving it as a recorded decision."""

    promotion, record = ensure_records(session, snapshot)
    if record is None:
        raise PromotionError("this capture showed no company name, so there are no candidates")
    try:
        enrichment.reject_candidate(
            session, record=record, domain=domain, actor=actor, reason=reason
        )
    except enrichment.EnrichmentError as exc:
        raise PromotionError(str(exc)) from exc
    evaluate_company(
        session,
        promotion=promotion,
        record=record,
        hints=company_hints(snapshot),
        actor=actor,
        allow_auto_confirm=False,
    )
    return promotion


# --- Company row --------------------------------------------------------------


def resolve_company_row(session: Session, *, domain: str, name: str | None) -> Company:
    """Find or create the canonical company for an exact domain.

    ``companies.domain`` is uniquely indexed, so an exact domain identifies at
    most one company: reuse is safe and creation cannot duplicate. The captured
    name is used only when creating; an existing company's name is never
    overwritten from a capture.
    """

    existing = session.scalars(select(Company).where(Company.domain == domain)).first()
    if existing is not None:
        return existing
    company = Company(name=name or domain, domain=domain)
    session.add(company)
    session.flush()
    return company


# --- Promotion ----------------------------------------------------------------


@dataclass
class PromotionResult:
    """The truthful outcome of one promotion attempt."""

    promotion: ContactCapturePromotion
    company_outcome: CompanyResolutionOutcome
    contact_outcome: ContactPromotionOutcome
    contact: Contact | None = None
    company: Company | None = None
    labels_applied: list[str] = field(default_factory=list)
    notes_linked: int = 0
    blocked_reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def promoted(self) -> bool:
        return self.contact is not None

    def summary(self) -> dict[str, Any]:
        return {
            "company_outcome": self.company_outcome.value,
            "contact_outcome": self.contact_outcome.value,
            "company_id": str(self.company.id) if self.company else None,
            "resolved_domain": self.promotion.resolved_domain,
            "contact_id": str(self.contact.id) if self.contact else None,
            "labels_applied": self.labels_applied,
            "notes_linked": self.notes_linked,
            "blocked_reason": self.blocked_reason,
            **self.detail,
        }


def _block(
    session: Session,
    *,
    promotion: PromotionResult,
    outcome: ContactPromotionOutcome,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> PromotionResult:
    promotion.contact_outcome = outcome
    promotion.blocked_reason = reason
    promotion.detail.update(detail or {})
    row = promotion.promotion
    row.contact_outcome = outcome
    row.blocked_reason = reason
    if detail:
        row.detail = {**(row.detail or {}), **detail}
    session.flush()
    return promotion


def _link_notes(session: Session, *, capture_id: uuid.UUID, contact_id: uuid.UUID) -> int:
    """Point this capture's existing notes at the promoted contact.

    The notes themselves are append-only and untouched: their text, scope,
    author and creation time are unchanged. Only the contact link — which was
    null while the capture was unmatched — is filled in.
    """

    notes = list(
        session.scalars(
            select(ContactCaptureNote).where(ContactCaptureNote.capture_id == capture_id)
        )
    )
    for note in notes:
        if note.contact_id is None:
            note.contact_id = contact_id
    if notes:
        session.flush()
    return len(notes)


def _record_provenance(
    session: Session,
    *,
    snapshot: LinkedInProfileSnapshot,
    contact: Contact,
    identity: PersonIdentity,
    hints: CompanyHints,
    actor: str,
) -> None:
    """Append the capture's observations to the DAT-005 ledger and reconcile.

    The promotion does not decide which value wins — the versioned freshness
    policy does, exactly as it does for a DAT-013 refresh. A manual override or
    newer evidence still beats this capture.
    """

    observed_at = (snapshot.captured_at or snapshot.ingested_at or datetime.now(UTC)).astimezone(
        UTC
    )
    proposed: dict[str, str | None] = {}
    if identity.title:
        proposed["title"] = identity.title
    if hints.name:
        proposed["company_name"] = hints.name
    if snapshot.normalized_profile_url:
        proposed["linkedin_url"] = snapshot.normalized_profile_url
    for field_name in _PROVENANCE_FIELDS:
        if field_name not in proposed:
            continue
        provenance.record_observation(
            session,
            contact_id=contact.id,
            field_name=field_name,
            value=proposed[field_name],
            source_name="linkedin-contact-capture",
            source_reference=str(snapshot.id),
            observed_at=observed_at,
            created_by=actor,
        )
        provenance.reconcile_field(session, contact=contact, field_name=field_name, actor=actor)


def promote(
    session: Session,
    *,
    snapshot: LinkedInProfileSnapshot,
    actor: str = PROMOTION_ACTOR,
    _fault: Any = None,
) -> PromotionResult:
    """Promote one resolved capture into a canonical Contact.

    Requires a confirmed company domain. Blocks — changing nothing but the
    recorded reason — when the company is unresolved, the person cannot be
    named, the identity is ambiguous, or the identity is suppressed. Idempotent:
    a capture that already has a promoted contact returns ``already_promoted``
    without touching anything.

    ``_fault`` is a test-only hook invoked after the writes and before the
    caller commits, used to prove a partial failure leaves nothing behind.
    """

    promotion_row, record = ensure_records(session, snapshot)
    hints = company_hints(snapshot)
    identity = person_identity(snapshot)

    result = PromotionResult(
        promotion=promotion_row,
        company_outcome=promotion_row.company_outcome,
        contact_outcome=promotion_row.contact_outcome,
    )

    # --- Already promoted: the database's unique capture_id makes this exact --
    if promotion_row.promoted_contact_id is not None:
        contact = session.get(Contact, promotion_row.promoted_contact_id)
        result.contact = contact
        result.company = (
            session.get(Company, promotion_row.resolved_company_id)
            if promotion_row.resolved_company_id
            else None
        )
        result.company_outcome = promotion_row.company_outcome
        result.contact_outcome = ContactPromotionOutcome.ALREADY_PROMOTED
        result.labels_applied = list(promotion_row.labels_applied or [])
        result.notes_linked = promotion_row.notes_linked
        promotion_row.contact_outcome = ContactPromotionOutcome.ALREADY_PROMOTED
        session.flush()
        return result

    company_outcome = evaluate_company(
        session, promotion=promotion_row, record=record, hints=hints, actor=actor
    )
    result.company_outcome = company_outcome

    if company_outcome not in _RESOLVED_COMPANY_OUTCOMES:
        return _block(
            session,
            promotion=result,
            outcome=ContactPromotionOutcome.PROMOTION_BLOCKED,
            reason=promotion_row.blocked_reason or "the company domain is not resolved yet",
        )

    # A confirmed domain lives on the enrichment record. A DAT-017A provisional
    # domain deliberately does not (it must never become a reusable approved
    # mapping), so for that outcome the promotion record's resolved_domain — set
    # from the decision — is the authoritative source.
    domain = record.confirmed_domain if record is not None else None
    if company_outcome is CompanyResolutionOutcome.DOMAIN_PROVISIONAL:
        domain = promotion_row.resolved_domain
    if not domain:
        return _block(
            session,
            promotion=result,
            outcome=ContactPromotionOutcome.PROMOTION_BLOCKED,
            reason="the resolved company decision carries no domain",
        )

    if not identity.is_nameable:
        return _block(
            session,
            promotion=result,
            outcome=ContactPromotionOutcome.PROMOTION_BLOCKED,
            reason=(
                "the capture does not show a first and last name, so a contact cannot be "
                "created without guessing one"
            ),
        )
    # ``is_nameable`` guarantees both parts; narrow them for the type checker.
    assert identity.first_name is not None
    assert identity.last_name is not None

    # --- Person identity: exact URL first, then the deterministic natural key --
    natural_key = norm.build_natural_key(identity.first_name, identity.last_name, domain)
    matched: Contact | None = None
    match_kind = "created"

    if snapshot.normalized_profile_url:
        url_matches = refresh_service.find_exact_matches(session, snapshot.normalized_profile_url)
        if len(url_matches) > 1:
            return _block(
                session,
                promotion=result,
                outcome=ContactPromotionOutcome.CONTACT_IDENTITY_AMBIGUOUS,
                reason=(
                    "more than one existing contact carries this exact LinkedIn URL; "
                    "resolve the duplicate before promoting"
                ),
                detail={"ambiguous_contact_ids": sorted(str(c.id) for c in url_matches)},
            )
        if len(url_matches) == 1:
            matched = url_matches[0]
            match_kind = "linked_by_url"

    if matched is None:
        deduped = dedup.find_existing_contact(session, email=None, natural_key=natural_key)
        if deduped.ambiguous:
            return _block(
                session,
                promotion=result,
                outcome=ContactPromotionOutcome.CONTACT_IDENTITY_AMBIGUOUS,
                reason=(
                    deduped.note
                    or "several existing contacts share this identity; kept separate for review"
                ),
                detail={"natural_key": natural_key},
            )
        if deduped.contact is not None:
            matched = deduped.contact
            match_kind = "linked_by_natural_key"

    # --- Suppression is authoritative, before anything is created -------------
    decision = evaluate_suppression(
        session, email=matched.email if matched else None, domain=domain
    )
    if decision.blocked:
        promotion_row.company_outcome = company_outcome
        promotion_row.resolved_domain = domain
        return _block(
            session,
            promotion=result,
            outcome=ContactPromotionOutcome.SUPPRESSED,
            reason=(
                f"this identity is suppressed ({decision.blocked_reason}); "
                "no contact was created or linked and the suppression is untouched"
            ),
            detail={"suppression_reason": decision.blocked_reason},
        )

    # --- Canonical company ----------------------------------------------------
    company = resolve_company_row(session, domain=domain, name=hints.name)

    # --- Canonical contact ----------------------------------------------------
    if matched is None:
        contact = Contact(
            first_name=identity.first_name,
            last_name=identity.last_name,
            company_name=hints.name or company.name,
            company_domain=domain,
            company_id=company.id,
            title=identity.title,
            linkedin_url=snapshot.normalized_profile_url,
            natural_key=natural_key,
        )
        session.add(contact)
        session.flush()
        contact_outcome = ContactPromotionOutcome.CONTACT_CREATED
    else:
        contact = matched
        contact_outcome = ContactPromotionOutcome.CONTACT_EXACT_MATCH_LINKED

    # The permanent company edge (APP-003, required by DAT-017A). Filled only
    # when it is empty: a contact already linked to another company is a real
    # disagreement, and re-parenting it here would be a silent merge of two
    # company identities. APP-003 already reports that disagreement as a
    # reviewable conflict, which is where it belongs.
    if contact.company_id is None:
        contact.company_id = company.id
        session.flush()

    _record_provenance(
        session, snapshot=snapshot, contact=contact, identity=identity, hints=hints, actor=actor
    )

    applied: list[str] = []
    requested = [name for name in (snapshot.operator_labels or []) if isinstance(name, str)]
    if requested:
        resolved_labels = labels_service.resolve_labels(session, requested)
        applied = labels_service.assign_labels(
            session,
            contact_id=contact.id,
            labels=resolved_labels.labels,
            capture_id=snapshot.id,
        )
    notes_linked = _link_notes(session, capture_id=snapshot.id, contact_id=contact.id)

    # The capture's EVIDENCE is untouched; only its reconciliation link is set,
    # exactly as DAT-012E does. Its ingest outcome stays historically truthful.
    snapshot.matched_contact_id = contact.id

    promotion_row.company_outcome = company_outcome
    promotion_row.contact_outcome = contact_outcome
    promotion_row.resolved_company_id = company.id
    promotion_row.resolved_domain = domain
    promotion_row.promoted_contact_id = contact.id
    promotion_row.labels_applied = applied or None
    promotion_row.notes_linked = notes_linked
    promotion_row.blocked_reason = None
    promotion_row.promoted_by = actor
    promotion_row.promoted_at = datetime.now(UTC)
    promotion_row.detail = {"match_kind": match_kind, "natural_key": natural_key}
    session.flush()

    result.contact = contact
    result.company = company
    result.contact_outcome = contact_outcome
    result.labels_applied = applied
    result.notes_linked = notes_linked
    result.detail = {"match_kind": match_kind}

    record_audit_event(
        session,
        actor=actor,
        action=PROMOTE_AUDIT_ACTION,
        entity_type=_ENTITY_TYPE,
        entity_id=str(promotion_row.id),
        new_state=contact_outcome.value,
        reason="contact capture promoted to a canonical contact",
        context={
            "capture_id": str(snapshot.id),
            "company_outcome": company_outcome.value,
            "contact_outcome": contact_outcome.value,
            "company_id": str(company.id),
            "resolved_domain": domain,
            "contact_id": str(contact.id),
            "match_kind": match_kind,
            "labels_applied": len(applied),
            "notes_linked": notes_linked,
        },
    )

    if _fault is not None:
        _fault()

    return result


# --- Operator view ------------------------------------------------------------


@dataclass
class CaptureResolutionView:
    """Everything the workbench shows for one pending or promoted capture."""

    snapshot: LinkedInProfileSnapshot
    promotion: ContactCapturePromotion
    record: SalesNavCompanyEnrichment | None
    hints: CompanyHints
    identity: PersonIdentity
    candidates: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    notes: list[ContactCaptureNote] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def can_promote(self) -> bool:
        return (
            self.promotion.promoted_contact_id is None
            and self.promotion.company_outcome in _RESOLVED_COMPANY_OUTCOMES
        )


def build_view(
    session: Session, snapshot: LinkedInProfileSnapshot, *, actor: str = PROMOTION_ACTOR
) -> CaptureResolutionView:
    """Assemble the operator view, refreshing the company state without calling out."""

    promotion, record = ensure_records(session, snapshot)
    hints = company_hints(snapshot)
    identity = person_identity(snapshot)
    evaluate_company(session, promotion=promotion, record=record, hints=hints, actor=actor)

    warnings: list[str] = []
    if not identity.is_nameable:
        warnings.append("no first and last name were captured; a contact cannot be created")
    if snapshot.normalized_profile_url is None:
        warnings.append("no LinkedIn profile URL was captured; identity stays uncertain")
    if snapshot.review_candidates:
        warnings.append(
            f"{len(snapshot.review_candidates)} weak-evidence review candidate(s) "
            "were recorded at capture"
        )
    if not hints.has_company:
        warnings.append("no company name was captured")

    notes = list(
        session.scalars(
            select(ContactCaptureNote)
            .where(ContactCaptureNote.capture_id == snapshot.id)
            .order_by(ContactCaptureNote.created_at)
        )
    )
    return CaptureResolutionView(
        snapshot=snapshot,
        promotion=promotion,
        record=record,
        hints=hints,
        identity=identity,
        candidates=list(record.candidates or []) if record else [],
        rejected=list(record.rejected_candidates or []) if record else [],
        notes=notes,
        warnings=warnings,
    )


def pending_captures(session: Session, *, limit: int = 200) -> list[LinkedInProfileSnapshot]:
    """Captures that have no canonical contact yet, oldest first.

    A capture already linked to a contact — whether by a DAT-013 exact-URL
    refresh or by a completed promotion — is not pending.
    """

    promoted = select(ContactCapturePromotion.capture_id).where(
        ContactCapturePromotion.promoted_contact_id.is_not(None)
    )
    return list(
        session.scalars(
            select(LinkedInProfileSnapshot)
            .where(
                LinkedInProfileSnapshot.matched_contact_id.is_(None),
                LinkedInProfileSnapshot.id.not_in(promoted),
            )
            .order_by(LinkedInProfileSnapshot.ingested_at)
            .limit(limit)
        )
    )

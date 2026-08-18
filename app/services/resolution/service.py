"""Automatic company-domain resolution, end to end (DAT-017A).

Gathers the evidence the policy is allowed to see, decides whether a provider
call is even warranted, records the decision, and applies it to the permanent
Company and the Contact.

**Two entry points, one process.** :func:`resolve` resolves the company a Chrome
capture observed, before any Contact exists. :func:`resolve_contact` resolves
the company a permanent Contact states, for a surface — Google Sheets — that
produces the person directly and has no capture. They differ only in what the
decision is *about* and in whether there is a promotion to finish; the evidence
gathering, the provider ladder, the policy, the decision row, the Company link
and the downstream gates are literally the same code. That is deliberate: a
second acquisition surface must not acquire a second, quietly divergent notion
of what a company is or how sure we are about its domain.

Everything it can reuse, it reuses rather than reimplementing: DAT-010 owns the
provider client and the candidate store, DAT-014 owns the promotion record and
the company/person hints, DAT-005 owns provenance. This module adds the decision
and the linking, and nothing else.

Four properties it is built to hold:

**A provider call is the last resort, not the first step.** The policy's
established-evidence half runs before anything is spent, and a lookup happens
only when that half returns nothing *and* no candidates are stored already. A
decision records whether a call actually happened, so the claim is auditable.

**Retries and recalculation cost nothing and change nothing.** Without ``force``
a capture that already has a decision returns it untouched. With ``force`` the
evidence is re-read, but a decision identical to the current one is not written
again (see :func:`app.services.resolution.store.record`).

**No silent merges.** A resolved domain reuses the company that already owns it
(``companies.domain`` is uniquely indexed) or creates one. Two company rows that
turn out to be the same organisation stay two rows and are reported as an
APP-003 identity conflict; nothing here folds one into the other.

**A correction never destroys what it disagrees with.** It supersedes.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.capture_promotion import ContactCapturePromotion
from app.models.company import Company
from app.models.company_domain_resolution import CompanyDomainResolution
from app.models.contact import Contact
from app.models.enums import (
    CompanyResolutionOutcome,
    DomainResolutionKind,
    DomainResolutionState,
    EnrichmentConfirmationSource,
    EnrichmentConfirmationStatus,
    EnrichmentLookupStatus,
)
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.models.salesnav_enrichment import SalesNavCompanyEnrichment
from app.services.audit import record_audit_event
from app.services.captures import promotion as capture_promotion
from app.services.enrichment import companies as enrichment
from app.services.enrichment import logodev, model_domain
from app.services.imports import normalization as norm
from app.services.operations import settings as operational
from app.services.resolution import policy, store
from app.services.thinking.claude_cli import ClaudeCliThinker
from app.services.thinking.contracts import Thinker

RESOLUTION_ACTOR = "domain-resolution"
_ENTITY_TYPE = "company_domain_resolution"

RESOLVE_AUDIT_ACTION = "capture.company_domain_resolved"
CORRECT_AUDIT_ACTION = "capture.company_domain_corrected"


class ResolutionError(Exception):
    """A deterministic, operator-facing resolution failure (bad input or state)."""


@dataclass
class ResolutionOutcome:
    """What one resolution attempt produced."""

    decision: CompanyDomainResolution
    #: False when the decision already existed and nothing changed.
    created: bool
    company: Company | None = None
    contact_linked: bool = False
    provider_call_made: bool = False
    #: True when the model fallback was asked during this resolution. Reported
    #: separately from ``provider_call_made`` because the two cost different
    #: things and a backfill pass is tuned by knowing which one it spent.
    model_call_made: bool = False
    #: DAT-017A automatic promotion. Present whenever a resolved decision tried
    #: to create or reuse the Contact itself, whether or not it succeeded — a
    #: blocked promotion is an outcome to report, not an error to swallow.
    promotion_result: capture_promotion.PromotionResult | None = None

    @property
    def contact(self) -> Contact | None:
        """The Contact this resolution created or reused, if any."""

        return self.promotion_result.contact if self.promotion_result else None

    @property
    def auto_promoted(self) -> bool:
        """True when this resolution produced a Contact without an operator click."""

        return self.contact is not None

    @property
    def state(self) -> DomainResolutionState:
        return self.decision.state

    @property
    def selected_domain(self) -> str | None:
        return self.decision.selected_domain

    def summary(self) -> dict[str, Any]:
        return {
            "state": self.decision.state.value,
            "policy_version": self.decision.policy_version,
            "selected_domain": self.decision.selected_domain,
            "reasons": [str(r) for r in (self.decision.reasons or [])],
            "warnings": [str(w) for w in (self.decision.warnings or [])],
            "provider_call_made": self.provider_call_made,
            "model_call_made": self.model_call_made,
            "company_id": str(self.company.id) if self.company else None,
            "decision_number": self.decision.decision_number,
            "created": self.created,
        }


@dataclass
class ProviderAccess:
    """How (and whether) the provider may be called for this resolution.

    Passed in rather than read from settings so the policy path stays testable
    without environment juggling, and so a caller that has no key simply does
    not supply one — which the policy reports truthfully as "lookup not run"
    instead of pretending the provider had nothing to say.
    """

    api_key: str | None = None
    search_url: str = "https://api.logo.dev/search"
    timeout: float = 10.0
    max_candidates: int = 10
    transport: logodev.Transport | None = None

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.api_key.strip())


@dataclass
class ModelAccess:
    """How (and whether) the model fallback may be called for this resolution.

    Deliberately shaped like :class:`ProviderAccess`: absent means "not asked",
    which the policy reports as such, rather than being mistaken for "asked and
    found nothing". A caller with the switch off passes nothing and the behaviour
    is exactly what it was before this fallback existed.

    ``thinker_factory`` rather than a thinker, for the same reason the Agent
    adapters take one: constructing a :class:`ClaudeCliThinker` per call keeps a
    long backfill pass from holding one subprocess wrapper open across hundreds of
    captures, and lets a test inject a scripted thinker without touching a
    subprocess at all.
    """

    thinker_factory: Callable[[], Thinker] | None = None
    timeout: float = model_domain.DEFAULT_TIMEOUT_SECONDS

    @property
    def available(self) -> bool:
        return self.thinker_factory is not None


def provider_access_for(session: Session, settings: Settings) -> ProviderAccess:
    """How logo.dev may be reached right now, or an access with no key.

    One definition, because three callers need the same answer and a fourth
    reading of "is the provider usable?" is how two acquisition surfaces start
    behaving differently. ``session`` is here because whether the provider may be
    called is an administrator's durable setting rather than an environment
    variable, so answering the question means reading the database.
    """

    usable = (
        operational.enabled(session, "salesnav_domain_enrichment", settings)
        and settings.has_logo_dev_key()
    )
    return ProviderAccess(
        api_key=settings.logo_dev_api_key if usable else None,
        search_url=settings.logo_dev_search_url,
        timeout=settings.logo_dev_timeout_seconds,
        max_candidates=settings.logo_dev_max_candidates,
    )


def model_access_for(session: Session, settings: Settings) -> ModelAccess:
    """The model fallback, if an administrator has switched it on.

    Suitable only for callers with no HTTP request to overrun — the durable
    worker's backfill pass and the Agent that owns a Contact's company stage. A
    model call with web search takes tens of seconds, which is why intake builds
    a provider-only access instead of calling this.
    """

    if not operational.enabled(session, "model_company_domain_lookup", settings):
        return ModelAccess()
    return ModelAccess(
        thinker_factory=lambda: ClaudeCliThinker(settings=settings),
        timeout=settings.model_domain_lookup_timeout_seconds,
    )


#: Reason codes that mean "the deterministic path is finished and has no domain",
#: which is the only state the model fallback is admitted in. Read from the policy
#: rather than re-derived, so the two cannot drift.
_MODEL_FALLBACK_REASONS: frozenset[str] = frozenset(
    {policy.REASON_PROVIDER_NO_CANDIDATES, policy.REASON_NO_ALIGNED_CANDIDATE}
)


def _wants_model_fallback(decision: policy.PolicyDecision) -> bool:
    """Whether this decision is one a model answer could legitimately improve.

    Only an UNRESOLVED decision, and only for the two reasons above. Notably NOT
    for ``multiple_plausible_candidates`` — several sources aligning and
    disagreeing is where the policy refuses to guess, and a third opinion there
    produces a more confident guess rather than a better one.
    """

    if decision.is_resolved:
        return False
    return any(reason in _MODEL_FALLBACK_REASONS for reason in decision.reasons)


# --- Evidence gathering -------------------------------------------------------


def _existing_company_matches(
    session: Session,
    *,
    normalized_name: str,
    linkedin_company_id: str | None,
) -> tuple[policy.ExistingCompanyMatch, ...]:
    """Permanent companies that already claim this identity AND carry a domain.

    Two independent axes, matched exactly:

    * the LinkedIn company identifier, which is indexed and unambiguous when
      both sides have it;
    * the normalized company name, compared in Python because the normalization
      (punctuation, spacing, trailing legal form) has no SQL equivalent.

    The name axis used to be pre-filtered in SQL with
    ``LOWER(name) LIKE '%<first six characters of the folded name>%'``, and that
    filter was **not** wider than the match it fed. Folding removes spaces, so
    ``"Kiln Systems"`` folds to ``"kilnsystems"`` whose first six characters are
    ``"kilnsy"`` — which never appears in ``"kiln systems"``. Any two-word
    company failed to match its own permanent row. On this path that was a
    missed cache and a wasted provider call; it became a correctness problem the
    moment a second acquisition surface started reading the same evidence,
    because the surface that scanned honestly and the surface that pre-filtered
    would answer differently about the same established company and could end up
    creating two Company rows for it.

    So the prefilter is gone and the comparison runs over the companies that
    carry a domain. At pilot scale that is a small, indexed-by-nothing scan
    measured in milliseconds, and correctness beats a filter that silently loses
    matches. The eventual fix is a stored, indexed normalized-name column, which
    is a schema change worth making on its own evidence rather than folded in
    here.

    **A company standing on a provisional decision is not established evidence.**
    This is the subtle half. A provisional decision creates a permanent Company
    so research can start, which means the very next evaluation would find that
    Company, treat "an existing company already has this domain" as settled, and
    upgrade the same guess to ``confirmed`` — the guess citing itself. Excluding
    provisional-backed companies here is the second half of the same rule that
    keeps a provisional decision out of the approved-mapping store; without both,
    uncertainty launders itself into certainty by a slightly longer route.
    """

    matches: dict[uuid.UUID, policy.ExistingCompanyMatch] = {}

    def consider(company: Company, matched_on: str) -> None:
        if company.id in matches or not company.domain:
            return
        if not _is_established(session, company):
            return
        matches[company.id] = policy.ExistingCompanyMatch(
            company_id=company.id,
            name=company.name,
            domain=company.domain,
            matched_on=matched_on,
        )

    if linkedin_company_id:
        for company in session.scalars(
            select(Company).where(
                Company.linkedin_company_id == linkedin_company_id,
                Company.domain.is_not(None),
            )
        ):
            consider(company, "linkedin_company_id")

    if normalized_name:
        for company in session.scalars(select(Company).where(Company.domain.is_not(None))):
            if policy.normalize_company_name(company.name) == normalized_name:
                consider(company, "normalized_name")

    return tuple(matches.values())


def _is_established(session: Session, company: Company) -> bool:
    """Whether this company's domain is evidence, or merely a repeat of a guess.

    ``None`` means no automatic decision produced this company — it was imported,
    entered, or confirmed by hand — which is exactly the established evidence the
    policy is looking for. ``CONFIRMED`` likewise. Anything else is a company
    whose own domain is not settled, and it cannot settle somebody else's.
    """

    state = store.company_state(session, company.id)
    return state is None or state is DomainResolutionState.CONFIRMED


def gather_evidence(
    session: Session,
    *,
    record: SalesNavCompanyEnrichment | None,
    hints: capture_promotion.CompanyHints,
) -> policy.ResolutionEvidence:
    """Assemble everything the policy may consider, and nothing it may not."""

    normalized = policy.normalize_company_name(hints.name)
    linkedin_company_id = hints.linkedin_id

    approved: frozenset[str] = frozenset()
    candidates: tuple[dict[str, Any], ...] = ()
    lookup_status = EnrichmentLookupStatus.NOT_STARTED
    provider: str | None = None
    model_status = EnrichmentLookupStatus.NOT_STARTED
    model_answer: str | None = None
    model_source: str | None = None
    model_claim: dict[str, Any] | None = None

    if record is not None:
        approved = frozenset(
            capture_promotion.prior_confirmed_domains(
                session,
                company_key_value=record.company_key,
                company_linkedin_id=record.company_linkedin_id,
                exclude_record_id=record.id,
            )
        )
        candidates = tuple(c for c in (record.candidates or []) if isinstance(c, dict))
        lookup_status = record.lookup_status
        provider = record.provider or (enrichment.PROVIDER if candidates else None)
        linkedin_company_id = record.company_linkedin_id or linkedin_company_id
        model_status = record.model_lookup_status
        model_answer = record.model_domain
        model_source = record.model_source_url
        model_claim = record.model_claim if isinstance(record.model_claim, dict) else None

    return policy.ResolutionEvidence(
        company_name=hints.name,
        normalized_company_name=normalized,
        linkedin_company_id=linkedin_company_id,
        approved_mapping_domains=approved,
        existing_companies=_existing_company_matches(
            session, normalized_name=normalized, linkedin_company_id=linkedin_company_id
        ),
        candidates=candidates,
        lookup_status=lookup_status,
        provider=provider,
        model_domain=model_answer,
        model_lookup_status=model_status,
        model_source_url=model_source,
        model_claim=model_claim,
    )


# --- Resolving ----------------------------------------------------------------


def resolve(
    session: Session,
    *,
    snapshot: LinkedInProfileSnapshot,
    access: ProviderAccess | None = None,
    model: ModelAccess | None = None,
    actor: str = RESOLUTION_ACTOR,
    force: bool = False,
) -> ResolutionOutcome:
    """Resolve one capture's company domain automatically.

    Without ``force`` a capture that already has a decision is returned as it
    stands: no evidence is re-read, no provider is called, no row is written.
    With ``force`` the evidence is re-gathered and re-judged, and a new decision
    row appears only if the answer actually changed.

    An operator correction is never overwritten by this: :func:`correct` records
    the operator's decision, and a later forced recalculation that would
    contradict it is refused rather than applied silently.
    """

    access = access or ProviderAccess()
    subject = store.ResolutionSubject.for_capture(snapshot.id)
    promotion, record = capture_promotion.ensure_records(session, snapshot)
    existing = _existing_or_none(session, subject=subject, force=force)
    if isinstance(existing, ResolutionOutcome):
        return existing

    hints = capture_promotion.company_hints(snapshot)
    evidence, provider_call_made, model_call_made = _run_lookups(
        session, record=record, hints=hints, access=access, model=model, actor=actor
    )

    decision = policy.evaluate(evidence)
    return _apply(
        session,
        subject=subject,
        snapshot=snapshot,
        promotion=promotion,
        record=record,
        hints=hints,
        evidence=evidence,
        decision=decision,
        kind=(
            DomainResolutionKind.RECALCULATION
            if existing is not None
            else DomainResolutionKind.AUTOMATIC
        ),
        actor=actor,
        provider_call_made=provider_call_made,
        model_call_made=model_call_made,
        audit_action=RESOLVE_AUDIT_ACTION,
        # The automatic path, and only it. An operator correction is a
        # deliberate act on a decision the operator is already looking at, so it
        # keeps its explicit Promote step rather than acquiring a side effect.
        auto_promote=True,
    )


def resolve_contact(
    session: Session,
    *,
    contact: Contact,
    access: ProviderAccess | None = None,
    model: ModelAccess | None = None,
    actor: str = RESOLUTION_ACTOR,
    force: bool = False,
) -> ResolutionOutcome:
    """Resolve the company a permanent Contact states, for a surface with no capture.

    The same process :func:`resolve` runs, about a different subject. A Contact
    that arrived from a spreadsheet carries a company *name* and nothing else;
    this is how that name enters the one company-resolution and evidence path
    the product has, instead of waiting for somebody to re-acquire the same
    person through the browser extension.

    What is identical, and identical because it is the same code: which evidence
    the policy may see, when a provider call is authorized at all, when the model
    fallback is admitted, how the decision is graded, that the decision row keeps
    its candidates and reasons, that a CONFIRMED decision becomes a reusable
    approved mapping and a PROVISIONAL one deliberately does not, and that
    :mod:`app.services.resolution.gates` decides what the result authorizes.

    What is different, and only this: there is no capture to promote, because the
    Contact already exists. A resolved decision links it to the permanent Company
    and fills the company domain it did not have; nothing here creates a person,
    a membership, or a campaign, and nothing here sends.

    Idempotent on the same terms as the capture path: without ``force`` a Contact
    that already has a decision gets it back untouched, with no evidence re-read
    and no provider call. An operator correction is never recalculated over.
    """

    access = access or ProviderAccess()
    subject = store.ResolutionSubject.for_contact(contact.id)
    existing = _existing_or_none(session, subject=subject, force=force)
    if isinstance(existing, ResolutionOutcome):
        # Returning the decision is not enough: the link is what the rest of the
        # product reads. A Contact whose edge was cleared after the decision was
        # made would otherwise be handed back a resolved answer while still
        # looking unresolved to every caller, and re-deciding is not the fix —
        # the decision is already right.
        existing.contact_linked = _link_subject_contact(
            session,
            contact=contact,
            company=existing.company,
            domain=existing.selected_domain,
        )
        return existing

    hints = contact_company_hints(contact)
    if not hints.has_company:
        raise ResolutionError(
            "this contact records no company name, so there is nothing to resolve; "
            "supply the employer before asking for a domain"
        )

    record = enrichment.ensure_contact_record(
        session, contact_id=contact.id, company_name=hints.name
    )
    evidence, provider_call_made, model_call_made = _run_lookups(
        session, record=record, hints=hints, access=access, model=model, actor=actor
    )

    decision = policy.evaluate(evidence)
    return _apply(
        session,
        subject=subject,
        contact=contact,
        record=record,
        hints=hints,
        evidence=evidence,
        decision=decision,
        kind=(
            DomainResolutionKind.RECALCULATION
            if existing is not None
            else DomainResolutionKind.AUTOMATIC
        ),
        actor=actor,
        provider_call_made=provider_call_made,
        model_call_made=model_call_made,
        audit_action=RESOLVE_AUDIT_ACTION,
    )


def contact_company_hints(contact: Contact) -> capture_promotion.CompanyHints:
    """The employer hints a permanent Contact carries, without inferring anything.

    A Contact records the company *name* and nothing else about the company's
    identity — no LinkedIn company page, no company identifier, no role location.
    Those fields are left empty rather than filled from something adjacent: the
    model fallback is told exactly which identifiers it was given, and a guessed
    hint would make a worse answer look better sourced than it is.
    """

    return capture_promotion.CompanyHints(
        name=norm.collapse_whitespace(contact.company_name),
        linkedin_url=None,
        linkedin_id=None,
        location=None,
    )


def _existing_or_none(
    session: Session, *, subject: store.ResolutionSubject, force: bool
) -> ResolutionOutcome | CompanyDomainResolution | None:
    """The decision this subject already has, and what to do about it.

    Returns a finished :class:`ResolutionOutcome` when the caller should simply
    hand that back (a decision exists and nothing was forced), the existing row
    when a recalculation may proceed over it, or ``None`` when this is the first
    evaluation. Shared so both entry points enforce the same two rules: a
    recorded decision is not re-derived for free, and an operator's correction is
    never silently recalculated away.
    """

    existing = store.current_decision(session, subject)
    if existing is None:
        return None
    if not force:
        return ResolutionOutcome(
            decision=existing, created=False, company=_company_of(session, existing)
        )
    if existing.decision_kind is DomainResolutionKind.OPERATOR_CORRECTION:
        raise ResolutionError(
            "an operator has already decided this company's domain by hand; "
            "correct that decision instead of recalculating over it"
        )
    return existing


def _run_lookups(
    session: Session,
    *,
    record: SalesNavCompanyEnrichment | None,
    hints: capture_promotion.CompanyHints,
    access: ProviderAccess,
    model: ModelAccess | None,
    actor: str,
) -> tuple[policy.ResolutionEvidence, bool, bool]:
    """Gather evidence, spending a provider and a model call only where warranted.

    Extracted whole from the capture path so the second acquisition surface runs
    the identical ladder rather than a similar one. Both rules below were argued
    for on the capture path and are unchanged; what changed is that they now
    apply to every surface by construction instead of by resemblance.
    """

    evidence = gather_evidence(session, record=record, hints=hints)

    # The one place a provider call can be authorized: established evidence had
    # nothing to say, no candidates are stored yet, and a key was supplied.
    provider_call_made = False
    if (
        policy.evaluate_established_evidence(evidence) is None
        and record is not None
        and record.lookup_status is EnrichmentLookupStatus.NOT_STARTED
        and access.available
    ):
        enrichment.run_lookup(
            session,
            record=record,
            api_key=access.api_key or "",
            search_url=access.search_url,
            timeout=access.timeout,
            max_candidates=access.max_candidates,
            actor=actor,
            force=False,
            transport=access.transport,
        )
        provider_call_made = True
        evidence = gather_evidence(session, record=record, hints=hints)

    # The model fallback, authorized on exactly one condition: the deterministic
    # path has now run and still cannot name a domain. That question is asked by
    # running the policy — not by inspecting the provider's status here — because
    # the policy is the only thing entitled to say what the provider's answer
    # meant, and "no candidates" and "candidates that all failed alignment" are
    # both cases the fallback serves while being different provider states.
    model_call_made = False
    factory = model.thinker_factory if model is not None else None
    if (
        model is not None
        and factory is not None
        and record is not None
        and record.model_lookup_status is EnrichmentLookupStatus.NOT_STARTED
        and _wants_model_fallback(policy.evaluate(evidence))
    ):
        enrichment.run_model_lookup(
            session,
            record=record,
            thinker=factory(),
            actor=actor,
            timeout_seconds=model.timeout,
            force=False,
        )
        model_call_made = True
        evidence = gather_evidence(session, record=record, hints=hints)

    return evidence, provider_call_made, model_call_made


def correct(
    session: Session,
    *,
    snapshot: LinkedInProfileSnapshot,
    domain: str | None,
    actor: str,
    note: str | None = None,
) -> ResolutionOutcome:
    """Record an operator's correction of a company-domain decision.

    ``domain`` names the right domain; ``None`` means the operator is explicitly
    leaving the company unresolved. Either way the earlier decision — its state,
    its candidates, its reasons — is superseded and kept, never edited away.

    A corrected domain re-points the permanent company link. It deliberately does
    NOT rewrite the contact's captured ``company_domain``: that string is
    captured evidence and dedup input (``natural_key`` is built from it), and
    APP-003 already treats a disagreement between it and the linked company as a
    reviewable identity conflict. Correcting one by silently rewriting the other
    would erase the disagreement instead of surfacing it.
    """

    promotion, record = capture_promotion.ensure_records(session, snapshot)
    hints = capture_promotion.company_hints(snapshot)

    normalized_domain: str | None = None
    if domain is not None:
        normalized_domain = norm.normalize_domain(domain)
        if normalized_domain is None or not norm.is_valid_hostname(normalized_domain):
            raise ResolutionError(
                f"{domain!r} is not a valid company domain; enter a hostname like example.com"
            )

    decision = policy.operator_correction(
        domain=normalized_domain,
        company_name=hints.name,
        normalized_company_name=policy.normalize_company_name(hints.name),
    )
    decision = _with_correction_warnings(session, snapshot, decision, normalized_domain)
    evidence = policy.ResolutionEvidence(
        company_name=hints.name,
        normalized_company_name=policy.normalize_company_name(hints.name),
    )
    return _apply(
        session,
        subject=store.ResolutionSubject.for_capture(snapshot.id),
        snapshot=snapshot,
        promotion=promotion,
        record=record,
        hints=hints,
        evidence=evidence,
        decision=decision,
        kind=DomainResolutionKind.OPERATOR_CORRECTION,
        actor=actor,
        provider_call_made=False,
        audit_action=CORRECT_AUDIT_ACTION,
        correction_note=note,
    )


def _with_correction_warnings(
    session: Session,
    snapshot: LinkedInProfileSnapshot,
    decision: policy.PolicyDecision,
    normalized_domain: str | None,
) -> policy.PolicyDecision:
    """Add the warning an operator needs when a correction leaves a disagreement."""

    if normalized_domain is None:
        return decision
    contact = _promoted_contact(session, snapshot.id)
    if contact is None or contact.company_domain == normalized_domain:
        return decision
    from dataclasses import replace

    return replace(
        decision,
        warnings=(*decision.warnings, policy.WARNING_CORRECTED_DOMAIN_DIFFERS),
    )


# --- Applying a decision ------------------------------------------------------


def _apply(
    session: Session,
    *,
    subject: store.ResolutionSubject,
    record: SalesNavCompanyEnrichment | None,
    hints: capture_promotion.CompanyHints,
    evidence: policy.ResolutionEvidence,
    decision: policy.PolicyDecision,
    kind: DomainResolutionKind,
    actor: str,
    provider_call_made: bool,
    audit_action: str,
    snapshot: LinkedInProfileSnapshot | None = None,
    promotion: ContactCapturePromotion | None = None,
    contact: Contact | None = None,
    model_call_made: bool = False,
    correction_note: str | None = None,
    auto_promote: bool = False,
) -> ResolutionOutcome:
    """Persist a decision and make the world match it.

    Shared by both subjects. ``promotion``/``snapshot`` are the capture path's
    extra work — bringing the DAT-014 promotion record in line and creating the
    Contact the capture implies — and are simply absent for a subject whose
    Contact already exists. Everything above that line is common: the Company
    row, the decision row, the approved-mapping write for a CONFIRMED decision,
    the contact link and the audit event.
    """

    company: Company | None = None
    if decision.selected_domain:
        company = capture_promotion.resolve_company_row(
            session, domain=decision.selected_domain, name=hints.name
        )

    row, created = store.record(
        session,
        subject=subject,
        decision=decision,
        kind=kind,
        actor=actor,
        enrichment_id=record.id if record is not None else None,
        resolved_company_id=company.id if company is not None else None,
        provider_call_made=provider_call_made,
        company_name_original=evidence.company_name,
        company_name_normalized=evidence.normalized_company_name or None,
        correction_note=correction_note,
    )

    if created:
        _apply_to_enrichment(session, record=record, decision=decision, actor=actor)
        if snapshot is not None and promotion is not None:
            _apply_to_promotion(
                session,
                snapshot=snapshot,
                promotion=promotion,
                record=record,
                hints=hints,
                decision=decision,
                company=company,
                actor=actor,
            )

    if promotion is not None:
        contact_linked = _link_promoted_contact(session, promotion=promotion, company=company)
    else:
        contact_linked = _link_subject_contact(
            session, contact=contact, company=company, domain=decision.selected_domain
        )

    # --- DAT-017A: a resolved decision finishes the job ------------------------
    #
    # The policy has already decided, on evidence, and that decision is now
    # persisted. Asking the operator to press Confirm and then Promote at this
    # point asks them to re-affirm a conclusion they did not reach and cannot
    # improve on — the two clicks carried no judgement, only friction.
    #
    # Both resolved states promote, and the difference between them is preserved
    # rather than erased: a PROVISIONAL decision stays provisional on the
    # promotion row, in the decision row and on the page. Creating the Contact is
    # not a promise about the domain. What a provisional domain may go on to
    # authorize is decided by ``app.services.resolution.gates``, in the services
    # that would otherwise act, and none of that changes here.
    #
    # UNRESOLVED never reaches this line: ambiguity, conflict, provider failure
    # and missing evidence are exactly the cases where an operator's judgement is
    # the thing that was missing, so they keep their manual controls.
    promotion_result: capture_promotion.PromotionResult | None = None
    if auto_promote and decision.is_resolved and snapshot is not None:
        promotion_result = _auto_promote(session, snapshot=snapshot, actor=actor)

    outcome = ResolutionOutcome(
        decision=row,
        created=created,
        company=company,
        contact_linked=contact_linked,
        provider_call_made=provider_call_made,
        model_call_made=model_call_made,
        promotion_result=promotion_result,
    )

    if created:
        record_audit_event(
            session,
            actor=actor,
            action=audit_action,
            entity_type=_ENTITY_TYPE,
            entity_id=str(row.id),
            new_state=row.state.value,
            reason="automatic company-domain resolution decision",
            context={
                "subject_type": subject.label,
                "subject_id": str(subject.reference),
                # Kept alongside the subject fields rather than replaced by them:
                # every reader written before a second subject existed looks for
                # this key, and a capture decision still has exactly the same
                # answer for it.
                "capture_id": str(subject.capture_id) if subject.capture_id else None,
                "contact_id": str(subject.contact_id) if subject.contact_id else None,
                "decision_number": row.decision_number,
                "decision_kind": row.decision_kind.value,
                **outcome.summary(),
            },
        )
    session.flush()
    return outcome


def _apply_to_enrichment(
    session: Session,
    *,
    record: SalesNavCompanyEnrichment | None,
    decision: policy.PolicyDecision,
    actor: str,
) -> None:
    """Write a CONFIRMED decision back to the DAT-010 candidate store — only that.

    A confirmed decision becomes a reusable approved mapping, which is exactly
    what lets the next capture at the same company resolve without a provider
    call. A **provisional** decision writes nothing here on purpose: if it did,
    :func:`app.services.captures.promotion.prior_confirmed_domains` would read
    it back as an approved mapping and the next capture would confirm from
    evidence nobody ever confirmed — one uncertain guess laundering itself into
    certainty across records.
    """

    if record is None or decision.state is not DomainResolutionState.CONFIRMED:
        return
    if not decision.selected_domain:
        return
    already = (
        record.confirmation_status is EnrichmentConfirmationStatus.CONFIRMED
        and record.confirmed_domain == decision.selected_domain
    )
    if already:
        return
    enrichment.confirm_record(
        session,
        record=record,
        source=EnrichmentConfirmationSource.AUTOMATIC_POLICY,
        domain=decision.selected_domain,
        actor=actor,
        note="confirmed automatically from evidence already on record",
    )


def _apply_to_promotion(
    session: Session,
    *,
    snapshot: LinkedInProfileSnapshot,
    promotion: ContactCapturePromotion,
    record: SalesNavCompanyEnrichment | None,
    hints: capture_promotion.CompanyHints,
    decision: policy.PolicyDecision,
    company: Company | None,
    actor: str,
) -> None:
    """Bring the DAT-014 promotion record in line with the decision."""

    if decision.state is DomainResolutionState.PROVISIONAL and decision.selected_domain:
        promotion.company_outcome = CompanyResolutionOutcome.DOMAIN_PROVISIONAL
        promotion.resolved_domain = decision.selected_domain
        promotion.blocked_reason = None
        if company is not None:
            promotion.resolved_company_id = company.id
        session.flush()
        return

    # CONFIRMED and UNRESOLVED both leave the DAT-014 evaluation authoritative:
    # a confirmation was just written to the enrichment record above, and an
    # unresolved decision must show the specific reason DAT-014 already words
    # (lookup not run, provider unavailable, candidates awaiting review) rather
    # than a flatter one from here.
    capture_promotion.evaluate_company(
        session, promotion=promotion, record=record, hints=hints, actor=actor
    )
    if company is not None:
        promotion.resolved_company_id = company.id
        session.flush()


def _auto_promote(
    session: Session,
    *,
    snapshot: LinkedInProfileSnapshot,
    actor: str,
) -> capture_promotion.PromotionResult:
    """Create or reuse the Contact for a capture whose domain is resolved.

    Delegates wholly to DAT-014's :func:`promote`. That is deliberate: promotion
    already knows how to reuse an existing person, refuse an ambiguous or
    suppressed identity, refuse a capture with no usable name, attach identity
    claims and keep a retry idempotent. Re-implementing any of that here would
    create a second, weaker definition of who a contact is.

    A blocked promotion is returned, not raised. "The domain is settled but this
    person cannot be created yet" is a truthful and fairly common outcome — an
    unnameable row, a suppressed address, an identity two contacts already claim
    — and the reason is recorded on the promotion row for the page to show. It
    is not a failure of the resolution that just succeeded.

    Transactionality is inherited rather than invented. Everything here runs in
    the caller's transaction, so a failure anywhere rolls back the decision, the
    Company, the Contact and the identity links together; there is no point at
    which a Company exists for a Contact that does not.
    """

    return capture_promotion.promote(session, snapshot=snapshot, actor=actor)


def _link_promoted_contact(
    session: Session, *, promotion: ContactCapturePromotion, company: Company | None
) -> bool:
    """Point an already-promoted contact at the resolved company.

    A capture resolved before promotion has no contact yet — the promotion links
    it (see :mod:`app.services.captures.promotion`). This covers the other order:
    a capture promoted first, or a correction that moves an existing contact to a
    different company. Re-pointing one contact's edge is not a merge; both
    company rows survive and any disagreement stays visible.
    """

    if company is None or promotion.promoted_contact_id is None:
        return False
    contact = session.get(Contact, promotion.promoted_contact_id)
    if contact is None or contact.company_id == company.id:
        return False
    contact.company_id = company.id
    session.flush()
    return True


def _link_subject_contact(
    session: Session,
    *,
    contact: Contact | None,
    company: Company | None,
    domain: str | None,
) -> bool:
    """Apply a contact-subject decision to the Contact it was made about.

    The counterpart of what promotion does for a capture: the Contact acquires
    the permanent Company edge, and the company domain it did not have.

    **Why filling ``company_domain`` here is not domain laundering.** The concern
    is real and it is the reason the Sheets intake path refuses to write one: a
    domain that arrives with no decision behind it reads, to every later reader,
    exactly like an established one. This write is the opposite case. The
    decision row exists, it is live, it names its state, and
    :func:`app.services.resolution.store.company_state` reports that state for
    this company to everything that asks — so a PROVISIONAL decision leaves the
    company un-established for :func:`_is_established`, opens company research
    and nothing else through :mod:`app.services.resolution.gates`, and is shown
    as provisional wherever a decision is displayed. It is exactly the position
    a capture-promoted Contact is in, reached by exactly the same policy.

    Only blanks are filled, and the company edge is re-pointed rather than
    merged: two Company rows that turn out to be one organisation stay two and
    are reported as an APP-003 identity conflict, as everywhere else.

    ``natural_key`` is deliberately left alone. It is a dedup fingerprint, and
    minting one for a Contact that already exists can create an ambiguity
    between two live records; that is a change to identity, which belongs to the
    paths that own identity rather than to a domain decision.
    """

    if contact is None or company is None or not domain:
        return False
    changed = False
    if contact.company_id != company.id:
        contact.company_id = company.id
        changed = True
    if not contact.company_domain:
        contact.company_domain = domain
        changed = True
    if changed:
        session.flush()
    return changed


def _promoted_contact(session: Session, capture_id: uuid.UUID) -> Contact | None:
    promotion = capture_promotion.get_promotion(session, capture_id)
    if promotion is None or promotion.promoted_contact_id is None:
        return None
    return session.get(Contact, promotion.promoted_contact_id)


def _company_of(session: Session, decision: CompanyDomainResolution) -> Company | None:
    if decision.resolved_company_id is None:
        return None
    return session.get(Company, decision.resolved_company_id)


# --- Operator view ------------------------------------------------------------


@dataclass
class DecisionView:
    """One decision as the workbench shows it, in plain language."""

    decision: CompanyDomainResolution
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def headline(self) -> str:
        """The state, in the words issue #171 asked for.

        Delegates to :func:`headline_for` rather than reading the state table
        directly, so the specific unresolved wordings — "Provider unavailable",
        "Conflicting candidates" — actually reach the page. Reading the table
        here would have left every unresolved decision saying the same thing
        while the specific words sat unused one function away.
        """

        return headline_for(self.decision)

    @property
    def needs_review(self) -> bool:
        """True for anything an operator should still look at.

        Both ``provisional`` and ``unresolved`` qualify: one is a domain nobody
        has corroborated, the other is no domain at all, and neither should sit
        in a queue that only surfaces failures.
        """

        return self.decision.state is not DomainResolutionState.CONFIRMED

    @property
    def review_label(self) -> str:
        """The headline plus an explicit "Needs review" where one is warranted."""

        return f"{self.headline} · Needs review" if self.needs_review else self.headline

    @property
    def is_provisional(self) -> bool:
        return self.decision.state is DomainResolutionState.PROVISIONAL


_HEADLINES = {
    DomainResolutionState.CONFIRMED: "Domain confirmed",
    DomainResolutionState.PROVISIONAL: "Domain provisional",
    DomainResolutionState.UNRESOLVED: "Domain unresolved",
}

#: Specific unresolved situations that deserve their own words on screen, rather
#: than every unresolved decision reading the same. Keyed by reason code.
_UNRESOLVED_HEADLINES = {
    policy.REASON_PROVIDER_UNAVAILABLE: "Provider unavailable",
    policy.REASON_MULTIPLE_ALIGNED_CANDIDATES: "Conflicting candidates",
    policy.REASON_CONFLICTING_APPROVED_MAPPINGS: "Conflicting candidates",
    policy.REASON_CONFLICTING_EXISTING_COMPANIES: "Conflicting candidates",
    policy.REASON_MAPPING_CONFLICTS_WITH_COMPANY: "Conflicting candidates",
}


def headline_for(decision: CompanyDomainResolution) -> str:
    """The plain-language state shown to an operator."""

    if decision.state is DomainResolutionState.UNRESOLVED:
        for code in decision.reasons or []:
            specific = _UNRESOLVED_HEADLINES.get(str(code))
            if specific:
                return specific
    return _HEADLINES[decision.state]


def build_decision_view(decision: CompanyDomainResolution | None) -> DecisionView | None:
    """Expand a stored decision into what the operator surfaces render."""

    if decision is None:
        return None
    return DecisionView(
        decision=decision,
        reasons=policy.explain(decision.reasons, table=policy.REASON_TEXT),
        warnings=policy.explain(decision.warnings, table=policy.WARNING_TEXT),
        candidates=[c for c in (decision.candidates or []) if isinstance(c, dict)],
    )


def capture_view(session: Session, capture_id: uuid.UUID) -> DecisionView | None:
    """The current decision for a capture, ready to render."""

    return build_decision_view(store.current_decision(session, capture_id))


def history_view(session: Session, capture_id: uuid.UUID) -> list[DecisionView]:
    """Every decision for a capture, newest first, ready to render."""

    views = [build_decision_view(d) for d in store.decision_history(session, capture_id)]
    return [v for v in views if v is not None]


def company_view(session: Session, company_id: uuid.UUID) -> DecisionView | None:
    """The strongest current decision behind a permanent company, ready to render."""

    decisions = store.current_decisions_for_company(session, company_id)
    return build_decision_view(decisions[0]) if decisions else None


def contact_view(session: Session, contact: Contact) -> DecisionView | None:
    """The decision behind a contact's company link, ready to render."""

    if contact.company_id is None:
        return None
    return company_view(session, contact.company_id)


def unresolved_captures(session: Session, *, limit: int = 200) -> list[CompanyDomainResolution]:
    """Live *capture* decisions that reached no confirmed domain.

    Restricted to capture subjects because this feeds the Capture page's review
    count, and a Contact acquired without a capture has nothing to review there.
    Its equivalent surfacing is the blocked Agent job on its Campaign, which is
    where an operator is already looking for it.
    """

    return list(
        session.scalars(
            select(CompanyDomainResolution)
            .where(
                CompanyDomainResolution.is_current.is_(True),
                CompanyDomainResolution.capture_id.is_not(None),
                or_(
                    CompanyDomainResolution.state == DomainResolutionState.UNRESOLVED,
                    CompanyDomainResolution.state == DomainResolutionState.PROVISIONAL,
                ),
            )
            .order_by(CompanyDomainResolution.decided_at)
            .limit(limit)
        )
    )

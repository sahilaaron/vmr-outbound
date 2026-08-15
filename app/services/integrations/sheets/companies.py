"""Turning a company *name* from a spreadsheet cell into a permanent Company.

The Company Agent needs two things before it will do anything: a
``contact.company_domain`` and exactly one permanent ``Company`` carrying that
exact domain. The Chrome capture path satisfies both before enrolment, through
``app/services/resolution``. A spreadsheet row has to reach the same place, and
this module is the shortest honest route — it reuses the same policy, the same
provider client and the same Company writer, and adds no judgement of its own.

What is reused, exactly
-----------------------

* ``resolution.policy.ResolutionEvidence`` — the same evidence shape, built from
  the same two public inputs: domains an operator has already confirmed for this
  name (``captures.promotion.prior_confirmed_domains``) and permanent Companies
  whose own domain is established (``resolution.store.company_state``).
* ``resolution.policy.evaluate`` — pure, unchanged, and the only thing allowed to
  decide. This module never picks a domain; it presents evidence and stores the
  answer.
* ``captures.promotion.resolve_company_row`` — the one existing writer that
  creates or reuses a ``Company`` for a domain, with no capture dependency.

The one rule this surface adds, and why
---------------------------------------

**Only ``CONFIRMED`` is accepted. ``PROVISIONAL`` is refused.** A provisional
domain is the policy saying "a matcher or a model suggested this and nothing has
established it". On the capture path that state is safe because it is *recorded*:
it lands in ``company_domain_resolutions``, the downstream gates read it, and the
stages that spend money stay shut until an operator confirms. That ledger is
keyed per capture and a spreadsheet row is not a capture, so a provisional domain
accepted here would carry no such record — and a company created from it would
read, to every later reader, exactly like one whose domain was established. That
is domain laundering, and refusing the state outright is the only version of this
that cannot do it by accident.

The operator-visible consequence is stated plainly in the sheet: the row says the
company could not be identified, and the fix is a more exact company name, or
capturing the person through the extension where the confirm-a-domain workflow
exists. It is a smaller product than "we guessed", and it is the true one.

Cost behaviour
--------------

At most **one** logo.dev call per distinct company name per submission. A name
that resolves creates a permanent Company, so every later row naming that
company — in this batch or any future one — is answered by established evidence
with no provider call at all. A name that does not resolve costs one call and
creates nothing, and the batch ceiling bounds how many of those one click can
buy.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.company import Company
from app.models.enums import DomainResolutionState, EnrichmentLookupStatus
from app.services.audit import record_audit_event
from app.services.captures import promotion as capture_promotion
from app.services.enrichment import companies as enrichment
from app.services.enrichment import logodev
from app.services.operations import settings as operational
from app.services.resolution import policy
from app.services.resolution import store as resolution_store

#: Recorded on every audit event this module writes, so a Company row created
#: from a spreadsheet is distinguishable from one created from a capture.
RESOLVER_VERSION = "sheets-company-resolution/1"

#: What the operator sees when nothing established the domain. One sentence, no
#: provider name, no internal code — the fix is the same whatever the provider
#: said.
UNRESOLVED_MESSAGE = (
    "the company could not be identified from this name; use the company's exact "
    "registered name, or capture this person through the VMR extension"
)

PROVISIONAL_MESSAGE = (
    "the company name matched only a suggestion that nothing has confirmed; "
    "confirm this company through the VMR app before submitting the row again"
)


@dataclass(frozen=True)
class CompanyOutcome:
    """The end of one company-name resolution: a Company, or a reason."""

    company: Company | None
    domain: str | None
    provider_call_made: bool
    reason: str | None = None
    reason_code: str | None = None

    @property
    def resolved(self) -> bool:
        return self.company is not None and self.domain is not None


class NameCache:
    """One resolution per distinct company name within one submission."""

    def __init__(self) -> None:
        self._entries: dict[str, CompanyOutcome] = {}

    def get(self, key: str) -> CompanyOutcome | None:
        return self._entries.get(key)

    def put(self, key: str, outcome: CompanyOutcome) -> None:
        # The cached copy never claims a second provider call was made; only the
        # row that actually bought the lookup reports having bought it.
        self._entries[key] = dataclasses.replace(outcome, provider_call_made=False)


def new_cache() -> NameCache:
    """A per-submission cache. Not global: a cache that outlives a request would
    keep answering with a Company that a later correction has since changed."""

    return NameCache()


def resolve_company(
    session: Session,
    *,
    company_name: str,
    settings: Settings,
    actor: str,
    cache: NameCache | None = None,
) -> CompanyOutcome:
    """Resolve one company name to a permanent Company, or explain why not."""

    key = enrichment.company_key(company_name)
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            return cached
    outcome = _resolve_uncached(session, company_name=company_name, settings=settings, actor=actor)
    if cache is not None:
        cache.put(key, outcome)
    return outcome


def _resolve_uncached(
    session: Session,
    *,
    company_name: str,
    settings: Settings,
    actor: str,
) -> CompanyOutcome:
    evidence = _evidence_for(session, company_name=company_name)

    settled = policy.evaluate_established_evidence(evidence)
    if settled is not None and settled.state is DomainResolutionState.CONFIRMED:
        return _accept(
            session,
            decision=settled,
            company_name=company_name,
            actor=actor,
            provider_call_made=False,
        )

    provider_call_made = False
    if settled is None and _provider_available(session, settings):
        evidence = _with_provider_candidates(evidence, company_name=company_name, settings=settings)
        provider_call_made = True

    decision = policy.evaluate(evidence)

    if decision.state is DomainResolutionState.CONFIRMED:
        return _accept(
            session,
            decision=decision,
            company_name=company_name,
            actor=actor,
            provider_call_made=provider_call_made,
        )
    if decision.state is DomainResolutionState.PROVISIONAL:
        return CompanyOutcome(
            company=None,
            domain=None,
            provider_call_made=provider_call_made,
            reason=PROVISIONAL_MESSAGE,
            reason_code="company_domain_provisional",
        )
    return CompanyOutcome(
        company=None,
        domain=None,
        provider_call_made=provider_call_made,
        reason=UNRESOLVED_MESSAGE,
        reason_code="company_domain_unresolved",
    )


def _evidence_for(session: Session, *, company_name: str) -> policy.ResolutionEvidence:
    """Everything the policy may consider about this name, and nothing it may not.

    The shape is exactly ``resolution.service.gather_evidence`` builds for a
    caller with no enrichment record, and both inputs come from the same public
    helpers that path uses — ``prior_confirmed_domains`` for mappings an operator
    has already confirmed, and permanent Companies for established evidence.

    The existing-Company scan is written here rather than delegated, for one
    concrete reason: ``service._existing_company_matches`` pre-filters candidates
    in SQL with ``LOWER(name) LIKE '%<first six characters of the folded name>%'``,
    and the folded name has had its spaces removed. ``"Kiln Systems"`` folds to
    ``"kilnsystems"``, whose first six characters are ``"kilnsy"``, which does not
    appear in ``"kiln systems"`` — so a two-word company never matches its own
    permanent row. On the capture path that is a missed cache and nothing worse
    (it falls through to a provider lookup), which is why it has gone unnoticed;
    here it would be the difference between a spreadsheet working and not. The
    comparison below is the same one the policy makes, without the prefilter that
    loses it. Fixing the shared helper is a separate change against the capture
    path, and is recorded in the post-launch backlog rather than made here.
    """

    normalized = policy.normalize_company_name(company_name)
    approved = frozenset(
        capture_promotion.prior_confirmed_domains(
            session,
            company_key_value=enrichment.company_key(company_name),
            company_linkedin_id=None,
        )
    )
    matches: list[policy.ExistingCompanyMatch] = []
    if normalized:
        for company in session.scalars(select(Company).where(Company.domain.is_not(None))):
            if policy.normalize_company_name(company.name) != normalized:
                continue
            if not _is_established(session, company):
                continue
            assert company.domain is not None  # narrowed by the query
            matches.append(
                policy.ExistingCompanyMatch(
                    company_id=company.id,
                    name=company.name,
                    domain=company.domain,
                    matched_on="normalized_name",
                )
            )
    return policy.ResolutionEvidence(
        company_name=company_name,
        normalized_company_name=normalized,
        linkedin_company_id=None,
        approved_mapping_domains=approved,
        existing_companies=tuple(matches),
    )


def _is_established(session: Session, company: Company) -> bool:
    """Whether this Company's domain is evidence, or merely a repeat of a guess.

    The same rule ``resolution.service`` applies: no automatic decision at all
    (imported, entered or confirmed by hand) or an explicitly confirmed one. A
    company whose own domain is provisional cannot settle somebody else's.
    """

    state = resolution_store.company_state(session, company.id)
    return state is None or state is DomainResolutionState.CONFIRMED


def _provider_available(session: Session, settings: Settings) -> bool:
    """Whether this deployment may spend a logo.dev lookup for a sheet row.

    Both switches are read the same way the capture backfill reads them, through
    the operator-controlled resolver, so turning domain resolution off in the
    Admin screen turns it off here too without a deploy.
    """

    if not settings.has_logo_dev_key():
        return False
    return operational.enabled(
        session, "automatic_company_domain_resolution", settings
    ) and operational.enabled(session, "salesnav_domain_enrichment", settings)


def _with_provider_candidates(
    evidence: policy.ResolutionEvidence,
    *,
    company_name: str,
    settings: Settings,
) -> policy.ResolutionEvidence:
    """One brand-matcher lookup, folded into the evidence the policy reads.

    Every provider condition — no match, rate limited, unreachable, unreadable —
    arrives as a status rather than an exception, and each one is carried into the
    evidence unchanged so the policy can name it in its reasons. A failed lookup
    is therefore an honest "we asked and got nothing", never a silent unresolved.
    """

    try:
        result = logodev.search_brands(
            company_name,
            api_key=settings.logo_dev_api_key or "",
            search_url=settings.logo_dev_search_url,
            timeout=settings.logo_dev_timeout_seconds,
            max_candidates=settings.logo_dev_max_candidates,
        )
    except ValueError:  # pragma: no cover - guarded by `_provider_available`
        return dataclasses.replace(evidence, lookup_status=EnrichmentLookupStatus.API_UNAVAILABLE)

    candidates = tuple(
        {"domain": candidate.domain, "name": candidate.name, "rank": index}
        for index, candidate in enumerate(result.candidates, start=1)
    )
    return dataclasses.replace(
        evidence,
        candidates=candidates,
        lookup_status=result.status,
        provider=enrichment.PROVIDER if candidates else evidence.provider,
    )


def _accept(
    session: Session,
    *,
    decision: policy.PolicyDecision,
    company_name: str,
    actor: str,
    provider_call_made: bool,
) -> CompanyOutcome:
    domain = decision.selected_domain
    if not domain:  # pragma: no cover - the policy's own state/domain invariant
        return CompanyOutcome(
            company=None,
            domain=None,
            provider_call_made=provider_call_made,
            reason=UNRESOLVED_MESSAGE,
            reason_code="company_domain_unresolved",
        )
    company = capture_promotion.resolve_company_row(session, domain=domain, name=company_name)
    record_audit_event(
        session,
        actor=actor,
        action="google_sheets.company_resolved",
        entity_type="company",
        entity_id=str(company.id),
        new_state=domain,
        reason="; ".join(decision.reasons) or "established evidence named this domain",
        context={
            "resolver_version": RESOLVER_VERSION,
            "submitted_company_name": company_name,
            "policy_version": decision.policy_version,
            "state": decision.state.value,
            "provider_call_made": provider_call_made,
            "provider": decision.provider,
        },
    )
    return CompanyOutcome(
        company=company,
        domain=domain,
        provider_call_made=provider_call_made,
    )


__all__ = [
    "PROVISIONAL_MESSAGE",
    "NameCache",
    "RESOLVER_VERSION",
    "UNRESOLVED_MESSAGE",
    "CompanyOutcome",
    "new_cache",
    "resolve_company",
]

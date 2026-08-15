"""Linking a spreadsheet's company *name* to a Company that already exists.

This module answers one narrow question, with free database reads and nothing
else: **has this deployment already established a domain for this company name?**
If it has, the row can be linked to that permanent ``Company`` immediately. If it
has not, that is not a failure and this module says nothing more — the row still
enrols, and establishing the company becomes the canonical pipeline's work.

Why this module no longer resolves anything
-------------------------------------------

It used to. It called logo.dev (and, through the policy, a model fallback),
graded the answer, and **refused the row** unless the result was ``CONFIRMED`` —
which provider evidence can never be in v1. The consequence, found in hosted
UAT, was that a spreadsheet could never introduce a company the product had not
already seen: the first row for every new company came back "could not prepare"
having spent a provider call to say so.

That was a second acquisition pipeline living inside an acquisition surface. The
canonical contract is that a surface supplies evidence and the Agent pipeline
does the intelligence, so the lookup, the grading and the refusal are gone. What
remains is a cache read: established evidence links the row immediately and for
free; anything else leaves ``company_domain`` NULL, which the Contact model
explicitly allows and documents as "not linked yet".

What happens to a company nobody has established
------------------------------------------------

The Contact is created carrying its ``company_name``, enrols, and the pipeline
starts. ``CompanyAgentAdapter`` then raises ``AgentBlocked("company_domain_missing")``
— a documented *non-terminal* condition that pauses the job, moves the stage to
``BLOCKED`` and shows up in the operator's review surfaces with its reason. That
is the canonical representation of "this needs company evidence", and it is the
same one every other blocked stage uses. It is deliberately **not** reinvented
here as an intake refusal.

Note what this does not do: the pipeline has no name-to-domain discovery stage of
its own, so an unseen company still needs operator evidence or a capture before
it advances. The repair moves that work to the canonical, reviewable place; it
does not claim the pipeline will silently solve it.

Domain laundering, unchanged
----------------------------

The previous refusal existed for a real reason: ``company_domain_resolutions`` is
keyed per capture, so a provisional domain accepted here would carry no decision
row and would read, to every later reader, exactly like an established one. That
reasoning still holds and is honoured more simply than before — **no provisional
domain is accepted, because none is ever obtained.** This module reads only
evidence something else already established, so there is nothing uncertain for it
to launder.

Cost behaviour
--------------

**Zero provider calls.** Not "at most one per name" — none. Deciding whether a
row may enter never spends money. Any provider work an unseen company needs
happens later, inside the execution path that owns it and accounts for it.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.enums import DomainResolutionState
from app.services.audit import record_audit_event
from app.services.captures import promotion as capture_promotion
from app.services.enrichment import companies as enrichment
from app.services.resolution import policy
from app.services.resolution import store as resolution_store

#: Recorded on every audit event this module writes, so a Company row created
#: from a spreadsheet is distinguishable from one created from a capture.
RESOLVER_VERSION = "sheets-company-resolution/1"


@dataclass(frozen=True)
class CompanyOutcome:
    """Whether established evidence already names a Company for this name.

    There is no ``reason`` and no ``reason_code``, and their absence is the
    point: "nothing has established this company yet" is an ordinary, expected
    answer that the pipeline handles, not a refusal this surface reports.
    """

    company: Company | None
    domain: str | None
    #: Always ``False``. Kept, rather than deleted, so the submit path's
    #: ``provider_calls_made`` counter survives as an executable invariant: a
    #: regression that reintroduces a lookup into intake makes it non-zero and
    #: fails a test, instead of quietly costing money again.
    provider_call_made: bool = False

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


def link_established_company(
    session: Session,
    *,
    company_name: str,
    actor: str,
    cache: NameCache | None = None,
) -> CompanyOutcome:
    """The permanent Company established evidence already names, or nothing.

    Named for what it now does. It does not "resolve" a company: it looks one up
    among the domains this deployment has already established, and an empty
    answer is success-with-nothing-to-say rather than a refusal.
    """

    key = enrichment.company_key(company_name)
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            return cached
    outcome = _resolve_uncached(session, company_name=company_name, actor=actor)
    if cache is not None:
        cache.put(key, outcome)
    return outcome


def _resolve_uncached(
    session: Session,
    *,
    company_name: str,
    actor: str,
) -> CompanyOutcome:
    """Established evidence only.

    ``evaluate_established_evidence`` reads operator-confirmed mappings and
    permanent Companies whose own domain is established — both free database
    reads. ``policy.evaluate`` is deliberately *not* called: it is the function
    that grades provider candidates, and this path has none to grade. Anything
    short of ``CONFIRMED`` means "not established yet", which is not this
    module's problem to solve.
    """

    evidence = _evidence_for(session, company_name=company_name)
    settled = policy.evaluate_established_evidence(evidence)
    if settled is not None and settled.state is DomainResolutionState.CONFIRMED:
        return _accept(session, decision=settled, company_name=company_name, actor=actor)
    return CompanyOutcome(company=None, domain=None)


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


def _accept(
    session: Session,
    *,
    decision: policy.PolicyDecision,
    company_name: str,
    actor: str,
) -> CompanyOutcome:
    domain = decision.selected_domain
    if not domain:  # pragma: no cover - the policy's own state/domain invariant
        return CompanyOutcome(company=None, domain=None)
    company = capture_promotion.resolve_company_row(session, domain=domain, name=company_name)
    record_audit_event(
        session,
        actor=actor,
        action="google_sheets.company_linked",
        entity_type="company",
        entity_id=str(company.id),
        new_state=domain,
        reason="; ".join(decision.reasons) or "established evidence named this domain",
        context={
            "resolver_version": RESOLVER_VERSION,
            "submitted_company_name": company_name,
            "policy_version": decision.policy_version,
            "state": decision.state.value,
            "provider_call_made": False,
            "provider": decision.provider,
        },
    )
    return CompanyOutcome(company=company, domain=domain)


__all__ = [
    "NameCache",
    "RESOLVER_VERSION",
    "CompanyOutcome",
    "link_established_company",
    "new_cache",
]

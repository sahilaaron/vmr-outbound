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
starts. ``CompanyAgentAdapter`` reaches the company stage, finds a Contact with a
name and no company, and asks the shared company-domain resolution process to
establish one — the same process, evidence, provider ladder, policy and decision
ledger a Chrome capture goes through. The decision it records is about the
Contact rather than about a capture, which is the whole of the difference; see
``app.services.resolution.store.ResolutionSubject``.

When that process cannot name a domain — nothing established, no provider
configured, conflicting candidates, or the policy declining to guess — the Agent
raises ``AgentBlocked("company_domain_missing")`` carrying what resolution
actually tried. That is a documented *non-terminal* condition that pauses the
job, moves the stage to ``BLOCKED`` and shows up in the operator's review
surfaces with its reason. It is deliberately **not** reinvented here as an
intake refusal.

Domain laundering, unchanged
----------------------------

The original refusal existed for a real reason: a provisional domain accepted
*here* would carry no decision row and would read, to every later reader, exactly
like an established one. That reasoning still holds and is honoured the same way
— **no provisional domain is accepted at intake, because none is ever obtained
here.** This module reads only evidence something else already established.

A provisional domain established later, by the Agent, is a different thing and is
safe for the opposite reason: it has a live decision row naming its state, so
``store.company_state`` reports it as provisional to everything that asks, the
company is not treated as established evidence for the next company, and
``resolution.gates`` opens company research and nothing else.

Cost behaviour
--------------

**Zero provider calls.** Not "at most one per name" — none. Deciding whether a
row may enter never spends money. Any provider work an unseen company needs
happens later, in the Company Agent, inside the durable worker that owns that
stage and accounts for its cost.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.enums import DomainResolutionState
from app.services.audit import record_audit_event
from app.services.captures import promotion as capture_promotion
from app.services.enrichment import companies as enrichment
from app.services.provenance import supplied_inputs
from app.services.resolution import policy
from app.services.resolution import service as resolution_service

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


def link_supplied_domain(
    session: Session,
    *,
    company_name: str,
    domain: str,
    origin: str,
    actor: str,
) -> CompanyOutcome:
    """The permanent Company for a domain the operator gave, created if new.

    This is the one place this module writes a Company the deployment had not
    already established, and it is safe for a reason that does not apply to
    anything else here: the domain is an **operator assertion**, not a provider
    candidate. The refusal this module documents at length exists to stop a
    graded, uncorroborated *lookup result* entering as though it were
    established; a website typed into the sheet by the person who knows the
    prospect is the same class of evidence the file import already treats as its
    second-strongest company signal (``CompanyBasis.WEBSITE_DOMAIN``), and it
    creates a Company from it in exactly this way.

    ``origin`` says which of the two operator assertions this was — a website
    they typed, or the employer half of an address they typed. It reaches only
    the audit event: the two are the same *kind* of evidence and must produce the
    same Company, so branching on it here would be the start of two company
    identities for one domain. Where they differ is in what the record says they
    were, which is what the audit event and the enrolment provenance carry. The
    file import draws exactly this distinction with
    ``CompanyBasis.WEBSITE_DOMAIN`` and ``CompanyBasis.EMAIL_DOMAIN``, both of
    which create a company the same way.

    No resolution decision is written, deliberately. A decision row means "the
    automatic resolution policy spoke about this company", and it did not — it
    was never asked. ``store.company_state`` therefore reports ``None`` for this
    Company, which ``resolution.gates`` already documents as unrestricted: a
    domain that did not come from automatic resolution is not something this task
    retroactively cast doubt on.

    Creating the row is what makes the supplied domain *useful* rather than
    harmful. ``CompanyAgentAdapter`` looks the Contact's domain up among permanent
    Companies and blocks with ``company_missing`` when none matches — so a domain
    recorded on the Contact and nowhere else would have left every supplied-domain
    row worse off than one that supplied nothing at all.
    """

    company = capture_promotion.resolve_company_row(session, domain=domain, name=company_name)
    record_audit_event(
        session,
        actor=actor,
        action="google_sheets.company_linked_from_supplied_domain",
        entity_type="company",
        entity_id=str(company.id),
        new_state=domain,
        reason=(
            "the operator supplied this company website in the spreadsheet"
            if origin == supplied_inputs.DOMAIN_SOURCE_WEBSITE
            else "this domain is the employer half of the address the operator supplied"
        ),
        context={
            "resolver_version": RESOLVER_VERSION,
            "submitted_company_name": company_name,
            "supplied_domain": domain,
            "domain_source": origin,
            "provider_call_made": False,
            "domain_resolution_performed": False,
        },
    )
    return CompanyOutcome(company=company, domain=domain)


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

    Delegated to ``resolution.service.gather_evidence`` with no enrichment record,
    which is exactly the shape that path builds for a caller with no provider
    candidates. It is delegated rather than reimplemented so this surface cannot
    answer "has this company been established?" differently from the Agent that
    will ask the same question about the same name a moment later — two readings
    of established evidence is how one company ends up with two Company rows.

    (This module used to keep its own copy of the existing-Company scan, because
    the shared helper's SQL prefilter dropped any two-word company. That prefilter
    is gone; see ``service._existing_company_matches``.)
    """

    return resolution_service.gather_evidence(
        session,
        record=None,
        hints=capture_promotion.CompanyHints(
            name=company_name,
            linkedin_url=None,
            linkedin_id=None,
            location=None,
        ),
    )


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
    "link_supplied_domain",
    "new_cache",
]

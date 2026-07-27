"""Versioned, deterministic company-domain resolution policy (DAT-017).

The question this module answers is narrow and worth stating precisely:

    Given everything the system already knows about one company, is there
    enough evidence to select its domain **without asking an operator**?

Everything here is a pure function over an evidence bundle. No database, no
provider, no clock, no randomness — so the same evidence always produces the
same decision, and a decision can be replayed and audited later from the stored
evidence alone. Gathering the evidence and acting on the decision belong to
:mod:`app.services.captures.domain_resolution`.

Why a policy rather than a heuristic
------------------------------------

The previous behaviour was that a provider's candidate list always went to an
operator, because *provider rank is not confidence*. That is still true, and
this policy does not weaken it: a logo.dev result never auto-confirms on its own,
no matter how highly it ranks or how alone it is. A single uncorroborated
provider result is exactly the case where a plausible-looking wrong domain does
the most damage — an email sent to a stranger's company.

What changed is that the system now has a second, independent kind of evidence
it was not consulting: the **website domain an operator captured from LinkedIn's
own company page**, keyed by the same LinkedIn company identifier the person
capture recorded. That is not a name guess; it is first-party evidence about a
specific company entity. When it agrees with a provider candidate, two
independent sources have named the same domain, and asking a human to retype
what two independent sources already agree on is not judgement — it is friction.

Evidence axes
-------------

An *axis* is a source of evidence that could be wrong on its own but is unlikely
to be wrong in the same direction as another axis. Corroboration is counted
across axes, never within one:

``PRIOR_MAPPING``
    A domain an operator already confirmed for this same normalized company.
``COMPANY_PAGE``
    ``linkedin_company_snapshots.website_domain`` from an operator-opened
    company page. *Identity-matched* when joined on the exact LinkedIn company
    id or the exact normalized company URL; otherwise name-matched and weaker.
``CANONICAL_COMPANY``
    An existing ``companies`` row already carrying this domain, reached through
    an identity-grade join.
``PROVIDER_CANDIDATE``
    A logo.dev candidate. Never decisive alone.

Name-derived agreement (the candidate's brand name matching the captured company
name, or the domain's own label matching it) is recorded as a *note* on the
evidence, not as an axis. Both are derived from the same string that produced
the provider query, so counting them as independent corroboration would be
circular — it would let a company called "Apex" auto-confirm ``apex.com`` purely
because the name matches itself.

The decision rules, in order
----------------------------

1. Two authoritative axes naming **different** domains is a ``CONFLICT``. It is
   checked first, because a conflict discovered after a selection is a silently
   wrong answer, and a wrong domain is worse than an unanswered one.
2. A single prior operator mapping, unopposed, is ``PRIOR_MAPPING_REUSED``.
   Replaying a decision the operator already made is not a new decision.
3. ``AUTO_CONFIRMED`` when either two independent axes agree on one domain, or a
   single identity-matched company-page domain names it.
4. A reachable provider that returned nothing usable, with no other evidence, is
   ``NO_CREDIBLE_CANDIDATE`` — distinct from an unreachable one.
5. An unreachable provider with no other evidence is ``PROVIDER_UNAVAILABLE``.
   No domain is ever invented to fill the gap.
6. Everything else is ``REVIEW_REQUIRED``, carrying a recommendation that is
   surfaced but never applied.

Conservative by construction, but not lazily so: the default is review only when
the evidence genuinely does not settle the question, never because review is the
safer thing to write.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.models.enums import DomainResolutionDecision, EnrichmentLookupStatus

__all__ = [
    "POLICY_NAME",
    "POLICY_VERSION",
    "EvidenceAxis",
    "Reason",
    "DomainEvidence",
    "ResolutionInput",
    "ResolutionDecision",
    "brand_name_agrees",
    "domain_label_agrees",
    "decide",
]

POLICY_NAME = "company-domain-resolution"
POLICY_VERSION = f"{POLICY_NAME}/1.0.0"


class EvidenceAxis:
    """Independent sources of domain evidence.

    A plain namespace of stable strings rather than an enum: these values are
    written into JSONB evidence records that must stay readable after the policy
    version moves on, and a future version may add an axis without a migration.
    """

    PRIOR_MAPPING = "prior_mapping"
    COMPANY_PAGE = "company_page"
    CANONICAL_COMPANY = "canonical_company"
    PROVIDER_CANDIDATE = "provider_candidate"

    #: Axes whose evidence can settle the question on its own terms. A provider
    #: candidate is deliberately absent.
    AUTHORITATIVE = frozenset({PRIOR_MAPPING, COMPANY_PAGE, CANONICAL_COMPANY})


class Reason:
    """Stable reason codes. Stored, displayed, and asserted on in tests."""

    PRIOR_MAPPING_SINGLE = "prior_mapping_single"
    PRIOR_MAPPING_CONFLICT = "prior_mapping_conflict"
    AUTHORITATIVE_CONFLICT = "authoritative_conflict"
    COMPANY_PAGE_IDENTITY_MATCH = "company_page_identity_match"
    COMPANY_PAGE_NAME_MATCH_ONLY = "company_page_name_match_only"
    CANONICAL_COMPANY_MATCH = "canonical_company_match"
    PROVIDER_CANDIDATE_CORROBORATED = "provider_candidate_corroborated"
    PROVIDER_CANDIDATE_UNCORROBORATED = "provider_candidate_uncorroborated"
    PROVIDER_MULTIPLE_CANDIDATES = "provider_multiple_candidates"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_NO_CANDIDATES = "provider_no_candidates"
    LOOKUP_NOT_RUN = "lookup_not_run"
    NO_EVIDENCE = "no_evidence"
    BRAND_NAME_AGREEMENT = "brand_name_agreement"
    DOMAIN_LABEL_AGREEMENT = "domain_label_agreement"
    INDEPENDENT_AXES_AGREE = "independent_axes_agree"


#: Lookup states meaning the provider could not be reached or answered
#: unusably. A retry may succeed; nothing may be concluded from them.
_UNAVAILABLE_STATUSES = frozenset(
    {
        EnrichmentLookupStatus.API_UNAVAILABLE,
        EnrichmentLookupStatus.RATE_LIMITED,
        EnrichmentLookupStatus.MALFORMED,
        EnrichmentLookupStatus.ERROR,
    }
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Suffixes that carry no identity: "Acme Ltd" and "Acme" are the same brand for
# the purpose of comparing a name against a domain label. Kept deliberately
# short — this is a comparison aid, never a matcher in its own right.
_LEGAL_SUFFIXES = (
    "incorporated", "inc", "llc", "llp", "ltd", "limited", "plc", "gmbh",
    "bv", "nv", "ab", "as", "oy", "sa", "srl", "spa", "pty", "pte",
    "corp", "corporation", "co", "company", "group", "holdings", "holding",
)  # fmt: skip


def _fold(value: str | None) -> str:
    """Reduce a name or label to comparable letters and digits."""

    if not value:
        return ""
    return _NON_ALNUM.sub("", value.strip().lower())


def _strip_legal_suffix(folded: str) -> str:
    for suffix in _LEGAL_SUFFIXES:
        if folded.endswith(suffix) and len(folded) > len(suffix) + 1:
            return folded[: -len(suffix)]
    return folded


def _registrable_label(domain: str | None) -> str:
    """The first label of a hostname — ``acme`` from ``acme.co.uk``."""

    if not domain:
        return ""
    return _fold(domain.split(".", 1)[0])


def brand_name_agrees(company_name: str | None, candidate_name: str | None) -> bool:
    """Whether a provider's brand name is the captured company name.

    Exact after folding and dropping a trailing legal suffix. Nothing fuzzy:
    "Acme Systems" and "Acme" are different companies until something other than
    string similarity says otherwise.
    """

    left = _strip_legal_suffix(_fold(company_name))
    right = _strip_legal_suffix(_fold(candidate_name))
    return bool(left) and left == right


def domain_label_agrees(company_name: str | None, domain: str | None) -> bool:
    """Whether a domain's own label spells the captured company name."""

    left = _strip_legal_suffix(_fold(company_name))
    right = _registrable_label(domain)
    return bool(left) and left == right


@dataclass(frozen=True)
class DomainEvidence:
    """One source naming one domain.

    ``identity_matched`` distinguishes evidence reached through an exact
    identifier (a LinkedIn company id, a normalized company URL) from evidence
    reached through a name. The difference matters: names collide, identifiers
    do not.
    """

    domain: str
    axis: str
    identity_matched: bool = False
    source_ref: str | None = None
    notes: tuple[str, ...] = ()
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """A JSON-safe record. This is what makes a decision replayable."""

        return {
            "domain": self.domain,
            "axis": self.axis,
            "identity_matched": self.identity_matched,
            "source_ref": self.source_ref,
            "notes": list(self.notes),
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class ResolutionInput:
    """Everything the policy is allowed to consider."""

    company_key: str
    company_name: str | None
    company_linkedin_id: str | None
    lookup_status: EnrichmentLookupStatus
    evidence: tuple[DomainEvidence, ...] = ()

    @property
    def provider_unavailable(self) -> bool:
        return self.lookup_status in _UNAVAILABLE_STATUSES

    @property
    def lookup_ran(self) -> bool:
        return self.lookup_status is not EnrichmentLookupStatus.NOT_STARTED


@dataclass(frozen=True)
class ResolutionDecision:
    """The policy's answer, with the reasoning that produced it."""

    decision: DomainResolutionDecision
    domain: str | None
    reasons: tuple[str, ...]
    policy_version: str = POLICY_VERSION
    #: For review cases: the domain the policy would suggest. Surfaced to an
    #: operator, never applied. A recommendation is not a decision.
    recommendation: str | None = None
    supporting_axes: tuple[str, ...] = ()

    @property
    def is_automatic(self) -> bool:
        return self.decision in (
            DomainResolutionDecision.AUTO_CONFIRMED,
            DomainResolutionDecision.PRIOR_MAPPING_REUSED,
        )

    @property
    def needs_review(self) -> bool:
        return self.decision in (
            DomainResolutionDecision.REVIEW_REQUIRED,
            DomainResolutionDecision.CONFLICT,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "domain": self.domain,
            "reasons": list(self.reasons),
            "policy_version": self.policy_version,
            "recommendation": self.recommendation,
            "supporting_axes": list(self.supporting_axes),
        }


def _by_domain(evidence: Iterable[DomainEvidence]) -> dict[str, list[DomainEvidence]]:
    grouped: dict[str, list[DomainEvidence]] = {}
    for item in evidence:
        if not item.domain:
            continue
        grouped.setdefault(item.domain, []).append(item)
    return grouped


def _axes(items: Iterable[DomainEvidence]) -> set[str]:
    return {item.axis for item in items}


def decide(data: ResolutionInput) -> ResolutionDecision:
    """Apply the policy. Pure: same evidence in, same decision out."""

    grouped = _by_domain(data.evidence)

    # --- 1. Conflict, checked before anything can be selected ----------------
    #
    # "Authoritative" here means an axis that could settle the question by
    # itself. Two of them naming different domains is not a tie to be broken by
    # preference order — it is a disagreement about which company this is, and
    # picking a side would be inventing a fact.
    authoritative_domains = {
        domain
        for domain, items in grouped.items()
        if any(
            item.axis in EvidenceAxis.AUTHORITATIVE
            and (item.identity_matched or item.axis == EvidenceAxis.PRIOR_MAPPING)
            for item in items
        )
    }
    prior_domains = sorted(
        domain
        for domain, items in grouped.items()
        if any(item.axis == EvidenceAxis.PRIOR_MAPPING for item in items)
    )

    if len(authoritative_domains) > 1:
        conflict_reasons = [Reason.AUTHORITATIVE_CONFLICT]
        if len(prior_domains) > 1:
            conflict_reasons.append(Reason.PRIOR_MAPPING_CONFLICT)
        return ResolutionDecision(
            decision=DomainResolutionDecision.CONFLICT,
            domain=None,
            reasons=tuple(conflict_reasons),
            recommendation=None,
            supporting_axes=tuple(sorted(EvidenceAxis.AUTHORITATIVE & _axes(data.evidence))),
        )

    # --- 2. A decision the operator already made -----------------------------
    if len(prior_domains) == 1:
        domain = prior_domains[0]
        return ResolutionDecision(
            decision=DomainResolutionDecision.PRIOR_MAPPING_REUSED,
            domain=domain,
            reasons=(Reason.PRIOR_MAPPING_SINGLE,),
            recommendation=None,
            supporting_axes=tuple(sorted(_axes(grouped[domain]))),
        )

    # --- 3. Automatic confirmation -------------------------------------------
    for domain, items in sorted(grouped.items()):
        axes = _axes(items)
        notes = {note for item in items for note in item.notes}

        identity_company_page = any(
            item.axis == EvidenceAxis.COMPANY_PAGE and item.identity_matched for item in items
        )
        independent = len(axes) >= 2

        if not (independent or identity_company_page):
            continue

        reasons: list[str] = []
        if independent:
            reasons.append(Reason.INDEPENDENT_AXES_AGREE)
        if identity_company_page:
            reasons.append(Reason.COMPANY_PAGE_IDENTITY_MATCH)
        if EvidenceAxis.PROVIDER_CANDIDATE in axes and independent:
            reasons.append(Reason.PROVIDER_CANDIDATE_CORROBORATED)
        if EvidenceAxis.CANONICAL_COMPANY in axes:
            reasons.append(Reason.CANONICAL_COMPANY_MATCH)
        # Name agreement never earns the decision; it is recorded because it is
        # part of why the answer looks right, and an operator auditing an
        # automatic decision deserves to see it.
        if Reason.BRAND_NAME_AGREEMENT in notes:
            reasons.append(Reason.BRAND_NAME_AGREEMENT)
        if Reason.DOMAIN_LABEL_AGREEMENT in notes:
            reasons.append(Reason.DOMAIN_LABEL_AGREEMENT)

        return ResolutionDecision(
            decision=DomainResolutionDecision.AUTO_CONFIRMED,
            domain=domain,
            reasons=tuple(reasons),
            recommendation=None,
            supporting_axes=tuple(sorted(axes)),
        )

    # --- 4/5. Nothing to select ----------------------------------------------
    if not grouped:
        if data.provider_unavailable:
            return ResolutionDecision(
                decision=DomainResolutionDecision.PROVIDER_UNAVAILABLE,
                domain=None,
                reasons=(Reason.PROVIDER_UNAVAILABLE,),
            )
        if not data.lookup_ran:
            return ResolutionDecision(
                decision=DomainResolutionDecision.REVIEW_REQUIRED,
                domain=None,
                reasons=(Reason.LOOKUP_NOT_RUN,),
            )
        return ResolutionDecision(
            decision=DomainResolutionDecision.NO_CREDIBLE_CANDIDATE,
            domain=None,
            reasons=(Reason.PROVIDER_NO_CANDIDATES, Reason.NO_EVIDENCE),
        )

    # --- 6. Something is there, but it does not settle the question ----------
    #
    # The recommendation is the single best-supported domain, offered so the
    # operator starts from the strongest option rather than an unordered list.
    # It is explicitly not applied anywhere.
    def _strength(item: tuple[str, list[DomainEvidence]]) -> tuple[int, int, str]:
        domain, items = item
        return (
            len(_axes(items)),
            sum(1 for i in items if i.notes),
            domain,
        )

    best_domain, best_items = max(grouped.items(), key=_strength)
    review_reasons: list[str] = []
    if any(
        item.axis == EvidenceAxis.COMPANY_PAGE and not item.identity_matched
        for items in grouped.values()
        for item in items
    ):
        review_reasons.append(Reason.COMPANY_PAGE_NAME_MATCH_ONLY)
    provider_domains = [
        domain
        for domain, items in grouped.items()
        if any(item.axis == EvidenceAxis.PROVIDER_CANDIDATE for item in items)
    ]
    if len(provider_domains) > 1:
        review_reasons.append(Reason.PROVIDER_MULTIPLE_CANDIDATES)
    elif len(provider_domains) == 1:
        review_reasons.append(Reason.PROVIDER_CANDIDATE_UNCORROBORATED)
    if data.provider_unavailable:
        review_reasons.append(Reason.PROVIDER_UNAVAILABLE)
    if not review_reasons:
        review_reasons.append(Reason.NO_EVIDENCE)

    return ResolutionDecision(
        decision=DomainResolutionDecision.REVIEW_REQUIRED,
        domain=None,
        reasons=tuple(review_reasons),
        recommendation=best_domain,
        supporting_axes=tuple(sorted(_axes(best_items))),
    )

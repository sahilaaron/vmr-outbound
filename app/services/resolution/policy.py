"""The versioned company-domain resolution policy (DAT-017A, practical v1).

Pure and deterministic. Everything this module needs arrives in a
:class:`ResolutionEvidence` record; it touches no database, no provider and no
clock, so a stored decision can be replayed exactly and every branch is testable
without a fixture.

The policy answers one question — *which domain, and how sure are we?* — with
one of three states, in a fixed order of preference:

1. **An approved mapping.** A domain already confirmed for this same normalized
   company (and compatible LinkedIn company identity). Exactly one → CONFIRMED,
   and no provider is asked. More than one → UNRESOLVED, because two earlier
   confirmations disagreeing is precisely the case where guessing is worst.
2. **An existing permanent Company.** A Company whose normalized name — or whose
   LinkedIn company identifier — matches, and which already carries a domain.
   One distinct domain → CONFIRMED. Several → UNRESOLVED.
3. **Provider candidates.** Filtered for validity and suitability, then required
   to *align* with the company name. Exactly one survivor → PROVISIONAL.
   None, or several → UNRESOLVED.

**Provider evidence never reaches CONFIRMED in v1.** This is the load-bearing
decision of the whole module, so it is worth being explicit about why. Issue
#171 defines ``confirmed`` as "sufficient deterministic evidence for normal
downstream use" and ``provisional`` as "likely provider-backed match ... not
independently corroborated". A single provider answering a single query is, by
construction, not independently corroborated — however well its name happens to
match. Corroboration is what DAT-017B / #183 adds, and until it exists the
honest ceiling for a provider candidate is ``provisional``. So CONFIRMED here
means only "something already established says so", which is a claim this task
can actually back up.

That also satisfies the rule the issue states twice: provider rank alone is
never a confirmed decision. Rank is recorded because a reviewer should see what
the provider thought — it is never an input to the state. Note what steps 1 and
2 have in common and step 3 does not: both replay evidence a human or an earlier
confirmed decision already put on record.

**Alignment is exact, not fuzzy.** A candidate aligns when its provider name, or
its domain's registrable label, normalizes to exactly the normalized company
name. Substring and prefix matching were deliberately left out: "Acme" matching
``acmecorp.com``, ``acme-dental.com`` and ``acmeholdings.io`` equally well is
how an automatic resolver quietly attaches people to the wrong company. Exact
matching resolves less and misattributes far less, and an unresolved capture
still reaches an operator with every candidate visible.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.models.enums import DomainResolutionState, EnrichmentLookupStatus
from app.services.imports import normalization as norm

#: The exact rules below. Stored on every decision so an old decision stays
#: interpretable after these rules change — a v1 decision is never re-explained
#: by v2's reasoning.
POLICY_VERSION = "company-domain-resolution/practical-v1"

#: Recorded as the provider on a decision the model fallback produced, so a stored
#: decision says which kind of source named the domain rather than leaving a reader
#: to infer it from the reason codes.
#:
#: Duplicated as a literal rather than imported from
#: ``app.services.enrichment.model_domain``: this module is pure and importing the
#: enrichment package would both invert the dependency and pull a subprocess-capable
#: seam into a module whose whole contract is that it touches nothing. A test asserts
#: the two strings stay equal.
MODEL_PROVIDER = "claude-cli-domain-finder"

# --- Reason codes -------------------------------------------------------------
#
# Stable strings, stored verbatim on the decision. They are the machine-readable
# half of "why"; the operator-facing sentences live in :data:`REASON_TEXT`.

REASON_NO_COMPANY_NAME = "no_company_name_captured"
REASON_REUSED_APPROVED_MAPPING = "reused_approved_company_domain_mapping"
REASON_CONFLICTING_APPROVED_MAPPINGS = "conflicting_approved_mappings"
REASON_MATCHED_EXISTING_COMPANY_NAME = "matched_existing_company_by_normalized_name"
REASON_MATCHED_EXISTING_COMPANY_LINKEDIN = "matched_existing_company_by_linkedin_id"
REASON_CONFLICTING_EXISTING_COMPANIES = "conflicting_existing_companies"
REASON_MAPPING_CONFLICTS_WITH_COMPANY = "approved_mapping_conflicts_with_existing_company"
REASON_PROVIDER_LOOKUP_NOT_RUN = "provider_lookup_not_run"
REASON_PROVIDER_UNAVAILABLE = "provider_unavailable"
REASON_PROVIDER_NO_CANDIDATES = "provider_returned_no_candidates"
REASON_NO_ALIGNED_CANDIDATE = "no_candidate_aligned_with_company_name"
REASON_MULTIPLE_ALIGNED_CANDIDATES = "multiple_plausible_candidates"
REASON_SINGLE_ALIGNED_CANDIDATE = "single_aligned_provider_candidate"
REASON_PROVIDER_EVIDENCE_UNCORROBORATED = "provider_evidence_not_independently_corroborated"
REASON_RANK_IS_NOT_CONFIRMATION = "provider_rank_is_not_confirmation"
REASON_OPERATOR_CORRECTION = "operator_correction"
REASON_OPERATOR_MARKED_UNRESOLVED = "operator_marked_unresolved"

# Model fallback (the searched answer behind the brand matcher).
REASON_MODEL_ASSERTED_DOMAIN = "model_asserted_domain_after_provider_found_none"
REASON_MODEL_EVIDENCE_UNCORROBORATED = "model_evidence_not_independently_corroborated"
REASON_MODEL_NAME_ALIGNMENT_WAIVED = "model_answer_not_subject_to_name_alignment"
REASON_MODEL_LOOKUP_NOT_RUN = "model_domain_lookup_not_run"
REASON_MODEL_NO_ANSWER = "model_could_not_establish_a_domain"
REASON_MODEL_UNAVAILABLE = "model_domain_lookup_unavailable"
REASON_MODEL_ANSWER_UNUSABLE = "model_answer_could_not_be_read_as_a_domain"
REASON_MODEL_DOMAIN_UNSUITABLE = "model_asserted_an_unsuitable_domain"

# Per-candidate rejection codes.
REJECTED_INVALID_DOMAIN = "invalid_domain"
REJECTED_SOCIAL_DOMAIN = "social_network_domain"
REJECTED_DIRECTORY_DOMAIN = "directory_or_data_aggregator_domain"
REJECTED_MARKETPLACE_DOMAIN = "marketplace_domain"
REJECTED_GENERIC_PLATFORM_DOMAIN = "generic_platform_or_mailbox_domain"
REJECTED_PARKED_DOMAIN = "parked_or_registrar_domain"
REJECTED_NAME_NOT_ALIGNED = "company_name_not_aligned"

# --- Warning codes ------------------------------------------------------------

WARNING_PROVISIONAL_LIMITS = "provisional_domain_authorizes_research_only"
WARNING_CANDIDATES_REJECTED = "some_candidates_were_rejected_as_unsuitable"
WARNING_MODEL_ANSWER_NOT_DETERMINISTIC = "domain_came_from_a_model_not_a_deterministic_source"
WARNING_NO_LINKEDIN_COMPANY_ID = "no_linkedin_company_identifier_captured"
WARNING_CORRECTION_SUPERSEDES = "operator_correction_supersedes_an_earlier_decision"
WARNING_CORRECTED_DOMAIN_DIFFERS = "corrected_domain_differs_from_the_contacts_captured_domain"

#: Plain-language sentences for the operator surfaces. The UI never invents its
#: own wording for a reason code, so what an operator reads and what is stored
#: cannot drift apart.
REASON_TEXT: dict[str, str] = {
    REASON_NO_COMPANY_NAME: "The capture showed no company name, so there was nothing to resolve.",
    REASON_REUSED_APPROVED_MAPPING: (
        "Reused a company domain that was already approved for this same company."
    ),
    REASON_CONFLICTING_APPROVED_MAPPINGS: (
        "This company name has been approved with more than one domain before, "
        "so the right one cannot be chosen automatically."
    ),
    REASON_MATCHED_EXISTING_COMPANY_NAME: (
        "An existing company with this exact normalized name already has this domain."
    ),
    REASON_MATCHED_EXISTING_COMPANY_LINKEDIN: (
        "An existing company with this exact LinkedIn company identifier already has this domain."
    ),
    REASON_CONFLICTING_EXISTING_COMPANIES: (
        "More than one existing company matches this name with a different domain."
    ),
    REASON_MAPPING_CONFLICTS_WITH_COMPANY: (
        "The approved mapping and an existing company record name different domains."
    ),
    REASON_PROVIDER_LOOKUP_NOT_RUN: "No domain lookup has been run for this company yet.",
    REASON_PROVIDER_UNAVAILABLE: (
        "The domain provider could not be reached or returned an unusable answer."
    ),
    REASON_PROVIDER_NO_CANDIDATES: "The domain provider returned no candidates for this company.",
    REASON_NO_ALIGNED_CANDIDATE: (
        "No candidate domain matched the captured company name closely enough to be used."
    ),
    REASON_MULTIPLE_ALIGNED_CANDIDATES: (
        "Several candidate domains match this company name equally well."
    ),
    REASON_SINGLE_ALIGNED_CANDIDATE: (
        "Exactly one candidate domain matches the captured company name."
    ),
    REASON_PROVIDER_EVIDENCE_UNCORROBORATED: (
        "Only the domain provider supports this match; nothing has independently confirmed it."
    ),
    REASON_MODEL_ASSERTED_DOMAIN: (
        "The domain provider found nothing, so a model was asked and named this domain."
    ),
    REASON_MODEL_EVIDENCE_UNCORROBORATED: (
        "Only a model's search supports this match; nothing deterministic has confirmed it."
    ),
    REASON_MODEL_NAME_ALIGNMENT_WAIVED: (
        "The name-alignment rule was not applied: a model answers about a named "
        "company rather than offering a list of similar names, and requiring the "
        "domain to spell the company name would reject exactly the companies the "
        "provider already failed on."
    ),
    REASON_MODEL_LOOKUP_NOT_RUN: "The model fallback is switched off, so it was not asked.",
    REASON_MODEL_NO_ANSWER: (
        "The model was asked and reported that it could not establish this company's domain."
    ),
    REASON_MODEL_UNAVAILABLE: "The model could not be reached, so no fallback answer exists.",
    REASON_MODEL_ANSWER_UNUSABLE: (
        "The model answered, but not with a domain that could be read. Worth trying "
        "again — unlike an unreachable model, this usually succeeds on a second ask."
    ),
    REASON_MODEL_DOMAIN_UNSUITABLE: (
        "The model named a domain that cannot be a company's own — a social network, "
        "directory, platform or parked page."
    ),
    REASON_RANK_IS_NOT_CONFIRMATION: (
        "The provider's ranking was recorded but was not treated as evidence."
    ),
    REASON_OPERATOR_CORRECTION: "An operator set this domain by hand.",
    REASON_OPERATOR_MARKED_UNRESOLVED: "An operator deliberately left this company unresolved.",
}

#: Plain-language sentences for warnings, same contract as :data:`REASON_TEXT`.
WARNING_TEXT: dict[str, str] = {
    WARNING_PROVISIONAL_LIMITS: (
        "A provisional domain may start company research only. It cannot qualify this "
        "company, draft outreach, look for an email address, add anyone to a campaign, "
        "or send anything until the identity is confirmed."
    ),
    WARNING_CANDIDATES_REJECTED: (
        "Some candidates were rejected as unusable company domains. They are kept below "
        "with the reason."
    ),
    WARNING_MODEL_ANSWER_NOT_DETERMINISTIC: (
        "This domain came from a model that searched the web, not from a deterministic "
        "lookup. It is worth a glance before you confirm it: a model can be confidently "
        "wrong in a way a name match cannot, and the source page it read is recorded "
        "below when it gave one."
    ),
    WARNING_NO_LINKEDIN_COMPANY_ID: (
        "The capture carried no LinkedIn company identifier, so companies sharing a "
        "display name could not be told apart by it."
    ),
    WARNING_CORRECTION_SUPERSEDES: (
        "This correction supersedes an earlier decision. The earlier decision and its "
        "candidates are kept."
    ),
    WARNING_CORRECTED_DOMAIN_DIFFERS: (
        "The corrected domain differs from the domain captured on the contact. The "
        "contact's captured value is left as it was and the disagreement is reported as "
        "a company identity conflict."
    ),
}


# --- Unsuitable domains -------------------------------------------------------
#
# A brand-search provider answers with whatever brand best matches a string, and
# for a company name it does not know that is very often a platform the company
# merely appears on. Each set below is one way that goes wrong. Membership is
# matched on the host and on any subdomain of it, so ``acme.wixsite.com`` is
# rejected exactly like ``wixsite.com``.
#
# The lists are deliberately conservative in the *other* direction too: a few
# entries (``google.com``, ``cloudflare.com``) are real companies somebody might
# genuinely be captured at. Rejecting them costs an unresolved capture that an
# operator settles by hand; accepting them risks attaching a person to a platform
# they merely use. For an internal-use v1 that trade is worth making, and it is
# recorded here rather than discovered later.

_SOCIAL_DOMAINS = frozenset(
    {
        "linkedin.com",
        "facebook.com",
        "fb.com",
        "twitter.com",
        "x.com",
        "instagram.com",
        "tiktok.com",
        "youtube.com",
        "pinterest.com",
        "reddit.com",
        "snapchat.com",
        "threads.net",
        "vk.com",
        "weibo.com",
        "xing.com",
        "meetup.com",
        "quora.com",
    }
)

_DIRECTORY_DOMAINS = frozenset(
    {
        "crunchbase.com",
        "zoominfo.com",
        "apollo.io",
        "rocketreach.co",
        "lusha.com",
        "glassdoor.com",
        "indeed.com",
        "yelp.com",
        "bbb.org",
        "dnb.com",
        "owler.com",
        "pitchbook.com",
        "tracxn.com",
        "clutch.co",
        "g2.com",
        "capterra.com",
        "trustpilot.com",
        "yellowpages.com",
        "manta.com",
        "bloomberg.com",
        "reuters.com",
        "forbes.com",
        "wikipedia.org",
        "wikidata.org",
        "opencorporates.com",
        "zaubacorp.com",
        "tofler.in",
        "justdial.com",
        "indiamart.com",
        "ambitionbox.com",
    }
)

_MARKETPLACE_DOMAINS = frozenset(
    {
        "amazon.com",
        "ebay.com",
        "etsy.com",
        "alibaba.com",
        "aliexpress.com",
        "walmart.com",
        "flipkart.com",
        "upwork.com",
        "fiverr.com",
        "udemy.com",
        "coursera.org",
        "booking.com",
        "tripadvisor.com",
    }
)

_GENERIC_PLATFORM_DOMAINS = frozenset(
    {
        # Mailbox providers — never a company's own domain in this context.
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "aol.com",
        "icloud.com",
        "protonmail.com",
        "proton.me",
        "zoho.com",
        "yandex.com",
        "qq.com",
        "163.com",
        "mail.ru",
        "rediffmail.com",
        # Platforms a company is hosted on or published through.
        "google.com",
        "googleusercontent.com",
        "sites.google.com",
        "business.site",
        "github.com",
        "github.io",
        "gitlab.com",
        "bitbucket.org",
        "medium.com",
        "substack.com",
        "notion.so",
        "notion.site",
        "wordpress.com",
        "wix.com",
        "wixsite.com",
        "squarespace.com",
        "weebly.com",
        "webflow.io",
        "blogspot.com",
        "tumblr.com",
        "netlify.app",
        "vercel.app",
        "herokuapp.com",
        "firebaseapp.com",
        "pages.dev",
        "glitch.me",
        "carrd.co",
        "strikingly.com",
        "jimdo.com",
        "godaddysites.com",
        "canva.site",
        "framer.website",
        "myshopify.com",
        "shopify.com",
        "bigcartel.com",
        "cloudflare.com",
        "linktr.ee",
    }
)

_PARKED_DOMAINS = frozenset(
    {
        "godaddy.com",
        "sedo.com",
        "sedoparking.com",
        "hugedomains.com",
        "afternic.com",
        "dan.com",
        "namecheap.com",
        "parkingcrew.net",
        "bodis.com",
        "above.com",
        "undeveloped.com",
        "domainmarket.com",
        "brandbucket.com",
        "squadhelp.com",
        "atom.com",
        "namesilo.com",
        "dynadot.com",
        "porkbun.com",
        "networksolutions.com",
        "register.com",
        "name.com",
        "enom.com",
        "tucows.com",
        "uniregistry.com",
        "epik.com",
    }
)

_UNSUITABLE: tuple[tuple[frozenset[str], str], ...] = (
    (_SOCIAL_DOMAINS, REJECTED_SOCIAL_DOMAIN),
    (_DIRECTORY_DOMAINS, REJECTED_DIRECTORY_DOMAIN),
    (_MARKETPLACE_DOMAINS, REJECTED_MARKETPLACE_DOMAIN),
    (_GENERIC_PLATFORM_DOMAINS, REJECTED_GENERIC_PLATFORM_DOMAIN),
    (_PARKED_DOMAINS, REJECTED_PARKED_DOMAIN),
)

# Legal-form tokens dropped from the end of a company name before comparing.
# Only true legal forms: words like "group" and "holdings" are part of a name
# and usually part of the domain too, so removing them would break the exact
# match this policy depends on rather than help it.
_LEGAL_FORM_TOKENS = frozenset(
    {
        "inc",
        "incorporated",
        "llc",
        "lc",
        "llp",
        "lp",
        "ltd",
        "limited",
        "plc",
        "corp",
        "corporation",
        "co",
        "company",
        "gmbh",
        "mbh",
        "ag",
        "kg",
        "ug",
        "bv",
        "nv",
        "sa",
        "sas",
        "sarl",
        "srl",
        "spa",
        "ab",
        "as",
        "oy",
        "aps",
        "pty",
        "pte",
        "pvt",
        "private",
        "sl",
        "sro",
        "doo",
        "dmcc",
        "fzco",
        "fze",
        "kk",
        "kft",
        "zrt",
    }
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Alignment kinds, recorded per candidate so a reviewer sees which axis matched.
ALIGNMENT_PROVIDER_NAME = "provider_name"
ALIGNMENT_DOMAIN_LABEL = "domain_label"
ALIGNMENT_BOTH = "provider_name+domain_label"

# Provider statuses that mean "ask again later" rather than "asked, and there is
# genuinely nothing". Mirrors the DAT-014 set so the two cannot drift.
_RETRYABLE_LOOKUP_STATUSES = frozenset(
    {
        EnrichmentLookupStatus.API_UNAVAILABLE,
        EnrichmentLookupStatus.RATE_LIMITED,
        EnrichmentLookupStatus.MALFORMED,
        EnrichmentLookupStatus.ERROR,
    }
)


def normalize_company_name(value: str | None) -> str:
    """Fold a company name to the form the policy compares.

    Case, punctuation, spacing and a trailing legal form all vary between a
    LinkedIn page, a Company row and a provider's brand name while naming the
    same organisation, so all four are removed: ``"Acme Solutions, Inc."`` and
    ``"acme  solutions"`` both fold to ``"acmesolutions"``.

    Returns ``""`` for a name that folds to nothing, which the policy treats as
    "no company name" rather than as a match against anything.
    """

    collapsed = norm.collapse_whitespace(value)
    if not collapsed:
        return ""
    lowered = collapsed.casefold().replace("&", " and ")
    tokens = [token for token in _NON_ALNUM.split(lowered) if token]
    # Strip legal forms only from the end, and never the entire name: a company
    # genuinely called "Limited" keeps its name rather than folding to nothing.
    while len(tokens) > 1 and tokens[-1] in _LEGAL_FORM_TOKENS:
        tokens.pop()
    return "".join(tokens)


def registrable_label(domain: str) -> str:
    """The comparable label of a domain: ``acme-solutions.co.uk`` → ``acmesolutions``.

    Takes the label to the left of the public suffix as far as a suffix list can
    be avoided — a two-part suffix is recognised only from a small set of common
    ones, because shipping (and ageing) a full public-suffix list is not
    warranted for an internal v1. A wrong split makes a candidate fail to align,
    which leaves the capture unresolved rather than misattributed.
    """

    parts = [p for p in domain.lower().split(".") if p]
    if len(parts) < 2:
        return _NON_ALNUM.sub("", domain.lower())
    if len(parts) >= 3 and ".".join(parts[-2:]) in _TWO_PART_SUFFIXES:
        label = parts[-3]
    else:
        label = parts[-2]
    return _NON_ALNUM.sub("", label)


_TWO_PART_SUFFIXES = frozenset(
    {
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "co.in",
        "net.in",
        "org.in",
        "co.jp",
        "co.nz",
        "co.za",
        "com.au",
        "net.au",
        "org.au",
        "com.br",
        "com.mx",
        "com.sg",
        "com.hk",
        "com.tr",
        "com.cn",
        "co.kr",
        "com.ar",
        "com.my",
        "com.ph",
        "co.id",
    }
)


def unsuitable_reason(domain: str) -> str | None:
    """Why *domain* cannot be a company's own domain, or None if it can be.

    Checks the host itself and every parent of it, so a subdomain of a platform
    is rejected with the platform's reason rather than sliding through.
    """

    host = domain.lower().strip(".")
    if not host:
        return REJECTED_INVALID_DOMAIN
    for blocked, reason in _UNSUITABLE:
        for candidate in _host_and_parents(host):
            if candidate in blocked:
                return reason
    return None


def _host_and_parents(host: str) -> list[str]:
    parts = host.split(".")
    return [".".join(parts[index:]) for index in range(len(parts) - 1)]


# --- Evidence -----------------------------------------------------------------


@dataclass(frozen=True)
class ExistingCompanyMatch:
    """A permanent Company that already claims this identity AND a domain.

    Only companies that carry a domain are gathered: one with the right name and
    no domain proves nothing about which domain is right, and treating it as a
    match would confirm from an absence.
    """

    company_id: uuid.UUID
    name: str
    domain: str
    #: ``"normalized_name"`` or ``"linkedin_company_id"``.
    matched_on: str


@dataclass(frozen=True)
class ResolutionEvidence:
    """Everything the policy is allowed to consider, gathered before it runs."""

    company_name: str | None
    normalized_company_name: str
    linkedin_company_id: str | None = None
    #: Domains already CONFIRMED for this same normalized company elsewhere.
    approved_mapping_domains: frozenset[str] = frozenset()
    existing_companies: tuple[ExistingCompanyMatch, ...] = ()
    #: Provider candidates as DAT-010 stored them: ``domain``, ``name``, ``rank``.
    candidates: tuple[dict[str, Any], ...] = ()
    lookup_status: EnrichmentLookupStatus = EnrichmentLookupStatus.NOT_STARTED
    provider: str | None = None
    #: The domain a model asserted, and the status of having asked. Consulted only
    #: where the deterministic path produced nothing usable — see :func:`evaluate`.
    model_domain: str | None = None
    model_lookup_status: EnrichmentLookupStatus = EnrichmentLookupStatus.NOT_STARTED
    model_source_url: str | None = None

    @property
    def has_company_name(self) -> bool:
        return bool(self.normalized_company_name)


@dataclass(frozen=True)
class CandidateEvaluation:
    """One provider candidate and exactly why it was kept or rejected."""

    domain: str
    name: str | None
    rank: int | None
    eligible: bool
    aligned: bool
    alignment: str | None
    rejection_reason: str | None

    def as_json(self) -> dict[str, Any]:
        """The stored shape. Kept flat so a decision row reads without a decoder."""

        return {
            "domain": self.domain,
            "name": self.name,
            "rank": self.rank,
            "eligible": self.eligible,
            "aligned": self.aligned,
            "alignment": self.alignment,
            "rejection_reason": self.rejection_reason,
        }


@dataclass(frozen=True)
class PolicyDecision:
    """The policy's answer, with everything needed to explain and store it."""

    state: DomainResolutionState
    policy_version: str = POLICY_VERSION
    selected_domain: str | None = None
    selected_candidate: dict[str, Any] | None = None
    provider: str | None = None
    provider_rank: int | None = None
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    candidates: tuple[CandidateEvaluation, ...] = field(default=())

    @property
    def is_resolved(self) -> bool:
        return self.state is not DomainResolutionState.UNRESOLVED

    def candidates_json(self) -> list[dict[str, Any]]:
        return [candidate.as_json() for candidate in self.candidates]


# --- The policy ---------------------------------------------------------------


def evaluate_established_evidence(evidence: ResolutionEvidence) -> PolicyDecision | None:
    """Steps 1 and 2: decide from evidence that is already on record.

    Returns ``None`` when neither settles it, which is the caller's signal that
    a provider lookup is the only remaining source — and therefore the *only*
    case in which spending a provider call is justified.
    """

    if not evidence.has_company_name:
        return PolicyDecision(
            state=DomainResolutionState.UNRESOLVED,
            reasons=(REASON_NO_COMPANY_NAME,),
        )

    mappings = {d for d in evidence.approved_mapping_domains if d}
    company_domains = {match.domain for match in evidence.existing_companies if match.domain}

    if len(mappings) > 1:
        return PolicyDecision(
            state=DomainResolutionState.UNRESOLVED,
            reasons=(REASON_CONFLICTING_APPROVED_MAPPINGS,),
            warnings=_identity_warnings(evidence),
        )

    if len(mappings) == 1:
        approved = next(iter(mappings))
        # An approved mapping and an existing Company naming different domains is
        # a disagreement between two things that are each supposed to be settled.
        # Picking either would be choosing a side silently.
        if company_domains and approved not in company_domains:
            return PolicyDecision(
                state=DomainResolutionState.UNRESOLVED,
                reasons=(REASON_MAPPING_CONFLICTS_WITH_COMPANY,),
                warnings=_identity_warnings(evidence),
            )
        return PolicyDecision(
            state=DomainResolutionState.CONFIRMED,
            selected_domain=approved,
            reasons=(REASON_REUSED_APPROVED_MAPPING,),
            warnings=_identity_warnings(evidence),
        )

    if len(company_domains) > 1:
        return PolicyDecision(
            state=DomainResolutionState.UNRESOLVED,
            reasons=(REASON_CONFLICTING_EXISTING_COMPANIES,),
            warnings=_identity_warnings(evidence),
        )

    if len(company_domains) == 1:
        matched_on = {match.matched_on for match in evidence.existing_companies}
        reason = (
            REASON_MATCHED_EXISTING_COMPANY_LINKEDIN
            if "linkedin_company_id" in matched_on
            else REASON_MATCHED_EXISTING_COMPANY_NAME
        )
        return PolicyDecision(
            state=DomainResolutionState.CONFIRMED,
            selected_domain=next(iter(company_domains)),
            reasons=(reason,),
            warnings=_identity_warnings(evidence),
        )

    return None


def _model_fallback(
    evidence: ResolutionEvidence,
    *,
    warnings: list[str],
    evaluated: tuple[CandidateEvaluation, ...],
    provider_reason: str,
) -> PolicyDecision:
    """The answer when the deterministic path produced nothing usable.

    Reached from exactly two places — the provider returned no candidates, or none
    of its candidates aligned with the company name. Both mean "the brand matcher
    has nothing to say about this company", which is the only situation a model
    answer is admitted in. It is deliberately *not* reached when several candidates
    align: sources disagreeing is where the policy refuses to guess, and adding a
    third opinion to a two-way disagreement makes the guess more confident rather
    than more correct.

    **The name-alignment rule is waived here, and only here.** Alignment exists
    because a brand matcher returns a ranked list of *similar names* and rank is
    not evidence, so a candidate has to earn its place by spelling the company's
    name. A model asked about one named company, having read a page, is making a
    different kind of claim — and requiring its answer to spell the name would
    reject precisely the companies the matcher already failed on, which is the
    entire population this fallback exists to serve. ``Alphabet`` → ``abc.xyz`` is
    the shape of the problem.

    What is *not* waived: the domain must still pass the same suitability check as
    any provider candidate, because a model asked for "the official domain" reaches
    for ``linkedin.com`` and ``crunchbase.com`` at least as readily. And the ceiling
    is still PROVISIONAL, so the stages that spend money and send mail stay shut.
    """

    if evidence.model_lookup_status is EnrichmentLookupStatus.NOT_STARTED:
        return PolicyDecision(
            state=DomainResolutionState.UNRESOLVED,
            reasons=(provider_reason, REASON_MODEL_LOOKUP_NOT_RUN),
            warnings=tuple(warnings),
            candidates=evaluated,
            provider=evidence.provider,
        )
    if evidence.model_lookup_status in _RETRYABLE_LOOKUP_STATUSES:
        # "Could not reach it" and "it answered with something unreadable" are both
        # retryable but call for different things from an operator — checking the CLI
        # versus simply asking again — so they are not collapsed into one code.
        return PolicyDecision(
            state=DomainResolutionState.UNRESOLVED,
            reasons=(
                provider_reason,
                REASON_MODEL_ANSWER_UNUSABLE
                if evidence.model_lookup_status is EnrichmentLookupStatus.MALFORMED
                else REASON_MODEL_UNAVAILABLE,
            ),
            warnings=tuple(warnings),
            candidates=evaluated,
            provider=evidence.provider,
        )

    domain = norm.collapse_whitespace(evidence.model_domain)
    if evidence.model_lookup_status is not EnrichmentLookupStatus.OK or not domain:
        return PolicyDecision(
            state=DomainResolutionState.UNRESOLVED,
            reasons=(provider_reason, REASON_MODEL_NO_ANSWER),
            warnings=tuple(warnings),
            candidates=evaluated,
            provider=evidence.provider,
        )

    unsuitable = unsuitable_reason(domain)
    if unsuitable is not None:
        rejected = CandidateEvaluation(
            domain=domain,
            name=None,
            rank=None,
            eligible=False,
            aligned=False,
            alignment=None,
            rejection_reason=unsuitable,
        )
        return PolicyDecision(
            state=DomainResolutionState.UNRESOLVED,
            reasons=(provider_reason, REASON_MODEL_DOMAIN_UNSUITABLE),
            warnings=tuple([*warnings, WARNING_CANDIDATES_REJECTED]),
            candidates=(*evaluated, rejected),
            provider=evidence.provider,
        )

    chosen = CandidateEvaluation(
        domain=domain,
        name=None,
        rank=None,
        eligible=True,
        # False, and honestly so: this candidate was never held to alignment. A
        # reader must not be able to mistake "we waived the rule" for "it passed".
        aligned=False,
        alignment=None,
        rejection_reason=None,
    )
    return PolicyDecision(
        state=DomainResolutionState.PROVISIONAL,
        selected_domain=domain,
        selected_candidate={
            **chosen.as_json(),
            "provider": MODEL_PROVIDER,
            "source_url": evidence.model_source_url,
        },
        provider=MODEL_PROVIDER,
        provider_rank=None,
        reasons=(
            provider_reason,
            REASON_MODEL_ASSERTED_DOMAIN,
            REASON_MODEL_NAME_ALIGNMENT_WAIVED,
            REASON_MODEL_EVIDENCE_UNCORROBORATED,
        ),
        warnings=tuple(
            [*warnings, WARNING_MODEL_ANSWER_NOT_DETERMINISTIC, WARNING_PROVISIONAL_LIMITS]
        ),
        candidates=(*evaluated, chosen),
    )


def evaluate(evidence: ResolutionEvidence) -> PolicyDecision:
    """Decide the truthful resolution state for one captured company."""

    established = evaluate_established_evidence(evidence)
    if established is not None:
        return established

    evaluated = tuple(
        _evaluate_candidate(raw, evidence.normalized_company_name)
        for raw in evidence.candidates
        if _candidate_domain(raw)
    )
    warnings = list(_identity_warnings(evidence))
    if any(
        c.rejection_reason and c.rejection_reason != REJECTED_NAME_NOT_ALIGNED for c in evaluated
    ):
        warnings.append(WARNING_CANDIDATES_REJECTED)

    # Provider conditions first: "we never asked" and "we asked and it broke" are
    # different from "we asked and there was nothing", and only the last one is
    # a statement about this company.
    if evidence.lookup_status is EnrichmentLookupStatus.NOT_STARTED:
        return PolicyDecision(
            state=DomainResolutionState.UNRESOLVED,
            reasons=(REASON_PROVIDER_LOOKUP_NOT_RUN,),
            warnings=tuple(warnings),
            candidates=evaluated,
            provider=evidence.provider,
        )
    if evidence.lookup_status in _RETRYABLE_LOOKUP_STATUSES:
        return PolicyDecision(
            state=DomainResolutionState.UNRESOLVED,
            reasons=(REASON_PROVIDER_UNAVAILABLE,),
            warnings=tuple(warnings),
            candidates=evaluated,
            provider=evidence.provider,
        )
    if not evaluated:
        return _model_fallback(
            evidence,
            warnings=warnings,
            evaluated=evaluated,
            provider_reason=REASON_PROVIDER_NO_CANDIDATES,
        )

    survivors = [c for c in evaluated if c.eligible and c.aligned]
    distinct = {c.domain for c in survivors}

    if not survivors:
        return _model_fallback(
            evidence,
            warnings=warnings,
            evaluated=evaluated,
            provider_reason=REASON_NO_ALIGNED_CANDIDATE,
        )
    if len(distinct) > 1:
        return PolicyDecision(
            state=DomainResolutionState.UNRESOLVED,
            reasons=(REASON_MULTIPLE_ALIGNED_CANDIDATES,),
            warnings=tuple(warnings),
            candidates=evaluated,
            provider=evidence.provider,
        )

    # Exactly one aligned, eligible candidate. Provisional — never confirmed:
    # one provider agreeing with itself is not corroboration.
    chosen = survivors[0]
    warnings.append(WARNING_PROVISIONAL_LIMITS)
    return PolicyDecision(
        state=DomainResolutionState.PROVISIONAL,
        selected_domain=chosen.domain,
        selected_candidate=chosen.as_json(),
        provider=evidence.provider,
        provider_rank=chosen.rank,
        reasons=(
            REASON_SINGLE_ALIGNED_CANDIDATE,
            REASON_PROVIDER_EVIDENCE_UNCORROBORATED,
            REASON_RANK_IS_NOT_CONFIRMATION,
        ),
        warnings=tuple(warnings),
        candidates=evaluated,
    )


def operator_correction(
    *,
    domain: str | None,
    company_name: str | None,
    normalized_company_name: str,
) -> PolicyDecision:
    """The decision an explicit operator correction produces.

    An operator naming a domain by hand is exactly the evidence ``confirmed``
    describes, so a correction confirms. An operator declining to name one is
    ``unresolved`` — recorded as a decision rather than left as an absence.
    """

    if domain is None:
        return PolicyDecision(
            state=DomainResolutionState.UNRESOLVED,
            reasons=(REASON_OPERATOR_MARKED_UNRESOLVED,),
            warnings=(WARNING_CORRECTION_SUPERSEDES,),
        )
    return PolicyDecision(
        state=DomainResolutionState.CONFIRMED,
        selected_domain=domain,
        reasons=(REASON_OPERATOR_CORRECTION,),
        warnings=(WARNING_CORRECTION_SUPERSEDES,),
    )


# --- Candidate evaluation -----------------------------------------------------


def _candidate_domain(raw: dict[str, Any]) -> str | None:
    value = raw.get("domain") if isinstance(raw, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def _evaluate_candidate(raw: dict[str, Any], normalized_company: str) -> CandidateEvaluation:
    """Judge one candidate: valid, suitable, and aligned with the company name."""

    original = _candidate_domain(raw) or ""
    name = raw.get("name") if isinstance(raw.get("name"), str) else None
    rank = raw.get("rank") if isinstance(raw.get("rank"), int) else None

    normalized = norm.normalize_domain(original)
    if normalized is None or not norm.is_valid_hostname(normalized):
        return CandidateEvaluation(
            domain=original,
            name=name,
            rank=rank,
            eligible=False,
            aligned=False,
            alignment=None,
            rejection_reason=REJECTED_INVALID_DOMAIN,
        )

    unsuitable = unsuitable_reason(normalized)
    if unsuitable is not None:
        return CandidateEvaluation(
            domain=normalized,
            name=name,
            rank=rank,
            eligible=False,
            aligned=False,
            alignment=None,
            rejection_reason=unsuitable,
        )

    alignment = _alignment(normalized, name, normalized_company)
    return CandidateEvaluation(
        domain=normalized,
        name=name,
        rank=rank,
        eligible=True,
        aligned=alignment is not None,
        alignment=alignment,
        rejection_reason=None if alignment else REJECTED_NAME_NOT_ALIGNED,
    )


def _alignment(domain: str, provider_name: str | None, normalized_company: str) -> str | None:
    """Which axis matches the company name exactly, if either does."""

    if not normalized_company:
        return None
    by_name = (
        normalize_company_name(provider_name) == normalized_company if provider_name else False
    )
    by_label = registrable_label(domain) == normalized_company
    if by_name and by_label:
        return ALIGNMENT_BOTH
    if by_name:
        return ALIGNMENT_PROVIDER_NAME
    if by_label:
        return ALIGNMENT_DOMAIN_LABEL
    return None


def _identity_warnings(evidence: ResolutionEvidence) -> tuple[str, ...]:
    if evidence.linkedin_company_id:
        return ()
    return (WARNING_NO_LINKEDIN_COMPANY_ID,)


def explain(codes: list[Any] | tuple[str, ...] | None, *, table: dict[str, str]) -> list[str]:
    """Plain-language sentences for stored reason/warning codes.

    An unrecognised code — one written by a policy version this build no longer
    carries — is shown as itself rather than dropped. A decision that cannot be
    fully explained must still be visible.
    """

    if not codes:
        return []
    return [table.get(str(code), str(code)) for code in codes]

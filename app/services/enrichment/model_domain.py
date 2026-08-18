"""Ask a language model for a company's domain, when logo.dev could not.

This is the fallback behind the deterministic lookup, and it exists because
logo.dev's Search Brands is a *brand-name matcher*: it answers well for a company
whose domain spells its name and returns nothing at all for the rest. "Nothing at
all" is the single largest reason a captured person never becomes a Contact, and
no amount of retrying a name matcher will change its answer.

A model with a web search can. What it cannot do is be trusted the way a
deterministic source is, so three rules bound it:

1. **It only runs when the deterministic path had nothing.** Never to break a tie
   between logo.dev candidates — two sources disagreeing is exactly where the
   policy refuses to guess, and a third opinion does not make that safer — and
   never to overrule an approved mapping or an established Company.
2. **Its answer is provisional, never confirmed.** The policy enforces that; this
   module could not grant confirmation if it wanted to. A provisional domain
   opens company research and nothing else, so a wrong answer costs one wasted
   crawl rather than mail sent to a stranger.
3. **The answer is a structured claim, not scraped text.** The seam returns parsed
   JSON, so the model can say ``{"domain": null}`` — a truthful "I could not
   determine it", which is a different outcome from a failed call and is recorded
   differently. A regex over free text cannot draw that distinction: it reads a
   refusal and an error identically.
4. **A domain is a claim, and the claim has to carry its receipts.** The model is
   required to state how sure it is, which pages it actually read, and whether
   other companies could answer to the same name. Those three fields are what the
   policy grades; a bare ``{"domain": "..."}`` is syntactically fine and is
   *rejected*, because "it looks like a hostname" is not evidence that it is this
   employer's hostname. This module parses and normalizes the claim; it does not
   grade it — :mod:`app.services.resolution.policy` does, on stored evidence, so
   the acceptance is replayable long after the call.

Domain hygiene is deliberately *not* re-implemented here. The answer goes through
the same :func:`policy.unsuitable_reason` check as a logo.dev candidate, because
a model asked for "the official domain" reaches for ``linkedin.com`` and
``crunchbase.com`` at least as readily as a brand matcher does. Only the
name-alignment rule is waived, and the policy is where that waiver lives and is
explained — not here.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Any

from app.models.enums import EnrichmentLookupStatus
from app.services.imports import normalization as norm
from app.services.thinking.contracts import (
    Thinker,
    ThinkingError,
    ThinkingMalformed,
    ThinkingRefused,
    ThinkingRequest,
    ThinkingTimeout,
    ThinkingUnavailable,
)

PROVIDER = "claude-cli-domain-finder"
#: Bumped from ``/1`` because the answer the model is asked for changed shape: a
#: bare domain is no longer a complete answer. Stored on the audit event, so a
#: row answered under the old contract stays readable as such rather than looking
#: like a new-contract answer that simply omitted its evidence.
LOOKUP_VERSION = "model-domain-lookup/2"
PURPOSE = "company_domain_lookup"

#: The most evidence items worth keeping, and the longest a single description may
#: be. A claim is stored on a durable row and shown to an operator; an unbounded
#: list of the model's prose would be neither auditable nor a sensible column.
MAX_EVIDENCE_ITEMS = 6
MAX_DETAIL_CHARS = 240
MAX_KIND_CHARS = 64
MAX_REASONING_CHARS = 400

#: Recorded when the answer never addressed whether another company could answer
#: to this name. Deliberately not None: "I checked and there is no other" is an
#: answer the policy accepts, and silence is not that answer.
AMBIGUITY_NOT_STATED = "not stated"

#: The ways a model writes "there is no competing company". Matched only after
#: whitespace collapse and case folding, and only in full — a sentence that
#: merely contains "none" is a description of an ambiguity, not a denial of one.
_NO_AMBIGUITY_WORDS = frozenset({"none", "null", "n/a", "na", "no", "no ambiguity", "-"})

#: Long enough for a couple of web searches, short enough that a stalled call does
#: not hold a backfill pass open. The seam's own default (240s) is sized for
#: dossier work and is far more than one domain question needs.
DEFAULT_TIMEOUT_SECONDS = 90.0

#: A bare hostname: at least two labels, an alphabetic final label. Anything with
#: a scheme, a path, a port or whitespace is stripped first, then this decides.
_DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$")

_STRIP_PREFIX = re.compile(r"^[a-z]+://")
_WWW = re.compile(r"^www\.")


class ModelConfidence(enum.StrEnum):
    """How sure the model says it is, in the only three grades it may use.

    A free-form number would invite a policy that compares 0.71 against 0.7 as
    though the difference meant something. Three named grades cannot be
    over-read, and only the top one is accepted — see
    :func:`app.services.resolution.policy.model_claim_verdict`.

    ``UNKNOWN`` is not a grade the model may return. It is what an answer that
    omitted the field, or returned an unrecognised one, is recorded as — kept
    distinct from ``LOW`` because "it declined to rate itself" and "it rated
    itself poorly" are different facts, even though both are rejected.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


PROMPT = """You identify the official website domain of a company.

Return ONLY a JSON object, with no prose before or after it:

{{"domain": "example.com",
  "official_website_url": "https://example.com/about",
  "confidence": "high",
  "evidence": [
    {{"url": "https://example.com/about", "kind": "official_site",
      "detail": "About page names the company and its stated location"}},
    {{"url": "https://www.linkedin.com/company/example", "kind": "linkedin_company",
      "detail": "LinkedIn company page links to example.com"}}
  ],
  "ambiguity": null,
  "reasoning_summary": "one or two sentences"}}

Rules:
- `domain` is the bare registrable domain of the company's OWN website. No scheme,
  no "www.", no path, no port, no subdomain unless the subdomain genuinely is the
  company's primary site.
- `domain` must NOT be a social network, directory, marketplace, aggregator, code
  host, website builder, mailbox provider or domain-parking page. LinkedIn,
  Crunchbase, Bloomberg, Wikipedia, Facebook, GitHub, Wix and the like are wrong
  answers even when they are the top search result for the company.
- `evidence` lists pages you ACTUALLY OPENED AND READ. At least one of them must be
  a page ON the domain you are naming, and its `detail` must say what on that page
  ties it to THIS company. A search-results page, a page you only saw summarised,
  or a directory profile is not enough on its own. Do not list a page you did not
  open.
- `confidence` is "high" ONLY when you read the company's own site and the identity
  matches on the identifiers given — name AND, where supplied, location, industry
  or LinkedIn company page. Use "medium" when the match is likely but a signal is
  missing or unchecked, and "low" when you are guessing. Rate honestly: "medium"
  is a useful answer here and "high" on a hunch is a harmful one.
- `ambiguity` describes any OTHER company that could answer to this name — a
  same-named company in another country, a parent or subsidiary, an unrelated
  business sharing an acronym. Name them. Use null ONLY when you checked and found
  no competing candidate. If you cannot tell two same-named companies apart, say so
  here; do not pick one.
- `reasoning_summary` is one or two sentences an operator can audit. Do not include
  your working.
- Use the identifiers to tell same-named companies apart. A company in a stated
  country or industry is not the same company as a same-named one elsewhere.
- If you cannot establish the domain for THIS company, return
  {{"domain": null, "reason": "<one short clause>"}}. That is a correct and useful
  answer, and it is the right answer whenever the evidence above does not exist. Do
  NOT guess, and do NOT return a plausible-looking domain you have not verified
  belongs to this company.

Company: {company}
Identifiers: {identifiers}"""


@dataclass(frozen=True)
class ModelDomainResult:
    """What one model lookup established, or why it established nothing.

    ``status`` reuses :class:`EnrichmentLookupStatus` rather than inventing a
    parallel vocabulary, so the capture page and the policy read a model lookup
    and a provider lookup with the same seven words.
    """

    status: EnrichmentLookupStatus
    domain: str | None = None
    source_url: str | None = None
    #: The model's own words for why it declined. Operator-facing, never a
    #: decision input.
    reason: str | None = None
    producer: str | None = None
    producer_version: str | None = None
    #: True when running this again could plausibly answer differently.
    retryable: bool = False
    #: How sure the model says it is. ``UNKNOWN`` when it did not say.
    confidence: ModelConfidence = ModelConfidence.UNKNOWN
    #: The pages it says it read, each normalized to ``url``/``host``/``kind``/
    #: ``detail``. ``host`` is the parsed hostname, so the policy can ask "was any
    #: of this on the domain being claimed?" without re-parsing URLs — the one
    #: place that normalization is done stays this module.
    evidence: tuple[dict[str, Any], ...] = ()
    #: The competing companies it found, in its own words, or None when it says
    #: there were none. Distinguished from "" so "it did not answer the question"
    #: is not read as "it checked and found nothing".
    ambiguity: str | None = None
    #: One or two auditable sentences. Never the model's working.
    reasoning_summary: str | None = None

    @property
    def found(self) -> bool:
        return self.status is EnrichmentLookupStatus.OK and bool(self.domain)

    def claim_payload(self) -> dict[str, Any] | None:
        """The durable record of what the model claimed, or None if it claimed nothing.

        Written to ``salesnav_company_enrichments.model_claim`` and read back by
        the policy, so the acceptance decision is replayed from what was stored
        rather than re-asked. Only produced for an answer that named a domain: a
        refusal and a failure have nothing to grade, and writing an empty claim
        for them would make "not evidenced" indistinguishable from "no answer".
        """

        if self.status is not EnrichmentLookupStatus.OK or not self.domain:
            return None
        return {
            "schema_version": LOOKUP_VERSION,
            "domain": self.domain,
            "official_website_url": self.source_url,
            "confidence": self.confidence.value,
            "evidence": [dict(item) for item in self.evidence],
            # Explicitly null-or-text, never absent: the policy distinguishes
            # "no competing company" from "the question went unanswered".
            "ambiguity": self.ambiguity,
            "reasoning_summary": self.reasoning_summary,
        }


def identifiers_for(
    *, location_hint: str | None, linkedin_company_url: str | None
) -> tuple[str, ...]:
    """The disambiguators worth spending prompt space on.

    Location is the one that matters in practice: a captured company name is
    frequently shared by unrelated companies in different countries, and the
    capture already records where this person works — a fact that has been stored
    on every enrichment record all along and never used for anything.

    The LinkedIn company URL is included when present because it names the exact
    organisation, which is a stronger identifier than any amount of prose. It is
    absent on a Sales Navigator search-results capture, which is precisely the
    case that needs the location most.
    """

    found: list[str] = []
    location = norm.collapse_whitespace(location_hint)
    if location:
        found.append(location)
    url = norm.collapse_whitespace(linkedin_company_url)
    if url:
        found.append(url)
    return tuple(found)


def build_prompt(company_name: str, identifiers: tuple[str, ...]) -> str:
    return PROMPT.format(
        company=company_name.strip(),
        identifiers=", ".join(identifiers) if identifiers else "(none given)",
    )


def normalize_domain(value: object) -> str | None:
    """A bare, lower-cased hostname, or None if *value* is not one.

    Tolerant about the shapes a model returns — a full URL, a ``www.`` prefix,
    trailing punctuation, an email address — and strict about the result: what
    comes back either matches a hostname exactly or is rejected. A half-parsed
    domain is worse than none, because it would be stored as a decision.
    """

    if not isinstance(value, str):
        return None
    text = value.strip().strip(".,;\"'<>()[]").lower()
    if not text:
        return None
    if "@" in text:  # an address was returned where a domain was asked for
        text = text.rsplit("@", 1)[-1]
    text = _STRIP_PREFIX.sub("", text)
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    text = text.split(":", 1)[0]
    text = _WWW.sub("", text).strip(".")
    if not text or not _DOMAIN_RE.match(text):
        return None
    return text


def _trimmed(value: object, limit: int) -> str | None:
    """Collapsed, length-capped text, or None when there is none to keep."""

    if not isinstance(value, str):
        return None
    text = norm.collapse_whitespace(value)
    return text[:limit] if text else None


def _read_confidence(value: object) -> ModelConfidence:
    """The stated grade, or UNKNOWN for anything that is not one of the three."""

    if not isinstance(value, str):
        return ModelConfidence.UNKNOWN
    try:
        grade = ModelConfidence(value.strip().lower())
    except ValueError:
        return ModelConfidence.UNKNOWN
    # "unknown" is ours to record, never the model's to claim.
    return ModelConfidence.UNKNOWN if grade is ModelConfidence.UNKNOWN else grade


def _read_evidence(value: object) -> tuple[dict[str, Any], ...]:
    """The pages the model says it read, normalized and bounded.

    An entry with no URL is dropped — there is nothing to record and nothing to
    check. An entry whose URL yields no host is *kept*, with ``host: null``: it is
    still what the model said it read, and the policy simply does not count it as
    support. Those two are deliberately different: dropping it would hide a model
    that cites prose instead of pages, which is a pattern worth being able to see.

    Items and detail text are capped because this is stored on a row and read by
    a person, not accumulated as model output.
    """

    if not isinstance(value, list):
        return ()
    items: list[dict[str, Any]] = []
    for entry in value:
        if len(items) >= MAX_EVIDENCE_ITEMS:
            break
        if isinstance(entry, str):
            entry = {"url": entry}
        if not isinstance(entry, dict):
            continue
        raw_url = entry.get("url")
        url = norm.collapse_whitespace(raw_url) if isinstance(raw_url, str) else None
        if not url:
            continue
        detail = entry.get("detail")
        kind = entry.get("kind")
        items.append(
            {
                "url": url[:1024],
                # Parsed here so the policy compares strings instead of learning
                # to read URLs. None when the URL is not one we can resolve to a
                # host, which the policy treats as unsupporting.
                "host": normalize_domain(url),
                "kind": _trimmed(kind, MAX_KIND_CHARS),
                "detail": _trimmed(detail, MAX_DETAIL_CHARS),
            }
        )
    return tuple(items)


def _read_ambiguity(payload: dict[str, Any]) -> str | None:
    """What the model said about competing same-named companies.

    ``None`` means it answered "none". A *missing* key is not the same answer,
    and is reported as the sentinel below so the policy can refuse an answer that
    never addressed the question — the single most common way a same-named
    company in another country gets silently selected.
    """

    if "ambiguity" not in payload:
        return AMBIGUITY_NOT_STATED
    value = payload.get("ambiguity")
    if value is None:
        return None
    if not isinstance(value, str):
        return AMBIGUITY_NOT_STATED
    text = norm.collapse_whitespace(value)
    if not text or text.lower() in _NO_AMBIGUITY_WORDS:
        return None
    return text[:MAX_DETAIL_CHARS]


def look_up(
    *,
    company_name: str,
    thinker: Thinker,
    location_hint: str | None = None,
    linkedin_company_url: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> ModelDomainResult:
    """Ask the model for one company's domain. Never raises for a model failure.

    Every failure mode becomes a status, because the caller is a background
    resolution pass over many captures: one company whose lookup times out must
    not end the pass for the rest, and the distinction between "asked and got
    nothing" and "could not ask" has to survive into the record so the operator
    knows whether retrying is worth anything.
    """

    name = norm.collapse_whitespace(company_name)
    if not name:
        return ModelDomainResult(status=EnrichmentLookupStatus.NO_MATCH, reason="no company name")

    request = ThinkingRequest(
        prompt=build_prompt(
            name,
            identifiers_for(location_hint=location_hint, linkedin_company_url=linkedin_company_url),
        ),
        purpose=PURPOSE,
        timeout_seconds=timeout_seconds,
        # The whole point of this fallback: logo.dev already answered from a
        # closed brand index and had nothing. Without search this call would be
        # asking the model to recall a domain, which is how a confident wrong
        # answer gets produced.
        allowed_tools=("WebSearch",),
    )

    try:
        result = thinker.think(request)
    except ThinkingTimeout as exc:
        return ModelDomainResult(
            status=EnrichmentLookupStatus.API_UNAVAILABLE, reason=exc.message, retryable=True
        )
    except ThinkingUnavailable as exc:
        return ModelDomainResult(
            status=EnrichmentLookupStatus.API_UNAVAILABLE, reason=exc.message, retryable=False
        )
    except ThinkingRefused as exc:
        return ModelDomainResult(
            status=EnrichmentLookupStatus.NO_MATCH, reason=exc.message, retryable=False
        )
    except ThinkingMalformed as exc:
        return ModelDomainResult(
            status=EnrichmentLookupStatus.MALFORMED, reason=exc.message, retryable=True
        )
    except ThinkingError as exc:  # a seam error added later, classified honestly
        return ModelDomainResult(
            status=EnrichmentLookupStatus.ERROR, reason=exc.message, retryable=exc.retryable
        )

    return _read(result.payload, producer=result.producer, version=result.producer_version)


def _read(payload: object, *, producer: str, version: str) -> ModelDomainResult:
    """Turn the model's JSON into a result, refusing anything ambiguous.

    A payload the seam handed back is already valid JSON; what is checked here is
    whether it is the *answer that was asked for*. A dict without a ``domain``
    key is malformed, but a dict whose ``domain`` is explicitly null is a
    deliberate "I could not tell" — the one distinction this whole module exists
    to preserve.
    """

    if not isinstance(payload, dict):
        return ModelDomainResult(
            status=EnrichmentLookupStatus.MALFORMED,
            reason="the answer was not a JSON object",
            producer=producer,
            producer_version=version,
            retryable=True,
        )
    if "domain" not in payload:
        return ModelDomainResult(
            status=EnrichmentLookupStatus.MALFORMED,
            reason="the answer had no 'domain' key",
            producer=producer,
            producer_version=version,
            retryable=True,
        )

    raw = payload.get("domain")
    stated_reason = payload.get("reason")
    reason = norm.collapse_whitespace(stated_reason if isinstance(stated_reason, str) else None)

    if raw is None:
        return ModelDomainResult(
            status=EnrichmentLookupStatus.NO_MATCH,
            reason=reason or "the model could not establish a domain",
            producer=producer,
            producer_version=version,
            # Declining is an answer, not a fault. Asking again without new
            # information would spend a call to be told the same thing.
            retryable=False,
        )

    domain = normalize_domain(raw)
    if domain is None:
        return ModelDomainResult(
            status=EnrichmentLookupStatus.MALFORMED,
            reason=f"{str(raw)[:80]!r} is not a domain",
            producer=producer,
            producer_version=version,
            retryable=True,
        )

    # ``official_website_url`` is what the contract asks for; ``source_url`` is
    # what the previous contract asked for and what the column is still called.
    # Both are read, the new name first, so an answer in either shape is
    # understood and neither is silently discarded.
    source = payload.get("official_website_url")
    if not isinstance(source, str) or not source.strip():
        source = payload.get("source_url")
    summary = payload.get("reasoning_summary")
    return ModelDomainResult(
        status=EnrichmentLookupStatus.OK,
        domain=domain,
        source_url=norm.collapse_whitespace(source) if isinstance(source, str) else None,
        reason=reason,
        producer=producer,
        producer_version=version,
        confidence=_read_confidence(payload.get("confidence")),
        evidence=_read_evidence(payload.get("evidence")),
        ambiguity=_read_ambiguity(payload),
        reasoning_summary=_trimmed(summary, MAX_REASONING_CHARS),
    )

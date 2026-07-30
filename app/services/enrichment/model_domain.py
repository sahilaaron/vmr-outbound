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

Domain hygiene is deliberately *not* re-implemented here. The answer goes through
the same :func:`policy.unsuitable_reason` check as a logo.dev candidate, because
a model asked for "the official domain" reaches for ``linkedin.com`` and
``crunchbase.com`` at least as readily as a brand matcher does. Only the
name-alignment rule is waived, and the policy is where that waiver lives and is
explained — not here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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
LOOKUP_VERSION = "model-domain-lookup/1"
PURPOSE = "company_domain_lookup"

#: Long enough for a couple of web searches, short enough that a stalled call does
#: not hold a backfill pass open. The seam's own default (240s) is sized for
#: dossier work and is far more than one domain question needs.
DEFAULT_TIMEOUT_SECONDS = 90.0

#: A bare hostname: at least two labels, an alphabetic final label. Anything with
#: a scheme, a path, a port or whitespace is stripped first, then this decides.
_DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$")

_STRIP_PREFIX = re.compile(r"^[a-z]+://")
_WWW = re.compile(r"^www\.")

PROMPT = """You identify the official website domain of a company.

Return ONLY a JSON object, with no prose before or after it:

{{"domain": "example.com", "source_url": "https://example.com/about"}}

Rules:
- `domain` is the bare registrable domain of the company's OWN website. No scheme,
  no "www.", no path, no port, no subdomain unless the subdomain genuinely is the
  company's primary site.
- `domain` must NOT be a social network, directory, marketplace, aggregator, code
  host, website builder, mailbox provider or domain-parking page. LinkedIn,
  Crunchbase, Bloomberg, Wikipedia, Facebook, GitHub, Wix and the like are wrong
  answers even when they are the top search result for the company.
- `source_url` is a page you actually read that shows the domain belongs to THIS
  company. Omit it or use null if you did not read one.
- Use the identifiers to tell same-named companies apart. A company in a stated
  country or industry is not the same company as a same-named one elsewhere.
- If you cannot establish the domain for THIS company with reasonable confidence,
  return {{"domain": null, "reason": "<one short clause>"}}. That is a correct and
  useful answer. Do NOT guess, and do NOT return a plausible-looking domain you
  have not verified belongs to this company.

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

    @property
    def found(self) -> bool:
        return self.status is EnrichmentLookupStatus.OK and bool(self.domain)


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

    source = payload.get("source_url")
    return ModelDomainResult(
        status=EnrichmentLookupStatus.OK,
        domain=domain,
        source_url=norm.collapse_whitespace(source) if isinstance(source, str) else None,
        reason=reason,
        producer=producer,
        producer_version=version,
    )

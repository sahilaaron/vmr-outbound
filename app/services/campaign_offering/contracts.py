"""The structured answer a Campaign offering read must produce, and its validator.

A prose blob would have been easier and is the wrong thing to store. Three
reasons, and the first is the one that decides it:

1. **A pitch has to be assembled, not quoted.** The Personalization Agent builds
   its prompt from named parts — what the offering is, who it is for, what it
   fixes, what it is allowed to claim. A paragraph forces every downstream caller
   to re-read it and guess which sentence was which, and two callers will guess
   differently.
2. **Garbage has to be refusable.** With named fields, "the model answered but
   said nothing about the offering" is a validation failure with a code. With a
   blob, it is a shorter blob, and the Campaign silently leads with nothing.
3. **A version has to be comparable.** ``digest`` over the normalized structure
   is what lets a re-analysis say "this is the same answer" without storing the
   model's wording twice.

**Validation refuses; it never repairs.** Fields are trimmed and bounded, lists
are capped, and non-strings inside a list are dropped — those are bounds, not
inventions. But a missing offering name, an empty summary, an absent connection
to the seller, or a model that reports it could not read the page all raise
:class:`OfferingResearchMalformed`. Nothing here fabricates a value to make a run
succeed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

#: Bumped when the *meaning* of the structure changes, not when a bound moves.
#: Stored on every accepted row so an old payload is identifiable rather than
#: inferred from which keys happen to be present.
CONTEXT_POLICY_VERSION = "1"

MAX_TEXT = 4_000
MAX_SHORT_TEXT = 500
MAX_ITEMS = 12


class OfferingResearchMalformed(ValueError):
    """The model answered, but not with a usable offering.

    ``code`` is what the queue records and what Admin diagnostics show. The
    customer never sees it — they see the product sentence about falling back to
    the Library offering.
    """

    def __init__(self, message: str, *, code: str = "offering_malformed") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class OfferingIntelligence:
    """One Campaign's researched offering, in the parts a pitch is built from."""

    #: What is being offered. The one field with no default: an answer that
    #: cannot name the offering has not read the page.
    offering_name: str
    summary: str
    #: How this offering stands with what the seller already is and sells. This
    #: is the "connect it to your company" half of the product promise, and it is
    #: required for the same reason the name is: without it the researched page
    #: is a stranger's marketing copy rather than something we can credibly say.
    seller_connection: str
    offering_type: str | None = None
    target_audience: tuple[str, ...] = ()
    customer_problems: tuple[str, ...] = ()
    use_cases: tuple[str, ...] = ()
    key_capabilities: tuple[str, ...] = ()
    benefits: tuple[str, ...] = ()
    #: Market, industry or report context, when the page states any. Frequently
    #: empty and legitimately so.
    market_context: tuple[str, ...] = ()
    buyer_relevance: tuple[str, ...] = ()
    #: What the page actually said, in its own words, for the parts above. Kept
    #: short and kept separate: it is evidence for an audit, not copy to send.
    source_evidence: tuple[str, ...] = ()
    #: What the page did not establish. An honest gap, recorded rather than
    #: filled in.
    unknowns: tuple[str, ...] = ()
    #: The address the model reports it actually read. May differ from the one it
    #: was given when the page redirected; stored so that difference is visible.
    source_url_read: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_payload(self) -> dict[str, Any]:
        """The JSONB shape stored on the run. Deterministic key order."""

        return {key: _jsonable(value) for key, value in sorted(asdict(self).items())}

    def digest(self) -> str:
        encoded = json.dumps(self.to_payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @property
    def is_thin(self) -> bool:
        """True when the answer names an offering but establishes little else.

        Not a refusal — a thin page is a real thing and the operator should see
        what was found. It drives a warning on the summary, nothing more.
        """

        return not (self.target_audience or self.customer_problems or self.key_capabilities)


def _jsonable(value: Any) -> Any:
    if isinstance(value, tuple):
        return list(value)
    return value


def _text(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:limit] if cleaned else None


def _items(value: Any, *, limit: int = MAX_ITEMS) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    out: list[str] = []
    for item in value:
        text = _text(item, limit=MAX_SHORT_TEXT)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return tuple(out)


def parse_offering_payload(payload: Any) -> OfferingIntelligence:
    """Turn one model answer into a validated :class:`OfferingIntelligence`.

    Raises :class:`OfferingResearchMalformed` for every shape that is not a
    usable offering, including the two the model is explicitly asked to report
    rather than invent: a page it could not reach, and a page with no offering on
    it.
    """

    if not isinstance(payload, dict):
        raise OfferingResearchMalformed(
            "The model did not return an offering object.", code="offering_not_an_object"
        )

    # The model is told to answer with `readable: false` rather than guess. Honour
    # that as a distinct outcome: "the page could not be read" and "the page had
    # no offering on it" are different sentences for the operator.
    if payload.get("readable") is False:
        reason = _text(payload.get("unreadable_reason"), limit=MAX_SHORT_TEXT)
        raise OfferingResearchMalformed(
            reason or "The page could not be read.", code="page_unreadable"
        )

    name = _text(payload.get("offering_name"), limit=MAX_SHORT_TEXT)
    if not name:
        raise OfferingResearchMalformed(
            "The answer did not name an offering.", code="offering_name_missing"
        )
    summary = _text(payload.get("summary"), limit=MAX_TEXT)
    if not summary:
        raise OfferingResearchMalformed(
            "The answer did not describe the offering.", code="offering_summary_missing"
        )
    connection = _text(payload.get("seller_connection"), limit=MAX_TEXT)
    if not connection:
        raise OfferingResearchMalformed(
            "The answer did not connect the offering to the seller.",
            code="seller_connection_missing",
        )

    return OfferingIntelligence(
        offering_name=name,
        summary=summary,
        seller_connection=connection,
        offering_type=_text(payload.get("offering_type"), limit=120),
        target_audience=_items(payload.get("target_audience")),
        customer_problems=_items(payload.get("customer_problems")),
        use_cases=_items(payload.get("use_cases")),
        key_capabilities=_items(payload.get("key_capabilities")),
        benefits=_items(payload.get("benefits")),
        market_context=_items(payload.get("market_context")),
        buyer_relevance=_items(payload.get("buyer_relevance")),
        source_evidence=_items(payload.get("source_evidence")),
        unknowns=_items(payload.get("unknowns")),
        source_url_read=_text(payload.get("source_url_read"), limit=2048),
    )


def offering_from_stored(payload: Any) -> OfferingIntelligence | None:
    """Rebuild an :class:`OfferingIntelligence` from a stored JSONB payload.

    Returns ``None`` rather than raising for a row that cannot be read back: a
    stored version that no longer parses must not break the Campaign that
    references it, and the resolver treats it as "no researched offering", which
    falls back to the Library exactly as a failure does.
    """

    if not isinstance(payload, dict):
        return None
    try:
        return parse_offering_payload(payload)
    except OfferingResearchMalformed:
        return None

"""Deterministic specialty hygiene (CI-002).

A specialty is *a concrete area of domain expertise, technical focus, service
practice or delivery competence that is narrower than the broad industry and
more specific than the general company type.*

"Antibody-drug conjugate development" is one. "Innovative customer-centric
solutions" is not, and the difference is not a matter of taste: the first tells
you what the company does when nobody is watching, and the second would be true
of any company that could afford a copywriter.

Everything in this module is deterministic, versioned and testable. The model
proposes; this decides. Four outcomes, in descending order of confidence:

**Accept** — specific, factual, non-promotional, evidence-backed. Stored resolved.

**Clean** — a promotional modifier can be *removed* and what remains is the
exact same factual specialty. "Leading cold-chain logistics provider" →
"cold-chain logistics". Cleaning only ever strips tokens from a curated list at
the edges of the phrase; it never reorders, substitutes or rephrases, because a
rewrite that changes meaning is worse than no cleaning at all.

**Unresolved** — plausible but not settled: too broad, promotional in a way that
cannot be safely stripped, or wording that belongs to another dimension. Kept and
shown. This is the outcome the whole design bends toward, because a suggestion an
operator can see is worth more than either a false fact or a silent deletion.

**Reject** — malformed, empty, pure marketing, or an outcome claim. Only these.

The rules are phrase-aware, not token-aware, and that distinction earns its
keep: "next-generation sequencing" is a real laboratory technique while
"next-generation solutions" is a brochure, and the two differ by one word.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass

from app.services.company_intelligence.normalization import normalize_term

#: Bumped whenever a rule below changes. Carried into the producer's policy
#: version so a rules change produces a new intelligence version rather than
#: silently reinterpreting an old one.
SPECIALTY_HYGIENE_VERSION = "1"

MIN_SPECIALTY_TOKENS = 2
MAX_SPECIALTY_TOKENS = 12
MAX_SPECIALTY_CHARS = 160

# Unresolved reason codes, shared with the producer and the Admin surface.
REASON_PROMOTIONAL = "promotional_language"
REASON_TOO_BROAD = "too_broad"
REASON_DIMENSION_OVERLAP = "dimension_boundary_unclear"
REASON_TOO_LONG = "specialty_too_long"

# Reject reason codes.
REJECT_EMPTY = "empty_value"
REJECT_MARKETING_ONLY = "marketing_language_only"
REJECT_OUTCOME_CLAIM = "outcome_claim_not_specialty"


class SpecialtyAction(enum.StrEnum):
    ACCEPT = "accept"
    CLEAN = "clean"
    UNRESOLVED = "unresolved"
    REJECT = "reject"


#: Words that describe how good something is rather than what it is. Presence of
#: one is a signal, never a verdict — see ``PROTECTED_PHRASES``.
PROMOTIONAL_TOKENS = frozenset(
    {
        "advanced",
        "award",
        "awarded",
        "award-winning",
        "best",
        "best-in-class",
        "bespoke",
        "comprehensive",
        "customer-centric",
        "customer-focused",
        "cutting-edge",
        "differentiated",
        "disruptive",
        "elite",
        "end-to-end",
        "exceptional",
        "expert",
        "first-class",
        "global",
        "groundbreaking",
        "holistic",
        "industry-leading",
        "innovative",
        "leading",
        "market-leading",
        "next-generation",
        "next-gen",
        "outstanding",
        "pioneering",
        "premier",
        "premium",
        "proven",
        "revolutionary",
        "seamless",
        "state-of-the-art",
        "sustainable",
        "superior",
        "transformative",
        "trusted",
        "unmatched",
        "unparalleled",
        "unrivalled",
        "unrivaled",
        "world-class",
        "world-leading",
    }
)

#: The promotional vocabulary as normalized *token sequences*, longest first.
#:
#: Sequences rather than a token set, because most of these are hyphenated
#: compounds — "world-class" normalizes to two tokens, and a set of single
#: tokens silently fails to match every one of them. Longest-first so
#: "next generation" is stripped before a shorter prefix of it could be.
_PROMOTIONAL_PHRASES: tuple[tuple[str, ...], ...] = tuple(
    sorted(
        (tuple(normalize_term(token).split()) for token in PROMOTIONAL_TOKENS),
        key=lambda phrase: (-len(phrase), phrase),
    )
)

#: Phrases in which a "promotional" token is load-bearing technical vocabulary.
#: Curated, small, and extended only by a human — the whole point is that this
#: list is short enough to read.
PROTECTED_PHRASES = frozenset(
    normalize_term(phrase)
    for phrase in (
        "next-generation sequencing",
        "next generation sequencing",
        "next-gen sequencing",
        "advanced driver assistance systems",
        "advanced driver assistance",
        "advanced materials",
        "advanced packaging",
        "advanced therapy medicinal products",
        "global navigation satellite systems",
        "global positioning systems",
        "expert systems",
        "expert determination",
        "premium bond",
        "state of the art review",
        "sustainable aviation fuel",
        "sustainable packaging",
        "sustainable materials",
        "green hydrogen",
    )
)

#: Words that name a whole field rather than a concentration inside one. Fine
#: inside a longer phrase ("industrial wastewater treatment"), never on their
#: own.
BROAD_TERMS = frozenset(
    normalize_term(term)
    for term in (
        "technology",
        "technologies",
        "software",
        "hardware",
        "manufacturing",
        "consulting",
        "consultancy",
        "research",
        "services",
        "service",
        "solutions",
        "solution",
        "innovation",
        "engineering",
        "marketing",
        "logistics",
        "healthcare",
        "finance",
        "financial services",
        "data",
        "analytics",
        "automation",
        "design",
        "training",
        "security",
        "support",
        "products",
        "systems",
        "platform",
        "platforms",
        "digital transformation",
        "sustainability",
        "quality",
        "operations",
        "management",
    )
)

#: Verbs that introduce a claimed benefit. A phrase that starts with one is
#: describing an outcome, which is a thing the customer gets, not a thing the
#: company does.
_OUTCOME_VERBS = frozenset(
    {
        "accelerating",
        "boosting",
        "delivering",
        "driving",
        "empowering",
        "enabling",
        "enhancing",
        "ensuring",
        "expanding",
        "growing",
        "helping",
        "improving",
        "increasing",
        "maximizing",
        "maximising",
        "minimizing",
        "minimising",
        "optimizing",
        "optimising",
        "reducing",
        "streamlining",
        "strengthening",
        "transforming",
        "unlocking",
    }
)

#: Trailing role nouns that describe *being* a supplier rather than the work.
#: Strippable, because "cold-chain logistics provider" and "cold-chain logistics"
#: name the same competence.
_TRAILING_ROLE_NOUNS = (
    "solutions provider",
    "service provider",
    "solutions",
    "provider",
    "providers",
    "specialist",
    "specialists",
    "company",
    "companies",
    "business",
    "vendor",
    "supplier",
    "partner",
    "leader",
    "expert",
    "experts",
    "firm",
)

_TOKEN_SPLIT = re.compile(r"\s+")


@dataclass(frozen=True)
class SpecialtyVerdict:
    """What deterministic hygiene decided about one proposed specialty."""

    action: SpecialtyAction
    #: The value to store as the normalized form, when cleaning applied.
    cleaned_value: str | None = None
    #: Reason code, for the unresolved state or for the rejection.
    reason: str | None = None
    #: One line an operator can read on the review screen.
    detail: str | None = None

    @property
    def keeps_value(self) -> bool:
        return self.action is not SpecialtyAction.REJECT


def _tokens(value: str) -> list[str]:
    normalized = normalize_term(value)
    return [token for token in _TOKEN_SPLIT.split(normalized) if token]


def _is_protected(value: str) -> bool:
    normalized = normalize_term(value)
    return any(phrase and phrase in normalized for phrase in PROTECTED_PHRASES)


def _promotional_at(tokens: list[str], index: int) -> tuple[str, ...] | None:
    for phrase in _PROMOTIONAL_PHRASES:
        end = index + len(phrase)
        if end <= len(tokens) and tuple(tokens[index:end]) == phrase:
            return phrase
    return None


def promotional_phrases_in(tokens: list[str]) -> list[str]:
    """Every promotional phrase present, wherever it sits."""

    found: list[str] = []
    index = 0
    while index < len(tokens):
        phrase = _promotional_at(tokens, index)
        if phrase is None:
            index += 1
            continue
        found.append(" ".join(phrase))
        index += len(phrase)
    return found


def _strip_promotional_prefix(tokens: list[str]) -> list[str]:
    """Remove promotional phrases from the front only.

    The front only, deliberately. A promotional word in the *middle* of a phrase
    is doing grammatical work — "cold chain innovative packaging" is not a phrase
    anybody wrote, and if they did, removing the middle word would be inventing
    a specialty rather than reading one.
    """

    start = 0
    while start < len(tokens):
        phrase = _promotional_at(tokens, start)
        if phrase is None:
            break
        start += len(phrase)
    return tokens[start:]


def _strip_role_noun(tokens: list[str], *, allow_empty: bool = False) -> list[str]:
    """Remove trailing role nouns, repeatedly.

    ``allow_empty`` decides what happens when the phrase is nothing but role
    nouns. When cleaning a value that keeps its meaning, an empty remainder means
    the strip went too far and the original stands. When *judging* a value, an
    empty remainder is the finding: "trusted partner" says nothing at all.
    """

    current = list(tokens)
    while current:
        joined = " ".join(current)
        for role in _TRAILING_ROLE_NOUNS:
            normalized = normalize_term(role)
            if joined == normalized:
                if allow_empty:
                    return []
                return current
            if joined.endswith(" " + normalized):
                remainder = joined[: -(len(normalized) + 1)].strip()
                current = [token for token in _TOKEN_SPLIT.split(remainder) if token]
                break
        else:
            break
    return current


def is_outcome_language(value: str) -> bool:
    """True when the phrase names a benefit rather than a competence."""

    tokens = _tokens(value)
    return bool(tokens) and tokens[0] in _OUTCOME_VERBS


def is_broad(value: str) -> bool:
    """True when the phrase is a whole field rather than a concentration."""

    normalized = normalize_term(value)
    if normalized in BROAD_TERMS:
        return True
    tokens = _tokens(value)
    # Two broad words stuck together are still broad: "technology solutions"
    # narrows nothing.
    return len(tokens) <= 2 and all(token in BROAD_TERMS for token in tokens)


def evaluate(value: str) -> SpecialtyVerdict:
    """Decide what to do with one proposed specialty. Pure and deterministic."""

    raw = " ".join(value.split()).strip() if value else ""
    if not raw:
        return SpecialtyVerdict(
            action=SpecialtyAction.REJECT,
            reason=REJECT_EMPTY,
            detail="the proposed specialty was empty",
        )
    if len(raw) > MAX_SPECIALTY_CHARS:
        return SpecialtyVerdict(
            action=SpecialtyAction.UNRESOLVED,
            reason=REASON_TOO_LONG,
            detail=(
                f"a specialty longer than {MAX_SPECIALTY_CHARS} characters is a sentence, "
                "not a concentration; kept for review rather than stored as settled"
            ),
        )

    if is_outcome_language(raw):
        return SpecialtyVerdict(
            action=SpecialtyAction.REJECT,
            reason=REJECT_OUTCOME_CLAIM,
            detail=(f"{raw!r} names a benefit a customer receives, not work the company performs"),
        )

    tokens = _tokens(raw)
    if not tokens:
        return SpecialtyVerdict(
            action=SpecialtyAction.REJECT,
            reason=REJECT_EMPTY,
            detail="the proposed specialty contained no usable characters",
        )
    if len(tokens) > MAX_SPECIALTY_TOKENS:
        return SpecialtyVerdict(
            action=SpecialtyAction.UNRESOLVED,
            reason=REASON_TOO_LONG,
            detail=f"{len(tokens)} words is a description, not a specialty",
        )

    protected = _is_protected(raw)
    promotional = promotional_phrases_in(tokens)

    if promotional and not protected:
        stripped = _strip_role_noun(_strip_promotional_prefix(tokens), allow_empty=True)
        if not stripped:
            # Nothing factual survives: the value was a compliment.
            return SpecialtyVerdict(
                action=SpecialtyAction.REJECT,
                reason=REJECT_MARKETING_ONLY,
                detail=f"{raw!r} is promotional wording with no factual specialty inside it",
            )
        if promotional_phrases_in(stripped):
            # The promotion is embedded where removing it would rewrite the
            # phrase rather than clean it.
            return SpecialtyVerdict(
                action=SpecialtyAction.UNRESOLVED,
                reason=REASON_PROMOTIONAL,
                detail=(
                    f"{raw!r} carries promotional wording ({', '.join(promotional)}) that "
                    "cannot be removed without changing what it says"
                ),
            )
        cleaned = " ".join(stripped)
        if len(stripped) < MIN_SPECIALTY_TOKENS or is_broad(cleaned):
            return SpecialtyVerdict(
                action=SpecialtyAction.UNRESOLVED,
                reason=REASON_TOO_BROAD,
                detail=(
                    f"removing the promotional wording from {raw!r} leaves {cleaned!r}, "
                    "which is a field rather than a specialty"
                ),
            )
        return SpecialtyVerdict(
            action=SpecialtyAction.CLEAN,
            cleaned_value=cleaned,
            detail=f"removed promotional wording ({', '.join(promotional)}) from {raw!r}",
        )

    if _strip_role_noun(tokens, allow_empty=True) == []:
        return SpecialtyVerdict(
            action=SpecialtyAction.UNRESOLVED,
            reason=REASON_TOO_BROAD,
            detail=f"{raw!r} names being a supplier rather than any particular work",
        )

    if is_broad(raw):
        return SpecialtyVerdict(
            action=SpecialtyAction.UNRESOLVED,
            reason=REASON_TOO_BROAD,
            detail=f"{raw!r} names a whole field rather than a concentration within one",
        )
    if len(tokens) < MIN_SPECIALTY_TOKENS and not protected:
        return SpecialtyVerdict(
            action=SpecialtyAction.UNRESOLVED,
            reason=REASON_TOO_BROAD,
            detail=f"{raw!r} is a single word, which is rarely specific enough to act on",
        )

    stripped = _strip_role_noun(tokens)
    if stripped != tokens:
        cleaned = " ".join(stripped)
        if len(stripped) < MIN_SPECIALTY_TOKENS or is_broad(cleaned):
            # "Digital transformation leader" is a field plus a boast. Removing
            # the boast does not make the field a specialty, so the value stays
            # visible and unsettled rather than being cleaned into something that
            # looks specific and is not.
            return SpecialtyVerdict(
                action=SpecialtyAction.UNRESOLVED,
                reason=REASON_TOO_BROAD,
                detail=(
                    f"{raw!r} reduces to {cleaned!r}, which names a whole field rather than "
                    "a concentration within one"
                ),
            )
        return SpecialtyVerdict(
            action=SpecialtyAction.CLEAN,
            cleaned_value=cleaned,
            detail=f"removed a trailing role noun from {raw!r}",
        )

    return SpecialtyVerdict(action=SpecialtyAction.ACCEPT)


def duplicate_key(value: str, cleaned: str | None = None) -> str:
    """The identity used for duplicate detection.

    Singular/plural is folded here and nowhere else: "battery pack assemblies"
    and "battery pack assembly" are the same competence written twice, and
    storing both would double-count it in every later summary. The fold is
    deliberately naive — a trailing ``s``/``es`` on the last token only — because
    a real stemmer would also fold words that are genuinely different.
    """

    tokens = _tokens(cleaned or value)
    if not tokens:
        return ""
    last = tokens[-1]
    if len(last) > 3 and last.endswith("ies"):
        last = last[:-3] + "y"
    elif len(last) > 3 and last.endswith("es") and not last.endswith("ses"):
        last = last[:-2]
    elif len(last) > 3 and last.endswith("s") and not last.endswith("ss"):
        last = last[:-1]
    return " ".join([*tokens[:-1], last])

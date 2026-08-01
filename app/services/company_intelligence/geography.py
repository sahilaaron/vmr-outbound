"""Deterministic geography candidate extraction (CI-002).

Geography went out in CI-001 as free text: whatever the model wrote, recorded
verbatim and flagged unmapped. That was honest but not useful — "EMEA", "our
London office" and "presented in Berlin" all landed in the same shapeless
column, and none of them could be filtered, counted or trusted.

This module is the deterministic half of the fix. It finds **candidate places**
in evidence the system already holds, canonicalises them against a versioned
reference edition, and hands the model a short list of handles to classify. The
division of labour is the whole design:

* **Deterministic code decides what places exist.** A place the evidence never
  named cannot become a candidate, so the model cannot invent a location.
* **The model decides what the relationship is.** No regex can tell
  "headquartered in Pune" from "presented at a conference in Pune", and pretending
  otherwise is how a conference schedule becomes a factory.

Three suppression tiers, because "this text contains the word Reading" and "this
company has an office in Reading" are very different claims:

1. **Never a candidate.** An ambiguous surface — an ordinary word, a common given
   name, a product word — with no qualifying signal beside it. "Reading the
   specification" is not Reading, Berkshire. Recorded as a warning, never silent.
2. **Never a candidate.** A hard-suppressing context: a publisher line, a journal,
   a conference, a university, a legal jurisdiction clause. These name a place
   that belongs to somebody else.
3. **A candidate, flagged.** A soft context — a customer example, an acquisition,
   a former site, a plan. The place is real and the company is genuinely near it;
   what is uncertain is the *relationship*, which is the model's question, and
   deterministic code then checks the answer against the flag.

**What is never scanned:** source URLs and source titles. A publisher's address
lives there, and a paper published in Heidelberg says nothing about the company.
Only claim text and evidence excerpts are read, which removes that whole class of
false positive structurally rather than by pattern.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.models.enums import IntelligenceGeoRelationship, IntelligencePresenceKind
from app.services.company_intelligence.normalization import normalize_term

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.company_intelligence.inputs import IntelligenceInput

DATA_FILE = Path(__file__).parent / "data" / "geography_base.json"

#: How many candidates are offered to the model. A bound is required: the prompt
#: has a finite budget, and a company that mentions ninety places needs review
#: rather than ninety classifications. Overflow is reported, never silent.
MAX_CANDIDATES = 25

#: How far, in tokens, a qualifying or suppressing signal may sit from a match
#: and still count. Small on purpose — a marker three sentences away is not
#: context, it is coincidence.
SIGNAL_WINDOW = 6

#: Longest surface, in tokens, the matcher will consider.
MAX_SURFACE_TOKENS = 6

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_SENTENCE_BREAK = re.compile(r"[.;:!?\n\r]")

#: Words that, near an ambiguous surface, make it a place rather than a word.
#: Deliberately about *location*, not about the company: "in", "based in" and
#: "site" are what turn "Reading" into a town.
_LOCATION_INDICATORS = frozenset(
    {
        "in",
        "at",
        "near",
        "based",
        "headquartered",
        "headquarters",
        "hq",
        "located",
        "location",
        "office",
        "offices",
        "branch",
        "facility",
        "facilities",
        "plant",
        "site",
        "campus",
        "laboratory",
        "lab",
        "warehouse",
        "centre",
        "center",
        "operations",
        "presence",
        "region",
        "from",
        "serving",
        "serves",
    }
)

#: Contexts in which a place name belongs to somebody else entirely. A match in
#: one of these is not a candidate at all.
_HARD_SUPPRESSORS: dict[str, str] = {
    "conference": "conference_or_event",
    "conferences": "conference_or_event",
    "summit": "conference_or_event",
    "expo": "conference_or_event",
    "exposition": "conference_or_event",
    "symposium": "conference_or_event",
    "tradeshow": "conference_or_event",
    "congress": "conference_or_event",
    "keynote": "conference_or_event",
    "exhibited": "conference_or_event",
    "exhibiting": "conference_or_event",
    "publisher": "publisher_or_source",
    "published": "publisher_or_source",
    "journal": "publisher_or_source",
    "proceedings": "publisher_or_source",
    "press": "publisher_or_source",
    "isbn": "publisher_or_source",
    "university": "biography_or_academia",
    "universities": "biography_or_academia",
    "graduated": "biography_or_academia",
    "phd": "biography_or_academia",
    "alumnus": "biography_or_academia",
    "alumna": "biography_or_academia",
    "born": "biography_or_academia",
    "jurisdiction": "legal_jurisdiction",
    "governed": "legal_jurisdiction",
    "laws": "legal_jurisdiction",
    "incorporated": "legal_jurisdiction",
    "registrar": "legal_jurisdiction",
    "arbitration": "legal_jurisdiction",
}

#: Contexts that make the *relationship* uncertain without making the place
#: irrelevant. The candidate survives, carrying the flag, and the model's answer
#: is checked against it afterwards.
_SOFT_FLAGS: dict[str, str] = {
    "customer": "customer_example",
    "customers": "customer_example",
    "client": "customer_example",
    "clients": "customer_example",
    "case": "customer_example",
    "acquired": "historical_or_acquired",
    "acquisition": "historical_or_acquired",
    "formerly": "historical_or_acquired",
    "previously": "historical_or_acquired",
    "divested": "historical_or_acquired",
    "closed": "historical_or_acquired",
    "planned": "planned",
    "plans": "planned",
    "will": "planned",
    "upcoming": "planned",
    "announced": "planned",
}

#: Which relationships a soft flag can honestly coexist with. A candidate found
#: only in a customer example that the model then calls a *headquarters* is not
#: a headquarters — it is a disagreement, and it is stored as one.
_FLAG_COMPATIBLE: dict[str, frozenset[IntelligenceGeoRelationship]] = {
    "customer_example": frozenset(
        {
            IntelligenceGeoRelationship.COMMERCIAL_MARKET,
            IntelligenceGeoRelationship.UNCLEAR,
        }
    ),
    "historical_or_acquired": frozenset(
        {
            IntelligenceGeoRelationship.HISTORICAL_PRESENCE,
            IntelligenceGeoRelationship.UNCLEAR,
        }
    ),
    "planned": frozenset(
        {
            IntelligenceGeoRelationship.PLANNED_PRESENCE,
            IntelligenceGeoRelationship.UNCLEAR,
        }
    ),
}

#: Relationship → what kind of presence it actually asserts. Derived
#: deterministically and stored, so a consumer filtering for "places this
#: company physically is" never has to re-implement the mapping.
PRESENCE_FOR_RELATIONSHIP: dict[IntelligenceGeoRelationship, IntelligencePresenceKind] = {
    IntelligenceGeoRelationship.HEADQUARTERS: IntelligencePresenceKind.PHYSICAL,
    IntelligenceGeoRelationship.OFFICE: IntelligencePresenceKind.PHYSICAL,
    IntelligenceGeoRelationship.BRANCH: IntelligencePresenceKind.PHYSICAL,
    IntelligenceGeoRelationship.FACILITY: IntelligencePresenceKind.PHYSICAL,
    IntelligenceGeoRelationship.MANUFACTURING: IntelligencePresenceKind.PHYSICAL,
    IntelligenceGeoRelationship.RESEARCH_AND_DEVELOPMENT: IntelligencePresenceKind.PHYSICAL,
    IntelligenceGeoRelationship.WAREHOUSE: IntelligencePresenceKind.PHYSICAL,
    IntelligenceGeoRelationship.DISTRIBUTION: IntelligencePresenceKind.PHYSICAL,
    IntelligenceGeoRelationship.OPERATIONS: IntelligencePresenceKind.PHYSICAL,
    IntelligenceGeoRelationship.COMMERCIAL_MARKET: IntelligencePresenceKind.COMMERCIAL,
    IntelligenceGeoRelationship.PLANNED_PRESENCE: IntelligencePresenceKind.PROSPECTIVE,
    IntelligenceGeoRelationship.HISTORICAL_PRESENCE: IntelligencePresenceKind.FORMER,
    IntelligenceGeoRelationship.UNCLEAR: IntelligencePresenceKind.UNKNOWN,
}

#: Relationships that assert something true *now*. Everything else is kept and
#: shown, but never counted as a settled current geography.
CURRENT_PRESENCE_KINDS = frozenset(
    {IntelligencePresenceKind.PHYSICAL, IntelligencePresenceKind.COMMERCIAL}
)

# Unresolved reason codes, shared with the producer and the Admin surface.
REASON_UNCLEAR_RELATIONSHIP = "unclear_relationship"
REASON_AMBIGUOUS_LOCATION = "ambiguous_location"
REASON_CONTEXT_MISMATCH = "context_conflicts_with_relationship"
REASON_NOT_CURRENT = "not_current_presence"


class GeographyDataError(RuntimeError):
    """The vendored geography edition is malformed.

    Raised loudly at load time rather than tolerated. A geography base with a
    duplicate code or a city that resolves to no country would produce
    order-dependent classifications, which is the exact failure this whole
    module exists to prevent.
    """


@dataclass(frozen=True)
class Place:
    """One canonical country or city in the loaded edition."""

    kind: str  # "country" | "city"
    code: str
    label: str
    country_code: str
    country_name: str
    country_alpha3: str
    region: str
    #: Surfaces that need a qualifying signal before they may match at all.
    ambiguous_surfaces: frozenset[str] = frozenset()

    @property
    def is_country(self) -> bool:
        return self.kind == "country"


@dataclass(frozen=True)
class GeographyBase:
    """One loaded, validated geography edition."""

    edition: str
    title: str
    source: str
    license: str
    criteria: str
    regions: tuple[str, ...]
    update_procedure: str
    places: tuple[Place, ...]
    #: normalized surface -> place code. One surface means one place, checked at
    #: load time; a collision is a data error, not a tie to break at runtime.
    surfaces: dict[str, str]
    by_code: dict[str, Place]

    @property
    def countries(self) -> tuple[Place, ...]:
        return tuple(place for place in self.places if place.is_country)

    @property
    def cities(self) -> tuple[Place, ...]:
        return tuple(place for place in self.places if not place.is_country)


@dataclass
class GeographyCandidate:
    """One place the evidence actually named, with where it was named."""

    handle: str
    place: Place
    #: The wording that matched, exactly as written in the evidence.
    matched_surface: str
    #: Evidence handles (``F1``…) whose text contained the match.
    evidence_handles: tuple[str, ...]
    #: Soft context flags observed near the match.
    flags: tuple[str, ...] = ()
    #: True when the match relied on a surface listed as ambiguous and a
    #: qualifying signal was found. The candidate is real; the reader should
    #: still know the word was a coin-flip.
    qualified_ambiguous: bool = False
    #: First (fact index, character offset) at which it was seen — the ordering
    #: key, so the same evidence always produces the same handles.
    first_seen: tuple[int, int] = (0, 0)

    def as_prompt_line(self) -> str:
        where = ", ".join(self.evidence_handles)
        notes: list[str] = []
        if self.qualified_ambiguous:
            notes.append("ambiguous name, qualified by nearby wording")
        notes.extend(flag.replace("_", " ") for flag in self.flags)
        suffix = f" [{'; '.join(notes)}]" if notes else ""
        if self.place.is_country:
            what = f"{self.place.label} (country)"
        else:
            what = f"{self.place.label}, {self.place.country_name} (city)"
        return f"[{self.handle}] {what} — mentioned in {where}{suffix}"


@dataclass(frozen=True)
class ExtractionResult:
    """Everything one deterministic scan produced, including what it refused."""

    candidates: tuple[GeographyCandidate, ...]
    #: Human-readable notes about matches that were deliberately not offered.
    #: Recorded on the produced version so a refusal is visible, never silent.
    warnings: tuple[str, ...] = ()

    def by_handle(self, handle: str) -> GeographyCandidate | None:
        needle = handle.strip().upper()
        for candidate in self.candidates:
            if candidate.handle == needle:
                return candidate
        return None


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def load_base(path: str | None = None) -> GeographyBase:
    """Load and validate the vendored geography edition.

    Cached: the file is committed data, it does not change at runtime, and
    re-parsing it per company would be a needless cost on every production run.
    """

    source_path = Path(path) if path else DATA_FILE
    try:
        raw: dict[str, Any] = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GeographyDataError(f"geography base at {source_path} is unreadable: {exc}") from exc
    return _build(raw)


def _build(raw: dict[str, Any]) -> GeographyBase:
    for key in ("edition", "title", "source", "license", "criteria", "countries"):
        if not raw.get(key):
            raise GeographyDataError(f"geography base is missing required key {key!r}")

    places: list[Place] = []
    surfaces: dict[str, str] = {}
    alpha2_seen: set[str] = set()
    alpha3_seen: set[str] = set()

    def claim(surface: str, code: str) -> None:
        key = normalize_term(surface)
        if not key:
            raise GeographyDataError(f"{surface!r} normalizes to nothing")
        owner = surfaces.get(key)
        if owner is not None and owner != code:
            raise GeographyDataError(
                f"surface {surface!r} would resolve to both {owner!r} and {code!r}; "
                "one surface must mean one place"
            )
        surfaces[key] = code

    for country in raw["countries"]:
        alpha2 = str(country.get("alpha2", ""))
        alpha3 = str(country.get("alpha3", ""))
        name = str(country.get("name", ""))
        if not re.fullmatch(r"[A-Z]{2}", alpha2):
            raise GeographyDataError(f"{name!r} has a malformed ISO alpha-2 code {alpha2!r}")
        if not re.fullmatch(r"[A-Z]{3}", alpha3):
            raise GeographyDataError(f"{name!r} has a malformed ISO alpha-3 code {alpha3!r}")
        if alpha2 in alpha2_seen or alpha3 in alpha3_seen:
            raise GeographyDataError(f"duplicate ISO code for {name!r}")
        alpha2_seen.add(alpha2)
        alpha3_seen.add(alpha3)

        country_code = alpha2.lower()
        country_ambiguous = frozenset(
            normalize_term(item) for item in country.get("ambiguous_surfaces", ())
        )
        country_place = Place(
            kind="country",
            code=country_code,
            label=name,
            country_code=country_code,
            country_name=name,
            country_alpha3=alpha3,
            region=str(country.get("region", "")),
            ambiguous_surfaces=country_ambiguous,
        )
        places.append(country_place)
        for surface in (name, alpha3, *country.get("aliases", ())):
            claim(str(surface), country_code)

        for city in country.get("cities", ()):
            city_code = str(city.get("code", ""))
            city_name = str(city.get("name", ""))
            if not city_code or not city_name:
                raise GeographyDataError(f"a city of {name!r} is missing a code or a name")
            if city_code in {place.code for place in places}:
                raise GeographyDataError(f"duplicate city code {city_code!r}")
            places.append(
                Place(
                    kind="city",
                    code=city_code,
                    label=city_name,
                    country_code=country_code,
                    country_name=name,
                    country_alpha3=alpha3,
                    region=str(country.get("region", "")),
                    ambiguous_surfaces=frozenset(
                        normalize_term(item) for item in city.get("ambiguous_surfaces", ())
                    ),
                )
            )
            for surface in (city_name, *city.get("aliases", ())):
                claim(str(surface), city_code)

    by_code = {place.code: place for place in places}
    for place in places:
        if not place.is_country and place.country_code not in by_code:
            raise GeographyDataError(f"city {place.code!r} resolves to no known country")

    return GeographyBase(
        edition=str(raw["edition"]),
        title=str(raw["title"]),
        source=str(raw["source"]),
        license=str(raw["license"]),
        criteria=str(raw["criteria"]),
        regions=tuple(str(item) for item in raw.get("regions", ())),
        update_procedure=str(raw.get("update_procedure", "")),
        places=tuple(places),
        surfaces=surfaces,
        by_code=by_code,
    )


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Token:
    text: str
    start: int
    end: int


def _tokenize(text: str) -> list[_Token]:
    """Word tokens with their spans, each normalized on its own.

    Normalizing per token rather than per string keeps the original offsets
    usable: NFKD folding changes lengths, and a match that cannot point back at
    the wording it matched is a match nobody can check.
    """

    tokens: list[_Token] = []
    for match in _WORD.finditer(text):
        folded = unicodedata.normalize("NFKD", match.group(0)).casefold()
        folded = "".join(char for char in folded if not unicodedata.combining(char))
        cleaned = re.sub(r"[^a-z0-9]+", "", folded)
        if cleaned:
            tokens.append(_Token(cleaned, match.start(), match.end()))
    return tokens


def _spans_one_sentence(text: str, tokens: list[_Token], start: int, end: int) -> bool:
    """True when a multi-token match does not straddle a sentence boundary.

    Without this, "…based in Bath. Reading the report…" could match a two-token
    surface across the full stop. Cheap to check, and the alternative is a class
    of match that reads as evidence and is not.
    """

    if end - start <= 1:
        return True
    between = text[tokens[start].end : tokens[end - 1].start]
    return _SENTENCE_BREAK.search(between) is None


def _context(tokens: list[_Token], start: int, end: int) -> set[str]:
    lo = max(0, start - SIGNAL_WINDOW)
    hi = min(len(tokens), end + SIGNAL_WINDOW)
    return {tokens[index].text for index in range(lo, hi) if not (start <= index < end)}


@dataclass
class _Hit:
    place: Place
    surface: str
    fact_index: int
    offset: int
    handle: str
    flags: set[str]
    qualified_ambiguous: bool


def _scan(
    base: GeographyBase,
    *,
    text: str,
    fact_index: int,
    evidence_handle: str,
    suppressed: list[str],
) -> list[_Hit]:
    """Find every place named in one unit of text. Longest match wins."""

    tokens = _tokenize(text)
    hits: list[_Hit] = []
    index = 0
    while index < len(tokens):
        matched = False
        upper = min(len(tokens), index + MAX_SURFACE_TOKENS)
        for end in range(upper, index, -1):
            if not _spans_one_sentence(text, tokens, index, end):
                continue
            key = " ".join(token.text for token in tokens[index:end])
            code = base.surfaces.get(key)
            if code is None:
                continue
            place = base.by_code[code]
            context = _context(tokens, index, end)
            surface_text = text[tokens[index].start : tokens[end - 1].end]

            hard = sorted(
                {_HARD_SUPPRESSORS[word] for word in context if word in _HARD_SUPPRESSORS}
            )
            if hard:
                suppressed.append(
                    f"{surface_text!r} in {evidence_handle} was not offered as a geography "
                    f"candidate: {', '.join(hard)} context"
                )
                index = end
                matched = True
                break

            qualified = False
            if key in place.ambiguous_surfaces:
                # Capitalization is a weak signal and is used as one: it is
                # necessary but never sufficient. "reading the manual" is not a
                # town whatever sits beside it, and "Reading" on its own is not a
                # town either — it takes a capital *and* something nearby that
                # makes it a place.
                capitalized = _starts_capitalized(surface_text)
                country_named = _country_named(base, tokens, place)
                indicated = bool(context & _LOCATION_INDICATORS)
                if not (capitalized and (country_named or indicated)):
                    suppressed.append(
                        f"{surface_text!r} in {evidence_handle} was not offered as a geography "
                        "candidate: the name is ambiguous and nothing nearby made it a place"
                    )
                    index = end
                    matched = True
                    break
                qualified = True

            hits.append(
                _Hit(
                    place=place,
                    surface=surface_text,
                    fact_index=fact_index,
                    offset=tokens[index].start,
                    handle=evidence_handle,
                    flags={_SOFT_FLAGS[word] for word in context if word in _SOFT_FLAGS},
                    qualified_ambiguous=qualified,
                )
            )
            index = end
            matched = True
            break
        if not matched:
            index += 1
    return hits


def _starts_capitalized(surface: str) -> bool:
    for char in surface:
        if char.isalpha():
            return char.isupper()
    return False


def _country_named(base: GeographyBase, tokens: list[_Token], place: Place) -> bool:
    """True when the place's own country is named anywhere in the same text.

    "Cambridge, United Kingdom" and "our Cambridge site in the UK" both qualify;
    "Cambridge" alone does not. Co-occurrence is a weak signal on its own, which
    is why it only ever *unlocks* an ambiguous surface rather than asserting
    anything by itself.
    """

    country = base.by_code.get(place.country_code)
    if country is None:  # pragma: no cover - load-time validation prevents this
        return False
    joined = " ".join(token.text for token in tokens)
    for surface, code in base.surfaces.items():
        if code == country.code and re.search(rf"(?<!\w){re.escape(surface)}(?!\w)", joined):
            return True
    return False


def extract_candidates(
    source: IntelligenceInput, *, base: GeographyBase | None = None
) -> ExtractionResult:
    """Find the places one company's committed evidence actually names.

    Reads claim text and evidence excerpts only — never a source URL or title,
    because a publisher's city is the publisher's, not the company's.
    """

    edition = base if base is not None else load_base()
    suppressed: list[str] = []
    hits: list[_Hit] = []

    for fact_index, fact in enumerate(source.facts):
        hits.extend(
            _scan(
                edition,
                text=fact.claim,
                fact_index=fact_index,
                evidence_handle=fact.ref,
                suppressed=suppressed,
            )
        )
        for evidence in fact.evidence:
            if evidence.excerpt and evidence.excerpt.strip() != fact.claim.strip():
                hits.extend(
                    _scan(
                        edition,
                        text=evidence.excerpt,
                        fact_index=fact_index,
                        evidence_handle=fact.ref,
                        suppressed=suppressed,
                    )
                )

    # The same excerpt often repeats its claim verbatim, so an identical refusal
    # can be observed twice. One refusal, one line: a reader counting them should
    # be counting places, not scans.
    seen_warnings: list[str] = []
    for warning in suppressed:
        if warning not in seen_warnings:
            seen_warnings.append(warning)
    suppressed = seen_warnings

    merged: dict[str, GeographyCandidate] = {}
    for hit in sorted(hits, key=lambda item: (item.fact_index, item.offset, item.place.code)):
        existing = merged.get(hit.place.code)
        if existing is None:
            merged[hit.place.code] = GeographyCandidate(
                handle="",
                place=hit.place,
                matched_surface=hit.surface,
                evidence_handles=(hit.handle,),
                flags=tuple(sorted(hit.flags)),
                qualified_ambiguous=hit.qualified_ambiguous,
                first_seen=(hit.fact_index, hit.offset),
            )
            continue
        if hit.handle not in existing.evidence_handles:
            existing.evidence_handles = (*existing.evidence_handles, hit.handle)
        # A second sighting that carries no soft flag clears the doubt the first
        # one raised: "acquired a Paris company" plus "our Paris office" is an
        # office. Flags therefore intersect rather than accumulate.
        existing.flags = tuple(sorted(set(existing.flags) & hit.flags))
        existing.qualified_ambiguous = existing.qualified_ambiguous and hit.qualified_ambiguous

    ordered = sorted(merged.values(), key=lambda item: (item.first_seen, item.place.code))
    if len(ordered) > MAX_CANDIDATES:
        dropped = ordered[MAX_CANDIDATES:]
        suppressed.append(
            f"{len(dropped)} geography candidate(s) beyond the cap of {MAX_CANDIDATES} were "
            f"not offered: {', '.join(item.place.label for item in dropped[:10])}"
        )
        ordered = ordered[:MAX_CANDIDATES]

    for position, candidate in enumerate(ordered, start=1):
        candidate.handle = f"G{position}"

    return ExtractionResult(candidates=tuple(ordered), warnings=tuple(suppressed))


# --------------------------------------------------------------------------
# Post-model checks
# --------------------------------------------------------------------------


def parse_relationship(value: Any) -> IntelligenceGeoRelationship | None:
    """The relationship the model named, or None when it is not one of ours."""

    try:
        return IntelligenceGeoRelationship(str(value).strip().lower())
    except (ValueError, AttributeError):
        return None


def presence_for(relationship: IntelligenceGeoRelationship) -> IntelligencePresenceKind:
    return PRESENCE_FOR_RELATIONSHIP[relationship]


def flags_allow(candidate: GeographyCandidate, relationship: IntelligenceGeoRelationship) -> bool:
    """Whether the context a candidate was found in can support this answer.

    A place seen only inside a customer example cannot become a headquarters on
    the model's say-so. The candidate and the answer disagree, and the honest
    record of a disagreement is an unresolved value, not a quiet preference for
    one side.
    """

    for flag in candidate.flags:
        allowed = _FLAG_COMPATIBLE.get(flag)
        if allowed is not None and relationship not in allowed:
            return False
    return True


def edition_fingerprint(base: GeographyBase | None = None) -> str:
    """The edition label, for the input digest and for display."""

    return (base if base is not None else load_base()).edition


@dataclass(frozen=True)
class GeographyDecision:
    """One validated geography classification, before it becomes a row."""

    candidate: GeographyCandidate
    relationship: IntelligenceGeoRelationship
    presence: IntelligencePresenceKind
    evidence_handles: tuple[str, ...]
    rationale: str | None
    confidence: float | None
    unresolved_reason: str | None
    is_current: bool
    #: Set when this row was derived from an accepted city rather than proposed.
    inferred_from: str | None = None
    _sort: tuple[int, int, str] = field(default=(0, 0, ""), compare=False)

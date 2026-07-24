"""Name/domain normalization for email generation (EML-001).

Turns a person's normalized display name and a confirmed company domain into the
ASCII tokens an email local part is built from, handling the cases that actually
occur in imported B2B data: diacritics, apostrophes and hyphens, compound and
particle surnames ("van der Berg", "de la Cruz"), middle names, and non-Latin
scripts that cannot be transliterated.

Design rules:

* Deterministic and versioned — the same input always yields the same tokens, and
  :data:`ENGINE_VERSION` changes when the rules change so stored candidates record
  exactly which engine produced them.
* Conservative — diacritics are folded to their ASCII base (``é`` -> ``e``); a
  character with no ASCII base is dropped, and if that empties a token the name is
  reported *unrenderable* rather than guessed at. We never invent letters.
* Lossless upstream — the contact's original and display names are untouched; this
  operates only on a copy for address construction.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Bumped whenever the tokenization rules change so a regenerate is detectable.
ENGINE_VERSION = "eml-1"

# Surname particles that are lower-cased and, in one common local-part variant,
# joined onto the following name part ("van der berg" -> "vanderberg"). We also
# keep a "last token only" variant ("berg"), so both real-world conventions are
# offered as candidates without guessing which a company uses.
_PARTICLES = frozenset(
    {"van", "von", "der", "den", "de", "del", "della", "di", "da", "la", "le", "el", "bin", "al"}
)

_ALLOWED = re.compile(r"[^a-z0-9]+")


def _fold_ascii(value: str) -> str:
    """Fold a Unicode string to lower-case ASCII letters/digits.

    Diacritics are decomposed and their combining marks dropped (NFKD), so ``ł``,
    ``ø`` and similar letters with no combining decomposition are handled by the
    explicit map below; anything else non-ASCII is removed.
    """

    # A few letters do not decompose to an ASCII base under NFKD; map them
    # explicitly so common European surnames still render.
    replacements = {
        "ø": "o",
        "ł": "l",
        "đ": "d",
        "ð": "d",
        "þ": "th",
        "ß": "ss",
        "æ": "ae",
        "œ": "oe",
    }
    lowered = value.lower()
    mapped = "".join(replacements.get(ch, ch) for ch in lowered)
    decomposed = unicodedata.normalize("NFKD", mapped)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _ALLOWED.sub("", stripped)


def _tokens(value: str | None) -> list[str]:
    if not value:
        return []
    # Split on whitespace and hyphens; apostrophes are removed inside a token
    # ("O'Brien" -> "obrien"). Empty tokens (after folding) are discarded.
    parts = re.split(r"[\s\-]+", value.strip())
    out: list[str] = []
    for part in parts:
        folded = _fold_ascii(part.replace("'", "").replace("’", ""))
        if folded:
            out.append(folded)
    return out


@dataclass(frozen=True)
class EmailIdentity:
    """Normalized ASCII name tokens used to build candidate local parts.

    ``renderable`` is False when the name cannot be reduced to ASCII letters (for
    example a name written only in a non-Latin script). Callers must route such a
    contact to human review rather than generate a meaningless address (EML-005).
    """

    first: str
    last: str
    first_initial: str
    last_initial: str
    middle_initials: tuple[str, ...] = ()
    # Alternative last-name renderings for particle/compound surnames.
    last_variants: tuple[str, ...] = ()
    renderable: bool = True
    reason: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)


def build_identity(first_name: str | None, last_name: str | None) -> EmailIdentity:
    """Build the ASCII token set for a person's name.

    Uses the first token of the given name as the first name and its remaining
    tokens as middle initials; the surname keeps particle-joined and last-token
    variants so both common conventions can be generated.
    """

    first_tokens = _tokens(first_name)
    last_tokens = _tokens(last_name)
    warnings: list[str] = []

    if not first_tokens and not last_tokens:
        return EmailIdentity(
            first="",
            last="",
            first_initial="",
            last_initial="",
            renderable=False,
            reason="name has no ASCII-renderable letters",
        )
    if not first_tokens:
        warnings.append("no renderable first name; used surname only")
    if not last_tokens:
        warnings.append("no renderable surname; used given name only")

    first = first_tokens[0] if first_tokens else ""
    middle = first_tokens[1:] if len(first_tokens) > 1 else []

    # Surname handling: separate leading particles from the significant parts.
    significant = [t for t in last_tokens if t not in _PARTICLES]
    last_variants: list[str] = []
    if last_tokens:
        joined_all = "".join(last_tokens)  # vanderberg
        last_variants.append(joined_all)
        if significant:
            joined_significant = "".join(significant)  # derberg? -> we keep both
            if joined_significant != joined_all:
                last_variants.append(joined_significant)
            if significant[-1] != joined_all:
                last_variants.append(significant[-1])  # berg (final significant token)
    # Primary "last" is the full joined surname (most portable), first variant.
    last = last_variants[0] if last_variants else ""
    # Deduplicate variants preserving order.
    seen: set[str] = set()
    deduped_list: list[str] = []
    for v in last_variants:
        if v not in seen:
            seen.add(v)
            deduped_list.append(v)
    deduped_variants = tuple(deduped_list)

    first_initial = first[0] if first else ""
    last_initial = last[0] if last else ""

    return EmailIdentity(
        first=first,
        last=last,
        first_initial=first_initial,
        last_initial=last_initial,
        middle_initials=tuple(t[0] for t in middle if t),
        last_variants=deduped_variants,
        renderable=True,
        warnings=tuple(warnings),
    )

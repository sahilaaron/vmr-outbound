"""Deterministic text normalization for vocabulary matching (CI-001).

One function, used everywhere a human-written or model-written string has to be
compared against a controlled term. It is deliberately small and deliberately
boring: normalization that tries to be clever is normalization that silently
maps two different industries onto one.

What it does, and only this:

* Unicode NFKD, so the composed and decomposed spellings of an accented name
  compare equal, and combining marks are dropped rather than kept as invisible
  differences.
* Case folding.
* ``&`` becomes ``and``, because "Pharma & Healthcare" and "Pharma and
  Healthcare" are the same phrase written twice.
* Every remaining non-alphanumeric run collapses to a single space, which
  absorbs hyphens, slashes, commas, parentheses and stray punctuation.
* Leading and trailing whitespace goes.

What it deliberately does NOT do: stemming, plural stripping, stopword removal,
synonym expansion or fuzzy distance. "Coating" and "Coatings" stay different
strings, and the way to make them the same answer is an **alias** — a decision
somebody made and signed, visible in the vocabulary — not a rule buried in a
matcher that nobody can review.
"""

from __future__ import annotations

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

#: Ampersand handling happens before punctuation collapse, so "R&D" becomes
#: "r and d" rather than "r d". The two are different phrases and only one of
#: them is what anyone wrote.
_AMPERSAND = re.compile(r"\s*&\s*")


def normalize_term(value: str) -> str:
    """Return the comparison form of ``value``. Empty input returns ``""``."""

    if not value:
        return ""
    folded = unicodedata.normalize("NFKD", value).casefold()
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    folded = _AMPERSAND.sub(" and ", folded)
    collapsed = _NON_ALNUM.sub(" ", folded)
    return collapsed.strip()


def slugify_code(value: str, *, prefix: str = "") -> str:
    """Return a stable, slug-shaped code for a canonical label.

    Used only when seeding a vocabulary from a source that supplies labels but
    no codes. Codes are stable identifiers, so this runs once per term at seed
    time and its output is then stored — it is never recomputed to *find* a term.
    """

    slug = _NON_ALNUM.sub("-", normalize_term(value)).strip("-")
    return f"{prefix}{slug}" if prefix else slug

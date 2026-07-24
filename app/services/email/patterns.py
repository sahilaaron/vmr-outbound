"""Versioned, deterministic email-pattern generation (EML-002).

Given a normalized :class:`EmailIdentity` and a domain, produce a bounded, ordered,
duplicate-free set of candidate addresses. Each candidate records the exact naming
pattern that produced it and the engine version, so the choice is transparent and
reproducible. Ordering here is the *base* priority by pattern commonness; internal
evidence may reorder candidates later (EML-004) but generation itself never marks
any address valid.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.services.email.normalization import ENGINE_VERSION, EmailIdentity

# Ordered common patterns, most→least frequent in B2B corporate mail. The base
# rank is the index. Templates receive the identity and return a local part or ""
# when the identity lacks the needed token (that pattern is then skipped).
_Template = Callable[[EmailIdentity], str]


def _p(fn: _Template) -> _Template:
    return fn


_PATTERNS: list[tuple[str, _Template]] = [
    ("{first}.{last}", _p(lambda i: f"{i.first}.{i.last}" if i.first and i.last else "")),
    ("{first}", _p(lambda i: i.first)),
    ("{f}{last}", _p(lambda i: f"{i.first_initial}{i.last}" if i.first_initial and i.last else "")),
    ("{first}{last}", _p(lambda i: f"{i.first}{i.last}" if i.first and i.last else "")),
    (
        "{f}.{last}",
        _p(lambda i: f"{i.first_initial}.{i.last}" if i.first_initial and i.last else ""),
    ),
    ("{first}_{last}", _p(lambda i: f"{i.first}_{i.last}" if i.first and i.last else "")),
    ("{last}.{first}", _p(lambda i: f"{i.last}.{i.first}" if i.first and i.last else "")),
    (
        "{first}{l}",
        _p(lambda i: f"{i.first}{i.last_initial}" if i.first and i.last_initial else ""),
    ),
    ("{last}", _p(lambda i: i.last)),
    (
        "{f}{l}",
        _p(
            lambda i: (
                f"{i.first_initial}{i.last_initial}" if i.first_initial and i.last_initial else ""
            )
        ),
    ),
]

# For alternate surname renderings (particle/compound names) we only re-emit the
# two most common patterns to keep the set bounded.
_VARIANT_PATTERNS: list[tuple[str, str]] = [
    ("{first}.{last}", "{first}.{lastvar}"),
    ("{f}{last}", "{f}{lastvar}"),
]


@dataclass(frozen=True)
class PatternCandidate:
    """One generated local part with the pattern and engine that produced it."""

    pattern: str
    local_part: str
    base_rank: int


def generate_local_parts(identity: EmailIdentity) -> list[PatternCandidate]:
    """Return the ordered, de-duplicated local-part candidates for an identity.

    Duplicate local parts (different patterns collapsing to the same string) keep
    only their first, highest-priority occurrence (EML-002: "without duplicates").
    """

    out: list[PatternCandidate] = []
    seen: set[str] = set()

    def _add(pattern: str, local: str) -> None:
        if not local or local in seen:
            return
        seen.add(local)
        out.append(PatternCandidate(pattern=pattern, local_part=local, base_rank=len(out)))

    for pattern, template in _PATTERNS:
        _add(pattern, template(identity))

    # Alternate surname renderings for particle/compound names.
    for alt_last in identity.last_variants[1:]:
        alt = EmailIdentity(
            first=identity.first,
            last=alt_last,
            first_initial=identity.first_initial,
            last_initial=alt_last[0] if alt_last else "",
        )
        for base_pattern, label in _VARIANT_PATTERNS:
            template = dict(_PATTERNS)[base_pattern]
            _add(label, template(alt))

    return out


def engine_version() -> str:
    """The current generation engine version stamped onto every candidate."""

    return ENGINE_VERSION

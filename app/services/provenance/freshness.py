"""Deterministic, versioned freshness policy for field-level provenance (DAT-005).

This module owns the single question *"given every observation of one contact
field, which one currently wins?"* The answer must be:

* **deterministic** — the same observations always yield the same winner;
* **reproducible** — the winner is a pure function of stored columns plus this
  policy version, so it can be recomputed from the ledger at any time;
* **safe** — newer evidence is never silently replaced by older evidence, and a
  manual operator override is never silently replaced by an import.

The policy is intentionally a pure function of the observations passed in; it does
no I/O. :mod:`app.services.provenance.service` applies it against the database and
records the outcome.

Ordering
--------
Each observation is reduced to a total-order sort key; the observation with the
greatest key wins. The key, most significant first:

1. ``is_manual_override`` — a manual override outranks every import observation.
2. ``effective_at`` — the observation time (``observed_at``), or ``ingested_at``
   when the source gave no observation time. Newer wins ("newer beats older").
3. ``has_observed_at`` — on an equal effective time, a real source observation
   time outranks one relying on the ingestion fallback (prefer known provenance).
4. ``ingested_at`` — a later ingestion breaks a remaining tie deterministically.
5. ``id`` — the final, always-unique tie-break, so the order is total.

Because the winner is the maximum under this key, appending an *older* observation
can never change the winner, and re-running the policy over the same rows always
returns the same winner. Every branch is covered by :mod:`tests.test_field_provenance`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

# Bump this string whenever the ordering rules below change. It is stored on
# every reconciled observation so a past decision is always attributable to the
# exact rules that produced it.
FRESHNESS_POLICY_VERSION = "freshness-v1"

# The operational contact fields whose value provenance is tracked and reconciled.
# Deliberately excludes identity/dedup keys (first_name, last_name, email,
# company_domain): changing those changes *identity*, which is governed by
# deduplication and identity resolution (DAT-004), not field freshness. These are
# the descriptive fields two imports may legitimately disagree about over time.
TRACKED_FIELDS: tuple[str, ...] = (
    "title",
    "company_name",
    "company_size",
    "industry",
    "country",
    "linkedin_url",
)


@dataclass(frozen=True)
class Observation:
    """The freshness-relevant projection of one stored field observation.

    Kept separate from the ORM model so the policy is a pure, easily-tested
    function. ``key`` is any stable identifier (the row id) used only as the final
    deterministic tie-break.
    """

    key: str
    value: str | None
    observed_at: datetime | None
    ingested_at: datetime
    is_manual_override: bool


def _effective_at(observation: Observation) -> datetime:
    """The timestamp used to compare freshness.

    Uses the source observation time when known, else the ingestion time as a
    lower bound on the value's age (the "missing timestamp" case). Both are
    timezone-aware in stored data.
    """

    if observation.observed_at is not None:
        return observation.observed_at
    return observation.ingested_at


def sort_key(observation: Observation) -> tuple[bool, datetime, bool, datetime, str]:
    """Total-order key; the greatest key is the winner. See module docstring."""

    return (
        observation.is_manual_override,
        _effective_at(observation),
        observation.observed_at is not None,
        observation.ingested_at,
        observation.key,
    )


def resolve_winner(observations: Sequence[Observation]) -> Observation | None:
    """Return the single winning observation, or None when there are none."""

    if not observations:
        return None
    return max(observations, key=sort_key)


def explain_decision(winner: Observation, others: Iterable[Observation]) -> str:
    """A short, human-readable reason the winner beat the rest of the set.

    The wording names the specific rule that decided it so an operator reading the
    audit trail sees *why* this value is the one in use, not merely that it is.
    """

    others = [o for o in others if o.key != winner.key]
    if not others:
        return "only observation of this field"
    if winner.is_manual_override:
        return "manual operator override outranks all import observations"

    challenger = max(others, key=sort_key)
    win_eff = _effective_at(winner)
    ch_eff = _effective_at(challenger)
    if win_eff > ch_eff:
        basis = "observed" if winner.observed_at is not None else "ingested"
        return f"most recent evidence ({basis} {win_eff.isoformat()})"
    if winner.observed_at is not None and challenger.observed_at is None:
        return "equal timestamp; a known source observation time outranks an ingestion fallback"
    if winner.ingested_at > challenger.ingested_at:
        return "equal observation time; most recently ingested observation wins"
    return "equal freshness; deterministic tie-break on observation id"

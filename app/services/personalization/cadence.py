"""Planned timing for a seven-message sequence.

Three words are used carefully here and nowhere interchangeably.

*Planned* timing is what the sequence records: an intended shape, decided when
the sequence was generated. *Recommended* delay is the gap this module suggests
between one message and the one before it. Neither is a **schedule**. Nothing
in this build enqueues anything at a time, and there is no scheduler to enqueue
it into. A sequence that says "Follow-up 3 on day 12" is describing an
intention a human may act on, not an instruction a machine will execute.

The default ladder widens as it goes -- 3, 4, 5, 6, 7, 10 days -- because a
reminder three days after a first message is normal and a sixth follow-up three
days after the fifth is harassment. A Campaign may override it; the override is
bounded, and an override that would produce impossible timing is refused rather
than clamped, because silently rewriting an operator's cadence into something
they did not ask for is worse than telling them it was wrong.

Where the override lives. ``campaigns.cadence_config`` is an existing JSONB
column that has been declared, migrated and plumbed through ``create_campaign``
and ``update_campaign`` since Phase 2, and never read by anything. This module
is its first consumer. It claims exactly one key -- ``"sequence"`` -- and
ignores everything else in the column, so a Campaign carrying unrelated cadence
notes keeps them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.campaign import Campaign
from app.models.email_sequence import SEQUENCE_LENGTH

#: The key inside ``campaigns.cadence_config`` this module owns.
CADENCE_KEY = "sequence"

#: Where a value this module cannot read is kept when it has to write beside it.
#: Only ever written by :func:`with_campaign_opt_in`, and never read back — the
#: point is that the original survives for a human to look at, not that anything
#: here knows what to do with it.
UNREADABLE_CONFIG_KEY = "_unreadable_cadence_config"
UNREADABLE_SEQUENCE_KEY = "_unreadable_sequence"

#: Bounded default. Day 0 for the initial message, then a widening ladder.
DEFAULT_ELAPSED_DAYS: tuple[int, ...] = (0, 3, 7, 12, 18, 25, 35)

#: A single gap wider than this is almost certainly a typo rather than a plan.
MAX_DELAY_DAYS = 365
#: And a sequence spanning more than this has stopped being one sequence.
MAX_ELAPSED_DAY = 3650

SOURCE_DEFAULT = "default"
SOURCE_CAMPAIGN = "campaign"


class CadenceError(ValueError):
    """A Campaign cadence override cannot be honoured as written."""


@dataclass(frozen=True)
class SequenceCadence:
    """The planned timing for all seven positions.

    ``elapsed_days[i]`` is the intended day of position ``i + 1`` counted from
    the initial message; ``delays[i]`` is the gap from its predecessor. The two
    are redundant on purpose: a reader asking "when" and a reader asking "how
    long after the last one" should not each have to do arithmetic that the
    other might do differently.
    """

    elapsed_days: tuple[int, ...]
    source: str

    @property
    def delays(self) -> tuple[int, ...]:
        previous = 0
        gaps: list[int] = []
        for day in self.elapsed_days:
            gaps.append(day - previous)
            previous = day
        return tuple(gaps)

    @property
    def span_days(self) -> int:
        return self.elapsed_days[-1]

    def for_position(self, position: int) -> tuple[int, int]:
        """Return ``(delay_from_predecessor, elapsed_day)`` for one position."""

        index = position - 1
        return self.delays[index], self.elapsed_days[index]

    def summary(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "elapsed_days": list(self.elapsed_days),
            "delays": list(self.delays),
            "span_days": self.span_days,
        }


def default_cadence() -> SequenceCadence:
    return SequenceCadence(elapsed_days=DEFAULT_ELAPSED_DAYS, source=SOURCE_DEFAULT)


def _validate_elapsed_days(values: list[Any]) -> tuple[int, ...]:
    """Refuse anything that is not a usable seven-step ladder.

    Every rejection here is a rejection rather than a correction. An operator
    who wrote a negative delay or put follow-up 4 before follow-up 3 has made a
    mistake worth seeing; clamping it would hide the mistake behind timing they
    never chose.
    """

    if len(values) != SEQUENCE_LENGTH:
        raise CadenceError(
            f"A sequence cadence needs exactly {SEQUENCE_LENGTH} elapsed days, "
            f"and this one has {len(values)}."
        )
    days: list[int] = []
    for index, value in enumerate(values):
        # bool is an int in Python, and True would silently become day 1.
        if isinstance(value, bool) or not isinstance(value, int):
            raise CadenceError(
                f"Elapsed day {index + 1} must be a whole number of days, not {value!r}."
            )
        days.append(value)
    if days[0] != 0:
        raise CadenceError(
            f"The initial message is day 0 by definition; it cannot be scheduled for day {days[0]}."
        )
    previous = 0
    for index, day in enumerate(days[1:], start=2):
        if day <= previous:
            raise CadenceError(
                f"Follow-up {index - 1} is planned for day {day}, which is not after "
                f"the message before it on day {previous}. A follow-up cannot precede "
                "or coincide with its predecessor."
            )
        if day - previous > MAX_DELAY_DAYS:
            raise CadenceError(
                f"The gap before follow-up {index - 1} is {day - previous} days, "
                f"beyond the {MAX_DELAY_DAYS}-day bound."
            )
        previous = day
    if days[-1] > MAX_ELAPSED_DAY:
        raise CadenceError(
            f"The sequence would span {days[-1]} days, beyond the {MAX_ELAPSED_DAY}-day bound."
        )
    return tuple(days)


def sequence_settings(campaign: Campaign) -> dict[str, Any]:
    """The ``sequence`` block of the Campaign's cadence config, or an empty one.

    Malformed configuration is treated as absent rather than fatal. A Campaign
    whose ``cadence_config`` holds a string where an object belongs has not
    opted in to anything, and refusing to render its page would be a worse
    answer than treating it as unconfigured.
    """

    raw = campaign.cadence_config
    if not isinstance(raw, dict):
        return {}
    block = raw.get(CADENCE_KEY)
    if not isinstance(block, dict):
        return {}
    return block


def campaign_opted_in(campaign: Campaign) -> bool:
    """Whether this Campaign has explicitly asked for seven-message sequences.

    Opt-in is per Campaign and defaults to off, so turning the deployment flag
    on does not silently change what every existing Campaign produces.
    """

    return sequence_settings(campaign).get("enabled") is True


def with_campaign_opt_in(campaign: Campaign, *, enabled: bool) -> dict[str, Any]:
    """The Campaign's ``cadence_config`` with only the opt-in flag changed.

    Lives next to :func:`sequence_settings` deliberately: the writer and the
    reader have to agree about the shape of this column, and keeping them in one
    module is what stops them drifting. Until now nothing wrote it at all — the
    opt-in could only be set by editing JSON by hand — so the reader's
    assumptions had never been tested from the writing side.

    Two properties matter and both are easy to get wrong:

    * **Nothing already stored is lost.** The column belongs to the Campaign,
      not to this module, and this module claims exactly one key. Unrelated keys
      are carried through, and a value this module cannot read is moved aside
      under :data:`UNREADABLE_CONFIG_KEY` or :data:`UNREADABLE_SEQUENCE_KEY`
      rather than being overwritten. Malformed configuration is treated as
      absent for *deciding*, matching :func:`sequence_settings`, but never as
      absent for *writing* — the reader can afford to read past something it
      does not understand, and a writer cannot afford to delete it.
    * **The flag is a real ``bool``.** :func:`campaign_opted_in` tests
      ``is True``, so an HTML checkbox arriving as the string ``"on"`` would
      write a value that reads back as *not* opted in — a control that appears
      to work and does nothing. Callers coerce before calling; the annotation
      keeps that requirement visible.
    """

    raw = campaign.cadence_config
    if isinstance(raw, dict):
        config: dict[str, Any] = dict(raw)
    else:
        # Not an object. The reader treats this as absent and carries on, and
        # this writer has to meet the same rows -- but "treat as absent" must
        # not become "delete", which is what replacing the column outright
        # would do.
        #
        # Refusing instead was tried and is worse: this function is on the path
        # of *every* settings save, so raising here would make an unreadable
        # value block renaming the campaign too, breaking the settings page for
        # exactly the campaign somebody is trying to repair.
        #
        # So the unreadable value is moved aside and kept. Nothing is lost,
        # nothing is blocked, and the next write sees a well-formed object and
        # quarantines nothing.
        config = {} if raw is None else {UNREADABLE_CONFIG_KEY: raw}
    block = config.get(CADENCE_KEY)
    if isinstance(block, dict):
        sequence: dict[str, Any] = dict(block)
    else:
        # The realistic case: `cadence_config` reaches the JSON API as an
        # unvalidated object, so `{"sequence": "..."}` is storable even though
        # the column itself is validated as an object.
        sequence = {}
        if block is not None:
            config[UNREADABLE_SEQUENCE_KEY] = block
    sequence["enabled"] = bool(enabled)
    config[CADENCE_KEY] = sequence
    return config


def resolve_cadence(campaign: Campaign) -> SequenceCadence:
    """The planned timing for this Campaign's sequences.

    Raises :class:`CadenceError` when the Campaign carries an override that
    cannot be honoured.
    """

    block = sequence_settings(campaign)
    raw = block.get("elapsed_days")
    if raw is None:
        return default_cadence()
    if not isinstance(raw, list):
        raise CadenceError("Campaign cadence 'elapsed_days' must be a list of whole days.")
    return SequenceCadence(elapsed_days=_validate_elapsed_days(raw), source=SOURCE_CAMPAIGN)

"""Bounding stored lineage JSON before it reaches an operator's screen.

A sequence records its Research, Insights and Company Intelligence lineage as
JSONB. Those values are written by trusted code today and are small — a dossier
id, a handful of insight ids, some counts. Nothing enforced that, so a page
rendering them was a page whose size was decided by whatever happened to be in
the column. A 2 MB lineage value produced a 2 MB Admin page.

This module is the boundary. It takes an arbitrary stored value and returns a
value that is safe to render: bounded in depth, in breadth, in string length and
in total size, and **honest about what it removed**. A truncated value is
replaced by a marker that says so, so a reader is never quietly shown a prefix as
though it were the whole thing.

Three things it deliberately does not do. It does not rewrite the stored value —
the record stays whole and a diagnosis is a view of it. It does not silently drop
keys, because a lineage missing a key reads as "there was no such lineage". And
it does not attempt to sanitise content for safety: escaping is the template's
job and Jinja already does it, so trying again here would be a second, weaker
implementation of something already correct.
"""

from __future__ import annotations

from typing import Any

#: A single string longer than this is a payload, not an identifier.
MAX_STRING_CHARS = 300
#: More keys than this in one object is a dump, not a record.
MAX_KEYS = 40
#: More entries than this in one list is the same.
MAX_ITEMS = 40
#: Deeper than this and nobody is reading it on a diagnosis page anyway.
MAX_DEPTH = 6
#: The ceiling on the whole rendered structure, counted in characters of
#: contained strings and keys. Reached only by something pathological.
MAX_TOTAL_CHARS = 20_000

TRUNCATION_KEY = "__truncated__"


def _marker(reason: str) -> str:
    return f"[truncated: {reason}]"


class _Budget:
    """Tracks how much of the total character allowance is left."""

    def __init__(self, total: int) -> None:
        self.remaining = total
        self.exhausted = False

    def take(self, amount: int) -> bool:
        if self.remaining < amount:
            self.exhausted = True
            return False
        self.remaining -= amount
        return True


def _bound(value: Any, *, depth: int, budget: _Budget) -> Any:
    if depth > MAX_DEPTH:
        return _marker(f"nested deeper than {MAX_DEPTH} levels")

    if isinstance(value, str):
        if len(value) > MAX_STRING_CHARS:
            budget.take(MAX_STRING_CHARS)
            return value[:MAX_STRING_CHARS] + _marker(f"{len(value)} characters")
        if not budget.take(len(value)):
            return _marker("lineage exceeded the total size limit")
        return value

    # bool before int/float: bool is an int in Python and would otherwise render
    # as 1 and 0, which is a different fact.
    if isinstance(value, bool) or value is None or isinstance(value, int | float):
        return value

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_KEYS:
                out[TRUNCATION_KEY] = _marker(f"{len(value) - MAX_KEYS} further key(s)")
                break
            if budget.exhausted:
                out[TRUNCATION_KEY] = _marker("lineage exceeded the total size limit")
                break
            name = str(key)[:MAX_STRING_CHARS]
            budget.take(len(name))
            out[name] = _bound(item, depth=depth + 1, budget=budget)
        return out

    if isinstance(value, list | tuple):
        out_list: list[Any] = []
        for index, item in enumerate(value):
            if index >= MAX_ITEMS:
                out_list.append(_marker(f"{len(value) - MAX_ITEMS} further item(s)"))
                break
            if budget.exhausted:
                out_list.append(_marker("lineage exceeded the total size limit"))
                break
            out_list.append(_bound(item, depth=depth + 1, budget=budget))
        return out_list

    # Anything else (a datetime, a UUID, something unexpected) becomes its own
    # bounded string rather than being dropped.
    return _bound(str(value), depth=depth, budget=budget)


def bounded_lineage(value: Any) -> dict[str, Any]:
    """Return a render-safe view of one stored lineage value.

    Always a dict, so a template can iterate it without a type check. A stored
    value that is not an object is wrapped rather than discarded, because
    "lineage was recorded but is not the shape we expect" is itself worth seeing
    on a diagnosis page.
    """

    if value is None:
        return {}
    budget = _Budget(MAX_TOTAL_CHARS)
    if not isinstance(value, dict):
        return {"value": _bound(value, depth=1, budget=budget)}
    bounded = _bound(value, depth=0, budget=budget)
    return bounded if isinstance(bounded, dict) else {"value": bounded}

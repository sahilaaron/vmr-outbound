"""Shared validation and errors for the seller knowledge base (KB-001).

One error class rather than one per module. Every refusal in this package has
the same shape — an operator typed something the record cannot hold — and the
web layer renders ``str(exc)`` straight into a flash message, so the messages
are written as complete sentences an operator can act on.

``MAX_LENGTHS`` is checked at the service boundary rather than left to the
database. A ``String(255)`` overflow aborts the whole transaction at the driver
with a message about a column; catching it here means the operator is told
which field was too long and everything else they typed survives.
"""

from __future__ import annotations

from typing import Any

# Column limits mirrored from the models. Text columns are unbounded in
# PostgreSQL, so the limits on long-form prose exist to keep a paste accident
# out of the database, not because the column would reject it.
MAX_LENGTHS: dict[str, int] = {
    "name": 255,
    "title": 255,
    "role_function": 255,
    "seniority": 120,
    "source_reference": 1024,
    "created_by": 120,
    "short_description": 2000,
    "description": 20000,
    "positioning": 20000,
    "communication_guidance": 20000,
    "notes": 20000,
    "statement": 4000,
    "supporting_detail": 20000,
    "explanation": 20000,
    "messaging_notes": 20000,
}

# A single list entry, and how many entries one field may hold. These are
# labels and short statements; the cap exists so a pasted document becomes an
# error the operator can see rather than a list of six hundred fragments.
MAX_LIST_ITEM_LENGTH = 500
MAX_LIST_ITEMS = 100

# Who the audit trail records for knowledge-base writes. The workbench has no
# authentication (it is local-only), so there is no user identity to record;
# saying "operator" is accurate, and inventing a name would not be.
OPERATOR_ACTOR = "operator"


class SellerKnowledgeError(ValueError):
    """A seller-side record that cannot be stored as asked.

    The message is safe to show an operator verbatim.
    """


def required_text(value: str | None, *, field: str, label: str) -> str:
    """Return a trimmed, length-checked value, refusing blank input."""

    text = (value or "").strip()
    if not text:
        raise SellerKnowledgeError(f"{label} is required.")
    limit = MAX_LENGTHS.get(field)
    if limit is not None and len(text) > limit:
        raise SellerKnowledgeError(f"{label} is too long (limit {limit} characters).")
    return text


def optional_text(value: str | None, *, field: str, label: str) -> str | None:
    """Return a trimmed, length-checked value, or ``None`` when empty.

    Empty and absent collapse to ``None`` on purpose for these free-text
    fields: an operator who clears a description means it is not set, and a
    stored empty string would be a third state nothing else distinguishes.
    """

    text = (value or "").strip()
    if not text:
        return None
    limit = MAX_LENGTHS.get(field)
    if limit is not None and len(text) > limit:
        raise SellerKnowledgeError(f"{label} is too long (limit {limit} characters).")
    return text


def clean_list(value: list[Any] | None, *, label: str) -> list[str] | None:
    """Normalize a list field, preserving the "not set" / "none apply" distinction.

    ``None`` in means nobody has filled this in and ``None`` comes back out.
    An empty list in means the operator considered it and there is nothing to
    say, and an empty list comes back out. Collapsing the two would lose the
    only signal that separates "unanswered" from "answered: none", which is the
    same distinction the dossier sections protect.

    Entries are trimmed, blanks dropped, and duplicates removed while keeping
    the operator's ordering — the order is how they chose to read it.
    """

    if value is None:
        return None
    if not isinstance(value, list):
        raise SellerKnowledgeError(f"{label} must be a list of short entries.")
    cleaned: list[str] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, str):
            raise SellerKnowledgeError(f"Every entry in {label} must be text.")
        text = entry.strip()
        if not text:
            continue
        if len(text) > MAX_LIST_ITEM_LENGTH:
            raise SellerKnowledgeError(
                f"One entry in {label} is too long (limit {MAX_LIST_ITEM_LENGTH} characters). "
                "Long prose belongs in the description or notes."
            )
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    if len(cleaned) > MAX_LIST_ITEMS:
        raise SellerKnowledgeError(f"{label} has more than {MAX_LIST_ITEMS} entries.")
    return cleaned


def parse_lines(raw: str | None) -> list[str] | None:
    """Turn a textarea's contents into a list field.

    The workbench collects list fields as one entry per line, because that is
    the only multi-value input a no-JavaScript form can offer without a fixed
    number of boxes. A textarea the operator never touched arrives as an empty
    string, which means "not set" — hence ``None`` rather than ``[]``.
    """

    if raw is None:
        return None
    if not raw.strip():
        return None
    return [line.strip() for line in raw.splitlines() if line.strip()]

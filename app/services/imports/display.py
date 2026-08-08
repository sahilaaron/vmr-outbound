"""The one boundary imported spreadsheet text crosses on its way to a person.

Every value that reached this system from a workbook cell is untrusted twice
over, in two different directions, and the two defences are not the same thing.

**Outward as HTML** is Jinja's job. Autoescaping handles it, it is on
everywhere, and nothing here weakens it.

**Outward as a spreadsheet** is this module's job. A value beginning with ``=``,
``+``, ``-`` or ``@`` is an expression to Excel, Google Sheets and LibreOffice,
so a cell that arrived as ``=cmd|'/c calc'!A0`` must not leave a rendered page,
an export or a copy-paste as one. Prefixing with an apostrophe is the
conventional neutralization: the text is preserved exactly and the receiving
application treats it as text.

The reason this module exists rather than a filter registered in one place is
the shape of the failure it is fixing. The protection was a convention — "call
``neutralize`` on spreadsheet-supplied values" — applied by hand at each call
site, and a convention applied by hand is applied unevenly. An independent
review found live formula strings rendering on four surfaces the previous repair
had not visited: the normalized address on the customer batch page, the imported
first name and title on the customer Contact page, the imported Company name on
the Contact and Company pages, and the sheet names in the workbook chooser.

So there is now exactly one function, registered into every template
environment that can render imported text under one name, and applied at the
read models rather than only in templates.

**What is never neutralized: the evidence itself.** ``import_rows.raw_data``,
``ImportedContactEmail.raw_email`` and the normalized reading keep exactly what
the file said. Neutralization is a projection concern — the moment a value is
shown or exported — because an operator asking "what did the file actually
contain" has to be able to get a true answer.
"""

from __future__ import annotations

from typing import Any

from app.services.imports.apollo import looks_like_formula, neutralize_formula

__all__ = ["is_formula_like", "safe_text", "safe_optional", "register_neutralize"]


def safe_text(value: Any) -> str:
    """Render any value as text that cannot travel onward as a formula.

    Returns ``""`` for ``None`` so a template can use it without a guard. Plain
    signed numbers pass through untouched — Excel renders ``-5`` as minus five,
    so prefixing it would damage an ordinary value to defend against nothing.
    """

    if value is None:
        return ""
    return neutralize_formula(str(value)) or ""


def safe_optional(value: str | None) -> str | None:
    """The same, but preserving ``None`` for read models that distinguish it."""

    return None if value is None else neutralize_formula(value)


def is_formula_like(value: str | None) -> bool:
    """Whether *value* would be read as an expression by a spreadsheet app."""

    return looks_like_formula(value)


def register_neutralize(environment: Any) -> None:
    """Install the boundary as the ``neutralize`` filter on a Jinja environment.

    Called from every template environment that can render imported text, so
    the filter means the same thing on the customer application and on the Admin
    Workbench and cannot drift between them.
    """

    environment.filters["neutralize"] = safe_text

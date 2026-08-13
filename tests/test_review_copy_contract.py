"""The static contract between ``sequence.js`` and the templates it drives.

``tests/test_review_copy_controls.py`` renders the pages and asserts the markup
is there. That is one half of the contract, and it is the half that cannot
break silently — a missing button is visible.

The other half can. The handler is a delegated listener keyed entirely on string
literals: an element id it looks the live region up by, an attribute selector it
matches clicks against, and the ``data-`` attribute names it reads off the button
it found. Every one of those literals is written twice, once in the JavaScript
and once in the Jinja template, with nothing connecting the two copies. Rename
one of them on the JavaScript side and the buttons still render, still say
``Copy Subject``, still pass every rendering test in this repository — and never
copy anything, because the selector matches nothing, or the live region resolves
to ``null``, or the announced label silently degrades. There is no browser here
to catch it, and staging serves the file under ``script-src 'self'`` where the
failure is a button that does nothing at all.

So this module reads both files as text and asserts the two copies are equal. It
runs no browser, renders no template and asserts nothing about the clipboard:
what it pins is that the strings the script looks for are the strings the markup
emits.

Both files are read with an explicit ``encoding="utf-8"``. The templates contain
em dashes and this repository is developed on a machine whose default encoding is
cp1252, where an implicit read raises ``UnicodeDecodeError``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The delegated handler. Note the path: it is served from the shared
#: ``app/web/static`` mount, not from a ``v2``-scoped one, and both the contact
#: page and the review queue load this same file rather than each carrying a
#: copy.
SEQUENCE_JS = REPO_ROOT / "app" / "web" / "static" / "sequence.js"

TEMPLATE_DIR = REPO_ROOT / "app" / "web" / "v2" / "templates"

#: The partial that renders the three copy buttons. Included by both pages, so
#: the buttons exist in exactly one place.
SEQUENCE_PARTIAL = TEMPLATE_DIR / "_sequence.html"

#: The two pages that own the live region and load the script.
LIVE_REGION_PAGES = (TEMPLATE_DIR / "review.html", TEMPLATE_DIR / "contact.html")

#: ``data-copy-label`` is the one attribute the script both writes and reads
#: itself: ``flash()`` stashes a button's original label there before replacing
#: it with "Copied", and restores it afterwards. No template emits it, and none
#: should — it is scratch storage on the element, not part of the markup
#: contract. Every *other* attribute the script reads has to come from a
#: template, which is what makes this an explicit allowance rather than a
#: blanket exemption.
JS_OWNED_ATTRIBUTES = frozenset({"data-copy-label"})


def _read(path: Path) -> str:
    assert path.is_file(), f"{path} does not exist; the contract test is pointed at the wrong file"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Reading the JavaScript
# ---------------------------------------------------------------------------

_JS_STRING_CONSTANT = re.compile(r"\bvar\s+([A-Za-z_$][\w$]*)\s*=\s*\"([^\"\\]*)\"\s*;")
_JS_GET_ELEMENT_BY_ID = re.compile(r"getElementById\(\s*([A-Za-z_$][\w$]*)\s*\)")
_JS_DATA_ATTRIBUTE = re.compile(r"[gs]etAttribute\(\s*\"(data-[a-z-]+)\"")
_JS_DELEGATION_SELECTOR = re.compile(r"closest\(\s*\"\[(data-[a-z-]+)\]\"\s*\)")
_JS_COPY_KIND = re.compile(r"kind\s*===\s*\"([a-z]+)\"")


def _js_string_constants(js: str) -> dict[str, str]:
    """``var NAME = "value";`` pairs, so a constant can be resolved by name.

    Resolving through the name rather than matching one hard-coded identifier is
    deliberate: renaming the *constant* is a harmless refactor and must not fail
    this suite, while changing its *value* is the break that must.
    """
    return {name: value for name, value in _JS_STRING_CONSTANT.findall(js)}


def _element_ids_the_script_looks_up(js: str) -> set[str]:
    """Ids passed to ``getElementById`` that resolve to a module constant.

    The per-button ids (``subjectId``, ``bodyId``) are read off attributes at
    runtime and cannot be resolved statically — they are covered instead by the
    attribute-name check below and by the rendering suite. What resolves here is
    the page-level id, which is the one written down twice.
    """
    constants = _js_string_constants(js)
    return {constants[name] for name in _JS_GET_ELEMENT_BY_ID.findall(js) if name in constants}


def _data_attributes_the_script_uses(js: str) -> set[str]:
    return set(_JS_DATA_ATTRIBUTE.findall(js))


def _delegation_attribute(js: str) -> str:
    found = _JS_DELEGATION_SELECTOR.findall(js)
    assert len(found) == 1, (
        f'expected exactly one delegated `closest("[data-...]")` selector, found {found}; '
        "a second copy mechanism is the thing this file exists to prevent"
    )
    return found[0]


def _copy_kinds_the_script_branches_on(js: str) -> set[str]:
    return set(_JS_COPY_KIND.findall(js))


# ---------------------------------------------------------------------------
# Reading the templates
# ---------------------------------------------------------------------------

_TEMPLATE_DATA_ATTRIBUTE = re.compile(r"\b(data-copy[a-z-]*)=")
_TEMPLATE_COPY_KIND = re.compile(r"data-copy=\"([a-z]+)\"")
_TEMPLATE_LIVE_REGION = re.compile(
    r"<[a-z]+\b[^>]*\bid=\"([^\"]+)\"[^>]*\baria-live=\"[^\"]*\"[^>]*>"
)


def _data_attributes_the_template_emits(html: str) -> set[str]:
    return set(_TEMPLATE_DATA_ATTRIBUTE.findall(html))


def _copy_kinds_the_template_emits(html: str) -> set[str]:
    return set(_TEMPLATE_COPY_KIND.findall(html))


def _live_region_ids(html: str) -> set[str]:
    return set(_TEMPLATE_LIVE_REGION.findall(html))


# ===========================================================================
# The contract
# ===========================================================================


def test_the_live_region_id_in_the_script_is_the_id_both_pages_render() -> None:
    """``announce()`` no-ops when it cannot find the region.

    That is the right runtime behaviour — a copy that worked must not throw
    because a page has no status area — and it is exactly what makes a renamed
    id invisible. The buttons would keep copying and stop telling a screen-reader
    user anything, and the rendering suite would stay green because the markup it
    checks is unchanged.
    """

    looked_up = _element_ids_the_script_looks_up(_read(SEQUENCE_JS))
    assert looked_up, "no module-level element id parsed from sequence.js — the reader is broken"

    for page in LIVE_REGION_PAGES:
        rendered = _live_region_ids(_read(page))
        assert rendered, f"{page.name} renders no element with aria-live — the reader is broken"
        assert rendered <= looked_up, (
            f"{page.name} renders live region id(s) {sorted(rendered - looked_up)} that "
            f"sequence.js never looks up (it looks up {sorted(looked_up)}); copy feedback "
            "would be silent"
        )


def test_the_delegation_selector_is_the_attribute_every_copy_button_carries() -> None:
    """The single point of failure for the whole feature.

    One listener on ``document`` matches ``target.closest("[data-copy]")`` and
    returns early when it finds nothing. Change that attribute name on the script
    side and *no button is ever handled* — no error, no console warning, three
    buttons that render perfectly and do nothing.
    """

    attribute = _delegation_attribute(_read(SEQUENCE_JS))
    markup = _read(SEQUENCE_PARTIAL)

    buttons = re.findall(r"<button\b[^>]*\bdata-copy=[^>]*>", markup)
    assert buttons, "no copy buttons parsed from _sequence.html — the reader is broken"
    for button in buttons:
        assert f"{attribute}=" in button, (
            f"a copy button does not carry `{attribute}`, the attribute sequence.js "
            f"delegates on, so clicking it is ignored: {button}"
        )


def test_every_data_attribute_the_script_reads_is_emitted_by_the_template() -> None:
    """The direction that catches a rename made only in the JavaScript.

    ``getAttribute`` returns ``null`` for a name nothing emits, and every read
    here has a fallback: the label degrades to the generic "Message", the kind
    resolves to ``null`` and the copy silently returns. None of it throws, so
    none of it surfaces without this assertion.
    """

    read_by_script = _data_attributes_the_script_uses(_read(SEQUENCE_JS))
    assert read_by_script, "no data- attributes parsed from sequence.js — the reader is broken"

    emitted = _data_attributes_the_template_emits(_read(SEQUENCE_PARTIAL))
    assert emitted, "no data- attributes parsed from _sequence.html — the reader is broken"

    orphaned = sorted(read_by_script - emitted - JS_OWNED_ATTRIBUTES)
    assert not orphaned, (
        f"sequence.js reads {orphaned}, which _sequence.html never emits. Either the "
        "template was not updated with the script, or the attribute is script-owned "
        f"scratch storage and belongs in JS_OWNED_ATTRIBUTES (currently "
        f"{sorted(JS_OWNED_ATTRIBUTES)})."
    )


def test_every_data_attribute_the_template_emits_is_read_by_the_script() -> None:
    """The same contract from the other side.

    An attribute in the markup that nothing reads is either dead weight or a
    rename that landed in the template and not in the handler. Both are worth a
    failing test: the second one breaks copying just as completely as the first
    direction does.
    """

    read_by_script = _data_attributes_the_script_uses(_read(SEQUENCE_JS))
    emitted = _data_attributes_the_template_emits(_read(SEQUENCE_PARTIAL))
    assert emitted, "no data- attributes parsed from _sequence.html — the reader is broken"

    unread = sorted(emitted - read_by_script)
    assert not unread, (
        f"_sequence.html emits {unread}, which sequence.js never reads; the markup and "
        "the handler have drifted apart"
    )


def test_the_copy_kinds_the_script_branches_on_are_the_kinds_the_template_emits() -> None:
    """``textFor()`` returns ``null`` for an unrecognised kind, and ``copy()``
    returns immediately on ``null``.

    So a kind the script does not know about is not an error — it is a button
    that does nothing, which is the failure mode this whole module is about. The
    two sets have to be equal rather than merely overlapping: a kind the script
    handles but no button requests is dead code, and a kind a button requests but
    the script does not handle is a dead button.
    """

    branched = _copy_kinds_the_script_branches_on(_read(SEQUENCE_JS))
    emitted = _copy_kinds_the_template_emits(_read(SEQUENCE_PARTIAL))

    assert branched, "no copy kinds parsed from sequence.js — the reader is broken"
    assert emitted, "no copy kinds parsed from _sequence.html — the reader is broken"
    assert branched == emitted, (
        f"sequence.js handles {sorted(branched)} but _sequence.html requests "
        f"{sorted(emitted)}; a button asking for a kind the handler does not know "
        "renders normally and copies nothing"
    )


def test_both_pages_load_the_one_shared_handler() -> None:
    """Anti-vacuity for everything above.

    Every assertion in this module compares one script against one partial. That
    comparison means nothing if a page has quietly stopped loading the script, or
    started loading a second one — the contract would hold for a file the browser
    never runs.
    """

    for page in LIVE_REGION_PAGES:
        markup = _read(page)
        sourced = re.findall(r"<script\b[^>]*\bsrc=\"([^\"]+)\"", markup)
        assert any(SEQUENCE_JS.name in src for src in sourced), (
            f"{page.name} renders a copy live region but does not load {SEQUENCE_JS.name}: "
            f"{sourced}"
        )

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

So this module reads the files as text and asserts the copies are equal. It runs
no browser, renders no template and asserts nothing about the clipboard: what it
pins is that the strings the script looks for are the strings the markup emits.

**Which surfaces it checks is derived, not listed.** The module used to name the
two pages that carried these controls. One of them, the Review queue, stopped
being a destination and its template was deleted — at which point the contract
failed on a missing path rather than on anything about copying, and the honest
repair was to stop writing the names down. The surfaces are now read out of the
templates themselves (:func:`_copy_surfaces`), with the sending desk partial
named as required coverage so that discovering nothing cannot pass for a green
build.

Both files are read with an explicit ``encoding="utf-8"``. The templates contain
em dashes and this repository is developed on a machine whose default encoding is
cp1252, where an implicit read raises ``UnicodeDecodeError``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The delegated handler. Note the path: it is served from the shared
#: ``app/web/static`` mount, not from a ``v2``-scoped one, and every surface that
#: offers a copy control loads this same file rather than each carrying a copy.
SEQUENCE_JS = REPO_ROOT / "app" / "web" / "static" / "sequence.js"

TEMPLATE_DIR = REPO_ROOT / "app" / "web" / "v2" / "templates"

#: The partial that renders the three per-message copy buttons.
SEQUENCE_PARTIAL = TEMPLATE_DIR / "_desk.html"

#: The surface this contract must never stop covering.
#:
#: Every other participant below is *discovered* — see :func:`_copy_surfaces` —
#: which is what keeps this module honest as the interface moves. Discovery
#: alone, though, is silently satisfiable by finding nothing: delete the copy
#: buttons everywhere and every assertion here passes over an empty set. So one
#: surface is named. The sending desk partial is the right one to name because
#: it is where a customer reads a prepared email and copies it — embedded in the
#: Campaign Overview — and it is the one surface the operating model says must
#: carry these controls.
#:
#: ``review.html`` and then ``contact.html`` used to be named here. Review/Emails
#: stopped being a destination, and the person page became a record rather than
#: an email surface; a contract that still pointed at either file would fail on
#: a missing path rather than on a broken contract, which is why the names moved
#: rather than the assertion.
REQUIRED_COPY_SURFACE = TEMPLATE_DIR / "_desk.html"

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


# ---------------------------------------------------------------------------
# Finding the surfaces, instead of listing them
# ---------------------------------------------------------------------------
#
# The original module hard-coded two page names. That was accurate while the
# interface had exactly two places to copy an email from, and it became a
# liability the moment one of them was retired: the contract failed because a
# path did not exist, which says nothing about whether copying still works.
#
# So the participants are read out of the templates. A *copy surface* is any
# template that renders the copy controls, plus every template that embeds one,
# transitively. That set is the thing the contract is actually about, it cannot
# drift from the templates because it is derived from them, and a new surface —
# the inline sending desk, say — joins it by existing rather than by someone
# remembering to add a name here.

#: ``{% include %}``, ``{% import %}`` and ``{% from %}`` — the three ways one
#: template's markup ends up inside another's output. ``{% extends %}`` is
#: deliberately absent: a child supplies blocks to its skeleton, so the skeleton
#: is not a surface that renders the child's buttons.
_TEMPLATE_EMBED = re.compile(r"{%-?\s*(?:include|import|from)\s+\"([^\"]+)\"")


def _embedded_by(html: str) -> set[str]:
    return set(_TEMPLATE_EMBED.findall(html))


def _copy_markup_templates() -> set[Path]:
    """Templates that emit at least one ``data-copy`` button."""

    return {
        path
        for path in sorted(TEMPLATE_DIR.glob("*.html"))
        if re.search(r"<button\b[^>]*\bdata-copy=", _read(path))
    }


def _embedders() -> dict[Path, set[Path]]:
    """For each template, the templates that include or import it."""

    parents: dict[Path, set[Path]] = {}
    for path in sorted(TEMPLATE_DIR.glob("*.html")):
        for name in _embedded_by(_read(path)):
            child = TEMPLATE_DIR / name
            if child.is_file():
                parents.setdefault(child, set()).add(path)
    return parents


def _copy_surfaces() -> set[Path]:
    """Every template that renders the copy controls, directly or by embedding."""

    parents = _embedders()
    surfaces = _copy_markup_templates()
    frontier = set(surfaces)
    while frontier:
        nxt: set[Path] = set()
        for path in frontier:
            for parent in parents.get(path, set()):
                if parent not in surfaces:
                    surfaces.add(parent)
                    nxt.add(parent)
        frontier = nxt
    return surfaces


def _loads_the_handler(html: str) -> bool:
    return any(
        SEQUENCE_JS.name in src for src in re.findall(r"<script\b[^>]*\bsrc=\"([^\"]+)\"", html)
    )


def _handler_reaches(path: Path, parents: dict[Path, set[Path]], seen: frozenset[Path]) -> bool:
    """Is ``sequence.js`` loaded on every rendering path that reaches ``path``?

    A partial does not have to load the script itself — it cannot, it has no
    ``<head>`` — but every page that embeds it must, or the buttons it renders
    are inert on that page. A template nobody embeds is a page, so it answers
    for itself.
    """

    html = _read(path)
    if _loads_the_handler(html):
        return True
    embedders = parents.get(path, set()) - seen
    if not embedders:
        return False
    return all(_handler_reaches(parent, parents, seen | {path}) for parent in embedders)


# ===========================================================================
# The contract
# ===========================================================================


def test_the_copy_surfaces_are_the_ones_this_contract_thinks_they_are() -> None:
    """Anti-vacuity for the discovery itself.

    Everything below quantifies over a computed set, and a computed set that
    came out empty makes every one of those assertions true. So the discovery
    is asserted first: the buttons still live in the desk partial, which is
    still a surface, and something still renders a live region.
    """

    markup = _copy_markup_templates()
    assert SEQUENCE_PARTIAL in markup, (
        f"no copy buttons parsed from {SEQUENCE_PARTIAL.name}; the reader is broken or the "
        "controls moved, and every assertion in this module is now quantifying over the "
        "wrong set"
    )

    surfaces = _copy_surfaces()
    assert REQUIRED_COPY_SURFACE in surfaces, (
        f"{REQUIRED_COPY_SURFACE.name} no longer renders the sequence copy controls "
        f"(surfaces found: {sorted(p.name for p in surfaces)}). The sending desk is "
        "required coverage; if it genuinely lost the controls, that is a product decision "
        "to argue for, not a set to quietly shrink."
    )

    with_regions = {path for path in surfaces if _live_region_ids(_read(path))}
    assert with_regions, (
        "no copy surface renders an aria-live region; copy feedback would be silent "
        "everywhere and the live-region assertion below would pass over nothing"
    )
    assert REQUIRED_COPY_SURFACE in with_regions, (
        f"{REQUIRED_COPY_SURFACE.name} renders copy controls but no aria-live region, so a "
        "screen-reader user is told nothing when a copy succeeds"
    )


def test_the_live_region_id_in_the_script_is_the_id_every_copy_surface_renders() -> None:
    """``announce()`` no-ops when it cannot find the region.

    That is the right runtime behaviour — a copy that worked must not throw
    because a page has no status area — and it is exactly what makes a renamed
    id invisible. The buttons would keep copying and stop telling a screen-reader
    user anything, and the rendering suite would stay green because the markup it
    checks is unchanged.
    """

    looked_up = _element_ids_the_script_looks_up(_read(SEQUENCE_JS))
    assert looked_up, "no module-level element id parsed from sequence.js — the reader is broken"

    for surface in sorted(_copy_surfaces()):
        rendered = _live_region_ids(_read(surface))
        # A surface that renders no region is covered by the discovery test
        # above, which requires at least one — and requires it of the Person
        # detail page by name. Here the question is only whether the ids that
        # *are* rendered are the ones the handler announces through.
        assert rendered <= looked_up, (
            f"{surface.name} renders live region id(s) {sorted(rendered - looked_up)} that "
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

    for template in sorted(_copy_markup_templates()):
        buttons = re.findall(r"<button\b[^>]*\bdata-copy=[^>]*>", _read(template))
        assert buttons, f"no copy buttons parsed from {template.name} — the reader is broken"
        for button in buttons:
            assert f"{attribute}=" in button, (
                f"a copy button in {template.name} does not carry `{attribute}`, the "
                f"attribute sequence.js delegates on, so clicking it is ignored: {button}"
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

    templates = sorted(_copy_markup_templates())
    emitted = {
        attribute
        for template in templates
        for attribute in _data_attributes_the_template_emits(_read(template))
    }
    assert emitted, "no data- attributes parsed from the copy markup — the reader is broken"

    orphaned = sorted(read_by_script - emitted - JS_OWNED_ATTRIBUTES)
    assert not orphaned, (
        f"sequence.js reads {orphaned}, which no copy template emits "
        f"({[t.name for t in templates]}). Either the markup was not updated with the "
        "script, or the attribute is script-owned scratch storage and belongs in "
        f"JS_OWNED_ATTRIBUTES (currently {sorted(JS_OWNED_ATTRIBUTES)})."
    )


def test_every_data_attribute_the_template_emits_is_read_by_the_script() -> None:
    """The same contract from the other side.

    An attribute in the markup that nothing reads is either dead weight or a
    rename that landed in the template and not in the handler. Both are worth a
    failing test: the second one breaks copying just as completely as the first
    direction does.
    """

    read_by_script = _data_attributes_the_script_uses(_read(SEQUENCE_JS))

    for template in sorted(_copy_markup_templates()):
        emitted = _data_attributes_the_template_emits(_read(template))
        assert emitted, f"no data- attributes parsed from {template.name} — the reader is broken"
        unread = sorted(emitted - read_by_script)
        assert not unread, (
            f"{template.name} emits {unread}, which sequence.js never reads; the markup and "
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
    templates = sorted(_copy_markup_templates())
    emitted = {
        kind for template in templates for kind in _copy_kinds_the_template_emits(_read(template))
    }

    assert branched, "no copy kinds parsed from sequence.js — the reader is broken"
    assert emitted, "no copy kinds parsed from the copy markup — the reader is broken"
    # Union across surfaces, not per surface. Today the desk offers one "Copy"
    # per email and the handler knows exactly that one kind; the subject and body
    # branches left with the person page's per-part buttons. What must not exist
    # is a kind nobody requests, or a request nobody handles.
    assert branched == emitted, (
        f"sequence.js handles {sorted(branched)} but {[t.name for t in templates]} request "
        f"{sorted(emitted)}; a button asking for a kind the handler does not know "
        "renders normally and copies nothing"
    )


def test_every_copy_surface_loads_the_one_shared_handler() -> None:
    """Anti-vacuity for everything above.

    Every assertion in this module compares one script against some markup. That
    comparison means nothing if a surface has quietly stopped loading the script
    — the contract would hold for a file the browser never runs, and the buttons
    would render perfectly and do nothing.

    A partial is answered for by the pages that embed it, since it has no
    ``<head>`` of its own; a template nobody embeds is a page and answers for
    itself.
    """

    parents = _embedders()
    for surface in sorted(_copy_surfaces()):
        assert _handler_reaches(surface, parents, frozenset()), (
            f"{surface.name} renders the copy controls but {SEQUENCE_JS.name} does not reach "
            "it: neither the template nor every page that embeds it loads the handler"
        )

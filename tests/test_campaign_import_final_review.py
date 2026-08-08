"""Regressions for the final independent review's two blockers.

Both were found by rendering the real Admin routes and by calling the real
parser — not by reading the code — and both had passed a green suite. So both
are covered here by matrices over the full input space the contract claims,
rather than by the one example each that happened to be tested before.

**Blocker 1** — four Admin surfaces rendered imported spreadsheet values without
the shared projection boundary: 64 of 64 hostile combinations leaked. The
permanent suite had route assertions for the customer pages and two Admin pages,
and none for the Admin contacts list, company detail, campaign detail or
failures inbox.

**Blocker 2** — the CSV reader accepted a quote that *begins* inside an unquoted
field. ``strict=True`` catches a bad transition after a quoted field, which is
the one shape the previous test exercised, and accepts ``A"da`` — so the product
silently rewrote the operator's bytes before storing them as immutable evidence,
while its own error message promised the opposite.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any

import pytest
from app.models.campaign import Campaign
from app.models.company import Company
from app.models.contact import Contact
from app.models.draft import DraftVersion
from app.services.imports import campaign_import, display, parsing
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests import apollo_factory as af

pytestmark = pytest.mark.usefixtures("enable_csv_import")


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    from app.core.config import get_settings
    from app.main import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__CSV_IMPORT", "true")
    monkeypatch.setenv("STAGED_UPLOADS_DIR", str(tmp_path / "staged"))
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


# ===========================================================================
# Blocker 1 — the Admin projection boundary
# ===========================================================================

#: The four characters a spreadsheet application reads as the start of an
#: expression.
PREFIXES: tuple[str, ...] = ("=", "+", "-", "@")

#: Leading forms that a spreadsheet strips before deciding, so each of them
#: still opens a formula. NBSP is included because it is whitespace to Python's
#: ``strip`` and invisible to an operator.
LEADS: tuple[str, ...] = ("", " ", "\t", " ")

#: The four surfaces the final review reproduced, plus the neighbours that share
#: their readers. Listed by name so a failure says which page leaked.
ADMIN_SURFACES: tuple[str, ...] = (
    "admin_contacts_list",
    "admin_company_detail",
    "admin_campaign_detail",
    "admin_failures",
)


def _hostile_row(prefix: str, lead: str) -> dict[str, str]:
    """One row whose every operator-visible field opens a formula."""

    marker = f"{lead}{prefix}cmd|"
    return af.row(
        **{
            "First Name": f"{marker}first",
            "Last Name": f"{marker}last",
            "Title": f"{marker}title",
            "Company Name": f"{marker}company",
            "Company Name for Emails": f"{marker}coemails",
            "Website": "https://engines.example",
            "City": f"{marker}city",
            "State": f"{marker}state",
            "Country": f"{marker}country",
            "Industry": f"{marker}industry",
            "Departments": f"{marker}dept",
            "Seniority": f"{marker}seniority",
            "Email Status": f"{marker}status",
            "Primary Email Source": f"{marker}source",
            "Primary Email Verification Source": f"{marker}vsource",
            "Primary Email Catch-all Status": f"{marker}catchall",
            # ``@`` cannot lead a valid local part, so that prefix is exercised
            # on every other field and the address stays well-formed.
            "Email": (
                "plain@engines.example" if prefix == "@" else f"{prefix}cmd|x@engines.example"
            ),
        }
    )


def _live_formulas(body: str, needle: str) -> list[str]:
    """Rendered text nodes that begin with the formula under test.

    Splits on tags so markup and CSS are not mistaken for content, and looks for
    the exact prefix being exercised rather than one fixed shape — searching
    only for ``=cmd|`` is why three of the four prefixes went unproven.
    """

    found: list[str] = []
    for chunk in re.split(r"<[^>]*>", body):
        for line in chunk.splitlines():
            stripped = line.strip()
            if stripped.startswith(needle):
                found.append(stripped[:60])
    return found


@pytest.fixture()
def hostile_admin_state(committed_session: Session) -> Any:
    """Imported data with a formula in every field, plus a refused row.

    Built once per prefix/lead pair by the tests that need it. The refused row
    exists so the failures inbox has something to render.
    """

    def _build(prefix: str, lead: str) -> dict[str, Any]:
        campaign = af.make_campaign(committed_session, name=f"{lead}{prefix}cmd|campaign")
        committed_session.commit()
        batch = campaign_import.confirm(
            committed_session,
            campaign_id=campaign.id,
            content=af.csv_bytes([_hostile_row(prefix, lead)]),
            filename=f"{lead}{prefix}cmd|file.csv",
        )
        campaign_import.confirm(
            committed_session,
            campaign_id=campaign.id,
            content=af.csv_bytes(
                [
                    _hostile_row(prefix, lead),
                    af.row(**{"Email": "not-an-address", "First Name": f"{lead}{prefix}cmd|bad"}),
                ]
            ),
            filename=f"{lead}{prefix}cmd|file2.csv",
        )
        committed_session.commit()
        contact = committed_session.scalars(select(Contact)).first()
        # A draft awaiting review, so /admin/review has a row at all. Without one
        # that page renders an empty queue and every absence assertion on it is
        # vacuous — which is exactly what an independent re-review found.
        if contact is not None:
            committed_session.add(
                DraftVersion(
                    contact_id=contact.id,
                    campaign_id=campaign.id,
                    version_number=1,
                    subject=f"{lead}{prefix}cmd|subject",
                    body="body",
                )
            )
            committed_session.commit()
        return {
            "campaign": campaign,
            "batch_id": batch.batch_id,
            "contact": contact,
            "company": committed_session.scalars(select(Company)).first(),
        }

    return _build


def _urls(state: dict[str, Any]) -> dict[str, str]:
    campaign: Campaign = state["campaign"]
    company: Company | None = state["company"]
    contact: Contact | None = state["contact"]
    urls = {
        "admin_contacts_list": "/admin/contacts",
        "admin_campaign_detail": f"/admin/campaigns/{campaign.id}",
        "admin_failures": "/admin/failures",
        "admin_import_batch": f"/admin/imports/{state['batch_id']}",
        "admin_campaigns_list": "/admin/campaigns",
        "admin_companies_list": "/admin/companies",
        "admin_overview": "/admin",
        "admin_review": "/admin/review",
    }
    if company is not None:
        urls["admin_company_detail"] = f"/admin/companies/{company.id}"
    if contact is not None:
        urls["admin_contact_detail"] = f"/admin/contacts/{contact.id}"
    return urls


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("lead", LEADS)
def test_the_four_reproduced_admin_surfaces_render_no_live_formula(
    client: Any, hostile_admin_state: Any, prefix: str, lead: str
) -> None:
    """The exact 4 x 4 x 4 matrix the final review reproduced, all 64 cases."""

    state = hostile_admin_state(prefix, lead)
    urls = _urls(state)
    needle = f"{prefix}cmd|"
    for surface in ADMIN_SURFACES:
        assert surface in urls, f"{surface} could not be built for this fixture"
        body = client.get(urls[surface]).text
        leaks = _live_formulas(body, needle)
        assert not leaks, f"{surface} leaked {prefix!r} after {lead!r}: {leaks[:3]}"


def _reachable_markers(body: str, needle: str) -> int:
    """How many times the hostile value appears anywhere in the rendered page.

    Counted on the whole body, neutralized or not, because this answers a
    different question from :func:`_live_formulas`: not "is it inert" but "did
    it get here at all". An absence assertion on a page the fixture cannot
    reach is not evidence of anything.
    """

    return body.count(needle.lstrip())


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("lead", LEADS)
def test_every_admin_surface_shows_the_hostile_value_and_shows_it_inert(
    client: Any, hostile_admin_state: Any, prefix: str, lead: str
) -> None:
    """Reachability first, inertness second — on every page, every combination.

    The previous version swept the same URLs asserting only absence, and one of
    them (``/admin/review``) had no hostile value on it at all: the fixture
    seeded no draft, so the review queue was empty and the assertion passed on
    a blank page. Two others were reachable only by accident of page-size
    ordering. This fixture creates exactly one Campaign, one Contact, one
    Company and one draft, so first-page ordering is not a question, and each
    page must **prove** the value arrived before it is credited with making it
    inert.
    """

    state = hostile_admin_state(prefix, lead)
    needle = f"{prefix}cmd|"
    for surface, url in _urls(state).items():
        response = client.get(url)
        assert response.status_code == 200, f"{surface} returned {response.status_code}"
        body = response.text
        assert _reachable_markers(body, needle) > 0, (
            f"{surface} rendered no hostile value for {prefix!r} after {lead!r}: "
            "its absence assertion would be vacuous"
        )
        leaks = _live_formulas(body, needle)
        assert not leaks, f"{surface} leaked {prefix!r} after {lead!r}: {leaks[:3]}"


def test_the_admin_sweep_covers_every_page_the_workbench_serves(
    client: Any, hostile_admin_state: Any
) -> None:
    """The sweep's URL list is the contract, so it is asserted rather than
    assumed — a page added later is not silently left out of the matrix."""

    state = hostile_admin_state("=", "")
    covered = set(_urls(state))
    assert covered == {
        "admin_contacts_list",
        "admin_company_detail",
        "admin_campaign_detail",
        "admin_failures",
        "admin_import_batch",
        "admin_contact_detail",
        "admin_campaigns_list",
        "admin_companies_list",
        "admin_overview",
        "admin_review",
    }
    assert set(ADMIN_SURFACES) <= covered


def test_the_review_page_really_carries_imported_text(
    client: Any, hostile_admin_state: Any
) -> None:
    """The page the re-review found unreachable, proven reachable on its own.

    ``/admin/review`` renders a draft queue: the campaign name, the contact
    label and the draft subject. All three can carry text that came from an
    imported file, so the page belongs in the matrix — it just needed a queue
    row to render.
    """

    state = hostile_admin_state("=", "")
    body = client.get(_urls(state)["admin_review"]).text
    assert "cmd|" in body
    # The contact label on that row is the imported name.
    contact: Contact | None = state["contact"]
    assert contact is not None and contact.first_name.endswith("cmd|first")
    assert "cmd|first" in body
    assert not _live_formulas(body, "=cmd|")


def test_the_hostile_values_are_actually_present_on_the_pages(
    client: Any, hostile_admin_state: Any
) -> None:
    """A per-page reachability census, reported by name.

    Kept as its own test so a page that stops carrying hostile values fails
    here — saying which page, and how many markers it lost — rather than being
    quietly re-credited as protected.
    """

    state = hostile_admin_state("=", "")
    census = {
        surface: _reachable_markers(client.get(url).text, "=cmd|")
        for surface, url in _urls(state).items()
    }
    unreachable = sorted(name for name, count in census.items() if count == 0)
    assert not unreachable, f"no hostile value reached: {unreachable} (census: {census})"


# ---------------------------------------------------------------------------
# Non-vacuity: the matrix above must turn red for the regression it guards.
# ---------------------------------------------------------------------------


@pytest.fixture()
def disabled_boundary(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Turn the projection boundary into a pass-through, in-process only.

    The boundary is applied in **two** places, and a mutation that removed only
    one would be as misleading as the vacuous assertions this pass exists to
    fix. Templates call it through the ``neutralize`` filter; the Admin lineage
    reader calls ``display.safe_optional`` before the template ever sees the
    value. Writing this fixture the first way — filters only — showed the import
    batch page as still-clean and correctly failed, which is how the second
    application came to be covered here.

    Both are replaced: the module attributes, because the reader looks them up
    at call time, and the four filter registries, because those hold a direct
    reference to the original function object. No repository file is touched and
    ``monkeypatch`` restores everything.
    """

    from app.services.admin_workbench import import_lineage
    from app.web.admin_workbench import templates as admin_templates
    from app.web.company_intelligence import templates as ci_templates
    from app.web.routes import templates as legacy_templates
    from app.web.v2.routes import templates as v2_templates

    def _passthrough(value: Any) -> str:
        return "" if value is None else str(value)

    def _passthrough_optional(value: str | None) -> str | None:
        return value

    monkeypatch.setattr(display, "safe_text", _passthrough)
    monkeypatch.setattr(display, "safe_optional", _passthrough_optional)
    monkeypatch.setattr(import_lineage, "_safe", _passthrough_optional)
    for templates in (admin_templates, v2_templates, legacy_templates, ci_templates):
        monkeypatch.setitem(templates.env.filters, "neutralize", _passthrough)
    return _passthrough


@pytest.mark.parametrize("prefix", PREFIXES)
def test_the_matrix_would_fail_if_the_boundary_were_removed(
    client: Any, hostile_admin_state: Any, disabled_boundary: Any, prefix: str
) -> None:
    """The mutation proof for Blocker 1.

    With the boundary neutered, EVERY page in the sweep must produce a live
    formula — otherwise the corresponding assertion in
    ``test_every_admin_surface_shows_the_hostile_value_and_shows_it_inert``
    could pass for a reason other than the projection working, and would not
    stop the regression it exists for.
    """

    state = hostile_admin_state(prefix, "")
    needle = f"{prefix}cmd|"
    unprotected: list[str] = []
    for surface, url in _urls(state).items():
        body = client.get(url).text
        if not _live_formulas(body, needle):
            unprotected.append(surface)
    assert not unprotected, (
        "with the boundary disabled these pages still showed no live formula, so "
        f"their absence assertion proves nothing: {unprotected}"
    )


def test_the_detector_itself_recognizes_a_live_formula() -> None:
    """And the detector is not the thing that is broken.

    ``_live_formulas`` is the instrument every absence assertion depends on. If
    it silently matched nothing the whole matrix would be green and empty.
    """

    for prefix in PREFIXES:
        rendered = f"<td>{prefix}cmd|first</td>"
        assert _live_formulas(rendered, f"{prefix}cmd|") == [f"{prefix}cmd|first"]
        neutralized = f"<td>&#39;{prefix}cmd|first</td>"
        assert _live_formulas(neutralized, f"{prefix}cmd|") == []
    # A tag or an attribute containing the text is not a rendered value.
    assert _live_formulas('<a title="=cmd|x">ok</a>', "=cmd|") == []


def test_the_no_company_branch_neutralizes_the_label_not_the_fallback(
    client: Any, committed_session: Session
) -> None:
    """The Jinja precedence bug the review named specifically.

    ``row.company_label or "—" | neutralize`` binds the filter to the fallback,
    so a present label goes out untouched and the em dash is neutralized
    instead. The fix has to neutralize whichever value is actually chosen.
    """

    source = Path("app/web/templates/admin/contacts.html").read_text()
    assert 'company_label or "—" | neutralize' not in source
    assert 'or "—" | neutralize' not in source

    campaign = af.make_campaign(committed_session)
    committed_session.commit()
    campaign_import.confirm(
        committed_session,
        campaign_id=campaign.id,
        content=af.csv_bytes([_hostile_row("=", "")]),
        filename="hostile.csv",
    )
    committed_session.commit()
    body = client.get("/admin/contacts").text
    assert "cmd|company" in body
    assert not _live_formulas(body, "=cmd|")


def test_no_admin_template_uses_a_second_neutralizer() -> None:
    """One boundary. A local copy would drift, which is the whole failure mode."""

    for path in sorted(Path("app/web/templates/admin").glob("*.html")):
        source = path.read_text()
        assert "safe_text" not in source, path.name
        assert "neutralize_formula" not in source, path.name


def test_the_durable_admin_evidence_is_unchanged_by_the_projection(
    committed_session: Session, client: Any
) -> None:
    """Neutralization is a projection; the record of what the file said stays
    exactly what the file said."""

    from app.models.import_batch import ImportRow

    campaign = af.make_campaign(committed_session)
    committed_session.commit()
    campaign_import.confirm(
        committed_session,
        campaign_id=campaign.id,
        content=af.csv_bytes([_hostile_row("=", "")]),
        filename="hostile.csv",
    )
    committed_session.commit()
    client.get("/admin/contacts")
    row = committed_session.scalars(select(ImportRow)).first()
    assert row is not None
    assert row.raw_data["First Name"] == "=cmd|first"
    assert row.raw_data["Company Name"] == "=cmd|company"


# ===========================================================================
# Blocker 2 — a quote that begins inside an unquoted field
# ===========================================================================

HEADER = "First Name,Last Name,Company Name,Email\n"


def _one_row(first_name_cell: str) -> bytes:
    return (HEADER + f"{first_name_cell},Lovelace,Engines,ada@engines.example\n").encode("utf-8")


#: The exact matrix from the final review, with the two illegal unquoted-field
#: forms that the previous suite never exercised.
ILLEGAL_QUOTING: tuple[tuple[str, str], ...] = (
    ("closing quote followed by more text", '"A"da'),
    ("unterminated quoted field", '"A'),
    ("quote opening inside an unquoted field", 'A"da'),
    ("quote pair inside an unquoted field", 'A"da"'),
)

VALID_QUOTING: tuple[tuple[str, str, str], ...] = (
    ("quoted delimiter", '"Ada, the first"', "Ada, the first"),
    ("doubled quotes", '"Love ""Lace"""', 'Love "Lace"'),
    ("plain quoted field", '"Ada"', "Ada"),
    ("no quotes at all", "Ada", "Ada"),
    ("quoted newline", '"Ada\nthe first"', "Ada\nthe first"),
)


@pytest.mark.parametrize(("label", "cell"), ILLEGAL_QUOTING)
def test_illegal_quoting_is_refused_by_the_parser(label: str, cell: str) -> None:
    with pytest.raises(parsing.MalformedFileError):
        parsing.parse_file(_one_row(cell), "q.csv")


@pytest.mark.parametrize(("label", "cell"), ILLEGAL_QUOTING)
def test_illegal_quoting_reaches_the_typed_import_error(label: str, cell: str) -> None:
    """The route catches ``CampaignImportError`` and nothing else, so the
    refusal has to arrive as one rather than as a bare 500."""

    with pytest.raises(campaign_import.UnreadableFileError):
        campaign_import.inspect(_one_row(cell), "q.csv")


@pytest.mark.parametrize(("label", "cell"), ILLEGAL_QUOTING)
def test_illegal_quoting_is_refused_at_the_route(
    label: str, cell: str, client: Any, committed_session: Session
) -> None:
    from app.models.import_batch import ImportBatch

    campaign = af.make_campaign(committed_session)
    committed_session.commit()
    response = client.post(
        f"/app/campaigns/{campaign.id}/imports",
        files={"file": ("q.csv", _one_row(cell), "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert "err=" in response.headers["location"], label
    # And nothing durable was written for a file that was never readable.
    assert committed_session.scalars(select(ImportBatch)).first() is None


@pytest.mark.parametrize(("label", "cell", "expected"), VALID_QUOTING)
def test_valid_quoting_still_parses(label: str, cell: str, expected: str) -> None:
    parsed = parsing.parse_file(_one_row(cell), "ok.csv")
    assert parsed.rows[0].cells[0] == expected, label


@pytest.mark.parametrize(("label", "cell", "expected"), VALID_QUOTING)
def test_valid_quoting_still_imports(
    label: str, cell: str, expected: str, db_session: Session
) -> None:
    campaign = af.make_campaign(db_session)
    result = campaign_import.confirm(
        db_session, campaign_id=campaign.id, content=_one_row(cell), filename="ok.csv"
    )
    assert result.imported == 1, label
    contact = db_session.scalars(select(Contact)).one()
    assert contact.first_name == " ".join(expected.split())


def test_a_quote_is_legal_in_any_field_position_of_a_valid_row() -> None:
    """The validator must not be positional — a quoted third column is as legal
    as a quoted first one."""

    content = (HEADER + 'Ada,Lovelace,"Engines, Ltd","ada@engines.example"\n').encode("utf-8")
    parsed = parsing.parse_file(content, "ok.csv")
    assert parsed.rows[0].cells[2] == "Engines, Ltd"


def test_a_quote_inside_a_later_column_is_still_refused() -> None:
    content = (HEADER + 'Ada,Lovelace,Engi"nes,ada@engines.example\n').encode("utf-8")
    with pytest.raises(parsing.MalformedFileError):
        parsing.parse_file(content, "q.csv")


def test_the_validator_is_a_state_walk_not_a_second_parser() -> None:
    """Small, deterministic, and it only ever looks at quote characters.

    Asserted so a later 'improvement' into a hand-rolled CSV parser has to be a
    deliberate decision rather than a drift.
    """

    import inspect as _inspect

    source = _inspect.getsource(parsing.validate_csv_quoting)
    assert "csv." not in source
    assert source.count("while") <= 1
    assert len(source.splitlines()) < 80


def test_a_file_with_no_quotes_short_circuits() -> None:
    """An ordinary export pays nothing for this check."""

    assert parsing.validate_csv_quoting("a,b,c\n1,2,3\n") is None


def test_the_quoting_message_is_the_one_the_product_promises() -> None:
    """The message already claimed a quotation mark in the middle of an unquoted
    value is an error. Until now that was not true."""

    with pytest.raises(parsing.MalformedFileError) as excinfo:
        parsing.parse_file(_one_row('A"da'), "q.csv")
    assert "quoting error" in str(excinfo.value)
    assert "middle of an unquoted value" in str(excinfo.value)


def test_the_two_csv_failures_remain_distinguishable() -> None:
    limit = parsing._csv_error_message(Exception("field larger than field limit (131072)"))
    quoting = parsing._csv_error_message(Exception("',' expected after '\"'"))
    assert "cell larger than" in limit
    assert "quoting error" in quoting


def test_quoting_validation_precedes_the_reader() -> None:
    """Refused before any row is built, so nothing malformed is materialized."""

    sentinel = io.StringIO(HEADER + 'A"da,L,E,a@x.example\n')

    class _Boom(io.StringIO):
        def readline(self, *args: Any, **kwargs: Any) -> str:  # pragma: no cover
            raise AssertionError("the reader ran before quoting was validated")

    original = csv.reader
    try:
        csv.reader = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[assignment]
            AssertionError("the reader was constructed before quoting was validated")
        )
        with pytest.raises(parsing.MalformedFileError):
            parsing.parse_file(sentinel.getvalue().encode("utf-8"), "q.csv")
    finally:
        csv.reader = original  # type: ignore[assignment]
    assert _Boom is not None


def test_the_shared_boundary_has_no_second_implementation() -> None:
    """SR-IMP-002's rule, asserted rather than assumed."""

    assert display.safe_text.__module__ == "app.services.imports.display"
    source = Path("app/services/imports/display.py").read_text()
    assert source.count("def safe_text") == 1

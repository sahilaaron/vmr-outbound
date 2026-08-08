"""Regressions for the second independent adversarial review (SR-IMP-001..008).

The first repair pass was reviewed again and four areas still failed live
attacks. Each is covered here by the attack that found it, asserting the
corrected contract rather than the defect.

The organising lesson of that review, and the reason several of these tests are
matrices rather than examples: a green suite is only worth what its assertions
cover. The previous pass had a Unicode test that used the same literal twice, a
formula test that searched for one prefix on two surfaces, and a restatement
test that asserted a value existed *somewhere*. All three passed while the
product was broken.
"""

from __future__ import annotations

import csv
import io
import unicodedata
from pathlib import Path
from typing import Any

import pytest
from app.models.contact import Contact
from app.models.enums import (
    ImportedEmailSlot,
    ImportedEmailStageOutcome,
    ImportedVerificationOutcome,
)
from app.models.import_batch import ImportBatch, ImportRow, ImportRowValidation
from app.models.imported_email import ImportedContactEmail
from app.services.admin_workbench.import_lineage import ImportLineageReader
from app.services.imports import apollo, campaign_import, display, parsing
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
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


def _confirm(session: Session, campaign: Any, rows: list[dict[str, str]], name: str) -> Any:
    return campaign_import.confirm(
        session, campaign_id=campaign.id, content=af.csv_bytes(rows), filename=name
    )


def _csv_bytes(header: tuple[str, ...], *rows: list[str]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(list(header))
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


def _primaries(session: Session) -> list[ImportedContactEmail]:
    return list(
        session.scalars(
            select(ImportedContactEmail)
            .where(ImportedContactEmail.slot == ImportedEmailSlot.PRIMARY)
            .order_by(ImportedContactEmail.source_row_number)
        ).all()
    )


def _accepted(records: list[ImportedContactEmail]) -> list[ImportedContactEmail]:
    return [
        record
        for record in records
        if record.email_stage_outcome is ImportedEmailStageOutcome.IMPORTED_EMAIL_ACCEPTED
    ]


def _retained(records: list[ImportedContactEmail]) -> list[ImportedContactEmail]:
    return [
        record
        for record in records
        if record.email_stage_outcome is None
        and record.rejection_code == campaign_import.RESTATED_CODE
    ]


# ===========================================================================
# SR-IMP-001 — every materially changed statement survives
# ===========================================================================

#: One column per durable provider claim, so the matrix is the contract rather
#: than a sample of it. The previous pass changed the status only, and every
#: other claim was silently discarded at commit.
CLAIM_CHANGES: tuple[tuple[str, str, str], ...] = (
    ("Email Status", "invalid", "provider_status_normalized"),
    ("Primary Email Last Verified At", "2026-07-04T09:30:00Z", "provider_last_verified_raw"),
    ("Primary Email Source", "Another Provider", "provider_source"),
    ("Primary Email Verification Source", "Another Verification", "provider_verification_source"),
    ("Primary Email Catch-all Status", "Catch-all", "provider_catch_all_normalized"),
)


@pytest.mark.parametrize(("column", "value", "attribute"), CLAIM_CHANGES)
def test_a_changed_provider_claim_is_retained_as_its_own_statement(
    db_session: Session, column: str, value: str, attribute: str
) -> None:
    campaign = af.make_campaign(db_session)
    _confirm(db_session, campaign, [af.row()], "original.csv")
    _confirm(db_session, campaign, [af.row(**{column: value})], "corrected.csv")

    records = _primaries(db_session)
    accepted = _accepted(records)
    retained = _retained(records)

    assert len(accepted) == 1, f"{column}: exactly one address stays in use"
    assert len(retained) == 1, f"{column}: the correction is on record"
    assert getattr(retained[0], attribute) is not None
    assert getattr(retained[0], attribute) != getattr(accepted[0], attribute)


@pytest.mark.parametrize(("column", "value", "attribute"), CLAIM_CHANGES)
def test_a_retained_correction_never_becomes_provider_verification(
    db_session: Session, column: str, value: str, attribute: str
) -> None:
    from app.models.email_evidence import ExactEmailVerification

    campaign = af.make_campaign(db_session)
    _confirm(db_session, campaign, [af.row()], "original.csv")
    _confirm(db_session, campaign, [af.row(**{column: value})], "corrected.csv")

    retained = _retained(_primaries(db_session))
    assert len(retained) == 1
    assert retained[0].email_stage_outcome is None
    assert retained[0].verification_stage_outcome is None
    assert db_session.scalar(select(func.count()).select_from(ExactEmailVerification)) == 0


@pytest.mark.parametrize(("column", "value", "attribute"), CLAIM_CHANGES)
def test_a_retained_correction_is_visible_from_its_own_batch(
    db_session: Session, column: str, value: str, attribute: str
) -> None:
    """The corrected upload must not look as though it contributed nothing."""

    campaign = af.make_campaign(db_session)
    _confirm(db_session, campaign, [af.row()], "original.csv")
    corrected = _confirm(db_session, campaign, [af.row(**{column: value})], "corrected.csv")
    db_session.flush()

    rows, _total = ImportLineageReader(db_session).batch_rows(corrected.batch_id)
    assert len(rows) == 1
    assert rows[0].primary_address is not None
    assert rows[0].primary_address.accepted is False
    retained = _retained(_primaries(db_session))
    assert rows[0].primary_address.email == retained[0].normalized_email


@pytest.mark.parametrize(("column", "value", "attribute"), CLAIM_CHANGES)
def test_re_importing_the_correction_adds_nothing(
    db_session: Session, column: str, value: str, attribute: str
) -> None:
    campaign = af.make_campaign(db_session)
    _confirm(db_session, campaign, [af.row()], "original.csv")
    _confirm(db_session, campaign, [af.row(**{column: value})], "corrected.csv")
    before = len(_primaries(db_session))

    # The same correction again, as a different file so the batch is not reused.
    _confirm(
        db_session,
        campaign,
        [af.row(**{column: value}), af.row(**{"Email": "grace@engines.example"})],
        "corrected-again.csv",
    )
    after = _primaries(db_session)
    assert len(_accepted(after)) == 2  # Ada's, unchanged, plus Grace's
    # Ada gained no second copy of a statement already on record.
    ada = [record for record in after if record.normalized_email == "ada@engines.example"]
    assert len(ada) == before


def test_two_different_corrections_are_two_retained_statements(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    _confirm(db_session, campaign, [af.row()], "original.csv")
    _confirm(db_session, campaign, [af.row(**{"Email Status": "invalid"})], "first.csv")
    _confirm(db_session, campaign, [af.row(**{"Primary Email Source": "Elsewhere"})], "second.csv")

    records = _primaries(db_session)
    assert len(_accepted(records)) == 1
    assert len(_retained(records)) == 2


def test_the_active_address_is_never_swapped_by_an_upload(db_session: Session) -> None:
    """A newer file restating a person cannot change which address is in use."""

    campaign = af.make_campaign(db_session)
    _confirm(db_session, campaign, [af.row()], "original.csv")
    before = campaign_import.accepted_primary_email(
        db_session,
        campaign_id=campaign.id,
        contact_id=db_session.scalars(select(Contact.id)).one(),
    )
    assert before is not None

    _confirm(db_session, campaign, [af.row(**{"Email Status": "invalid"})], "corrected.csv")
    after = campaign_import.accepted_primary_email(
        db_session,
        campaign_id=campaign.id,
        contact_id=db_session.scalars(select(Contact.id)).one(),
    )
    assert after is not None
    assert after.id == before.id
    assert after.normalized_email == before.normalized_email


def test_a_changed_address_for_the_same_person_is_held_not_swapped(
    db_session: Session,
) -> None:
    """The explicit changed-address disposition.

    A second file giving the same person a different address is a conflict an
    operator decides, so the row is held for review, the active address is
    untouched, and the supplied address is retained as neutral evidence — not
    labelled as refused, because nothing is known to be wrong with it.
    """

    campaign = af.make_campaign(db_session)
    _confirm(db_session, campaign, [af.row()], "original.csv")
    result = _confirm(
        db_session,
        campaign,
        [
            af.row(
                **{
                    "Email": "ada.new@engines.example",
                    # The same person by the file's own exact signals, so this is
                    # a changed address rather than a different human being.
                    "Apollo Contact Id": "apollo-contact-ada",
                    "Person Linkedin Url": "https://www.linkedin.com/in/ada",
                }
            )
        ],
        "changed-address.csv",
    )
    assert result.review_required == 1

    records = _primaries(db_session)
    accepted = _accepted(records)
    assert len(accepted) == 1
    assert accepted[0].normalized_email == "ada@engines.example"

    supplied = [r for r in records if r.normalized_email == "ada.new@engines.example"]
    assert len(supplied) == 1
    assert supplied[0].email_stage_outcome is None
    assert supplied[0].verification_stage_outcome is None
    assert supplied[0].rejection_code == campaign_import.HELD_CODE


#: Every column of ``imported_contact_emails`` that IS part of what the vendor
#: stated. Written out in full and asserted as an exact set, not a sample: the
#: digest is built by exclusion, so a column added to the model later joins the
#: statement automatically and nothing would notice unless this list is pinned.
#: A new column arriving here should be a deliberate edit with a reason.
STATEMENT_CLAIM_COLUMNS: frozenset[str] = frozenset(
    {
        "slot",
        "raw_email",
        "normalized_email",
        "source_schema",
        "provider_source",
        "provider_status_raw",
        "provider_status_normalized",
        "provider_verification_source",
        "provider_catch_all_raw",
        "provider_catch_all_normalized",
        "provider_last_verified_at",
        "provider_last_verified_raw",
    }
)

#: Everything else: where the statement came from, and what VMR did with it.
STATEMENT_PROVENANCE_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "import_batch_id",
        "import_row_id",
        "campaign_id",
        "contact_id",
        "source_row_number",
        "source_sheet_name",
        "source_file_checksum",
        "row_fingerprint",
        "email_stage_outcome",
        "verification_stage_outcome",
        "rejection_code",
        "created_at",
    }
)


def test_the_statement_digest_partitions_the_model_exactly() -> None:
    """The exact partition, both directions, with nothing unaccounted for."""

    columns = {column.name for column in ImportedContactEmail.__table__.columns}
    assert STATEMENT_CLAIM_COLUMNS | STATEMENT_PROVENANCE_COLUMNS == columns, (
        "a column was added to imported_contact_emails without being classified "
        "as a claim or as provenance; the digest includes it by default"
    )
    assert not (STATEMENT_CLAIM_COLUMNS & STATEMENT_PROVENANCE_COLUMNS)
    assert campaign_import._STATEMENT_EXCLUDED_COLUMNS == STATEMENT_PROVENANCE_COLUMNS

    included = columns - campaign_import._STATEMENT_EXCLUDED_COLUMNS
    assert included == STATEMENT_CLAIM_COLUMNS
    assert len(STATEMENT_CLAIM_COLUMNS) == 12
    assert len(STATEMENT_PROVENANCE_COLUMNS) == 13


def _statement_probe(db_session: Session) -> ImportedContactEmail:
    campaign = af.make_campaign(db_session)
    _confirm(db_session, campaign, [af.row()], "probe.csv")
    return db_session.scalars(select(ImportedContactEmail)).first()  # type: ignore[return-value]


_MUTATIONS: dict[str, Any] = {
    "slot": ImportedEmailSlot.SECONDARY,
    "raw_email": "mutated@engines.example",
    "normalized_email": "mutated@engines.example",
    "source_schema": "mutated_schema_v9",
    "provider_source": "Mutated Source",
    "provider_status_raw": "Mutated",
    "provider_status_normalized": "mutated",
    "provider_verification_source": "Mutated Verification",
    "provider_catch_all_raw": "Mutated Catch-all",
    "provider_catch_all_normalized": "mutated catch-all",
    "provider_last_verified_raw": "mutated timestamp",
}


@pytest.mark.parametrize("column", sorted(_MUTATIONS))
def test_every_claim_column_changes_the_digest(db_session: Session, column: str) -> None:
    record = _statement_probe(db_session)
    before = campaign_import.statement_digest(record)
    setattr(record, column, _MUTATIONS[column])
    assert campaign_import.statement_digest(record) != before, column


def test_the_datetime_claim_column_changes_the_digest(db_session: Session) -> None:
    from datetime import UTC, datetime

    record = _statement_probe(db_session)
    before = campaign_import.statement_digest(record)
    record.provider_last_verified_at = datetime(2031, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert campaign_import.statement_digest(record) != before


@pytest.mark.parametrize(
    "column",
    sorted(
        STATEMENT_PROVENANCE_COLUMNS
        - {"id", "created_at", "campaign_id", "import_batch_id", "import_row_id", "contact_id"}
    ),
)
def test_no_provenance_column_changes_the_digest(db_session: Session, column: str) -> None:
    record = _statement_probe(db_session)
    before = campaign_import.statement_digest(record)
    replacements: dict[str, Any] = {
        "source_row_number": 4242,
        "source_sheet_name": "Another Sheet",
        "source_file_checksum": "0" * 64,
        "row_fingerprint": "f" * 64,
        "email_stage_outcome": None,
        "verification_stage_outcome": None,
        "rejection_code": "some_code",
    }
    setattr(record, column, replacements[column])
    assert campaign_import.statement_digest(record) == before, column


# ===========================================================================
# SR-IMP-002 — one neutralization boundary, every prefix, every surface
# ===========================================================================

#: Excel, Sheets and LibreOffice all treat these as the start of an expression.
FORMULA_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@")
#: Leading whitespace a spreadsheet strips before deciding. NBSP included: it is
#: whitespace to Python's ``strip`` and invisible to an operator.
LEADING_WHITESPACE: tuple[str, ...] = ("", " ", "\t", " ", "  \t")


@pytest.mark.parametrize("prefix", FORMULA_PREFIXES)
@pytest.mark.parametrize("lead", LEADING_WHITESPACE)
def test_the_boundary_neutralizes_every_prefix_after_any_whitespace(prefix: str, lead: str) -> None:
    payload = f"{lead}{prefix}cmd|'/c calc'!A0"
    assert display.is_formula_like(payload) is True
    assert display.safe_text(payload).startswith("'")


@pytest.mark.parametrize(
    "payload", ["-5", "+3.14", "-2.0", "+1e3", "-1e+4", "1,234", "Ada", "ada@x.example", ""]
)
def test_the_boundary_leaves_ordinary_values_alone(payload: str) -> None:
    assert display.safe_text(payload) == payload


def test_the_boundary_is_the_same_object_in_all_four_environments() -> None:
    """One function, one name, in **every** environment that renders imported
    text — the previous version of this test checked two of the four while
    claiming everywhere, and a missing registration in one of the other two
    later took 223 tests down."""

    from app.web.admin_workbench import templates as admin_templates
    from app.web.company_intelligence import templates as ci_templates
    from app.web.routes import templates as legacy_templates
    from app.web.v2.routes import templates as v2_templates

    environments = {
        "admin": admin_templates,
        "customer_v2": v2_templates,
        "legacy_customer": legacy_templates,
        "company_intelligence": ci_templates,
    }
    for name, templates in environments.items():
        assert "neutralize" in templates.env.filters, f"{name} has no neutralize filter"
        assert templates.env.filters["neutralize"] is display.safe_text, name
    assert len(environments) == 4


def test_every_admin_template_compiles() -> None:
    """The filter is only useful where the template can actually be loaded."""

    from app.web.admin_workbench import templates as admin_templates

    for path in sorted(Path("app/web/templates/admin").glob("*.html")):
        admin_templates.env.get_template(f"admin/{path.name}")


def _hostile_row(prefix: str) -> dict[str, str]:
    """One row whose every operator-visible field is formula-like."""

    return af.row(
        **{
            "First Name": f"{prefix}cmd|first",
            "Last Name": f"{prefix}cmd|last",
            "Title": f"{prefix}cmd|title",
            "Company Name": f"{prefix}cmd|company",
            "Company Name for Emails": f"{prefix}cmd|companyemails",
            "Website": "https://engines.example",
            "City": f"{prefix}cmd|city",
            # ``@`` cannot lead a valid local part — two ``@`` in one address is
            # not an address — so that prefix is exercised on every other field
            # and the address is left well-formed.
            "Email": (
                "plain@engines.example" if prefix == "@" else f"{prefix}cmd|x@engines.example"
            ),
            "Email Status": f"{prefix}cmd|status",
            "Primary Email Source": f"{prefix}cmd|source",
            "Primary Email Verification Source": f"{prefix}cmd|vsource",
            "Primary Email Catch-all Status": f"{prefix}cmd|catchall",
            "Industry": f"{prefix}cmd|industry",
        }
    )


def _assert_inert(body: str, prefix: str) -> None:
    """No rendered text node may begin with a formula character.

    Splits on tags so markup and CSS are not mistaken for content, and checks
    the actual prefix under test rather than one fixed ``=cmd|`` shape — the
    narrow search is what let three of the four prefixes go unproven.
    """

    import re

    needle = f"{prefix}cmd|"
    for chunk in re.split(r"<[^>]*>", body):
        for line in chunk.splitlines():
            stripped = line.strip()
            assert not stripped.startswith(needle), f"live {prefix!r} formula rendered: {stripped}"


@pytest.mark.parametrize("prefix", FORMULA_PREFIXES)
def test_the_import_preview_renders_no_live_formula(
    committed_session: Session, client: Any, prefix: str
) -> None:
    campaign = af.make_campaign(committed_session)
    committed_session.commit()
    upload = client.post(
        f"/app/campaigns/{campaign.id}/imports",
        files={"file": (f"{prefix}cmd|name.csv", af.csv_bytes([_hostile_row(prefix)]), "text/csv")},
        follow_redirects=False,
    )
    assert upload.status_code in (302, 303)
    body = client.get(upload.headers["location"], follow_redirects=True).text
    assert "cmd|first" in body
    _assert_inert(body, prefix)


@pytest.mark.parametrize("prefix", FORMULA_PREFIXES)
def test_the_customer_batch_page_renders_no_live_formula(
    committed_session: Session, client: Any, prefix: str
) -> None:
    campaign = af.make_campaign(committed_session)
    committed_session.commit()
    result = campaign_import.confirm(
        committed_session,
        campaign_id=campaign.id,
        content=af.csv_bytes([_hostile_row(prefix)]),
        filename=f"{prefix}cmd|name.csv",
    )
    committed_session.commit()
    body = client.get(f"/app/campaigns/{campaign.id}/imports/{result.batch_id}").text
    _assert_inert(body, prefix)


@pytest.mark.parametrize("prefix", FORMULA_PREFIXES)
def test_the_customer_contact_and_company_pages_render_no_live_formula(
    committed_session: Session, client: Any, prefix: str
) -> None:
    """The shared pages, which show imported values without knowing they are."""

    campaign = af.make_campaign(committed_session)
    committed_session.commit()
    campaign_import.confirm(
        committed_session,
        campaign_id=campaign.id,
        content=af.csv_bytes([_hostile_row(prefix)]),
        filename="hostile.csv",
    )
    committed_session.commit()
    contact = committed_session.scalars(select(Contact)).first()
    assert contact is not None
    _assert_inert(client.get(f"/app/contacts/{contact.id}").text, prefix)
    if contact.company_id is not None:
        _assert_inert(client.get(f"/app/companies/{contact.company_id}").text, prefix)


@pytest.mark.parametrize("prefix", FORMULA_PREFIXES)
def test_the_admin_import_and_contact_pages_render_no_live_formula(
    committed_session: Session, client: Any, prefix: str
) -> None:
    campaign = af.make_campaign(committed_session)
    committed_session.commit()
    result = campaign_import.confirm(
        committed_session,
        campaign_id=campaign.id,
        content=af.csv_bytes([_hostile_row(prefix)]),
        filename=f"{prefix}cmd|name.csv",
    )
    committed_session.commit()
    _assert_inert(client.get(f"/admin/imports/{result.batch_id}").text, prefix)
    contact = committed_session.scalars(select(Contact)).first()
    assert contact is not None
    _assert_inert(client.get(f"/admin/contacts/{contact.id}").text, prefix)


@pytest.mark.parametrize("prefix", FORMULA_PREFIXES)
def test_the_workbook_sheet_chooser_renders_no_live_formula(
    committed_session: Session, client: Any, prefix: str
) -> None:
    """Sheet names come from the workbook and are rendered as chooser labels."""

    campaign = af.make_campaign(committed_session)
    committed_session.commit()
    workbook = af.xlsx_bytes(
        {
            f"{prefix}cmd|sheet": (af.APOLLO_HEADER, [af.row()]),
            f"{prefix}cmd|other": (af.APOLLO_HEADER, [af.row(**{"Email": "g@engines.example"})]),
        }
    )
    upload = client.post(
        f"/app/campaigns/{campaign.id}/imports",
        files={
            "file": (
                "book.xlsx",
                workbook,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        follow_redirects=False,
    )
    assert upload.status_code in (302, 303)
    body = client.get(upload.headers["location"], follow_redirects=True).text
    assert "cmd|sheet" in body
    _assert_inert(body, prefix)


def test_the_durable_evidence_is_never_neutralized(db_session: Session) -> None:
    """Neutralization is a projection. The record of what the file said must
    stay exactly what the file said, or the audit answer is a lie."""

    campaign = af.make_campaign(db_session)
    _confirm(db_session, campaign, [_hostile_row("=")], "hostile.csv")
    raw_row = db_session.scalars(select(ImportRow)).one()
    assert raw_row.raw_data["First Name"] == "=cmd|first"
    record = db_session.scalars(select(ImportedContactEmail)).first()
    assert record is not None
    assert record.raw_email.startswith("=cmd|")


# ===========================================================================
# SR-IMP-003 — the width ceiling counts physical positions
# ===========================================================================


def test_blank_columns_count_towards_the_width_limit_in_csv() -> None:
    header = ("First Name", "Last Name", "Company Name", "Email") + ("",) * 512
    row = ["Ada", "Lovelace", "Engines", "ada@engines.example"] + [""] * 512
    with pytest.raises(campaign_import.CampaignImportError) as excinfo:
        campaign_import.inspect(_csv_bytes(header, row), "wide.csv")
    assert "516" in str(excinfo.value)


def test_blank_columns_count_towards_the_width_limit_in_xlsx() -> None:
    header = ("First Name", "Last Name", "Company Name", "Email") + ("",) * 512
    with pytest.raises(campaign_import.CampaignImportError):
        campaign_import.inspect(af.xlsx_bytes({"Contacts": (header, [af.row()])}), "wide.xlsx")


def test_the_width_limit_is_reached_before_any_data_row_is_built() -> None:
    """Refused at the header, so the expansion is never materialized.

    The parser is called directly here: if the ceiling were only enforced by the
    import service, the parser would already have built one Python object per
    blank cell per row before anyone objected.
    """

    header = ("First Name", "Last Name", "Company Name", "Email") + ("",) * 600
    rows = [["Ada", "Lovelace", "Engines", "ada@engines.example"] + [""] * 600] * 50
    with pytest.raises(parsing.MalformedFileError):
        parsing.parse_file(_csv_bytes(header, *rows), "wide.csv")


class _CountingStringIO(io.StringIO):
    """A text stream that records how many lines anything asks it for.

    Installed *into the parsing module*, so the count is what the real parser
    read — not what a test-constructed reader read on its behalf. The previous
    version of this sentinel built its own ``csv.reader``, consumed the header
    itself and then called ``_check_width`` directly, which proves the width
    check can reject a header and proves nothing at all about when the parser
    calls it. A deliberate late-rejection mutant survived it.
    """

    lines_read = 0

    def readline(self, *args: Any, **kwargs: Any) -> str:
        type(self).lines_read += 1
        return super().readline(*args, **kwargs)

    def __next__(self) -> str:
        line = self.readline()
        if not line:
            raise StopIteration
        return line


class _ParsingIOShim:
    """Stands in for ``parsing.io`` so only this module's streams are counted."""

    StringIO = _CountingStringIO
    BytesIO = io.BytesIO


def _assert_width_rejected_before_any_data_row(
    parse: Any, content: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sentinel itself, shared by the real test and the mutation proof.

    *parse* is called exactly as the product calls it, with the counting stream
    installed inside ``parsing``. It must refuse, and it must have asked for the
    header and nothing more.

    Shared deliberately: a mutation proof that exercised a *copy* of this logic
    would only prove the copy fails.
    """

    _CountingStringIO.lines_read = 0
    monkeypatch.setattr(parsing, "io", _ParsingIOShim)
    with pytest.raises(parsing.MalformedFileError):
        parse(content, "wide.csv")
    assert _CountingStringIO.lines_read == 1, (
        f"the parser read {_CountingStringIO.lines_read} lines; an over-wide file "
        "must be refused after the header and before any data row is requested"
    )


def _over_wide_csv(rows: int = 50) -> bytes:
    header = ",".join(("First Name", "Last Name", "Company Name", "Email") + ("",) * 600)
    body = "Ada,Lovelace,Engines,ada@engines.example" + "," * 600
    return ("\n".join([header] + [body] * rows) + "\n").encode("utf-8")


def test_the_real_parser_refuses_an_over_wide_file_before_reading_a_data_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through ``parsing.parse_file`` — the entrypoint the product uses.

    The parser reads the header, the parser applies the width check, and the
    stream records that nothing past the header was ever requested.
    """

    _assert_width_rejected_before_any_data_row(parsing.parse_file, _over_wide_csv(), monkeypatch)


def _late_rejecting_parse_file(content: bytes, filename: str | None) -> parsing.ParsedFile:
    """A mutant parser that checks the width one data row too late.

    Exactly the regression the sentinel claims to prevent. Written here rather
    than by editing the repository, so no production file is touched.
    """

    text = content.decode("utf-8-sig")
    reader = csv.reader(parsing.io.StringIO(text), strict=True)  # type: ignore[attr-defined]
    header = next(reader, None) or []
    next(reader, None)  # <- the mutation: one data row consumed first
    parsing._check_width(tuple(header), sheet_name=None)
    raise AssertionError("unreachable for an over-wide file")


def test_the_sentinel_fails_against_a_parser_that_checks_the_width_too_late(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mutation proof: the sentinel must turn red for the exact regression.

    A green test is worth nothing until it has been shown to fail for the reason
    it exists. The mutant is refused for width just as the real parser is, so
    the ONLY thing separating them is when the data row was read — which is what
    the sentinel is supposed to detect.
    """

    with pytest.raises(AssertionError) as excinfo:
        _assert_width_rejected_before_any_data_row(
            _late_rejecting_parse_file, _over_wide_csv(), monkeypatch
        )
    assert "before any data row is requested" in str(excinfo.value)


def test_the_real_parser_is_restored_after_the_mutation_proof() -> None:
    """``monkeypatch`` undoes the stream shim; the mutant was never installed."""

    assert parsing.io is io
    with pytest.raises(parsing.MalformedFileError):
        parsing.parse_file(_over_wide_csv(rows=1), "wide.csv")


def _counting_workbook(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Wrap ``load_workbook`` so every row the parser pulls is counted."""

    counter = {"rows": 0}
    real = parsing.load_workbook

    class _Sheet:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def iter_rows(self, *args: Any, **kwargs: Any) -> Any:
            for row in self._inner.iter_rows(*args, **kwargs):
                counter["rows"] += 1
                yield row

    class _Book:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self.sheetnames = inner.sheetnames

        def __getitem__(self, name: str) -> Any:
            return _Sheet(self._inner[name])

        def close(self) -> None:
            self._inner.close()

    monkeypatch.setattr(parsing, "load_workbook", lambda *a, **k: _Book(real(*a, **k)))
    return counter


def test_the_real_xlsx_parser_refuses_before_reading_a_data_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same proof for the workbook path, at the same entrypoint."""

    counter = _counting_workbook(monkeypatch)
    header = ("First Name", "Last Name", "Company Name", "Email") + ("",) * 600
    content = af.xlsx_positional_bytes(
        "Contacts", header, [["Ada", "Lovelace", "Engines", "a@x.example"] + [""] * 600] * 5
    )
    with pytest.raises(parsing.MalformedFileError):
        parsing.parse_file(content, "wide.xlsx")
    assert counter["rows"] == 1, (
        f"the workbook reader pulled {counter['rows']} rows; the header alone is enough "
        "to know the sheet is too wide"
    )


def test_the_xlsx_sentinel_fails_if_the_width_check_is_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation proof for the workbook path.

    With the width check neutered, the parser reads the whole sheet — so the
    sentinel's row count rises and the assertion it makes has to fail.
    """

    counter = _counting_workbook(monkeypatch)
    monkeypatch.setattr(parsing, "_check_width", lambda *a, **k: None)
    header = ("First Name", "Last Name", "Company Name", "Email") + ("",) * 600
    content = af.xlsx_positional_bytes(
        "Contacts", header, [["Ada", "Lovelace", "Engines", "a@x.example"] + [""] * 600] * 5
    )
    parsing.parse_file(content, "wide.xlsx")
    assert counter["rows"] > 1, "the mutant must read past the header"


def test_a_file_at_the_limit_is_still_accepted() -> None:
    header = ("First Name", "Last Name", "Company Name", "Email") + tuple(
        f"Extra {index}" for index in range(parsing.MAX_PHYSICAL_COLUMNS - 4)
    )
    row = ["Ada", "Lovelace", "Engines", "ada@engines.example"] + [""] * (
        parsing.MAX_PHYSICAL_COLUMNS - 4
    )
    inspection = campaign_import.inspect(_csv_bytes(header, row), "exact.csv")
    assert inspection.sheets[0].detection.recognized is True


# ===========================================================================
# SR-IMP-004 — every physical column keeps its attribution
# ===========================================================================

BLANK_HEADER = ("First Name", "Last Name", "Company Name", "Email", "", "")
BLANK_ROW = ["Ada", "Lovelace", "Engines", "ada@engines.example", "left", "right"]


def test_two_blank_headers_remain_distinguishable_in_the_durable_row() -> None:
    parsed = parsing.parse_file(_csv_bytes(BLANK_HEADER, BLANK_ROW), "blanks.csv")
    raw = parsed.rows[0].raw
    assert raw[parsing.positional_key(4)] == "left"
    assert raw[parsing.positional_key(5)] == "right"


def test_two_blank_headers_both_reach_the_normalized_extras() -> None:
    parsed = parsing.parse_file(_csv_bytes(BLANK_HEADER, BLANK_ROW), "blanks.csv")
    detection = apollo.detect_schema(parsed.sheets[0].columns)
    row = parsed.rows[0]
    reading = apollo.read_row(row.raw, detection, row_number=1, cells=row.cells)
    assert set(reading.extras.values()) >= {"left", "right"}


def test_repeated_blank_headers_do_not_collapse() -> None:
    header = ("First Name", "Last Name", "Company Name", "Email", "", "", "", "")
    row = ["Ada", "Lovelace", "Engines", "ada@engines.example", "a", "b", "c", "d"]
    parsed = parsing.parse_file(_csv_bytes(header, row), "blanks.csv")
    detection = apollo.detect_schema(parsed.sheets[0].columns)
    parsed_row = parsed.rows[0]
    reading = apollo.read_row(parsed_row.raw, detection, row_number=1, cells=parsed_row.cells)
    assert set(reading.extras.values()) >= {"a", "b", "c", "d"}


def test_xlsx_attributes_repeated_blank_headers_by_position() -> None:
    """Rows supplied by POSITION, not by header-keyed dictionary.

    The previous version used the dictionary helper, which collapses two blank
    headers into one key before openpyxl sees the row — so it could only assert
    the column tuple, and proved nothing about whether distinct values under
    repeated blank headers survive. Four blanks with four different values here.
    """

    header = ("First Name", "Last Name", "Company Name", "Email", "", "", "", "")
    content = af.xlsx_positional_bytes(
        "Contacts",
        header,
        [["Ada", "Lovelace", "Engines", "ada@engines.example", "w", "x", "y", "z"]],
    )
    parsed = parsing.parse_file(content, "blanks.xlsx")
    sheet = parsed.sheets[0]
    assert sheet.columns == header

    row = parsed.rows[0]
    # Raw durable evidence keeps each physical position separately.
    for offset, value in enumerate(("w", "x", "y", "z"), start=4):
        assert row.raw[parsing.positional_key(offset)] == value
    assert row.cells[4:] == ("w", "x", "y", "z")

    # And all four reach the normalized extras, still distinct.
    reading = apollo.read_row(
        row.raw, apollo.detect_schema(sheet.columns), row_number=1, cells=row.cells
    )
    assert set(reading.extras.values()) >= {"w", "x", "y", "z"}
    assert len({k for k in reading.extras if k.startswith("(column ")}) == 4


def test_csv_and_xlsx_agree_about_repeated_blank_headers() -> None:
    header = ("First Name", "Last Name", "Company Name", "Email", "", "", "", "")
    values = ["Ada", "Lovelace", "Engines", "ada@engines.example", "w", "x", "y", "z"]
    from_csv = parsing.parse_file(_csv_bytes(header, values), "b.csv").rows[0]
    from_xlsx = parsing.parse_file(
        af.xlsx_positional_bytes("Contacts", header, [values]), "b.xlsx"
    ).rows[0]
    assert from_csv.raw == from_xlsx.raw
    assert from_csv.cells == from_xlsx.cells


def test_preview_and_commit_agree_about_blank_header_values(db_session: Session) -> None:
    content = _csv_bytes(BLANK_HEADER, BLANK_ROW)
    campaign = af.make_campaign(db_session)
    preview = campaign_import.preview(
        db_session, campaign_id=campaign.id, content=content, filename="blanks.csv"
    )
    previewed = preview.rows[0].apollo_row.extras

    campaign_import.confirm(
        db_session, campaign_id=campaign.id, content=content, filename="blanks.csv"
    )
    validation = db_session.scalars(select(ImportRowValidation)).one()
    committed = (validation.normalized_data or {}).get("extras", {})
    assert previewed == committed
    assert set(committed.values()) >= {"left", "right"}


# ===========================================================================
# SR-IMP-006 — the database refuses impossible accepted states
# ===========================================================================


def _seed(session: Session) -> tuple[Any, ImportedContactEmail]:
    campaign = af.make_campaign(session)
    _confirm(session, campaign, [af.row()], "a.csv")
    return campaign, session.scalars(select(ImportedContactEmail)).one()


def _sibling_row(session: Session, original: ImportedContactEmail, number: int) -> ImportRow:
    row = ImportRow(
        batch_id=original.import_batch_id,
        row_number=number,
        sheet_index=0,
        raw_data={"Email": "other@engines.example"},
    )
    session.add(row)
    session.flush()
    return row


def _record(
    original: ImportedContactEmail, row: ImportRow, **overrides: Any
) -> ImportedContactEmail:
    values: dict[str, Any] = {
        "import_batch_id": original.import_batch_id,
        "import_row_id": row.id,
        "campaign_id": original.campaign_id,
        "contact_id": original.contact_id,
        "slot": ImportedEmailSlot.PRIMARY,
        "raw_email": "other@engines.example",
        "normalized_email": "other@engines.example",
        "source_row_number": row.row_number,
        "source_file_checksum": original.source_file_checksum,
        "source_schema": original.source_schema,
        "row_fingerprint": f"fingerprint-{row.row_number}",
        "email_stage_outcome": ImportedEmailStageOutcome.IMPORTED_EMAIL_ACCEPTED,
        "verification_stage_outcome": (
            ImportedVerificationOutcome.VERIFICATION_BYPASSED_IMPORTED_EMAIL
        ),
    }
    values.update(overrides)
    return ImportedContactEmail(**values)


def test_an_accepted_address_cannot_be_an_orphan(db_session: Session) -> None:
    """NULL contact_id means held for review. An accepted record with no Contact
    asserts both at once — and the uniqueness index cannot even see it."""

    _campaign, original = _seed(db_session)
    row = _sibling_row(db_session, original, 91)
    db_session.add(_record(original, row, contact_id=None))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_an_alternate_address_cannot_be_accepted(db_session: Session) -> None:
    """A secondary address is retained and never promoted; deciding otherwise is
    a judgement about a person the file does not license anyone to make."""

    _campaign, original = _seed(db_session)
    row = _sibling_row(db_session, original, 92)
    db_session.add(_record(original, row, slot=ImportedEmailSlot.SECONDARY))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_a_retained_alternate_is_still_allowed(db_session: Session) -> None:
    _campaign, original = _seed(db_session)
    row = _sibling_row(db_session, original, 93)
    db_session.add(
        _record(
            original,
            row,
            slot=ImportedEmailSlot.SECONDARY,
            email_stage_outcome=None,
            verification_stage_outcome=None,
        )
    )
    db_session.flush()  # must not raise


def test_a_held_orphan_record_is_still_allowed(db_session: Session) -> None:
    _campaign, original = _seed(db_session)
    row = _sibling_row(db_session, original, 94)
    db_session.add(
        _record(
            original,
            row,
            contact_id=None,
            email_stage_outcome=None,
            verification_stage_outcome=None,
            rejection_code=campaign_import.HELD_CODE,
        )
    )
    db_session.flush()  # must not raise


def test_a_rejected_record_is_still_allowed(db_session: Session) -> None:
    _campaign, original = _seed(db_session)
    row = _sibling_row(db_session, original, 95)
    db_session.add(
        _record(
            original,
            row,
            normalized_email=None,
            raw_email="not-an-address",
            email_stage_outcome=ImportedEmailStageOutcome.IMPORTED_EMAIL_REJECTED,
            verification_stage_outcome=ImportedVerificationOutcome.VERIFICATION_NOT_PERFORMED,
            rejection_code="email_malformed",
        )
    )
    db_session.flush()  # must not raise


# ===========================================================================
# SR-IMP-007 — malformed quoting is refused, not silently rewritten
# ===========================================================================


def test_an_illegal_quote_transition_is_refused() -> None:
    content = b'First Name,Last Name,Company Name,Email\n"A"da,Lovelace,E,a@x.example\n'
    with pytest.raises(parsing.MalformedFileError):
        parsing.parse_file(content, "q.csv")


def test_an_unterminated_quoted_field_is_refused() -> None:
    content = b'First Name,Last Name,Company Name,Email\n"A,Lovelace,E,a@x.example\n'
    with pytest.raises(parsing.MalformedFileError):
        parsing.parse_file(content, "q.csv")


def test_a_malformed_quote_reaches_the_route_as_a_typed_import_error() -> None:
    content = b'First Name,Last Name,Company Name,Email\n"A"da,Lovelace,E,a@x.example\n'
    with pytest.raises(campaign_import.CampaignImportError):
        campaign_import.inspect(content, "q.csv")


def test_ordinary_quoting_still_parses() -> None:
    content = (
        b"First Name,Last Name,Company Name,Email\n"
        b'"Ada, the first","Love ""Lace""",Engines,ada@engines.example\n'
    )
    parsed = parsing.parse_file(content, "ok.csv")
    assert parsed.rows[0].cells[0] == "Ada, the first"
    assert parsed.rows[0].cells[1] == 'Love "Lace"'


def test_the_two_csv_failures_are_described_differently() -> None:
    """The one message used to claim an unclosed quote was rejected while it was
    silently accepted. Two failures, two accurate sentences."""

    limit = parsing._csv_error_message(Exception("field larger than field limit (131072)"))
    quoting = parsing._csv_error_message(Exception("',' expected after '\"'"))
    assert "cell larger than" in limit
    assert "quoting error" in quoting
    assert limit != quoting


# ===========================================================================
# SR-IMP-008 — the tests that were vacuous
# ===========================================================================


def test_composed_and_decomposed_names_really_are_different_inputs() -> None:
    """The previous version of this test used one literal twice.

    Both forms are constructed here, and the test first proves they are
    different byte sequences — otherwise it proves nothing about normalization.
    """

    nfc = unicodedata.normalize("NFC", "José")
    nfd = unicodedata.normalize("NFD", "José")
    assert nfc != nfd
    assert len(nfd) > len(nfc)

    from app.services.imports import normalization as norm

    assert norm.normalize_name(nfc) == norm.normalize_name(nfd)

    detection = apollo.detect_schema(af.APOLLO_HEADER)
    composed = apollo.read_row(af.row(**{"First Name": nfc}), detection, row_number=1)
    decomposed = apollo.read_row(af.row(**{"First Name": nfd}), detection, row_number=1)
    assert apollo.row_fingerprint(composed) == apollo.row_fingerprint(decomposed)


def test_an_opaque_identifier_keeps_its_composition() -> None:
    """The other half of the boundary: a vendor key is compared byte for byte."""

    detection = apollo.detect_schema(af.APOLLO_HEADER)
    nfc = unicodedata.normalize("NFC", "id-café")
    nfd = unicodedata.normalize("NFD", "id-café")
    composed = apollo.read_row(af.row(**{"Apollo Contact Id": nfc}), detection, row_number=1)
    decomposed = apollo.read_row(af.row(**{"Apollo Contact Id": nfd}), detection, row_number=1)
    assert apollo.row_fingerprint(composed) != apollo.row_fingerprint(decomposed)


def _migration_module() -> Any:
    import importlib.util

    path = Path("migrations/versions/c1f7a3e29b04_campaign_contact_file_import.py")
    spec = importlib.util.spec_from_file_location("imp001_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_downgrade_guard_sees_a_real_populated_database(db_session: Session) -> None:
    """Runtime, not source inspection.

    The previous pass asserted that the word ``RuntimeError`` appeared in the
    migration file. This runs the guard's own predicate against a live
    connection holding real imported evidence.
    """

    module = _migration_module()
    connection = db_session.connection()
    assert module._unrecoverable_state(connection) == []

    campaign = af.make_campaign(db_session)
    _confirm(db_session, campaign, [af.row()], "a.csv")
    db_session.flush()

    holdings = module._unrecoverable_state(connection)
    assert holdings, "a populated database must block the downgrade"
    assert any("imported address record" in entry for entry in holdings)
    assert any("per-row import decision" in entry for entry in holdings)


def test_the_downgrade_guard_ignores_an_empty_batch_shell(db_session: Session) -> None:
    """A batch carrying only the defaults the upgrade itself wrote is not a
    decision worth making the migration one-way over."""

    module = _migration_module()
    campaign = af.make_campaign(db_session)
    db_session.add(
        ImportBatch(
            campaign_id=campaign.id,
            content_hash="none",
            source_format="csv",
            total_rows=0,
        )
    )
    db_session.flush()
    assert module._unrecoverable_state(db_session.connection()) == []

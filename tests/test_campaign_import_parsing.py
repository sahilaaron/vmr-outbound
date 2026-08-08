"""Apollo schema detection, file parsing, and preview safety (IMP-001 §25.1-9)."""

from __future__ import annotations

import random

import pytest
from app.models.campaign import CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.import_batch import ImportBatch
from app.models.imported_email import ImportedContactEmail
from app.services.imports import apollo, campaign_import
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests import apollo_factory as af

pytestmark = pytest.mark.usefixtures("enable_csv_import")


# --- 1-4. Detection by header name, in any order, with extra columns ---------


def test_apollo_csv_is_detected_by_headers() -> None:
    detection = apollo.detect_schema(list(af.APOLLO_HEADER))
    assert detection.recognized
    assert detection.schema_id == apollo.APOLLO_SCHEMA_ID
    assert detection.is_apollo_export
    assert detection.column_for("email") == "Email"
    assert detection.column_for("company_linkedin_url") == "Company Linkedin Url"


def test_apollo_xlsx_is_detected_by_headers() -> None:
    content = af.xlsx_bytes({"Contacts": (af.APOLLO_HEADER, [af.row()])})
    inspection = campaign_import.inspect(content, "apollo.xlsx")
    assert inspection.source_format == "xlsx"
    sheet = inspection.sheet(None)
    assert sheet is not None
    assert sheet.detection.schema_id == apollo.APOLLO_SCHEMA_ID
    assert sheet.detection.is_apollo_export


def test_header_order_does_not_matter() -> None:
    shuffled = list(af.APOLLO_HEADER)
    random.Random(7).shuffle(shuffled)
    detection = apollo.detect_schema(shuffled)
    assert detection.recognized
    assert detection.column_for("first_name") == "First Name"
    assert detection.column_for("apollo_account_id") == "Apollo Account Id"


def test_extra_columns_do_not_break_parsing_and_are_kept_verbatim() -> None:
    header = (*af.APOLLO_HEADER, "Custom Score", "Internal Note")
    detection = apollo.detect_schema(list(header))
    assert detection.recognized
    assert "Custom Score" in detection.unmapped_columns
    raw = af.row()
    raw["Custom Score"] = "88"
    raw["Internal Note"] = "spoke at conference"
    parsed = apollo.read_row(raw, detection, row_number=1)
    assert parsed.extras["Custom Score"] == "88"
    assert parsed.first_name == "Ada"


def test_alias_spellings_resolve_to_the_same_field() -> None:
    detection = apollo.detect_schema(
        ["first_name", "LAST NAME", "Company", "E-Mail", "person linkedin url"]
    )
    assert detection.column_for("first_name") == "first_name"
    assert detection.column_for("last_name") == "LAST NAME"
    assert detection.column_for("company_name") == "Company"
    assert detection.column_for("email") == "E-Mail"


def test_a_second_column_claiming_a_taken_field_is_reported_not_applied() -> None:
    detection = apollo.detect_schema([*af.MINIMAL_HEADER, "E-Mail"])
    assert detection.column_for("email") == "Email"
    assert ("E-Mail", "email") in detection.duplicate_columns
    assert "E-Mail" in detection.unmapped_columns


# --- 5. Missing required headers are rejected --------------------------------


def test_missing_required_headers_are_rejected_with_an_actionable_message(
    db_session: Session,
) -> None:
    campaign = af.make_campaign(db_session)
    content = af.csv_bytes([af.row()], header=("First Name", "Last Name", "Company Name"))
    result = campaign_import.preview(
        db_session, campaign_id=campaign.id, content=content, filename="no-email.csv"
    )
    assert not result.is_importable
    assert result.structure_error is not None
    assert "Email" in result.structure_error
    assert result.schema_id is None


def test_confirm_refuses_a_file_whose_schema_is_not_recognized(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    content = af.csv_bytes([af.row()], header=("First Name", "Last Name", "Company Name"))
    with pytest.raises(campaign_import.UnreadableFileError):
        campaign_import.confirm(
            db_session, campaign_id=campaign.id, content=content, filename="no-email.csv"
        )


# --- 6. Multiple worksheets are handled safely -------------------------------


def test_multiple_worksheets_are_reported_and_one_can_be_chosen(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    content = af.xlsx_bytes(
        {
            "Notes": (("Heading",), [{"Heading": "not a contact sheet"}]),
            "Contacts": (af.APOLLO_HEADER, [af.row()]),
            "More": (
                af.APOLLO_HEADER,
                [af.row(**{"Email": "grace@engines.example", "First Name": "Grace"})],
            ),
        }
    )
    inspection = campaign_import.inspect(content, "multi.xlsx")
    assert len(inspection.sheets) == 3
    assert {sheet.name for sheet in inspection.importable_sheets} == {"Contacts", "More"}
    assert inspection.needs_sheet_choice

    chosen = campaign_import.preview(
        db_session, campaign_id=campaign.id, content=content, filename="multi.xlsx", sheet_index=2
    )
    assert chosen.sheet_name == "More"
    assert chosen.total_rows == 1
    assert chosen.rows[0].apollo_row.first_name == "Grace"


def test_a_sheet_with_a_header_and_no_rows_is_refused_not_silently_empty(
    db_session: Session,
) -> None:
    campaign = af.make_campaign(db_session)
    content = af.xlsx_bytes({"Contacts": (af.APOLLO_HEADER, [])})
    result = campaign_import.preview(
        db_session, campaign_id=campaign.id, content=content, filename="empty.xlsx"
    )
    assert result.structure_error is not None


# --- 7. Preview does not mutate the database ---------------------------------


def _row_counts(session: Session) -> dict[str, int]:
    return {
        model.__name__: int(session.scalar(select(func.count()).select_from(model)) or 0)
        for model in (
            Contact,
            Company,
            CampaignContact,
            ImportBatch,
            ImportedContactEmail,
        )
    }


def test_preview_creates_nothing_at_all(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    db_session.flush()
    before = _row_counts(db_session)

    content = af.csv_bytes(
        [
            af.row(),
            af.row(**{"Email": "grace@engines.example", "First Name": "Grace"}),
            af.row(**{"Email": "not-an-address", "First Name": "Broken"}),
        ]
    )
    result = campaign_import.preview(
        db_session, campaign_id=campaign.id, content=content, filename="apollo.csv"
    )
    assert result.total_rows == 3
    assert _row_counts(db_session) == before


def test_preview_and_confirm_agree_row_for_row(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    content = af.csv_bytes(
        [
            af.row(),
            af.row(**{"Email": "grace@engines.example", "First Name": "Grace"}),
            af.row(**{"First Name": "", "Last Name": "", "Email": "x@engines.example"}),
        ]
    )
    predicted = campaign_import.preview(
        db_session, campaign_id=campaign.id, content=content, filename="apollo.csv"
    )
    committed = campaign_import.confirm(
        db_session, campaign_id=campaign.id, content=content, filename="apollo.csv"
    )
    assert predicted.counts["imported"] == committed.imported
    assert predicted.counts["failed"] == committed.failed


# --- 8. Formulas and macros are never executed -------------------------------


def test_formula_cells_are_never_evaluated_and_are_flagged(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    content = af.xlsx_with_formula(af.APOLLO_HEADER, [af.row()])
    result = campaign_import.preview(
        db_session, campaign_id=campaign.id, content=content, filename="formula.xlsx"
    )
    assert result.total_rows == 1
    # The workbook is read with cached values only, so an unevaluated formula
    # contributes nothing. What must never appear anywhere is its RESULT.
    row = result.rows[0]
    assert "2" not in (row.apollo_row.extras.values() or [])
    assert row.apollo_row.first_name == "Ada"


def test_a_formula_shaped_cell_value_is_carried_as_text_and_warned_about() -> None:
    detection = apollo.detect_schema(list(af.APOLLO_HEADER))
    raw = af.row(**{"Company Name": '=cmd|"/c calc"!A0'})
    parsed = apollo.read_row(raw, detection, row_number=1)
    assert parsed.company_name == '=cmd|"/c calc"!A0'
    assert any(code == "formula_like_cells" for code, _ in parsed.warnings)


def test_formula_values_are_neutralized_on_the_way_out() -> None:
    assert apollo.neutralize_formula("=1+1") == "'=1+1"
    assert apollo.neutralize_formula("+SUM(A1)") == "'+SUM(A1)"
    assert apollo.neutralize_formula("-2+3") == "'-2+3"
    assert apollo.neutralize_formula("@import") == "'@import"
    assert apollo.neutralize_formula("Analytical Engines") == "Analytical Engines"


# --- 9. Oversized or malformed files are rejected safely ---------------------


def test_an_unsupported_file_type_is_refused() -> None:
    with pytest.raises(campaign_import.UnreadableFileError) as excinfo:
        campaign_import.inspect(b"anything", "contacts.xls")
    assert ".xlsx" in str(excinfo.value)


def test_an_empty_file_is_refused() -> None:
    with pytest.raises(campaign_import.UnreadableFileError):
        campaign_import.inspect(b"", "contacts.csv")


def test_a_renamed_executable_is_refused_as_an_unreadable_workbook() -> None:
    with pytest.raises(campaign_import.UnreadableFileError) as excinfo:
        campaign_import.inspect(b"MZ\x90\x00\x03\x00\x00\x00", "contacts.xlsx")
    assert "could not be opened" in str(excinfo.value)


def test_undecodable_bytes_are_refused_with_an_encoding_message() -> None:
    with pytest.raises(campaign_import.UnreadableFileError) as excinfo:
        campaign_import.inspect(b"\xff\xfe\x00\x01\x02bad", "contacts.csv")
    assert "UTF-8" in str(excinfo.value)


def test_utf8_with_a_byte_order_mark_is_read_normally(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    content = af.csv_bytes([af.row()], bom=True)
    result = campaign_import.preview(
        db_session, campaign_id=campaign.id, content=content, filename="bom.csv"
    )
    assert result.is_importable
    assert result.rows[0].apollo_row.first_name == "Ada"


def test_too_many_rows_is_refused_before_anything_is_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(campaign_import, "MAX_DATA_ROWS", 2)
    content = af.csv_bytes([af.row(**{"Email": f"p{i}@engines.example"}) for i in range(3)])
    with pytest.raises(campaign_import.UnreadableFileError) as excinfo:
        campaign_import.inspect(content, "big.csv")
    assert "row limit" in str(excinfo.value)


def test_too_many_columns_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(campaign_import, "MAX_COLUMNS", 5)
    with pytest.raises(campaign_import.UnreadableFileError) as excinfo:
        campaign_import.inspect(af.csv_bytes([af.row()]), "wide.csv")
    assert "column limit" in str(excinfo.value)


def test_hostile_filenames_are_sanitized() -> None:
    assert campaign_import.sanitize_filename("../../etc/passwd") == "passwd"
    assert campaign_import.sanitize_filename(r"C:\Windows\System32\evil.csv") == "evil.csv"
    assert campaign_import.sanitize_filename("a/b/../c.csv") == "c.csv"
    assert campaign_import.sanitize_filename(None) == "upload"
    assert campaign_import.sanitize_filename("...") == "upload"


def test_the_feature_switch_gates_every_entry_point(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    campaign = af.make_campaign(db_session)
    content = af.csv_bytes([af.row()])
    monkeypatch.delenv("FEATURES__CSV_IMPORT", raising=False)
    get_settings.cache_clear()
    for call in (
        lambda: campaign_import.inspect(content, "apollo.csv"),
        lambda: campaign_import.preview(
            db_session, campaign_id=campaign.id, content=content, filename="apollo.csv"
        ),
        lambda: campaign_import.confirm(
            db_session, campaign_id=campaign.id, content=content, filename="apollo.csv"
        ),
    ):
        with pytest.raises(campaign_import.FeatureDisabledError):
            call()

"""Preview (non-committing) and unified XLSX import pipeline tests."""

from __future__ import annotations

import io

import pytest
from app.models.campaign import CampaignContact
from app.models.contact import Contact
from app.models.enums import (
    ImportBatchStatus,
    ImportRowOutcome,
    ImportSourceFormat,
    SuppressionReason,
    SuppressionType,
)
from app.models.import_batch import ImportBatch, ImportRow, ImportRowValidation
from app.services.campaigns import create_campaign
from app.services.imports import parsing
from app.services.imports.importer import run_import
from app.services.imports.preview import preview_import
from app.services.suppressions import add_suppression
from openpyxl import Workbook
from sqlalchemy import func, select
from sqlalchemy.orm import Session

MAPPING = {
    "First Name": "first_name",
    "Surname": "last_name",
    "Company": "company_name",
    "Website": "company_domain",
    "Email Address": "email",
}


def _xlsx(sheets: dict[str, list[list[object]]]) -> bytes:
    workbook = Workbook()
    active = workbook.active
    first = True
    for name, rows in sheets.items():
        if first and active is not None:
            worksheet = active
            worksheet.title = name
            first = False
        else:
            worksheet = workbook.create_sheet(title=name)
        for row in rows:
            worksheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _two_sheet_workbook() -> bytes:
    header = ["First Name", "Surname", "Company", "Website", "Email Address"]
    return _xlsx(
        {
            "Mining": [
                header,
                ["Elena", "Petrova", "Granite Co", "granite.example", "elena@granite.example"],
                ["Broken", "", "No Domain Ltd", "", "not-an-email"],
            ],
            "Cement": [
                header,
                ["Rahul", "Kapoor", "Deccan Cement", "deccan.example", "rahul@deccan.example"],
                ["Nina", "Kovacs", "Blocked Corp", "blocked.example", "nina@blocked.example"],
            ],
            "Notes": [],
        }
    )


def _counts(db: Session) -> tuple[int, int, int, int]:
    return (
        db.scalar(select(func.count(Contact.id))) or 0,
        db.scalar(select(func.count(CampaignContact.id))) or 0,
        db.scalar(select(func.count(ImportBatch.id))) or 0,
        db.scalar(select(func.count(ImportRow.id))) or 0,
    )


# --- Preview: no persistence of any kind ---------------------------------------


def test_preview_predicts_outcomes_without_writing(db_session: Session) -> None:
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="nina@blocked.example",
        reason=SuppressionReason.OPT_OUT,
    )
    before = _counts(db_session)

    parsed = parsing.parse_xlsx(_two_sheet_workbook())
    result = preview_import(
        db_session, parsed=parsed, sheet_selection=[0, 1], column_mapping=dict(MAPPING)
    )

    assert result.is_importable
    assert result.total_rows == 4
    assert (result.accepted, result.rejected, result.suppressed) == (2, 1, 1)
    assert result.problems and all(p.row_number == 2 for p in result.problems)
    assert _counts(db_session) == before  # preview wrote absolutely nothing


def test_preview_catches_intra_file_duplicates(db_session: Session) -> None:
    csv = (
        b"first_name,last_name,company_name,company_domain,email\n"
        b"Ada,Lovelace,Engines,engines.example,ada@engines.example\n"
        b"Ada,Lovelace,Engines,engines.example,ada@engines.example\n"
    )
    parsed = parsing.parse_csv(csv)
    result = preview_import(db_session, parsed=parsed, sheet_selection=None, column_mapping=None)
    assert (result.accepted, result.duplicate) == (1, 1)
    assert "earlier row in this file" in (result.rows[1].note or "")


def test_preview_reports_structure_error_for_bad_mapping(db_session: Session) -> None:
    parsed = parsing.parse_xlsx(_two_sheet_workbook())
    result = preview_import(
        db_session,
        parsed=parsed,
        sheet_selection=[0],
        column_mapping={"First Name": "first_name"},
    )
    assert not result.is_importable
    assert result.structure_error is not None
    assert "required" in result.structure_error


# --- XLSX through the one shared committing pipeline ----------------------------


@pytest.mark.usefixtures("enable_csv_import")
def test_xlsx_import_multi_sheet_with_mapping(db_session: Session) -> None:
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="nina@blocked.example",
        reason=SuppressionReason.OPT_OUT,
    )
    campaign = create_campaign(db_session, name="XLSX campaign")
    db_session.commit()

    summary = run_import(
        db_session,
        campaign_id=campaign.id,
        content=_two_sheet_workbook(),
        filename="workbook.xlsx",
        sheet_selection=[0, 1],
        column_mapping=dict(MAPPING),
    )

    assert summary.status is ImportBatchStatus.COMPLETED
    assert summary.total_rows == 4
    assert summary.accepted_rows == 2
    assert summary.rejected_rows == 1
    assert summary.suppressed_rows == 1
    assert summary.contacts_created == 2

    batch = db_session.get(ImportBatch, summary.batch_id)
    assert batch is not None
    assert batch.source_format is ImportSourceFormat.XLSX
    assert batch.column_mapping == MAPPING
    assert batch.mapper_version is not None
    assert batch.parser_version == parsing.XLSX_PARSER_VERSION

    rows = db_session.scalars(
        select(ImportRow)
        .where(ImportRow.batch_id == batch.id)
        .order_by(ImportRow.sheet_index, ImportRow.row_number)
    ).all()
    # Workbook filename, sheet name/index, and per-sheet row numbers preserved.
    assert batch.filename == "workbook.xlsx"
    assert [(r.sheet_index, r.sheet_name, r.row_number) for r in rows] == [
        (0, "Mining", 1),
        (0, "Mining", 2),
        (1, "Cement", 1),
        (1, "Cement", 2),
    ]
    # Raw rows keep the ORIGINAL (pre-mapping) headers verbatim.
    assert rows[0].raw_data["First Name"] == "Elena"


@pytest.mark.usefixtures("enable_csv_import")
def test_xlsx_repeated_confirm_is_idempotent(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="XLSX idempotent")
    db_session.commit()
    content = _two_sheet_workbook()

    first = run_import(
        db_session,
        campaign_id=campaign.id,
        content=content,
        filename="workbook.xlsx",
        sheet_selection=[0, 1],
        column_mapping=dict(MAPPING),
    )
    second = run_import(
        db_session,
        campaign_id=campaign.id,
        content=content,
        filename="workbook.xlsx",
        sheet_selection=[0, 1],
        column_mapping=dict(MAPPING),
    )
    assert second.reused_existing_batch
    assert second.batch_id == first.batch_id

    # A deliberately different interpretation (other sheet selection) is a new import.
    third = run_import(
        db_session,
        campaign_id=campaign.id,
        content=content,
        filename="workbook.xlsx",
        sheet_selection=[0],
        column_mapping=dict(MAPPING),
    )
    assert not third.reused_existing_batch
    assert third.batch_id != first.batch_id


@pytest.mark.usefixtures("enable_csv_import")
def test_malformed_workbook_fails_visibly(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="Malformed workbook")
    db_session.commit()
    summary = run_import(
        db_session,
        campaign_id=campaign.id,
        content=b"not really an xlsx",
        filename="broken.xlsx",
    )
    assert summary.status is ImportBatchStatus.FAILED
    assert summary.error_detail is not None
    assert "could not be opened" in summary.error_detail
    assert summary.contacts_created == 0


@pytest.mark.usefixtures("enable_csv_import")
def test_empty_workbook_fails_visibly(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="Empty workbook")
    db_session.commit()
    summary = run_import(
        db_session,
        campaign_id=campaign.id,
        content=_xlsx({"Blank": []}),
        filename="empty.xlsx",
    )
    assert summary.status is ImportBatchStatus.FAILED
    assert summary.error_detail is not None


@pytest.mark.usefixtures("enable_csv_import")
def test_unsupported_extension_fails_visibly(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="Unsupported extension")
    db_session.commit()
    summary = run_import(
        db_session,
        campaign_id=campaign.id,
        content=b"a,b\n1,2\n",
        filename="legacy.xls",
    )
    assert summary.status is ImportBatchStatus.FAILED
    assert summary.error_detail is not None
    assert ".xls" in summary.error_detail


# --- Ambiguous outcome (DAT-004-compatible representation) ----------------------


@pytest.mark.usefixtures("enable_csv_import")
def test_ambiguous_match_is_explicit_reviewable_and_creates_nothing(
    db_session: Session,
) -> None:
    campaign = create_campaign(db_session, name="Ambiguity campaign")
    db_session.commit()

    # Two existing contacts share the natural key (distinguished by email).
    seed = (
        b"first_name,last_name,company_name,company_domain,email\n"
        b"Jo,Doe,Acme,acme.example,jo1@acme.example\n"
        b"Jo,Doe,Acme,acme.example,jo2@acme.example\n"
    )
    run_import(db_session, campaign_id=campaign.id, content=seed, filename="seed.csv")
    contacts_before = db_session.scalar(select(func.count(Contact.id))) or 0

    # An email-less row with that natural key cannot pick a merge target.
    ambiguous_csv = (
        b"first_name,last_name,company_name,company_domain,email\nJo,Doe,Acme,acme.example,\n"
    )
    summary = run_import(
        db_session, campaign_id=campaign.id, content=ambiguous_csv, filename="ambiguous.csv"
    )

    assert summary.status is ImportBatchStatus.COMPLETED
    assert summary.ambiguous_rows == 1
    assert summary.accepted_rows == 0
    assert summary.contacts_created == 0
    # No contact and no membership were silently created or merged.
    assert (db_session.scalar(select(func.count(Contact.id))) or 0) == contacts_before

    batch = db_session.get(ImportBatch, summary.batch_id)
    assert batch is not None and batch.ambiguous_rows == 1

    validation = db_session.scalars(
        select(ImportRowValidation)
        .join(ImportRow, ImportRow.id == ImportRowValidation.import_row_id)
        .where(
            ImportRow.batch_id == summary.batch_id,
            ImportRowValidation.outcome == ImportRowOutcome.AMBIGUOUS,
        )
    ).one()
    assert validation.contact_id is None
    assert validation.note is not None
    assert "kept separate" in validation.note  # the reason is recorded for review


@pytest.mark.usefixtures("enable_csv_import")
def test_interrupted_import_marks_batch_failed_and_keeps_raw_rows(
    db_session: Session,
) -> None:
    campaign = create_campaign(db_session, name="Interrupted XLSX")
    db_session.commit()

    def _boom() -> None:
        raise RuntimeError("simulated interruption")

    with pytest.raises(RuntimeError):
        run_import(
            db_session,
            campaign_id=campaign.id,
            content=_two_sheet_workbook(),
            filename="workbook.xlsx",
            sheet_selection=[0, 1],
            column_mapping=dict(MAPPING),
            _fault=_boom,
        )
    batch = db_session.scalars(
        select(ImportBatch).where(ImportBatch.campaign_id == campaign.id)
    ).one()
    assert batch.status is ImportBatchStatus.FAILED
    raw_count = (
        db_session.scalar(select(func.count(ImportRow.id)).where(ImportRow.batch_id == batch.id))
        or 0
    )
    assert raw_count == 4  # raw capture survives the rollback
    assert campaign.id is not None
    assert (
        db_session.scalar(
            select(func.count(Contact.id)).join(
                CampaignContact, CampaignContact.contact_id == Contact.id
            )
        )
        or 0
    ) == 0

"""Shared parsing layer tests: CSV and XLSX into one neutral shape."""

from __future__ import annotations

import io
from datetime import date, datetime

import pytest
from app.services.imports import parsing
from openpyxl import Workbook


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


# --- Format detection ---------------------------------------------------------


def test_detect_format_csv_and_xlsx() -> None:
    assert parsing.detect_format("contacts.CSV") == "csv"
    assert parsing.detect_format("Workbook.XLSX") == "xlsx"


def test_detect_format_rejects_xls_with_guidance() -> None:
    with pytest.raises(parsing.UnsupportedFormatError, match=r"\.xls"):
        parsing.detect_format("legacy.xls")


def test_detect_format_rejects_other_extensions() -> None:
    for name in ("data.txt", "sheet.ods", "noext", None):
        with pytest.raises(parsing.UnsupportedFormatError):
            parsing.detect_format(name)


def test_empty_upload_is_malformed() -> None:
    with pytest.raises(parsing.MalformedFileError, match="empty"):
        parsing.parse_file(b"", "contacts.csv")


# --- CSV ------------------------------------------------------------------------


def test_parse_csv_single_sheet_shape() -> None:
    parsed = parsing.parse_csv(b"first_name,last_name\nAda,Lovelace\n\nGrace,Hopper\n")
    assert parsed.source_format == "csv"
    assert len(parsed.sheets) == 1
    sheet = parsed.sheets[0]
    assert sheet.index == 0 and sheet.name is None
    assert sheet.header == ["first_name", "last_name"]
    assert sheet.data_row_count == 2  # blank line skipped
    assert [r.row_number for r in parsed.rows] == [1, 2]
    assert parsed.rows[0].sheet_index == 0 and parsed.rows[0].sheet_name is None


def test_parse_csv_rejects_non_utf8() -> None:
    with pytest.raises(parsing.MalformedFileError, match="UTF-8"):
        parsing.parse_csv("émile".encode("utf-16"))


# --- XLSX -----------------------------------------------------------------------


def test_parse_xlsx_preserves_sheet_identity_and_row_numbers() -> None:
    content = _xlsx(
        {
            "Mining": [["first_name", "last_name"], ["Elena", "Petrova"], ["Tomas", "L"]],
            "Cement": [["first_name", "last_name"], ["Rahul", "Kapoor"]],
        }
    )
    parsed = parsing.parse_xlsx(content)
    assert parsed.source_format == "xlsx"
    assert [(s.index, s.name, s.data_row_count) for s in parsed.sheets] == [
        (0, "Mining", 2),
        (1, "Cement", 1),
    ]
    cement_rows = parsed.rows_for_sheets([1])
    assert len(cement_rows) == 1
    assert cement_rows[0].sheet_name == "Cement"
    assert cement_rows[0].row_number == 1  # per-sheet numbering
    assert cement_rows[0].raw["first_name"] == "Rahul"


def test_parse_xlsx_renders_cells_verbatim() -> None:
    content = _xlsx(
        {
            "S": [
                ["n", "f", "b", "d", "dt", "empty"],
                [42, 1.5, True, date(2026, 7, 1), datetime(2026, 7, 1, 9, 30), None],
            ]
        }
    )
    parsed = parsing.parse_xlsx(content)
    raw = parsed.rows[0].raw
    assert raw["n"] == "42"  # integral float has no trailing .0
    assert raw["f"] == "1.5"
    assert raw["b"] == "TRUE"
    assert raw["d"] == "2026-07-01"
    assert raw["dt"].startswith("2026-07-01 09:30")
    assert raw["empty"] == ""


def test_parse_xlsx_malformed_bytes_visible_error() -> None:
    with pytest.raises(parsing.MalformedFileError, match="could not be opened"):
        parsing.parse_xlsx(b"this is not a zip archive")


def test_parse_xlsx_empty_workbook_visible_error() -> None:
    with pytest.raises(parsing.MalformedFileError, match="empty"):
        parsing.parse_xlsx(_xlsx({"Blank": []}))


def test_parse_xlsx_headerless_sheet_listed_but_unusable() -> None:
    content = _xlsx({"Data": [["first_name"], ["Ada"]], "Notes": []})
    parsed = parsing.parse_xlsx(content)
    notes = parsed.sheet(1)
    assert notes is not None and notes.header == [] and notes.data_row_count == 0

"""Shared spreadsheet parsing for the two authorized import formats (CSV, XLSX).

Both formats are parsed into one neutral shape — sheets of header + verbatim
string rows — so every downstream stage (mapping, validation, normalization,
deduplication, provenance, suppression, persistence) runs on exactly one code
path. Business rules are never duplicated per format.

Boundaries (product rule): ``.csv`` and ``.xlsx`` only. ``.xls``, Google Sheets
direct import, and other formats are rejected visibly at this layer.

XLSX specifics:

* Parsed with ``openpyxl`` in read-only mode; cell values are rendered to
  strings verbatim-as-displayed (numbers keep their repr, dates use ISO format).
* Sheet name, sheet index, and the original per-sheet row number are preserved
  on every row.
* A malformed workbook (not a real ``.xlsx``) or an empty workbook (no sheet
  with a header) is a visible, batch-level failure — never a silent success.

A CSV file is represented as a single sheet with index 0 and no sheet name, so
flat files flow through the same pipeline unchanged.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any
from zipfile import BadZipFile

from openpyxl import load_workbook

CSV_PARSER_VERSION = "csv-2"
XLSX_PARSER_VERSION = "xlsx-1"

SUPPORTED_EXTENSIONS = (".csv", ".xlsx")

_UNMAPPED_KEY = "_unmapped"


class UnsupportedFormatError(Exception):
    """Raised for a file that is not one of the authorized formats."""


class MalformedFileError(Exception):
    """Raised when a file cannot be parsed as its claimed format."""


@dataclass(frozen=True)
class ParsedRow:
    """One verbatim data row, keyed by the sheet's header."""

    sheet_index: int
    sheet_name: str | None
    row_number: int  # original per-sheet data-row number (header excluded)
    raw: dict[str, str]


@dataclass(frozen=True)
class SheetInfo:
    """Inspection summary of one sheet (for the workbook-inspection step)."""

    index: int
    name: str | None
    header: list[str]
    data_row_count: int


@dataclass
class ParsedFile:
    """A parsed CSV or XLSX file in the neutral shared shape."""

    source_format: str  # "csv" | "xlsx"
    parser_version: str
    sheets: list[SheetInfo] = field(default_factory=list)
    rows: list[ParsedRow] = field(default_factory=list)

    def sheet(self, index: int) -> SheetInfo | None:
        for info in self.sheets:
            if info.index == index:
                return info
        return None

    def rows_for_sheets(self, indexes: list[int] | None) -> list[ParsedRow]:
        """Rows restricted to the selected sheets (all rows when None)."""

        if indexes is None:
            return list(self.rows)
        wanted = set(indexes)
        return [row for row in self.rows if row.sheet_index in wanted]


def detect_format(filename: str | None) -> str:
    """Return "csv" or "xlsx" from the filename, or raise for anything else."""

    name = (filename or "").strip().lower()
    if name.endswith(".csv"):
        return "csv"
    if name.endswith(".xlsx"):
        return "xlsx"
    if name.endswith(".xls"):
        raise UnsupportedFormatError(
            "Legacy .xls workbooks are not supported. Save the file as .xlsx "
            "(or export a .csv) and upload again."
        )
    raise UnsupportedFormatError(
        "Unsupported file type. The import accepts .csv and .xlsx files only."
    )


def _cell_to_text(value: Any) -> str:
    """Render one cell value to its verbatim string form."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        # A date-only cell arrives as midnight; render it as the operator saw it.
        if value.time() == time.min:
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def parse_csv(content: bytes) -> ParsedFile:
    """Parse CSV bytes into the shared shape (single sheet, index 0)."""

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MalformedFileError(
            "The file is not readable as UTF-8 text. Re-export the CSV with "
            "UTF-8 encoding and upload again."
        ) from exc

    reader = csv.DictReader(io.StringIO(text), restkey=_UNMAPPED_KEY, restval="")
    rows: list[ParsedRow] = []
    row_number = 0
    for row in reader:
        cleaned: dict[str, str] = {}
        for key, value in row.items():
            if key is None:
                continue
            if key == _UNMAPPED_KEY and isinstance(value, list):
                cleaned[key] = ", ".join(str(v) for v in value)
            else:
                cleaned[key] = str(value) if value is not None else ""
        if not any(v.strip() for v in cleaned.values()):
            continue  # skip fully-empty lines
        row_number += 1
        rows.append(ParsedRow(sheet_index=0, sheet_name=None, row_number=row_number, raw=cleaned))

    header = [h for h in (reader.fieldnames or []) if h and h != _UNMAPPED_KEY]
    parsed = ParsedFile(source_format="csv", parser_version=CSV_PARSER_VERSION)
    parsed.sheets = [SheetInfo(index=0, name=None, header=header, data_row_count=len(rows))]
    parsed.rows = rows
    return parsed


def parse_xlsx(content: bytes) -> ParsedFile:
    """Parse XLSX bytes into the shared shape, one entry per worksheet.

    The first non-empty row of each sheet is its header. Sheets with no header
    row contribute no rows but still appear in the inspection summary (with an
    empty header) so the operator can see they were found and skipped.
    """

    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except (BadZipFile, KeyError, OSError, ValueError) as exc:
        raise MalformedFileError(
            "The file could not be opened as an .xlsx workbook. It may be "
            "corrupted, password-protected, or mislabelled. Re-export it from "
            "the source application and upload again."
        ) from exc

    try:
        parsed = ParsedFile(source_format="xlsx", parser_version=XLSX_PARSER_VERSION)
        if not workbook.sheetnames:
            raise MalformedFileError("The workbook contains no sheets.")

        for sheet_index, sheet_name in enumerate(workbook.sheetnames):
            worksheet = workbook[sheet_name]
            header: list[str] = []
            data_row_count = 0
            row_number = 0
            for values in worksheet.iter_rows(values_only=True):
                texts = [_cell_to_text(v) for v in values]
                if not any(t.strip() for t in texts):
                    continue  # skip fully-empty rows (including leading ones)
                if not header:
                    header = [t.strip() for t in texts]
                    continue
                row_number += 1
                raw: dict[str, str] = {}
                extras: list[str] = []
                for position, text in enumerate(texts):
                    if position < len(header) and header[position]:
                        raw[header[position]] = text
                    elif text.strip():
                        extras.append(text)
                if extras:
                    raw[_UNMAPPED_KEY] = ", ".join(extras)
                data_row_count += 1
                parsed.rows.append(
                    ParsedRow(
                        sheet_index=sheet_index,
                        sheet_name=sheet_name,
                        row_number=row_number,
                        raw=raw,
                    )
                )
            parsed.sheets.append(
                SheetInfo(
                    index=sheet_index,
                    name=sheet_name,
                    header=[h for h in header if h],
                    data_row_count=data_row_count,
                )
            )

        if all(not sheet.header for sheet in parsed.sheets):
            raise MalformedFileError(
                "The workbook is empty: no sheet contains a header row. Add a "
                "header row naming the contact columns and upload again."
            )
        return parsed
    finally:
        workbook.close()


def parse_file(content: bytes, filename: str | None) -> ParsedFile:
    """Parse an uploaded file by its extension into the shared shape."""

    file_format = detect_format(filename)
    if not content:
        raise MalformedFileError("The uploaded file is empty (0 bytes).")
    if file_format == "csv":
        return parse_csv(content)
    return parse_xlsx(content)

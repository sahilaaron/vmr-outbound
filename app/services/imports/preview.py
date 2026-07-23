"""Non-committing import preview (the "validate before you commit" step).

Runs the exact same shared pipeline stages as a real import — parsing, mapping,
validation, normalization, deduplication lookups, and suppression checks — but
performs **no writes of any kind**: no contacts, no campaign memberships, no
suppressions, no batches, no rows, no outcomes. The database session is used
for read-only lookups only.

Intra-file duplicates are simulated with in-memory registers so the preview
predicts the same outcome sequence the committing importer would produce.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.services.imports import dedup, parsing, validation
from app.services.imports import mapping as mapping_service
from app.services.imports.importer import _validate_structure
from app.services.suppressions import find_active_suppression


@dataclass(frozen=True)
class PreviewProblem:
    """One actionable row-level problem found during preview."""

    sheet_index: int
    sheet_name: str | None
    row_number: int
    column: str | None
    code: str
    message: str


@dataclass(frozen=True)
class PreviewRow:
    """The predicted outcome for one row."""

    sheet_index: int
    sheet_name: str | None
    row_number: int
    outcome: str  # accepted | rejected | duplicate | suppressed | ambiguous
    raw: dict[str, str]
    normalized: dict[str, str | None]
    note: str | None


@dataclass
class PreviewResult:
    """The full non-committing preview of one staged file interpretation."""

    total_rows: int = 0
    accepted: int = 0
    rejected: int = 0
    duplicate: int = 0
    suppressed: int = 0
    ambiguous: int = 0
    structure_error: str | None = None
    rows: list[PreviewRow] = field(default_factory=list)
    problems: list[PreviewProblem] = field(default_factory=list)

    @property
    def is_importable(self) -> bool:
        return self.structure_error is None


def preview_import(
    session: Session,
    *,
    parsed: parsing.ParsedFile,
    sheet_selection: list[int] | None,
    column_mapping: dict[str, str] | None,
) -> PreviewResult:
    """Predict the outcome of importing *parsed* without persisting anything."""

    result = PreviewResult()

    structure_error = _validate_structure(parsed, sheet_selection, column_mapping)
    if structure_error is not None:
        result.structure_error = structure_error
        return result

    rows = parsed.rows_for_sheets(sheet_selection)
    result.total_rows = len(rows)

    # In-memory registers simulating contacts this import would create, so
    # intra-file duplicates are predicted the same way the importer catches them.
    seen_emails: set[str] = set()
    seen_natural_keys: dict[str, int] = {}

    for parsed_row in rows:
        source = (
            mapping_service.apply_mapping(parsed_row.raw, column_mapping)
            if column_mapping is not None
            else parsed_row.raw
        )
        validated = validation.validate_row(parsed_row.row_number, source)

        if not validated.is_valid:
            for err in validated.errors:
                result.problems.append(
                    PreviewProblem(
                        sheet_index=parsed_row.sheet_index,
                        sheet_name=parsed_row.sheet_name,
                        row_number=parsed_row.row_number,
                        column=err.column,
                        code=err.code,
                        message=err.message,
                    )
                )
            result.rejected += 1
            result.rows.append(
                PreviewRow(
                    sheet_index=parsed_row.sheet_index,
                    sheet_name=parsed_row.sheet_name,
                    row_number=parsed_row.row_number,
                    outcome="rejected",
                    raw=parsed_row.raw,
                    normalized=validated.normalized,
                    note="; ".join(e.message for e in validated.errors),
                )
            )
            continue

        normalized = validated.normalized
        email = normalized["email"]
        domain = normalized["company_domain"]
        natural_key = validated.natural_key

        outcome = "accepted"
        note: str | None = None

        suppression = find_active_suppression(session, email=email, domain=domain)
        if suppression is not None:
            outcome = "suppressed"
            note = (
                f"would be blocked by the {suppression.suppression_type.value} suppression "
                f"ledger ({suppression.reason.value})"
            )
        else:
            match = dedup.find_existing_contact(session, email=email, natural_key=natural_key)
            if match.is_match:
                outcome = "duplicate"
                note = match.note
            elif match.ambiguous:
                outcome = "ambiguous"
                note = match.note
            elif email and email in seen_emails:
                outcome = "duplicate"
                note = "duplicate of an earlier row in this file (same email)"
            elif not email and natural_key and seen_natural_keys.get(natural_key, 0) >= 1:
                outcome = "duplicate"
                note = "duplicate of an earlier row in this file (same name and domain)"

        if outcome == "accepted":
            if email:
                seen_emails.add(email)
            if natural_key:
                seen_natural_keys[natural_key] = seen_natural_keys.get(natural_key, 0) + 1

        setattr(result, outcome, getattr(result, outcome) + 1)
        result.rows.append(
            PreviewRow(
                sheet_index=parsed_row.sheet_index,
                sheet_name=parsed_row.sheet_name,
                row_number=parsed_row.row_number,
                outcome=outcome,
                raw=parsed_row.raw,
                normalized=normalized,
                note=note,
            )
        )

    return result

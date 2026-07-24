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
from typing import Any

from sqlalchemy.orm import Session

from app.models.import_batch import ImportRow
from app.services.enrichment import companies as enrichment_companies
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
    raw: dict[str, Any]
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


def _predict_row(
    session: Session,
    *,
    sheet_index: int,
    sheet_name: str | None,
    row_number: int,
    raw: dict[str, Any],
    column_mapping: dict[str, str] | None,
    result: PreviewResult,
    seen_emails: set[str],
    seen_natural_keys: dict[str, int],
    domain_overlay: dict[str, str] | None = None,
) -> None:
    """Predict one row's outcome and record it on *result* (no writes).

    This is the single, shared prediction step used for both a parsed file and an
    already-captured staged batch, so every source predicts outcomes through the
    exact same validation, suppression, and deduplication rules.

    ``domain_overlay`` (DAT-010) fills a mapped row's missing ``company_domain``
    from an operator-confirmed company domain, so the preview predicts the same
    outcome the committing importer will produce — never touching the raw row.
    """

    source = (
        mapping_service.apply_mapping(raw, column_mapping)
        if column_mapping is not None
        else dict(raw)
    )
    enrichment_companies.apply_overlay_to_source(source, domain_overlay)
    validated = validation.validate_row(row_number, source)

    if not validated.is_valid:
        for err in validated.errors:
            result.problems.append(
                PreviewProblem(
                    sheet_index=sheet_index,
                    sheet_name=sheet_name,
                    row_number=row_number,
                    column=err.column,
                    code=err.code,
                    message=err.message,
                )
            )
        result.rejected += 1
        result.rows.append(
            PreviewRow(
                sheet_index=sheet_index,
                sheet_name=sheet_name,
                row_number=row_number,
                outcome="rejected",
                raw=raw,
                normalized=validated.normalized,
                note="; ".join(e.message for e in validated.errors),
            )
        )
        return

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
            sheet_index=sheet_index,
            sheet_name=sheet_name,
            row_number=row_number,
            outcome=outcome,
            raw=raw,
            normalized=normalized,
            note=note,
        )
    )


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
        _predict_row(
            session,
            sheet_index=parsed_row.sheet_index,
            sheet_name=parsed_row.sheet_name,
            row_number=parsed_row.row_number,
            raw=parsed_row.raw,
            column_mapping=column_mapping,
            result=result,
            seen_emails=seen_emails,
            seen_natural_keys=seen_natural_keys,
        )

    return result


def preview_pending_batch(
    session: Session,
    *,
    rows: list[ImportRow],
    column_mapping: dict[str, str] | None,
    domain_overlay: dict[str, str] | None = None,
) -> PreviewResult:
    """Predict the outcome of committing an already-captured staged batch.

    Runs the immutable raw rows of a pending ``ImportBatch`` (e.g. a Sales
    Navigator capture) through the exact same per-row prediction as a spreadsheet
    preview — no writes, no contacts. There is no file-structure gate: a
    capture has no header, so a row missing a required field (Sales Navigator does
    not provide ``company_domain``) is predicted as ``rejected`` by validation,
    exactly as the committing importer would, rather than being silently skipped.

    ``domain_overlay`` (DAT-010) supplies operator-confirmed company domains so a
    resolved company's rows preview as accepted while an unresolved company's
    rows still preview as rejected — the same outcome the confirm step commits.
    """

    result = PreviewResult()
    ordered = sorted(rows, key=lambda r: (r.sheet_index, r.row_number))
    result.total_rows = len(ordered)

    seen_emails: set[str] = set()
    seen_natural_keys: dict[str, int] = {}

    for row in ordered:
        _predict_row(
            session,
            sheet_index=row.sheet_index,
            sheet_name=row.sheet_name,
            row_number=row.row_number,
            raw=dict(row.raw_data),
            column_mapping=column_mapping,
            result=result,
            seen_emails=seen_emails,
            seen_natural_keys=seen_natural_keys,
            domain_overlay=domain_overlay,
        )

    return result

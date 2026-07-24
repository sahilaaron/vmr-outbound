"""Staged import orchestration for the two authorized formats (DAT-002).

The import is deliberately staged so a malformed batch can never corrupt data:

1. **Raw capture (committed first).** A batch is created and every original row
   is written verbatim to the immutable ``import_rows`` table, then committed. If
   later processing fails, the raw capture survives for audit and re-processing.
2. **Validation + normalization + dedup + suppression (single transaction).**
   Each raw row is validated independently; rejected rows keep actionable
   row-level errors; accepted rows are normalized, de-duplicated conservatively,
   checked against the suppression ledger, and only then committed as contacts
   and campaign memberships. On any failure the whole processing transaction is
   rolled back (no partial contacts) and the batch is marked ``FAILED`` — the raw
   rows remain.
3. **Summary.** Per-row outcomes and batch counts are returned.

Re-running the exact same file into the same campaign is idempotent: an identical
completed batch (same content, sheet selection, and column mapping) short-
circuits, and overlapping-but-not-identical batches are reconciled by
deduplication rather than creating duplicate contacts.

CSV and XLSX flow through one pipeline: the format-specific work ends at
:mod:`app.services.imports.parsing`, which renders both formats into the same
neutral rows; everything after that (mapping, validation, normalization,
deduplication, provenance, suppression, persistence) is shared.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.enums import (
    ContactWorkflowState,
    ImportBatchStatus,
    ImportRowOutcome,
    ImportSourceFormat,
)
from app.models.import_batch import ImportBatch, ImportRow, ImportRowError, ImportRowValidation
from app.models.provenance import ProvenanceRecord
from app.services.audit import record_audit_event
from app.services.contact_state import transition_contact_state
from app.services.imports import dedup, parsing, validation
from app.services.imports import mapping as mapping_service
from app.services.suppressions import find_active_suppression

_UNMAPPED_KEY = "_unmapped"
_TERMINAL_STATES = frozenset({ContactWorkflowState.SUPPRESSED, ContactWorkflowState.EXCLUDED})


class FeatureDisabledError(Exception):
    """Raised when CSV import is attempted while the feature switch is off."""


class CampaignNotFound(Exception):
    """Raised when the target campaign does not exist."""


class BatchNotProcessable(Exception):
    """Raised when a batch cannot be processed in place (wrong status)."""


@dataclass(frozen=True)
class BatchProvenance:
    """Operator-supplied provenance captured once for a whole batch."""

    source_name: str | None = None
    source_reference: str | None = None
    exported_by: str | None = None
    exported_at: date | None = None


@dataclass
class ImportSummary:
    """The result of an import run."""

    batch_id: uuid.UUID
    status: ImportBatchStatus
    total_rows: int
    accepted_rows: int
    rejected_rows: int
    duplicate_rows: int
    suppressed_rows: int
    ambiguous_rows: int
    contacts_created: int
    reused_existing_batch: bool = False
    error_detail: str | None = None


def _validate_structure(
    parsed: parsing.ParsedFile,
    sheet_selection: list[int] | None,
    column_mapping: dict[str, str] | None,
) -> str | None:
    """Return an actionable batch-level error if the file structure is unusable.

    A file is rejected outright — never treated as a completed import — when it
    has no usable header, when any selected sheet is missing a required column
    after mapping (which also catches a headerless file, whose first data line
    becomes a pseudo-header lacking the required names), or when the selection
    contains no data rows (DAT-002 contract). The check runs per selected sheet
    so a multi-sheet workbook fails with the exact sheet named.
    """

    sheets = [s for s in parsed.sheets if sheet_selection is None or s.index in sheet_selection]
    if not sheets:
        return "No sheet was selected for import."

    label = "CSV" if parsed.source_format == "csv" else "Workbook"

    for sheet in sheets:
        where = f"sheet {sheet.name!r}" if sheet.name is not None else "the file"
        if not sheet.header:
            return f"{label}: {where} has no header row (it is empty or unreadable)."

        if column_mapping is not None:
            mapped_targets = {
                target for column, target in column_mapping.items() if column in set(sheet.header)
            }
            missing = [c for c in validation.REQUIRED_COLUMNS if c not in mapped_targets]
            if missing:
                return (
                    f"{label}: {where} is missing a mapped source column for required "
                    f"field(s): {', '.join(missing)}. Adjust the column mapping or the file."
                )
        else:
            present = {h.strip().lower() for h in sheet.header if h and h != _UNMAPPED_KEY}
            missing = [c for c in validation.REQUIRED_COLUMNS if c not in present]
            if missing:
                found = ", ".join(sorted(present)) or "none"
                return (
                    f"{label}: {where} is missing required column(s): "
                    f"{', '.join(missing)}. Columns found: {found}. "
                    "The header must name first_name, last_name, "
                    "company_name, and company_domain."
                )

    if not parsed.rows_for_sheets(sheet_selection):
        return f"{label}: the selected sheet(s) have a valid header but no data rows."

    return None


def _content_hash(
    content: bytes,
    sheet_selection: list[int] | None = None,
    column_mapping: dict[str, str] | None = None,
) -> str:
    """Hash identifying one *interpretation* of one uploaded file.

    The same bytes confirmed with a different sheet selection or column mapping
    are a different import; the same bytes with the same interpretation are the
    same import (idempotent confirm). A plain CSV import with no explicit
    selection or mapping hashes to the raw content hash, unchanged from DAT-002.
    """

    digest = hashlib.sha256(content)
    if sheet_selection is not None or column_mapping is not None:
        extras = {
            "sheets": sorted(sheet_selection) if sheet_selection is not None else None,
            "mapping": dict(sorted(column_mapping.items())) if column_mapping else None,
        }
        digest.update(json.dumps(extras, sort_keys=True).encode("utf-8"))
    return digest.hexdigest()


def _summary_from_batch(batch: ImportBatch, *, reused: bool = False) -> ImportSummary:
    return ImportSummary(
        batch_id=batch.id,
        status=batch.status,
        total_rows=batch.total_rows,
        accepted_rows=batch.accepted_rows,
        rejected_rows=batch.rejected_rows,
        duplicate_rows=batch.duplicate_rows,
        suppressed_rows=batch.suppressed_rows,
        ambiguous_rows=batch.ambiguous_rows,
        contacts_created=batch.contacts_created,
        reused_existing_batch=reused,
        error_detail=batch.error_detail,
    )


def _resolve_provenance(
    normalized: dict[str, str | None], provenance: BatchProvenance
) -> dict[str, Any]:
    """Row-level provenance columns override the batch defaults when present."""

    exported_at: date | None = provenance.exported_at
    row_exported_at = normalized.get("exported_at")
    if row_exported_at:
        exported_at = date.fromisoformat(row_exported_at)
    return {
        "source_name": normalized.get("source_name") or provenance.source_name,
        "source_reference": normalized.get("source_reference") or provenance.source_reference,
        "exported_by": normalized.get("exported_by") or provenance.exported_by,
        "exported_at": exported_at,
    }


def _get_membership(
    session: Session, campaign_id: uuid.UUID, contact_id: uuid.UUID
) -> CampaignContact | None:
    return session.scalars(
        select(CampaignContact).where(
            CampaignContact.campaign_id == campaign_id,
            CampaignContact.contact_id == contact_id,
        )
    ).first()


def _create_membership(
    session: Session,
    *,
    campaign_id: uuid.UUID,
    contact_id: uuid.UUID,
    batch_id: uuid.UUID,
    state: ContactWorkflowState,
) -> CampaignContact:
    membership = CampaignContact(
        campaign_id=campaign_id,
        contact_id=contact_id,
        source_batch_id=batch_id,
        state=state,
    )
    session.add(membership)
    session.flush()
    return membership


def _suppress_all_memberships(
    session: Session,
    *,
    contact_id: uuid.UUID,
    current_campaign_id: uuid.UUID,
    batch_id: uuid.UUID,
    actor: str,
) -> None:
    """Suppress a contact across every campaign it belongs to.

    Transitions each non-terminal membership for the contact to SUPPRESSED
    (audited) and guarantees the campaign currently being imported also carries a
    suppressed membership. The suppression ledger remains the authority; this
    only propagates that authority to the contact's memberships so it cannot stay
    eligible in another campaign.
    """

    memberships = session.scalars(
        select(CampaignContact).where(CampaignContact.contact_id == contact_id)
    ).all()

    seen_current = False
    for membership in memberships:
        if membership.campaign_id == current_campaign_id:
            seen_current = True
        if membership.state not in _TERMINAL_STATES:
            transition_contact_state(
                session,
                membership,
                target=ContactWorkflowState.SUPPRESSED,
                actor=actor,
                reason="suppressed identity observed during import (all campaigns)",
            )

    if not seen_current:
        _create_membership(
            session,
            campaign_id=current_campaign_id,
            contact_id=contact_id,
            batch_id=batch_id,
            state=ContactWorkflowState.SUPPRESSED,
        )


def _append_provenance(
    session: Session,
    *,
    contact_id: uuid.UUID,
    batch_id: uuid.UUID,
    row_id: uuid.UUID,
    resolved: dict[str, Any],
) -> None:
    session.add(
        ProvenanceRecord(
            contact_id=contact_id,
            import_batch_id=batch_id,
            import_row_id=row_id,
            source_name=resolved["source_name"],
            source_reference=resolved["source_reference"],
            exported_by=resolved["exported_by"],
            exported_at=resolved["exported_at"],
        )
    )


def _create_contact(
    session: Session, normalized: dict[str, str | None], natural_key: str
) -> Contact:
    # Required identity fields are guaranteed non-None on a valid row; narrow the
    # optional dict values for the type checker.
    first_name = normalized["first_name"]
    last_name = normalized["last_name"]
    company_name = normalized["company_name"]
    company_domain = normalized["company_domain"]
    assert first_name is not None
    assert last_name is not None
    assert company_name is not None
    assert company_domain is not None
    contact = Contact(
        first_name=first_name,
        last_name=last_name,
        company_name=company_name,
        company_domain=company_domain,
        email=normalized["email"],
        title=normalized["title"],
        linkedin_url=normalized["linkedin_url"],
        country=normalized["country"],
        industry=normalized["industry"],
        company_size=normalized["company_size"],
        natural_key=natural_key,
    )
    session.add(contact)
    session.flush()
    return contact


class _Counts:
    """Mutable running tally of per-row outcomes for the batch summary."""

    def __init__(self) -> None:
        self.accepted = 0
        self.rejected = 0
        self.duplicate = 0
        self.suppressed = 0
        self.ambiguous = 0
        self.contacts_created = 0


def _process_row(
    session: Session,
    *,
    campaign: Campaign,
    batch: ImportBatch,
    import_row: ImportRow,
    validated: validation.ValidatedRow,
    provenance: BatchProvenance,
    actor: str,
    counts: _Counts,
) -> None:
    """Validate, normalize, dedup, suppression-check, and persist a single row."""

    # 1. Rejected rows: keep the raw row and record actionable errors, no contact.
    if not validated.is_valid:
        result = ImportRowValidation(import_row_id=import_row.id, outcome=ImportRowOutcome.REJECTED)
        session.add(result)
        for err in validated.errors:
            session.add(
                ImportRowError(
                    import_row_id=import_row.id,
                    column_name=err.column,
                    code=err.code,
                    message=err.message,
                )
            )
        counts.rejected += 1
        return

    normalized = validated.normalized
    natural_key = validated.natural_key
    assert natural_key is not None  # guaranteed for a valid row
    email = normalized["email"]
    domain = normalized["company_domain"]
    resolved_provenance = _resolve_provenance(normalized, provenance)

    suppression = find_active_suppression(session, email=email, domain=domain)
    match = dedup.find_existing_contact(session, email=email, natural_key=natural_key)

    # 2. Suppressed identity: never produces an eligible membership. If a contact
    #    already exists here and is eligible, actively suppress it so it cannot
    #    silently stay eligible after being added to the ledger.
    if suppression is not None:
        contact = match.contact
        note = (
            f"suppressed by {suppression.suppression_type.value} ledger entry "
            f"({suppression.reason.value})"
        )
        result = ImportRowValidation(
            import_row_id=import_row.id,
            outcome=ImportRowOutcome.SUPPRESSED,
            contact_id=contact.id if contact is not None else None,
            suppression_id=suppression.id,
            normalized_data=dict(normalized),
            note=note,
        )
        session.add(result)
        if contact is not None:
            # A suppressed identity must not stay eligible in ANY campaign. Move
            # every non-terminal membership for this contact (across all
            # campaigns) to SUPPRESSED, and ensure the campaign being imported
            # also carries a suppressed membership. The ledger stays authoritative.
            _suppress_all_memberships(
                session,
                contact_id=contact.id,
                current_campaign_id=campaign.id,
                batch_id=batch.id,
                actor=actor,
            )
            _append_provenance(
                session,
                contact_id=contact.id,
                batch_id=batch.id,
                row_id=import_row.id,
                resolved=resolved_provenance,
            )
        counts.suppressed += 1
        return

    # 3. Duplicate of an existing contact (not suppressed): link, do not re-create.
    if match.is_match and match.contact is not None:
        contact = match.contact
        result = ImportRowValidation(
            import_row_id=import_row.id,
            outcome=ImportRowOutcome.DUPLICATE,
            contact_id=contact.id,
            match_type=match.match_type,
            normalized_data=dict(normalized),
            note=match.note,
        )
        session.add(result)
        if _get_membership(session, campaign.id, contact.id) is None:
            _create_membership(
                session,
                campaign_id=campaign.id,
                contact_id=contact.id,
                batch_id=batch.id,
                state=ContactWorkflowState.IMPORTED,
            )
        _append_provenance(
            session,
            contact_id=contact.id,
            batch_id=batch.id,
            row_id=import_row.id,
            resolved=resolved_provenance,
        )
        counts.duplicate += 1
        return

    # 4. Ambiguous identity: several existing contacts share this row's natural
    #    key, so a merge target cannot be chosen safely. The row is neither
    #    merged nor silently accepted — it becomes an explicit, reviewable
    #    outcome with no contact and no campaign membership (DAT-004).
    if match.ambiguous:
        result = ImportRowValidation(
            import_row_id=import_row.id,
            outcome=ImportRowOutcome.AMBIGUOUS,
            normalized_data=dict(normalized),
            note=match.note,
        )
        session.add(result)
        record_audit_event(
            session,
            actor=actor,
            action="import.row_ambiguous",
            entity_type="import_row",
            entity_id=str(import_row.id),
            new_state=ImportRowOutcome.AMBIGUOUS.value,
            reason=match.note or "ambiguous identity match",
            context={"batch_id": str(batch.id), "row_number": import_row.row_number},
        )
        counts.ambiguous += 1
        return

    # 5. Accepted: a new contact.
    contact = _create_contact(session, normalized, natural_key)
    result = ImportRowValidation(
        import_row_id=import_row.id,
        outcome=ImportRowOutcome.ACCEPTED,
        contact_id=contact.id,
        normalized_data=dict(normalized),
    )
    session.add(result)
    _create_membership(
        session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        batch_id=batch.id,
        state=ContactWorkflowState.IMPORTED,
    )
    _append_provenance(
        session,
        contact_id=contact.id,
        batch_id=batch.id,
        row_id=import_row.id,
        resolved=resolved_provenance,
    )
    record_audit_event(
        session,
        actor=actor,
        action="contact.created",
        entity_type="contact",
        entity_id=str(contact.id),
        new_state=ContactWorkflowState.IMPORTED.value,
        reason="contact created from authorized import",
        context={"batch_id": str(batch.id), "row_number": import_row.row_number},
    )
    counts.accepted += 1
    counts.contacts_created += 1


def run_import(
    session: Session,
    *,
    campaign_id: uuid.UUID,
    content: bytes,
    filename: str | None = None,
    provenance: BatchProvenance | None = None,
    sheet_selection: list[int] | None = None,
    column_mapping: dict[str, str] | None = None,
    mime_type: str | None = None,
    actor: str = "importer",
    _fault: Callable[[], None] | None = None,
) -> ImportSummary:
    """Run a staged CSV/XLSX import into *campaign_id* and return a summary.

    ``sheet_selection`` restricts an XLSX import to deliberately chosen sheets
    (all sheets when None; ignored for CSV, which is a single sheet).
    ``column_mapping`` is the operator-confirmed ``source column -> system
    field`` mapping; when None the file's own header must already match the
    contact-input contract (the DAT-002 behaviour, unchanged).

    ``_fault`` is a test-only hook invoked after all rows are processed but before
    the processing transaction commits, used to prove rollback and recovery.
    """

    if not get_settings().features.csv_import:
        raise FeatureDisabledError("CSV import is disabled (FEATURES__CSV_IMPORT is off).")

    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise CampaignNotFound(f"campaign {campaign_id} does not exist")

    provenance = provenance or BatchProvenance()
    content_hash = _content_hash(content, sheet_selection, column_mapping)

    # Idempotent retry: an identical completed batch (same content and same
    # interpretation) for this campaign is reused, never duplicated.
    existing = session.scalars(
        select(ImportBatch).where(
            ImportBatch.campaign_id == campaign_id,
            ImportBatch.content_hash == content_hash,
            ImportBatch.status == ImportBatchStatus.COMPLETED,
        )
    ).first()
    if existing is not None:
        return _summary_from_batch(existing, reused=True)

    # --- Parse (format-specific work ends here) ------------------------------
    parse_error: str | None = None
    try:
        if filename is not None:
            parsed = parsing.parse_file(content, filename)
        else:
            parsed = parsing.parse_csv(content)  # DAT-002 default: raw CSV body
    except (parsing.UnsupportedFormatError, parsing.MalformedFileError) as exc:
        # A file that cannot be parsed still produces a visible FAILED batch —
        # never a silent drop and never a completed import.
        parsed = parsing.ParsedFile(source_format="csv", parser_version="unparsed")
        if filename is not None and filename.lower().endswith(".xlsx"):
            parsed.source_format = "xlsx"
        parse_error = str(exc)

    selected_rows = parsed.rows_for_sheets(sheet_selection)

    # --- Stage 1: durable raw capture ---------------------------------------
    batch = ImportBatch(
        campaign_id=campaign_id,
        filename=filename,
        content_hash=content_hash,
        status=ImportBatchStatus.VALIDATING,
        source_format=(
            ImportSourceFormat.XLSX if parsed.source_format == "xlsx" else ImportSourceFormat.CSV
        ),
        mime_type=mime_type,
        parser_version=parsed.parser_version,
        mapper_version=mapping_service.MAPPER_VERSION if column_mapping is not None else None,
        column_mapping=dict(column_mapping) if column_mapping is not None else None,
        source_name=provenance.source_name,
        source_reference=provenance.source_reference,
        exported_by=provenance.exported_by,
        exported_at=provenance.exported_at,
        total_rows=len(selected_rows),
    )
    session.add(batch)
    session.flush()
    record_audit_event(
        session,
        actor=actor,
        action="import.batch_created",
        entity_type="import_batch",
        entity_id=str(batch.id),
        new_state=ImportBatchStatus.VALIDATING.value,
        reason="authorized import received",
        context={
            "campaign_id": str(campaign_id),
            "total_rows": len(selected_rows),
            "filename": filename,
            "source_format": parsed.source_format,
        },
    )
    import_rows: list[tuple[ImportRow, dict[str, str]]] = []
    for parsed_row in selected_rows:
        import_row = ImportRow(
            batch_id=batch.id,
            row_number=parsed_row.row_number,
            sheet_index=parsed_row.sheet_index,
            sheet_name=parsed_row.sheet_name,
            raw_data=parsed_row.raw,
        )
        session.add(import_row)
        import_rows.append((import_row, parsed_row.raw))
    session.flush()
    batch_id = batch.id
    session.commit()  # raw capture is now durable even if processing fails

    # --- Structure gate: an unusable file never becomes a completed import ----
    structure_error = parse_error or _validate_structure(parsed, sheet_selection, column_mapping)
    if structure_error is not None:
        batch.status = ImportBatchStatus.FAILED
        batch.error_detail = structure_error
        record_audit_event(
            session,
            actor=actor,
            action="import.failed",
            entity_type="import_batch",
            entity_id=str(batch.id),
            previous_state=ImportBatchStatus.VALIDATING.value,
            new_state=ImportBatchStatus.FAILED.value,
            reason=structure_error,
        )
        session.commit()
        return _summary_from_batch(batch)

    # --- Stage 2: validation + persistence (atomic) --------------------------
    counts = _Counts()
    try:
        for import_row, raw in import_rows:
            source = (
                mapping_service.apply_mapping(raw, column_mapping)
                if column_mapping is not None
                else raw
            )
            validated = validation.validate_row(import_row.row_number, source)
            _process_row(
                session,
                campaign=campaign,
                batch=batch,
                import_row=import_row,
                validated=validated,
                provenance=provenance,
                actor=actor,
                counts=counts,
            )
        if _fault is not None:
            _fault()

        batch.status = ImportBatchStatus.COMPLETED
        batch.accepted_rows = counts.accepted
        batch.rejected_rows = counts.rejected
        batch.duplicate_rows = counts.duplicate
        batch.suppressed_rows = counts.suppressed
        batch.ambiguous_rows = counts.ambiguous
        batch.contacts_created = counts.contacts_created
        batch.completed_at = datetime.now(UTC)
        record_audit_event(
            session,
            actor=actor,
            action="import.completed",
            entity_type="import_batch",
            entity_id=str(batch.id),
            previous_state=ImportBatchStatus.VALIDATING.value,
            new_state=ImportBatchStatus.COMPLETED.value,
            reason="import processed",
            context={
                "accepted": counts.accepted,
                "rejected": counts.rejected,
                "duplicate": counts.duplicate,
                "suppressed": counts.suppressed,
                "ambiguous": counts.ambiguous,
                "contacts_created": counts.contacts_created,
            },
        )
        session.commit()
        return _summary_from_batch(batch)
    except Exception as exc:
        # Roll back all processing work; no partial contacts are committed.
        session.rollback()
        failed = session.get(ImportBatch, batch_id)
        if failed is not None:
            failed.status = ImportBatchStatus.FAILED
            failed.error_detail = f"{type(exc).__name__}: {exc}"
            record_audit_event(
                session,
                actor=actor,
                action="import.failed",
                entity_type="import_batch",
                entity_id=str(failed.id),
                previous_state=ImportBatchStatus.VALIDATING.value,
                new_state=ImportBatchStatus.FAILED.value,
                reason=failed.error_detail,
            )
            session.commit()
        raise


def process_pending_batch(
    session: Session,
    *,
    batch: ImportBatch,
    column_mapping: dict[str, str] | None,
    actor: str = "workbench",
    _fault: Callable[[], None] | None = None,
) -> ImportSummary:
    """Process an already raw-captured PENDING batch into contacts, in place.

    This is stage 2 of the staged import applied to a batch whose immutable raw
    rows already exist (e.g. a Sales Navigator capture staged by DAT-009). It runs
    the SAME per-row processing as :func:`run_import` — the shared
    :func:`_process_row` — so validation, normalization, deduplication,
    suppression, ambiguity handling, provenance, and contact creation are
    identical to a spreadsheet import; nothing is bypassed. The batch transitions
    ``pending -> completed`` (or ``failed`` on error, rolling back all processing
    work). It creates a second import pipeline nowhere: only the entry point
    differs (an existing batch vs. a freshly parsed file).

    Idempotent: a batch that is already ``completed`` is returned unchanged.
    """

    if not get_settings().features.csv_import:
        raise FeatureDisabledError("CSV import is disabled (FEATURES__CSV_IMPORT is off).")

    # Idempotent confirm: an already-processed batch is returned as-is.
    if batch.status == ImportBatchStatus.COMPLETED:
        return _summary_from_batch(batch, reused=True)
    if batch.status != ImportBatchStatus.PENDING:
        raise BatchNotProcessable(
            f"batch {batch.id} is {batch.status.value}, not pending; it cannot be processed."
        )

    campaign = session.get(Campaign, batch.campaign_id)
    if campaign is None:
        raise CampaignNotFound(f"campaign {batch.campaign_id} does not exist")

    import_rows = list(
        session.scalars(
            select(ImportRow)
            .where(ImportRow.batch_id == batch.id)
            .order_by(ImportRow.sheet_index, ImportRow.row_number)
        ).all()
    )

    # Batch-level provenance was captured at staging time; reuse it verbatim.
    provenance = BatchProvenance(
        source_name=batch.source_name,
        source_reference=batch.source_reference,
        exported_by=batch.exported_by,
        exported_at=batch.exported_at,
    )

    # Record the operator-confirmed mapping on the batch so its interpretation is
    # reproducible (same column as a spreadsheet import).
    batch.status = ImportBatchStatus.VALIDATING
    batch.column_mapping = dict(column_mapping) if column_mapping is not None else None
    batch.mapper_version = mapping_service.MAPPER_VERSION if column_mapping is not None else None
    session.flush()

    counts = _Counts()
    try:
        for import_row in import_rows:
            raw = dict(import_row.raw_data)
            source = (
                mapping_service.apply_mapping(raw, column_mapping)
                if column_mapping is not None
                else raw
            )
            validated = validation.validate_row(import_row.row_number, source)
            _process_row(
                session,
                campaign=campaign,
                batch=batch,
                import_row=import_row,
                validated=validated,
                provenance=provenance,
                actor=actor,
                counts=counts,
            )
        if _fault is not None:
            _fault()

        batch.status = ImportBatchStatus.COMPLETED
        batch.accepted_rows = counts.accepted
        batch.rejected_rows = counts.rejected
        batch.duplicate_rows = counts.duplicate
        batch.suppressed_rows = counts.suppressed
        batch.ambiguous_rows = counts.ambiguous
        batch.contacts_created = counts.contacts_created
        batch.completed_at = datetime.now(UTC)
        record_audit_event(
            session,
            actor=actor,
            action="import.completed",
            entity_type="import_batch",
            entity_id=str(batch.id),
            previous_state=ImportBatchStatus.PENDING.value,
            new_state=ImportBatchStatus.COMPLETED.value,
            reason="staged batch processed after operator confirmation",
            context={
                "accepted": counts.accepted,
                "rejected": counts.rejected,
                "duplicate": counts.duplicate,
                "suppressed": counts.suppressed,
                "ambiguous": counts.ambiguous,
                "contacts_created": counts.contacts_created,
                "source_format": batch.source_format.value,
            },
        )
        session.commit()
        return _summary_from_batch(batch)
    except Exception as exc:
        session.rollback()
        failed = session.get(ImportBatch, batch.id)
        if failed is not None:
            failed.status = ImportBatchStatus.FAILED
            failed.error_detail = f"{type(exc).__name__}: {exc}"
            record_audit_event(
                session,
                actor=actor,
                action="import.failed",
                entity_type="import_batch",
                entity_id=str(failed.id),
                previous_state=ImportBatchStatus.VALIDATING.value,
                new_state=ImportBatchStatus.FAILED.value,
                reason=failed.error_detail,
            )
            session.commit()
        raise

"""Import-batch models: batch, immutable raw rows, validation results, errors.

The staged import splits three distinct concerns into three tables (DAT-002):

* :class:`ImportBatch` — one row per CSV upload, with batch-level provenance and
  the import summary counts.
* :class:`ImportRow` — the **immutable** raw capture of every original CSV row,
  written once before any transformation and never updated.
* :class:`ImportRowValidation` — the per-row outcome (accepted / rejected /
  duplicate / suppressed) plus the normalized view and any contact link.
* :class:`ImportRowError` — zero or more actionable, row-level validation errors.

Separating the immutable raw row from its mutable processing result guarantees
no malformed row is silently discarded: every raw row is retained and every raw
row has exactly one outcome.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import (
    DedupMatchType,
    ImportBatchStatus,
    ImportRowOutcome,
    ImportSourceFormat,
)


class ImportBatch(Base):
    """A single authorized CSV upload into one campaign."""

    __tablename__ = "import_batches"
    __table_args__ = (
        Index("ix_import_batches_campaign_id", "campaign_id"),
        # Content hash lets the importer recognise an identical re-upload and
        # keep retries idempotent.
        Index("ix_import_batches_content_hash", "content_hash"),
        # A Sales Navigator capture batch is idempotent on the extension-minted
        # ``client_batch_id`` (DAT-009). The unique constraint makes a duplicate
        # submission fail at the database, not only in application code. Spreadsheet
        # batches leave this NULL, and PostgreSQL treats NULLs as distinct, so the
        # constraint never affects CSV/XLSX imports.
        UniqueConstraint("client_batch_id", name="uq_import_batches_client_batch_id"),
        Index("ix_import_batches_client_batch_id", "client_batch_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
    )
    filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # File checksum/hash of the original upload (also drives idempotent retry).
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[ImportBatchStatus] = mapped_column(
        Enum(ImportBatchStatus, name="import_batch_status"),
        nullable=False,
        default=ImportBatchStatus.PENDING,
    )

    # --- Import-format metadata (CSV or XLSX; DAT-001) -----------------------
    # The import system is not CSV-only: the first launch supports CSV and XLSX.
    # Defaults to CSV so the existing importer keeps working unchanged.
    source_format: Mapped[ImportSourceFormat] = mapped_column(
        Enum(ImportSourceFormat, name="import_source_format"),
        nullable=False,
        default=ImportSourceFormat.CSV,
        server_default=ImportSourceFormat.CSV.name,
    )
    # --- Recognized source schema (IMP-001) ----------------------------------
    #
    # Deliberately a column rather than a new ``ImportSourceFormat`` member. The
    # format is how the bytes were encoded (csv/xlsx); the schema is whose export
    # it is (``apollo_contact_export_v1``). Collapsing them would have made
    # "an Apollo export saved as CSV" unrepresentable, and adding a member to a
    # live PostgreSQL enum is a one-way migration besides.
    #
    # NULL on every pre-IMP-001 batch and on the generic contact-contract import,
    # which recognizes no schema and asks the operator to map columns instead.
    source_schema: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The worksheet the operator selected, when the workbook had more than one.
    selected_sheet_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    selected_sheet_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: The upload's filename after sanitization, stored beside the original so a
    #: hostile name is visible as submitted and safe as used.
    sanitized_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: The header row as detected, verbatim. Lets a later reader see exactly what
    #: the file offered without re-parsing the bytes.
    detected_headers: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    #: Who uploaded it, where the deployment knows. Single-operator today.
    uploaded_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: When the operator confirmed the preview — the first durable-mutation point.
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Rows that resolved to a Contact already in this Campaign. Counted apart
    #: from ``duplicate_rows`` because "this person is already here" and "this
    #: file lists them twice" call for different operator actions.
    already_in_campaign_rows: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    parser_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    mapper_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # The operator-confirmed column mapping (source column -> system field) that
    # was applied to this batch, so a batch's interpretation is reproducible.
    column_mapping: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # --- Sales Navigator capture provenance (DAT-009) ------------------------
    # The extension-minted idempotency key for a Sales Navigator capture batch.
    # NULL for spreadsheet imports. A re-submission with the same key returns the
    # existing staged batch instead of creating a second one.
    client_batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Verbatim batch-level provenance from the capture extension (schema version,
    # source, capture timestamp, search URL, and the raw extraction_metadata
    # object). Stored as received so no extension provenance is lost. Never holds
    # LinkedIn credentials, cookies, or secrets — the contract forbids them and
    # the endpoint only persists the fields defined by the intake schema.
    source_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # --- Batch-level provenance (contact-input contract) ---------------------
    source_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_reference: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    exported_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    exported_at: Mapped[date | None] = mapped_column(Date, nullable=True)

    # --- Import summary counts ----------------------------------------------
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accepted_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    suppressed_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ambiguous_rows: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    contacts_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"ImportBatch(id={self.id!r}, campaign_id={self.campaign_id!r}, "
            f"status={self.status.value!r}, total_rows={self.total_rows!r})"
        )


class ImportRow(Base):
    """Immutable, verbatim capture of one original CSV/XLSX row.

    Written once at the raw-capture stage. ``raw_data`` is never mutated, so the
    original imported values are always available for audit and re-processing. For
    an XLSX workbook a row is identified by its sheet and its original per-sheet
    row number; a flat CSV is represented as a single sheet (``sheet_index`` 0).
    """

    __tablename__ = "import_rows"
    __table_args__ = (
        # A row is unique within its (batch, sheet). CSV rows all use sheet 0, so
        # this preserves the original per-batch uniqueness for flat files.
        UniqueConstraint(
            "batch_id", "sheet_index", "row_number", name="uq_import_rows_batch_sheet_row"
        ),
        Index("ix_import_rows_batch_id", "batch_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("import_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Original row number within its sheet (header excluded). Per-file for CSV.
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Sheet identity (XLSX). CSV is a single sheet: index 0, name NULL.
    sheet_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    sheet_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The original row exactly as read (header -> raw string value). Immutable.
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"ImportRow(batch_id={self.batch_id!r}, row_number={self.row_number!r})"


class ImportRowValidation(Base):
    """The single processing outcome for one raw row."""

    __tablename__ = "import_row_validations"
    __table_args__ = (
        UniqueConstraint("import_row_id", name="uq_import_row_validations_row"),
        Index("ix_import_row_validations_outcome", "outcome"),
        Index("ix_import_row_validations_contact_id", "contact_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    import_row_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("import_rows.id", ondelete="CASCADE"),
        nullable=False,
    )
    outcome: Mapped[ImportRowOutcome] = mapped_column(
        Enum(ImportRowOutcome, name="import_row_outcome"),
        nullable=False,
    )
    # Normalized view of the row (present when accepted or duplicate).
    normalized_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # The contact created (accepted) or matched (duplicate); null otherwise.
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True,
    )
    # How a duplicate was matched (null unless outcome == duplicate).
    match_type: Mapped[DedupMatchType | None] = mapped_column(
        Enum(DedupMatchType, name="dedup_match_type"),
        nullable=True,
    )
    # The suppression entry that blocked the row (null unless suppressed).
    suppression_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("suppressions.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Human-readable explanation (which contact matched, ambiguity, etc.).
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Campaign-bound file import result detail (IMP-001) -------------------
    #
    # All nullable, all NULL for the pre-IMP-001 contact-contract importer, which
    # resolves no Company and creates no imported-email evidence. Added to this
    # table rather than a parallel one because a row already has exactly one
    # outcome here, and a second outcome table would be a second place to look
    # for the same question.
    #
    #: Deterministic fingerprint of the identity-bearing cells of the raw row.
    #: Two files that state the same person the same way share it, which is what
    #: makes re-importing an edited file process only what actually changed.
    row_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The permanent Company the row resolved to, when it resolved to one.
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: The Campaign membership the row produced or reused. Both foreign keys
    #: below are named explicitly because the naming convention would generate
    #: identifiers at or beyond PostgreSQL's 63-byte limit, where the server
    #: truncates silently and `alembic check` then reports a permanent drift.
    campaign_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "campaign_contacts.id",
            ondelete="SET NULL",
            name="fk_import_row_validations_campaign_contact",
        ),
        nullable=True,
    )
    #: The accepted imported primary address, when one was accepted.
    imported_email_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "imported_contact_emails.id",
            ondelete="SET NULL",
            name="fk_import_row_validations_imported_email",
        ),
        nullable=True,
    )
    #: Which evidence decided the Contact, e.g. ``normalized_email``,
    #: ``apollo_contact_id``, ``linkedin_profile_url``, ``created``.
    contact_match_basis: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Which evidence decided the Company, following the IMP-001 hierarchy.
    company_match_basis: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Whether the membership was created by this row or already existed.
    membership_action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Non-fatal, operator-visible observations about this row. Each entry is a
    #: ``{"code": ..., "message": ...}`` object. A warning never blocks an import;
    #: it is a fact the operator should know, kept where they can see it.
    warnings: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    #: Stable machine code for the single reason a row failed or needs review.
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"ImportRowValidation(import_row_id={self.import_row_id!r}, "
            f"outcome={self.outcome.value!r})"
        )


class ImportRowError(Base):
    """One actionable, row-level validation error."""

    __tablename__ = "import_row_errors"
    __table_args__ = (Index("ix_import_row_errors_import_row_id", "import_row_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    import_row_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("import_rows.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The offending column, when the error is column-specific.
    column_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Stable machine code, e.g. "missing_required", "invalid_domain".
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    # Actionable message including the row context.
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"ImportRowError(code={self.code!r}, column={self.column_name!r})"

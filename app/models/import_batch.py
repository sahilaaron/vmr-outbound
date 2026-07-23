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

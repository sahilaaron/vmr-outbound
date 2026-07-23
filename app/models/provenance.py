"""Provenance / source records.

Every time a contact value is observed in an import, a provenance record is
appended (never overwritten). Each record ties a contact to the exact batch and
raw row that observed it, along with the operator-supplied source metadata and
the observation time. Appending rather than overwriting means an older import
can never silently replace newer evidence (DAT-005).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ProvenanceRecord(Base):
    """One observation of a contact from one authorized import row."""

    __tablename__ = "provenance_records"
    __table_args__ = (
        Index("ix_provenance_records_contact_id", "contact_id"),
        Index("ix_provenance_records_import_batch_id", "import_batch_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    import_batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("import_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    import_row_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("import_rows.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Snapshot of the source metadata at observation time (from the batch).
    source_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_reference: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    exported_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    exported_at: Mapped[date | None] = mapped_column(Date, nullable=True)

    # When the system observed this value (import time).
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"ProvenanceRecord(contact_id={self.contact_id!r}, "
            f"import_batch_id={self.import_batch_id!r})"
        )

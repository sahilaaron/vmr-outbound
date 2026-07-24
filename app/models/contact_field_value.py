"""Field-level value provenance and freshness ledger (DAT-005).

:class:`~app.models.provenance.ProvenanceRecord` answers *"which imports observed
this contact?"* — one row per observation of a whole contact. It does not answer
*"why is the ``title`` currently shown the value it is?"* when two imports disagree
about that one field. :class:`ContactFieldValue` closes that gap: it records every
observed value of every tracked **operational field**, so each field carries its
own append-only history.

Every observation preserves:

* the value observed;
* where it came from (source metadata + import batch/row, or a manual override);
* when the source observed it (``observed_at``) and when the system ingested it
  (``ingested_at``);
* confidence, when the source supplies one;
* whether it is the current winner, and — for the whole set — the deterministic,
  versioned freshness policy that made it win.

The table is append-only: a new observation never edits or deletes an older one,
so an older import can never silently erase newer evidence, and superseded values
remain fully auditable. Exactly one observation per (contact, field) is the
current winner; the winning value is always reproducible from the stored rows by
re-running the freshness policy (see :mod:`app.services.provenance.freshness`).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ContactFieldValue(Base):
    """One observation of one operational field of one contact (append-only)."""

    __tablename__ = "contact_field_values"
    __table_args__ = (
        Index("ix_contact_field_values_contact_field", "contact_id", "field_name"),
        # Exactly one current winner per (contact, field). Enforced at the
        # database as a partial unique index so the "current value" is never
        # ambiguous, however many observations accumulate.
        Index(
            "uq_contact_field_values_winner",
            "contact_id",
            "field_name",
            unique=True,
            postgresql_where="is_current_winner",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The operational field this observation is about (e.g. "title"). One of
    # app.services.provenance.freshness.TRACKED_FIELDS.
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    # The observed normalized value. NULL means the source observed the field as
    # empty — a real observation, kept distinct from "never observed".
    value: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Origin: an import observation OR a manual override ------------------
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("import_batches.id", ondelete="SET NULL"),
        nullable=True,
    )
    import_row_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("import_rows.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Snapshot of the source metadata at observation time (from the batch/row).
    source_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_reference: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    exported_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # A manual operator override is always explicit and always outranks import
    # evidence until a newer manual override replaces it (DAT-005).
    is_manual_override: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Who recorded a manual override (operator id/email). NULL for imports.
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- Timestamps ---------------------------------------------------------
    # When the *source* observed the value. NULL when the source gave no
    # observation time; the freshness policy handles the missing case explicitly.
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When *this system* ingested the observation. Always known.
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Optional source confidence (0..1). Recorded for auditability; the launch
    # freshness policy does not yet weight by it (documented limitation).
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- Reconciliation result ----------------------------------------------
    is_current_winner: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # The freshness policy version that last evaluated this observation, so a
    # decision is reproducible and attributable to a specific rule set.
    policy_version: Mapped[str] = mapped_column(String(50), nullable=False)
    # Human-readable explanation of why this observation currently wins or lost.
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"ContactFieldValue(contact_id={self.contact_id!r}, field={self.field_name!r}, "
            f"value={self.value!r}, winner={self.is_current_winner!r})"
        )

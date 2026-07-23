"""Identity-resolution records (DAT-004).

When an operator resolves an ambiguous imported identity, or merges two confirmed
duplicate contacts, the decision is written here as an immutable record. This is
the resolution audit history required by DAT-004: it captures the actor, the
action, the human reason, and a snapshot of the previous and resulting state, and
it makes repeated submissions idempotent.

An :class:`IdentityResolution` is distinct from the import outcome it resolves:
the originating :class:`~app.models.import_batch.ImportRowValidation` keeps its
``AMBIGUOUS`` outcome forever (the import genuinely was ambiguous), while this
record documents the *post-import* human decision layered on top of it. Nothing
here overwrites or deletes the raw import row or its provenance.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import IdentityResolutionType


class IdentityResolution(Base):
    """One operator decision resolving an ambiguous identity or duplicate pair."""

    __tablename__ = "identity_resolutions"
    __table_args__ = (
        # At most one active resolution per ambiguous import row: resolving the
        # same row twice returns the existing decision instead of mutating again
        # (idempotency; a partial unique index so merge-only rows, which have no
        # import row, do not collide on NULL).
        Index(
            "uq_identity_resolutions_import_row",
            "import_row_id",
            unique=True,
            postgresql_where="import_row_id IS NOT NULL",
        ),
        # A caller-supplied idempotency key makes a retried submission a no-op
        # even before any row/contact linkage exists.
        UniqueConstraint("idempotency_key", name="uq_identity_resolutions_idempotency_key"),
        Index("ix_identity_resolutions_target_contact_id", "target_contact_id"),
        Index("ix_identity_resolutions_merged_contact_id", "merged_contact_id"),
        Index("ix_identity_resolutions_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    resolution_type: Mapped[IdentityResolutionType] = mapped_column(
        Enum(IdentityResolutionType, name="identity_resolution_type"),
        nullable=False,
    )

    # The ambiguous import row being resolved (NULL for a standalone contact merge
    # that was not initiated from a specific row). ``SET NULL`` keeps the audit
    # record even if the row were ever removed — but the row is never deleted.
    import_row_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("import_rows.id", ondelete="SET NULL"),
        nullable=True,
    )

    # The contact this resolution assigned to, created, or kept as the survivor.
    target_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True,
    )
    # For MERGE: the tombstoned (losing) contact folded into the survivor.
    merged_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Who decided, and why (the human reason is required by the audit contract).
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Stable client-supplied key that de-duplicates retried submissions.
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)

    # Structured before/after snapshots (contact ids, membership states, etc.),
    # so the exact consequence of the decision stays inspectable after the fact.
    previous_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    resulting_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"IdentityResolution(type={self.resolution_type.value!r}, "
            f"import_row_id={self.import_row_id!r}, target={self.target_contact_id!r})"
        )

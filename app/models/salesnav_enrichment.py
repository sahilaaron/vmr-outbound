"""Sales Navigator company-domain enrichment records (DAT-010).

A Sales Navigator capture never exposes ``company_domain``, so every captured
company must have a domain supplied before its rows can pass validation. This
table records — once per unique company per staged batch — the logo.dev lookup
and the operator's explicit confirmation, entirely separately from the immutable
Sales Navigator raw rows (:class:`~app.models.import_batch.ImportRow`, never
mutated) and from a contact's :class:`~app.models.provenance.ProvenanceRecord`.

It is provenance/audit metadata: it holds what was searched, what candidates
logo.dev returned, and which domain the operator confirmed (a candidate, a manual
override, or an explicit "leave unresolved"), with the actor and time. The
confirmed domain is applied to the batch's matching rows at preview/confirm time
as an overlay; the raw capture is not touched.

Nothing here is a secret: the logo.dev API key is never stored, serialized, or
referenced by this model.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import (
    EnrichmentConfirmationSource,
    EnrichmentConfirmationStatus,
    EnrichmentLookupStatus,
)


class SalesNavCompanyEnrichment(Base):
    """One unique company within one staged batch, plus its lookup + decision."""

    __tablename__ = "salesnav_company_enrichments"
    __table_args__ = (
        # One enrichment record per unique company (by normalized key) per batch,
        # so a confirmed domain propagates to every matching row exactly once and
        # a company is looked up at most once unless the operator refreshes.
        UniqueConstraint(
            "batch_id", "company_key", name="uq_salesnav_company_enrichments_batch_company"
        ),
        Index("ix_salesnav_company_enrichments_batch_id", "batch_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("import_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Normalized grouping key (collapsed, case-folded company name). Rows whose
    # mapped company_name matches this key receive the confirmed domain.
    company_key: Mapped[str] = mapped_column(String(512), nullable=False)
    # The company name as first seen (for display); the raw values stay on the
    # immutable import rows.
    company_name: Mapped[str] = mapped_column(String(512), nullable=False)
    row_count: Mapped[int] = mapped_column(nullable=False, default=0)

    # --- Lookup state --------------------------------------------------------
    lookup_status: Mapped[EnrichmentLookupStatus] = mapped_column(
        Enum(EnrichmentLookupStatus, name="enrichment_lookup_status"),
        nullable=False,
        default=EnrichmentLookupStatus.NOT_STARTED,
    )
    # Candidates returned by logo.dev, as a list of {"domain", "name"} objects.
    # Never includes a logo URL, score, or the API key.
    candidates: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    # The exact query string sent to logo.dev (the company name). Non-secret.
    lookup_query: Mapped[str | None] = mapped_column(String(512), nullable=True)
    looked_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Count of lookups run for this company (refresh/retry increments it).
    lookup_attempts: Mapped[int] = mapped_column(nullable=False, default=0)

    # --- Operator confirmation ----------------------------------------------
    confirmation_status: Mapped[EnrichmentConfirmationStatus] = mapped_column(
        Enum(EnrichmentConfirmationStatus, name="enrichment_confirmation_status"),
        nullable=False,
        default=EnrichmentConfirmationStatus.UNCONFIRMED,
    )
    # The domain the operator confirmed (normalized hostname), or NULL when
    # unconfirmed or explicitly left unresolved.
    confirmed_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confirmation_source: Mapped[EnrichmentConfirmationSource | None] = mapped_column(
        Enum(EnrichmentConfirmationSource, name="enrichment_confirmation_source"),
        nullable=True,
    )
    confirmed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Optional operator note explaining a manual override or an unresolved mark.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"SalesNavCompanyEnrichment(batch_id={self.batch_id!r}, "
            f"company_key={self.company_key!r}, lookup_status={self.lookup_status.value!r}, "
            f"confirmation_status={self.confirmation_status.value!r})"
        )

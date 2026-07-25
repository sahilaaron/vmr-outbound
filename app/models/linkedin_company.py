"""LinkedIn company-page capture snapshots (DAT-012G).

One **immutable** row per accepted company capture payload: the verbatim
submitted body, the authoritatively normalized company URL/identifier,
deterministic-only headquarters parts, provenance timestamps, and the truthful
match outcome. Company snapshots are firmographic *evidence* — they never
rewrite a canonical :class:`~app.models.company.Company` row; matching links
evidence to an existing company (exact LinkedIn URL/ID lineage or an exact
unique domain) and everything weaker becomes review candidates.
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
from app.models.enums import LinkedInSnapshotOutcome


class LinkedInCompanySnapshot(Base):
    """One immutable, operator-reviewed capture of a LinkedIn company page."""

    __tablename__ = "linkedin_company_snapshots"
    __table_args__ = (
        UniqueConstraint("client_capture_id", name="uq_li_company_snapshots_client_capture_id"),
        Index("ix_li_company_snapshots_normalized_url", "normalized_company_url"),
        Index("ix_li_company_snapshots_company_li_id", "company_linkedin_id"),
        Index("ix_li_company_snapshots_matched_company_id", "matched_company_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # --- Idempotency / integrity ---------------------------------------------
    client_capture_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # --- Contract provenance ---------------------------------------------------
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_company_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    company_linkedin_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # The backend-normalized registrable domain of the displayed website.
    website_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True
    )

    # --- Timestamps ------------------------------------------------------------
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # --- Extraction metadata ----------------------------------------------------
    extraction_status: Mapped[str] = mapped_column(String(32), nullable=False)
    adapter_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    missing_sections: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    page_warnings: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)

    # --- Immutable capture -------------------------------------------------------
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    company_fields: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # --- Deterministic-only headquarters parts ----------------------------------
    # Parsed from the DISPLAYED headquarters text only when the split is
    # unambiguous ("City, Region, Country" — three comma parts). Never derived
    # from a person's location, a role location, or an employee location; the
    # displayed value is always preserved verbatim inside company_fields.
    hq_city: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hq_region: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hq_country: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- Match outcome (truthful) -------------------------------------------------
    outcome: Mapped[LinkedInSnapshotOutcome] = mapped_column(
        Enum(LinkedInSnapshotOutcome, name="linkedin_snapshot_outcome", create_type=False),
        nullable=False,
        default=LinkedInSnapshotOutcome.STORED,
    )
    matched_company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="SET NULL"), nullable=True
    )
    review_candidates: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<LinkedInCompanySnapshot id={self.id} url={self.normalized_company_url!r} "
            f"outcome={self.outcome}>"
        )

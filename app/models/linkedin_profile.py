"""LinkedIn profile-capture snapshot models (DAT-012D).

Two tables persist what the operator-driven extension captured from a manually
opened MAIN LinkedIn profile page:

* :class:`LinkedInProfileSnapshot` — one **immutable** row per accepted capture
  payload: the verbatim submitted body, the authoritatively normalized profile
  URL, provenance timestamps, extraction status/warnings, and the truthful
  ingest outcome.
* :class:`LinkedInProfileExperienceObservation` — the nested experience entries
  of that snapshot, one row per observed role, never flattened and never
  destroyed by later captures (history accumulates snapshot by snapshot).

Snapshots are evidence, not canonical truth. Nothing here mutates a contact;
reconciliation (exact-URL matching, DAT-005 freshness, DAT-006 suppression)
happens in a separate service layer (DAT-012E) and records its outcome back
onto the snapshot without ever rewriting the captured payload.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import LinkedInSnapshotOutcome


class LinkedInProfileSnapshot(Base):
    """One immutable, operator-reviewed capture of a LinkedIn profile page."""

    __tablename__ = "linkedin_profile_snapshots"
    __table_args__ = (
        # Retries of the same reviewed draft are idempotent on the client-minted
        # capture id; the database enforces it, not just application code.
        UniqueConstraint("client_capture_id", name="uq_li_profile_snapshots_client_capture_id"),
        Index("ix_li_profile_snapshots_normalized_url", "normalized_profile_url"),
        Index("ix_li_profile_snapshots_public_identifier", "public_identifier"),
        Index("ix_li_profile_snapshots_matched_contact_id", "matched_contact_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # --- Idempotency / integrity ------------------------------------------------
    client_capture_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # --- Contract provenance ----------------------------------------------------
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    # The raw page URL at capture time, verbatim (may include query context).
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The backend-normalized identity URL used for exact matching (DAT-012E).
    normalized_profile_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    public_identifier: Mapped[str | None] = mapped_column(String(256), nullable=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="SET NULL"),
        nullable=True,
    )

    # --- Timestamps ---------------------------------------------------------------
    # When the operator captured the page (client clock, from the payload).
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the backend accepted and persisted the snapshot (server clock).
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # --- Extraction metadata ------------------------------------------------------
    extraction_status: Mapped[str] = mapped_column(String(32), nullable=False)
    adapter_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    missing_sections: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    page_warnings: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSONB, nullable=True)

    # --- Immutable capture --------------------------------------------------------
    # The entire submitted payload, verbatim. Write-once; never updated.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # The normalized profile field observations (the payload's ``profile`` block)
    # duplicated for query convenience. Same write-once discipline.
    profile_fields: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # --- Ingest outcome (truthful; updated only by the reconciliation service) ----
    outcome: Mapped[LinkedInSnapshotOutcome] = mapped_column(
        Enum(LinkedInSnapshotOutcome, name="linkedin_snapshot_outcome"),
        nullable=False,
        default=LinkedInSnapshotOutcome.STORED,
    )
    matched_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True,
    )

    experiences: Mapped[list[LinkedInProfileExperienceObservation]] = relationship(
        back_populates="snapshot",
        cascade="all, delete-orphan",
        order_by="LinkedInProfileExperienceObservation.position_index",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<LinkedInProfileSnapshot id={self.id} url={self.normalized_profile_url!r} "
            f"outcome={self.outcome}>"
        )


class LinkedInProfileExperienceObservation(Base):
    """One observed experience entry of one profile snapshot (nested, immutable)."""

    __tablename__ = "linkedin_profile_experience_observations"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id", "position_index", name="uq_li_profile_exp_snapshot_position"
        ),
        Index("ix_li_profile_exp_snapshot_id", "snapshot_id"),
        Index("ix_li_profile_exp_company_li_id", "company_linkedin_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("linkedin_profile_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )

    # On-page order (1-based). Position 1 is the top (most recent) entry.
    position_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # "basic" | "chained" — which recognized layout produced this entry.
    layout: Mapped[str] = mapped_column(String(16), nullable=False)

    company_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    company_linkedin_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    company_linkedin_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    job_title: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Displayed timeline/duration, verbatim, plus deterministic-only parses.
    timeline_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dates_reliable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    employment_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The role's displayed location — distinct from the person's displayed
    # profile location and from any company headquarters.
    role_location: Mapped[str | None] = mapped_column(Text, nullable=True)
    workplace_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_current: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_lines: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    warnings: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    snapshot: Mapped[LinkedInProfileSnapshot] = relationship(back_populates="experiences")

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<LinkedInProfileExperienceObservation snapshot={self.snapshot_id} "
            f"pos={self.position_index} title={self.job_title!r}>"
        )

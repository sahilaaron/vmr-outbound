"""Shared evidence and insight models (INS-001).

``Insight`` stores a versioned claim about exactly one permanent Company or
Contact. ``InsightEvidence`` stores the external observation supporting that
claim. Keeping the two apart lets several sources support or contradict one
claim without copying the claim, and keeps research reusable across campaigns.

The old DAT-001 source columns remain on ``Insight`` for migration
compatibility. New writes go through :mod:`app.services.insights.evidence` and
leave those legacy columns empty; all source material belongs on
``InsightEvidence``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import InsightKind, InsightState, InsightSubject


class Insight(Base):
    """A single factual claim about a company or contact, with provenance."""

    __tablename__ = "insights"
    __table_args__ = (
        Index("ix_insights_contact_id", "contact_id"),
        Index("ix_insights_company_id", "company_id"),
        Index("ix_insights_subject", "subject"),
        Index("ix_insights_state", "state"),
        CheckConstraint(
            "(subject = 'COMPANY' AND company_id IS NOT NULL AND contact_id IS NULL) "
            "OR (subject = 'CONTACT' AND contact_id IS NOT NULL AND company_id IS NULL)",
            name="insight_exactly_one_subject",
        ),
        CheckConstraint("btrim(claim) <> ''", name="insight_claim_not_blank"),
        CheckConstraint("version > 0", name="insight_version_positive"),
        UniqueConstraint(
            "company_id",
            "idempotency_key",
            name="uq_insights_company_idempotency",
        ),
        UniqueConstraint(
            "contact_id",
            "idempotency_key",
            name="uq_insights_contact_idempotency",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subject: Mapped[InsightSubject] = mapped_column(
        Enum(InsightSubject, name="insight_subject"), nullable=False
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=True,
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=True,
    )
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[InsightKind] = mapped_column(
        Enum(InsightKind, name="insight_kind"), nullable=False, default=InsightKind.FACT
    )
    state: Mapped[InsightState] = mapped_column(
        Enum(InsightState, name="insight_state"),
        nullable=False,
        default=InsightState.SUPPORTED,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # DAT-001 compatibility only. New source material is stored on
    # InsightEvidence, where several observations can support one claim.
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # When the underlying evidence is considered current as of (freshness).
    freshness_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Insight(id={self.id!r}, subject={self.subject.value!r})"


class InsightEvidence(Base):
    """One external observation supporting or contradicting an insight."""

    __tablename__ = "insight_evidence"
    __table_args__ = (
        Index("ix_insight_evidence_insight_id", "insight_id"),
        UniqueConstraint(
            "insight_id",
            "source_url",
            "version",
            name="uq_insight_evidence_source_version",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="insight_evidence_confidence_range",
        ),
        CheckConstraint("version > 0", name="insight_evidence_version_positive"),
        CheckConstraint(
            "(source_record_type IS NULL AND source_record_id IS NULL) "
            "OR (source_record_type IS NOT NULL AND source_record_id IS NOT NULL)",
            name="insight_evidence_source_record_pair",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    insight_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("insights.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    source_title: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    extraction_method: Mapped[str | None] = mapped_column(String(255), nullable=True)
    freshness_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_record_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_record_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"InsightEvidence(insight_id={self.insight_id!r}, source_url={self.source_url!r})"

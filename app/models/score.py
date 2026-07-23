"""Score models (DAT-001 representation).

Represents versioned, explainable scores (Initial Fit and Outreach Readiness),
their component values, and the evidence they cite. This slice only represents
the tables; no score is calculated here (scoring is a later phase).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ScoreType


class Score(Base):
    """A single explainable score for a contact (optionally in a campaign)."""

    __tablename__ = "scores"
    __table_args__ = (
        Index("ix_scores_contact_id", "contact_id"),
        Index("ix_scores_campaign_id", "campaign_id"),
        Index("ix_scores_type", "score_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=True,
    )
    score_type: Mapped[ScoreType] = mapped_column(
        Enum(ScoreType, name="score_type"), nullable=False
    )
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"Score(id={self.id!r}, type={self.score_type.value!r}, total={self.total!r}, "
            f"rule_version={self.rule_version!r})"
        )


class ScoreComponent(Base):
    """One named component contributing to a score's total."""

    __tablename__ = "score_components"
    __table_args__ = (
        UniqueConstraint("score_id", "name", name="uq_score_components_score_name"),
        Index("ix_score_components_score_id", "score_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    score_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scores.id", ondelete="CASCADE"),
        nullable=False,
    )
    # e.g. company_fit, contact_fit, evidence_of_need, timing, personalization_material.
    name: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False)
    weight: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"ScoreComponent(score_id={self.score_id!r}, name={self.name!r}, value={self.value!r})"
        )


class ScoreEvidence(Base):
    """Links a score to an insight it cites as evidence."""

    __tablename__ = "score_evidence"
    __table_args__ = (
        UniqueConstraint("score_id", "insight_id", name="uq_score_evidence_score_insight"),
        Index("ix_score_evidence_score_id", "score_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    score_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scores.id", ondelete="CASCADE"),
        nullable=False,
    )
    insight_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("insights.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"ScoreEvidence(score_id={self.score_id!r}, insight_id={self.insight_id!r})"

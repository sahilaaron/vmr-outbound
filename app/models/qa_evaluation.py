"""Versioned QA-policy evaluation records (DAT-012F).

Each row is one evaluation of one contact against one profile snapshot under
one named, versioned backend policy. Evaluations are append-only evidence: a
re-evaluation (new snapshot, new policy version, changed thresholds) adds a new
row rather than rewriting history.

An evaluation is a *recommendation with evidence*, never a mutation: writing
one changes no contact field, no workflow state, no suppression, no
verification, no approval, and no schedule.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import QAOutcome


class ContactQAEvaluation(Base):
    """One versioned, evidence-backed QA evaluation of a contact snapshot."""

    __tablename__ = "contact_qa_evaluations"
    __table_args__ = (
        Index("ix_contact_qa_evaluations_contact_id", "contact_id"),
        Index("ix_contact_qa_evaluations_snapshot_id", "snapshot_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # --- Policy identity ------------------------------------------------------
    policy_name: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # --- Subject + evidence ---------------------------------------------------
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
    )
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("linkedin_profile_snapshots.id", ondelete="SET NULL"),
        nullable=True,
    )
    # What the contact looked like when evaluated + which evidence was used
    # (snapshot/observation identifiers, expectation values). Immutable record.
    contact_expectation: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    evidence_refs: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # --- Result ---------------------------------------------------------------
    outcome: Mapped[QAOutcome] = mapped_column(Enum(QAOutcome, name="qa_outcome"), nullable=False)
    reason_codes: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    # Configurable review signals with the thresholds that were in force, so the
    # decision is reproducible even after configuration changes.
    signals: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_action: Mapped[str] = mapped_column(String(64), nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ContactQAEvaluation contact={self.contact_id} outcome={self.outcome} "
            f"policy={self.policy_version}>"
        )

"""Audit event model.

Every material automated action records an audit event (GOAL.md acceptance
criterion; AGENTS.md guardrail: "Every automated mutation must record actor,
timestamp, previous state, new state, and reason"). This is the minimal Phase 0
model; later phases add domain tables but reuse this audit trail.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuditEvent(Base):
    """An immutable record of a material action taken by the system or a user."""

    __tablename__ = "audit_events"
    __table_args__ = (
        # Audit history is read by entity (contact detail view) and by time
        # (system health / recent activity).
        Index("ix_audit_events_entity", "entity_type", "entity_id"),
        Index("ix_audit_events_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Who performed the action: a human operator id/email or a service name.
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    # What happened, e.g. "contact.suppressed", "draft.approved".
    action: Mapped[str] = mapped_column(String(255), nullable=False)

    # The subject of the action. entity_id is a stable string reference (often a
    # UUID) but stored as text so the audit log never depends on FK availability.
    entity_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # State transition, when applicable.
    previous_state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    new_state: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Human-readable justification for the action.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Whether the action occurred while dry-run mode was active. Lets outcome
    # data distinguish simulated actions from real ones.
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Optional structured context; never used to store secrets.
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"AuditEvent(id={self.id!r}, actor={self.actor!r}, "
            f"action={self.action!r}, entity={self.entity_type!r}:{self.entity_id!r})"
        )

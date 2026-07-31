"""Immutable Personalization policy versions and append-only activations.

The policy snapshot is deliberately separate from ``AgentControl.config``.
Agent controls decide whether work may execute and retain the existing
``{"live": true}`` spending authority.  A Personalization policy defines how an
already-authorized execution should write.  Mixing the two would make a status
toggle capable of replacing writing policy, or a policy edit capable of
granting live execution.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PersonalizationPolicyVersion(Base):
    """One immutable, validated Personalization policy snapshot."""

    __tablename__ = "personalization_policy_versions"
    __table_args__ = (
        UniqueConstraint("version_number", name="uq_personalization_policy_versions_number"),
        Index("ix_personalization_policy_versions_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    validation_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    based_on_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("personalization_policy_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    change_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PersonalizationPolicyActivation(Base):
    """Append-only record selecting an immutable policy version as active."""

    __tablename__ = "personalization_policy_activations"
    __table_args__ = (Index("ix_personalization_policy_activations_time", "activated_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    policy_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("personalization_policy_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    previous_policy_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("personalization_policy_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    activated_by: Mapped[str] = mapped_column(String(128), nullable=False)
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class ImmutablePolicyHistoryError(RuntimeError):
    """A caller attempted to alter append-only policy history."""


def _reject_history_mutation(_mapper: Any, _connection: Any, _target: Any) -> None:
    raise ImmutablePolicyHistoryError("Personalization policy history is append-only.")


for _history_model in (PersonalizationPolicyVersion, PersonalizationPolicyActivation):
    event.listen(_history_model, "before_update", _reject_history_mutation)
    event.listen(_history_model, "before_delete", _reject_history_mutation)

"""Persistent execution controls for the common Agent framework."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import AgentControlStatus, AgentIdentifier


class AgentControl(Base):
    """Optional global override of one Agent's registry defaults."""

    __tablename__ = "agent_controls"

    agent_id: Mapped[AgentIdentifier] = mapped_column(
        Enum(AgentIdentifier, name="agent_identifier"), primary_key=True
    )
    status: Mapped[AgentControlStatus] = mapped_column(
        Enum(AgentControlStatus, name="agent_control_status"), nullable=False
    )
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CampaignAgentOverride(Base):
    """Campaign-level override; absence means inherit the global control."""

    __tablename__ = "campaign_agent_overrides"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id",
            "agent_id",
            name="uq_campaign_agent_overrides_campaign_agent",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[AgentIdentifier] = mapped_column(
        Enum(AgentIdentifier, name="agent_identifier"), nullable=False
    )
    status: Mapped[AgentControlStatus] = mapped_column(
        Enum(AgentControlStatus, name="agent_control_status"), nullable=False
    )
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

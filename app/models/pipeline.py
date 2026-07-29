"""Durable Campaign Contact pipeline state and append-only history."""

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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import (
    AgentIdentifier,
    CaptureCampaignFilingStatus,
    PipelineEventType,
    PipelineStageStatus,
)


class CampaignContactSource(Base):
    """Append-only provenance for how a Contact entered a Campaign."""

    __tablename__ = "campaign_contact_sources"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_campaign_contact_sources_idempotency_key"),
        Index("ix_campaign_contact_sources_membership", "campaign_contact_id", "recorded_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaign_contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(512), nullable=True)
    capture_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("linkedin_profile_snapshots.id", ondelete="SET NULL"),
        nullable=True,
    )
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("import_batches.id", ondelete="SET NULL"),
        nullable=True,
    )
    collection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contact_labels.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_context: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    recorded_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CampaignContactAgentState(Base):
    """One durable projection for one Agent on one Campaign Contact."""

    __tablename__ = "campaign_contact_agent_states"
    __table_args__ = (
        UniqueConstraint(
            "campaign_contact_id",
            "agent_id",
            name="uq_campaign_contact_agent_states_membership_agent",
        ),
        Index(
            "ix_campaign_contact_agent_states_status",
            "agent_id",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaign_contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[AgentIdentifier] = mapped_column(
        Enum(AgentIdentifier, name="agent_identifier"), nullable=False
    )
    status: Mapped[PipelineStageStatus] = mapped_column(
        Enum(PipelineStageStatus, name="pipeline_stage_status"),
        nullable=False,
        default=PipelineStageStatus.WAITING,
        server_default=PipelineStageStatus.WAITING.name,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    latest_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("verification_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    reason_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    reason_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    waiting_on_agent: Mapped[AgentIdentifier | None] = mapped_column(
        Enum(AgentIdentifier, name="agent_identifier"), nullable=True
    )
    output_reference: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class PipelineEvent(Base):
    """Append-only explanation of every pipeline state transition."""

    __tablename__ = "pipeline_events"
    __table_args__ = (
        Index("ix_pipeline_events_membership_time", "campaign_contact_id", "occurred_at"),
        Index("ix_pipeline_events_job_id", "job_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaign_contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[AgentIdentifier | None] = mapped_column(
        Enum(AgentIdentifier, name="agent_identifier"), nullable=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("verification_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type: Mapped[PipelineEventType] = mapped_column(
        Enum(PipelineEventType, name="pipeline_event_type"), nullable=False
    )
    from_status: Mapped[PipelineStageStatus | None] = mapped_column(
        Enum(PipelineStageStatus, name="pipeline_stage_status"), nullable=True
    )
    to_status: Mapped[PipelineStageStatus | None] = mapped_column(
        Enum(PipelineStageStatus, name="pipeline_stage_status"), nullable=True
    )
    reason_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    reason_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CaptureCampaignFiling(Base):
    """Truthful outcome of one optional capture-to-Campaign filing request."""

    __tablename__ = "capture_campaign_filings"
    __table_args__ = (
        UniqueConstraint("capture_id", name="uq_capture_campaign_filings_capture_id"),
        Index("ix_capture_campaign_filings_status", "status", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    capture_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("linkedin_profile_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    submission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contact_capture_submissions.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Request intent is preserved even if the referenced Campaign is missing.
    requested_campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="SET NULL"),
        nullable=True,
    )
    campaign_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaign_contacts.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[CaptureCampaignFilingStatus] = mapped_column(
        Enum(CaptureCampaignFilingStatus, name="capture_campaign_filing_status"),
        nullable=False,
        default=CaptureCampaignFilingStatus.PENDING,
        server_default=CaptureCampaignFilingStatus.PENDING.name,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

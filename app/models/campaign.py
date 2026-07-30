"""Campaign and campaign-membership models.

A campaign is the shell that an authorized contact batch is imported into
(CMP-001, minimum fields for this slice). A ``CampaignContact`` row is the
membership that links a contact to a campaign and carries that contact's
explicit, audited workflow state *for that campaign* — so the same contact can
appear in several campaigns without losing per-campaign progress or creating a
duplicate active-outreach record (CMP-002, CMP-003).
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import (
    AgentIdentifier,
    CampaignContactEligibility,
    CampaignMembershipStatus,
    CampaignStatus,
    ContactWorkflowState,
    PipelineStageStatus,
)


class Campaign(Base):
    """Campaign-specific operating context over reusable Contacts and Companies."""

    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Free-shaped settings stay structured and bounded by the Campaign service.
    # Seller offerings and proof points remain references in seller_knowledge.py;
    # these fields do not copy those permanent knowledge records.
    sender_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    target_audience: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    messaging_direction: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_cta: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    cadence_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    sending_settings: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Lifecycle status and execution control are deliberately separate. A draft
    # or active Campaign can be disabled without archiving it or losing state.
    execution_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # --- Per-campaign policy switches ---
    #
    # Both loosen a rule that is strict by default, and both are per-campaign
    # rather than global because the right answer differs by audience: a campaign
    # into a well-known industry can afford to wait for confirmed domains, and a
    # campaign into a long tail of small firms mostly cannot.
    #
    # A provisional domain is a single provider candidate that nothing has
    # independently corroborated. Opening it authorizes real spend and real
    # outreach on a domain nobody confirmed, so it is off unless asked for. Note
    # what this does NOT change: a provisional decision still never writes itself
    # into the approved-mapping store and a provisional-backed Company is still
    # not treated as established evidence. Those two guards are what stop a guess
    # upgrading itself to certainty, and they are independent of this switch.
    allow_provisional_domains: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    settings_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[CampaignStatus] = mapped_column(
        Enum(CampaignStatus, name="campaign_status"),
        nullable=False,
        default=CampaignStatus.DRAFT,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Campaign(id={self.id!r}, name={self.name!r}, status={self.status.value!r})"


class CampaignContact(Base):
    """Membership linking a contact to a campaign with a per-campaign state."""

    __tablename__ = "campaign_contacts"
    __table_args__ = (
        # One membership per (campaign, contact): a contact cannot have two
        # active outreach records in the same campaign (CMP-003).
        UniqueConstraint("campaign_id", "contact_id", name="uq_campaign_contacts_campaign_contact"),
        Index("ix_campaign_contacts_campaign_id", "campaign_id"),
        Index("ix_campaign_contacts_contact_id", "contact_id"),
        Index("ix_campaign_contacts_state", "state"),
        Index("ix_campaign_contacts_membership_status", "campaign_id", "membership_status"),
        Index("ix_campaign_contacts_pipeline_status", "campaign_id", "pipeline_status"),
        Index("ix_campaign_contacts_current_stage", "campaign_id", "current_stage"),
        Index("ix_campaign_contacts_eligibility", "campaign_id", "eligibility_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    state: Mapped[ContactWorkflowState] = mapped_column(
        Enum(ContactWorkflowState, name="contact_workflow_state"),
        nullable=False,
        default=ContactWorkflowState.IMPORTED,
    )
    # Which import batch first added this contact to this campaign (provenance).
    source_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("import_batches.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_capture_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("linkedin_profile_snapshots.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_kind: Mapped[str] = mapped_column(
        String(64), nullable=False, default="legacy", server_default="legacy"
    )
    source_reference: Mapped[str | None] = mapped_column(String(512), nullable=True)
    enrolled_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    enrolled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    membership_status: Mapped[CampaignMembershipStatus] = mapped_column(
        Enum(CampaignMembershipStatus, name="campaign_membership_status"),
        nullable=False,
        default=CampaignMembershipStatus.ACTIVE,
        server_default=CampaignMembershipStatus.ACTIVE.name,
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    eligibility_status: Mapped[CampaignContactEligibility] = mapped_column(
        Enum(CampaignContactEligibility, name="campaign_contact_eligibility"),
        nullable=False,
        default=CampaignContactEligibility.UNKNOWN,
        server_default=CampaignContactEligibility.UNKNOWN.name,
    )
    blocking_reasons: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    qualification_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    review_state: Mapped[str] = mapped_column(
        String(64), nullable=False, default="pending", server_default="pending"
    )
    sending_state: Mapped[str] = mapped_column(
        String(64), nullable=False, default="not_started", server_default="not_started"
    )
    provider_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    # Fast audience-view projection. Append-only pipeline_events remains the
    # explainable history; per-Agent detail lives in campaign_contact_agent_states.
    desired_stage: Mapped[AgentIdentifier] = mapped_column(
        Enum(AgentIdentifier, name="agent_identifier"),
        nullable=False,
        default=AgentIdentifier.SENDING,
        server_default=AgentIdentifier.SENDING.name,
    )
    current_stage: Mapped[AgentIdentifier | None] = mapped_column(
        Enum(AgentIdentifier, name="agent_identifier"), nullable=True
    )
    latest_completed_stage: Mapped[AgentIdentifier | None] = mapped_column(
        Enum(AgentIdentifier, name="agent_identifier"), nullable=True
    )
    next_stage: Mapped[AgentIdentifier | None] = mapped_column(
        Enum(AgentIdentifier, name="agent_identifier"),
        nullable=True,
        default=AgentIdentifier.IDENTITY,
        server_default=AgentIdentifier.IDENTITY.name,
    )
    pipeline_status: Mapped[PipelineStageStatus] = mapped_column(
        Enum(PipelineStageStatus, name="pipeline_stage_status"),
        nullable=False,
        default=PipelineStageStatus.WAITING,
        server_default=PipelineStageStatus.WAITING.name,
    )
    processing_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"CampaignContact(campaign_id={self.campaign_id!r}, "
            f"contact_id={self.contact_id!r}, state={self.state.value!r})"
        )

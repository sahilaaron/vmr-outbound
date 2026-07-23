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
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import CampaignStatus, ContactWorkflowState


class Campaign(Base):
    """A campaign that can receive an authorized contact import."""

    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
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

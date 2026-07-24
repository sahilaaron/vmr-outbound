"""Campaign and campaign-membership models.

A campaign is the shell that an authorized contact batch is imported into. It
carries the minimum launch-ready settings for the first controlled pilot
(CMP-001): the offer, structured audience-targeting rules, structured
exclusions, the minimum Initial Fit Score a contact must reach to enter
research, copy tone, owner, source, and a sending-configuration reference. A
``CampaignContact`` row is the membership that links a contact to a campaign
and carries that contact's explicit, audited workflow state *for that
campaign* — so the same contact can appear in several campaigns without losing
per-campaign progress or creating a duplicate active-outreach record (CMP-002,
CMP-003).

``audience_rules`` and ``exclusions`` are stored as JSONB (the same structured
representation already used for other free-shaped campaign/import data, e.g.
``ImportBatch.source_metadata`` and ``AuditEvent.context``) rather than
flattened into free text, so the targeting criteria a campaign was built
against stays machine-readable and auditable. Their internal shape is
intentionally not fixed by a schema here: CMP-001 only guarantees the top
level is a JSON object and that it round-trips exactly. Defining the rule
vocabulary itself is downstream (post-launch campaign-builder) scope.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import CampaignStatus, ContactWorkflowState

# Default minimum Initial Fit Score (0-100) a contact must reach to enter
# research when a campaign does not set its own threshold explicitly. Matches
# the absolute launch threshold defined in docs/GOAL.md ("at least 85/100").
DEFAULT_MIN_SCORE_THRESHOLD = 85


class Campaign(Base):
    """A campaign that can receive an authorized contact import."""

    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Draft settings (CMP-001) --------------------------------------------
    # What is being offered to the prospect. Free text; drafting/personalization
    # phases consume it, none of that is implemented here.
    offer: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Structured targeting criteria the campaign was built against (JSON
    # object). Kept structured, not flattened into text, so it stays readable
    # and machine-checkable. See the module docstring for why the internal
    # shape is not fixed here.
    audience_rules: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Structured exclusion criteria scoped to *this campaign's targeting*
    # (e.g. "do not target this industry/title for this offer"). This is
    # distinct from the global suppression ledger (DAT-006, app/models/
    # suppression.py), which is the authoritative, campaign-independent record
    # of identities that must never be contacted at all; campaign exclusions
    # narrow this campaign's audience and never weaken or replace suppression
    # enforcement.
    exclusions: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Minimum Initial Fit Score (0-100) a contact must reach to enter research
    # for this campaign. Always set (never "no gate"); defaults to the launch
    # absolute threshold from docs/GOAL.md.
    min_score_threshold: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_MIN_SCORE_THRESHOLD,
        server_default=str(DEFAULT_MIN_SCORE_THRESHOLD),
    )
    # Requested copy tone for drafting (e.g. "direct", "warm"). Free text label;
    # no controlled vocabulary is enforced yet (none is justified at this scope).
    tone: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Operator id/email accountable for this campaign.
    owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Where this campaign's targeting originates (e.g. "sales_navigator",
    # "manual"). Free text label; not validated against the import-source enum,
    # which describes how *contacts* enter, not why the campaign exists.
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Opaque reference to this campaign's sending configuration (e.g. a
    # Saleshandy campaign/sequence id or mailbox-pool label). CMP-001 only
    # stores the reference; it never calls a sending provider.
    sending_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)

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

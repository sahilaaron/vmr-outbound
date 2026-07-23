"""Campaign service (CMP-001, minimal slice).

Creates the campaign shell that an authorized import targets. Only the minimum
fields needed to receive an import are set here; richer campaign settings (offer,
tone, audience rules, sending reference) are added when their phases need them.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.campaign import Campaign
from app.models.enums import CampaignStatus
from app.services.audit import record_audit_event


class CampaignError(Exception):
    """Raised when a campaign cannot be created as requested."""


def create_campaign(
    session: Session,
    *,
    name: str,
    description: str | None = None,
    status: CampaignStatus = CampaignStatus.DRAFT,
    actor: str = "operator",
) -> Campaign:
    """Create and persist a campaign, recording an audit event.

    The campaign is added and flushed (so it receives its id) but not committed —
    the caller owns the transaction boundary.
    """

    cleaned = name.strip()
    if not cleaned:
        raise CampaignError("campaign name is required")

    campaign = Campaign(name=cleaned, description=description, status=status)
    session.add(campaign)
    session.flush()

    record_audit_event(
        session,
        actor=actor,
        action="campaign.created",
        entity_type="campaign",
        entity_id=str(campaign.id),
        new_state=campaign.status.value,
        reason="campaign created",
    )
    return campaign

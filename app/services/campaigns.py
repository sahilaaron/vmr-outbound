"""Campaign service (CMP-001, minimal slice).

Creates the campaign shell that an authorized import targets, and provides the
read paths the workbench lists and detail pages use. Only the minimum fields
needed to receive an import exist here; richer campaign settings (offer, tone,
audience rules, sending reference) are added when their phases need them —
adding them is CMP-001's remaining scope, deliberately not expanded here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.enums import CampaignStatus, ContactWorkflowState
from app.models.import_batch import ImportBatch
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


@dataclass
class CampaignOverview:
    """A campaign with the aggregate counts the workbench list shows."""

    campaign: Campaign
    contact_count: int = 0
    import_count: int = 0
    state_counts: dict[str, int] = field(default_factory=dict)


def list_campaigns(session: Session) -> list[CampaignOverview]:
    """All campaigns, newest first, with membership and import counts."""

    campaigns = session.scalars(select(Campaign).order_by(Campaign.created_at.desc())).all()

    member_counts: dict[uuid.UUID, int] = {
        campaign_id: count
        for campaign_id, count in session.execute(
            select(CampaignContact.campaign_id, func.count(CampaignContact.id)).group_by(
                CampaignContact.campaign_id
            )
        ).all()
    }
    import_counts: dict[uuid.UUID, int] = {
        campaign_id: count
        for campaign_id, count in session.execute(
            select(ImportBatch.campaign_id, func.count(ImportBatch.id)).group_by(
                ImportBatch.campaign_id
            )
        ).all()
    }
    return [
        CampaignOverview(
            campaign=c,
            contact_count=member_counts.get(c.id, 0),
            import_count=import_counts.get(c.id, 0),
        )
        for c in campaigns
    ]


def get_campaign_overview(session: Session, campaign_id: uuid.UUID) -> CampaignOverview | None:
    """One campaign with its per-state membership counts, or None."""

    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        return None

    state_rows = session.execute(
        select(CampaignContact.state, func.count(CampaignContact.id))
        .where(CampaignContact.campaign_id == campaign_id)
        .group_by(CampaignContact.state)
    ).all()
    state_counts = {state.value: count for state, count in state_rows}
    return CampaignOverview(
        campaign=campaign,
        contact_count=sum(state_counts.values()),
        import_count=session.scalar(
            select(func.count(ImportBatch.id)).where(ImportBatch.campaign_id == campaign_id)
        )
        or 0,
        state_counts=state_counts,
    )


def campaign_imports(session: Session, campaign_id: uuid.UUID) -> list[ImportBatch]:
    """Import batches linked to one campaign, newest first."""

    return list(
        session.scalars(
            select(ImportBatch)
            .where(ImportBatch.campaign_id == campaign_id)
            .order_by(ImportBatch.created_at.desc())
        ).all()
    )


def campaign_members(
    session: Session,
    campaign_id: uuid.UUID,
    *,
    state: ContactWorkflowState | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[tuple[CampaignContact, Contact]], int]:
    """Paginated memberships (with contacts) for one campaign, newest first."""

    query = (
        select(CampaignContact, Contact)
        .join(Contact, Contact.id == CampaignContact.contact_id)
        .where(CampaignContact.campaign_id == campaign_id)
    )
    count_query = select(func.count(CampaignContact.id)).where(
        CampaignContact.campaign_id == campaign_id
    )
    if state is not None:
        query = query.where(CampaignContact.state == state)
        count_query = count_query.where(CampaignContact.state == state)
    total = session.scalar(count_query) or 0
    rows = session.execute(
        query.order_by(CampaignContact.created_at.desc()).limit(limit).offset(offset)
    ).all()
    return [(membership, contact) for membership, contact in rows], total

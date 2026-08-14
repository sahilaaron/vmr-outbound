"""Campaign-to-offering associations (KB-001).

What this does: records which offerings a campaign concerns, for organisation,
tracking, reporting, and later context retrieval.

What this deliberately does not do, and why each one is a decision rather than
an omission:

* **No primary offering, and no ordering.** A campaign about two offerings is
  usually about both. Forcing a rank would make the system look like it knew
  something it does not, and every downstream reader would start trusting the
  order.
* **No requirement.** Zero associations is a valid, permanent state. A campaign
  can be perfectly well-defined by its own copy without naming an offering.
* **No effect on content.** Associating an offering never writes, replaces, or
  suggests email copy, a subject line, or a call to action. The campaign
  operator owns all of that directly. This module has no access to draft
  content and is not called from anywhere that does.

The association is a reference, never a copy. Archiving an offering therefore
leaves every campaign that names it intact and still resolvable — which is the
whole reason archiving exists instead of deletion.

The caller owns the transaction boundary; nothing here commits.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign
from app.models.enums import SellerRecordState
from app.models.seller_knowledge import CampaignOffering, SellerOffering
from app.services.audit import record_audit_event
from app.services.campaign_access import CampaignActor, scope_campaign_statement
from app.services.seller.common import OPERATOR_ACTOR, SellerKnowledgeError


def offerings_for_campaign(session: Session, campaign_id: uuid.UUID) -> list[SellerOffering]:
    """Return every offering associated with a campaign, archived ones included.

    Archived offerings are returned deliberately. Hiding them would make a
    campaign appear to concern fewer things than it does, and the association
    is a historical fact about that campaign, not a live menu.
    """

    return list(
        session.scalars(
            select(SellerOffering)
            .join(CampaignOffering, CampaignOffering.offering_id == SellerOffering.id)
            .where(CampaignOffering.campaign_id == campaign_id)
            .order_by(SellerOffering.state, SellerOffering.name)
        ).all()
    )


def campaigns_for_offering(
    session: Session, offering_id: uuid.UUID, *, actor: CampaignActor
) -> list[Campaign]:
    """Every campaign that concerns an offering **and that ``actor`` may see**.

    An offering is seller knowledge and is not campaign-scoped, but the list of
    campaigns using it is a list of campaigns, so it is scoped like every other
    one. ``actor`` is required for the reason
    :func:`app.services.campaigns.list_campaigns` gives: a default here would put
    campaign names on a page nobody scoped.
    """

    statement = scope_campaign_statement(
        select(Campaign)
        .join(CampaignOffering, CampaignOffering.campaign_id == Campaign.id)
        .where(CampaignOffering.offering_id == offering_id)
        .order_by(Campaign.created_at.desc()),
        actor,
    )
    return list(session.scalars(statement).all())


def associate(
    session: Session,
    *,
    campaign: Campaign,
    offering_id: uuid.UUID,
    actor: str | None = None,
) -> tuple[SellerOffering, bool]:
    """Associate an offering with a campaign.

    Returns ``(offering, created)``. Associating something already associated
    is success and writes nothing.
    """

    offering = session.get(SellerOffering, offering_id)
    if offering is None:
        raise SellerKnowledgeError("That offering no longer exists.")
    if offering.state is not SellerRecordState.ACTIVE:
        # An archived offering already on a campaign stays there; adding a new
        # link to something withdrawn would record a decision to sell it.
        raise SellerKnowledgeError(
            f"“{offering.name}” is archived. Restore it in the Knowledge Base "
            "before adding it to a campaign."
        )
    existing = session.scalars(
        select(CampaignOffering).where(
            CampaignOffering.campaign_id == campaign.id,
            CampaignOffering.offering_id == offering.id,
        )
    ).first()
    if existing is not None:
        return offering, False

    session.add(
        CampaignOffering(
            campaign_id=campaign.id,
            offering_id=offering.id,
            created_by=actor,
        )
    )
    session.flush()
    record_audit_event(
        session,
        actor=actor or OPERATOR_ACTOR,
        action="campaign.offering_linked",
        entity_type="campaign",
        entity_id=str(campaign.id),
        reason="Operator recorded that this campaign concerns an offering.",
        context={"offering_id": str(offering.id), "offering_name": offering.name},
    )
    return offering, True


def dissociate(
    session: Session,
    *,
    campaign: Campaign,
    offering_id: uuid.UUID,
    actor: str | None = None,
) -> bool:
    """Remove an association.

    Deletes the link row only. The offering keeps every other association it
    has and stays exactly as it was; nothing about the campaign's own copy,
    call to action, or membership changes.
    """

    existing = session.scalars(
        select(CampaignOffering).where(
            CampaignOffering.campaign_id == campaign.id,
            CampaignOffering.offering_id == offering_id,
        )
    ).first()
    if existing is None:
        return False
    session.delete(existing)
    session.flush()
    record_audit_event(
        session,
        actor=actor or OPERATOR_ACTOR,
        action="campaign.offering_unlinked",
        entity_type="campaign",
        entity_id=str(campaign.id),
        reason="Operator removed an offering association from this campaign.",
        context={"offering_id": str(offering_id)},
    )
    return True


def selectable_offerings(session: Session, campaign_id: uuid.UUID) -> list[SellerOffering]:
    """Active offerings not already associated with this campaign."""

    linked = select(CampaignOffering.offering_id).where(CampaignOffering.campaign_id == campaign_id)
    return list(
        session.scalars(
            select(SellerOffering)
            .where(
                SellerOffering.state == SellerRecordState.ACTIVE,
                SellerOffering.id.not_in(linked),
            )
            .order_by(SellerOffering.name)
        ).all()
    )

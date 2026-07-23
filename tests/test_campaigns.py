"""Campaign-creation tests (CMP-001, minimal slice)."""

from __future__ import annotations

import pytest
from app.models.audit_event import AuditEvent
from app.models.campaign import Campaign
from app.models.enums import CampaignStatus
from app.services.campaigns import CampaignError, create_campaign
from sqlalchemy import select
from sqlalchemy.orm import Session


def test_create_campaign_persists_with_defaults(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="  Pilot 100  ")
    assert campaign.name == "Pilot 100"  # trimmed
    assert campaign.status is CampaignStatus.DRAFT

    fetched = db_session.get(Campaign, campaign.id)
    assert fetched is not None
    assert fetched.created_at is not None


def test_create_campaign_records_audit_event(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="Audited Campaign")
    events = db_session.scalars(
        select(AuditEvent).where(
            AuditEvent.action == "campaign.created",
            AuditEvent.entity_id == str(campaign.id),
        )
    ).all()
    assert len(events) == 1
    assert events[0].dry_run is True  # default dry-run stamping preserved


def test_create_campaign_rejects_blank_name(db_session: Session) -> None:
    with pytest.raises(CampaignError):
        create_campaign(db_session, name="   ")

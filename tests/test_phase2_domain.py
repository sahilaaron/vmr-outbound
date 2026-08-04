"""Phase 2 Campaign Contact and Collection domain invariants."""

from __future__ import annotations

import uuid

import pytest
from app.models.campaign import Campaign, CampaignContact
from app.models.collection import CollectionMembership
from app.models.company import Company
from app.models.contact import Contact
from app.models.enums import (
    AgentIdentifier,
    AgentJobStatus,
    CampaignContactEligibility,
    CampaignMembershipStatus,
    CampaignStatus,
    PipelineStageStatus,
    SuppressionReason,
    SuppressionType,
)
from app.models.pipeline import CampaignContactAgentState, CampaignContactSource
from app.models.verification_job import AgentJob
from app.services import campaign_contacts, collections
from app.services.suppressions import add_suppression
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


def _complete_contact(db: Session, *, email: str | None = None) -> Contact:
    company = Company(name="Acme Research", domain="acme.example")
    db.add(company)
    db.flush()
    contact = Contact(
        first_name="Ada",
        last_name="Lovelace",
        company_name=company.name,
        company_domain=company.domain,
        company_id=company.id,
        email=email,
        natural_key="ada|lovelace|acme.example",
    )
    db.add(contact)
    db.flush()
    return contact


def _campaign(db: Session) -> Campaign:
    campaign = Campaign(
        name=f"Phase 2 {uuid.uuid4()}",
        status=CampaignStatus.ACTIVE,
        execution_enabled=False,
    )
    db.add(campaign)
    db.flush()
    return campaign


def test_campaign_contact_upsert_is_idempotent_and_preserves_sources(
    db_session: Session,
) -> None:
    campaign = _campaign(db_session)
    contact = _complete_contact(db_session)

    first = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        source_reference="audience-review",
        idempotency_key="enrol-ada-once",
        enqueue=False,
    )
    replay = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        source_reference="audience-review",
        idempotency_key="enrol-ada-once",
        enqueue=False,
    )
    second_source = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="collection",
        source_reference="priority-accounts",
        idempotency_key="enrol-ada-collection",
        enqueue=False,
    )

    assert first.created is True
    assert replay.created is False and replay.source_created is False
    assert second_source.created is False and second_source.source_created is True
    assert first.membership.id == replay.membership.id == second_source.membership.id
    assert db_session.scalar(select(func.count()).select_from(CampaignContact)) == 1
    sources = db_session.scalars(
        select(CampaignContactSource).order_by(CampaignContactSource.recorded_at)
    ).all()
    assert [source.idempotency_key for source in sources] == [
        "enrol-ada-once",
        "enrol-ada-collection",
    ]
    with pytest.raises(
        campaign_contacts.CampaignContactError,
        match="different source intent",
    ):
        campaign_contacts.enrol_contact(
            db_session,
            campaign_id=campaign.id,
            contact_id=contact.id,
            source_type="manual",
            source_reference="changed-intent",
            idempotency_key="enrol-ada-once",
            enqueue=False,
        )


def test_campaign_contact_database_constraint_rejects_duplicate_pair(
    db_session: Session,
) -> None:
    campaign = _campaign(db_session)
    contact = _complete_contact(db_session)
    campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=False,
    )

    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(CampaignContact(campaign_id=campaign.id, contact_id=contact.id))
        db_session.flush()


def test_collection_membership_is_idempotent_and_independent_of_campaign(
    db_session: Session,
) -> None:
    contact = _complete_contact(db_session)
    campaign = _campaign(db_session)
    collection, created = collections.create_collection(
        db_session,
        name="Priority Accounts",
        description="Global reusable audience",
    )
    same, created_again = collections.create_collection(
        db_session,
        name=" priority accounts ",
    )
    first, assigned = collections.assign_contact(
        db_session,
        collection_id=collection.id,
        contact_id=contact.id,
    )
    replay, assigned_again = collections.assign_contact(
        db_session,
        collection_id=collection.id,
        contact_id=contact.id,
    )
    link, linked = collections.associate_campaign(
        db_session,
        collection_id=collection.id,
        campaign_id=campaign.id,
    )
    same_link, linked_again = collections.associate_campaign(
        db_session,
        collection_id=collection.id,
        campaign_id=campaign.id,
    )

    assert created is True and created_again is False and same.id == collection.id
    assert assigned is True and assigned_again is False and first.id == replay.id
    assert linked is True and linked_again is False and link.id == same_link.id
    assert db_session.scalar(select(func.count()).select_from(CollectionMembership)) == 1
    assert db_session.scalar(select(func.count()).select_from(CampaignContact)) == 0

    assert collections.dissociate_campaign(
        db_session,
        collection_id=collection.id,
        campaign_id=campaign.id,
    )
    # Removing the Campaign association never removes global membership.
    assert db_session.get(CollectionMembership, first.id) is not None


def test_archived_campaign_contact_is_not_silently_reactivated(
    db_session: Session,
) -> None:
    campaign = _campaign(db_session)
    contact = _complete_contact(db_session)
    first = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=False,
    )
    campaign_contacts.archive_membership(
        db_session,
        campaign_contact_id=first.membership.id,
        reason="operator removed audience member",
    )
    replay = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        source_reference="retry",
        enqueue=True,
    )

    assert replay.created is False
    assert replay.membership.membership_status is CampaignMembershipStatus.ARCHIVED
    assert replay.queued_job is None


def test_suppression_is_a_terminal_campaign_eligibility_block(
    db_session: Session,
) -> None:
    campaign = _campaign(db_session)
    contact = _complete_contact(db_session, email="ada@acme.example")
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="ada@acme.example",
        reason=SuppressionReason.OPT_OUT,
        source="phase2-test",
    )

    result = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=True,
    )

    assert result.membership.eligibility_status is CampaignContactEligibility.BLOCKED
    assert result.membership.pipeline_status is PipelineStageStatus.BLOCKED
    assert any(reason["code"] == "suppression" for reason in result.membership.blocking_reasons)
    assert result.queued_job is None
    jobs = db_session.scalars(select(AgentJob)).all()
    assert len(jobs) == 1
    assert jobs[0].agent_id is AgentIdentifier.CAPTURE
    assert jobs[0].status is AgentJobStatus.SUCCEEDED


def test_idempotent_reenrolment_rechecks_late_suppression(
    db_session: Session,
) -> None:
    campaign = _campaign(db_session)
    contact = _complete_contact(db_session, email="late@acme.example")
    first = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        idempotency_key="late-suppression-first",
        enqueue=False,
    )
    assert first.membership.eligibility_status is CampaignContactEligibility.ELIGIBLE

    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="late@acme.example",
        reason=SuppressionReason.OPT_OUT,
        source="late-test",
    )
    replay = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        idempotency_key="late-suppression-replay",
        enqueue=True,
    )

    assert replay.membership.eligibility_status is CampaignContactEligibility.BLOCKED
    assert replay.membership.pipeline_status is PipelineStageStatus.BLOCKED
    stage = db_session.scalars(
        select(CampaignContactAgentState).where(
            CampaignContactAgentState.campaign_contact_id == replay.membership.id,
            CampaignContactAgentState.agent_id == replay.membership.next_stage,
        )
    ).one()
    assert stage.status is PipelineStageStatus.BLOCKED
    assert replay.queued_job is None

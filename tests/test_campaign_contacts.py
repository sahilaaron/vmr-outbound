"""Campaign-contact membership and outreach-history tests (CMP-003).

Covers the nine invariants from issue #21: multi-campaign membership without
losing earlier activity, no duplicate active outreach (both at the
service layer and — genuinely, across independent database connections — at
the database layer), suppression/eligibility gates staying authoritative, and
deduplication (contact merge) never collapsing distinct historical records.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.db.session import engine
from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.enums import ContactWorkflowState, SuppressionReason, SuppressionType
from app.models.external_event import ExternalEvent
from app.services import identity
from app.services.campaign_contacts import (
    CampaignContactNotFound,
    OutreachError,
    campaign_contact_history,
    contact_campaign_history,
    ensure_membership,
    record_outreach_event,
)
from app.services.campaigns import create_campaign
from app.services.contact_state import transition_contact_state
from app.services.suppressions import add_suppression
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


def _now() -> datetime:
    return datetime.now(UTC)


def _contact(
    db: Session,
    *,
    email: str | None,
    domain: str = "acme.example",
    first: str = "Ada",
    last: str = "Lovelace",
) -> Contact:
    contact = Contact(
        first_name=first,
        last_name=last,
        company_name="Acme",
        company_domain=domain,
        email=email,
        natural_key=f"{first.casefold()}|{last.casefold()}|{domain}",
    )
    db.add(contact)
    db.flush()
    return contact


# --- Invariants 1, 2, 3, 6: multi-campaign membership preserves history -------


def test_contact_can_join_two_campaigns_without_losing_campaign_a_history(
    db_session: Session,
) -> None:
    campaign_a = create_campaign(db_session, name="Campaign A")
    campaign_b = create_campaign(db_session, name="Campaign B")
    contact = _contact(db_session, email="ada@acme.example")

    membership_a, created_a = ensure_membership(
        db_session, campaign_id=campaign_a.id, contact_id=contact.id
    )
    assert created_a is True
    assert membership_a.state is ContactWorkflowState.IMPORTED

    event_1, _ = record_outreach_event(
        db_session,
        campaign_contact=membership_a,
        provider="saleshandy",
        external_event_id="evt-1",
        event_type="send_attempted",
        occurred_at=_now(),
        is_outbound=True,
    )
    event_2, _ = record_outreach_event(
        db_session,
        campaign_contact=membership_a,
        provider="saleshandy",
        external_event_id="evt-2",
        event_type="bounced",
        occurred_at=_now(),
    )
    history_before = campaign_contact_history(db_session, membership_a.id)
    assert [e.id for e in history_before] == [event_1.id, event_2.id]

    # The same contact now joins a second, distinct campaign.
    membership_b, created_b = ensure_membership(
        db_session, campaign_id=campaign_b.id, contact_id=contact.id
    )
    assert created_b is True
    assert membership_b.id != membership_a.id
    assert membership_b.state is ContactWorkflowState.IMPORTED

    # Campaign A's membership and history are completely untouched.
    db_session.expire_all()
    reloaded_a = db_session.get(CampaignContact, membership_a.id)
    assert reloaded_a is not None
    assert reloaded_a.state is ContactWorkflowState.IMPORTED
    history_after = campaign_contact_history(db_session, membership_a.id)
    assert [e.id for e in history_after] == [event_1.id, event_2.id]

    # Campaign B starts with no history of its own.
    assert campaign_contact_history(db_session, membership_b.id) == []

    # Both memberships coexist for the same contact — two distinct rows.
    memberships = db_session.scalars(
        select(CampaignContact).where(CampaignContact.contact_id == contact.id)
    ).all()
    assert {m.campaign_id for m in memberships} == {campaign_a.id, campaign_b.id}


def test_completed_outreach_history_does_not_block_future_membership(
    db_session: Session,
) -> None:
    """Invariant 6: old history is never a reason to refuse a new membership."""

    campaign_a = create_campaign(db_session, name="Finished Campaign")
    campaign_b = create_campaign(db_session, name="New Campaign")
    contact = _contact(db_session, email="finished@acme.example")

    membership_a, _ = ensure_membership(
        db_session, campaign_id=campaign_a.id, contact_id=contact.id
    )
    record_outreach_event(
        db_session,
        campaign_contact=membership_a,
        provider="saleshandy",
        external_event_id="fin-1",
        event_type="send_attempted",
        occurred_at=_now(),
        is_outbound=True,
    )
    record_outreach_event(
        db_session,
        campaign_contact=membership_a,
        provider="saleshandy",
        external_event_id="fin-2",
        event_type="unsubscribed",
        occurred_at=_now(),
    )
    transition_contact_state(
        db_session,
        membership_a,
        target=ContactWorkflowState.EXCLUDED,
        actor="tester",
        reason="unsubscribed",
    )

    membership_b, created_b = ensure_membership(
        db_session, campaign_id=campaign_b.id, contact_id=contact.id
    )
    assert created_b is True
    assert membership_b.state is ContactWorkflowState.IMPORTED  # a fresh, unblocked start

    history_a = contact_campaign_history(
        db_session, contact_id=contact.id, campaign_id=campaign_a.id
    )
    assert len(history_a) == 2  # campaign A's completed history is preserved, not touched


# --- Invariant 4: duplicate active membership is rejected/idempotently reused -


def test_ensure_membership_is_idempotent(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="Idempotent Campaign")
    contact = _contact(db_session, email="idempotent@acme.example")

    first, created_first = ensure_membership(
        db_session, campaign_id=campaign.id, contact_id=contact.id
    )
    second, created_second = ensure_membership(
        db_session, campaign_id=campaign.id, contact_id=contact.id
    )

    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    count = db_session.scalar(
        select(func.count(CampaignContact.id)).where(
            CampaignContact.campaign_id == campaign.id,
            CampaignContact.contact_id == contact.id,
        )
    )
    assert count == 1


def test_ensure_membership_unknown_campaign_or_contact_raises(db_session: Session) -> None:
    contact = _contact(db_session, email="orphan@acme.example")
    with pytest.raises(CampaignContactNotFound):
        ensure_membership(db_session, campaign_id=uuid.uuid4(), contact_id=contact.id)

    campaign = create_campaign(db_session, name="Real Campaign")
    with pytest.raises(CampaignContactNotFound):
        ensure_membership(db_session, campaign_id=campaign.id, contact_id=uuid.uuid4())


# --- Invariant 7: suppression / eligibility gates are not bypassed ------------


def test_ensure_membership_starts_suppressed_when_ledger_blocks(db_session: Session) -> None:
    contact = _contact(db_session, email="blocked@acme.example")
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value=contact.email or "",
        reason=SuppressionReason.OPT_OUT,
    )
    campaign = create_campaign(db_session, name="Suppressed Join Campaign")

    membership, created = ensure_membership(
        db_session, campaign_id=campaign.id, contact_id=contact.id
    )
    assert created is True
    assert membership.state is ContactWorkflowState.SUPPRESSED  # never IMPORTED


def test_record_outreach_event_refuses_outbound_when_ledger_suppresses(
    db_session: Session,
) -> None:
    campaign = create_campaign(db_session, name="Ledger Gate Campaign")
    contact = _contact(db_session, email="gate@acme.example")
    membership, _ = ensure_membership(db_session, campaign_id=campaign.id, contact_id=contact.id)
    assert membership.state is ContactWorkflowState.IMPORTED

    # Fine while nothing suppresses this identity.
    record_outreach_event(
        db_session,
        campaign_contact=membership,
        provider="saleshandy",
        external_event_id="gate-1",
        event_type="send_attempted",
        occurred_at=_now(),
        is_outbound=True,
    )

    # A suppression lands on the ledger AFTER the membership was created.
    # membership.state is still IMPORTED (nothing re-checks it automatically) —
    # the gate must still block, proving it is re-evaluated fresh against the
    # ledger and not merely read off a possibly-stale membership state.
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value=contact.email or "",
        reason=SuppressionReason.HARD_BOUNCE,
    )

    with pytest.raises(OutreachError):
        record_outreach_event(
            db_session,
            campaign_contact=membership,
            provider="saleshandy",
            external_event_id="gate-2",
            event_type="send_attempted",
            occurred_at=_now(),
            is_outbound=True,
        )

    # A non-outbound event (e.g. recording the bounce itself) is still allowed —
    # history must stay complete even once a contact becomes ineligible.
    event, created = record_outreach_event(
        db_session,
        campaign_contact=membership,
        provider="saleshandy",
        external_event_id="gate-3",
        event_type="bounced",
        occurred_at=_now(),
    )
    assert created is True


def test_record_outreach_event_refuses_outbound_for_terminal_membership(
    db_session: Session,
) -> None:
    campaign = create_campaign(db_session, name="Terminal Gate Campaign")
    contact = _contact(db_session, email="terminal@acme.example")
    membership, _ = ensure_membership(db_session, campaign_id=campaign.id, contact_id=contact.id)
    transition_contact_state(
        db_session,
        membership,
        target=ContactWorkflowState.EXCLUDED,
        actor="tester",
        reason="excluded",
    )

    with pytest.raises(OutreachError):
        record_outreach_event(
            db_session,
            campaign_contact=membership,
            provider="saleshandy",
            external_event_id="term-1",
            event_type="send_attempted",
            occurred_at=_now(),
            is_outbound=True,
        )

    # History can still be recorded against a terminal membership.
    event, created = record_outreach_event(
        db_session,
        campaign_contact=membership,
        provider="saleshandy",
        external_event_id="term-2",
        event_type="stopped",
        occurred_at=_now(),
    )
    assert created is True


# --- Event-level idempotency (part of invariant 4/9) --------------------------


def test_record_outreach_event_is_idempotent_by_external_id(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="Idempotent Event Campaign")
    contact = _contact(db_session, email="idempotent-event@acme.example")
    membership, _ = ensure_membership(db_session, campaign_id=campaign.id, contact_id=contact.id)

    first, created_first = record_outreach_event(
        db_session,
        campaign_contact=membership,
        provider="saleshandy",
        external_event_id="dup-1",
        event_type="send_attempted",
        occurred_at=_now(),
        is_outbound=True,
    )
    second, created_second = record_outreach_event(
        db_session,
        campaign_contact=membership,
        provider="saleshandy",
        external_event_id="dup-1",
        event_type="send_attempted",
        occurred_at=_now(),
        is_outbound=True,
    )

    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    count = db_session.scalar(
        select(func.count(ExternalEvent.id)).where(
            ExternalEvent.provider == "saleshandy", ExternalEvent.external_event_id == "dup-1"
        )
    )
    assert count == 1


# --- Invariant 5: history survives a membership row being removed -------------


def test_external_event_survives_membership_deletion_via_set_null(db_session: Session) -> None:
    """The FK's ON DELETE SET NULL is the defense-in-depth fallback: even a raw
    membership deletion (not routed through the merge re-parenting logic) must
    not take a historical event down with it."""

    campaign = create_campaign(db_session, name="Set Null Campaign")
    contact = _contact(db_session, email="setnull@acme.example")
    membership, _ = ensure_membership(db_session, campaign_id=campaign.id, contact_id=contact.id)
    event, _ = record_outreach_event(
        db_session,
        campaign_contact=membership,
        provider="saleshandy",
        external_event_id="setnull-1",
        event_type="bounced",
        occurred_at=_now(),
    )

    db_session.delete(membership)
    db_session.flush()
    db_session.expire_all()

    reloaded = db_session.get(ExternalEvent, event.id)
    assert reloaded is not None  # never deleted
    assert reloaded.campaign_contact_id is None
    # Still attributable via the durable keys.
    assert reloaded.contact_id == contact.id
    assert reloaded.campaign_id == campaign.id


# --- Invariant 8: dedup (contact merge) never collapses distinct history ------


def test_merge_collision_reparents_outreach_history_onto_survivor(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="Merge History Campaign")
    survivor = _contact(db_session, email=None, first="Sam", last="Tarly", domain="oldtown.example")
    loser = _contact(db_session, email=None, first="Sam", last="Tarly", domain="oldtown.example")

    survivor_membership = CampaignContact(campaign_id=campaign.id, contact_id=survivor.id)
    loser_membership = CampaignContact(campaign_id=campaign.id, contact_id=loser.id)
    db_session.add_all([survivor_membership, loser_membership])
    db_session.flush()

    event, _ = record_outreach_event(
        db_session,
        campaign_contact=loser_membership,
        provider="saleshandy",
        external_event_id="merge-1",
        event_type="bounced",
        occurred_at=_now(),
    )

    identity.merge_contacts(
        db_session,
        survivor_id=survivor.id,
        loser_id=loser.id,
        idempotency_key="merge-history-1",
        actor="tester",
        reason="confirmed duplicate",
    )

    db_session.expire_all()
    # The loser's redundant membership is gone (coalesced, same as pre-existing
    # DAT-004 behaviour) ...
    assert db_session.get(CampaignContact, loser_membership.id) is None
    # ... but its outreach event survives, re-homed onto the survivor's
    # membership rather than orphaned or deleted.
    reloaded_event = db_session.get(ExternalEvent, event.id)
    assert reloaded_event is not None
    assert reloaded_event.campaign_contact_id == survivor_membership.id
    assert reloaded_event.contact_id == survivor.id

    history = contact_campaign_history(db_session, contact_id=survivor.id, campaign_id=campaign.id)
    assert [e.id for e in history] == [event.id]


# --- Invariant 9: DB-level protection under real concurrency ------------------
#
# These two tests deliberately do NOT use the shared ``db_session`` fixture: it
# wraps every test in one uncommitted outer transaction, which cannot prove a
# cross-connection database race. Each test opens independent sessions bound to
# the same real engine, commits for real, and cleans up its own rows in a
# ``finally`` block.


def test_duplicate_membership_insert_fails_at_db_level_under_concurrency() -> None:
    suffix = uuid.uuid4().hex[:12]
    setup = Session(bind=engine)
    campaign_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    try:
        campaign = Campaign(name=f"Concurrency Campaign {suffix}")
        contact = Contact(
            first_name="Race",
            last_name="Condition",
            company_name="Race Co",
            company_domain=f"race-{suffix}.example",
            email=f"race-{suffix}@race.example",
            natural_key=f"race|condition|race-{suffix}.example",
        )
        setup.add_all([campaign, contact])
        setup.commit()
        campaign_id, contact_id = campaign.id, contact.id

        session_a = Session(bind=engine)
        session_b = Session(bind=engine)
        try:
            # First "concurrent" attempt: succeeds.
            session_a.add(CampaignContact(campaign_id=campaign_id, contact_id=contact_id))
            session_a.commit()

            # Second independent session attempts the exact same insert — a raw
            # second INSERT, not a call through ensure_membership's app-level
            # existence check — and must be rejected by the database itself.
            session_b.add(CampaignContact(campaign_id=campaign_id, contact_id=contact_id))
            with pytest.raises(IntegrityError):
                session_b.commit()
            session_b.rollback()
        finally:
            session_a.close()
            session_b.close()

        remaining = setup.scalar(
            select(func.count(CampaignContact.id)).where(
                CampaignContact.campaign_id == campaign_id,
                CampaignContact.contact_id == contact_id,
            )
        )
        assert remaining == 1
    finally:
        if contact_id is not None:
            setup.execute(delete(CampaignContact).where(CampaignContact.contact_id == contact_id))
            setup.execute(delete(Contact).where(Contact.id == contact_id))
        if campaign_id is not None:
            setup.execute(delete(Campaign).where(Campaign.id == campaign_id))
        setup.commit()
        setup.close()


def test_duplicate_outreach_event_insert_fails_at_db_level_under_concurrency() -> None:
    suffix = uuid.uuid4().hex[:12]
    setup = Session(bind=engine)
    campaign_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    membership_id: uuid.UUID | None = None
    try:
        campaign = Campaign(name=f"Concurrency Event Campaign {suffix}")
        contact = Contact(
            first_name="Dupe",
            last_name="Event",
            company_name="Dupe Co",
            company_domain=f"dupe-{suffix}.example",
            email=f"dupe-{suffix}@dupe.example",
            natural_key=f"dupe|event|dupe-{suffix}.example",
        )
        setup.add_all([campaign, contact])
        setup.flush()
        membership = CampaignContact(campaign_id=campaign.id, contact_id=contact.id)
        setup.add(membership)
        setup.commit()
        campaign_id, contact_id, membership_id = campaign.id, contact.id, membership.id

        session_a = Session(bind=engine)
        session_b = Session(bind=engine)
        try:
            session_a.add(
                ExternalEvent(
                    provider="saleshandy",
                    external_event_id=f"race-evt-{suffix}",
                    event_type="send_attempted",
                    received_at=_now(),
                    contact_id=contact_id,
                    campaign_id=campaign_id,
                    campaign_contact_id=membership_id,
                )
            )
            session_a.commit()

            session_b.add(
                ExternalEvent(
                    provider="saleshandy",
                    external_event_id=f"race-evt-{suffix}",
                    event_type="send_attempted",
                    received_at=_now(),
                    contact_id=contact_id,
                    campaign_id=campaign_id,
                    campaign_contact_id=membership_id,
                )
            )
            with pytest.raises(IntegrityError):
                session_b.commit()
            session_b.rollback()
        finally:
            session_a.close()
            session_b.close()

        remaining = setup.scalar(
            select(func.count(ExternalEvent.id)).where(
                ExternalEvent.provider == "saleshandy",
                ExternalEvent.external_event_id == f"race-evt-{suffix}",
            )
        )
        assert remaining == 1
    finally:
        if campaign_id is not None:
            setup.execute(delete(ExternalEvent).where(ExternalEvent.campaign_id == campaign_id))
            setup.execute(delete(CampaignContact).where(CampaignContact.campaign_id == campaign_id))
        if contact_id is not None:
            setup.execute(delete(Contact).where(Contact.id == contact_id))
        if campaign_id is not None:
            setup.execute(delete(Campaign).where(Campaign.id == campaign_id))
        setup.commit()
        setup.close()

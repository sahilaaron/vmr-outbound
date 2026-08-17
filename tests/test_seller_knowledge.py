"""Seller-side knowledge base service tests (KB-001).

These cover the rules that make the seller side safe to build on: that entering
a record is the only authorization it needs, that archiving never strands a
campaign, that associations are references rather than copies, and that
readiness describes what exists without ever becoming a gate.
"""

from __future__ import annotations

import uuid

import pytest
from app.models.audit_event import AuditEvent
from app.models.campaign import Campaign
from app.models.enums import (
    ContextReadinessState,
    SellerClaimScope,
    SellerOfferingType,
    SellerRecordState,
)
from app.models.seller_knowledge import CampaignOffering, SellerOffering, SellerProofPoint
from app.models.seller_profile import SellerProfile
from app.services.campaigns import create_campaign
from app.services.seller import campaign_offerings, context, readiness, records
from app.services.seller import profile as profile_service
from app.services.seller.common import SellerKnowledgeError, clean_list, parse_lines
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

# --- Helpers -----------------------------------------------------------------


def make_offering(session: Session, *, name: str = "Cement outlook") -> SellerOffering:
    return records.create_offering(
        session,
        name=name,
        offering_type=SellerOfferingType.RESEARCH_REPORT,
        short_description="Annual outlook.",
    )


def make_campaign(session: Session, *, name: str = "Cement EU pilot") -> Campaign:
    return create_campaign(session, name=name)


def audit_actions(session: Session) -> list[str]:
    return list(session.scalars(select(AuditEvent.action)).all())


# --- 1. The company profile --------------------------------------------------


def test_the_profile_starts_absent_and_that_is_a_real_answer(db_session: Session) -> None:
    """No profile is the normal starting state, not a failure."""

    assert profile_service.get_profile(db_session) is None
    item = readiness.company_profile_item(db_session)
    assert item.state is ContextReadinessState.NOT_CONFIGURED


def test_saving_the_profile_twice_edits_one_row_rather_than_making_a_second(
    db_session: Session,
) -> None:
    profile, created = profile_service.save_profile(db_session, name="  Verified Market Research  ")
    assert created is True
    assert profile.name == "Verified Market Research"  # trimmed

    again, created_again = profile_service.save_profile(db_session, name="VMR")
    assert created_again is False
    assert again.id == profile.id
    assert db_session.scalar(select(SellerProfile).where(SellerProfile.name == "VMR")) is not None
    assert len(list(db_session.scalars(select(SellerProfile)).all())) == 1


def test_a_second_current_profile_is_refused_by_the_database(db_session: Session) -> None:
    """The single-profile rule is a schema guarantee, not a service convention."""

    profile_service.save_profile(db_session, name="First")
    db_session.add(SellerProfile(name="Second", is_current=True))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_a_profile_needs_a_name_and_says_so_in_words(db_session: Session) -> None:
    with pytest.raises(SellerKnowledgeError, match="Company name is required"):
        profile_service.save_profile(db_session, name="   ")


def test_an_unanswered_list_and_an_empty_one_are_different_answers(
    db_session: Session,
) -> None:
    """``None`` means nobody looked; ``[]`` means they looked and said none."""

    profile, _ = profile_service.save_profile(
        db_session,
        name="VMR",
        industries_served=None,
        geographies_served=[],
    )
    assert profile.industries_served is None
    assert profile.geographies_served == []

    item = readiness.company_profile_item(db_session)
    assert item.state is ContextReadinessState.INCOMPLETE
    assert "industries served" in item.reason
    # The empty-but-answered list is NOT reported as missing.
    assert "geographies served" not in item.reason


def test_a_complete_profile_reads_as_configured(db_session: Session) -> None:
    profile_service.save_profile(
        db_session,
        name="VMR",
        short_description="Research firm.",
        description="We publish market research.",
        positioning="Depth over breadth.",
        industries_served=["Cement"],
        geographies_served=["EU"],
        capabilities=["Custom research"],
    )
    item = readiness.company_profile_item(db_session)
    assert item.state is ContextReadinessState.CONFIGURED


def test_saving_the_profile_is_audited(db_session: Session) -> None:
    profile_service.save_profile(db_session, name="VMR")
    profile_service.save_profile(db_session, name="VMR")
    assert "seller_profile.created" in audit_actions(db_session)
    assert "seller_profile.updated" in audit_actions(db_session)


# --- 2. Validation -----------------------------------------------------------


def test_list_entries_are_trimmed_deduplicated_and_keep_the_operators_order() -> None:
    assert clean_list(["  Cement ", "Chemicals", "cement", ""], label="x") == [
        "Cement",
        "Chemicals",
    ]


def test_a_list_entry_that_is_really_an_essay_is_refused() -> None:
    with pytest.raises(SellerKnowledgeError, match="too long"):
        clean_list(["x" * 501], label="Capabilities")


def test_a_non_text_list_entry_is_refused() -> None:
    with pytest.raises(SellerKnowledgeError, match="must be text"):
        clean_list([123], label="Capabilities")


def test_an_untouched_textarea_means_not_entered_rather_than_empty() -> None:
    assert parse_lines("") is None
    assert parse_lines("   ") is None
    assert parse_lines("Cement\n\nChemicals") == ["Cement", "Chemicals"]


def test_an_over_long_name_is_refused_before_the_driver_sees_it(db_session: Session) -> None:
    with pytest.raises(SellerKnowledgeError, match="too long"):
        records.create_offering(db_session, name="x" * 256)


# --- 3. Offerings ------------------------------------------------------------


def test_an_offering_is_usable_the_moment_it_is_entered(db_session: Session) -> None:
    """Entering a record IS the approval; there is no second step (KB-001)."""

    offering = make_offering(db_session)
    assert offering.state is SellerRecordState.ACTIVE
    assert offering in records.list_offerings(db_session)
    assert "seller_offering.created" in audit_actions(db_session)


def test_two_active_offerings_may_not_share_a_name(db_session: Session) -> None:
    make_offering(db_session, name="Cement outlook")
    with pytest.raises(SellerKnowledgeError, match="already exists"):
        make_offering(db_session, name="  cement OUTLOOK  ")


def test_archiving_frees_the_name_for_reuse(db_session: Session) -> None:
    """Withdrawing something should not force the operator to rename history."""

    first = make_offering(db_session, name="Cement outlook")
    records.archive_offering(db_session, first)
    second = make_offering(db_session, name="Cement outlook")
    assert second.id != first.id


def test_restoring_an_offering_whose_name_was_reused_is_refused(db_session: Session) -> None:
    first = make_offering(db_session, name="Cement outlook")
    records.archive_offering(db_session, first)
    make_offering(db_session, name="Cement outlook")
    with pytest.raises(SellerKnowledgeError, match="already exists"):
        records.restore_offering(db_session, first)


def test_archiving_is_reversible_and_records_both_directions(db_session: Session) -> None:
    offering = make_offering(db_session)
    assert records.archive_offering(db_session, offering) is True
    assert offering.state is SellerRecordState.ARCHIVED
    assert offering.archived_at is not None
    assert records.restore_offering(db_session, offering) is True
    assert offering.state is SellerRecordState.ACTIVE
    assert offering.archived_at is None
    actions = audit_actions(db_session)
    assert "seller_offering.archived" in actions
    assert "seller_offering.restored" in actions


def test_archiving_something_already_archived_changes_and_records_nothing(
    db_session: Session,
) -> None:
    offering = make_offering(db_session)
    records.archive_offering(db_session, offering)
    before = len(audit_actions(db_session))
    assert records.archive_offering(db_session, offering) is False
    assert len(audit_actions(db_session)) == before


def test_archived_offerings_are_hidden_by_default_and_shown_on_request(
    db_session: Session,
) -> None:
    offering = make_offering(db_session)
    records.archive_offering(db_session, offering)
    assert records.list_offerings(db_session) == []
    assert records.list_offerings(db_session, include_archived=True) == [offering]


def test_editing_an_offering_leaves_its_associations_alone(db_session: Session) -> None:
    offering = make_offering(db_session)
    proof_point = records.create_proof_point(db_session, statement="Since 2009.")
    records.link_to_offering(
        db_session, offering=offering, kind="proof_point", related_id=proof_point.id
    )
    records.update_offering(
        db_session,
        offering,
        name="Cement outlook, annual",
        offering_type=SellerOfferingType.SUBSCRIPTION,
    )
    assert records.proof_points_for_offering(db_session, offering.id) == [proof_point]


# --- 4. Proof points are shared, not copied ----------------------------------


def test_one_proof_point_serves_many_offerings_without_being_duplicated(
    db_session: Session,
) -> None:
    """A fact about us does not become a different fact per offering."""

    first = make_offering(db_session, name="Cement outlook")
    second = make_offering(db_session, name="Chemicals outlook")
    proof_point = records.create_proof_point(db_session, statement="Covering cement since 2009.")

    records.link_to_offering(
        db_session, offering=first, kind="proof_point", related_id=proof_point.id
    )
    records.link_to_offering(
        db_session, offering=second, kind="proof_point", related_id=proof_point.id
    )

    stored = list(db_session.scalars(select(SellerProofPoint)).all())
    assert len(stored) == 1
    assert records.offerings_for_proof_point(db_session, proof_point.id) == [first, second]


def test_correcting_a_proof_point_corrects_it_everywhere(db_session: Session) -> None:
    first = make_offering(db_session, name="Cement outlook")
    second = make_offering(db_session, name="Chemicals outlook")
    proof_point = records.create_proof_point(db_session, statement="Since 2010.")
    for offering in (first, second):
        records.link_to_offering(
            db_session, offering=offering, kind="proof_point", related_id=proof_point.id
        )
    records.update_proof_point(db_session, proof_point, statement="Since 2009.")
    for offering in (first, second):
        linked = records.proof_points_for_offering(db_session, offering.id)
        assert [record.statement for record in linked] == ["Since 2009."]


def test_linking_the_same_proof_point_twice_is_success_and_writes_nothing(
    db_session: Session,
) -> None:
    offering = make_offering(db_session)
    proof_point = records.create_proof_point(db_session, statement="Since 2009.")
    assert (
        records.link_to_offering(
            db_session, offering=offering, kind="proof_point", related_id=proof_point.id
        )
        is True
    )
    before = len(audit_actions(db_session))
    assert (
        records.link_to_offering(
            db_session, offering=offering, kind="proof_point", related_id=proof_point.id
        )
        is False
    )
    assert len(audit_actions(db_session)) == before


def test_the_database_refuses_a_duplicate_association_row(db_session: Session) -> None:
    from app.models.seller_knowledge import SellerOfferingProofPoint

    offering = make_offering(db_session)
    proof_point = records.create_proof_point(db_session, statement="Since 2009.")
    records.link_to_offering(
        db_session, offering=offering, kind="proof_point", related_id=proof_point.id
    )
    db_session.add(SellerOfferingProofPoint(offering_id=offering.id, proof_point_id=proof_point.id))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_unlinking_removes_the_association_and_keeps_the_record(db_session: Session) -> None:
    offering = make_offering(db_session)
    proof_point = records.create_proof_point(db_session, statement="Since 2009.")
    records.link_to_offering(
        db_session, offering=offering, kind="proof_point", related_id=proof_point.id
    )
    assert (
        records.unlink_from_offering(
            db_session, offering=offering, kind="proof_point", related_id=proof_point.id
        )
        is True
    )
    assert records.proof_points_for_offering(db_session, offering.id) == []
    assert records.get_proof_point(db_session, proof_point.id) is not None


def test_unlinking_something_that_was_never_linked_is_not_an_error(db_session: Session) -> None:
    offering = make_offering(db_session)
    assert (
        records.unlink_from_offering(
            db_session, offering=offering, kind="proof_point", related_id=uuid.uuid4()
        )
        is False
    )


def test_linking_an_archived_record_is_refused_with_a_usable_message(
    db_session: Session,
) -> None:
    offering = make_offering(db_session)
    proof_point = records.create_proof_point(db_session, statement="Since 2009.")
    records.archive_proof_point(db_session, proof_point)
    with pytest.raises(SellerKnowledgeError, match="archived"):
        records.link_to_offering(
            db_session, offering=offering, kind="proof_point", related_id=proof_point.id
        )


def test_linking_something_that_does_not_exist_is_refused(db_session: Session) -> None:
    offering = make_offering(db_session)
    with pytest.raises(SellerKnowledgeError, match="no longer exists"):
        records.link_to_offering(
            db_session, offering=offering, kind="persona", related_id=uuid.uuid4()
        )


# --- 5. Restricted claims ----------------------------------------------------


def test_a_global_restriction_needs_no_offering_to_mean_something(db_session: Session) -> None:
    claim = records.create_restricted_claim(
        db_session, title="No guarantees", explanation="Never promise an outcome."
    )
    assert claim.scope is SellerClaimScope.GLOBAL
    assert records.offerings_for_restricted_claim(db_session, claim.id) == []


def test_widening_an_offering_scoped_restriction_drops_its_now_misleading_links(
    db_session: Session,
) -> None:
    """Links that imply a narrowing must not survive the narrowing's removal."""

    offering = make_offering(db_session)
    claim = records.create_restricted_claim(
        db_session,
        title="No named clients",
        explanation="Never name a client.",
        scope=SellerClaimScope.OFFERING,
    )
    records.link_to_offering(
        db_session, offering=offering, kind="restricted_claim", related_id=claim.id
    )
    assert records.restricted_claims_for_offering(db_session, offering.id) == [claim]

    records.update_restricted_claim(
        db_session,
        claim,
        title="No named clients",
        explanation="Never name a client.",
        scope=SellerClaimScope.GLOBAL,
    )
    assert records.restricted_claims_for_offering(db_session, offering.id) == []


def test_a_global_restriction_cannot_be_narrowed_to_one_offering(db_session: Session) -> None:
    """It already applies everywhere; a link would imply a narrowing it never had."""

    offering = make_offering(db_session)
    claim = records.create_restricted_claim(
        db_session, title="No guarantees", explanation="Never promise an outcome."
    )
    with pytest.raises(SellerKnowledgeError, match="applies to everything already"):
        records.link_to_offering(
            db_session, offering=offering, kind="restricted_claim", related_id=claim.id
        )


def test_widening_a_restriction_records_which_offerings_it_used_to_name(
    db_session: Session,
) -> None:
    """The dropped links are the one fact nothing else can reconstruct afterwards."""

    offering = make_offering(db_session)
    claim = records.create_restricted_claim(
        db_session,
        title="No named clients",
        explanation="Never name a client.",
        scope=SellerClaimScope.OFFERING,
    )
    records.link_to_offering(
        db_session, offering=offering, kind="restricted_claim", related_id=claim.id
    )
    records.update_restricted_claim(
        db_session,
        claim,
        title="No named clients",
        explanation="Never name a client.",
        scope=SellerClaimScope.GLOBAL,
    )
    event = db_session.scalars(
        select(AuditEvent)
        .where(AuditEvent.action == "seller_restricted_claim.updated")
        .order_by(AuditEvent.created_at.desc())
    ).first()
    assert event is not None
    assert event.context is not None
    assert event.context["dropped_offering_ids"] == [str(offering.id)]


def test_editing_a_restriction_without_changing_scope_records_no_transition(
    db_session: Session,
) -> None:
    claim = records.create_restricted_claim(
        db_session, title="No guarantees", explanation="Never promise an outcome."
    )
    records.update_restricted_claim(
        db_session,
        claim,
        title="No guarantees at all",
        explanation="Never promise an outcome.",
        scope=SellerClaimScope.GLOBAL,
    )
    event = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "seller_restricted_claim.updated")
    ).first()
    assert event is not None
    assert event.previous_state is None
    assert event.new_state is None


def test_editing_a_proof_point_or_persona_keeps_their_associations(
    db_session: Session,
) -> None:
    """The three record types that are edited from a list, not a detail page."""

    offering = make_offering(db_session)
    proof_point = records.create_proof_point(db_session, statement="Since 2010.")
    persona = records.create_persona(db_session, name="Head of Strategy")
    records.link_to_offering(
        db_session, offering=offering, kind="proof_point", related_id=proof_point.id
    )
    records.link_to_offering(db_session, offering=offering, kind="persona", related_id=persona.id)
    records.update_proof_point(db_session, proof_point, statement="Since 2009.")
    records.update_persona(db_session, persona, name="Head of Corporate Strategy")
    assert records.proof_points_for_offering(db_session, offering.id) == [proof_point]
    assert records.personas_for_offering(db_session, offering.id) == [persona]


def test_the_batched_used_by_lookup_matches_the_per_record_one(
    db_session: Session,
) -> None:
    """The list pages use the batched query; it must not answer differently."""

    first = make_offering(db_session, name="Cement outlook")
    second = make_offering(db_session, name="Chemicals outlook")
    shared = records.create_proof_point(db_session, statement="Since 2009.")
    lonely = records.create_proof_point(db_session, statement="Unlinked.")
    for offering in (first, second):
        records.link_to_offering(
            db_session, offering=offering, kind="proof_point", related_id=shared.id
        )

    batched = records.offerings_by_record(
        db_session, kind="proof_point", record_ids=[shared.id, lonely.id]
    )
    assert batched[shared.id] == records.offerings_for_proof_point(db_session, shared.id)
    # A record with no associations still gets an entry, so the template can
    # index it without a guard.
    assert batched[lonely.id] == []
    assert records.offerings_by_record(db_session, kind="persona", record_ids=[]) == {}


def test_a_restriction_requires_both_a_title_and_an_explanation(db_session: Session) -> None:
    with pytest.raises(SellerKnowledgeError, match="Explanation is required"):
        records.create_restricted_claim(db_session, title="No guarantees", explanation="  ")


# --- 6. Personas are not contacts --------------------------------------------


def test_a_persona_is_a_seller_record_and_touches_no_contact(db_session: Session) -> None:
    from app.models.contact import Contact

    persona = records.create_persona(db_session, name="Head of Strategy")
    assert persona.state is SellerRecordState.ACTIVE
    assert db_session.scalar(select(Contact)) is None


def test_two_active_personas_may_not_share_a_name(db_session: Session) -> None:
    records.create_persona(db_session, name="Head of Strategy")
    with pytest.raises(SellerKnowledgeError, match="already exists"):
        records.create_persona(db_session, name="head of strategy")


# --- 7. Campaign associations ------------------------------------------------


def test_a_campaign_may_concern_several_offerings_with_no_primary_and_no_order(
    db_session: Session,
) -> None:
    campaign = make_campaign(db_session)
    first = make_offering(db_session, name="Cement outlook")
    second = make_offering(db_session, name="Chemicals outlook")

    campaign_offerings.associate(db_session, campaign=campaign, offering_id=first.id)
    campaign_offerings.associate(db_session, campaign=campaign, offering_id=second.id)

    linked = campaign_offerings.offerings_for_campaign(db_session, campaign.id)
    assert set(linked) == {first, second}
    # Nothing on the link says which one leads.
    rows = list(db_session.scalars(select(CampaignOffering)).all())
    assert {column.name for column in CampaignOffering.__table__.columns} == {
        "id",
        "campaign_id",
        "offering_id",
        "created_by",
        "created_at",
    }
    assert len(rows) == 2


def test_a_campaign_with_no_offerings_is_a_valid_state(db_session: Session) -> None:
    campaign = make_campaign(db_session)
    assert campaign_offerings.offerings_for_campaign(db_session, campaign.id) == []
    report = readiness.campaign_report(db_session, campaign)
    item = next(entry for entry in report.items if entry.key == "campaign_offerings")
    assert item.state is ContextReadinessState.NOT_CONFIGURED
    assert "allowed" in item.reason


def test_associating_the_same_offering_twice_is_success_and_writes_nothing(
    db_session: Session,
) -> None:
    campaign = make_campaign(db_session)
    offering = make_offering(db_session)
    _, created = campaign_offerings.associate(
        db_session, campaign=campaign, offering_id=offering.id
    )
    assert created is True
    before = len(audit_actions(db_session))
    _, created_again = campaign_offerings.associate(
        db_session, campaign=campaign, offering_id=offering.id
    )
    assert created_again is False
    assert len(audit_actions(db_session)) == before


def test_archiving_an_offering_leaves_the_campaign_that_names_it_intact(
    db_session: Session,
) -> None:
    """The whole reason archiving exists instead of deletion."""

    campaign = make_campaign(db_session)
    offering = make_offering(db_session)
    campaign_offerings.associate(db_session, campaign=campaign, offering_id=offering.id)

    records.archive_offering(db_session, offering)

    still_linked = campaign_offerings.offerings_for_campaign(db_session, campaign.id)
    assert still_linked == [offering]
    assert still_linked[0].state is SellerRecordState.ARCHIVED


def test_an_archived_offering_cannot_be_newly_added_to_a_campaign(db_session: Session) -> None:
    campaign = make_campaign(db_session)
    offering = make_offering(db_session)
    records.archive_offering(db_session, offering)
    with pytest.raises(SellerKnowledgeError, match="archived"):
        campaign_offerings.associate(db_session, campaign=campaign, offering_id=offering.id)


def test_an_archived_offering_is_kept_out_of_the_campaign_picker(db_session: Session) -> None:
    campaign = make_campaign(db_session)
    live = make_offering(db_session, name="Cement outlook")
    withdrawn = make_offering(db_session, name="Chemicals outlook")
    records.archive_offering(db_session, withdrawn)
    assert campaign_offerings.selectable_offerings(db_session, campaign.id) == [live]


def test_an_already_linked_offering_is_kept_out_of_the_picker(db_session: Session) -> None:
    campaign = make_campaign(db_session)
    offering = make_offering(db_session)
    campaign_offerings.associate(db_session, campaign=campaign, offering_id=offering.id)
    assert campaign_offerings.selectable_offerings(db_session, campaign.id) == []


def test_removing_an_association_changes_nothing_else(db_session: Session) -> None:
    campaign = make_campaign(db_session)
    offering = make_offering(db_session)
    proof_point = records.create_proof_point(db_session, statement="Since 2009.")
    records.link_to_offering(
        db_session, offering=offering, kind="proof_point", related_id=proof_point.id
    )
    campaign_offerings.associate(db_session, campaign=campaign, offering_id=offering.id)

    assert (
        campaign_offerings.dissociate(db_session, campaign=campaign, offering_id=offering.id)
        is True
    )
    assert campaign_offerings.offerings_for_campaign(db_session, campaign.id) == []
    # The offering, its own associations, and the campaign all survive.
    assert records.get_offering(db_session, offering.id) is not None
    assert records.proof_points_for_offering(db_session, offering.id) == [proof_point]
    assert db_session.get(Campaign, campaign.id) is not None


def test_removing_an_association_that_does_not_exist_is_not_an_error(
    db_session: Session,
) -> None:
    campaign = make_campaign(db_session)
    assert (
        campaign_offerings.dissociate(db_session, campaign=campaign, offering_id=uuid.uuid4())
        is False
    )


def test_campaign_association_is_audited_in_both_directions(db_session: Session) -> None:
    campaign = make_campaign(db_session)
    offering = make_offering(db_session)
    campaign_offerings.associate(db_session, campaign=campaign, offering_id=offering.id)
    campaign_offerings.dissociate(db_session, campaign=campaign, offering_id=offering.id)
    actions = audit_actions(db_session)
    assert "campaign.offering_linked" in actions
    assert "campaign.offering_unlinked" in actions


def test_deleting_a_campaign_removes_only_its_links(db_session: Session) -> None:
    campaign = make_campaign(db_session)
    offering = make_offering(db_session)
    campaign_offerings.associate(db_session, campaign=campaign, offering_id=offering.id)
    db_session.delete(campaign)
    db_session.flush()
    assert db_session.scalar(select(CampaignOffering)) is None
    assert records.get_offering(db_session, offering.id) is not None


# --- 8. Readiness is deterministic and explainable ---------------------------


def test_readiness_reports_every_dimension_separately(db_session: Session) -> None:
    report = readiness.seller_report(db_session)
    assert [item.key for item in report.items] == [
        "company_profile",
        "offerings",
        "proof_points",
        "restricted_claims",
        "personas",
    ]
    assert all(item.reason for item in report.items)
    assert report.configured_count == 0


def test_readiness_is_reproducible(db_session: Session) -> None:
    """No model, no randomness — the same database gives the same answer."""

    make_offering(db_session)
    first = readiness.seller_report(db_session)
    second = readiness.seller_report(db_session)
    assert [(item.key, item.state, item.reason) for item in first.items] == [
        (item.key, item.state, item.reason) for item in second.items
    ]


def test_everything_archived_reads_as_incomplete_not_as_never_started(
    db_session: Session,
) -> None:
    offering = make_offering(db_session)
    records.archive_offering(db_session, offering)
    report = readiness.seller_report(db_session)
    item = next(entry for entry in report.items if entry.key == "offerings")
    assert item.state is ContextReadinessState.INCOMPLETE
    assert "archived" in item.reason


def test_the_campaign_messaging_item_is_not_applicable_and_says_why(
    db_session: Session,
) -> None:
    """The campaign record has no messaging or CTA columns yet (CMP-*, DRF-*)."""

    campaign = make_campaign(db_session)
    report = readiness.campaign_report(db_session, campaign)
    item = next(entry for entry in report.items if entry.key == "campaign_messaging")
    assert item.state is ContextReadinessState.NOT_APPLICABLE
    assert "no messaging" in item.reason
    # Not-applicable items are excluded from the "how many could I configure" count.
    assert report.applicable_count == 1


def test_readiness_blocks_nothing(db_session: Session) -> None:
    """An empty knowledge base must not stop a campaign being created or used."""

    report = readiness.seller_report(db_session)
    assert report.configured_count == 0
    campaign = make_campaign(db_session, name="Runs anyway")
    assert campaign.id is not None
    offering = make_offering(db_session)
    _, created = campaign_offerings.associate(
        db_session, campaign=campaign, offering_id=offering.id
    )
    assert created is True


def test_nothing_outside_the_knowledge_base_consults_readiness() -> None:
    """ "Not a gate" is a structural claim, so assert it structurally.

    The behavioural test above would still pass if some unrelated module —
    eligibility, verification, scheduling — imported readiness and consulted it.
    This asserts what the ADR actually promises: nothing outside this feature
    imports it at all.
    """

    import pathlib
    import re

    # Real imports only. A docstring that names the module — as the enum's does
    # — is a cross-reference, not a dependency.
    imports_readiness = re.compile(
        r"^\s*(from\s+app\.services\.seller\s+import\s+[^\n]*\breadiness\b"
        r"|from\s+app\.services\.seller\.readiness\s+import"
        r"|import\s+app\.services\.seller\.readiness)",
        re.MULTILINE,
    )
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    seller_package = root / "services" / "seller"
    # The Knowledge Base may read its own readiness, and a *presentation* surface
    # may render it. Anything else consulting it would make it a gate, which is the
    # thing this test exists to prevent.
    #
    # One presentation surface is named, and it does not branch on the result:
    #
    # * ``web/v2/pages/library.py`` — the Library. It is deliberately
    #   customer-visible: normal users browse it, only an administrator edits the
    #   records behind it (in place, under ``/app/admin/library``), and Campaign
    #   Setup selects approved offerings from it. It displays ``seller_report`` as information
    #   and nothing more — no route it serves refuses, redirects, or withholds on
    #   the strength of readiness, and nothing downstream of it consults the
    #   value. Removing readiness from the Library to satisfy this test would
    #   hide the one thing the page exists to show; making the Library
    #   administrator-only would answer a structural test with a product change.
    #
    # This is a list of exactly one module, not a pattern. ``web/v2/pages/`` as a
    # whole is emphatically not exempt: those modules are where a readiness check
    # would most plausibly turn into an execution gate, so a third importer
    # appearing there must fail here and be argued for on its own terms.
    allowed = {
        root / "web" / "v2" / "pages" / "library.py",
    }
    # An allowance that outlives the import it was written for is a hole: the
    # module could start consulting readiness again — or a new one could be added
    # under a stale name — and this test would say nothing. So every entry has to
    # earn its place on every run. This is what retired ``web/v2/routes.py`` from
    # the set when the Slice-1 route split moved the Library out of it, and
    # ``web/routes.py`` when the legacy Knowledge Base pages were removed.
    for path in sorted(allowed):
        assert path.is_file(), f"{path.relative_to(root)} is allow-listed but does not exist"
        assert imports_readiness.search(path.read_text(encoding="utf-8")) is not None, (
            f"{path.relative_to(root)} is allow-listed but no longer imports readiness; "
            "delete the entry rather than leaving an unearned exemption behind"
        )
    importers = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if seller_package not in path.parents
        and path not in allowed
        and imports_readiness.search(path.read_text(encoding="utf-8")) is not None
    )
    assert importers == [], f"readiness must not be consulted from {importers}"


# --- 9. The context retrieval boundary ---------------------------------------


def test_context_for_a_campaign_returns_only_what_that_campaign_names(
    db_session: Session,
) -> None:
    campaign = make_campaign(db_session)
    named = make_offering(db_session, name="Cement outlook")
    make_offering(db_session, name="Unrelated outlook")
    campaign_offerings.associate(db_session, campaign=campaign, offering_id=named.id)

    assembled = context.assemble(db_session, campaign_id=campaign.id)
    assert [entry.offering.name for entry in assembled.offerings] == ["Cement outlook"]


def test_context_separates_global_restrictions_from_offering_scoped_ones(
    db_session: Session,
) -> None:
    campaign = make_campaign(db_session)
    offering = make_offering(db_session)
    campaign_offerings.associate(db_session, campaign=campaign, offering_id=offering.id)

    global_claim = records.create_restricted_claim(
        db_session, title="No guarantees", explanation="Never promise an outcome."
    )
    scoped = records.create_restricted_claim(
        db_session,
        title="No named clients",
        explanation="Never name a client.",
        scope=SellerClaimScope.OFFERING,
    )
    records.link_to_offering(
        db_session, offering=offering, kind="restricted_claim", related_id=scoped.id
    )

    assembled = context.assemble(db_session, campaign_id=campaign.id)
    assert assembled.global_restricted_claims == (global_claim,)
    assert assembled.offerings[0].restricted_claims == (scoped,)


def test_context_excludes_archived_records_but_flags_an_archived_offering(
    db_session: Session,
) -> None:
    campaign = make_campaign(db_session)
    offering = make_offering(db_session)
    campaign_offerings.associate(db_session, campaign=campaign, offering_id=offering.id)
    withdrawn = records.create_proof_point(db_session, statement="Old claim.")
    records.link_to_offering(
        db_session, offering=offering, kind="proof_point", related_id=withdrawn.id
    )
    records.archive_proof_point(db_session, withdrawn)
    records.archive_offering(db_session, offering)

    assembled = context.assemble(db_session, campaign_id=campaign.id)
    assert assembled.offerings[0].is_archived is True
    assert assembled.offerings[0].proof_points == ()


def test_context_says_plainly_when_a_campaign_names_nothing(db_session: Session) -> None:
    campaign = make_campaign(db_session)
    assembled = context.assemble(db_session, campaign_id=campaign.id)
    assert assembled.offerings == ()
    assert any("valid configuration" in note for note in assembled.notes)


def test_context_reads_and_never_writes(db_session: Session) -> None:
    make_offering(db_session)
    before = len(audit_actions(db_session))
    context.assemble(db_session)
    context.assemble(db_session)
    assert len(audit_actions(db_session)) == before


def test_an_empty_knowledge_base_reports_itself_as_empty(db_session: Session) -> None:
    assert context.assemble(db_session).is_empty is True

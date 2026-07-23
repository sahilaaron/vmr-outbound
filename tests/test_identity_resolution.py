"""DAT-004 identity-resolution service tests.

These exercise the operator resolution of ambiguous imported identities and the
merge of confirmed duplicate contacts, covering every required scenario: a clear
assignment, new-contact creation, intentional separation, a safe merge,
conflicting emails, same-name-different-company, same-company-similar-names,
repeated/already-resolved submissions (idempotency), suppression preservation, a
campaign-membership collision, an interrupted transaction that rolls back, and
malformed/unauthorized requests.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from app.core.config import get_settings
from app.models.audit_event import AuditEvent
from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.enums import (
    ContactWorkflowState,
    IdentityResolutionType,
    ImportRowOutcome,
    SuppressionReason,
    SuppressionType,
)
from app.models.identity_resolution import IdentityResolution
from app.models.import_batch import ImportRow, ImportRowValidation
from app.models.provenance import ProvenanceRecord
from app.services import identity
from app.services.campaigns import campaign_members, create_campaign, get_campaign_overview
from app.services.imports.importer import run_import
from app.services.suppressions import add_suppression
from sqlalchemy import func, select
from sqlalchemy.orm import Session


@pytest.fixture()
def enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("FEATURES__CSV_IMPORT", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def _seed_two_candidates(session: Session, campaign: Campaign) -> list[Contact]:
    """Two contacts sharing a natural key but distinguished by different emails."""

    csv = (
        b"first_name,last_name,company_name,company_domain,email\n"
        b"Jon,Snow,Winterfell,winterfell.example,jon.a@winterfell.example\n"
        b"Jon,Snow,Winterfell,winterfell.example,jon.b@winterfell.example\n"
    )
    run_import(session, campaign_id=campaign.id, content=csv, filename="seed.csv")
    contacts = list(
        session.scalars(select(Contact).where(Contact.company_domain == "winterfell.example")).all()
    )
    assert len(contacts) == 2
    return contacts


def _make_ambiguous_row(session: Session, campaign: Campaign) -> uuid.UUID:
    """Import an email-less row whose natural key matches both seeded candidates."""

    csv = (
        b"first_name,last_name,company_name,company_domain\n"
        b"Jon,Snow,Winterfell,winterfell.example\n"
    )
    summary = run_import(session, campaign_id=campaign.id, content=csv, filename="ambig.csv")
    assert summary.ambiguous_rows == 1
    validation = session.scalars(
        select(ImportRowValidation).where(ImportRowValidation.outcome == ImportRowOutcome.AMBIGUOUS)
    ).first()
    assert validation is not None
    return validation.import_row_id


@pytest.fixture()
def scenario(db_session: Session, enabled: None) -> tuple[Campaign, list[Contact], uuid.UUID]:
    """Candidates live in campaign A; the ambiguous row is imported into campaign B.

    Keeping the two apart means the candidate contacts are not already members of
    the target campaign, so a clean assignment genuinely adds a fresh membership
    (and the collision case can be set up deliberately).
    """

    campaign_a = create_campaign(db_session, name="Seed Campaign A")
    candidates = _seed_two_candidates(db_session, campaign_a)
    campaign_b = create_campaign(db_session, name="Ambiguity Campaign B")
    row_id = _make_ambiguous_row(db_session, campaign_b)
    return campaign_b, candidates, row_id


# --- 1. Clear existing-contact assignment ------------------------------------


def test_assign_to_existing_contact(scenario, db_session: Session) -> None:
    campaign, candidates, row_id = scenario
    target = candidates[0]
    before = db_session.scalar(select(func.count(Contact.id)))

    result = identity.resolve_row(
        db_session,
        import_row_id=row_id,
        action=IdentityResolutionType.ASSIGN_EXISTING,
        idempotency_key="k-assign",
        actor="tester",
        reason="same person",
        target_contact_id=target.id,
    )

    # No new contact; the chosen contact joined the campaign; provenance appended.
    assert db_session.scalar(select(func.count(Contact.id))) == before
    assert result.resolution.target_contact_id == target.id
    membership = db_session.scalars(
        select(CampaignContact).where(
            CampaignContact.campaign_id == campaign.id,
            CampaignContact.contact_id == target.id,
        )
    ).first()
    assert membership is not None and membership.state == ContactWorkflowState.IMPORTED
    prov = db_session.scalar(
        select(func.count(ProvenanceRecord.id)).where(ProvenanceRecord.contact_id == target.id)
    )
    assert prov >= 1
    # Audit + resolution record with actor and reason preserved.
    assert result.resolution.actor == "tester"
    assert result.resolution.reason == "same person"
    audit = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "identity.assign_existing")
    ).first()
    assert audit is not None


def test_original_row_and_outcome_are_preserved(scenario, db_session: Session) -> None:
    _campaign, candidates, row_id = scenario
    raw_before = dict(db_session.get(ImportRow, row_id).raw_data)

    identity.resolve_row(
        db_session,
        import_row_id=row_id,
        action=IdentityResolutionType.ASSIGN_EXISTING,
        idempotency_key="k",
        actor="tester",
        target_contact_id=candidates[0].id,
    )

    row = db_session.get(ImportRow, row_id)
    assert dict(row.raw_data) == raw_before  # raw row never mutated
    validation = db_session.scalars(
        select(ImportRowValidation).where(ImportRowValidation.import_row_id == row_id)
    ).first()
    assert validation.outcome == ImportRowOutcome.AMBIGUOUS  # import history preserved


# --- 2. New-contact creation -------------------------------------------------


def test_create_new_contact(scenario, db_session: Session) -> None:
    campaign, _candidates, row_id = scenario
    before = db_session.scalar(select(func.count(Contact.id)))

    result = identity.resolve_row(
        db_session,
        import_row_id=row_id,
        action=IdentityResolutionType.CREATE_NEW,
        idempotency_key="k-new",
        actor="tester",
    )

    assert db_session.scalar(select(func.count(Contact.id))) == before + 1
    new_contact = db_session.get(Contact, result.resolution.target_contact_id)
    assert new_contact is not None and new_contact.first_name == "Jon"
    membership = db_session.scalars(
        select(CampaignContact).where(CampaignContact.contact_id == new_contact.id)
    ).first()
    assert membership is not None and membership.campaign_id == campaign.id


# --- 3. Intentional separation ----------------------------------------------


def test_mark_separate_creates_distinct_contact(scenario, db_session: Session) -> None:
    _campaign, candidates, row_id = scenario
    result = identity.resolve_row(
        db_session,
        import_row_id=row_id,
        action=IdentityResolutionType.MARK_SEPARATE,
        idempotency_key="k-sep",
        actor="tester",
        reason="different Jon Snow",
    )
    assert result.resolution.resolution_type == IdentityResolutionType.MARK_SEPARATE
    distinguished = result.resolution.resulting_state["distinguished_from"]
    assert set(distinguished) == {str(candidates[0].id), str(candidates[1].id)}


# --- 4. Safe duplicate merge -------------------------------------------------


def _two_emailless_duplicates(session: Session, campaign: Campaign) -> list[Contact]:
    """Two email-less contacts that share a natural key (safe to merge)."""

    a = Contact(
        first_name="Arya",
        last_name="Stark",
        company_name="Winterfell",
        company_domain="winterfell.example",
        natural_key="arya|stark|winterfell.example",
    )
    b = Contact(
        first_name="Arya",
        last_name="Stark",
        company_name="Winterfell",
        company_domain="winterfell.example",
        natural_key="arya|stark|winterfell.example",
    )
    session.add_all([a, b])
    session.flush()
    session.add_all(
        [
            CampaignContact(campaign_id=campaign.id, contact_id=a.id),
            ProvenanceRecord(
                contact_id=b.id,
                import_batch_id=_any_batch(session),
                import_row_id=_any_row(session),
                source_name="seed",
            ),
        ]
    )
    session.flush()
    return [a, b]


def _any_batch(session: Session) -> uuid.UUID:
    from app.models.import_batch import ImportBatch

    return session.scalars(select(ImportBatch.id)).first()


def _any_row(session: Session) -> uuid.UUID:
    return session.scalars(select(ImportRow.id)).first()


def test_safe_merge_transfers_and_tombstones(scenario, db_session: Session) -> None:
    campaign, _candidates, _row_id = scenario
    survivor, loser = _two_emailless_duplicates(db_session, campaign)

    result = identity.merge_contacts(
        db_session,
        survivor_id=survivor.id,
        loser_id=loser.id,
        idempotency_key="k-merge",
        actor="tester",
        reason="confirmed duplicate",
    )

    db_session.refresh(loser)
    db_session.refresh(survivor)
    assert loser.merged_into_id == survivor.id  # tombstoned, not deleted
    assert db_session.get(Contact, loser.id) is not None  # still present
    # Loser's provenance re-homed to survivor; no membership duplicated.
    assert (
        db_session.scalar(
            select(func.count(ProvenanceRecord.id)).where(
                ProvenanceRecord.contact_id == survivor.id
            )
        )
        >= 1
    )
    assert result.resolution.resulting_state["provenance_transferred"] >= 1
    # Merged contact is excluded from later candidate discovery.
    assert loser.id not in {
        c.contact.id for c in identity._find_candidates(db_session, _ambig_validation(db_session))
    }


def _ambig_validation(session: Session) -> ImportRowValidation:
    return session.scalars(
        select(ImportRowValidation).where(ImportRowValidation.outcome == ImportRowOutcome.AMBIGUOUS)
    ).first()


def test_merge_inherits_email_when_survivor_has_none(scenario, db_session: Session) -> None:
    campaign, _candidates, _row_id = scenario
    survivor = Contact(
        first_name="Bran",
        last_name="Stark",
        company_name="Winterfell",
        company_domain="winterfell.example",
        natural_key="bran|stark|winterfell.example",
    )
    loser = Contact(
        first_name="Bran",
        last_name="Stark",
        company_name="Winterfell",
        company_domain="winterfell.example",
        email="bran@winterfell.example",
        natural_key="bran|stark|winterfell.example",
    )
    db_session.add_all([survivor, loser])
    db_session.flush()

    identity.merge_contacts(
        db_session,
        survivor_id=survivor.id,
        loser_id=loser.id,
        idempotency_key="k-inherit",
        actor="tester",
        reason="same person",
    )
    db_session.refresh(survivor)
    db_session.refresh(loser)
    assert survivor.email == "bran@winterfell.example"  # inherited
    assert loser.email is None  # moved, so the unique email index is never violated


# --- 5. Conflicting emails ---------------------------------------------------


def test_merge_with_conflicting_emails_is_refused(scenario, db_session: Session) -> None:
    campaign, candidates, _row_id = scenario
    # candidates[0] and [1] have different emails -> conflicting -> not mergeable.
    with pytest.raises(identity.ResolutionError, match="conflicting emails"):
        identity.merge_contacts(
            db_session,
            survivor_id=candidates[0].id,
            loser_id=candidates[1].id,
            idempotency_key="k-conflict",
            actor="tester",
            reason="same person",
        )
    # Both remain active and separate.
    db_session.refresh(candidates[1])
    assert candidates[1].merged_into_id is None


# --- 6. Same name, different company -----------------------------------------


def test_same_name_different_company_is_not_a_candidate(db_session: Session, enabled: None) -> None:
    campaign = create_campaign(db_session, name="Cross Company")
    _seed_two_candidates(db_session, campaign)  # Jon Snow @ winterfell.example (x2)
    # A different-company Jon Snow: same name, different domain -> different key.
    other = Contact(
        first_name="Jon",
        last_name="Snow",
        company_name="Castle Black",
        company_domain="castleblack.example",
        natural_key="jon|snow|castleblack.example",
    )
    db_session.add(other)
    db_session.flush()
    row_id = _make_ambiguous_row(db_session, campaign)

    review = identity.get_row_review(db_session, row_id)
    candidate_ids = {c.contact.id for c in review.candidates}
    assert other.id not in candidate_ids  # never matched across companies
    assert len(candidate_ids) == 2


# --- 7. Same company, similar (not identical) names --------------------------


def test_same_company_similar_names_do_not_collide(db_session: Session, enabled: None) -> None:
    campaign = create_campaign(db_session, name="Similar Names")
    csv = (
        b"first_name,last_name,company_name,company_domain\n"
        b"Jon,Snow,Winterfell,winterfell.example\n"
        b"Jon,Snowe,Winterfell,winterfell.example\n"  # 'Snowe' != 'Snow'
    )
    summary = run_import(db_session, campaign_id=campaign.id, content=csv, filename="similar.csv")
    # Different natural keys -> two separate accepted contacts, no ambiguity.
    assert summary.ambiguous_rows == 0
    assert summary.accepted_rows == 2


# --- 8 & 9. Repeated submission / already-resolved (idempotency) -------------


def test_repeated_submission_is_idempotent(scenario, db_session: Session) -> None:
    _campaign, candidates, row_id = scenario
    first = identity.resolve_row(
        db_session,
        import_row_id=row_id,
        action=IdentityResolutionType.ASSIGN_EXISTING,
        idempotency_key="k-once",
        actor="tester",
        target_contact_id=candidates[0].id,
    )
    contacts_after_first = db_session.scalar(select(func.count(Contact.id)))
    memberships_after_first = db_session.scalar(select(func.count(CampaignContact.id)))

    # Same key again -> returns the same decision, mutates nothing.
    again = identity.resolve_row(
        db_session,
        import_row_id=row_id,
        action=IdentityResolutionType.ASSIGN_EXISTING,
        idempotency_key="k-once",
        actor="tester",
        target_contact_id=candidates[0].id,
    )
    assert again.reused is True
    assert again.resolution.id == first.resolution.id
    assert db_session.scalar(select(func.count(Contact.id))) == contacts_after_first
    assert db_session.scalar(select(func.count(CampaignContact.id))) == memberships_after_first
    assert db_session.scalar(select(func.count(IdentityResolution.id))) == 1


def test_already_resolved_row_leaves_queue_and_blocks_new_action(
    scenario, db_session: Session
) -> None:
    _campaign, candidates, row_id = scenario
    identity.resolve_row(
        db_session,
        import_row_id=row_id,
        action=IdentityResolutionType.ASSIGN_EXISTING,
        idempotency_key="k-a",
        actor="tester",
        target_contact_id=candidates[0].id,
    )
    # Out of the work queue, but the detail still renders (marked resolved).
    items, total = identity.list_review_queue(db_session)
    assert total == 0 and items == []
    review = identity.get_row_review(db_session, row_id)
    assert review is not None and review.existing_resolution is not None
    # A second submission for the already-resolved row is a safe no-op: it returns
    # the recorded decision and never applies a second, conflicting mutation.
    contacts_before = db_session.scalar(select(func.count(Contact.id)))
    again = identity.resolve_row(
        db_session,
        import_row_id=row_id,
        action=IdentityResolutionType.CREATE_NEW,
        idempotency_key="k-b",
        actor="tester",
    )
    assert again.reused is True
    assert again.resolution.resolution_type == IdentityResolutionType.ASSIGN_EXISTING
    assert db_session.scalar(select(func.count(Contact.id))) == contacts_before


# --- 10. Suppression preservation --------------------------------------------


def test_suppression_forces_suppressed_membership(scenario, db_session: Session) -> None:
    campaign, candidates, row_id = scenario
    # A suppression arrives between import and review.
    add_suppression(
        db_session,
        suppression_type=SuppressionType.DOMAIN,
        value="winterfell.example",
        reason=SuppressionReason.INTERNAL_EXCLUSION,
    )
    result = identity.resolve_row(
        db_session,
        import_row_id=row_id,
        action=IdentityResolutionType.CREATE_NEW,
        idempotency_key="k-supp",
        actor="tester",
    )
    membership = db_session.scalars(
        select(CampaignContact).where(
            CampaignContact.contact_id == result.resolution.target_contact_id
        )
    ).first()
    assert membership.state == ContactWorkflowState.SUPPRESSED  # cannot enter outreach
    assert result.resolution.resulting_state["suppression_enforced"] is True


def test_merge_into_suppressed_survivor_stays_suppressed(scenario, db_session: Session) -> None:
    campaign, _candidates, _row_id = scenario
    survivor, loser = _two_emailless_duplicates(db_session, campaign)
    add_suppression(
        db_session,
        suppression_type=SuppressionType.DOMAIN,
        value="winterfell.example",
        reason=SuppressionReason.OPT_OUT,
    )
    identity.merge_contacts(
        db_session,
        survivor_id=survivor.id,
        loser_id=loser.id,
        idempotency_key="k-ms",
        actor="tester",
        reason="same person",
    )
    for membership in db_session.scalars(
        select(CampaignContact).where(CampaignContact.contact_id == survivor.id)
    ).all():
        assert membership.state == ContactWorkflowState.SUPPRESSED


# --- 11. Campaign-membership collision ---------------------------------------


def test_assign_to_already_member_does_not_duplicate(scenario, db_session: Session) -> None:
    campaign, candidates, row_id = scenario
    target = candidates[0]
    # Put the target into the campaign first.
    db_session.add(CampaignContact(campaign_id=campaign.id, contact_id=target.id))
    db_session.flush()
    memberships_before = db_session.scalar(
        select(func.count(CampaignContact.id)).where(
            CampaignContact.campaign_id == campaign.id,
            CampaignContact.contact_id == target.id,
        )
    )
    assert memberships_before == 1

    result = identity.resolve_row(
        db_session,
        import_row_id=row_id,
        action=IdentityResolutionType.ASSIGN_EXISTING,
        idempotency_key="k-coll",
        actor="tester",
        target_contact_id=target.id,
    )
    memberships_after = db_session.scalar(
        select(func.count(CampaignContact.id)).where(
            CampaignContact.campaign_id == campaign.id,
            CampaignContact.contact_id == target.id,
        )
    )
    assert memberships_after == 1  # no duplicate active membership
    assert result.preview.membership_collision is True
    # Provenance is still recorded for the observation.
    assert (
        db_session.scalar(
            select(func.count(ProvenanceRecord.id)).where(ProvenanceRecord.contact_id == target.id)
        )
        >= 1
    )


# --- 12. Interrupted transaction and rollback --------------------------------


def test_interrupted_resolution_rolls_back(scenario, db_session: Session) -> None:
    _campaign, _candidates, row_id = scenario
    contacts_before = db_session.scalar(select(func.count(Contact.id)))

    def boom() -> None:
        raise RuntimeError("simulated crash mid-transaction")

    with pytest.raises(RuntimeError, match="simulated crash"):
        identity.resolve_row(
            db_session,
            import_row_id=row_id,
            action=IdentityResolutionType.CREATE_NEW,
            idempotency_key="k-fault",
            actor="tester",
            _fault=boom,
        )
    # Nothing persisted: no new contact, no resolution record.
    assert db_session.scalar(select(func.count(Contact.id))) == contacts_before
    assert (
        db_session.scalar(
            select(func.count(IdentityResolution.id)).where(
                IdentityResolution.idempotency_key == "k-fault"
            )
        )
        == 0
    )
    # The row is still resolvable afterwards (recovery).
    assert identity.get_row_review(db_session, row_id) is not None


# --- 13. Unauthorized / malformed requests -----------------------------------


def test_resolve_unknown_row_raises(db_session: Session, enabled: None) -> None:
    with pytest.raises(identity.ResolutionError):
        identity.resolve_row(
            db_session,
            import_row_id=uuid.uuid4(),
            action=IdentityResolutionType.CREATE_NEW,
            idempotency_key="k",
            actor="t",
        )


def test_assign_to_non_candidate_raises(scenario, db_session: Session) -> None:
    _campaign, _candidates, row_id = scenario
    stranger = Contact(
        first_name="Tywin",
        last_name="Lannister",
        company_name="Casterly",
        company_domain="casterly.example",
        natural_key="tywin|lannister|casterly.example",
    )
    db_session.add(stranger)
    db_session.flush()
    with pytest.raises(identity.ResolutionError, match="candidate"):
        identity.resolve_row(
            db_session,
            import_row_id=row_id,
            action=IdentityResolutionType.ASSIGN_EXISTING,
            idempotency_key="k",
            actor="t",
            target_contact_id=stranger.id,
        )


def test_merge_missing_target_raises(scenario, db_session: Session) -> None:
    _campaign, candidates, _row_id = scenario
    with pytest.raises(identity.ResolutionError):
        identity.merge_contacts(
            db_session,
            survivor_id=candidates[0].id,
            loser_id=None,  # type: ignore[arg-type]
            idempotency_key="k",
            actor="t",
        )


def test_resolve_non_ambiguous_row_raises(db_session: Session, enabled: None) -> None:
    campaign = create_campaign(db_session, name="Plain Import")
    csv = (
        b"first_name,last_name,company_name,company_domain,email\n"
        b"Sansa,Stark,Winterfell,winterfell.example,sansa@winterfell.example\n"
    )
    run_import(db_session, campaign_id=campaign.id, content=csv, filename="ok.csv")
    accepted = db_session.scalars(
        select(ImportRowValidation).where(ImportRowValidation.outcome == ImportRowOutcome.ACCEPTED)
    ).first()
    with pytest.raises(identity.ResolutionError):
        identity.resolve_row(
            db_session,
            import_row_id=accepted.import_row_id,
            action=IdentityResolutionType.CREATE_NEW,
            idempotency_key="k",
            actor="t",
        )


# --- Review-verdict corrections (PR #122): negative tests ---------------------


def _emailless_pair(session: Session, *, first: str, domain: str) -> list[Contact]:
    """Two email-less contacts sharing a natural key under one domain."""

    pair = [
        Contact(
            first_name=first,
            last_name="Doe",
            company_name="Co",
            company_domain=domain,
            natural_key=f"{first.casefold()}|doe|{domain}",
        )
        for _ in range(2)
    ]
    session.add_all(pair)
    session.flush()
    return pair


def test_row_merge_rejects_non_candidate_contacts(scenario, db_session: Session) -> None:
    """Finding 1: a row-driven merge may only combine that row's own candidates.

    A forged POST naming two arbitrary (existing, active, non-conflicting)
    contacts must be refused — the candidate set is recomputed from the row.
    """

    _campaign, candidates, row_id = scenario
    # A stranger who is NOT a candidate of the ambiguous row.
    stranger = Contact(
        first_name="Petyr",
        last_name="Baelish",
        company_name="Mockingbird",
        company_domain="mockingbird.example",
        natural_key="petyr|baelish|mockingbird.example",
    )
    db_session.add(stranger)
    db_session.flush()

    with pytest.raises(identity.ResolutionError, match="candidate"):
        identity.merge_contacts(
            db_session,
            survivor_id=candidates[0].id,
            loser_id=stranger.id,
            idempotency_key="k-forge",
            actor="attacker",
            reason="forged",
            import_row_id=row_id,
        )
    db_session.refresh(stranger)
    assert stranger.merged_into_id is None  # untouched


def test_merge_preserves_suppression_from_loser_only(db_session: Session, enabled: None) -> None:
    """Finding 2: a ledger hit on the LOSER alone still suppresses the survivor.

    The survivor's domain is clean; the loser's domain is suppressed. After the
    merge the survivor's memberships must all be SUPPRESSED — otherwise a
    suppressed identity would silently stay eligible.
    """

    campaign = create_campaign(db_session, name="Loser Suppression")
    survivor = Contact(
        first_name="Olenna",
        last_name="Tyrell",
        company_name="Highgarden",
        company_domain="highgarden.example",
        natural_key="olenna|tyrell|highgarden.example",
    )
    loser = Contact(
        first_name="Olenna",
        last_name="Tyrell",
        company_name="Old Co",
        company_domain="oldco.example",
        natural_key="olenna|tyrell|oldco.example",
    )
    db_session.add_all([survivor, loser])
    db_session.flush()
    db_session.add(CampaignContact(campaign_id=campaign.id, contact_id=survivor.id))
    db_session.flush()
    # Only the loser's domain is suppressed.
    add_suppression(
        db_session,
        suppression_type=SuppressionType.DOMAIN,
        value="oldco.example",
        reason=SuppressionReason.OPT_OUT,
    )

    identity.merge_contacts(
        db_session,
        survivor_id=survivor.id,
        loser_id=loser.id,
        idempotency_key="k-loser-supp",
        actor="tester",
        reason="same person",
    )
    memberships = db_session.scalars(
        select(CampaignContact).where(CampaignContact.contact_id == survivor.id)
    ).all()
    assert memberships and all(m.state == ContactWorkflowState.SUPPRESSED for m in memberships)


def test_merge_collision_coalesces_membership_off_loser(db_session: Session, enabled: None) -> None:
    """Finding 3: a colliding membership must not stay active on the tombstone.

    Both contacts are members of the same campaign. After the merge only the
    survivor may hold an active membership; the campaign count and member reads
    must not include the merged-away identity.
    """

    campaign = create_campaign(db_session, name="Collision Campaign")
    survivor, loser = _emailless_pair(db_session, first="Davos", domain="seaworth.example")
    db_session.add_all(
        [
            CampaignContact(campaign_id=campaign.id, contact_id=survivor.id),
            CampaignContact(campaign_id=campaign.id, contact_id=loser.id),
        ]
    )
    db_session.flush()
    assert get_campaign_overview(db_session, campaign.id).contact_count == 2

    result = identity.merge_contacts(
        db_session,
        survivor_id=survivor.id,
        loser_id=loser.id,
        idempotency_key="k-coalesce",
        actor="tester",
        reason="same person",
    )

    # Only the survivor remains a member; the loser has no active membership.
    members, total = campaign_members(db_session, campaign.id)
    assert total == 1
    assert [c.id for _m, c in members] == [survivor.id]
    assert (
        db_session.scalar(
            select(func.count(CampaignContact.id)).where(CampaignContact.contact_id == loser.id)
        )
        == 0
    )
    assert get_campaign_overview(db_session, campaign.id).contact_count == 1
    assert result.resolution.resulting_state["coalesced_campaigns"] == [str(campaign.id)]


def test_merge_requires_non_empty_reason(scenario, db_session: Session) -> None:
    """Finding 4: a destructive merge is refused without an operator reason."""

    _campaign, _candidates, campaign_row = scenario
    survivor, loser = _emailless_pair(db_session, first="Missandei", domain="naath.example")
    for bad_reason in (None, "", "   "):
        with pytest.raises(identity.ResolutionError, match="reason"):
            identity.merge_contacts(
                db_session,
                survivor_id=survivor.id,
                loser_id=loser.id,
                idempotency_key=f"k-noreason-{bad_reason!r}",
                actor="tester",
                reason=bad_reason,
            )
    db_session.refresh(loser)
    assert loser.merged_into_id is None  # nothing merged


def test_mark_separate_resolves_present_row_only(db_session: Session, enabled: None) -> None:
    """Clarification: MARK_SEPARATE resolves the current row only.

    It never auto-suppresses future matching, so a later import sharing the same
    natural key is flagged ambiguous again for a fresh, explicit decision.
    """

    campaign_a = create_campaign(db_session, name="Sep A")
    _seed_two_candidates(db_session, campaign_a)
    campaign_b = create_campaign(db_session, name="Sep B")
    row_id = _make_ambiguous_row(db_session, campaign_b)

    identity.resolve_row(
        db_session,
        import_row_id=row_id,
        action=IdentityResolutionType.MARK_SEPARATE,
        idempotency_key="k-sep-scope",
        actor="tester",
        reason="distinct person",
    )
    # A later import of the same name (now three same-key contacts) is ambiguous again.
    csv = (
        b"first_name,last_name,company_name,company_domain\n"
        b"Jon,Snow,Winterfell,winterfell.example\n"
    )
    summary = run_import(
        db_session, campaign_id=campaign_b.id, content=csv, filename="ambig-again.csv"
    )
    assert summary.ambiguous_rows == 1

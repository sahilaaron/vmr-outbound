"""Identity resolution through the REAL capture-to-contact promotion path (DAT-019).

The service-level rules are covered in ``test_linkedin_identity_links.py``. These
tests are the production path: a real DAT-013 submission is staged through
``capture_intake``, its domain is confirmed, and ``promotion.promote`` runs — the
same sequence the operator drives. What is asserted is what ends up in the
database afterwards.

The governing rule, unchanged: a member id and a vanity URL are associated
automatically only when both were directly observed in the same authenticated
capture for the same displayed person. Everything else preserves both
identifiers, and an unresolved duplicate is safer than a false merge.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from app.core.config import get_settings
from app.models.contact import Contact
from app.models.enums import (
    ContactPromotionOutcome,
    EnrichmentConfirmationSource,
    IdentityLinkDecision,
    IdentityLinkState,
    LinkedInIdentifierKind,
)
from app.models.linkedin_identity_link import LinkedInIdentityLink
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.services import identity_links
from app.services.captures import intake as capture_intake
from app.services.captures import promotion as promo
from sqlalchemy import func, select
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "extensions" / "salesnav-capture" / "docs" / "fixtures"
SALESNAV_SUBMISSION = json.loads(
    (FIXTURES / "contact-capture.salesnav.example.json").read_text("utf-8")
)

LOOPBACK = "http://127.0.0.1:8000"

# Row one: a member id and no visible /in/ link.
MEMBER_ONLY_ID = "ACwAAAB1x9k"
MEMBER_ONLY_DOMAIN = "northwind-logistics.example"
# Row two: BOTH identifiers directly observed on the same row.
BOTH_MEMBER_ID = "ACwAAAC9zzz"
BOTH_VANITY = "https://www.linkedin.com/in/tomoya-okaku"
BOTH_DOMAIN = "sakura-robotics.example"
# The fixture person for that row, as the capture actually names them.
BOTH_FIRST = "大角"
BOTH_LAST = "知也"


@pytest.fixture()
def enable_capture(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_INTAKE", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_PROMOTION", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def _stage(db: Session, submission: dict[str, Any]) -> list[LinkedInProfileSnapshot]:
    payload = copy.deepcopy(submission)
    payload["client_submission_id"] = str(uuid.uuid4())
    for capture in payload["contacts"]:
        capture["client_capture_id"] = str(uuid.uuid4())
    result = capture_intake.stage_contact_captures(db, payload=payload, operator_base_url=LOOPBACK)
    ids = [uuid.UUID(str(r.capture_id)) for r in result.results]
    return [db.get(LinkedInProfileSnapshot, cid) for cid in ids]  # type: ignore[misc]


def _member_only(db: Session) -> LinkedInProfileSnapshot:
    captures = _stage(db, SALESNAV_SUBMISSION)
    return next(c for c in captures if c.salesnav_member_id == MEMBER_ONLY_ID)


def _both(db: Session) -> LinkedInProfileSnapshot:
    captures = _stage(db, SALESNAV_SUBMISSION)
    return next(c for c in captures if c.salesnav_member_id == BOTH_MEMBER_ID)


def _promote(db: Session, snapshot: LinkedInProfileSnapshot, domain: str) -> Any:
    # A manual override: these fixtures never ran a provider lookup, and the
    # company half is not what is under test here.
    promo.confirm_domain(
        db,
        snapshot=snapshot,
        source=EnrichmentConfirmationSource.MANUAL,
        domain=domain,
        actor="test",
    )
    return promo.promote(db, snapshot=snapshot, actor="test")


def _links(db: Session, kind: LinkedInIdentifierKind) -> list[LinkedInIdentityLink]:
    return list(
        db.scalars(
            select(LinkedInIdentityLink).where(LinkedInIdentityLink.identifier_kind == kind.value)
        ).all()
    )


# --- 1. member id, no observed URL -------------------------------------------


def test_promoting_a_member_only_row_records_the_id_and_invents_no_url(
    db_session: Session, enable_capture: None
) -> None:
    snapshot = _member_only(db_session)
    assert snapshot.normalized_profile_url is None, "precondition: no /in/ link was visible"
    assert snapshot.salesnav_member_id == MEMBER_ONLY_ID

    result = _promote(db_session, snapshot, MEMBER_ONLY_DOMAIN)
    assert result.contact_outcome is ContactPromotionOutcome.CONTACT_CREATED

    contact = db_session.scalars(select(Contact)).one()
    assert contact.linkedin_url is None, "a member id is not a published profile URL"

    member_links = _links(db_session, LinkedInIdentifierKind.SALESNAV_MEMBER_ID)
    assert [x.identifier_value for x in member_links] == [MEMBER_ONLY_ID]
    assert member_links[0].contact_id == contact.id
    assert member_links[0].state == IdentityLinkState.ACTIVE
    assert member_links[0].capture_id == snapshot.id
    # Nothing was bridged: there was no second identifier to bridge to.
    assert member_links[0].decision_kind == IdentityLinkDecision.OBSERVED_CAPTURE
    assert _links(db_session, LinkedInIdentifierKind.PUBLIC_VANITY_URL) == []


def test_the_member_id_keeps_its_casing_through_the_whole_production_path(
    db_session: Session, enable_capture: None
) -> None:
    snapshot = _member_only(db_session)
    _promote(db_session, snapshot, MEMBER_ONLY_DOMAIN)

    stored = _links(db_session, LinkedInIdentifierKind.SALESNAV_MEMBER_ID)[0].identifier_value
    assert stored == MEMBER_ONLY_ID
    assert stored != MEMBER_ONLY_ID.lower()
    assert snapshot.salesnav_member_id == MEMBER_ONLY_ID


# --- 2 & 3. both observed on one capture -> the bridge ------------------------


def test_a_row_showing_both_identifiers_bridges_them(
    db_session: Session, enable_capture: None
) -> None:
    snapshot = _both(db_session)
    assert snapshot.normalized_profile_url == BOTH_VANITY
    assert snapshot.salesnav_member_id == BOTH_MEMBER_ID

    _promote(db_session, snapshot, BOTH_DOMAIN)
    contact = db_session.scalars(select(Contact)).one()

    for kind, value in (
        (LinkedInIdentifierKind.SALESNAV_MEMBER_ID, BOTH_MEMBER_ID),
        (LinkedInIdentifierKind.PUBLIC_VANITY_URL, BOTH_VANITY),
    ):
        found = identity_links.lookup_contact(db_session, kind, value)
        assert found is not None and found.id == contact.id


def test_the_bridge_rows_keep_the_capture_and_the_co_occurrence_that_justified_them(
    db_session: Session, enable_capture: None
) -> None:
    snapshot = _both(db_session)
    _promote(db_session, snapshot, BOTH_DOMAIN)

    rows = _links(db_session, LinkedInIdentifierKind.SALESNAV_MEMBER_ID) + _links(
        db_session, LinkedInIdentifierKind.PUBLIC_VANITY_URL
    )
    assert len(rows) == 2
    for row in rows:
        assert row.decision_kind == IdentityLinkDecision.SAME_CAPTURE_OBSERVED
        assert row.capture_id == snapshot.id
        assert row.source_surface == snapshot.source_surface
        assert row.corroboration is not None
        assert row.corroboration["rule"] == "same_capture_co_occurrence"
        assert row.corroboration["observed_member_id"] == BOTH_MEMBER_ID
        assert row.corroboration["observed_vanity_url"] == BOTH_VANITY


# --- 4. idempotency -----------------------------------------------------------


def test_recapturing_the_same_row_creates_no_second_contact_or_link(
    db_session: Session, enable_capture: None
) -> None:
    first = _both(db_session)
    _promote(db_session, first, BOTH_DOMAIN)

    again = _both(db_session)  # a fresh submission of the identical row
    _promote(db_session, again, BOTH_DOMAIN)

    assert db_session.scalar(select(func.count()).select_from(Contact)) == 1
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(LinkedInIdentityLink)
            .where(LinkedInIdentityLink.state == IdentityLinkState.ACTIVE.value)
        )
        == 2
    )


# --- 5 & 6. the two surfaces meet, in both orders -----------------------------


def test_capturing_the_profile_page_afterwards_resolves_to_the_same_contact(
    db_session: Session, enable_capture: None
) -> None:
    """Sales Navigator row (both identifiers) first, profile page second."""

    _promote(db_session, _both(db_session), BOTH_DOMAIN)
    contact_id = db_session.scalars(select(Contact)).one().id

    # The profile page carries only the handle; the bridge is what recognises it.
    matched = identity_links.lookup_contact(
        db_session, LinkedInIdentifierKind.PUBLIC_VANITY_URL, BOTH_VANITY
    )
    assert matched is not None and matched.id == contact_id


def test_profile_first_then_a_row_showing_both_still_yields_one_contact(
    db_session: Session, enable_capture: None
) -> None:
    existing = Contact(
        first_name=BOTH_FIRST,
        last_name=BOTH_LAST,
        company_name="Sakura Robotics",
        company_domain=BOTH_DOMAIN,
        linkedin_url=BOTH_VANITY,
        natural_key=f"{BOTH_FIRST.casefold()}|{BOTH_LAST.casefold()}|{BOTH_DOMAIN}",
    )
    db_session.add(existing)
    db_session.flush()
    identity_links.record_observed(
        db_session,
        contact=existing,
        kind=LinkedInIdentifierKind.PUBLIC_VANITY_URL,
        value=BOTH_VANITY,
        decided_by="test",
    )
    db_session.flush()

    result = _promote(db_session, _both(db_session), BOTH_DOMAIN)
    assert result.contact_outcome is ContactPromotionOutcome.CONTACT_EXACT_MATCH_LINKED
    assert db_session.scalar(select(func.count()).select_from(Contact)) == 1
    # The member id has now joined the same contact, through the observed pair.
    found = identity_links.lookup_contact(
        db_session, LinkedInIdentifierKind.SALESNAV_MEMBER_ID, BOTH_MEMBER_ID
    )
    assert found is not None and found.id == existing.id


# --- 7 & 8. conflict routes to review, never merges ---------------------------


def test_an_identifier_owned_by_another_contact_blocks_instead_of_merging(
    db_session: Session, enable_capture: None
) -> None:
    """The member id belongs to someone the natural key does not resolve to."""

    other = Contact(
        first_name="Someone",
        last_name="Else",
        company_name="Sakura Robotics",
        company_domain=BOTH_DOMAIN,
        natural_key=f"someone|else|{BOTH_DOMAIN}",
    )
    db_session.add(other)
    db_session.flush()
    identity_links.record_observed(
        db_session,
        contact=other,
        kind=LinkedInIdentifierKind.SALESNAV_MEMBER_ID,
        value=BOTH_MEMBER_ID,
        decided_by="test",
    )
    # And a DIFFERENT contact owns the handle.
    rival = Contact(
        first_name=BOTH_FIRST,
        last_name=BOTH_LAST,
        company_name="Sakura Robotics",
        company_domain=BOTH_DOMAIN,
        linkedin_url=BOTH_VANITY,
        natural_key=f"{BOTH_FIRST.casefold()}|{BOTH_LAST.casefold()}|{BOTH_DOMAIN}",
    )
    db_session.add(rival)
    db_session.flush()
    identity_links.record_observed(
        db_session,
        contact=rival,
        kind=LinkedInIdentifierKind.PUBLIC_VANITY_URL,
        value=BOTH_VANITY,
        decided_by="test",
    )
    db_session.flush()

    before = db_session.scalar(select(func.count()).select_from(Contact))
    result = _promote(db_session, _both(db_session), BOTH_DOMAIN)

    assert result.contact_outcome is ContactPromotionOutcome.CONTACT_IDENTITY_AMBIGUOUS
    assert db_session.scalar(select(func.count()).select_from(Contact)) == before
    for contact in (other, rival):
        db_session.refresh(contact)
        assert contact.merged_into_id is None


def test_two_identifiers_pointing_at_two_contacts_is_reported_not_reconciled(
    db_session: Session, enable_capture: None
) -> None:
    result_detail = None
    other = Contact(
        first_name="Someone",
        last_name="Else",
        company_name="Sakura Robotics",
        company_domain=BOTH_DOMAIN,
        natural_key=f"someone|else|{BOTH_DOMAIN}",
    )
    rival = Contact(
        first_name="Another",
        last_name="Person",
        company_name="Sakura Robotics",
        company_domain=BOTH_DOMAIN,
        natural_key=f"another|person|{BOTH_DOMAIN}",
    )
    db_session.add_all([other, rival])
    db_session.flush()
    identity_links.record_observed(
        db_session,
        contact=other,
        kind=LinkedInIdentifierKind.SALESNAV_MEMBER_ID,
        value=BOTH_MEMBER_ID,
        decided_by="test",
    )
    identity_links.record_observed(
        db_session,
        contact=rival,
        kind=LinkedInIdentifierKind.PUBLIC_VANITY_URL,
        value=BOTH_VANITY,
        decided_by="test",
    )
    db_session.flush()

    result = _promote(db_session, _both(db_session), BOTH_DOMAIN)
    result_detail = result.detail or {}

    assert result.contact_outcome is ContactPromotionOutcome.CONTACT_IDENTITY_AMBIGUOUS
    assert sorted(result_detail.get("ambiguous_contact_ids", [])) == sorted(
        {str(other.id), str(rival.id)}
    )
    db_session.refresh(other)
    db_session.refresh(rival)
    assert other.merged_into_id is None and rival.merged_into_id is None


# --- 9 & 10. rows that must not answer a match --------------------------------


def test_a_suspected_legacy_alias_is_ignored_when_matching(
    db_session: Session, enable_capture: None
) -> None:
    legacy = Contact(
        first_name="Legacy",
        last_name="Person",
        company_name="Sakura Robotics",
        company_domain=BOTH_DOMAIN,
        linkedin_url=BOTH_VANITY,
        natural_key=f"legacy|person|{BOTH_DOMAIN}",
    )
    db_session.add(legacy)
    db_session.flush()
    db_session.add(
        LinkedInIdentityLink(
            contact_id=legacy.id,
            identifier_kind=LinkedInIdentifierKind.PUBLIC_VANITY_URL.value,
            identifier_value=BOTH_VANITY,
            state=IdentityLinkState.ACTIVE.value,
            decision_kind=IdentityLinkDecision.MIGRATION_BACKFILL.value,
            suspected_alias=True,
            decided_by="migration:dat-019",
        )
    )
    db_session.flush()

    _promote(db_session, _both(db_session), BOTH_DOMAIN)

    # The flagged row did not answer the match, and was not rewritten either.
    flagged = db_session.scalars(
        select(LinkedInIdentityLink).where(LinkedInIdentityLink.suspected_alias.is_(True))
    ).one()
    assert flagged.identifier_value == BOTH_VANITY
    assert flagged.contact_id == legacy.id
    db_session.refresh(legacy)
    assert legacy.linkedin_url == BOTH_VANITY


def test_a_superseded_identity_link_is_ignored_when_matching(
    db_session: Session, enable_capture: None
) -> None:
    retired = Contact(
        first_name="Retired",
        last_name="Claim",
        company_name="Sakura Robotics",
        company_domain=BOTH_DOMAIN,
        natural_key=f"retired|claim|{BOTH_DOMAIN}",
    )
    db_session.add(retired)
    db_session.flush()
    identity_links.record_observed(
        db_session,
        contact=retired,
        kind=LinkedInIdentifierKind.SALESNAV_MEMBER_ID,
        value=BOTH_MEMBER_ID,
        decided_by="test",
    )
    db_session.flush()
    identity_links.revoke(
        db_session,
        kind=LinkedInIdentifierKind.SALESNAV_MEMBER_ID,
        value=BOTH_MEMBER_ID,
        reason="operator decided this was a different person",
        decided_by="operator:test",
    )
    db_session.flush()

    result = _promote(db_session, _both(db_session), BOTH_DOMAIN)

    # The superseded claim neither matched nor blocked; a new contact was made.
    assert result.contact_outcome is ContactPromotionOutcome.CONTACT_CREATED
    active = identity_links.lookup_contact(
        db_session, LinkedInIdentifierKind.SALESNAV_MEMBER_ID, BOTH_MEMBER_ID
    )
    assert active is not None and active.id != retired.id


# --- 11 & 12. comparison rules through the real path --------------------------


def test_a_differently_cased_member_id_does_not_match(
    db_session: Session, enable_capture: None
) -> None:
    holder = Contact(
        first_name="Case",
        last_name="Holder",
        company_name="Sakura Robotics",
        company_domain=BOTH_DOMAIN,
        natural_key=f"case|holder|{BOTH_DOMAIN}",
    )
    db_session.add(holder)
    db_session.flush()
    identity_links.record_observed(
        db_session,
        contact=holder,
        kind=LinkedInIdentifierKind.SALESNAV_MEMBER_ID,
        value=BOTH_MEMBER_ID.lower(),
        decided_by="test",
    )
    db_session.flush()

    result = _promote(db_session, _both(db_session), BOTH_DOMAIN)
    # The lower-cased id is a different identifier, so it neither matched nor
    # collided: the capture made its own contact.
    assert result.contact_outcome is ContactPromotionOutcome.CONTACT_CREATED


def test_a_differently_cased_vanity_url_does_match(
    db_session: Session, enable_capture: None
) -> None:
    holder = Contact(
        first_name=BOTH_FIRST,
        last_name=BOTH_LAST,
        company_name="Sakura Robotics",
        company_domain=BOTH_DOMAIN,
        natural_key=f"{BOTH_FIRST.casefold()}|{BOTH_LAST.casefold()}|{BOTH_DOMAIN}",
    )
    db_session.add(holder)
    db_session.flush()
    identity_links.record_observed(
        db_session,
        contact=holder,
        kind=LinkedInIdentifierKind.PUBLIC_VANITY_URL,
        value="https://www.linkedin.com/in/Tomoya-Okaku/?trk=x",
        decided_by="test",
    )
    db_session.flush()

    result = _promote(db_session, _both(db_session), BOTH_DOMAIN)
    assert result.contact_outcome is ContactPromotionOutcome.CONTACT_EXACT_MATCH_LINKED
    assert db_session.scalar(select(func.count()).select_from(Contact)) == 1


# --- 13. canonical URL protection through the real path -----------------------


def test_promotion_fills_an_empty_handle_but_never_replaces_an_observed_one(
    db_session: Session, enable_capture: None
) -> None:
    held = "https://www.linkedin.com/in/already-observed"
    holder = Contact(
        first_name=BOTH_FIRST,
        last_name=BOTH_LAST,
        company_name="Sakura Robotics",
        company_domain=BOTH_DOMAIN,
        linkedin_url=held,
        natural_key=f"{BOTH_FIRST.casefold()}|{BOTH_LAST.casefold()}|{BOTH_DOMAIN}",
    )
    db_session.add(holder)
    db_session.flush()
    identity_links.record_observed(
        db_session,
        contact=holder,
        kind=LinkedInIdentifierKind.PUBLIC_VANITY_URL,
        value=held,
        decided_by="test",
    )
    db_session.flush()

    # Natural key links this capture to the holder; the capture carries a
    # different observed handle. The stored one is already observed, so it stands.
    _promote(db_session, _both(db_session), BOTH_DOMAIN)
    db_session.refresh(holder)
    assert holder.linkedin_url == held


def test_a_contact_with_no_handle_gains_the_observed_one(
    db_session: Session, enable_capture: None
) -> None:
    holder = Contact(
        first_name=BOTH_FIRST,
        last_name=BOTH_LAST,
        company_name="Sakura Robotics",
        company_domain=BOTH_DOMAIN,
        linkedin_url=None,
        natural_key=f"{BOTH_FIRST.casefold()}|{BOTH_LAST.casefold()}|{BOTH_DOMAIN}",
    )
    db_session.add(holder)
    db_session.flush()

    _promote(db_session, _both(db_session), BOTH_DOMAIN)
    db_session.refresh(holder)
    assert holder.linkedin_url == BOTH_VANITY


def test_a_member_only_capture_never_writes_a_canonical_url(
    db_session: Session, enable_capture: None
) -> None:
    holder = Contact(
        first_name="Dana",
        last_name="Whitfield",
        company_name="Northwind Logistics",
        company_domain=MEMBER_ONLY_DOMAIN,
        linkedin_url=None,
        natural_key=f"dana|whitfield|{MEMBER_ONLY_DOMAIN}",
    )
    db_session.add(holder)
    db_session.flush()

    _promote(db_session, _member_only(db_session), MEMBER_ONLY_DOMAIN)
    db_session.refresh(holder)
    assert holder.linkedin_url is None, "an identifier must not become a published URL"


# --- 14. a blocked promotion leaves no half-made bridge -----------------------


def test_a_blocked_promotion_leaves_no_active_bridge_behind(
    db_session: Session, enable_capture: None
) -> None:
    """Ambiguity is decided before anything is written, so there is nothing to undo."""

    other = Contact(
        first_name="Someone",
        last_name="Else",
        company_name="Sakura Robotics",
        company_domain=BOTH_DOMAIN,
        natural_key=f"someone|else|{BOTH_DOMAIN}",
    )
    rival = Contact(
        first_name="Another",
        last_name="Person",
        company_name="Sakura Robotics",
        company_domain=BOTH_DOMAIN,
        natural_key=f"another|person|{BOTH_DOMAIN}",
    )
    db_session.add_all([other, rival])
    db_session.flush()
    identity_links.record_observed(
        db_session,
        contact=other,
        kind=LinkedInIdentifierKind.SALESNAV_MEMBER_ID,
        value=BOTH_MEMBER_ID,
        decided_by="test",
    )
    identity_links.record_observed(
        db_session,
        contact=rival,
        kind=LinkedInIdentifierKind.PUBLIC_VANITY_URL,
        value=BOTH_VANITY,
        decided_by="test",
    )
    db_session.flush()
    before = db_session.scalar(select(func.count()).select_from(LinkedInIdentityLink))

    result = _promote(db_session, _both(db_session), BOTH_DOMAIN)
    assert result.contact_outcome is ContactPromotionOutcome.CONTACT_IDENTITY_AMBIGUOUS

    after = db_session.scalar(select(func.count()).select_from(LinkedInIdentityLink))
    assert after == before, "a refused promotion writes no identity links at all"
    # And no SAME_CAPTURE bridge was created for either identifier.
    bridges = db_session.scalars(
        select(LinkedInIdentityLink).where(
            LinkedInIdentityLink.decision_kind == IdentityLinkDecision.SAME_CAPTURE_OBSERVED.value
        )
    ).all()
    assert bridges == []

"""LinkedIn identity links (DAT-019 / #195).

Two acquisition surfaces describe the same person with different identifier
forms: a Sales Navigator results row carries an opaque member id, a profile page
carries the published `/in/` handle. Identity here is matched by exact
normalized string, so those are two keys for one human and the person fragments
into two contacts.

The rules these tests hold the backend to:

* the member id is stored verbatim, with its original casing, and is never put
  through the vanity-URL normalizer (which lowercases slugs);
* a member id is an identifier, never the canonical published profile URL;
* the two forms are bridged automatically ONLY when both were directly observed
  in the same authenticated capture for the same displayed person — never on
  name, company, title, a compatible-looking separate capture, or a generated
  alias;
* a directly observed vanity URL is never displaced by a member-id alias;
* conflicting evidence preserves both identifiers and routes to DAT-004 review;
* repeated captures are idempotent.

An unresolved duplicate is safer than a false merge, and every assertion here
leans that way.
"""

from __future__ import annotations

import pytest
from app.models.contact import Contact
from app.models.enums import IdentityLinkDecision, IdentityLinkState, LinkedInIdentifierKind
from app.models.linkedin_identity_link import LinkedInIdentityLink
from app.services import identity_links
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

MEMBER_ID = "ACwAAAACaUgB2RAj8vfkHcwfcSBgD7GEr0BIIkU"
VANITY = "https://www.linkedin.com/in/danawhitfield"


def _contact(session: Session, *, first: str = "Dana", last: str = "Whitfield") -> Contact:
    contact = Contact(
        first_name=first,
        last_name=last,
        company_name="Northwind Logistics",
        company_domain="northwind-logistics.com",
        natural_key=f"{first.casefold()}|{last.casefold()}|northwind-logistics.com",
    )
    session.add(contact)
    session.flush()
    return contact


# --- casing -------------------------------------------------------------------


def test_member_id_is_stored_verbatim_with_its_original_casing(db_session: Session) -> None:
    contact = _contact(db_session)
    identity_links.record_observed(
        db_session,
        contact=contact,
        kind=LinkedInIdentifierKind.SALESNAV_MEMBER_ID,
        value=MEMBER_ID,
        decided_by="test",
    )
    db_session.flush()

    stored = db_session.scalars(
        select(LinkedInIdentityLink.identifier_value).where(
            LinkedInIdentityLink.identifier_kind == LinkedInIdentifierKind.SALESNAV_MEMBER_ID
        )
    ).one()
    assert stored == MEMBER_ID
    assert stored != MEMBER_ID.lower(), "the identifier is case-sensitive and must not be folded"


def test_normalizing_an_identifier_never_lowercases_a_member_id(db_session: Session) -> None:
    assert (
        identity_links.normalize_identifier(LinkedInIdentifierKind.SALESNAV_MEMBER_ID, MEMBER_ID)
        == MEMBER_ID
    )
    # A vanity URL is normalized as before — that path is unchanged.
    assert (
        identity_links.normalize_identifier(
            LinkedInIdentifierKind.PUBLIC_VANITY_URL,
            "https://www.linkedin.com/in/DanaWhitfield/?trk=x",
        )
        == VANITY
    )


# --- lookup -------------------------------------------------------------------


def test_member_id_lookup_is_exact_and_case_sensitive(db_session: Session) -> None:
    contact = _contact(db_session)
    identity_links.record_observed(
        db_session,
        contact=contact,
        kind=LinkedInIdentifierKind.SALESNAV_MEMBER_ID,
        value=MEMBER_ID,
        decided_by="test",
    )
    db_session.flush()

    found = identity_links.lookup_contact(
        db_session, LinkedInIdentifierKind.SALESNAV_MEMBER_ID, MEMBER_ID
    )
    assert found is not None and found.id == contact.id

    missed = identity_links.lookup_contact(
        db_session, LinkedInIdentifierKind.SALESNAV_MEMBER_ID, MEMBER_ID.lower()
    )
    assert missed is None, "a differently-cased id is a different identifier, not a match"


def test_lookup_is_backed_by_an_index_rather_than_a_scan(db_session: Session) -> None:
    indexes = inspect(db_session.get_bind()).get_indexes("linkedin_identity_links")
    covering = [
        ix for ix in indexes if ix["column_names"][:2] == ["identifier_kind", "identifier_value"]
    ]
    assert covering, "identifier lookup must be indexed, not an O(n) Python scan"


# --- the automatic bridge -----------------------------------------------------


def test_same_capture_co_occurrence_bridges_the_two_identifier_forms(
    db_session: Session,
) -> None:
    """The ONLY automatic bridge: both identifiers seen on one captured person."""

    contact = _contact(db_session)
    result = identity_links.bridge_observed_pair(
        db_session,
        contact=contact,
        member_id=MEMBER_ID,
        vanity_url=VANITY,
        capture_id=None,
        source_surface="salesnav_people_results",
        decided_by="test",
    )
    db_session.flush()
    assert result.bridged is True

    for kind, value in (
        (LinkedInIdentifierKind.SALESNAV_MEMBER_ID, MEMBER_ID),
        (LinkedInIdentifierKind.PUBLIC_VANITY_URL, VANITY),
    ):
        found = identity_links.lookup_contact(db_session, kind, value)
        assert found is not None and found.id == contact.id

    link = db_session.scalars(
        select(LinkedInIdentityLink).where(
            LinkedInIdentityLink.identifier_kind == LinkedInIdentifierKind.SALESNAV_MEMBER_ID
        )
    ).one()
    assert link.decision_kind == IdentityLinkDecision.SAME_CAPTURE_OBSERVED
    assert link.corroboration is not None
    assert link.corroboration.get("observed_vanity_url") == VANITY


def test_a_member_id_alone_never_becomes_the_canonical_profile_url(
    db_session: Session,
) -> None:
    contact = _contact(db_session)
    identity_links.record_observed(
        db_session,
        contact=contact,
        kind=LinkedInIdentifierKind.SALESNAV_MEMBER_ID,
        value=MEMBER_ID,
        decided_by="test",
    )
    db_session.flush()
    db_session.refresh(contact)

    assert contact.linkedin_url is None, "an identifier is not a published URL"
    vanity_links = db_session.scalars(
        select(LinkedInIdentityLink).where(
            LinkedInIdentityLink.identifier_kind == LinkedInIdentifierKind.PUBLIC_VANITY_URL
        )
    ).all()
    assert vanity_links == [], "no vanity identity may be invented from a member id"


def test_an_observed_vanity_url_is_never_displaced_by_an_alias(db_session: Session) -> None:
    contact = _contact(db_session)
    contact.linkedin_url = VANITY
    identity_links.record_observed(
        db_session,
        contact=contact,
        kind=LinkedInIdentifierKind.PUBLIC_VANITY_URL,
        value=VANITY,
        decided_by="test",
    )
    db_session.flush()

    alias = f"https://www.linkedin.com/in/{MEMBER_ID.lower()}"
    applied = identity_links.propose_canonical_url(
        db_session, contact=contact, url=alias, observed=False
    )
    db_session.flush()
    db_session.refresh(contact)

    assert applied is False
    assert contact.linkedin_url == VANITY


# --- conflict routes to review, never to a merge ------------------------------


def test_a_conflicting_mapping_does_not_auto_merge(db_session: Session) -> None:
    first = _contact(db_session, first="Dana", last="Whitfield")
    second = _contact(db_session, first="Dana", last="Whitfield-Other")
    identity_links.record_observed(
        db_session,
        contact=first,
        kind=LinkedInIdentifierKind.SALESNAV_MEMBER_ID,
        value=MEMBER_ID,
        decided_by="test",
    )
    db_session.flush()

    outcome = identity_links.record_observed(
        db_session,
        contact=second,
        kind=LinkedInIdentifierKind.SALESNAV_MEMBER_ID,
        value=MEMBER_ID,
        decided_by="test",
    )
    db_session.flush()

    assert outcome.state == IdentityLinkState.NEEDS_REVIEW
    assert outcome.conflicting_contact_id == first.id
    # Both contacts survive; nothing was merged behind the operator's back.
    db_session.refresh(first)
    db_session.refresh(second)
    assert first.merged_into_id is None
    assert second.merged_into_id is None
    # The winning identifier still resolves to the first contact only.
    found = identity_links.lookup_contact(
        db_session, LinkedInIdentifierKind.SALESNAV_MEMBER_ID, MEMBER_ID
    )
    assert found is not None and found.id == first.id


def test_the_database_refuses_two_active_claims_on_one_identifier(
    db_session: Session,
) -> None:
    first = _contact(db_session, first="Dana", last="Whitfield")
    second = _contact(db_session, first="Other", last="Person")
    for contact in (first, second):
        db_session.add(
            LinkedInIdentityLink(
                contact_id=contact.id,
                identifier_kind=LinkedInIdentifierKind.SALESNAV_MEMBER_ID,
                identifier_value=MEMBER_ID,
                state=IdentityLinkState.ACTIVE,
                decision_kind=IdentityLinkDecision.OBSERVED_CAPTURE,
                decided_by="test",
            )
        )
    with pytest.raises(IntegrityError):
        db_session.flush()


# --- idempotency --------------------------------------------------------------


def test_recording_the_same_observation_twice_changes_nothing(db_session: Session) -> None:
    contact = _contact(db_session)
    for _ in range(3):
        identity_links.record_observed(
            db_session,
            contact=contact,
            kind=LinkedInIdentifierKind.SALESNAV_MEMBER_ID,
            value=MEMBER_ID,
            decided_by="test",
        )
        db_session.flush()

    rows = db_session.scalars(
        select(LinkedInIdentityLink).where(
            LinkedInIdentityLink.identifier_kind == LinkedInIdentifierKind.SALESNAV_MEMBER_ID
        )
    ).all()
    assert len(rows) == 1


# --- reversibility and audit --------------------------------------------------


def test_an_association_can_be_reversed_without_deleting_its_history(
    db_session: Session,
) -> None:
    contact = _contact(db_session)
    identity_links.record_observed(
        db_session,
        contact=contact,
        kind=LinkedInIdentifierKind.SALESNAV_MEMBER_ID,
        value=MEMBER_ID,
        decided_by="test",
    )
    db_session.flush()

    identity_links.revoke(
        db_session,
        kind=LinkedInIdentifierKind.SALESNAV_MEMBER_ID,
        value=MEMBER_ID,
        reason="operator says this is a different person",
        decided_by="operator:sahil",
    )
    db_session.flush()

    assert (
        identity_links.lookup_contact(
            db_session, LinkedInIdentifierKind.SALESNAV_MEMBER_ID, MEMBER_ID
        )
        is None
    )
    history = db_session.scalars(select(LinkedInIdentityLink)).all()
    assert len(history) == 1, "the record is superseded, never deleted"
    assert history[0].state == IdentityLinkState.SUPERSEDED
    assert history[0].superseded_at is not None
    assert history[0].reason == "operator says this is a different person"

    # And the identifier can then be claimed by the right contact.
    other = _contact(db_session, first="Dana", last="Whitfield-Real")
    again = identity_links.record_observed(
        db_session,
        contact=other,
        kind=LinkedInIdentifierKind.SALESNAV_MEMBER_ID,
        value=MEMBER_ID,
        decided_by="test",
    )
    db_session.flush()
    assert again.state == IdentityLinkState.ACTIVE


# --- legacy rows: flagged, never rewritten ------------------------------------


def test_a_legacy_lowercased_alias_is_detected_deterministically() -> None:
    lead = (
        "https://www.linkedin.com/sales/lead/ACwAAAACaUgB2RAj8vfkHcwfcSBgD7GEr0BIIkU,NAME_SEARCH,ct"
    )
    stored = "https://www.linkedin.com/in/acwaaaacaugb2raj8vfkhcwfcsbgd7ger0biiku"
    assert identity_links.looks_like_member_id_alias(stored, lead) is True

    # A real handle stored beside the same lead URL is not a false positive.
    assert identity_links.looks_like_member_id_alias(VANITY, lead) is False
    # And with no lead URL there is nothing to compare against, so no claim.
    assert identity_links.looks_like_member_id_alias(stored, None) is False


def test_a_flagged_legacy_value_is_preserved_and_excluded_from_matching(
    db_session: Session,
) -> None:
    contact = _contact(db_session)
    alias = f"https://www.linkedin.com/in/{MEMBER_ID.lower()}"
    contact.linkedin_url = alias
    link = LinkedInIdentityLink(
        contact_id=contact.id,
        identifier_kind=LinkedInIdentifierKind.PUBLIC_VANITY_URL,
        identifier_value=alias,
        state=IdentityLinkState.ACTIVE,
        decision_kind=IdentityLinkDecision.MIGRATION_BACKFILL,
        suspected_alias=True,
        decided_by="migration:dat-019",
    )
    db_session.add(link)
    db_session.flush()
    db_session.refresh(contact)

    # The stored value is untouched — flagged, not repaired.
    assert contact.linkedin_url == alias
    # But it must not act as a canonical identity for matching.
    assert (
        identity_links.lookup_contact(db_session, LinkedInIdentifierKind.PUBLIC_VANITY_URL, alias)
        is None
    )


def test_a_flagged_identifier_does_not_block_the_real_handle(db_session: Session) -> None:
    """A suspected alias is excluded from uniqueness, so truth can still land."""

    contact = _contact(db_session)
    alias = f"https://www.linkedin.com/in/{MEMBER_ID.lower()}"
    db_session.add(
        LinkedInIdentityLink(
            contact_id=contact.id,
            identifier_kind=LinkedInIdentifierKind.PUBLIC_VANITY_URL,
            identifier_value=alias,
            state=IdentityLinkState.ACTIVE,
            decision_kind=IdentityLinkDecision.MIGRATION_BACKFILL,
            suspected_alias=True,
            decided_by="migration:dat-019",
        )
    )
    db_session.flush()

    outcome = identity_links.record_observed(
        db_session,
        contact=contact,
        kind=LinkedInIdentifierKind.PUBLIC_VANITY_URL,
        value=VANITY,
        decided_by="test",
    )
    db_session.flush()
    assert outcome.state == IdentityLinkState.ACTIVE


# --- the table exists with the shape the migration promises -------------------


def test_the_identity_link_table_has_its_partial_unique_index(db_session: Session) -> None:
    rows = db_session.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'linkedin_identity_links' AND indexdef ILIKE '%UNIQUE%'"
        )
    ).all()
    assert rows, "one identifier may not be actively claimed by two contacts"
    combined = " ".join(r[0] for r in rows).lower()
    assert "where" in combined, "uniqueness is partial: superseded history must coexist"


def test_snapshots_carry_the_member_id_and_the_url_source(db_session: Session) -> None:
    cols = {
        c["name"] for c in inspect(db_session.get_bind()).get_columns("linkedin_profile_snapshots")
    }
    assert "salesnav_member_id" in cols
    assert "profile_url_source" in cols

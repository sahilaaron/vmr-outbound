"""DAT-020A — the derived resolving alias is evidence, never identity.

A Sales Navigator row exposes an opaque member id. LinkedIn's ``/in/`` route
accepts that id and redirects, so ``https://www.linkedin.com/in/<member-id>`` is
a genuinely useful way to open a profile whose published handle is not known
yet. DAT-019 was right that this alias is not the person's handle; DAT-020
restored it as a navigation aid without restoring the confusion.

These tests hold the line between those two statements. They run the real intake
and the real promotion against a live Postgres, and assert on the three values a
capture can know about one person's LinkedIn identity:

* the **observed vanity URL** — authoritative, and the only one that is identity;
* the **derived resolving alias** — navigation and evidence, nothing more;
* the **opaque member id** — a matchable identifier, but only against other
  member ids.

The failures worth catching here are quiet ones: an alias reaching
``contacts.linkedin_url``, a ``PUBLIC_VANITY_URL`` claim minted from a derived
value, a member id folded to lower case by a normalizer meant for handles, or a
retry quietly producing a second contact.
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
    EnrichmentConfirmationSource,
    IdentityLinkState,
    LinkedInIdentifierKind,
)
from app.models.linkedin_identity_link import LinkedInIdentityLink
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.services.captures import intake as capture_intake
from app.services.captures import promotion as promo
from sqlalchemy import func, select
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "extensions" / "salesnav-capture" / "docs" / "fixtures"
SALESNAV_SUBMISSION = json.loads(
    (FIXTURES / "contact-capture.salesnav.example.json").read_text("utf-8")
)
PROFILE_SUBMISSION = json.loads(
    (FIXTURES / "contact-capture.profile.example.json").read_text("utf-8")
)

LOOPBACK = "http://127.0.0.1:8000"
DOMAIN = "meridianworks.example"

# The member id exactly as the committed fixture carries it. Mixed case on
# purpose: it is the property most easily destroyed by a careless normalizer.
FIXTURE_MEMBER_ID = "ACwAAAB1x9k"
FIXTURE_ALIAS = "https://www.linkedin.com/in/ACwAAAB1x9k"


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


@pytest.fixture()
def row_capture(db_session: Session) -> LinkedInProfileSnapshot:
    """A Sales Navigator row: member id and derived alias, no observed handle."""

    captures = _stage(db_session, SALESNAV_SUBMISSION)
    return next(c for c in captures if c.salesnav_member_id == FIXTURE_MEMBER_ID)


def _confirm(db: Session, snapshot: LinkedInProfileSnapshot) -> None:
    promo.confirm_domain(
        db,
        snapshot=snapshot,
        source=EnrichmentConfirmationSource.MANUAL,
        domain=DOMAIN,
        actor="test",
    )


def _active_links(db: Session, contact_id: uuid.UUID) -> list[LinkedInIdentityLink]:
    return list(
        db.scalars(
            select(LinkedInIdentityLink).where(
                LinkedInIdentityLink.contact_id == contact_id,
                LinkedInIdentityLink.state == IdentityLinkState.ACTIVE,
            )
        )
    )


# --- The three values stay three values ---------------------------------------


def test_capture_keeps_observed_url_alias_and_member_id_apart(
    db_session: Session, row_capture: LinkedInProfileSnapshot
) -> None:
    """One row, three distinct stored facts — and no invented handle."""

    # The row showed no /in/ link, so identity stays honestly unknown...
    assert row_capture.normalized_profile_url is None
    assert row_capture.profile_url_source is None
    # ...while the alias and the id are both recorded, in their own columns.
    assert row_capture.salesnav_alias_url == FIXTURE_ALIAS
    assert row_capture.salesnav_member_id == FIXTURE_MEMBER_ID
    assert row_capture.salesnav_lead_url is not None


def test_member_id_is_stored_verbatim_and_the_alias_preserves_its_casing(
    row_capture: LinkedInProfileSnapshot,
) -> None:
    """Folding the case would break the very redirect the alias exists for."""

    assert row_capture.salesnav_member_id == FIXTURE_MEMBER_ID
    assert row_capture.salesnav_member_id != FIXTURE_MEMBER_ID.lower()
    assert row_capture.salesnav_alias_url is not None
    assert row_capture.salesnav_alias_url.endswith(FIXTURE_MEMBER_ID)
    assert FIXTURE_MEMBER_ID.lower() not in row_capture.salesnav_alias_url.rsplit("/", 1)[-1] or (
        FIXTURE_MEMBER_ID.islower()
    )


def test_projection_exposes_the_alias_under_its_own_key(
    row_capture: LinkedInProfileSnapshot,
) -> None:
    """A reader of profile_fields cannot mistake the alias for the handle."""

    fields = row_capture.profile_fields or {}
    assert fields["salesnav_alias_url"] == FIXTURE_ALIAS
    assert fields["salesnav_member_id"] == FIXTURE_MEMBER_ID
    assert fields["linkedin_profile_url"] is None


def test_the_alias_never_becomes_the_normalized_profile_url(
    row_capture: LinkedInProfileSnapshot,
) -> None:
    """The canonical identity column stays null rather than take a derived value."""

    assert row_capture.normalized_profile_url is None
    assert row_capture.salesnav_alias_url is not None
    assert row_capture.normalized_profile_url != row_capture.salesnav_alias_url


# --- Promotion before any vanity URL is known ---------------------------------


def test_promotion_before_a_handle_is_known_identifies_by_member_id_claim(
    db_session: Session, row_capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    """The contact is usable with no canonical LinkedIn URL at all."""

    _confirm(db_session, row_capture)
    result = promo.promote(db_session, snapshot=row_capture, actor="test")
    db_session.flush()

    contact = result.contact
    assert contact is not None

    # The alias is emphatically NOT the contact's LinkedIn URL.
    assert contact.linkedin_url is None

    links = _active_links(db_session, contact.id)
    kinds = {link.identifier_kind for link in links}
    assert LinkedInIdentifierKind.SALESNAV_MEMBER_ID in kinds
    assert LinkedInIdentifierKind.PUBLIC_VANITY_URL not in kinds

    member_link = next(
        link for link in links if link.identifier_kind == LinkedInIdentifierKind.SALESNAV_MEMBER_ID
    )
    assert member_link.identifier_value == FIXTURE_MEMBER_ID


def test_no_public_vanity_claim_is_ever_minted_from_the_derived_alias(
    db_session: Session, row_capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    """The alias resolves; that still does not make it an observed handle."""

    _confirm(db_session, row_capture)
    promo.promote(db_session, snapshot=row_capture, actor="test")
    db_session.flush()

    vanity_values = set(
        db_session.scalars(
            select(LinkedInIdentityLink.identifier_value).where(
                LinkedInIdentityLink.identifier_kind == LinkedInIdentifierKind.PUBLIC_VANITY_URL
            )
        )
    )
    assert FIXTURE_ALIAS not in vanity_values
    # Nor a normalized form of it.
    assert not any(FIXTURE_MEMBER_ID.lower() in value for value in vanity_values)


def test_the_alias_is_never_written_to_any_contact_linkedin_url(
    db_session: Session, row_capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    _confirm(db_session, row_capture)
    promo.promote(db_session, snapshot=row_capture, actor="test")
    db_session.flush()

    stored = set(db_session.scalars(select(Contact.linkedin_url)))
    assert FIXTURE_ALIAS not in stored
    assert all(value is None or FIXTURE_MEMBER_ID not in value for value in stored)


# --- Idempotency --------------------------------------------------------------


def test_repeated_promotion_creates_no_duplicate_contact_or_active_claim(
    db_session: Session, row_capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    """A retry returns the same truthful outcome, not a second person."""

    _confirm(db_session, row_capture)
    first = promo.promote(db_session, snapshot=row_capture, actor="test")
    db_session.flush()
    contacts_after_first = db_session.scalar(select(func.count()).select_from(Contact))

    second = promo.promote(db_session, snapshot=row_capture, actor="test")
    db_session.flush()

    assert second.contact is not None and first.contact is not None
    assert second.contact.id == first.contact.id
    assert db_session.scalar(select(func.count()).select_from(Contact)) == contacts_after_first

    assert first.contact is not None
    member_claims = [
        link
        for link in _active_links(db_session, first.contact.id)
        if link.identifier_kind == LinkedInIdentifierKind.SALESNAV_MEMBER_ID
    ]
    assert len(member_claims) == 1


def test_a_second_capture_of_the_same_member_resolves_to_the_same_contact(
    db_session: Session, row_capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    """The member id is what makes two Sales Navigator captures one person."""

    _confirm(db_session, row_capture)
    first = promo.promote(db_session, snapshot=row_capture, actor="test")
    db_session.flush()

    again = next(
        c
        for c in _stage(db_session, SALESNAV_SUBMISSION)
        if c.salesnav_member_id == FIXTURE_MEMBER_ID and c.id != row_capture.id
    )
    _confirm(db_session, again)
    second = promo.promote(db_session, snapshot=again, actor="test")
    db_session.flush()

    assert second.contact is not None and first.contact is not None
    assert second.contact.id == first.contact.id


# --- An observed handle always outranks a derived alias -----------------------


def test_an_observed_handle_is_never_replaced_by_a_derived_alias(
    db_session: Session, enable_capture: None
) -> None:
    """A real profile capture keeps its own URL; the alias stays out of it."""

    profile = _stage(db_session, PROFILE_SUBMISSION)[0]
    assert profile.normalized_profile_url is not None
    observed = profile.normalized_profile_url

    # A person-profile capture never comes from Sales Navigator, so it carries
    # neither an id nor an alias — the contract says so and intake honours it.
    assert profile.salesnav_member_id is None
    assert profile.salesnav_alias_url is None

    _confirm(db_session, profile)
    result = promo.promote(db_session, snapshot=profile, actor="test")
    db_session.flush()

    contact = result.contact
    assert contact is not None
    assert contact.linkedin_url is not None
    assert contact.linkedin_url == observed
    assert FIXTURE_MEMBER_ID not in contact.linkedin_url


# --- The capture detail surface -----------------------------------------------


def test_capture_detail_page_distinguishes_all_three_values(
    db_session: Session,
    row_capture: LinkedInProfileSnapshot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator reading the page can tell which value is which."""

    from app.api.deps import get_db
    from app.main import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_INTAKE", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_PROMOTION", "true")
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as client:
            response = client.get(f"/contact-captures/{row_capture.id}")
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()

    assert response.status_code == 200
    body = response.text
    assert "salesnav_alias_url" in body
    assert "salesnav_member_id" in body
    assert FIXTURE_ALIAS in body
    assert FIXTURE_MEMBER_ID in body
    # The page must say what the alias is, not merely show it.
    assert "Derived resolving alias" in body
    assert 'data-linkedin="derived"' in body

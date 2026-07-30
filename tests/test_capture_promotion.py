"""Contact-capture promotion tests (DAT-014).

Exercises the bridge from a staged DAT-013 capture to a canonical Contact
through the existing DAT-010 logo.dev candidate flow, against a live Postgres.

The provider is always stubbed: the normal suite never depends on logo.dev, and
no API key is needed to run any of this.

The guarantees under test are the product ones: a domain is never fabricated, a
top-ranked provider result is never truth on its own, every candidate and every
operator decision is preserved, ambiguity blocks promotion, suppression stays
authoritative, labels and notes carry over, the original capture stays
immutable, retries are idempotent, and promotion creates identity — never
campaign membership, email candidates, qualification, or outreach readiness.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.audit_event import AuditEvent
from app.models.campaign import CampaignContact
from app.models.capture_promotion import ContactCapturePromotion
from app.models.company import Company
from app.models.contact import Contact
from app.models.contact_capture import ContactCaptureNote, ContactLabel, ContactLabelAssignment
from app.models.contact_field_value import ContactFieldValue
from app.models.draft import DraftVersion
from app.models.email_candidate import EmailCandidate
from app.models.enums import (
    CompanyResolutionOutcome,
    ContactPromotionOutcome,
    EnrichmentConfirmationSource,
    EnrichmentConfirmationStatus,
    EnrichmentLookupStatus,
    SuppressionReason,
    SuppressionType,
)
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.models.salesnav_enrichment import SalesNavCompanyEnrichment
from app.services.captures import intake as capture_intake
from app.services.captures import promotion as promo
from app.services.enrichment import logodev
from app.services.suppressions import add_suppression
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "extensions" / "salesnav-capture" / "docs" / "fixtures"
PROFILE_SUBMISSION = json.loads(
    (FIXTURES / "contact-capture.profile.example.json").read_text("utf-8")
)
SALESNAV_SUBMISSION = json.loads(
    (FIXTURES / "contact-capture.salesnav.example.json").read_text("utf-8")
)

LOOPBACK = "http://127.0.0.1:8000"
DOMAIN = "meridianworks.example"


# --- Provider stubs -----------------------------------------------------------


def transport_returning(*brands: dict[str, Any]) -> logodev.Transport:
    """A stub transport answering 200 with the documented brand-array body."""

    def _transport(url: str, headers: Any, timeout: float) -> logodev.RawResponse:
        assert "Authorization" in headers, "the client must authenticate"
        return logodev.RawResponse(status_code=200, body=json.dumps(list(brands)))

    return _transport


def transport_status(code: int, body: str = "[]") -> logodev.Transport:
    def _transport(url: str, headers: Any, timeout: float) -> logodev.RawResponse:
        return logodev.RawResponse(status_code=code, body=body)

    return _transport


def transport_failing() -> logodev.Transport:
    def _transport(url: str, headers: Any, timeout: float) -> logodev.RawResponse:
        raise logodev.TransportError("logo.dev request failed: URLError")

    return _transport


ONE_BRAND = ({"domain": DOMAIN, "name": "Meridian Works"},)
TWO_BRANDS = (
    {"domain": DOMAIN, "name": "Meridian Works"},
    {"domain": "meridian-works.example", "name": "Meridian Works Group"},
)


def run_lookup(
    db: Session, snapshot: LinkedInProfileSnapshot, transport: logodev.Transport, **kwargs: Any
) -> tuple[ContactCapturePromotion, SalesNavCompanyEnrichment | None]:
    return promo.run_lookup(
        db,
        snapshot=snapshot,
        api_key="test-key-never-real",
        search_url="https://api.logo.dev/search",
        timeout=5.0,
        max_candidates=10,
        actor="test",
        transport=transport,
        **kwargs,
    )


# --- Fixtures -----------------------------------------------------------------


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
    """Stage a real DAT-013 submission and return its captures."""

    payload = copy.deepcopy(submission)
    payload["client_submission_id"] = str(uuid.uuid4())
    for capture in payload["contacts"]:
        capture["client_capture_id"] = str(uuid.uuid4())
    result = capture_intake.stage_contact_captures(db, payload=payload, operator_base_url=LOOPBACK)
    ids = [uuid.UUID(str(r.capture_id)) for r in result.results]
    return [db.get(LinkedInProfileSnapshot, cid) for cid in ids]  # type: ignore[misc]


@pytest.fixture()
def capture(db_session: Session) -> LinkedInProfileSnapshot:
    """One unmatched profile capture: Morgan Vale at Meridian Works."""

    return _stage(db_session, PROFILE_SUBMISSION)[0]


@pytest.fixture()
def row_capture(db_session: Session) -> LinkedInProfileSnapshot:
    """A Sales Navigator row capture: a name and company, no profile URL."""

    captures = _stage(db_session, SALESNAV_SUBMISSION)
    return next(c for c in captures if c.normalized_profile_url is None)


def _seed_contact(
    db: Session,
    *,
    first: str = "Morgan",
    last: str = "Vale",
    domain: str = DOMAIN,
    linkedin_url: str | None = None,
    email: str | None = None,
    title: str | None = "Operations Manager",
) -> Contact:
    contact = Contact(
        first_name=first,
        last_name=last,
        company_name="Meridian Works",
        company_domain=domain,
        title=title,
        linkedin_url=linkedin_url,
        email=email,
        natural_key=f"{first.casefold()}|{last.casefold()}|{domain}",
    )
    db.add(contact)
    db.flush()
    return contact


def _confirm(db: Session, snapshot: LinkedInProfileSnapshot, domain: str = DOMAIN) -> None:
    promo.confirm_domain(
        db,
        snapshot=snapshot,
        source=EnrichmentConfirmationSource.CANDIDATE,
        domain=domain,
        actor="test",
    )


# --- Eligibility and hints ----------------------------------------------------


def test_an_unmatched_capture_is_eligible_and_carries_its_company_hints(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    assert capture.matched_contact_id is None
    assert capture in promo.pending_captures(db_session)

    hints = promo.company_hints(capture)
    assert hints.name == "Meridian Works"
    assert hints.linkedin_url == "https://www.linkedin.com/company/meridian-works"
    assert hints.linkedin_id == "meridian-works"
    assert hints.location  # the role's displayed location

    identity = promo.person_identity(capture)
    assert (identity.first_name, identity.last_name) == ("Morgan", "Vale")
    assert identity.title == "Director of Operations"


def test_a_results_row_capture_uses_its_employment_hint(
    db_session: Session, row_capture: LinkedInProfileSnapshot
) -> None:
    """A results row shows a company but no experience history."""

    hints = promo.company_hints(row_capture)
    assert hints.name == "Northwind Logistics"
    identity = promo.person_identity(row_capture)
    assert (identity.first_name, identity.last_name) == ("Dana", "Whitfield")
    assert identity.title == "Head of Operations"


def test_a_capture_with_no_company_has_nothing_to_look_up(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    payload = copy.deepcopy(capture.payload)
    payload["experience_observations"] = []
    payload["current_employment_hint"] = dict.fromkeys(payload["current_employment_hint"])
    capture.payload = payload
    db_session.flush()

    promotion, record = promo.ensure_records(db_session, capture)
    assert record is None
    outcome = promo.evaluate_company(
        db_session, promotion=promotion, record=None, hints=promo.company_hints(capture)
    )
    assert outcome is CompanyResolutionOutcome.NO_CANDIDATE
    assert "no company name" in (promotion.blocked_reason or "")


# --- Lookup and candidate provenance ------------------------------------------


def test_the_captured_company_name_is_what_gets_looked_up(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    seen: dict[str, str] = {}

    def _transport(url: str, headers: Any, timeout: float) -> logodev.RawResponse:
        seen["url"] = url
        return logodev.RawResponse(status_code=200, body=json.dumps(list(ONE_BRAND)))

    run_lookup(db_session, capture, _transport)
    assert "Meridian+Works" in seen["url"] or "Meridian%20Works" in seen["url"]


def test_candidates_are_preserved_with_full_provenance(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    promotion, record = run_lookup(db_session, capture, transport_returning(*TWO_BRANDS))
    assert record is not None

    assert record.capture_id == capture.id
    assert record.batch_id is None
    assert record.lookup_query == "Meridian Works"
    assert record.normalized_query == "meridian works"
    assert record.provider == "logo.dev"
    assert record.lookup_version
    assert record.looked_up_at is not None
    assert record.lookup_attempts == 1
    assert record.company_linkedin_url == "https://www.linkedin.com/company/meridian-works"
    assert record.company_linkedin_id == "meridian-works"
    assert record.location_hint

    assert [c["rank"] for c in record.candidates] == [1, 2]
    assert [c["domain"] for c in record.candidates] == [DOMAIN, "meridian-works.example"]
    # logo.dev returns no score; the absence is recorded, never invented.
    assert all(c["confidence"] is None for c in record.candidates)
    assert promotion.company_outcome is CompanyResolutionOutcome.MULTIPLE_CANDIDATES_REVIEW_REQUIRED


def test_one_candidate_is_presented_for_review_not_auto_accepted(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    promotion, record = run_lookup(db_session, capture, transport_returning(*ONE_BRAND))
    assert record is not None
    assert promotion.company_outcome is CompanyResolutionOutcome.CANDIDATE_REVIEW_REQUIRED
    # The top (and only) result is NOT confirmed merely because it ranks first.
    assert record.confirmation_status is EnrichmentConfirmationStatus.UNCONFIRMED
    assert record.confirmed_domain is None
    assert promotion.resolved_domain is None


def test_multiple_candidates_block_promotion_until_one_is_chosen(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    run_lookup(db_session, capture, transport_returning(*TWO_BRANDS))
    result = promo.promote(db_session, snapshot=capture, actor="test")
    assert result.contact_outcome is ContactPromotionOutcome.PROMOTION_BLOCKED
    assert result.company_outcome is CompanyResolutionOutcome.MULTIPLE_CANDIDATES_REVIEW_REQUIRED
    assert db_session.scalar(select(func.count()).select_from(Contact)) == 0


def test_no_candidate_leaves_the_capture_pending(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    promotion, _record = run_lookup(db_session, capture, transport_returning())
    assert promotion.company_outcome is CompanyResolutionOutcome.NO_CANDIDATE
    result = promo.promote(db_session, snapshot=capture, actor="test")
    assert result.contact_outcome is ContactPromotionOutcome.PROMOTION_BLOCKED
    assert capture in promo.pending_captures(db_session)


def test_a_provider_failure_is_retryable(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    promotion, record = run_lookup(db_session, capture, transport_failing())
    assert record is not None
    assert record.lookup_status is EnrichmentLookupStatus.API_UNAVAILABLE
    assert promotion.company_outcome is CompanyResolutionOutcome.LOOKUP_UNAVAILABLE
    assert "retry" in (promotion.blocked_reason or "")

    promotion, record = run_lookup(db_session, capture, transport_returning(*ONE_BRAND), force=True)
    assert record is not None
    assert record.lookup_attempts == 2
    assert promotion.company_outcome is CompanyResolutionOutcome.CANDIDATE_REVIEW_REQUIRED


def test_a_rate_limited_lookup_is_retryable(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    promotion, record = run_lookup(db_session, capture, transport_status(429))
    assert record is not None
    assert record.lookup_status is EnrichmentLookupStatus.RATE_LIMITED
    assert promotion.company_outcome is CompanyResolutionOutcome.LOOKUP_UNAVAILABLE


def test_a_malformed_provider_result_never_becomes_a_candidate(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    promotion, record = run_lookup(db_session, capture, transport_status(200, "not json at all"))
    assert record is not None
    assert record.lookup_status is EnrichmentLookupStatus.MALFORMED
    assert record.candidates == []
    assert promotion.company_outcome is CompanyResolutionOutcome.LOOKUP_UNAVAILABLE


# --- Operator decisions -------------------------------------------------------


def test_operator_confirmation_is_recorded_with_actor_and_time(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    run_lookup(db_session, capture, transport_returning(*TWO_BRANDS))
    promotion = promo.confirm_domain(
        db_session,
        snapshot=capture,
        source=EnrichmentConfirmationSource.CANDIDATE,
        domain=DOMAIN,
        actor="operator-a",
        note="matches the website on their About page",
    )
    record = promo.get_enrichment(db_session, capture.id)
    assert record is not None
    assert record.confirmation_status is EnrichmentConfirmationStatus.CONFIRMED
    assert record.confirmed_domain == DOMAIN
    assert record.confirmation_source is EnrichmentConfirmationSource.CANDIDATE
    assert record.confirmed_by == "operator-a"
    assert record.confirmed_at is not None
    assert record.note
    assert promotion.company_outcome is CompanyResolutionOutcome.DOMAIN_CANDIDATE_CONFIRMED


def test_confirming_a_domain_that_was_never_a_candidate_is_refused(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    run_lookup(db_session, capture, transport_returning(*ONE_BRAND))
    with pytest.raises(promo.PromotionError):
        promo.confirm_domain(
            db_session,
            snapshot=capture,
            source=EnrichmentConfirmationSource.CANDIDATE,
            domain="somewhere-else.example",
            actor="test",
        )


def test_a_manual_domain_is_allowed_and_recorded_as_manual(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    promo.confirm_domain(
        db_session,
        snapshot=capture,
        source=EnrichmentConfirmationSource.MANUAL,
        domain="https://www.meridianworks.example/about",
        actor="test",
    )
    record = promo.get_enrichment(db_session, capture.id)
    assert record is not None
    assert record.confirmed_domain == DOMAIN
    assert record.confirmation_source is EnrichmentConfirmationSource.MANUAL


def test_an_invalid_manual_domain_changes_nothing(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    with pytest.raises(promo.PromotionError):
        promo.confirm_domain(
            db_session,
            snapshot=capture,
            source=EnrichmentConfirmationSource.MANUAL,
            domain="not a domain",
            actor="test",
        )
    record = promo.get_enrichment(db_session, capture.id)
    assert record is not None
    assert record.confirmation_status is EnrichmentConfirmationStatus.UNCONFIRMED


def test_a_rejected_candidate_is_preserved_as_a_decision(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    run_lookup(db_session, capture, transport_returning(*TWO_BRANDS))
    promotion = promo.reject_candidate(
        db_session,
        snapshot=capture,
        domain="meridian-works.example",
        actor="operator-a",
        reason="that is a different company with a similar name",
    )
    record = promo.get_enrichment(db_session, capture.id)
    assert record is not None
    assert [c["domain"] for c in record.candidates] == [DOMAIN]
    assert len(record.rejected_candidates) == 1
    rejected = record.rejected_candidates[0]
    assert rejected["domain"] == "meridian-works.example"
    assert rejected["rejected_by"] == "operator-a"
    assert rejected["rejected_at"]
    assert "different company" in rejected["rejection_reason"]
    assert promotion.company_outcome is CompanyResolutionOutcome.CANDIDATE_REVIEW_REQUIRED


def test_rejecting_every_candidate_leaves_no_candidate(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    run_lookup(db_session, capture, transport_returning(*ONE_BRAND))
    promotion = promo.reject_candidate(
        db_session, snapshot=capture, domain=DOMAIN, actor="test", reason="wrong company"
    )
    assert promotion.company_outcome is CompanyResolutionOutcome.NO_CANDIDATE
    record = promo.get_enrichment(db_session, capture.id)
    assert record is not None
    assert record.confirmed_domain is None


def test_leaving_a_capture_unresolved_is_recorded_and_blocks_promotion(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    promotion = promo.confirm_domain(
        db_session,
        snapshot=capture,
        source=EnrichmentConfirmationSource.UNRESOLVED,
        domain=None,
        actor="test",
        note="cannot tell which entity this is",
    )
    assert promotion.company_outcome is CompanyResolutionOutcome.LEFT_UNRESOLVED
    result = promo.promote(db_session, snapshot=capture, actor="test")
    assert result.contact_outcome is ContactPromotionOutcome.PROMOTION_BLOCKED
    assert db_session.scalar(select(func.count()).select_from(Contact)) == 0


# --- Auto-confirmation policy -------------------------------------------------


def test_a_previously_confirmed_company_is_reused_without_calling_the_provider(
    db_session: Session,
) -> None:
    """The only automatic confirmation: replaying the operator's own decision."""

    first = _stage(db_session, PROFILE_SUBMISSION)[0]
    run_lookup(db_session, first, transport_returning(*ONE_BRAND))
    _confirm(db_session, first)

    second = _stage(db_session, PROFILE_SUBMISSION)[0]
    promotion, record = promo.ensure_records(db_session, second)
    outcome = promo.evaluate_company(
        db_session, promotion=promotion, record=record, hints=promo.company_hints(second)
    )
    assert outcome is CompanyResolutionOutcome.EXISTING_COMPANY_RESOLVED
    assert record is not None
    assert record.confirmed_domain == DOMAIN
    assert record.confirmation_source is EnrichmentConfirmationSource.PRIOR_MAPPING
    # No provider call happened: the lookup never ran for the second capture.
    assert record.lookup_status is EnrichmentLookupStatus.NOT_STARTED


def test_two_disagreeing_prior_confirmations_are_ambiguous_not_a_coin_toss(
    db_session: Session,
) -> None:
    a = _stage(db_session, PROFILE_SUBMISSION)[0]
    promo.confirm_domain(
        db_session,
        snapshot=a,
        source=EnrichmentConfirmationSource.MANUAL,
        domain=DOMAIN,
        actor="test",
    )
    b = _stage(db_session, PROFILE_SUBMISSION)[0]
    promo.confirm_domain(
        db_session,
        snapshot=b,
        source=EnrichmentConfirmationSource.MANUAL,
        domain="other-meridian.example",
        actor="test",
    )

    c = _stage(db_session, PROFILE_SUBMISSION)[0]
    promotion, record = promo.ensure_records(db_session, c)
    outcome = promo.evaluate_company(
        db_session, promotion=promotion, record=record, hints=promo.company_hints(c)
    )
    assert outcome is CompanyResolutionOutcome.COMPANY_IDENTITY_AMBIGUOUS
    assert record is not None
    assert record.confirmation_status is EnrichmentConfirmationStatus.UNCONFIRMED
    assert len(promotion.detail["prior_confirmed_domains"]) == 2


def test_a_prior_confirmation_for_a_different_linkedin_company_is_not_reused(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """Same display name, different LinkedIn company: not the same employer."""

    other = SalesNavCompanyEnrichment(
        capture_id=None,
        batch_id=None,
        company_key="meridian works",
        company_name="Meridian Works",
        company_linkedin_id="a-different-meridian",
        confirmation_status=EnrichmentConfirmationStatus.CONFIRMED,
        confirmed_domain="not-ours.example",
        confirmation_source=EnrichmentConfirmationSource.MANUAL,
    )
    # Owner check constraint: attach it to a capture so the row is legal.
    decoy = _stage(db_session, PROFILE_SUBMISSION)[0]
    other.capture_id = decoy.id
    db_session.add(other)
    db_session.flush()

    promotion, record = promo.ensure_records(db_session, capture)
    outcome = promo.evaluate_company(
        db_session, promotion=promotion, record=record, hints=promo.company_hints(capture)
    )
    assert outcome is CompanyResolutionOutcome.PENDING_LOOKUP


# --- Company resolution -------------------------------------------------------


def test_promotion_creates_the_company_once_and_reuses_it_afterwards(db_session: Session) -> None:
    first = _stage(db_session, PROFILE_SUBMISSION)[0]
    _stage_and_confirm(db_session, first)
    result_a = promo.promote(db_session, snapshot=first, actor="test")
    assert result_a.company is not None
    assert result_a.company.domain == DOMAIN

    second = _stage(db_session, SALESNAV_SUBMISSION)[1]  # a different person
    promo.confirm_domain(
        db_session,
        snapshot=second,
        source=EnrichmentConfirmationSource.MANUAL,
        domain=DOMAIN,
        actor="test",
    )
    result_b = promo.promote(db_session, snapshot=second, actor="test")
    assert result_b.company is not None
    assert result_b.company.id == result_a.company.id
    assert db_session.scalar(select(func.count()).select_from(Company)) == 1


def test_an_existing_company_row_is_reused_never_rewritten(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    existing = Company(name="Meridian Works Holdings", domain=DOMAIN, industry="Facilities")
    db_session.add(existing)
    db_session.flush()

    _stage_and_confirm(db_session, capture)
    result = promo.promote(db_session, snapshot=capture, actor="test")
    assert result.company is not None
    assert result.company.id == existing.id
    # The capture never overwrites what the company record already says.
    assert result.company.name == "Meridian Works Holdings"
    assert result.company.industry == "Facilities"


def _stage_and_confirm(db: Session, snapshot: LinkedInProfileSnapshot) -> None:
    run_lookup(db, snapshot, transport_returning(*ONE_BRAND))
    _confirm(db, snapshot)


# --- Contact promotion --------------------------------------------------------


def test_promotion_creates_a_canonical_contact_from_the_capture(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    _stage_and_confirm(db_session, capture)
    result = promo.promote(db_session, snapshot=capture, actor="operator-a")

    assert result.company_outcome is CompanyResolutionOutcome.DOMAIN_CANDIDATE_CONFIRMED
    assert result.contact_outcome is ContactPromotionOutcome.CONTACT_CREATED
    contact = result.contact
    assert contact is not None
    assert (contact.first_name, contact.last_name) == ("Morgan", "Vale")
    assert contact.company_domain == DOMAIN
    assert contact.company_name == "Meridian Works"
    assert contact.title == "Director of Operations"
    assert contact.linkedin_url == "https://www.linkedin.com/in/morgan-vale"
    assert contact.natural_key == f"morgan|vale|{DOMAIN}"
    # Identity, not permission: no email is invented.
    assert contact.email is None

    promotion = promo.get_promotion(db_session, capture.id)
    assert promotion is not None
    assert promotion.promoted_contact_id == contact.id
    assert promotion.resolved_domain == DOMAIN
    assert promotion.resolved_company_id is not None
    assert promotion.promoted_by == "operator-a"
    assert promotion.promoted_at is not None
    # The capture is permanently linked to the contact it became.
    assert capture.matched_contact_id == contact.id
    assert capture not in promo.pending_captures(db_session)


def test_an_exact_url_match_links_instead_of_duplicating(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    existing = _seed_contact(
        db_session, linkedin_url="https://www.LinkedIn.com/in/Morgan-Vale/?trk=x"
    )
    _stage_and_confirm(db_session, capture)
    result = promo.promote(db_session, snapshot=capture, actor="test")

    assert result.contact_outcome is ContactPromotionOutcome.CONTACT_EXACT_MATCH_LINKED
    assert result.contact is not None
    assert result.contact.id == existing.id
    assert db_session.scalar(select(func.count()).select_from(Contact)) == 1
    assert result.promotion.detail["match_kind"] == "linked_by_url"


def test_an_exact_natural_key_match_links_instead_of_duplicating(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    existing = _seed_contact(db_session, linkedin_url=None)
    _stage_and_confirm(db_session, capture)
    result = promo.promote(db_session, snapshot=capture, actor="test")

    assert result.contact_outcome is ContactPromotionOutcome.CONTACT_EXACT_MATCH_LINKED
    assert result.contact is not None and result.contact.id == existing.id
    assert result.promotion.detail["match_kind"] == "linked_by_natural_key"


def test_two_contacts_on_one_url_block_promotion(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    _seed_contact(db_session, linkedin_url="https://www.linkedin.com/in/morgan-vale")
    _seed_contact(
        db_session,
        domain="second.example",
        linkedin_url="https://www.linkedin.com/in/Morgan-Vale/",
    )
    before = db_session.scalar(select(func.count()).select_from(Contact))

    _stage_and_confirm(db_session, capture)
    result = promo.promote(db_session, snapshot=capture, actor="test")

    assert result.contact_outcome is ContactPromotionOutcome.CONTACT_IDENTITY_AMBIGUOUS
    assert result.contact is None
    assert db_session.scalar(select(func.count()).select_from(Contact)) == before
    assert len(result.promotion.detail["ambiguous_contact_ids"]) == 2


def test_an_ambiguous_natural_key_blocks_promotion(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    _seed_contact(db_session, email="a@meridianworks.example")
    _seed_contact(db_session, email="b@meridianworks.example")
    before = db_session.scalar(select(func.count()).select_from(Contact))

    _stage_and_confirm(db_session, capture)
    result = promo.promote(db_session, snapshot=capture, actor="test")

    assert result.contact_outcome is ContactPromotionOutcome.CONTACT_IDENTITY_AMBIGUOUS
    assert db_session.scalar(select(func.count()).select_from(Contact)) == before


def test_a_capture_without_a_usable_name_cannot_be_promoted(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    fields = dict(capture.profile_fields)
    fields.update({"full_name": "Cher", "first_name": None, "last_name": None})
    capture.profile_fields = fields
    db_session.flush()

    _stage_and_confirm(db_session, capture)
    result = promo.promote(db_session, snapshot=capture, actor="test")
    assert result.contact_outcome is ContactPromotionOutcome.PROMOTION_BLOCKED
    assert "first and last name" in (result.blocked_reason or "")
    assert db_session.scalar(select(func.count()).select_from(Contact)) == 0


# --- Suppression --------------------------------------------------------------


def test_a_suppressed_domain_blocks_promotion_entirely(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    add_suppression(
        db_session,
        suppression_type=SuppressionType.DOMAIN,
        value=DOMAIN,
        reason=SuppressionReason.COMPETITOR,
        source="test",
    )
    _stage_and_confirm(db_session, capture)
    result = promo.promote(db_session, snapshot=capture, actor="test")

    assert result.contact_outcome is ContactPromotionOutcome.SUPPRESSED
    assert result.contact is None
    assert db_session.scalar(select(func.count()).select_from(Contact)) == 0
    assert db_session.scalar(select(func.count()).select_from(Company)) == 0
    assert result.promotion.detail["suppression_reason"]
    assert capture.matched_contact_id is None


def test_a_suppressed_matched_contact_is_never_touched(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    existing = _seed_contact(
        db_session,
        email="morgan@meridianworks.example",
        linkedin_url="https://www.linkedin.com/in/morgan-vale",
    )
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="morgan@meridianworks.example",
        reason=SuppressionReason.OPT_OUT,
        source="test",
    )
    _stage_and_confirm(db_session, capture)
    result = promo.promote(db_session, snapshot=capture, actor="test")

    assert result.contact_outcome is ContactPromotionOutcome.SUPPRESSED
    db_session.refresh(existing)
    assert existing.title == "Operations Manager"  # untouched
    assert db_session.scalar(select(func.count()).select_from(ContactLabelAssignment)) == 0


# --- Labels, notes, provenance ------------------------------------------------


def test_labels_carry_over_to_the_promoted_contact(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    assert capture.operator_labels == ["Healthcare", "Market Entry"]
    _stage_and_confirm(db_session, capture)
    result = promo.promote(db_session, snapshot=capture, actor="test")

    assert sorted(result.labels_applied) == ["Healthcare", "Market Entry"]
    assert result.contact is not None
    applied = db_session.scalars(
        select(ContactLabel.name)
        .join(ContactLabelAssignment, ContactLabelAssignment.label_id == ContactLabel.id)
        .where(ContactLabelAssignment.contact_id == result.contact.id)
        .order_by(ContactLabel.slug)
    ).all()
    assert list(applied) == ["Healthcare", "Market Entry"]
    assert result.promotion.labels_applied == result.labels_applied


def test_notes_carry_over_append_only_and_unmodified(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    before = db_session.scalars(
        select(ContactCaptureNote).where(ContactCaptureNote.capture_id == capture.id)
    ).all()
    assert before
    original = [(n.id, n.note_text, n.created_at, n.scope) for n in before]

    _stage_and_confirm(db_session, capture)
    result = promo.promote(db_session, snapshot=capture, actor="test")
    assert result.contact is not None

    after = db_session.scalars(
        select(ContactCaptureNote).where(ContactCaptureNote.capture_id == capture.id)
    ).all()
    # Same rows, same text, same time: only the contact link was filled in.
    assert [(n.id, n.note_text, n.created_at, n.scope) for n in after] == original
    assert all(n.contact_id == result.contact.id for n in after)
    assert result.notes_linked == len(after)


def test_the_capture_evidence_is_promoted_into_the_provenance_ledger(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    _stage_and_confirm(db_session, capture)
    result = promo.promote(db_session, snapshot=capture, actor="test")
    assert result.contact is not None

    fields = db_session.scalars(
        select(ContactFieldValue.field_name).where(
            ContactFieldValue.contact_id == result.contact.id
        )
    ).all()
    assert {"title", "company_name", "linkedin_url"} <= set(fields)


# --- Immutability, idempotency, isolation -------------------------------------


def test_the_original_capture_stays_immutable(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    payload = copy.deepcopy(capture.payload)
    profile_fields = copy.deepcopy(capture.profile_fields)
    content_hash = capture.content_hash
    schema_version = capture.schema_version
    experiences = [(e.position_index, e.job_title, e.company_name) for e in capture.experiences]

    _stage_and_confirm(db_session, capture)
    promo.promote(db_session, snapshot=capture, actor="test")
    db_session.refresh(capture)

    assert capture.payload == payload
    assert capture.profile_fields == profile_fields
    assert capture.content_hash == content_hash
    assert capture.schema_version == schema_version
    assert [
        (e.position_index, e.job_title, e.company_name) for e in capture.experiences
    ] == experiences


def test_promoting_twice_is_idempotent(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    _stage_and_confirm(db_session, capture)
    first = promo.promote(db_session, snapshot=capture, actor="test")
    second = promo.promote(db_session, snapshot=capture, actor="test")

    assert first.contact_outcome is ContactPromotionOutcome.CONTACT_CREATED
    assert second.contact_outcome is ContactPromotionOutcome.ALREADY_PROMOTED
    assert second.contact is not None and first.contact is not None
    assert second.contact.id == first.contact.id
    assert db_session.scalar(select(func.count()).select_from(Contact)) == 1
    assert db_session.scalar(select(func.count()).select_from(ContactCapturePromotion)) == 1
    assert db_session.scalar(select(func.count()).select_from(Company)) == 1
    # No second set of label assignments or notes.
    assert db_session.scalar(select(func.count()).select_from(ContactLabelAssignment)) == 2


def test_a_partial_failure_leaves_nothing_behind(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    _stage_and_confirm(db_session, capture)
    db_session.commit()

    def _boom() -> None:
        raise RuntimeError("interrupted mid-promotion")

    with pytest.raises(RuntimeError):
        promo.promote(db_session, snapshot=capture, actor="test", _fault=_boom)
    db_session.rollback()

    assert db_session.scalar(select(func.count()).select_from(Contact)) == 0
    assert db_session.scalar(select(func.count()).select_from(Company)) == 0
    promotion = promo.get_promotion(db_session, capture.id)
    assert promotion is None or promotion.promoted_contact_id is None


def test_promotion_creates_no_campaign_email_or_outreach_state(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    _stage_and_confirm(db_session, capture)
    result = promo.promote(db_session, snapshot=capture, actor="test")
    assert result.contact is not None

    for model in (CampaignContact, EmailCandidate, DraftVersion):
        assert db_session.scalar(select(func.count()).select_from(model)) == 0


def test_promotion_is_audited(db_session: Session, capture: LinkedInProfileSnapshot) -> None:
    _stage_and_confirm(db_session, capture)
    promo.promote(db_session, snapshot=capture, actor="operator-a")
    event = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == promo.PROMOTE_AUDIT_ACTION)
    ).one()
    assert event.actor == "operator-a"
    assert event.context["resolved_domain"] == DOMAIN
    assert event.context["company_outcome"]
    assert event.context["contact_outcome"]


def test_a_batch_owned_enrichment_record_is_untouched_by_capture_work(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """DAT-010 records and DAT-014 records never collide in the shared table."""

    _stage_and_confirm(db_session, capture)
    records = db_session.scalars(select(SalesNavCompanyEnrichment)).all()
    assert len(records) == 1
    assert records[0].capture_id == capture.id
    assert records[0].batch_id is None
    assert records[0].owner_label == "capture"


# --- Workbench boundary -------------------------------------------------------


@pytest.fixture()
def workbench_client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
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
            yield client
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


@pytest.fixture()
def workbench_without_promotion(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_INTAKE", "true")
    monkeypatch.delenv("FEATURES__CONTACT_CAPTURE_PROMOTION", raising=False)
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_the_pending_page_lists_captures_and_their_resolution_state(
    workbench_client: TestClient, capture: LinkedInProfileSnapshot
) -> None:
    response = workbench_client.get("/contact-captures/pending")
    assert response.status_code == 200
    assert "Morgan Vale" in response.text
    assert "Meridian Works" in response.text
    assert "pending_lookup" in response.text


def test_the_capture_page_shows_the_resolution_card_and_its_controls(
    workbench_client: TestClient, capture: LinkedInProfileSnapshot
) -> None:
    response = workbench_client.get(f"/contact-captures/{capture.id}")
    assert response.status_code == 200
    assert "Company resolution and promotion" in response.text
    assert "Promote to contact" in response.text
    assert "captured company" in response.text
    assert "Run domain lookup" in response.text


def test_promotion_routes_are_absent_when_the_feature_is_off(
    workbench_without_promotion: TestClient,
    db_session: Session,
    capture: LinkedInProfileSnapshot,
) -> None:
    listing = workbench_without_promotion.get("/contact-captures/pending")
    assert listing.status_code == 404

    # The capture page still renders as read-only evidence, without controls.
    page = workbench_without_promotion.get(f"/contact-captures/{capture.id}")
    assert page.status_code == 200
    assert "Company resolution and promotion" not in page.text
    assert "Promote to contact" not in page.text

    for path in ("promote", "company/lookup"):
        response = workbench_without_promotion.post(
            f"/contact-captures/{capture.id}/{path}", follow_redirects=False
        )
        assert response.status_code in (302, 303, 307)
    # Nothing was created by any of those calls.
    assert db_session.scalar(select(func.count()).select_from(ContactCapturePromotion)) == 0
    assert db_session.scalar(select(func.count()).select_from(Contact)) == 0


def test_the_workbench_confirm_and_promote_round_trip(
    workbench_client: TestClient, db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    confirm = workbench_client.post(
        f"/contact-captures/{capture.id}/company/confirm",
        data={"decision": "manual", "domain": DOMAIN},
        follow_redirects=False,
    )
    assert confirm.status_code in (302, 303, 307)

    promote = workbench_client.post(
        f"/contact-captures/{capture.id}/promote", follow_redirects=False
    )
    assert promote.status_code in (302, 303, 307)

    promotion = promo.get_promotion(db_session, capture.id)
    assert promotion is not None
    assert promotion.promoted_contact_id is not None
    assert promotion.resolved_domain == DOMAIN


def test_a_lookup_without_a_provider_key_runs_nothing_and_says_so(
    workbench_client: TestClient, db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    response = workbench_client.post(
        f"/contact-captures/{capture.id}/company/lookup", follow_redirects=False
    )
    assert response.status_code in (302, 303, 307)
    record = promo.get_enrichment(db_session, capture.id)
    assert record is None or record.lookup_status is EnrichmentLookupStatus.NOT_STARTED


def test_the_workbench_refuses_a_non_local_environment(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workbench (and therefore promotion) cannot serve a non-local env."""

    from app.main import WorkbenchConfigurationError

    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_PROMOTION", "true")
    monkeypatch.setenv("APP_ENV", "staging")
    get_settings.cache_clear()
    try:
        with pytest.raises(WorkbenchConfigurationError):
            create_app()
    finally:
        get_settings.cache_clear()


# --- Migration ----------------------------------------------------------------


def test_the_shared_enrichment_table_enforces_exactly_one_owner(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    from sqlalchemy.exc import IntegrityError

    orphan = SalesNavCompanyEnrichment(
        batch_id=None, capture_id=None, company_key="x", company_name="X"
    )
    db_session.add(orphan)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_promotion_records_are_unique_per_capture(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    from sqlalchemy.exc import IntegrityError

    promo.ensure_records(db_session, capture)
    duplicate = ContactCapturePromotion(
        capture_id=capture.id,
        company_outcome=CompanyResolutionOutcome.PENDING_LOOKUP,
        contact_outcome=ContactPromotionOutcome.PENDING,
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_observed_at_is_taken_from_the_capture_not_the_clock(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """Promotion evidence must be dated when it was OBSERVED, not when promoted."""

    _stage_and_confirm(db_session, capture)
    result = promo.promote(db_session, snapshot=capture, actor="test")
    assert result.contact is not None
    observed = db_session.scalars(
        select(ContactFieldValue.observed_at).where(
            ContactFieldValue.contact_id == result.contact.id
        )
    ).all()
    captured_at = capture.captured_at
    assert captured_at is not None
    assert all(o is not None and o.astimezone(UTC) == captured_at.astimezone(UTC) for o in observed)
    assert captured_at < datetime.now(UTC)


# --- UI-014: stale promotion-refusal copy -------------------------------------


def test_confirming_a_candidate_clears_the_earlier_refusal(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """The page must not say "promotable" and "blocked" in the same breath.

    DAT-011 saw exactly that: the capture moved to domain_candidate_confirmed and
    promotion worked, while the row still carried the refusal from before the
    confirmation. The refusal is what the page renders, so a stale one is a
    contradiction an operator has to resolve by guessing.
    """

    promotion, _record = run_lookup(db_session, capture, transport_returning(*TWO_BRANDS))
    assert promotion.blocked_reason, "the multi-candidate state must give a reason"

    promotion = promo.confirm_domain(
        db_session,
        snapshot=capture,
        source=EnrichmentConfirmationSource.CANDIDATE,
        domain=DOMAIN,
        actor="operator-a",
    )

    assert promotion.company_outcome is CompanyResolutionOutcome.DOMAIN_CANDIDATE_CONFIRMED
    assert promotion.blocked_reason is None
    view = promo.build_view(db_session, capture)
    assert view.can_promote
    assert view.promotion.blocked_reason is None


def test_the_refusal_stays_cleared_when_the_page_is_reloaded(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """``build_view`` re-evaluates on every visit, so it is where copy comes back."""

    run_lookup(db_session, capture, transport_returning(*TWO_BRANDS))
    _stage_and_confirm(db_session, capture)

    for _ in range(3):
        view = promo.build_view(db_session, capture)
        assert view.promotion.blocked_reason is None
        assert "waiting for your confirmation" not in (view.promotion.blocked_reason or "")


def test_a_manual_domain_also_clears_the_earlier_refusal(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    run_lookup(db_session, capture, transport_returning(*TWO_BRANDS))

    promotion = promo.confirm_domain(
        db_session,
        snapshot=capture,
        source=EnrichmentConfirmationSource.MANUAL,
        domain="typed-by-hand.example",
        actor="operator-a",
    )

    assert promotion.blocked_reason is None
    assert promotion.resolved_domain == "typed-by-hand.example"


def test_the_refusal_counts_the_candidates_actually_waiting(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """A rejection shrinks the set, so the sentence describing it has to shrink too."""

    promotion, _record = run_lookup(db_session, capture, transport_returning(*TWO_BRANDS))
    assert promotion.blocked_reason == "2 domain candidates are waiting for your confirmation"

    promotion = promo.reject_candidate(
        db_session,
        snapshot=capture,
        domain="meridian-works.example",
        actor="operator-a",
        reason="different company, similar name",
    )

    assert promotion.company_outcome is CompanyResolutionOutcome.CANDIDATE_REVIEW_REQUIRED
    assert promotion.blocked_reason == "1 domain candidate is waiting for your confirmation"
    assert "2 domain candidates" not in (promotion.blocked_reason or "")


def test_a_genuinely_blocked_capture_keeps_its_reason(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """Clearing stale copy must not clear true copy.

    Each of these is a real refusal, and each has to survive both the evaluation
    that produced it and every later page load.
    """

    run_lookup(db_session, capture, transport_returning())
    view = promo.build_view(db_session, capture)
    assert view.promotion.company_outcome is CompanyResolutionOutcome.NO_CANDIDATE
    assert "no usable domain candidate" in (view.promotion.blocked_reason or "")
    assert not view.can_promote

    promo.confirm_domain(
        db_session,
        snapshot=capture,
        source=EnrichmentConfirmationSource.UNRESOLVED,
        domain=None,
        actor="operator-a",
        note="two entities share this name",
    )
    view = promo.build_view(db_session, capture)
    assert view.promotion.company_outcome is CompanyResolutionOutcome.LEFT_UNRESOLVED
    assert view.promotion.blocked_reason == "the operator left this company deliberately unresolved"
    assert not view.can_promote


def test_promotion_still_succeeds_after_the_refusal_is_cleared(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """The gate itself was never wrong, and must stay exactly as strict."""

    run_lookup(db_session, capture, transport_returning(*TWO_BRANDS))
    _stage_and_confirm(db_session, capture)

    result = promo.promote(db_session, snapshot=capture, actor="test")

    assert result.promoted
    assert result.company_outcome is CompanyResolutionOutcome.DOMAIN_CANDIDATE_CONFIRMED
    assert result.blocked_reason is None
    assert result.promotion.blocked_reason is None


@pytest.mark.parametrize(
    ("source", "domain", "expected"),
    [
        (EnrichmentConfirmationSource.CANDIDATE, DOMAIN, "domain candidate confirmed"),
        (EnrichmentConfirmationSource.MANUAL, "typed-by-hand.example", "domain entered manually"),
    ],
)
def test_the_outcome_phrase_names_how_the_domain_was_decided(
    db_session: Session,
    capture: LinkedInProfileSnapshot,
    source: EnrichmentConfirmationSource,
    domain: str,
    expected: str,
) -> None:
    """One outcome value covers both, so the enum name alone credits the provider.

    A domain the operator typed is not a candidate the provider offered, and
    telling them it was misreports whose decision it is.
    """

    run_lookup(db_session, capture, transport_returning(*TWO_BRANDS))
    promotion = promo.confirm_domain(
        db_session, snapshot=capture, source=source, domain=domain, actor="operator-a"
    )
    record = promo.get_enrichment(db_session, capture.id)

    assert promotion.company_outcome is CompanyResolutionOutcome.DOMAIN_CANDIDATE_CONFIRMED
    assert promo.company_outcome_phrase(promotion.company_outcome, record=record) == expected


def test_the_outcome_phrase_falls_back_to_the_outcome_when_it_cannot_narrow_it(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """A provisional domain has no confirmation source by design; say so plainly."""

    assert (
        promo.company_outcome_phrase(CompanyResolutionOutcome.DOMAIN_PROVISIONAL, record=None)
        == "domain provisional"
    )
    assert (
        promo.company_outcome_phrase(
            CompanyResolutionOutcome.DOMAIN_CANDIDATE_CONFIRMED, record=None
        )
        == "domain candidate confirmed"
    )


def test_the_displayed_location_survives_promotion(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """A capture's location used to vanish the moment the Contact existed.

    The value lived only in the capture's ``profile_fields`` JSON, and the CRM row
    for a promoted contact read ``contacts.country`` — which nothing writes. So the
    pending capture showed a location and the contact beside it showed nothing,
    which reads as data loss because it was.
    """

    capture.profile_fields = dict(capture.profile_fields or {})
    capture.profile_fields["displayed_location"] = "Greater Chicago Area"
    db_session.flush()

    _stage_and_confirm(db_session, capture)
    result = promo.promote(db_session, snapshot=capture)
    assert result.contact is not None
    assert result.contact.location == "Greater Chicago Area"


def test_a_capture_with_no_location_carries_none_rather_than_an_empty_string(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """A page that showed no location must stay distinguishable from a blank one."""

    capture.profile_fields = dict(capture.profile_fields or {})
    capture.profile_fields["displayed_location"] = "   "
    db_session.flush()

    _stage_and_confirm(db_session, capture)
    result = promo.promote(db_session, snapshot=capture)
    assert result.contact is not None
    assert result.contact.location is None

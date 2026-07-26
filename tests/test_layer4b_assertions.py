"""Regression tests for the Layer 4B acceptance harness (scripts/layer4b_assertions.py).

The harness grades a live DAT-014 acceptance run. It runs against a local
database that also accumulates unrelated captures — chiefly those created while
exercising the capture extension against real LinkedIn pages.

An earlier version graded every capture-owned row in the database, which
produced false failures: check A failed on an unrelated capture whose lookup was
``API_UNAVAILABLE``, check C failed because that capture was never confirmed, and
check C2 reported an aggregate attempt total mixing acceptance attempts with
unrelated ones. Those were harness scoping defects, not DAT-014 defects.

These tests hold the scoping in place, and — just as importantly — prove the
scoping did not simply disable the checks: a sanctioned row with a bad status,
missing confirmation, malformed candidate data or a wrong attempt count still
fails.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from app.models.company import Company
from app.models.contact import Contact
from app.models.contact_capture import ContactCaptureNote, ContactLabel, ContactLabelAssignment
from app.models.enums import (
    CompanyResolutionOutcome,
    ContactPromotionOutcome,
    EnrichmentConfirmationSource,
    EnrichmentConfirmationStatus,
    EnrichmentLookupStatus,
    LinkedInSnapshotOutcome,
)
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.models.salesnav_enrichment import SalesNavCompanyEnrichment
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from datetime import UTC

import layer4b_assertions as harness  # noqa: E402

DOMAIN = "mozilla.example"
CANDIDATE: dict[str, Any] = {"domain": DOMAIN, "name": "Mozilla", "rank": 1, "confidence": None}
REJECTED: dict[str, Any] = {
    "domain": "decoy.example",
    "name": "Mozilla Decoy",
    "rank": 2,
    "confidence": None,
    "rejection_reason": "different entity",
    "rejected_by": "workbench",
    "rejected_at": "2026-07-26T18:00:00+00:00",
}


# --- Builders -----------------------------------------------------------------


def _capture(db: Session, *, name: str) -> LinkedInProfileSnapshot:
    snapshot = LinkedInProfileSnapshot(
        client_capture_id=str(uuid.uuid4()),
        content_hash=uuid.uuid4().hex,
        schema_version="linkedin-contact-capture/2.0.0",
        source="chrome-extension:linkedin-contact-capture",
        capture_mode="linkedin_profile",
        source_surface="linkedin_person_profile",
        source_url="https://www.linkedin.com/in/" + name,
        normalized_profile_url="https://www.linkedin.com/in/" + name,
        extraction_status="ok",
        payload={"synthetic": True},
        profile_fields={"full_name": name},
        outcome=LinkedInSnapshotOutcome.UNMATCHED_STAGED,
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def _enrichment(
    db: Session,
    capture: LinkedInProfileSnapshot,
    *,
    status: EnrichmentLookupStatus,
    attempts: int,
    source: EnrichmentConfirmationSource | None,
    candidates: list[dict[str, Any]] | None = None,
    rejected: list[dict[str, Any]] | None = None,
) -> SalesNavCompanyEnrichment:
    confirmed = source is not None
    record = SalesNavCompanyEnrichment(
        capture_id=capture.id,
        company_key="mozilla",
        company_name="Mozilla",
        lookup_query="Mozilla",
        normalized_query="mozilla",
        provider="logo.dev",
        lookup_version="logo.dev/search-brands/v1",
        lookup_status=status,
        lookup_attempts=attempts,
        looked_up_at=_now() if status is not EnrichmentLookupStatus.NOT_STARTED else None,
        candidates=candidates if candidates is not None else [dict(CANDIDATE)],
        rejected_candidates=rejected or [],
        confirmation_status=(
            EnrichmentConfirmationStatus.CONFIRMED
            if confirmed
            else EnrichmentConfirmationStatus.UNCONFIRMED
        ),
        confirmation_source=source,
        confirmed_domain=DOMAIN if confirmed else None,
        confirmed_by="workbench" if confirmed else None,
        confirmed_at=_now() if confirmed else None,
    )
    db.add(record)
    db.flush()
    return record


def _now() -> Any:
    from datetime import datetime

    return datetime.now(UTC)


def _promote(db: Session, capture: LinkedInProfileSnapshot, *, first: str, last: str) -> Contact:
    from app.models.capture_promotion import ContactCapturePromotion

    company = db.query(Company).filter(Company.domain == DOMAIN).one_or_none()
    if company is None:
        company = Company(name="Mozilla", domain=DOMAIN)
        db.add(company)
        db.flush()
    contact = Contact(
        first_name=first,
        last_name=last,
        company_name="Mozilla",
        company_domain=DOMAIN,
        title="Director of Operations",
        natural_key=f"{first.casefold()}|{last.casefold()}|{DOMAIN}",
    )
    db.add(contact)
    db.flush()
    db.add(
        ContactCapturePromotion(
            capture_id=capture.id,
            company_outcome=CompanyResolutionOutcome.DOMAIN_CANDIDATE_CONFIRMED,
            contact_outcome=ContactPromotionOutcome.CONTACT_CREATED,
            resolved_company_id=company.id,
            resolved_domain=DOMAIN,
            promoted_contact_id=contact.id,
            labels_applied=["Healthcare"],
            notes_linked=1,
        )
    )
    db.add(
        ContactCaptureNote(
            capture_id=capture.id,
            contact_id=contact.id,
            scope="submission",
            note_text="synthetic acceptance note",
            author="workbench",
        )
    )
    label = db.query(ContactLabel).filter(ContactLabel.slug == "healthcare").one_or_none()
    if label is None:
        label = ContactLabel(name="Healthcare", slug="healthcare")
        db.add(label)
        db.flush()
    db.add(
        ContactLabelAssignment(
            contact_id=contact.id, label_id=label.id, capture_id=capture.id, source="capture"
        )
    )
    capture.matched_contact_id = contact.id
    db.flush()
    return contact


@pytest.fixture()
def sanctioned(db_session: Session) -> list[str]:
    """Morgan (live lookup, 2 attempts) and Riley (prior mapping, 0 attempts)."""

    morgan = _capture(db_session, name="morgan-vale")
    _enrichment(
        db_session,
        morgan,
        status=EnrichmentLookupStatus.OK,
        attempts=2,
        source=EnrichmentConfirmationSource.CANDIDATE,
        rejected=[dict(REJECTED)],
    )
    _promote(db_session, morgan, first="Morgan", last="Vale")

    riley = _capture(db_session, name="riley-chen")
    _enrichment(
        db_session,
        riley,
        status=EnrichmentLookupStatus.NOT_STARTED,
        attempts=0,
        source=EnrichmentConfirmationSource.PRIOR_MAPPING,
    )
    _promote(db_session, riley, first="Riley", last="Chen")
    return [str(morgan.id), str(riley.id)]


@pytest.fixture()
def unrelated(db_session: Session) -> str:
    """An unrelated extension-test capture: api_unavailable, 1 attempt, unconfirmed.

    This is exactly the shape that produced the false failures.
    """

    other = _capture(db_session, name="unrelated-real-capture")
    _enrichment(
        db_session,
        other,
        status=EnrichmentLookupStatus.API_UNAVAILABLE,
        attempts=1,
        source=None,
        candidates=[],
    )
    return str(other.id)


def _run(db: Session, captures: list[str], expected: int = 2) -> harness.Result:
    return harness.evaluate(db.connection(), captures, expected)


# --- The scoping defect this harness had --------------------------------------


def test_unrelated_capture_cannot_fail_acceptance(
    db_session: Session, sanctioned: list[str], unrelated: str
) -> None:
    """An unrelated api_unavailable/unconfirmed capture must not fail A, C or C2."""

    result = _run(db_session, sanctioned)

    assert result.failed == [], f"unrelated rows leaked into graded checks: {result.failed}"
    assert result.empty == []
    assert result.passed


def test_unrelated_attempts_are_excluded_from_the_acceptance_total(
    db_session: Session, sanctioned: list[str], unrelated: str
) -> None:
    """C2 counts 2 (Morgan) + 0 (Riley) = 2, never the database-wide 3."""

    result = _run(db_session, sanctioned, expected=2)

    row = result.rows["C2"][0]
    assert row["scoped_provider_attempts"] == 2
    assert row["authorised_attempts"] == 2
    assert row["reused_without_lookup"] == 1
    assert row["verdict"] == "PASS"

    # The aggregate total really is higher — the harness must not use it.
    assert result.excluded["excluded_captures"] == 1
    assert result.excluded["excluded_provider_attempts"] == 1


def test_aggregate_total_is_not_substituted_for_the_scoped_total(
    db_session: Session, sanctioned: list[str], unrelated: str
) -> None:
    """Asking for the aggregate figure (3) must FAIL: scope is 2, not 3."""

    result = _run(db_session, sanctioned, expected=3)

    assert "C2" in result.failed
    assert result.rows["C2"][0]["scoped_provider_attempts"] == 2


def test_excluded_section_reports_counts_only(
    db_session: Session, sanctioned: list[str], unrelated: str
) -> None:
    """The informational section must expose no personal data."""

    result = _run(db_session, sanctioned)
    report = harness.render(
        result,
        database="vmr_dat014",
        captures=sanctioned,
        expected_attempts=2,
        attempts_note=None,
    )

    assert set(result.excluded) == {"excluded_captures", "excluded_provider_attempts"}
    assert "unrelated-real-capture" not in report
    assert unrelated not in report
    assert "DAT-016" in report and "#167" in report


# --- The checks must still bite on sanctioned rows ----------------------------


def test_sanctioned_bad_lookup_status_still_fails_a(
    db_session: Session, sanctioned: list[str]
) -> None:
    record = (
        db_session.query(SalesNavCompanyEnrichment)
        .filter(SalesNavCompanyEnrichment.lookup_attempts == 2)
        .one()
    )
    record.lookup_status = EnrichmentLookupStatus.API_UNAVAILABLE
    db_session.flush()

    assert "A" in _run(db_session, sanctioned).failed


def test_sanctioned_missing_confirmation_still_fails_c(
    db_session: Session, sanctioned: list[str]
) -> None:
    record = (
        db_session.query(SalesNavCompanyEnrichment)
        .filter(
            SalesNavCompanyEnrichment.confirmation_source == EnrichmentConfirmationSource.CANDIDATE
        )
        .one()
    )
    record.confirmed_by = None
    db_session.flush()

    assert "C" in _run(db_session, sanctioned).failed


def test_sanctioned_candidate_without_rank_or_confidence_still_fails_b(
    db_session: Session, sanctioned: list[str]
) -> None:
    record = (
        db_session.query(SalesNavCompanyEnrichment)
        .filter(SalesNavCompanyEnrichment.lookup_attempts == 2)
        .one()
    )
    record.candidates = [{"domain": DOMAIN, "name": "Mozilla"}]  # no rank, no confidence key
    db_session.flush()

    assert "B" in _run(db_session, sanctioned).failed


def test_sanctioned_invented_confidence_still_fails_b(
    db_session: Session, sanctioned: list[str]
) -> None:
    record = (
        db_session.query(SalesNavCompanyEnrichment)
        .filter(SalesNavCompanyEnrichment.lookup_attempts == 2)
        .one()
    )
    record.candidates = [{"domain": DOMAIN, "name": "Mozilla", "rank": 1, "confidence": 0.92}]
    db_session.flush()

    assert "B" in _run(db_session, sanctioned).failed


def test_sanctioned_wrong_attempt_count_still_fails_c2(
    db_session: Session, sanctioned: list[str]
) -> None:
    record = (
        db_session.query(SalesNavCompanyEnrichment)
        .filter(SalesNavCompanyEnrichment.lookup_attempts == 2)
        .one()
    )
    record.lookup_attempts = 5
    db_session.flush()

    assert "C2" in _run(db_session, sanctioned).failed


def test_prior_mapping_that_actually_called_the_provider_fails_c2(
    db_session: Session, sanctioned: list[str]
) -> None:
    """A prior-mapping reuse must cost zero provider attempts."""

    record = (
        db_session.query(SalesNavCompanyEnrichment)
        .filter(
            SalesNavCompanyEnrichment.confirmation_source
            == EnrichmentConfirmationSource.PRIOR_MAPPING
        )
        .one()
    )
    record.lookup_attempts = 1
    db_session.flush()

    assert "C2" in _run(db_session, sanctioned, expected=3).failed


def test_rejection_without_a_reason_still_fails_d(
    db_session: Session, sanctioned: list[str]
) -> None:
    record = (
        db_session.query(SalesNavCompanyEnrichment)
        .filter(SalesNavCompanyEnrichment.lookup_attempts == 2)
        .one()
    )
    record.rejected_candidates = [{"domain": "decoy.example", "rank": 2, "confidence": None}]
    db_session.flush()

    assert "D" in _run(db_session, sanctioned).failed


def test_invented_email_still_fails_f(db_session: Session, sanctioned: list[str]) -> None:
    contact = db_session.query(Contact).filter(Contact.first_name == "Morgan").one()
    contact.email = "morgan@" + DOMAIN
    db_session.flush()

    assert "F" in _run(db_session, sanctioned).failed


def test_missing_sanctioned_capture_is_reported_as_no_rows(db_session: Session) -> None:
    """Pointing the harness at a capture that does not exist must not silently pass."""

    result = _run(db_session, [str(uuid.uuid4())])

    assert not result.passed
    assert "A" in result.empty


def test_sanctioned_capture_ids_are_the_documented_fixture_captures() -> None:
    """The default scope is the two acceptance captures, not "every capture"."""

    assert harness.DEFAULT_SANCTIONED_CAPTURES == (
        "1b9ea638-12d5-4066-b391-6faedb31d21a",
        "737dc59a-af6e-4474-803e-951d2ce8c1d9",
    )
    assert harness.EXPECTED_DATABASE == "vmr_dat014"
    # Every graded check must be scoped; only informational ones may be global.
    for check in harness.build_checks():
        if check.graded:
            assert ":captures" in check.sql, f"graded check {check.key} is not scoped"

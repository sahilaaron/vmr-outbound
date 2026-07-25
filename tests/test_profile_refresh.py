"""Exact-URL contact refresh from profile snapshots (DAT-012E).

Proves the absolute identity rule (exact normalized LinkedIn URL only), the
DAT-005 freshness integration (older evidence never replaces newer; manual
overrides win), DAT-006 suppression authority, review-only weak matching, and
truthful outcomes — against a live Postgres.
"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.models.contact import Contact
from app.models.enums import (
    LinkedInSnapshotOutcome,
    SuppressionReason,
    SuppressionType,
)
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.services.imports.linkedin_profile_intake import stage_profile_snapshot
from app.services.imports.normalization import normalize_linkedin_profile_url
from app.services.profiles.refresh import reconcile_snapshot
from app.services.provenance.service import set_manual_override
from app.services.suppressions import add_suppression, find_active_suppression
from sqlalchemy import func, select
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PAYLOAD = json.loads(
    (
        REPO_ROOT
        / "extensions"
        / "salesnav-capture"
        / "docs"
        / "fixtures"
        / "profile.payload.example.json"
    ).read_text("utf-8")
)
# The example fixture's profile URL slug (see profile.payload.example.json).
SNAPSHOT_URL = "https://www.linkedin.com/in/morgan-vale"


def _make_contact(
    db: Session,
    *,
    first: str = "Morgan",
    last: str = "Vale",
    company: str = "Meridian Works",
    title: str | None = "Operations Manager",
    linkedin_url: str | None = "https://www.linkedin.com/in/Morgan-Vale?utm_source=share",
    email: str | None = None,
) -> Contact:
    contact = Contact(
        first_name=first,
        last_name=last,
        company_name=company,
        company_domain="example.test",
        title=title,
        linkedin_url=linkedin_url,
        email=email,
        natural_key=f"{first.casefold()}|{last.casefold()}|example.test",
    )
    db.add(contact)
    db.flush()
    return contact


def _stage(db: Session, *, captured_at: str | None = None) -> LinkedInProfileSnapshot:
    payload = copy.deepcopy(EXAMPLE_PAYLOAD)
    payload["client_capture_id"] = str(uuid.uuid4())
    payload["campaign_id"] = None
    if captured_at is not None:
        payload["captured_at"] = captured_at
        payload["profile"]["observed_at"] = captured_at
        for e in payload["experiences"]:
            e["observed_at"] = captured_at
    result = stage_profile_snapshot(db, payload=payload, operator_base_url="http://127.0.0.1:8000")
    snapshot = db.get(LinkedInProfileSnapshot, uuid.UUID(result.snapshot_id))
    assert snapshot is not None
    return snapshot


# --- Exact matching ----------------------------------------------------------


def test_exact_url_match_refreshes_the_right_contact(db_session: Session) -> None:
    # The stored contact URL differs in case, host form, and query string —
    # the SHARED normalization still makes it the same exact identity.
    contact = _make_contact(db_session)
    decoy = _make_contact(
        db_session,
        first="Morgan",
        last="Vale",
        company="Different Corp",
        linkedin_url="https://www.linkedin.com/in/a-different-person",
    )
    snapshot = _stage(db_session)

    result = reconcile_snapshot(db_session, snapshot)

    assert result.outcome == LinkedInSnapshotOutcome.EXACT_MATCH_REFRESHED
    assert result.matched_contact_id == str(contact.id)
    assert snapshot.matched_contact_id == contact.id
    # Title refreshed from the snapshot's current role.
    assert contact.title == "Director of Operations"
    assert "title" in result.refreshed_fields
    # The decoy (same name!) was never touched: name is never an identity.
    assert decoy.title == "Operations Manager"
    assert snapshot.refresh_summary is not None
    assert snapshot.reconciled_at is not None


def test_unchanged_recapture_is_idempotent(db_session: Session) -> None:
    _make_contact(db_session)
    first = reconcile_snapshot(db_session, _stage(db_session))
    assert first.outcome == LinkedInSnapshotOutcome.EXACT_MATCH_REFRESHED

    second = reconcile_snapshot(db_session, _stage(db_session))
    assert second.outcome == LinkedInSnapshotOutcome.EXACT_MATCH_UNCHANGED
    assert second.refreshed_fields == []
    assert set(second.unchanged_fields) >= {"title", "company_name"}


def test_older_evidence_cannot_replace_newer(db_session: Session) -> None:
    contact = _make_contact(db_session)
    # Newer capture first.
    newer = _stage(db_session, captured_at="2026-07-24T10:00:00.000Z")
    reconcile_snapshot(db_session, newer)
    title_after_newer = contact.title
    assert title_after_newer == "Director of Operations"

    # A manual override outranks every observation.
    set_manual_override(
        db_session,
        contact=contact,
        field_name="title",
        value="Handpicked Title",
        actor="operator",
        reason="manual correction",
    )
    assert contact.title == "Handpicked Title"

    # An OLDER capture then arrives: it records evidence but changes nothing.
    older = _stage(db_session, captured_at="2020-01-01T10:00:00.000Z")
    result = reconcile_snapshot(db_session, older)
    assert result.outcome == LinkedInSnapshotOutcome.EXACT_MATCH_UNCHANGED
    assert contact.title == "Handpicked Title"


# --- Suppression -------------------------------------------------------------


def test_suppressed_contact_records_evidence_but_never_refreshes(
    db_session: Session,
) -> None:
    contact = _make_contact(db_session, email="morgan@example.test")
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="morgan@example.test",
        reason=SuppressionReason.OPT_OUT,
    )
    snapshot = _stage(db_session)
    result = reconcile_snapshot(db_session, snapshot)

    assert result.outcome == LinkedInSnapshotOutcome.SUPPRESSED
    assert result.suppression_reason == "email opt_out"
    # Evidence is linked; canonical fields untouched.
    assert snapshot.matched_contact_id == contact.id
    assert contact.title == "Operations Manager"
    assert result.refreshed_fields == []
    # The suppression itself is untouched (still active, same reason).
    still = find_active_suppression(db_session, email="morgan@example.test", domain="example.test")
    assert still is not None and still.reason == SuppressionReason.OPT_OUT


# --- Weak matching is review-only ---------------------------------------------


def test_name_only_matches_never_merge_automatically(db_session: Session) -> None:
    # Same person name + same company, but NO linkedin_url on the contact:
    # must NOT auto-merge; it becomes a review candidate only.
    contact = _make_contact(db_session, linkedin_url=None)
    snapshot = _stage(db_session)
    result = reconcile_snapshot(db_session, snapshot)

    assert result.outcome == LinkedInSnapshotOutcome.UNMATCHED_STAGED
    assert result.matched_contact_id is None
    assert snapshot.matched_contact_id is None
    assert contact.title == "Operations Manager"  # untouched
    assert len(result.review_candidates) == 1
    candidate = result.review_candidates[0]
    assert candidate["contact_id"] == str(contact.id)
    assert "name" in candidate["match_basis"]
    assert "name_company" in candidate["match_basis"]
    assert candidate["auto_merge"] is False
    # No contact rows were created either.
    assert db_session.scalar(select(func.count()).select_from(Contact)) == 1


def test_fuzzy_url_resemblance_is_not_a_match(db_session: Session) -> None:
    _make_contact(db_session, linkedin_url="https://www.linkedin.com/in/morgan-vale-2")
    snapshot = _stage(db_session)
    result = reconcile_snapshot(db_session, snapshot)
    assert result.outcome == LinkedInSnapshotOutcome.UNMATCHED_STAGED
    assert result.matched_contact_id is None


def test_ambiguous_shared_url_goes_to_review(db_session: Session) -> None:
    a = _make_contact(db_session)
    b = _make_contact(
        db_session,
        first="M.",
        last="Vale",
        linkedin_url="https://linkedin.com/in/morgan-vale/",
        email="other@example.test",
    )
    snapshot = _stage(db_session)
    result = reconcile_snapshot(db_session, snapshot)

    assert result.outcome == LinkedInSnapshotOutcome.AMBIGUOUS_REVIEW
    ids = {c["contact_id"] for c in result.review_candidates}
    assert ids == {str(a.id), str(b.id)}
    # Nothing merged, nothing refreshed on either contact.
    assert a.title == "Operations Manager"
    assert b.title == "Operations Manager"


# --- No downstream side effects -----------------------------------------------


def test_refresh_never_creates_outreach_side_effects(db_session: Session) -> None:
    """A refresh outcome changes contact FIELDS only — no verification, no
    suppression change, no approval, no schedule, no campaign membership."""

    from app.models.campaign import CampaignContact
    from app.models.draft import DraftVersion
    from app.models.suppression import Suppression
    from app.models.verification_job import VerificationJob

    _make_contact(db_session)
    reconcile_snapshot(db_session, _stage(db_session))

    for model in (VerificationJob, DraftVersion, CampaignContact, Suppression):
        assert db_session.scalar(select(func.count()).select_from(model)) == 0


def test_contact_side_normalization_is_shared(db_session: Session) -> None:
    """The same normalizer produces the same key for every URL variant a
    contact may have arrived with (CSV import, SalesNav, manual entry)."""

    variants = [
        "https://www.linkedin.com/in/Morgan-Vale/",
        "http://linkedin.com/in/morgan-vale",
        "www.linkedin.com/in/morgan-vale?trk=people-search",
        "linkedin.com/in/MORGAN-VALE",
    ]
    for v in variants:
        assert normalize_linkedin_profile_url(v) == SNAPSHOT_URL
    # And non-profile URLs can never produce a profile identity.
    assert normalize_linkedin_profile_url("https://www.linkedin.com/company/morgan-vale") is None
    assert normalize_linkedin_profile_url("https://example.com/in/morgan-vale") is None


def test_snapshot_datetime_normalization(db_session: Session) -> None:
    _make_contact(db_session)
    snapshot = _stage(db_session, captured_at="2026-07-24T10:00:00.000Z")
    reconcile_snapshot(db_session, snapshot)
    assert snapshot.reconciled_at is not None
    assert snapshot.reconciled_at.astimezone(UTC) <= datetime.now(UTC)


def test_stage_with_reconcile_returns_truthful_outcome_and_qa(db_session: Session) -> None:
    """DAT-012E wired into intake: with reconcile enabled, the staging response
    reports the reconciliation outcome and a QA evaluation exists for the match."""

    from app.models.qa_evaluation import ContactQAEvaluation

    contact = _make_contact(db_session)
    payload = copy.deepcopy(EXAMPLE_PAYLOAD)
    payload["client_capture_id"] = str(uuid.uuid4())
    payload["campaign_id"] = None
    result = stage_profile_snapshot(
        db_session,
        payload=payload,
        operator_base_url="http://127.0.0.1:8000",
        reconcile=True,
    )
    assert result.outcome == "exact_match_refreshed"
    assert contact.title == "Director of Operations"
    evaluations = db_session.scalars(select(ContactQAEvaluation)).all()
    assert len(evaluations) == 1
    assert evaluations[0].contact_id == contact.id

    # Idempotent replay reports the SAME stored outcome without re-evaluating.
    replay = stage_profile_snapshot(
        db_session,
        payload=payload,
        operator_base_url="http://127.0.0.1:8000",
        reconcile=True,
    )
    assert replay.already_received is True
    assert replay.outcome == "exact_match_refreshed"
    assert len(db_session.scalars(select(ContactQAEvaluation)).all()) == 1

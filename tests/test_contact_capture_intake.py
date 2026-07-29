"""Contact-first capture intake tests (DAT-013).

Exercises the real ``POST /api/intake/contact-captures`` route and the capture
service against a live Postgres, using the extension's committed contract schema
and example payloads as the source of truth.

The guarantees under test are the product ones: no campaign is ever required,
one submission persists permanent per-person evidence and a permanent Contact,
only an exact LinkedIn identifier may refresh an existing contact, ambiguity and
suppression stay authoritative, optional Campaign filing is isolated and
idempotent, labels and notes are optional and append-only, and capture alone
never makes a contact outreach-eligible.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.audit_event import AuditEvent
from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.contact_capture import (
    ContactCaptureNote,
    ContactCaptureSubmission,
    ContactLabel,
    ContactLabelAssignment,
)
from app.models.enums import (
    CampaignStatus,
    CaptureCampaignFilingStatus,
    LinkedInSnapshotOutcome,
    SuppressionReason,
    SuppressionType,
)
from app.models.linkedin_profile import (
    LinkedInProfileExperienceObservation,
    LinkedInProfileSnapshot,
)
from app.models.pipeline import CaptureCampaignFiling
from app.models.qa_evaluation import ContactQAEvaluation
from app.services.captures import intake as cc
from app.services.captures import labels as labels_service
from app.services.suppressions import add_suppression
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = REPO_ROOT / "extensions" / "salesnav-capture" / "docs"
PROFILE_SUBMISSION = json.loads(
    (CONTRACT_DIR / "fixtures" / "contact-capture.profile.example.json").read_text("utf-8")
)
SALESNAV_SUBMISSION = json.loads(
    (CONTRACT_DIR / "fixtures" / "contact-capture.salesnav.example.json").read_text("utf-8")
)
LEGACY_PROFILE_PAYLOAD = json.loads(
    (CONTRACT_DIR / "fixtures" / "profile.payload.example.json").read_text("utf-8")
)

INTAKE_URL = "/api/intake/contact-captures"
LABELS_URL = "/api/contact-labels"
LOOKUP_URL = "/api/contacts/lookup"
LOOPBACK_ORIGIN = "http://127.0.0.1:8000"
EXTENSION_ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
PROFILE_URL = "https://www.linkedin.com/in/morgan-vale"


# --- Fixtures ----------------------------------------------------------------


@pytest.fixture()
def enable_contact_capture(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_INTAKE", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture()
def client(db_session: Session) -> Iterator[TestClient]:
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _fresh(base: dict) -> dict:
    """A deep copy of a fixture submission with fresh client-minted ids."""

    payload = copy.deepcopy(base)
    payload["client_submission_id"] = str(uuid.uuid4())
    for capture in payload["contacts"]:
        capture["client_capture_id"] = str(uuid.uuid4())
    return payload


def _seed_contact(
    db: Session,
    *,
    first: str = "Morgan",
    last: str = "Vale",
    company: str = "Meridian Works",
    domain: str = "meridianworks.example",
    title: str | None = "Operations Manager",
    linkedin_url: str | None = "https://www.LinkedIn.com/in/Morgan-Vale/?trk=search",
    email: str | None = None,
) -> Contact:
    contact = Contact(
        first_name=first,
        last_name=last,
        company_name=company,
        company_domain=domain,
        title=title,
        linkedin_url=linkedin_url,
        email=email,
        natural_key=f"{first.casefold()}|{last.casefold()}|{domain}",
    )
    db.add(contact)
    db.flush()
    return contact


def _snapshot_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(LinkedInProfileSnapshot)) or 0


def _contact_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Contact)) or 0


def _post(client: TestClient, payload: dict) -> object:
    return client.post(INTAKE_URL, json=payload, headers={"Origin": EXTENSION_ORIGIN})


# --- Feature gate and boundary guards ----------------------------------------


def test_endpoint_is_absent_until_the_feature_is_enabled(client: TestClient) -> None:
    response = _post(client, _fresh(PROFILE_SUBMISSION))
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_non_local_environment_is_refused(
    client: TestClient, enable_contact_capture: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    get_settings.cache_clear()
    try:
        response = _post(client, _fresh(PROFILE_SUBMISSION))
    finally:
        get_settings.cache_clear()
    assert response.status_code == 403
    assert response.json()["error"] == "unauthorized"


def test_remote_origin_is_refused(client: TestClient, enable_contact_capture: None) -> None:
    response = client.post(
        INTAKE_URL, json=_fresh(PROFILE_SUBMISSION), headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403


def test_preflight_reflects_a_loopback_origin(
    client: TestClient, enable_contact_capture: None
) -> None:
    response = client.options(INTAKE_URL, headers={"Origin": LOOPBACK_ORIGIN})
    assert response.status_code == 204
    assert response.headers["Access-Control-Allow-Origin"] == LOOPBACK_ORIGIN


def test_malformed_json_is_a_deterministic_400(
    client: TestClient, enable_contact_capture: None
) -> None:
    response = client.post(
        INTAKE_URL,
        content=b"{not json",
        headers={"Origin": EXTENSION_ORIGIN, "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_json"


def test_oversized_body_is_rejected_before_parsing(
    client: TestClient, enable_contact_capture: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONTACT_CAPTURE_INTAKE_MAX_BYTES", "512")
    get_settings.cache_clear()
    try:
        response = _post(client, _fresh(PROFILE_SUBMISSION))
    finally:
        get_settings.cache_clear()
    assert response.status_code == 413
    assert response.json()["error"] == "payload_too_large"


# --- The contact-first contract ----------------------------------------------


def test_no_campaign_is_required(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    """Acquisition creates the permanent Contact without Campaign context."""

    payload = _fresh(PROFILE_SUBMISSION)
    payload["campaign_id"] = None
    response = _post(client, payload)
    assert response.status_code == 201, response.text
    snapshot = db_session.scalars(select(LinkedInProfileSnapshot)).one()
    assert snapshot.campaign_id is None
    assert snapshot.capture_mode == cc.CAPTURE_MODE_PROFILE
    assert _contact_count(db_session) == 1
    assert db_session.scalar(select(func.count()).select_from(CampaignContact)) == 0


def test_campaign_selection_adds_one_idempotent_campaign_contact(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    campaign = Campaign(name="Optional capture filing", status=CampaignStatus.DRAFT)
    db_session.add(campaign)
    db_session.flush()
    payload = _fresh(PROFILE_SUBMISSION)
    payload["campaign_id"] = str(campaign.id)

    response = _post(client, payload)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["counts"]["created"] == 1
    assert body["counts"]["campaign_filings_applied"] == 1
    membership = db_session.scalars(select(CampaignContact)).one()
    contact = db_session.scalars(select(Contact)).one()
    assert membership.campaign_id == campaign.id
    assert membership.contact_id == contact.id
    filing = db_session.scalars(select(CaptureCampaignFiling)).one()
    assert filing.status is CaptureCampaignFilingStatus.APPLIED
    assert filing.campaign_contact_id == membership.id

    replay = _post(client, payload)
    assert replay.status_code == 200
    assert replay.json()["already_received"] is True
    assert db_session.scalar(select(func.count()).select_from(CampaignContact)) == 1


def test_malformed_campaign_id_is_rejected(
    client: TestClient, enable_contact_capture: None
) -> None:
    payload = _fresh(PROFILE_SUBMISSION)
    payload["campaign_id"] = "camp_demo_001"
    response = _post(client, payload)
    assert response.status_code == 422
    assert response.json()["error"] == "validation_failed"


def test_unknown_campaign_filing_fails_without_losing_the_contact(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    payload = _fresh(PROFILE_SUBMISSION)
    payload["campaign_id"] = str(uuid.uuid4())
    response = _post(client, payload)
    assert response.status_code == 201
    body = response.json()
    assert body["counts"]["created"] == 1
    assert body["counts"]["campaign_filings_failed"] == 1
    assert body["results"][0]["campaign_filing"]["error_code"] == "campaign_not_found"
    assert _contact_count(db_session) == 1
    assert db_session.scalar(select(func.count()).select_from(CampaignContact)) == 0


def test_legacy_contract_is_refused_with_a_pointer_to_its_own_route(
    client: TestClient, enable_contact_capture: None
) -> None:
    """A v1 body is never silently reinterpreted as a contact-first submission."""

    response = _post(client, copy.deepcopy(LEGACY_PROFILE_PAYLOAD))
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "unsupported_contract"
    assert any("/api/intake/linkedin-profile/stage" in detail for detail in body["details"])


def test_unsupported_major_version_is_refused(
    client: TestClient, enable_contact_capture: None
) -> None:
    payload = _fresh(PROFILE_SUBMISSION)
    payload["schema_version"] = "linkedin-contact-capture/3.0.0"
    response = _post(client, payload)
    assert response.status_code == 422
    assert response.json()["error"] == "unsupported_contract"


def test_capture_with_no_identity_signal_is_refused(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    payload = _fresh(PROFILE_SUBMISSION)
    person = payload["contacts"][0]["person"]
    person["linkedin_profile_url"] = None
    person["salesnav_lead_url"] = None
    person["full_name"] = None
    response = _post(client, payload)
    assert response.status_code == 422
    assert _snapshot_count(db_session) == 0


def test_malformed_profile_url_fails_the_contract_pattern(
    client: TestClient, enable_contact_capture: None
) -> None:
    payload = _fresh(PROFILE_SUBMISSION)
    payload["contacts"][0]["person"]["linkedin_profile_url"] = "https://linkedin.com.evil/in/x"
    response = _post(client, payload)
    assert response.status_code == 422


# --- Saving a person ----------------------------------------------------------


def test_manually_opened_profile_is_saved_as_permanent_capture_evidence(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    response = _post(client, _fresh(PROFILE_SUBMISSION))
    assert response.status_code == 201
    body = response.json()

    assert body["already_received"] is False
    assert body["counts"]["submitted"] == 1
    assert body["counts"]["staged_unmatched"] == 0
    assert body["counts"]["created"] == 1
    assert _contact_count(db_session) == 1

    snapshot = db_session.scalars(select(LinkedInProfileSnapshot)).one()
    contact = db_session.scalars(select(Contact)).one()
    assert snapshot.outcome is LinkedInSnapshotOutcome.CONTACT_CREATED
    assert snapshot.matched_contact_id == contact.id
    assert contact.first_name == "Morgan"
    assert contact.last_name == "Vale"
    assert contact.company_domain is None
    assert contact.natural_key is None
    assert snapshot.normalized_profile_url == PROFILE_URL
    assert snapshot.payload["person"]["full_name"] == "Morgan Vale"
    assert snapshot.profile_fields["about_text"].startswith("Operations leader")

    experiences = db_session.scalars(select(LinkedInProfileExperienceObservation)).all()
    assert [e.position_index for e in experiences] == [1, 2]

    result = body["results"][0]
    assert result["capture_url"].endswith(f"/contact-captures/{snapshot.id}")
    assert result["contact_url"].endswith(f"/contacts/{contact.id}")
    assert result["outcome"] == "created"


def test_salesnav_rows_are_saved_without_a_campaign_and_keep_uncertain_identity(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    response = _post(client, _fresh(SALESNAV_SUBMISSION))
    assert response.status_code == 201
    body = response.json()
    assert body["counts"]["submitted"] == 2
    assert body["counts"]["created"] == 2
    assert body["counts"]["staged_unmatched"] == 0
    assert _contact_count(db_session) == 2

    snapshots = {
        s.payload["person"]["full_name"]: s
        for s in db_session.scalars(select(LinkedInProfileSnapshot))
    }
    no_url = snapshots["Dana Whitfield"]
    with_url = snapshots["\u5927\u89d2 \u77e5\u4e5f"]
    # A Sales Navigator lead URL is context, never a canonical identity.
    assert no_url.normalized_profile_url is None
    assert no_url.salesnav_lead_url == "https://www.linkedin.com/sales/lead/ACwAAAB1x9k"
    assert "no canonical LinkedIn profile URL" in no_url.refresh_summary["skipped_fields"]["*"]
    assert with_url.normalized_profile_url == "https://www.linkedin.com/in/tomoya-okaku"
    assert all(s.capture_mode == cc.CAPTURE_MODE_SALESNAV for s in snapshots.values())


def test_operator_can_exclude_rows_by_simply_not_submitting_them(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    """Exclusion is enforced by the contract: only included rows are ever sent."""

    payload = _fresh(SALESNAV_SUBMISSION)
    payload["contacts"] = payload["contacts"][:1]
    response = _post(client, payload)
    assert response.status_code == 201
    assert response.json()["counts"]["submitted"] == 1
    assert _snapshot_count(db_session) == 1


# --- Identity -----------------------------------------------------------------


def test_exact_url_match_refreshes_the_right_contact_only(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    target = _seed_contact(db_session)
    decoy = _seed_contact(db_session, domain="other.example", linkedin_url=None)

    response = _post(client, _fresh(PROFILE_SUBMISSION))
    assert response.status_code == 201
    body = response.json()
    assert body["counts"]["refreshed_exact_match"] == 1

    db_session.refresh(target)
    db_session.refresh(decoy)
    assert target.title == "Director of Operations"
    assert decoy.title == "Operations Manager"
    result = body["results"][0]
    assert result["matched_contact_id"] == str(target.id)
    assert result["contact_url"].endswith(f"/contacts/{target.id}")


def test_older_evidence_cannot_replace_newer(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    _seed_contact(db_session)
    _post(client, _fresh(PROFILE_SUBMISSION))

    stale = _fresh(PROFILE_SUBMISSION)
    old = (datetime.now(UTC) - timedelta(days=2000)).isoformat()
    stale["contacts"][0]["captured_at"] = old
    stale["contacts"][0]["current_employment_hint"]["title"] = "Intern"
    stale["contacts"][0]["experience_observations"][0]["job_title"] = "Intern"
    response = _post(client, stale)

    assert response.json()["counts"]["exact_match_unchanged"] == 1
    contact = db_session.scalars(select(Contact)).first()
    assert contact is not None
    assert contact.title == "Director of Operations"


def test_two_contacts_on_one_url_stay_ambiguous_and_nothing_merges(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    _seed_contact(db_session)
    _seed_contact(db_session, domain="second.example")
    before = _contact_count(db_session)

    response = _post(client, _fresh(PROFILE_SUBMISSION))
    body = response.json()
    assert body["counts"]["staged_ambiguous"] == 1
    assert body["results"][0]["review_candidate_count"] == 2
    assert _contact_count(db_session) == before
    assert all(c.merged_into_id is None for c in db_session.scalars(select(Contact)))


def test_weak_name_match_is_review_only(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    _seed_contact(db_session, linkedin_url=None)
    response = _post(client, _fresh(PROFILE_SUBMISSION))
    body = response.json()
    assert body["counts"]["created"] == 1
    snapshot = db_session.scalars(select(LinkedInProfileSnapshot)).one()
    assert snapshot.review_candidates
    assert all(c["auto_merge"] is False for c in snapshot.review_candidates)
    # A second permanent Contact represents the captured person, but the weak
    # candidate is never silently merged into it.
    assert _contact_count(db_session) == 2


def test_duplicate_person_in_one_submission_is_marked_and_reconciled_once(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    target = _seed_contact(db_session)
    payload = _fresh(PROFILE_SUBMISSION)
    second = copy.deepcopy(payload["contacts"][0])
    second["client_capture_id"] = str(uuid.uuid4())
    payload["contacts"].append(second)

    body = _post(client, payload).json()
    assert body["counts"]["refreshed_exact_match"] == 1
    assert body["counts"]["duplicate_in_submission"] == 1

    first_id = payload["contacts"][0]["client_capture_id"]
    second_id = second["client_capture_id"]
    by_id = {s.client_capture_id: s for s in db_session.scalars(select(LinkedInProfileSnapshot))}
    assert by_id[second_id].outcome is LinkedInSnapshotOutcome.DUPLICATE_IN_SUBMISSION
    assert by_id[second_id].duplicate_of_id == by_id[first_id].id
    # The duplicate's evidence is preserved, not dropped.
    assert by_id[second_id].payload["person"]["full_name"] == "Morgan Vale"
    assert by_id[second_id].matched_contact_id == target.id
    assert str(target.id) == body["results"][0]["matched_contact_id"]


def test_repeated_capture_id_within_one_submission_is_refused(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    payload = _fresh(PROFILE_SUBMISSION)
    payload["contacts"].append(copy.deepcopy(payload["contacts"][0]))
    response = _post(client, payload)
    assert response.status_code == 422
    assert _snapshot_count(db_session) == 0


# --- Suppression --------------------------------------------------------------


def test_suppression_remains_authoritative(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    contact = _seed_contact(db_session, email="morgan@meridianworks.example")
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="morgan@meridianworks.example",
        reason=SuppressionReason.OPT_OUT,
        source="test",
    )
    payload = _fresh(PROFILE_SUBMISSION)
    body = _post(client, payload).json()

    assert body["counts"]["suppressed"] == 1
    db_session.refresh(contact)
    assert contact.title == "Operations Manager"  # untouched
    # A suppressed contact is never labelled by a capture either.
    assert db_session.scalar(select(func.count()).select_from(ContactLabelAssignment)) == 0
    snapshot = db_session.scalars(select(LinkedInProfileSnapshot)).one()
    assert snapshot.matched_contact_id == contact.id  # evidence still linked


def test_capture_never_makes_a_contact_outreach_eligible(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    contact = _seed_contact(db_session)
    _post(client, _fresh(PROFILE_SUBMISSION))
    db_session.refresh(contact)
    assert contact.email is None
    # No email candidate, verification, draft, approval, or campaign membership.
    from app.models.campaign import CampaignContact
    from app.models.draft import DraftVersion
    from app.models.email_candidate import EmailCandidate

    for model in (EmailCandidate, DraftVersion, CampaignContact):
        assert db_session.scalar(select(func.count()).select_from(model)) == 0


# --- Labels and notes ---------------------------------------------------------


def test_labels_are_optional(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    payload = _fresh(PROFILE_SUBMISSION)
    payload["operator_metadata"] = {"labels": [], "note": None}
    response = _post(client, payload)
    assert response.status_code == 201
    assert db_session.scalar(select(func.count()).select_from(ContactLabel)) == 0
    assert db_session.scalar(select(func.count()).select_from(ContactCaptureNote)) == 0


def test_labels_are_created_once_and_applied_to_a_matched_contact(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    contact = _seed_contact(db_session)
    body = _post(client, _fresh(PROFILE_SUBMISSION)).json()
    assert body["counts"]["labels_applied"] == 2
    assert sorted(body["results"][0]["labels_applied"]) == ["Healthcare", "Market Entry"]

    slugs = sorted(db_session.scalars(select(ContactLabel.slug).order_by(ContactLabel.slug)).all())
    assert slugs == ["healthcare", "market-entry"]
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(ContactLabelAssignment)
            .where(ContactLabelAssignment.contact_id == contact.id)
        )
        == 2
    )

    # A second submission with the same labels adds no duplicates.
    again = _post(client, _fresh(PROFILE_SUBMISSION)).json()
    assert again["counts"]["labels_applied"] == 0
    assert db_session.scalar(select(func.count()).select_from(ContactLabel)) == 2


def test_labels_on_a_new_contact_are_applied_and_preserved_on_capture(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    body = _post(client, _fresh(PROFILE_SUBMISSION)).json()
    assert body["counts"]["labels_applied"] == 2
    snapshot = db_session.scalars(select(LinkedInProfileSnapshot)).one()
    assert snapshot.operator_labels == ["Healthcare", "Market Entry"]
    contact = db_session.scalars(select(Contact)).one()
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(ContactLabelAssignment)
            .where(ContactLabelAssignment.contact_id == contact.id)
        )
        == 2
    )
    assert db_session.scalar(select(func.count()).select_from(ContactLabel)) == 2


def test_notes_are_append_only_across_a_refresh(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    _seed_contact(db_session)
    _post(client, _fresh(PROFILE_SUBMISSION))
    second = _fresh(PROFILE_SUBMISSION)
    second["operator_metadata"]["note"] = "Follow up after September."
    _post(client, second)

    notes = db_session.scalars(
        select(ContactCaptureNote).order_by(ContactCaptureNote.created_at)
    ).all()
    assert len(notes) == 2
    texts = {n.note_text for n in notes}
    assert "Follow up after September." in texts
    assert any("Met at SaaStr." in t for t in texts)


def test_per_contact_note_overrides_the_submission_note(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    payload = _fresh(SALESNAV_SUBMISSION)
    payload["contacts"][0]["operator_metadata"]["note"] = "Row-specific context."
    _post(client, payload)
    notes = {n.scope: n.note_text for n in db_session.scalars(select(ContactCaptureNote))}
    assert notes["contact"] == "Row-specific context."
    assert notes["submission"].startswith("Second page")


def test_label_slugging_collapses_spelling_variants() -> None:
    assert labels_service.slugify_label("Venture Capital") == "venture-capital"
    assert labels_service.slugify_label("venture  capital") == "venture-capital"
    assert labels_service.slugify_label("Venture-Capital!") == "venture-capital"
    assert labels_service.slugify_label("   ") is None
    assert labels_service.normalize_requested_labels(["A", "a", " a "]) == ["A"]


# --- Idempotency and failure --------------------------------------------------


def test_identical_retry_replays_the_original_outcome(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    _seed_contact(db_session)
    payload = _fresh(PROFILE_SUBMISSION)
    first = _post(client, payload)
    second = _post(client, payload)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["already_received"] is True
    assert second.json()["submission_id"] == first.json()["submission_id"]
    assert second.json()["counts"] == first.json()["counts"]
    assert _snapshot_count(db_session) == 1
    assert db_session.scalar(select(func.count()).select_from(ContactCaptureNote)) == 1


def test_reused_submission_id_with_changed_content_conflicts(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    payload = _fresh(PROFILE_SUBMISSION)
    _post(client, payload)
    changed = copy.deepcopy(payload)
    changed["operator_metadata"]["note"] = "different"
    response = _post(client, changed)
    assert response.status_code == 409
    assert response.json()["error"] == "client_submission_id_conflict"
    assert _snapshot_count(db_session) == 1


def test_capture_id_belonging_to_another_submission_conflicts(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    first = _fresh(PROFILE_SUBMISSION)
    _post(client, first)
    second = _fresh(PROFILE_SUBMISSION)
    second["contacts"][0]["client_capture_id"] = first["contacts"][0]["client_capture_id"]
    response = _post(client, second)
    assert response.status_code == 409
    assert response.json()["error"] == "client_capture_id_conflict"
    assert _snapshot_count(db_session) == 1


def test_a_mid_write_failure_leaves_nothing_behind(
    enable_contact_capture: None, db_session: Session
) -> None:
    def _boom() -> None:
        raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError):
        cc.stage_contact_captures(
            db_session,
            payload=_fresh(PROFILE_SUBMISSION),
            operator_base_url=LOOPBACK_ORIGIN,
            _fault=_boom,
        )
    assert _snapshot_count(db_session) == 0
    assert db_session.scalar(select(func.count()).select_from(ContactCaptureSubmission)) == 0


def test_timeout_rolls_the_whole_submission_back(
    enable_contact_capture: None, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks = iter([0.0] + [100.0] * 50)
    monkeypatch.setattr(cc, "_CLOCK_OVERRIDE", lambda: next(ticks))
    with pytest.raises(cc.IntakeTimeoutError):
        cc.stage_contact_captures(
            db_session,
            payload=_fresh(PROFILE_SUBMISSION),
            operator_base_url=LOOPBACK_ORIGIN,
            timeout_seconds=1.0,
        )
    assert _snapshot_count(db_session) == 0


def test_rejections_are_audited_without_leaking_captured_values(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    _post(client, copy.deepcopy(LEGACY_PROFILE_PAYLOAD))
    event = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == cc.FAILURE_AUDIT_ACTION)
    ).one()
    serialized = json.dumps(event.context)
    assert "Morgan" not in serialized
    assert "morgan-vale" not in serialized
    assert event.context["error_code"] == "unsupported_contract"


def test_success_is_audited(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    _post(client, _fresh(PROFILE_SUBMISSION))
    event = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == cc.SUCCESS_AUDIT_ACTION)
    ).one()
    assert event.context["capture_mode"] == cc.CAPTURE_MODE_PROFILE
    assert event.context["counts"]["submitted"] == 1
    assert "Morgan" not in json.dumps(event.context)


# --- QA policy ----------------------------------------------------------------


def test_matched_profile_capture_records_a_versioned_qa_evaluation(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    _seed_contact(db_session)
    _post(client, _fresh(PROFILE_SUBMISSION))
    evaluation = db_session.scalars(select(ContactQAEvaluation)).one()
    assert evaluation.policy_version
    assert evaluation.outcome is not None


# --- Companion read endpoints -------------------------------------------------


def test_label_list_endpoint_is_gated_and_minimal(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    _post(client, _fresh(PROFILE_SUBMISSION))
    response = client.get(LABELS_URL, headers={"Origin": EXTENSION_ORIGIN})
    assert response.status_code == 200
    assert response.json()["labels"] == [
        {"slug": "healthcare", "name": "Healthcare"},
        {"slug": "market-entry", "name": "Market Entry"},
    ]


def test_label_list_endpoint_is_absent_when_the_feature_is_off(client: TestClient) -> None:
    assert client.get(LABELS_URL, headers={"Origin": EXTENSION_ORIGIN}).status_code == 404


def test_lookup_reports_existence_only(
    client: TestClient, enable_contact_capture: None, db_session: Session
) -> None:
    response = client.get(
        LOOKUP_URL,
        params={"linkedin_profile_url": PROFILE_URL},
        headers={"Origin": EXTENSION_ORIGIN},
    )
    assert response.json() == {
        "match": "none",
        "contact_count": 0,
        "normalized_profile_url": PROFILE_URL,
    }

    _seed_contact(db_session)
    response = client.get(
        LOOKUP_URL,
        params={"linkedin_profile_url": PROFILE_URL},
        headers={"Origin": EXTENSION_ORIGIN},
    )
    body = response.json()
    assert body["match"] == "exact"
    assert body["contact_count"] == 1
    assert "Morgan" not in json.dumps(body)


def test_lookup_of_a_non_profile_url_is_unknown(
    client: TestClient, enable_contact_capture: None
) -> None:
    response = client.get(
        LOOKUP_URL,
        params={"linkedin_profile_url": "https://www.linkedin.com/company/meridian-works"},
        headers={"Origin": EXTENSION_ORIGIN},
    )
    assert response.json()["match"] == "unknown"


# --- Operator pages -----------------------------------------------------------


@pytest.fixture()
def workbench_client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A workbench-enabled app: the pages mount only when the switch is on."""

    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_INTAKE", "true")
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_capture_and_submission_pages_render(
    workbench_client: TestClient,
    db_session: Session,
) -> None:
    client = workbench_client
    body = _post(client, _fresh(PROFILE_SUBMISSION)).json()
    submission_id = body["submission_id"]
    capture_id = body["results"][0]["capture_id"]

    page = client.get(f"/contact-captures/{capture_id}")
    assert page.status_code == 200
    assert "Contact capture" in page.text
    assert "Healthcare" in page.text
    assert "Met at SaaStr." in page.text

    submission_page = client.get(f"/contact-captures/submissions/{submission_id}")
    assert submission_page.status_code == 200
    assert "created" in submission_page.text


def test_unknown_capture_page_is_a_clean_not_found(workbench_client: TestClient) -> None:
    response = workbench_client.get(f"/contact-captures/{uuid.uuid4()}")
    assert response.status_code == 404

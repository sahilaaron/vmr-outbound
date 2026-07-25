"""LinkedIn profile capture intake tests (DAT-012D).

Exercises the real ``POST /api/intake/linkedin-profile/stage`` route and the
snapshot service against a live Postgres, using the extension's committed
contract schema and example payload as the source of truth. Proves the hard
guarantees: an accepted capture persists exactly one immutable snapshot plus its
nested experience observations, retries are idempotent, invalid payloads fail
clearly, and zero contacts (or any downstream artifact) change.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.audit_event import AuditEvent
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CampaignStatus, LinkedInSnapshotOutcome
from app.models.linkedin_profile import (
    LinkedInProfileExperienceObservation,
    LinkedInProfileSnapshot,
)
from app.services.imports import linkedin_profile_intake as lpi
from app.services.imports.linkedin_profile_intake import (
    FAILURE_AUDIT_ACTION,
    SUCCESS_AUDIT_ACTION,
    IdempotencyConflictError,
    IntakeTimeoutError,
    ProfileIntakeError,
    ValidationFailedError,
    stage_profile_snapshot,
)
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = REPO_ROOT / "extensions" / "salesnav-capture" / "docs"
EXAMPLE_PAYLOAD = json.loads(
    (CONTRACT_DIR / "fixtures" / "profile.payload.example.json").read_text("utf-8")
)

INTAKE_URL = "/api/intake/linkedin-profile/stage"
LOOPBACK_ORIGIN = "http://127.0.0.1:8000"
EXTENSION_ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"


# --- Fixtures ----------------------------------------------------------------


@pytest.fixture()
def enable_profile_intake(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("FEATURES__LINKEDIN_PROFILE_INTAKE", "true")
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


def _payload(*, client_capture_id: str | None = None, campaign_id: str | None = None) -> dict:
    payload = copy.deepcopy(EXAMPLE_PAYLOAD)
    payload["client_capture_id"] = client_capture_id or str(uuid.uuid4())
    payload["campaign_id"] = campaign_id
    return payload


def _snapshot_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(LinkedInProfileSnapshot)) or 0


def _contact_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Contact)) or 0


# --- Route boundary guards ---------------------------------------------------


def test_endpoint_is_404_while_feature_disabled(client: TestClient) -> None:
    resp = client.post(INTAKE_URL, json=_payload(), headers={"origin": LOOPBACK_ORIGIN})
    assert resp.status_code == 404


def test_non_local_environment_is_refused(
    client: TestClient, enable_profile_intake: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    get_settings.cache_clear()
    try:
        resp = client.post(INTAKE_URL, json=_payload(), headers={"origin": LOOPBACK_ORIGIN})
    finally:
        get_settings.cache_clear()
    assert resp.status_code == 403
    assert resp.json()["error"] == "unauthorized"


def test_disallowed_origin_is_refused(client: TestClient, enable_profile_intake: None) -> None:
    resp = client.post(INTAKE_URL, json=_payload(), headers={"origin": "https://evil.example.com"})
    assert resp.status_code == 403


def test_oversized_body_is_rejected_before_parsing(
    client: TestClient, enable_profile_intake: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LINKEDIN_PROFILE_INTAKE_MAX_BYTES", "64")
    get_settings.cache_clear()
    try:
        resp = client.post(INTAKE_URL, json=_payload(), headers={"origin": LOOPBACK_ORIGIN})
    finally:
        get_settings.cache_clear()
    assert resp.status_code == 413
    assert resp.json()["error"] == "payload_too_large"


def test_invalid_json_is_a_400(client: TestClient, enable_profile_intake: None) -> None:
    resp = client.post(
        INTAKE_URL,
        content=b"not json{",
        headers={"origin": LOOPBACK_ORIGIN, "content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_json"


def test_preflight_reflects_allowed_origin(client: TestClient, enable_profile_intake: None) -> None:
    resp = client.options(INTAKE_URL, headers={"origin": EXTENSION_ORIGIN})
    assert resp.status_code == 204
    assert resp.headers["access-control-allow-origin"] == EXTENSION_ORIGIN


# --- Successful staging ------------------------------------------------------


def test_stage_persists_immutable_snapshot_and_nested_observations(
    client: TestClient, enable_profile_intake: None, db_session: Session
) -> None:
    payload = _payload()
    before_contacts = _contact_count(db_session)

    resp = client.post(INTAKE_URL, json=payload, headers={"origin": EXTENSION_ORIGIN})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["outcome"] == "stored"
    assert body["already_received"] is False
    assert body["client_capture_id"] == payload["client_capture_id"]
    assert body["operator_workbench_url"].endswith(f"/profiles/{body['snapshot_id']}")

    snapshot = db_session.get(LinkedInProfileSnapshot, uuid.UUID(body["snapshot_id"]))
    assert snapshot is not None
    # The submitted payload is stored verbatim (immutability of the raw capture).
    assert snapshot.payload == payload
    assert snapshot.outcome == LinkedInSnapshotOutcome.STORED
    assert snapshot.normalized_profile_url == "https://www.linkedin.com/in/morgan-vale"
    assert snapshot.public_identifier == payload["profile"]["public_identifier"]
    assert snapshot.extraction_status in ("ok", "partial")

    # Nested experience history is preserved as rows, never flattened.
    obs = list(
        db_session.scalars(
            select(LinkedInProfileExperienceObservation)
            .where(LinkedInProfileExperienceObservation.snapshot_id == snapshot.id)
            .order_by(LinkedInProfileExperienceObservation.position_index)
        )
    )
    assert len(obs) == len(payload["experiences"])
    assert obs[0].job_title == payload["experiences"][0]["job_title"]
    assert obs[0].is_current is True
    assert obs[0].start_year == payload["experiences"][0]["start_date"]["year"]

    # Zero canonical changes: no contact created or modified.
    assert _contact_count(db_session) == before_contacts

    # Success audit recorded with safe context only.
    audit = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == SUCCESS_AUDIT_ACTION)
    ).first()
    assert audit is not None
    assert audit.context["snapshot_id"] == body["snapshot_id"]
    assert "full_name" not in json.dumps(audit.context)


def test_retry_with_same_content_is_idempotent(
    client: TestClient, enable_profile_intake: None, db_session: Session
) -> None:
    payload = _payload()
    first = client.post(INTAKE_URL, json=payload, headers={"origin": EXTENSION_ORIGIN})
    assert first.status_code == 201
    second = client.post(INTAKE_URL, json=payload, headers={"origin": EXTENSION_ORIGIN})
    assert second.status_code == 200
    assert second.json()["already_received"] is True
    assert second.json()["snapshot_id"] == first.json()["snapshot_id"]
    assert _snapshot_count(db_session) == 1


def test_same_capture_id_with_changed_content_conflicts(
    client: TestClient, enable_profile_intake: None, db_session: Session
) -> None:
    payload = _payload()
    assert (
        client.post(INTAKE_URL, json=payload, headers={"origin": EXTENSION_ORIGIN}).status_code
        == 201
    )
    changed = copy.deepcopy(payload)
    changed["profile"]["headline"] = "A different headline"
    resp = client.post(INTAKE_URL, json=changed, headers={"origin": EXTENSION_ORIGIN})
    assert resp.status_code == 409
    assert resp.json()["error"] == "client_capture_id_conflict"
    assert _snapshot_count(db_session) == 1


# --- Validation --------------------------------------------------------------


def test_wrong_schema_version_major_is_rejected(
    client: TestClient, enable_profile_intake: None
) -> None:
    payload = _payload()
    payload["schema_version"] = "linkedin-profile-capture/2.0.0"
    resp = client.post(INTAKE_URL, json=payload, headers={"origin": EXTENSION_ORIGIN})
    assert resp.status_code == 422
    assert resp.json()["error"] == "validation_failed"


def test_schema_violations_fail_clearly_with_details(
    client: TestClient, enable_profile_intake: None, db_session: Session
) -> None:
    payload = _payload()
    del payload["profile"]["full_name"]
    payload["experiences"][0]["layout"] = "surprising"
    resp = client.post(INTAKE_URL, json=payload, headers={"origin": EXTENSION_ORIGIN})
    assert resp.status_code == 422
    details = resp.json()["details"]
    assert any("full_name" in d for d in details)
    assert any("layout" in d for d in details)
    assert _snapshot_count(db_session) == 0
    # Failure audit is PII-free.
    audit = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == FAILURE_AUDIT_ACTION)
    ).first()
    assert audit is not None
    assert "Morgan" not in json.dumps(audit.context)


def test_profile_url_must_normalize_to_a_main_profile(
    db_session: Session,
) -> None:
    payload = _payload()
    payload["profile"]["linkedin_profile_url"] = (
        "https://www.linkedin.com/in/morgan-vale/details/experience"
    )
    with pytest.raises(ValidationFailedError):
        stage_profile_snapshot(
            db_session, payload=payload, operator_base_url="http://127.0.0.1:8000"
        )


def test_unknown_campaign_is_refused_but_null_is_allowed(
    client: TestClient, enable_profile_intake: None, db_session: Session
) -> None:
    missing = client.post(
        INTAKE_URL,
        json=_payload(campaign_id=str(uuid.uuid4())),
        headers={"origin": EXTENSION_ORIGIN},
    )
    assert missing.status_code == 409
    assert missing.json()["error"] == "campaign_invalid"

    campaign = Campaign(name="Pilot", status=CampaignStatus.DRAFT)
    db_session.add(campaign)
    db_session.flush()
    ok = client.post(
        INTAKE_URL,
        json=_payload(campaign_id=str(campaign.id)),
        headers={"origin": EXTENSION_ORIGIN},
    )
    assert ok.status_code == 201
    snapshot = db_session.scalars(select(LinkedInProfileSnapshot)).first()
    assert snapshot is not None and snapshot.campaign_id == campaign.id


# --- Rollback / timeout ------------------------------------------------------


def test_mid_write_failure_rolls_back_to_zero_rows(db_session: Session) -> None:
    def _boom() -> None:
        raise RuntimeError("mid-write failure")

    with pytest.raises(RuntimeError):
        stage_profile_snapshot(
            db_session,
            payload=_payload(),
            operator_base_url="http://127.0.0.1:8000",
            _fault=_boom,
        )
    assert _snapshot_count(db_session) == 0
    assert (
        db_session.scalar(select(func.count()).select_from(LinkedInProfileExperienceObservation))
        == 0
    )


def test_deadline_breach_raises_timeout_and_persists_nothing(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = iter([0.0, 100.0, 100.0, 100.0, 100.0])
    monkeypatch.setattr(lpi, "_CLOCK_OVERRIDE", lambda: next(clock))
    with pytest.raises(IntakeTimeoutError):
        stage_profile_snapshot(
            db_session,
            payload=_payload(),
            operator_base_url="http://127.0.0.1:8000",
            timeout_seconds=1.0,
        )
    assert _snapshot_count(db_session) == 0


# --- Error surface completeness ---------------------------------------------


def test_error_bodies_are_typed_and_stable() -> None:
    for exc_cls, code, status in [
        (lpi.InvalidJsonError, "invalid_json", 400),
        (lpi.ValidationFailedError, "validation_failed", 422),
        (lpi.CampaignInvalidError, "campaign_invalid", 409),
        (IdempotencyConflictError, "client_capture_id_conflict", 409),
        (lpi.PayloadTooLargeError, "payload_too_large", 413),
        (lpi.UnauthorizedError, "unauthorized", 403),
        (IntakeTimeoutError, "timeout", 504),
    ]:
        exc: ProfileIntakeError = exc_cls("boom")
        assert exc.error_code == code
        assert exc.http_status == status
        assert exc.to_body()["error"] == code

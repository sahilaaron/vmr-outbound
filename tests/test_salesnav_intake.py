"""Sales Navigator capture intake staging tests (DAT-009).

Exercises the real ``POST /api/intake/sales-navigator/stage`` route and the
staging service against a live Postgres, using the extension's committed contract
fixtures/schema as the source of truth. Covers the successful staging path and
every deterministic negative path in the contract, and proves the two hard
guarantees: a staged batch creates only the batch and its immutable raw rows, and
zero contacts (or any downstream artifact) are created before operator
confirmation.
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
from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.enums import ImportBatchStatus, ImportSourceFormat
from app.models.import_batch import ImportBatch, ImportRow, ImportRowValidation
from app.models.provenance import ProvenanceRecord
from app.services.campaigns import create_campaign
from app.services.imports.salesnav_intake import (
    IdempotencyConflictError,
    SalesNavIntakeError,
    stage_salesnav_batch,
)
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = REPO_ROOT / "extensions" / "salesnav-capture" / "docs"
EXAMPLE_PAYLOAD = json.loads(
    (CONTRACT_DIR / "fixtures" / "payload.example.json").read_text("utf-8")
)

INTAKE_URL = "/api/intake/sales-navigator/stage"
LOOPBACK_ORIGIN = "http://127.0.0.1:8000"
EXTENSION_ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"


# --- Fixtures ----------------------------------------------------------------


@pytest.fixture()
def enable_salesnav_intake(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Enable the ``salesnav_intake`` feature switch for one test."""

    monkeypatch.setenv("FEATURES__SALESNAV_INTAKE", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture()
def client(db_session: Session) -> Iterator[TestClient]:
    """A TestClient whose DB dependency is the rolled-back test session."""

    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _payload(campaign_id: str | None, *, client_batch_id: str | None = None) -> dict:
    """A fresh, valid contract payload targeting ``campaign_id``."""

    payload = copy.deepcopy(EXAMPLE_PAYLOAD)
    payload["campaign_id"] = campaign_id
    payload["client_batch_id"] = client_batch_id or str(uuid.uuid4())
    return payload


def _make_campaign(db_session: Session, name: str) -> Campaign:
    campaign = create_campaign(db_session, name=name)
    db_session.flush()
    return campaign


def _post(client: TestClient, payload: dict, *, origin: str | None = LOOPBACK_ORIGIN):
    headers = {"content-type": "application/json"}
    if origin is not None:
        headers["origin"] = origin
    return client.post(INTAKE_URL, content=json.dumps(payload), headers=headers)


# --- Success + core guarantees ----------------------------------------------


@pytest.mark.usefixtures("enable_salesnav_intake")
def test_stage_success_creates_batch_and_raw_rows_only(
    client: TestClient, db_session: Session
) -> None:
    campaign = _make_campaign(db_session, "SN success")
    payload = _payload(str(campaign.id))

    resp = _post(client, payload)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["client_batch_id"] == payload["client_batch_id"]
    assert body["record_count"] == 2
    assert body["already_received"] is False
    assert body["warnings"] == []
    assert body["operator_workbench_url"].startswith("http://127.0.0.1:8000/imports/")
    assert body["staging_id"] in body["operator_workbench_url"]

    # Exactly one batch, correct source + status, provenance preserved verbatim.
    batch = db_session.scalars(select(ImportBatch)).one()
    assert batch.source_format == ImportSourceFormat.SALES_NAVIGATOR
    assert batch.status == ImportBatchStatus.PENDING
    assert batch.client_batch_id == payload["client_batch_id"]
    assert batch.total_rows == 2
    assert batch.contacts_created == 0
    assert batch.source_reference == payload["current_search_url"]
    assert batch.source_metadata["schema_version"] == "salesnav-capture/1.0.0"
    assert batch.source_metadata["extraction_metadata"] == payload["extraction_metadata"]

    # Immutable raw rows preserve every raw value, warning and unicode name.
    rows = db_session.scalars(
        select(ImportRow).where(ImportRow.batch_id == batch.id).order_by(ImportRow.row_number)
    ).all()
    assert [r.raw_data for r in rows] == payload["records"]
    assert rows[1].raw_data["rawFullName"] == "大角 知也"  # unicode preserved verbatim
    assert rows[0].raw_data["warnings"] == [
        {"code": "missing_field", "field": "linkedinProfileUrl"}
    ]


@pytest.mark.usefixtures("enable_salesnav_intake")
def test_no_contacts_or_downstream_artifacts_created(
    client: TestClient, db_session: Session
) -> None:
    campaign = _make_campaign(db_session, "SN no contacts")
    resp = _post(client, _payload(str(campaign.id)))
    assert resp.status_code == 201

    # Staging must produce zero contacts / memberships / validations / provenance.
    assert db_session.scalar(select(func.count(Contact.id))) == 0
    assert db_session.scalar(select(func.count(CampaignContact.id))) == 0
    assert db_session.scalar(select(func.count(ImportRowValidation.id))) == 0
    assert db_session.scalar(select(func.count(ProvenanceRecord.id))) == 0


@pytest.mark.usefixtures("enable_salesnav_intake")
def test_workbench_location_is_loopback_deep_link(client: TestClient, db_session: Session) -> None:
    campaign = _make_campaign(db_session, "SN workbench url")
    resp = _post(client, _payload(str(campaign.id)))
    body = resp.json()
    batch = db_session.scalars(select(ImportBatch)).one()
    assert body["operator_workbench_url"] == f"http://127.0.0.1:8000/imports/{batch.id}"


# --- Idempotency -------------------------------------------------------------


@pytest.mark.usefixtures("enable_salesnav_intake")
def test_duplicate_client_batch_id_is_idempotent(client: TestClient, db_session: Session) -> None:
    campaign = _make_campaign(db_session, "SN idempotent")
    payload = _payload(str(campaign.id))

    first = _post(client, payload)
    second = _post(client, payload)

    assert first.status_code == 201
    assert first.json()["already_received"] is False
    assert second.status_code == 200
    assert second.json()["already_received"] is True
    assert second.json()["staging_id"] == first.json()["staging_id"]

    # Only one batch and one set of rows exist after the retry.
    assert db_session.scalar(select(func.count(ImportBatch.id))) == 1
    assert db_session.scalar(select(func.count(ImportRow.id))) == 2


@pytest.mark.usefixtures("enable_salesnav_intake")
def test_reused_batch_id_with_changed_payload_conflicts(
    client: TestClient, db_session: Session
) -> None:
    campaign = _make_campaign(db_session, "SN conflict")
    payload = _payload(str(campaign.id))
    assert _post(client, payload).status_code == 201

    changed = copy.deepcopy(payload)  # same client_batch_id, different content
    changed["records"] = changed["records"][:1]
    changed["extraction_metadata"]["record_count"] = 1

    resp = _post(client, changed)
    assert resp.status_code == 409
    assert resp.json()["error"] == "client_batch_id_conflict"
    # Nothing new staged; the original batch is untouched.
    assert db_session.scalar(select(func.count(ImportBatch.id))) == 1
    assert db_session.scalar(select(func.count(ImportRow.id))) == 2


# --- Malformed / version / campaign ------------------------------------------


@pytest.mark.usefixtures("enable_salesnav_intake")
def test_invalid_json_returns_400(client: TestClient) -> None:
    resp = client.post(
        INTAKE_URL,
        content=b"{not valid json",
        headers={"content-type": "application/json", "origin": LOOPBACK_ORIGIN},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_json"


@pytest.mark.usefixtures("enable_salesnav_intake")
def test_unsupported_contract_version_returns_422(client: TestClient, db_session: Session) -> None:
    campaign = _make_campaign(db_session, "SN bad version")
    payload = _payload(str(campaign.id))
    payload["schema_version"] = "salesnav-capture/2.0.0"

    resp = _post(client, payload)
    assert resp.status_code == 422
    assert resp.json()["error"] == "validation_failed"
    assert any("2.0.0" in d for d in resp.json()["details"])
    assert db_session.scalar(select(func.count(ImportBatch.id))) == 0


@pytest.mark.usefixtures("enable_salesnav_intake")
def test_unknown_campaign_returns_409(client: TestClient, db_session: Session) -> None:
    resp = _post(client, _payload(str(uuid.uuid4())))
    assert resp.status_code == 409
    assert resp.json()["error"] == "campaign_invalid"
    assert db_session.scalar(select(func.count(ImportBatch.id))) == 0


@pytest.mark.usefixtures("enable_salesnav_intake")
def test_null_campaign_returns_409(client: TestClient) -> None:
    resp = _post(client, _payload(None))
    assert resp.status_code == 409
    assert resp.json()["error"] == "campaign_invalid"


@pytest.mark.usefixtures("enable_salesnav_intake")
def test_archived_campaign_is_unavailable_409(client: TestClient, db_session: Session) -> None:
    from app.models.enums import CampaignStatus

    campaign = _make_campaign(db_session, "SN archived")
    campaign.status = CampaignStatus.ARCHIVED
    db_session.flush()
    resp = _post(client, _payload(str(campaign.id)))
    assert resp.status_code == 409
    assert resp.json()["error"] == "campaign_invalid"


# --- Records validation ------------------------------------------------------


@pytest.mark.usefixtures("enable_salesnav_intake")
def test_records_with_null_fields_and_warnings_stage_ok(
    client: TestClient, db_session: Session
) -> None:
    """A partially complete record (nulls + warnings) is valid and is staged."""

    campaign = _make_campaign(db_session, "SN partial ok")
    payload = _payload(str(campaign.id))
    sparse = copy.deepcopy(payload["records"][0])
    for field in ("lastName", "title", "companyName", "location", "salesNavCompanyUrl"):
        sparse[field] = None
    sparse["warnings"] = [{"code": "missing_field", "field": "companyName"}]
    payload["records"] = [sparse]
    payload["extraction_metadata"]["record_count"] = 1

    resp = _post(client, payload)
    assert resp.status_code == 201
    row = db_session.scalars(select(ImportRow)).one()
    assert row.raw_data["warnings"] == [{"code": "missing_field", "field": "companyName"}]
    assert row.raw_data["companyName"] is None  # null preserved, never guessed


@pytest.mark.usefixtures("enable_salesnav_intake")
def test_schema_invalid_record_rejects_whole_batch(client: TestClient, db_session: Session) -> None:
    campaign = _make_campaign(db_session, "SN schema invalid")
    payload = _payload(str(campaign.id))
    payload["records"][1]["salesNavLeadUrl"] = 12345  # wrong type

    resp = _post(client, payload)
    assert resp.status_code == 422
    assert resp.json()["error"] == "validation_failed"
    assert any("salesNavLeadUrl" in d for d in resp.json()["details"])
    # Transactional: a single bad record stages nothing.
    assert db_session.scalar(select(func.count(ImportBatch.id))) == 0
    assert db_session.scalar(select(func.count(ImportRow.id))) == 0


@pytest.mark.usefixtures("enable_salesnav_intake")
def test_empty_record_is_rejected(client: TestClient, db_session: Session) -> None:
    campaign = _make_campaign(db_session, "SN empty record")
    payload = _payload(str(campaign.id))
    empty = copy.deepcopy(payload["records"][0])
    for field in ("firstName", "lastName", "rawFullName", "linkedinProfileUrl", "salesNavLeadUrl"):
        empty[field] = None
    payload["records"] = [empty]
    payload["extraction_metadata"]["record_count"] = 1

    resp = _post(client, payload)
    assert resp.status_code == 422
    assert any("empty record not allowed" in d for d in resp.json()["details"])


# --- Access control ----------------------------------------------------------


def test_feature_disabled_returns_404(client: TestClient, db_session: Session) -> None:
    campaign = _make_campaign(db_session, "SN disabled")
    resp = _post(client, _payload(str(campaign.id)))
    assert resp.status_code == 404


@pytest.mark.usefixtures("enable_salesnav_intake")
def test_disallowed_origin_returns_403(client: TestClient, db_session: Session) -> None:
    campaign = _make_campaign(db_session, "SN bad origin")
    resp = _post(client, _payload(str(campaign.id)), origin="https://evil.example")
    assert resp.status_code == 403
    assert resp.json()["error"] == "unauthorized"
    assert db_session.scalar(select(func.count(ImportBatch.id))) == 0


@pytest.mark.usefixtures("enable_salesnav_intake")
def test_extension_origin_is_allowed(client: TestClient, db_session: Session) -> None:
    campaign = _make_campaign(db_session, "SN ext origin")
    resp = _post(client, _payload(str(campaign.id)), origin=EXTENSION_ORIGIN)
    assert resp.status_code == 201
    assert resp.headers["access-control-allow-origin"] == EXTENSION_ORIGIN


@pytest.mark.usefixtures("enable_salesnav_intake")
def test_non_local_environment_returns_403(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = _make_campaign(db_session, "SN non local")
    monkeypatch.setenv("APP_ENV", "staging")
    get_settings.cache_clear()
    resp = _post(client, _payload(str(campaign.id)))
    assert resp.status_code == 403
    assert resp.json()["error"] == "unauthorized"


@pytest.mark.usefixtures("enable_salesnav_intake")
def test_oversized_payload_returns_413(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = _make_campaign(db_session, "SN oversized")
    monkeypatch.setenv("SALESNAV_INTAKE_MAX_BYTES", "200")
    get_settings.cache_clear()
    resp = _post(client, _payload(str(campaign.id)))
    assert resp.status_code == 413
    assert resp.json()["error"] == "payload_too_large"
    assert db_session.scalar(select(func.count(ImportBatch.id))) == 0


# --- CORS preflight ----------------------------------------------------------


@pytest.mark.usefixtures("enable_salesnav_intake")
def test_cors_preflight_reflects_allowed_origin(client: TestClient) -> None:
    resp = client.options(INTAKE_URL, headers={"origin": LOOPBACK_ORIGIN})
    assert resp.status_code == 204
    assert resp.headers["access-control-allow-origin"] == LOOPBACK_ORIGIN
    assert resp.headers["access-control-allow-methods"] == "POST, GET, OPTIONS"
    assert "Idempotency-Key" in resp.headers["access-control-allow-headers"]
    assert "X-Client-Batch-Id" in resp.headers["access-control-allow-headers"]


@pytest.mark.usefixtures("enable_salesnav_intake")
def test_cors_preflight_rejects_disallowed_origin(client: TestClient) -> None:
    resp = client.options(INTAKE_URL, headers={"origin": "https://evil.example"})
    assert resp.status_code == 403
    assert "access-control-allow-origin" not in resp.headers


# --- Audit safety ------------------------------------------------------------


@pytest.mark.usefixtures("enable_salesnav_intake")
def test_audit_record_is_safe_and_credential_free(client: TestClient, db_session: Session) -> None:
    campaign = _make_campaign(db_session, "SN audit")
    payload = _payload(str(campaign.id))
    resp = _post(client, payload)
    assert resp.status_code == 201

    event = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "import.salesnav_staged")
    ).one()
    assert event.entity_type == "import_batch"
    assert set(event.context) == {
        "campaign_id",
        "client_batch_id",
        "record_count",
        "schema_version",
        "source",
        "source_format",
    }
    # No credentials/cookies/secrets and no raw-record PII anywhere in the event.
    serialized = json.dumps(
        {
            "action": event.action,
            "reason": event.reason,
            "context": event.context,
            "new_state": event.new_state,
        }
    ).lower()
    for banned in ("cookie", "authorization", "password", "secret", "token", "li_at", "session"):
        assert banned not in serialized
    assert "whitfield" not in serialized  # a captured surname must not leak into audit


# --- Transaction rollback (service-level) ------------------------------------


@pytest.mark.usefixtures("enable_salesnav_intake")
def test_transaction_rollback_stages_nothing(db_session: Session) -> None:
    campaign = _make_campaign(db_session, "SN rollback")
    payload = _payload(str(campaign.id))

    def _boom() -> None:
        raise RuntimeError("injected failure after rows written, before commit")

    with pytest.raises(RuntimeError):
        stage_salesnav_batch(
            db_session,
            payload=payload,
            operator_base_url=LOOPBACK_ORIGIN,
            _fault=_boom,
        )

    # The staging transaction rolled back: no batch, no rows persisted.
    assert db_session.scalar(select(func.count(ImportBatch.id))) == 0
    assert db_session.scalar(select(func.count(ImportRow.id))) == 0


@pytest.mark.usefixtures("enable_salesnav_intake")
def test_service_conflict_raises_typed_error(db_session: Session) -> None:
    campaign = _make_campaign(db_session, "SN svc conflict")
    payload = _payload(str(campaign.id))
    stage_salesnav_batch(db_session, payload=payload, operator_base_url=LOOPBACK_ORIGIN)

    changed = copy.deepcopy(payload)
    changed["records"] = changed["records"][:1]
    with pytest.raises(IdempotencyConflictError) as excinfo:
        stage_salesnav_batch(db_session, payload=changed, operator_base_url=LOOPBACK_ORIGIN)
    assert isinstance(excinfo.value, SalesNavIntakeError)
    assert excinfo.value.http_status == 409

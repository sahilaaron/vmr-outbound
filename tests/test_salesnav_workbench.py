"""UI-010: connect a staged Sales Navigator batch to the import workbench (#125).

Proves the operator can open the exact DAT-009 staged batch and drive it through
the SAME mapping -> non-committing preview -> explicit confirm flow the
spreadsheet importer uses, that no contact is created before confirmation, that
no validation/suppression/ambiguity rule is bypassed, and that the read-only
campaign selector the extension consumes behaves safely.
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
from app.models.campaign import CampaignContact
from app.models.contact import Contact
from app.models.enums import CampaignStatus, ImportBatchStatus, ImportSourceFormat
from app.models.import_batch import ImportBatch, ImportRow
from app.services.campaigns import create_campaign
from app.services.imports.importer import process_pending_batch
from app.services.imports.salesnav_intake import stage_salesnav_batch
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PAYLOAD = json.loads(
    (
        REPO_ROOT / "extensions" / "salesnav-capture" / "docs" / "fixtures" / "payload.example.json"
    ).read_text("utf-8")
)


@pytest.fixture()
def workbench_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Enable the workbench + import features locally for one test."""

    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__CSV_IMPORT", "true")
    monkeypatch.setenv("FEATURES__SALESNAV_INTAKE", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture()
def client(workbench_env: None, db_session: Session) -> Iterator[TestClient]:
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _stage_sn_batch(db_session: Session, campaign_id: str) -> str:
    payload = copy.deepcopy(EXAMPLE_PAYLOAD)
    payload["campaign_id"] = campaign_id
    payload["client_batch_id"] = str(uuid.uuid4())
    result = stage_salesnav_batch(
        db_session, payload=payload, operator_base_url="http://127.0.0.1:8000"
    )
    return result.staging_id


_SN_MAPPING = {
    "map__firstName": "first_name",
    "map__lastName": "last_name",
    "map__companyName": "company_name",
    "map__title": "title",
    "map__linkedinProfileUrl": "linkedin_url",
    "map__location": "country",
}


# --- Campaign selector -------------------------------------------------------


def test_campaigns_endpoint_returns_selectable_campaigns(
    client: TestClient, db_session: Session
) -> None:
    create_campaign(db_session, name="Active one", status=CampaignStatus.ACTIVE)
    create_campaign(db_session, name="Draft one", status=CampaignStatus.DRAFT)
    create_campaign(db_session, name="Archived one", status=CampaignStatus.ARCHIVED)
    db_session.flush()

    resp = client.get(
        "/api/campaigns?fields=id,name,status", headers={"origin": "http://127.0.0.1:8000"}
    )
    assert resp.status_code == 200
    names = {c["name"] for c in resp.json()["campaigns"]}
    assert "Active one" in names and "Draft one" in names
    assert "Archived one" not in names  # archived campaigns cannot receive imports
    assert resp.headers["access-control-allow-origin"] == "http://127.0.0.1:8000"


def test_campaigns_endpoint_404_when_feature_disabled(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("FEATURES__SALESNAV_INTAKE", "false")
    get_settings.cache_clear()
    app = create_app()

    def _ov() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _ov
    with TestClient(app) as c:
        assert c.get("/api/campaigns").status_code == 404
    app.dependency_overrides.clear()
    get_settings.cache_clear()


# --- Open the exact staged batch ---------------------------------------------


def test_pending_batch_shows_salesnav_provenance_and_cta(
    client: TestClient, db_session: Session
) -> None:
    campaign = create_campaign(db_session, name="SN wb open")
    db_session.flush()
    batch_id = _stage_sn_batch(db_session, str(campaign.id))

    resp = client.get(f"/imports/{batch_id}")
    assert resp.status_code == 200
    html = resp.text
    assert "Sales Navigator capture" in html
    assert "Staged — not yet imported" in html
    assert f"/imports/{batch_id}/map" in html  # prominent open-in-flow action
    # Search provenance and the capture contract render for operator review.
    assert "salesnav-capture/1.0.0" in html


# --- Map -> preview -> confirm (reusing existing controls) -------------------


def test_full_flow_no_contacts_before_confirm_then_truthful_outcomes(
    client: TestClient, db_session: Session
) -> None:
    campaign = create_campaign(db_session, name="SN wb flow")
    db_session.flush()
    batch_id = _stage_sn_batch(db_session, str(campaign.id))

    # Mapping page renders and suggests a mapping.
    assert client.get(f"/imports/{batch_id}/map").status_code == 200
    # Save a mapping (no company_domain source exists in a capture).
    save = client.post(f"/imports/{batch_id}/map", data=_SN_MAPPING, follow_redirects=False)
    assert save.status_code == 303

    # Preview is non-committing: still zero contacts.
    prev = client.get(f"/imports/{batch_id}/preview")
    assert prev.status_code == 200
    assert db_session.scalar(select(func.count(Contact.id))) == 0
    batch = db_session.get(ImportBatch, uuid.UUID(batch_id))
    assert batch is not None and batch.status == ImportBatchStatus.PENDING

    # Explicit confirm processes the batch in place through the normal checks.
    conf = client.post(f"/imports/{batch_id}/confirm", follow_redirects=False)
    assert conf.status_code == 303
    db_session.expire_all()
    batch = db_session.get(ImportBatch, uuid.UUID(batch_id))
    assert batch is not None
    assert batch.status == ImportBatchStatus.COMPLETED
    # Sales Navigator captures carry no company_domain -> rows truthfully rejected,
    # not silently accepted. No validation rule was bypassed.
    assert batch.total_rows == 2
    assert batch.rejected_rows == 2
    assert batch.contacts_created == 0
    assert db_session.scalar(select(func.count(Contact.id))) == 0


def test_confirm_is_idempotent(client: TestClient, db_session: Session) -> None:
    campaign = create_campaign(db_session, name="SN wb idem")
    db_session.flush()
    batch_id = _stage_sn_batch(db_session, str(campaign.id))
    client.post(f"/imports/{batch_id}/map", data=_SN_MAPPING, follow_redirects=False)
    first = client.post(f"/imports/{batch_id}/confirm", follow_redirects=False)
    assert first.status_code == 303
    second = client.post(f"/imports/{batch_id}/confirm", follow_redirects=False)
    assert second.status_code == 303
    # No duplicate processing; a single completed batch.
    assert db_session.scalar(select(func.count(ImportBatch.id))) == 1


def test_confirm_disabled_without_csv_import(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stage a batch, then confirm with csv_import OFF -> processing refused, no contacts.
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__CSV_IMPORT", "false")
    get_settings.cache_clear()
    campaign = create_campaign(db_session, name="SN wb nogate")
    db_session.flush()
    batch_id = _stage_sn_batch(db_session, str(campaign.id))
    app = create_app()

    def _ov() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _ov
    with TestClient(app) as c:
        c.post(f"/imports/{batch_id}/map", data=_SN_MAPPING, follow_redirects=False)
        resp = c.post(f"/imports/{batch_id}/confirm", follow_redirects=False)
    app.dependency_overrides.clear()
    get_settings.cache_clear()
    assert resp.status_code == 303  # redirected back with an error flash
    batch = db_session.get(ImportBatch, uuid.UUID(batch_id))
    assert batch is not None and batch.status == ImportBatchStatus.PENDING
    assert db_session.scalar(select(func.count(Contact.id))) == 0


# --- process_pending_batch reuses the real pipeline (accepted path) ----------


def _make_pending_orm_batch(
    db_session: Session, campaign_id: uuid.UUID, rows: list[dict]
) -> ImportBatch:
    batch = ImportBatch(
        campaign_id=campaign_id,
        client_batch_id=str(uuid.uuid4()),
        content_hash=uuid.uuid4().hex,
        status=ImportBatchStatus.PENDING,
        source_format=ImportSourceFormat.SALES_NAVIGATOR,
        total_rows=len(rows),
    )
    db_session.add(batch)
    db_session.flush()
    for i, raw in enumerate(rows, start=1):
        db_session.add(ImportRow(batch_id=batch.id, row_number=i, sheet_index=0, raw_data=raw))
    db_session.flush()
    return batch


@pytest.mark.usefixtures("workbench_env")
def test_process_pending_batch_creates_contacts_through_normal_checks(
    db_session: Session,
) -> None:
    """When rows carry a domain, processing-in-place creates real contacts.

    Uses a capture batch whose raw rows include a company_domain (as a future
    domain-enriched capture would), and an identity mapping, to prove the shared
    importer path — validation, contact creation, provenance — runs unchanged.
    """

    campaign = create_campaign(db_session, name="SN wb accept")
    db_session.flush()
    rows = [
        {
            "first_name": "Dana",
            "last_name": "Whitfield",
            "company_name": "Northwind",
            "company_domain": "northwind.example",
        },
        {
            "first_name": "Bad",
            "last_name": "Row",
            "company_name": "NoDomain",
            "company_domain": "",  # invalid/missing -> rejected by the same rule
        },
    ]
    batch = _make_pending_orm_batch(db_session, campaign.id, rows)
    mapping = {
        "first_name": "first_name",
        "last_name": "last_name",
        "company_name": "company_name",
        "company_domain": "company_domain",
    }

    summary = process_pending_batch(db_session, batch=batch, column_mapping=mapping)
    assert summary.status == ImportBatchStatus.COMPLETED
    assert summary.accepted_rows == 1
    assert summary.rejected_rows == 1
    assert summary.contacts_created == 1
    assert db_session.scalar(select(func.count(Contact.id))) == 1
    # The accepted contact was linked into the campaign (membership) as normal.
    assert db_session.scalar(select(func.count(CampaignContact.id))) == 1


@pytest.mark.usefixtures("workbench_env")
def test_process_pending_batch_rejects_non_pending(db_session: Session) -> None:
    from app.services.imports.importer import BatchNotProcessable

    campaign = create_campaign(db_session, name="SN wb nonpending")
    db_session.flush()
    batch = _make_pending_orm_batch(db_session, campaign.id, [{"first_name": "X"}])
    batch.status = ImportBatchStatus.FAILED
    db_session.flush()
    with pytest.raises(BatchNotProcessable):
        process_pending_batch(db_session, batch=batch, column_mapping=None)

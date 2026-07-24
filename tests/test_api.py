"""API route tests for campaign creation and staged import (CMP-001, DAT-002)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from app.api.deps import get_db
from app.main import create_app
from app.services.campaigns import create_campaign
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

ONE_CONTACT_CSV = (
    b"first_name,last_name,company_name,company_domain,email\n"
    b"Sam,Smith,Acme Widgets,acme.example,sam@acme.example\n"
)


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


def test_create_campaign_route_returns_201(client: TestClient) -> None:
    resp = client.post("/campaigns", json={"name": "API Campaign"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "API Campaign"
    assert body["status"] == "draft"


def test_create_campaign_route_rejects_blank_name(client: TestClient) -> None:
    resp = client.post("/campaigns", json={"name": "   "})
    assert resp.status_code == 400


def test_create_campaign_route_persists_full_settings(client: TestClient) -> None:
    resp = client.post(
        "/campaigns",
        json={
            "name": "Full API Campaign",
            "offer": "Free audit",
            "audience_rules": {"titles": ["CTO"]},
            "exclusions": {"excluded_titles": ["Intern"]},
            "min_score_threshold": 70,
            "tone": "direct",
            "owner": "sahil@example.com",
            "source": "manual",
            "sending_reference": "seq-1",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["offer"] == "Free audit"
    assert body["audience_rules"] == {"titles": ["CTO"]}
    assert body["exclusions"] == {"excluded_titles": ["Intern"]}
    assert body["min_score_threshold"] == 70
    assert body["tone"] == "direct"
    assert body["owner"] == "sahil@example.com"
    assert body["source"] == "manual"
    assert body["sending_reference"] == "seq-1"


def test_create_campaign_route_rejects_invalid_audience_rules(client: TestClient) -> None:
    resp = client.post(
        "/campaigns", json={"name": "Bad Rules", "audience_rules": ["not", "an", "object"]}
    )
    assert resp.status_code == 422  # pydantic rejects the wrong JSON shape


def test_create_campaign_route_rejects_out_of_range_threshold(client: TestClient) -> None:
    resp = client.post("/campaigns", json={"name": "Bad Threshold", "min_score_threshold": 500})
    assert resp.status_code == 422  # pydantic Field(ge=0, le=100) rejects it


def test_get_campaign_route_returns_settings(client: TestClient, db_session: Session) -> None:
    campaign = create_campaign(db_session, name="Readable Campaign", tone="warm")
    db_session.flush()

    resp = client.get(f"/api/campaigns/{campaign.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Readable Campaign"
    assert body["tone"] == "warm"


def test_get_campaign_route_404_for_missing(client: TestClient) -> None:
    resp = client.get(f"/api/campaigns/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_update_campaign_route_partial_update(client: TestClient, db_session: Session) -> None:
    campaign = create_campaign(db_session, name="Update Me", offer="Original offer", tone="warm")
    db_session.flush()

    resp = client.patch(f"/campaigns/{campaign.id}", json={"tone": "urgent"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["tone"] == "urgent"
    assert body["offer"] == "Original offer"  # untouched


def test_update_campaign_route_explicit_clear(client: TestClient, db_session: Session) -> None:
    campaign = create_campaign(db_session, name="Clear Me", description="Some description")
    db_session.flush()

    resp = client.patch(f"/campaigns/{campaign.id}", json={"description": None})
    assert resp.status_code == 200
    assert resp.json()["description"] is None


def test_update_campaign_route_rejects_null_name(client: TestClient, db_session: Session) -> None:
    campaign = create_campaign(db_session, name="Protected Name")
    db_session.flush()

    resp = client.patch(f"/campaigns/{campaign.id}", json={"name": None})
    assert resp.status_code == 400


def test_update_campaign_route_rejects_illegal_transition(
    client: TestClient, db_session: Session
) -> None:
    campaign = create_campaign(db_session, name="Transition Test")
    db_session.flush()

    archive_resp = client.patch(f"/campaigns/{campaign.id}", json={"status": "archived"})
    assert archive_resp.status_code == 200

    revive_resp = client.patch(f"/campaigns/{campaign.id}", json={"status": "draft"})
    assert revive_resp.status_code == 400


def test_update_campaign_route_404_for_missing(client: TestClient) -> None:
    resp = client.patch(f"/campaigns/{uuid.uuid4()}", json={"tone": "warm"})
    assert resp.status_code == 404


def test_import_route_disabled_returns_404(client: TestClient, db_session: Session) -> None:
    campaign = create_campaign(db_session, name="Disabled import")
    resp = client.post(f"/campaigns/{campaign.id}/imports", content=ONE_CONTACT_CSV)
    assert resp.status_code == 404


@pytest.mark.usefixtures("enable_csv_import")
def test_import_route_imports_when_enabled(client: TestClient, db_session: Session) -> None:
    campaign = create_campaign(db_session, name="Enabled import")
    resp = client.post(
        f"/campaigns/{campaign.id}/imports",
        params={"source_name": "API export"},
        content=ONE_CONTACT_CSV,
        headers={"content-type": "text/csv"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_rows"] == 1
    assert body["accepted_rows"] == 1
    assert body["contacts_created"] == 1
    assert body["status"] == "completed"

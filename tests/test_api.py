"""API route tests for campaign creation and staged import (CMP-001, DAT-002)."""

from __future__ import annotations

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

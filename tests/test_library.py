"""The Library: read for everyone, edited by administrators only, in place."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.enums import SellerClaimScope
from app.services.seller import profile as seller_profile
from app.services.seller import records as seller_records
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


@pytest.fixture()
def client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__SELLER_KNOWLEDGE_BASE", "true")
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as app_client:
        yield app_client
    get_settings.cache_clear()


def test_the_library_has_the_customer_views_and_no_admin_links(client: TestClient) -> None:
    body = client.get("/app/library").text
    for label in (
        "Overview",
        "Business profile",
        "Offerings",
        "Proof",
        "Message rules",
        "Personas",
    ):
        assert label in body
    assert "/knowledge-base" not in body
    assert "Edit in admin" not in body


def test_an_administrator_can_enter_the_business_profile_in_place(
    client: TestClient, db_session: Session
) -> None:
    empty = client.get("/app/library/company").text
    assert "No business profile yet" in empty
    assert 'action="/app/admin/library/company"' in empty  # local sessions are administrators

    response = client.post(
        "/app/admin/library/company",
        data={
            "name": "Verified Market Research",
            "short_description": "Market intelligence for investment teams.",
            "industries_served": "Manufacturing\nEnergy",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "ok=" in response.headers["location"]
    profile = seller_profile.get_profile(db_session)
    assert profile is not None and profile.name == "Verified Market Research"
    assert profile.industries_served == ["Manufacturing", "Energy"]

    page = client.get("/app/library/company").text
    assert "Verified Market Research" in page
    assert "Edit the business profile" in page


def test_offerings_rules_and_personas_are_created_and_linked_from_the_library(
    client: TestClient, db_session: Session
) -> None:
    created = client.post(
        "/app/admin/library/offerings",
        data={"name": "Custom research", "offering_type": "service", "short_description": "x"},
        follow_redirects=False,
    )
    assert created.status_code == 303 and "offering=" in created.headers["location"]
    offering = seller_records.list_offerings(db_session)[0]

    rule = client.post(
        "/app/admin/library/restricted-claims",
        data={
            "title": "No guarantees",
            "explanation": "We do not promise outcomes.",
            "scope": "offering",
        },
        follow_redirects=False,
    )
    assert "ok=" in rule.headers["location"]
    claim = seller_records.list_restricted_claims(db_session)[0]
    assert claim.scope is SellerClaimScope.OFFERING

    linked = client.post(
        f"/app/admin/library/offerings/{offering.id}/links",
        data={"kind": "restricted_claim", "related_id": str(claim.id)},
        follow_redirects=False,
    )
    assert "ok=" in linked.headers["location"]
    assert seller_records.restricted_claims_for_offering(db_session, offering.id)

    persona = client.post(
        "/app/admin/library/personas",
        data={"name": "Head of Research", "role_function": "Research", "seniority": "Head"},
        follow_redirects=False,
    )
    assert "ok=" in persona.headers["location"]

    page = client.get(f"/app/library/offerings?offering={offering.id}").text
    assert "Custom research" in page
    assert "No guarantees" in page
    assert "Message rules that apply" in page

    archived = client.post(
        f"/app/admin/library/offerings/{offering.id}/state",
        data={"action": "archive"},
        follow_redirects=False,
    )
    assert "archived" in archived.headers["location"]
    db_session.refresh(offering)
    assert offering.state.value == "archived"


def test_library_writes_are_administrator_only_by_policy() -> None:
    from app.core.auth.policy import is_admin_only_request

    for path in (
        "/app/admin/library/company",
        "/app/admin/library/offerings",
        "/app/admin/library/proof-points",
        "/app/admin/library/restricted-claims",
        "/app/admin/library/personas",
    ):
        assert is_admin_only_request(path, "POST"), path
    assert not is_admin_only_request("/app/library", "GET")

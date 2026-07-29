"""Thin Phase 2 API contracts expose durable audience and pipeline state."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.api.deps import get_db
from app.main import create_app
from app.models.contact import Contact
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


@pytest.fixture()
def phase2_client(db_session: Session) -> Iterator[TestClient]:
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def test_campaign_collection_enrolment_and_pipeline_read_contract(
    phase2_client: TestClient,
    db_session: Session,
) -> None:
    campaign_response = phase2_client.post(
        "/api/campaigns",
        json={
            "name": "API Phase 2",
            "sender_context": {"seller": "VMR"},
            "target_audience": {"role": "research leader"},
        },
    )
    assert campaign_response.status_code == 201
    campaign = campaign_response.json()
    assert campaign["execution_enabled"] is False
    assert campaign["settings_version"] == 1
    campaign_list = phase2_client.get("/api/campaigns?fields=id,name,status")
    assert campaign_list.status_code == 200
    assert [
        row["id"] for row in campaign_list.json()["campaigns"] if row["name"] == "API Phase 2"
    ] == [campaign["id"]]

    collection_response = phase2_client.post(
        "/api/collections",
        json={"name": "Healthcare", "description": "Global vertical"},
    )
    assert collection_response.status_code == 200
    collection = collection_response.json()["collection"]

    contact = Contact(
        first_name="Grace",
        last_name="Hopper",
        company_name="Compilers Inc",
        company_domain="compilers.example",
        natural_key="grace|hopper|compilers.example",
    )
    db_session.add(contact)
    db_session.flush()

    first_assignment = phase2_client.put(
        f"/api/collections/{collection['id']}/contacts/{contact.id}"
    )
    replay_assignment = phase2_client.put(
        f"/api/collections/{collection['id']}/contacts/{contact.id}"
    )
    assert first_assignment.json()["created"] is True
    assert replay_assignment.json()["created"] is False

    association = phase2_client.put(
        f"/api/campaigns/{campaign['id']}/collections/{collection['id']}",
        json={"role": "audience"},
    )
    assert association.json()["created"] is True

    enrolment = phase2_client.post(
        f"/api/campaigns/{campaign['id']}/contacts/{contact.id}",
        json={
            "source_type": "manual",
            "idempotency_key": "api-enrol-grace",
            "desired_stage": "company",
        },
    )
    assert enrolment.status_code == 200
    body = enrolment.json()
    assert body["created"] is True
    assert body["campaign_contact"]["contact_id"] == str(contact.id)
    assert body["campaign_contact"]["pipeline_status"] == "disabled"
    assert body["queued_job"] is None

    membership_id = body["campaign_contact"]["id"]
    pipeline_response = phase2_client.get(f"/api/campaign-contacts/{membership_id}/pipeline")
    assert pipeline_response.status_code == 200
    pipeline = pipeline_response.json()
    assert pipeline["campaign_contact"]["next_stage"] == "identity"
    assert any(stage["agent_id"] == "capture" for stage in pipeline["stages"])
    assert any(event["event_type"] == "enrolled" for event in pipeline["events"])

    audience = phase2_client.get(f"/api/campaigns/{campaign['id']}/contacts")
    assert audience.status_code == 200
    assert audience.json()["total"] == 1


def test_agent_registry_exposes_stored_global_control(
    phase2_client: TestClient,
) -> None:
    baseline = phase2_client.get("/api/agents")
    assert baseline.status_code == 200
    identity = next(agent for agent in baseline.json()["agents"] if agent["agent_id"] == "identity")
    assert identity["configured_status"] == "enabled"
    assert identity["global_control"] is None
    assert identity["skippable"] is False

    updated = phase2_client.put(
        "/api/agents/identity/control",
        json={
            "status": "paused",
            "config": {"batch_size": 25},
            "reason": "maintenance",
        },
    )
    assert updated.status_code == 200

    current = phase2_client.get("/api/agents")
    identity = next(agent for agent in current.json()["agents"] if agent["agent_id"] == "identity")
    assert identity["configured_status"] == "paused"
    assert identity["global_control"]["config"] == {"batch_size": 25}

"""App shell health tests (FND-003)."""

from __future__ import annotations

from app.main import create_app
from fastapi.testclient import TestClient

client = TestClient(create_app())


def test_healthz_is_minimal() -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_checks_database() -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ready",
        "checks": {"configuration": "ok", "database": "ok"},
    }

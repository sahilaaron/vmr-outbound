"""App shell health tests (FND-003)."""

from __future__ import annotations

from app.main import create_app
from fastapi.testclient import TestClient

client = TestClient(create_app())


def test_health_reports_safe_defaults() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # The shell ships with dry-run on and no features enabled.
    assert body["dry_run"] is True
    assert body["features_enabled"] == []
    assert "version" in body


def test_ready_checks_database() -> None:
    resp = client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["database"] == "ok"

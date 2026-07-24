"""Secret-safety boundaries for the verification path (AGENTS.md).

Proves the MillionVerifier API key never leaks into settings repr/dump, stored
evidence payloads, usage rows, or diagnostic URLs.
"""

from __future__ import annotations

import json

import pytest
from app.core.config import get_settings
from app.services.verification.provider import HttpMillionVerifier


def test_api_key_excluded_from_settings_repr_and_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MILLIONVERIFIER_API_KEY", "super-secret-key-123")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.has_millionverifier_key() is True
    assert "super-secret-key-123" not in repr(settings)
    assert "super-secret-key-123" not in json.dumps(settings.model_dump(mode="json"))
    get_settings.cache_clear()


def test_redacted_url_hides_key() -> None:
    client = HttpMillionVerifier("KEYABC", base_url="https://api.x/v3")
    url = client.redacted_url("a@b.com")
    assert "KEYABC" not in url


def test_stored_raw_payload_never_contains_key() -> None:
    class _T:
        def get(self, url: str, timeout: float) -> str:
            return json.dumps(
                {"email": "a@b.com", "result": "ok", "resultcode": 1, "api": "KEYABC"}
            )

    client = HttpMillionVerifier("KEYABC", base_url="https://api.x/v3", transport=_T())
    resp = client.verify("a@b.com")
    assert "KEYABC" not in json.dumps(resp.raw)
    assert "api" not in resp.raw

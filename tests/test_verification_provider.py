"""VER-001 / VER-002: provider adapter, test keys, HTTP client, key redaction.

No test here touches the network: the simulator is deterministic and the live
HTTP client is exercised through an injected fake transport.
"""

from __future__ import annotations

import json
import urllib.error

import pytest
from app.services.verification.provider import (
    LIVE_PROVIDER_LABEL,
    REQUEST_HEADERS,
    SIMULATOR_PROVIDER_LABEL,
    HttpMillionVerifier,
    ProviderTransientError,
    SimulatedMillionVerifier,
    UrllibTransport,
    build_provider,
    evidence_provider_label,
)


def test_simulator_derives_outcome_from_address() -> None:
    sim = SimulatedMillionVerifier()
    assert sim.verify("ok@acme.com").result == "ok"
    assert sim.verify("invalid@acme.com").result == "invalid"
    assert sim.verify("someone@catchall.example").result == "catch_all"
    assert sim.verify("unknown@acme.com").result == "unknown"
    assert sim.verify("user@mailinator.com").result == "disposable"


def test_simulator_role_flag_on_role_localpart() -> None:
    resp = SimulatedMillionVerifier().verify("info@acme.com")
    assert resp.result == "ok"
    assert resp.role is True


def test_simulator_insufficient_credits_and_timeout() -> None:
    sim = SimulatedMillionVerifier()
    assert sim.verify("nocredits@acme.com").error == "insufficient_credits"
    with pytest.raises(ProviderTransientError):
        sim.verify("timeoutuser@acme.com")


def test_documented_test_keys_route_to_simulator() -> None:
    assert SimulatedMillionVerifier("API_KEY_FOR_OK").verify("x@y.com").result == "ok"
    assert SimulatedMillionVerifier("API_KEY_FOR_CATCH_ALL").verify("x@y.com").result == "catch_all"
    assert (
        SimulatedMillionVerifier("API_KEY_FOR_ERROR_INSUFFICIENT_CREDITS").verify("x@y.com").error
        == "insufficient_credits"
    )


def test_build_provider_never_returns_live_for_test_key() -> None:
    provider = build_provider(
        api_key="API_KEY_FOR_OK", base_url="https://x", timeout_seconds=20, live=True
    )
    assert isinstance(provider, SimulatedMillionVerifier)


def test_build_provider_simulator_when_no_key() -> None:
    provider = build_provider(api_key=None, base_url="https://x", timeout_seconds=20, live=True)
    assert isinstance(provider, SimulatedMillionVerifier)


def test_build_provider_live_with_real_key_selects_http() -> None:
    provider = build_provider(
        api_key="a-real-live-key", base_url="https://x", timeout_seconds=20, live=True
    )
    assert isinstance(provider, HttpMillionVerifier)
    assert provider.simulated is False


def test_build_provider_real_key_without_live_stays_simulator() -> None:
    # A real key present but live NOT requested must never reach the network.
    provider = build_provider(
        api_key="a-real-live-key", base_url="https://x", timeout_seconds=20, live=False
    )
    assert isinstance(provider, SimulatedMillionVerifier)
    assert provider.simulated is True


def test_provider_simulated_flags_and_evidence_labels() -> None:
    sim = SimulatedMillionVerifier("a-real-live-key")
    live = HttpMillionVerifier("a-real-live-key", base_url="https://x")
    assert sim.simulated is True
    assert live.simulated is False
    assert evidence_provider_label(sim) == SIMULATOR_PROVIDER_LABEL
    assert evidence_provider_label(live) == LIVE_PROVIDER_LABEL
    # The two labels are distinguishable so a simulated row is never shown as live.
    assert SIMULATOR_PROVIDER_LABEL != LIVE_PROVIDER_LABEL
    assert SIMULATOR_PROVIDER_LABEL.endswith("-simulator")


class _FakeTransport:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.last_url: str | None = None

    def get(self, url: str, timeout: float) -> str:
        self.last_url = url
        return json.dumps(self.payload)


def test_http_client_builds_request_and_parses_response() -> None:
    fake = _FakeTransport(
        {"email": "a@b.com", "result": "ok", "resultcode": 1, "credits": 42, "role": False}
    )
    client = HttpMillionVerifier("SECRETKEY", base_url="https://api.x/v3", transport=fake)
    resp = client.verify("a@b.com")
    assert resp.result == "ok"
    assert resp.resultcode == 1
    assert resp.credits == 42
    assert "email=a%40b.com" in (fake.last_url or "")


def test_http_client_redacts_key_in_diagnostic_url() -> None:
    client = HttpMillionVerifier("SUPERSECRET", base_url="https://api.x/v3")
    redacted = client.redacted_url("a@b.com")
    assert "SUPERSECRET" not in redacted
    assert "REDACTED" in redacted


def test_http_client_never_stores_key_in_payload() -> None:
    fake = _FakeTransport({"email": "a@b.com", "result": "ok", "resultcode": 1, "api": "SECRET"})
    client = HttpMillionVerifier("SECRET", base_url="https://api.x/v3", transport=fake)
    resp = client.verify("a@b.com")
    assert "api" not in resp.raw
    assert "SECRET" not in json.dumps(resp.raw)


def test_http_client_transport_failure_is_transient() -> None:
    class _Boom:
        def get(self, url: str, timeout: float) -> str:
            raise OSError("connection refused SECRET")

    client = HttpMillionVerifier("SECRET", base_url="https://api.x/v3", transport=_Boom())
    with pytest.raises(ProviderTransientError) as exc:
        client.verify("a@b.com")
    assert "SECRET" not in str(exc.value)  # redacted


def test_http_client_malformed_json_is_transient() -> None:
    class _Bad:
        def get(self, url: str, timeout: float) -> str:
            return "not json"

    client = HttpMillionVerifier("SECRET", base_url="https://api.x/v3", transport=_Bad())
    with pytest.raises(ProviderTransientError):
        client.verify("a@b.com")


def test_urllib_transport_sends_accept_and_user_agent_headers() -> None:
    # The header contract is verified offline via the pure request builder.
    req = UrllibTransport().build_request("https://api.x/v3?api=SECRET&email=a%40b.com")
    assert req.get_header("Accept") == "application/json"
    assert req.get_header("User-agent") == "vmr-outbound/0.0.1"
    assert REQUEST_HEADERS == {
        "Accept": "application/json",
        "User-Agent": "vmr-outbound/0.0.1",
    }
    # The URL (which carries the key) is passed through unchanged for the GET.
    assert req.full_url == "https://api.x/v3?api=SECRET&email=a%40b.com"


class _HttpErrorTransport:
    def __init__(self, code: int, msg: str = "denied") -> None:
        self.code = code
        self.msg = msg

    def get(self, url: str, timeout: float) -> str:
        raise urllib.error.HTTPError(url, self.code, self.msg, {}, None)  # type: ignore[arg-type]


def test_http_401_403_map_to_access_rejected_not_retryable() -> None:
    for code in (401, 403):
        client = HttpMillionVerifier(
            "SECRET", base_url="https://api.x/v3", transport=_HttpErrorTransport(code)
        )
        resp = client.verify("a@b.com")  # returns, does not raise (no auto-retry)
        assert resp.error == "access_rejected"
        assert resp.result is None
        assert resp.raw.get("http_status") == code
        assert "SECRET" not in json.dumps(resp.raw)


def test_other_http_errors_stay_transient_and_redacted() -> None:
    # A 500 includes the key in its message on purpose; it must be redacted and
    # remain a retryable transport failure.
    client = HttpMillionVerifier(
        "SECRET", base_url="https://api.x/v3", transport=_HttpErrorTransport(500, "boom SECRET")
    )
    with pytest.raises(ProviderTransientError) as exc:
        client.verify("a@b.com")
    assert "SECRET" not in str(exc.value)

"""Production HTTP/configuration hardening contracts."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import pytest
from app.core.config import Settings
from app.core.diagnostics import MAX_DEPTH_MARKER, REDACTED, serialize_diagnostic
from app.core.features import FeatureFlags
from app.core.http import RequestContext, current_request_id, valid_request_id
from app.core.runtime import RuntimeConfigurationError
from app.main import create_app
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError


def _ready() -> None:
    return None


def _failed_readiness() -> None:
    raise RuntimeError(
        "password=DATABASE-PASSWORD postgresql://user:secret@db.internal/vmr /srv/private/app.py"
    )


def _client(
    settings: Settings | None = None,
    *,
    probe: object = _ready,
    raise_server_exceptions: bool = True,
    base_url: str = "http://testserver",
    peer: tuple[str, int] = ("testclient", 50000),
) -> TestClient:
    return TestClient(
        create_app(settings or Settings(_env_file=None), readiness_probe=probe),  # type: ignore[arg-type,call-arg]
        raise_server_exceptions=raise_server_exceptions,
        base_url=base_url,
        client=peer,
    )


def _request_logs(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    result = []
    for record in caplog.records:
        if record.name != "vmr.http" or not record.getMessage().startswith("{"):
            continue
        parsed = json.loads(record.getMessage())
        if parsed.get("event") == "http_request":
            result.append(parsed)
    return result


def test_healthz_is_minimal_and_dependency_free() -> None:
    response = _client(probe=_failed_readiness).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_healthy_contract() -> None:
    response = _client().get("/readyz")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"configuration": "ok", "database": "ok"},
    }


@pytest.mark.parametrize("probe", [_failed_readiness, lambda: (_ for _ in ()).throw(ValueError())])
def test_readyz_failure_is_503_and_sanitized(probe: object) -> None:
    response = _client(probe=probe).get("/readyz")
    rendered = response.text
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"configuration": "ok", "database": "failed"},
    }
    for forbidden in ("DATABASE-PASSWORD", "postgresql", "db.internal", "/srv", "RuntimeError"):
        assert forbidden not in rendered


def test_readyz_uses_the_real_disposable_postgres() -> None:
    response = TestClient(create_app()).get("/readyz")
    assert response.status_code == 200
    assert response.json()["checks"]["database"] == "ok"


def test_readiness_has_a_wall_clock_bound_before_sql() -> None:
    class StalledEngine:
        def connect(self) -> object:
            time.sleep(0.2)
            raise AssertionError("late connection result")

    from app.core.health import DatabaseReadinessProbe

    probe = DatabaseReadinessProbe(StalledEngine(), timeout_seconds=0.02)  # type: ignore[arg-type]
    started = time.perf_counter()
    with pytest.raises(TimeoutError, match="wall-clock"):
        probe()
    assert time.perf_counter() - started < 0.1


def test_readiness_rejects_concurrent_pressure_within_the_same_budget() -> None:
    class StalledEngine:
        def connect(self) -> object:
            time.sleep(0.2)
            raise AssertionError("late connection result")

    from app.core.health import DatabaseReadinessProbe

    probe = DatabaseReadinessProbe(StalledEngine(), timeout_seconds=0.02)  # type: ignore[arg-type]
    with pytest.raises(TimeoutError, match="wall-clock"):
        probe()
    started = time.perf_counter()
    with pytest.raises(TimeoutError, match="already in progress"):
        probe()
    assert time.perf_counter() - started < 0.1


def test_request_id_is_generated_and_returned() -> None:
    response = _client().get("/healthz")
    request_id = response.headers["x-request-id"]
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)


def test_valid_external_request_id_is_preserved_in_response_and_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="vmr.http"):
        response = _client().get("/healthz", headers={"X-Request-ID": "deploy-42:probe.7"})
    assert response.headers["x-request-id"] == "deploy-42:probe.7"
    assert _request_logs(caplog)[-1]["request_id"] == "deploy-42:probe.7"


def test_invalid_request_ids_are_rejected() -> None:
    assert valid_request_id("safe-id") is True
    assert valid_request_id("safe\r\nX-Injected: yes") is False
    assert valid_request_id("x" * 65) is False

    oversized = _client().get("/healthz", headers={"X-Request-ID": "x" * 65})
    assert oversized.headers["x-request-id"] != "x" * 65
    assert re.fullmatch(r"[0-9a-f]{32}", oversized.headers["x-request-id"])


def test_structured_log_uses_route_template_and_excludes_sensitive_input(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(Settings(_env_file=None), readiness_probe=_ready)  # type: ignore[call-arg]

    @app.post("/log-test/{item_id}")
    async def log_test(item_id: str) -> dict[str, str]:
        return {"item_id": item_id}

    with caplog.at_level(logging.INFO, logger="vmr.http"):
        response = TestClient(app).post(
            "/log-test/customer@example.com?token=QUERY-SECRET",
            content="BODY-SECRET personalized email text",
            headers={
                "Authorization": "Bearer AUTHORIZATION-SECRET",
                "Cookie": "session=COOKIE-SECRET",
            },
        )
    assert response.status_code == 200
    event = _request_logs(caplog)[-1]
    assert event["route"] == "/log-test/{item_id}"
    assert event["method"] == "POST"
    assert event["status_code"] == 200
    rendered = json.dumps(event)
    for forbidden in (
        "customer@example.com",
        "QUERY-SECRET",
        "BODY-SECRET",
        "AUTHORIZATION-SECRET",
        "COOKIE-SECRET",
    ):
        assert forbidden not in rendered


def test_unhandled_exception_is_sanitized_and_correlated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(Settings(_env_file=None), readiness_probe=_ready)  # type: ignore[call-arg]

    @app.get("/explode")
    async def explode() -> None:
        assert current_request_id() == "incident-123"
        raise RuntimeError("SQL SELECT secret FROM /private/path password=DO-NOT-RETURN")

    with caplog.at_level(logging.INFO, logger="vmr.http"):
        response = TestClient(app, raise_server_exceptions=False).get(
            "/explode", headers={"X-Request-ID": "incident-123"}
        )
    assert response.status_code == 500
    assert response.headers["x-request-id"] == "incident-123"
    assert response.json() == {
        "error": "internal_server_error",
        "message": "The request could not be completed.",
        "request_id": "incident-123",
    }
    for forbidden in ("SELECT", "/private", "DO-NOT-RETURN", "RuntimeError"):
        assert forbidden not in response.text
    exception_events = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "vmr.http" and "http_unhandled_exception" in record.getMessage()
    ]
    assert exception_events[-1]["request_id"] == "incident-123"


def test_nested_exception_log_keeps_location_but_not_messages(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(Settings(_env_file=None), readiness_probe=_ready)  # type: ignore[call-arg]

    @app.get("/nested-explode")
    async def nested_explode() -> None:
        try:
            raise ValueError("password=INNER-SECRET\r\nFORGED-INNER")
        except ValueError as exc:
            raise RuntimeError("postgresql://u:OUTER-SECRET@db/vmr\r\nFORGED-OUTER") from exc

    with caplog.at_level(logging.INFO, logger="vmr.http"):
        response = TestClient(app, raise_server_exceptions=False).get("/nested-explode")
    assert response.status_code == 500
    event = next(
        json.loads(record.getMessage())
        for record in caplog.records
        if '"event":"http_unhandled_exception"' in record.getMessage()
    )
    assert [item["exception_type"] for item in event["exceptions"]] == [
        "RuntimeError",
        "ValueError",
    ]
    assert any(frame["function"] == "nested_explode" for frame in event["exceptions"][0]["frames"])
    for forbidden in ("INNER-SECRET", "OUTER-SECRET", "FORGED-INNER", "FORGED-OUTER"):
        assert forbidden not in caplog.text


def test_known_http_exception_keeps_its_intended_response() -> None:
    app = create_app(Settings(_env_file=None), readiness_probe=_ready)  # type: ignore[call-arg]

    @app.get("/known")
    async def known() -> None:
        raise HTTPException(status_code=409, detail="known conflict")

    response = TestClient(app).get("/known")
    assert response.status_code == 409
    assert response.json() == {"detail": "known conflict"}


def test_standard_security_and_cache_headers_are_pinned() -> None:
    response = _client().get("/healthz")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "style-src 'self' 'unsafe-inline'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert "strict-transport-security" not in response.headers


def test_hsts_requires_known_https() -> None:
    response = _client(base_url="https://testserver").get("/healthz")
    assert response.headers["strict-transport-security"] == "max-age=31536000"


def test_static_files_are_cacheable_but_customer_and_admin_pages_are_not() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        features=FeatureFlags(workbench=True),
    )
    with _client(settings) as client:
        static = client.get("/static/app.css")
        assert static.status_code == 200
        assert static.headers["cache-control"] == "public, max-age=3600"
        assert client.get("/app").headers["cache-control"] == "no-store"
        assert client.get("/admin").headers["cache-control"] == "no-store"


def test_trusted_host_accepts_allowed_and_rejects_bad_host() -> None:
    client = _client()
    assert client.get("/healthz").status_code == 200
    rejected = client.get("/healthz", headers={"Host": "evil.example"})
    assert rejected.status_code == 400
    assert rejected.headers["x-request-id"]


def test_trusted_host_normalization_matches_runtime() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        trusted_hosts=("Example.COM.", "[::1]"),
    )
    assert settings.trusted_hosts == ("example.com", "[::1]")
    client = _client(settings)
    assert client.get("/healthz", headers={"Host": "EXAMPLE.COM.:8443"}).status_code == 200
    assert client.get("/healthz", headers={"Host": "[::1]:8443"}).status_code == 200
    assert client.get("/healthz", headers={"Host": "example.com.evil"}).status_code == 400


def test_trusted_host_configuration_rejects_ports() -> None:
    with pytest.raises(ValidationError, match="must not include ports"):
        Settings(_env_file=None, trusted_hosts=("example.com:8443",))  # type: ignore[call-arg]


def test_direct_peer_cannot_spoof_forwarded_headers(caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        trusted_proxy_cidrs=("10.0.0.0/8",),
    )
    with caplog.at_level(logging.INFO, logger="vmr.http"):
        response = _client(settings, peer=("203.0.113.9", 50000)).get(
            "/healthz",
            headers={"X-Forwarded-For": "198.51.100.7", "X-Forwarded-Proto": "https"},
        )
    event = _request_logs(caplog)[-1]
    assert event["trusted_proxy"] is False
    assert event["client_ip"] == "203.0.113.9"
    assert event["scheme"] == "http"
    assert "strict-transport-security" not in response.headers


def test_trusted_proxy_information_is_used_conservatively(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        trusted_proxy_cidrs=("10.0.0.0/8",),
    )
    with caplog.at_level(logging.INFO, logger="vmr.http"):
        response = _client(settings, peer=("10.2.3.4", 50000)).get(
            "/healthz",
            headers={
                "X-Forwarded-For": "192.0.2.99, 198.51.100.7",
                "X-Forwarded-Proto": "https",
            },
        )
    event = _request_logs(caplog)[-1]
    assert event["trusted_proxy"] is True
    assert event["client_ip"] == "198.51.100.7"
    assert event["scheme"] == "https"
    assert response.headers["strict-transport-security"] == "max-age=31536000"


def test_request_context_ignores_malformed_forwarding() -> None:
    scope = {
        "type": "http",
        "client": ("10.1.1.1", 1234),
        "scheme": "http",
        "headers": [(b"x-forwarded-for", b"not-an-ip"), (b"x-forwarded-proto", b"ftp")],
    }
    context = RequestContext(scope, ("10.0.0.0/8",))  # type: ignore[arg-type]
    assert str(context.client) == "10.1.1.1"
    assert context.scheme == "http"


def _production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "app_env": "production",
        "database_url": "postgresql+psycopg://service:password@db.example.com/vmr_prod",
        "trusted_hosts": ("outbound.example.com",),
        "trusted_proxy_cidrs": ("10.20.0.0/24",),
        "dry_run": True,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"debug": True}, "DEBUG"),
        ({"trusted_hosts": ("*",)}, "TRUSTED_HOSTS"),
        ({"trusted_proxy_cidrs": ("0.0.0.0/0",)}, "TRUSTED_PROXY_CIDRS"),
        (
            {"database_url": "postgresql+psycopg://user:TOP-SECRET@127.0.0.1/vmr_dev"},
            "DATABASE_URL",
        ),
        ({"dry_run": False}, "DRY_RUN"),
    ],
)
def test_production_refuses_known_dangerous_configuration(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(RuntimeConfigurationError, match=message) as caught:
        create_app(_production_settings(**overrides), readiness_probe=_ready)
    assert "TOP-SECRET" not in str(caught.value)


def test_production_accepts_a_safe_known_configuration() -> None:
    app = create_app(_production_settings(), readiness_probe=_ready)
    with TestClient(app, base_url="https://outbound.example.com") as client:
        assert client.get("/healthz").status_code == 200


def test_production_rejects_canonical_local_host_variants() -> None:
    for host in ("localhost", "LOCALHOST", "localhost."):
        with pytest.raises(RuntimeConfigurationError, match="TRUSTED_HOSTS"):
            create_app(_production_settings(trusted_hosts=(host,)), readiness_probe=_ready)


def test_production_rejects_trailing_dot_local_database_host() -> None:
    with pytest.raises(RuntimeConfigurationError, match="DATABASE_URL"):
        create_app(
            _production_settings(database_url="postgresql+psycopg://u:p@localhost./vmr_prod"),
            readiness_probe=_ready,
        )


def test_malformed_production_database_url_is_validated_before_engine_construction() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "APP_ENV": "production",
            "DATABASE_URL": "not-a-sqlalchemy-url",
            "DRY_RUN": "true",
            "TRUSTED_HOSTS": '["outbound.example.com"]',
            "TRUSTED_PROXY_CIDRS": '["10.20.0.0/24"]',
            "VMR_TEST_MODE": "1",
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=Path.cwd(),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "RuntimeConfigurationError" in output
    assert "sqlalchemy.exc.ArgumentError" not in output


def test_local_defaults_remain_compatible() -> None:
    with _client() as client:
        assert client.get("/healthz").status_code == 200


def test_request_size_under_over_malformed_and_absent() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        max_request_bytes=10,
        max_upload_bytes=10,
    )
    app = create_app(settings, readiness_probe=_ready)

    @app.post("/accept-body")
    async def accept_body() -> dict[str, bool]:
        return {"accepted": True}

    client = TestClient(app)
    assert client.post("/accept-body", content=b"1234567890").status_code == 200
    assert (
        client.post("/accept-body", content=b"x", headers={"Content-Length": "11"}).status_code
        == 413
    )
    malformed = client.post(
        "/accept-body", content=b"x", headers={"Content-Length": "not-a-number"}
    )
    assert malformed.status_code == 400
    # GET carries no request body and no Content-Length; the middleware must not
    # fabricate one or reject the request merely because the header is absent.
    assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize(
    "value",
    ["-1", "+1", " 1", "1.0", "\N{SUPERSCRIPT TWO}", "9" * 10_000],
)
def test_malformed_content_length_edges_are_controlled_400(value: str) -> None:
    from app.core.http import ProductionHTTPMiddleware

    middleware = ProductionHTTPMiddleware(
        lambda _scope, _receive, _send: None,  # type: ignore[arg-type]
        max_request_bytes=10,
        trusted_proxy_cidrs=(),
        hsts_max_age_seconds=0,
    )
    response = middleware._content_length_response(  # noqa: SLF001
        {"headers": [(b"content-length", value.encode("latin-1"))]},  # type: ignore[arg-type]
        "request-id",
    )
    assert response is not None
    assert response.status_code == 400


def test_global_request_limit_cannot_break_the_existing_upload_limit() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        max_request_bytes=100,
        max_upload_bytes=101,
    )
    with pytest.raises(RuntimeConfigurationError, match="MAX_UPLOAD_BYTES"):
        create_app(settings, readiness_probe=_ready)


def test_bounded_diagnostics_are_deterministic_redacted_and_safe() -> None:
    small = serialize_diagnostic({"z": 2, "a": "plain"})
    assert small == {"a": "plain", "z": 2}

    huge = serialize_diagnostic("x" * 20, max_string=8)
    assert huge == "xxxxxxxx…[truncated 12 chars]"

    deep: object = {"one": {"two": {"three": "value"}}}
    assert serialize_diagnostic(deep, max_depth=2) == {"one": {"two": MAX_DEPTH_MARKER}}

    array = serialize_diagnostic(list(range(8)), max_items=3)
    assert array == [0, 1, 2, "[truncated 5 items]"]

    malicious = serialize_diagnostic("<script>alert(1)</script>")
    assert malicious == "&lt;script&gt;alert(1)&lt;/script&gt;"

    exception = serialize_diagnostic(RuntimeError("password=NEVER-RENDER"))
    assert exception == {"error_type": "RuntimeError", "message": "[exception detail withheld]"}
    assert "NEVER-RENDER" not in str(exception)

    secrets = serialize_diagnostic(
        {
            "password": "one",
            "api_key": "two",
            "Authorization": "three",
            "cookie": "four",
            "nested": {"refresh_token": "five"},
        }
    )
    assert secrets == {
        "Authorization": REDACTED,
        "api_key": REDACTED,
        "cookie": REDACTED,
        "nested": {"refresh_token": REDACTED},
        "password": REDACTED,
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "postgresql://reporter:db%2Fpassword@db.example.com/vmr",
            "postgresql://[redacted]@db.example.com/vmr",
        ),
        (
            "https://user:p%40ss@example.com/private",
            "https://[redacted]@example.com/private",
        ),
        ("https://example.com/public", "https://example.com/public"),
    ],
)
def test_diagnostic_urls_redact_userinfo(value: str, expected: str) -> None:
    assert serialize_diagnostic({"endpoint": value}) == {"endpoint": expected}


class _CountingSequence(Sequence[int]):
    def __init__(self, count: int) -> None:
        self.count = count
        self.reads = 0

    def __len__(self) -> int:
        return self.count

    def __getitem__(self, index: int) -> int:
        if index >= self.count:
            raise IndexError
        self.reads += 1
        return index


class _CountingMapping(Mapping[str, int]):
    def __init__(self, count: int) -> None:
        self.count = count
        self.reads = 0

    def __len__(self) -> int:
        return self.count

    def __iter__(self) -> Iterator[str]:
        for index in range(self.count):
            self.reads += 1
            yield f"key-{index:06d}"

    def __getitem__(self, key: str) -> int:
        return int(key.rsplit("-", 1)[-1])


class _HostileKey:
    def __str__(self) -> str:
        raise RuntimeError("hostile __str__")

    def __repr__(self) -> str:
        raise RuntimeError("hostile __repr__")


def test_diagnostic_collection_work_is_bounded() -> None:
    sequence = _CountingSequence(20_000)
    mapping = _CountingMapping(20_000)
    assert serialize_diagnostic(sequence, max_items=3) == [0, 1, 2, "[truncated 19997 items]"]
    mapped = serialize_diagnostic(mapping, max_items=3)
    assert mapped["…"] == "[truncated 19997 items]"  # type: ignore[index]
    assert sequence.reads == 4
    assert mapping.reads == 4


def test_diagnostic_hostile_mapping_key_uses_a_fixed_marker() -> None:
    result = serialize_diagnostic({_HostileKey(): "value"})
    assert result == {"[unsupported key _HostileKey #1]": "value"}


def test_campaign_archive_confirmation_is_csp_compatible() -> None:
    template = Path("app/web/v2/templates/campaigns.html").read_text(encoding="utf-8")
    script = Path("app/web/static/campaigns.js").read_text(encoding="utf-8")
    csp = _client().get("/healthz").headers["content-security-policy"]
    assert "onsubmit=" not in template
    assert "data-archive-confirm=" in template
    assert "window.confirm" in script
    assert "script-src 'self'" in csp
    assert "script-src 'self' 'unsafe-inline'" not in csp


def test_version_is_benign_and_deployment_provided() -> None:
    assert _client().get("/version").json() == {"version": "unknown"}
    settings = Settings(_env_file=None, release_id="release-2026.08.07+build.4")  # type: ignore[call-arg]
    assert _client(settings).get("/version").json() == {"version": "release-2026.08.07+build.4"}

"""Production HTTP/configuration hardening contracts."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import select
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from app.core.auth.config import AuthSettings
from app.core.config import Settings
from app.core.features import FeatureFlags
from app.core.http import RequestContext, _host_from_scope, current_request_id, valid_request_id
from app.core.runtime import RuntimeConfigurationError
from app.main import create_app
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.engine import make_url


class _FreezingPostgresProxy:
    """Tiny TCP proxy that can drop server replies only after PostgreSQL startup."""

    def __init__(self, target_host: str, target_port: int) -> None:
        self.freeze_query_replies = threading.Event()
        self._stop = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        self._listener.settimeout(0.1)
        self.host, self.port = self._listener.getsockname()
        self._target = (target_host, target_port)
        self._connections: set[socket.socket] = set()
        self._guard = threading.Lock()
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._proxy, args=(client,), daemon=True).start()

    def _proxy(self, client: socket.socket) -> None:
        upstream = socket.create_connection(self._target, timeout=1)
        client.settimeout(None)
        upstream.settimeout(None)
        with self._guard:
            self._connections.update((client, upstream))
        startup_tail = b""
        startup_complete = False
        query_seen = False
        try:
            while not self._stop.is_set():
                readable, _, _ = select.select((client, upstream), (), (), 0.1)
                if client in readable:
                    data = client.recv(64 * 1024)
                    if not data:
                        return
                    if startup_complete:
                        query_seen = True
                    upstream.sendall(data)
                if upstream in readable:
                    data = upstream.recv(64 * 1024)
                    if not data:
                        return
                    if not startup_complete:
                        startup_tail = (startup_tail + data)[-64:]
                        startup_complete = b"Z\x00\x00\x00\x05" in startup_tail
                    if self.freeze_query_replies.is_set() and query_seen:
                        continue
                    client.sendall(data)
        except (ConnectionError, OSError):
            return
        finally:
            with self._guard:
                self._connections.discard(client)
                self._connections.discard(upstream)
            client.close()
            upstream.close()

    def close(self) -> None:
        self._stop.set()
        self._listener.close()
        with self._guard:
            connections = tuple(self._connections)
        for connection in connections:
            connection.close()
        self._thread.join(timeout=1)

    def __enter__(self) -> _FreezingPostgresProxy:
        return self

    def __exit__(self, *args: object) -> None:
        del args
        self.close()


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


def test_legacy_health_paths_expose_the_new_authoritative_contracts() -> None:
    client = _client()
    assert client.get("/health").json() == {"status": "ok"}
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json() == {
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
    from app.core.health import DatabaseReadinessProbe

    async def stalled_connection(*args: object, **kwargs: object) -> object:
        del args, kwargs
        await asyncio.sleep(0.2)
        raise AssertionError("late connection result")

    probe = DatabaseReadinessProbe(
        "postgresql://service@db.example.com/vmr",
        timeout_seconds=0.02,
        connect_timeout_seconds=1,
        connection_factory=stalled_connection,  # type: ignore[arg-type]
    )
    started = time.perf_counter()
    with pytest.raises(TimeoutError, match="wall-clock"):
        asyncio.run(probe())
    assert time.perf_counter() - started < 0.1


def test_readiness_contention_and_work_share_one_absolute_deadline() -> None:
    from app.core.health import DatabaseReadinessProbe

    async def stalled_connection(*args: object, **kwargs: object) -> object:
        del args, kwargs
        await asyncio.sleep(1)
        raise AssertionError("late connection result")

    timeout = 0.08
    probe = DatabaseReadinessProbe(
        "postgresql://service@db.example.com/vmr",
        timeout_seconds=timeout,
        connect_timeout_seconds=1,
        connection_factory=stalled_connection,  # type: ignore[arg-type]
    )

    async def attack() -> float:
        first = asyncio.create_task(probe())
        await asyncio.sleep(timeout * 0.05)
        started = time.perf_counter()
        with pytest.raises(TimeoutError, match="wall-clock"):
            await probe()
        elapsed = time.perf_counter() - started
        with pytest.raises(TimeoutError, match="wall-clock"):
            await first
        return elapsed

    elapsed = asyncio.run(attack())
    assert timeout * 0.85 <= elapsed < timeout * 1.4


def test_timed_out_readiness_operation_does_not_poison_later_probe() -> None:
    from app.core.health import DatabaseReadinessProbe

    class Cursor:
        async def fetchone(self) -> tuple[int]:
            return (1,)

    class Connection:
        closed = False

        async def execute(self, *args: object, **kwargs: object) -> Cursor:
            del args, kwargs
            return Cursor()

        async def close(self) -> None:
            self.closed = True

    stalled = True

    async def connection_factory(*args: object, **kwargs: object) -> Connection:
        del args, kwargs
        if stalled:
            await asyncio.Event().wait()
        return Connection()

    probe = DatabaseReadinessProbe(
        "postgresql://service@db.example.com/vmr",
        timeout_seconds=0.02,
        connect_timeout_seconds=1,
        connection_factory=connection_factory,  # type: ignore[arg-type]
    )

    async def attack() -> None:
        nonlocal stalled
        with pytest.raises(TimeoutError, match="wall-clock"):
            await probe()
        stalled = False
        await probe()

    asyncio.run(attack())


def test_cancelled_readiness_caller_does_not_leave_database_work_running() -> None:
    from app.core.health import DatabaseReadinessProbe

    class Cursor:
        async def fetchone(self) -> tuple[int]:
            return (1,)

    class Connection:
        async def execute(self, *args: object, **kwargs: object) -> Cursor:
            del args, kwargs
            return Cursor()

        async def close(self) -> None:
            return None

    stalled = True

    async def connection_factory(*args: object, **kwargs: object) -> Connection:
        del args, kwargs
        if stalled:
            await asyncio.Event().wait()
        return Connection()

    probe = DatabaseReadinessProbe(
        "postgresql://service@db.example.com/vmr",
        timeout_seconds=1,
        connect_timeout_seconds=1,
        connection_factory=connection_factory,  # type: ignore[arg-type]
    )

    async def attack() -> None:
        nonlocal stalled
        caller = asyncio.create_task(probe())
        await asyncio.sleep(0.01)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        stalled = False
        await probe()
        assert len(asyncio.all_tasks()) == 1

    asyncio.run(attack())


def test_readyz_recovers_after_a_midstream_tcp_freeze() -> None:
    database_url = make_url(os.environ["DATABASE_URL"])
    assert database_url.host is not None
    target_port = database_url.port or 5432
    with _FreezingPostgresProxy(database_url.host, target_port) as proxy:
        proxy_url = database_url.set(host=proxy.host, port=proxy.port).update_query_dict(
            {"sslmode": "disable"}
        )
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            database_url=proxy_url.render_as_string(hide_password=False),
            readiness_timeout_seconds=0.2,
            database_connect_timeout_seconds=1,
        )
        with TestClient(create_app(settings)) as client:
            assert client.get("/readyz").status_code == 200
            proxy.freeze_query_replies.set()
            started = time.perf_counter()
            stalled = client.get("/readyz")
            elapsed = time.perf_counter() - started
            assert stalled.status_code == 503
            assert elapsed < 0.5
            proxy.freeze_query_replies.clear()
            recovered = client.get("/readyz")
            assert recovered.status_code == 200
            assert recovered.json()["checks"]["database"] == "ok"
    assert not [thread for thread in threading.enumerate() if thread.name == "vmr-readiness"]


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


def test_exception_group_log_keeps_bounded_member_types_without_messages(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(Settings(_env_file=None), readiness_probe=_ready)  # type: ignore[call-arg]

    @app.get("/group-explode")
    async def group_explode() -> None:
        raise ExceptionGroup(
            "GROUP-SECRET",
            [ValueError("VALUE-SECRET"), RuntimeError("RUNTIME-SECRET")],
        )

    with caplog.at_level(logging.INFO, logger="vmr.http"):
        response = TestClient(app, raise_server_exceptions=False).get("/group-explode")
    assert response.status_code == 500
    event = next(
        json.loads(record.getMessage())
        for record in caplog.records
        if '"event":"http_unhandled_exception"' in record.getMessage()
    )
    assert [item["exception_type"] for item in event["exceptions"]] == [
        "ExceptionGroup",
        "ValueError",
        "RuntimeError",
    ]
    for forbidden in ("GROUP-SECRET", "VALUE-SECRET", "RUNTIME-SECRET"):
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


def test_fastapi_oauth_redirect_receives_the_documentation_csp() -> None:
    response = _client().get("/docs/oauth2-redirect")
    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "https://cdn.jsdelivr.net" in csp
    assert "script-src 'self' 'unsafe-inline'" in csp


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


@pytest.mark.parametrize("host", [" example.com", "example.com ", "example.com\t"])
def test_trusted_host_rejects_boundary_whitespace(host: str) -> None:
    with pytest.raises(ValidationError, match="leading or trailing whitespace"):
        Settings(_env_file=None, trusted_hosts=(host,))  # type: ignore[call-arg]


def test_bracketed_host_port_requires_ascii_decimal_digits() -> None:
    assert _host_from_scope({"headers": [(b"host", b"[::1]:8443")]}) == "[::1]"  # type: ignore[arg-type]
    assert _host_from_scope({"headers": [(b"host", "[::1]:²".encode("latin-1"))]}) is None  # type: ignore[arg-type]


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


# --- Forwarded scheme reaching downstream URL construction --------------------
#
# uvicorn runs with --no-proxy-headers, so nothing rewrites scope["scheme"]
# before the hardening boundary. Starlette builds request.url, request.base_url,
# every redirect and every url_for(...) from that value, so a scheme left at
# "http" behind TLS termination produced absolute http:// asset URLs on pages
# served over HTTPS, which browsers refuse as mixed active content.


def _scheme_probe_app(
    *,
    trusted_proxy_cidrs: tuple[str, ...] = ("10.0.0.0/8",),
    workbench: bool = True,
) -> object:
    """The real application, plus one route that reports what routing saw."""

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        trusted_proxy_cidrs=trusted_proxy_cidrs,
        features=FeatureFlags(workbench=workbench),
    )
    app = create_app(settings, readiness_probe=_ready)  # type: ignore[arg-type]

    @app.get("/__scheme_probe", include_in_schema=False)
    def scheme_probe(request: Request) -> dict[str, str]:
        return {
            "scope_scheme": str(request.scope["scheme"]),
            "url": str(request.url),
            "base_url": str(request.base_url),
            "static_url": str(request.url_for("static", path="app.css")),
        }

    return app


def test_trusted_proxy_scheme_reaches_downstream_request_handling() -> None:
    """The decided scheme must be visible to routing, not only to the log line."""

    client = TestClient(
        _scheme_probe_app(),  # type: ignore[arg-type]
        base_url="http://testserver",
        client=("10.2.3.4", 50000),
    )
    body = client.get("/__scheme_probe", headers={"X-Forwarded-Proto": "https"}).json()

    assert body["scope_scheme"] == "https"
    assert body["url"] == "https://testserver/__scheme_probe"
    assert body["base_url"] == "https://testserver/"


def test_trusted_proxy_scheme_makes_url_for_emit_https_absolute_urls() -> None:
    """url_for is where the defect surfaced: absolute URLs carry the scheme."""

    client = TestClient(
        _scheme_probe_app(),  # type: ignore[arg-type]
        base_url="http://testserver",
        client=("10.2.3.4", 50000),
    )
    body = client.get("/__scheme_probe", headers={"X-Forwarded-Proto": "https"}).json()

    assert body["static_url"] == "https://testserver/static/app.css"
    assert not body["static_url"].startswith("http://")


def test_untrusted_peer_cannot_move_the_request_scheme() -> None:
    """A spoofed X-Forwarded-Proto from a direct caller changes nothing."""

    client = TestClient(
        _scheme_probe_app(trusted_proxy_cidrs=("10.0.0.0/8",)),  # type: ignore[arg-type]
        base_url="http://testserver",
        client=("203.0.113.9", 50000),
    )
    body = client.get("/__scheme_probe", headers={"X-Forwarded-Proto": "https"}).json()

    assert body["scope_scheme"] == "http"
    assert body["url"] == "http://testserver/__scheme_probe"
    assert body["static_url"] == "http://testserver/static/app.css"


def test_trusted_proxy_forwarding_plain_http_leaves_the_scheme_http() -> None:
    """A trusted proxy is trusted in both directions, not only upwards."""

    client = TestClient(
        _scheme_probe_app(),  # type: ignore[arg-type]
        base_url="http://testserver",
        client=("10.2.3.4", 50000),
    )
    body = client.get("/__scheme_probe", headers={"X-Forwarded-Proto": "http"}).json()

    assert body["scope_scheme"] == "http"
    assert body["static_url"] == "http://testserver/static/app.css"


def test_trusted_proxy_with_malformed_forwarded_proto_leaves_the_scheme_alone() -> None:
    """An unusable value falls back to the original scheme, never to a guess."""

    client = TestClient(
        _scheme_probe_app(),  # type: ignore[arg-type]
        base_url="http://testserver",
        client=("10.2.3.4", 50000),
    )
    body = client.get("/__scheme_probe", headers={"X-Forwarded-Proto": "ftp"}).json()

    assert body["scope_scheme"] == "http"


def test_a_real_template_emits_https_asset_urls_behind_tls_termination() -> None:
    """Pin the exact shipped failure to a real template, not a synthetic one.

    `app/web/templates/base.html` is the shell every Workbench page extends, and
    its stylesheet link is the tag Chrome refused. Rendering the real file
    through the real Jinja environment is what makes this regression impossible
    to reintroduce by changing the middleware alone.
    """

    from app.web.routes import templates

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        trusted_proxy_cidrs=("10.0.0.0/8",),
        features=FeatureFlags(workbench=True),
    )
    app = create_app(settings, readiness_probe=_ready)  # type: ignore[arg-type]

    @app.get("/__render_probe", include_in_schema=False)
    def render_probe(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request=request, name="base.html", context={})

    client = TestClient(app, base_url="http://testserver", client=("10.2.3.4", 50000))
    html = client.get("/__render_probe", headers={"X-Forwarded-Proto": "https"}).text

    assert '<link rel="stylesheet" href="https://testserver/static/app.css">' in html
    # The failure mode was an absolute http:// asset URL on an https page. No
    # asset reference may carry it, whatever else the shell renders.
    assert "http://testserver/static/" not in html


def _production_settings(**overrides: object) -> Settings:
    """A production configuration that is safe by the *whole* startup contract.

    The `auth` block is new and is not decoration: `create_app()` now refuses to
    start any hosted environment without a complete hosted-authentication
    boundary, so a fixture that omitted it would fail on the auth contract before
    reaching the runtime rule each test below is actually about. Every assertion
    in this section is unchanged; they now run against a configuration that is
    also authenticated, which is what a real deployment would be.
    """

    values: dict[str, object] = {
        "_env_file": None,
        "app_env": "production",
        "database_url": "postgresql+psycopg://service:password@db.example.com/vmr_prod",
        "trusted_hosts": ("outbound.example.com",),
        "trusted_proxy_cidrs": ("10.20.0.0/24",),
        "dry_run": True,
        "auth": AuthSettings(
            enabled=True,
            session_secret="production-session-secret-at-least-32-chars",
            google_client_id="production-client-id",
            google_client_secret="production-client-secret",
            allowed_operator_emails=("operator@example.com",),
            public_base_url="https://outbound.example.com",
        ),
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


@pytest.mark.parametrize("host", ["127.1", "0177.0.0.1", "2130706433", "0x7f000001"])
def test_production_rejects_legacy_numeric_loopback_database_hosts(host: str) -> None:
    with pytest.raises(RuntimeConfigurationError, match="DATABASE_URL"):
        create_app(
            _production_settings(database_url=f"postgresql+psycopg://u:p@{host}/vmr_prod"),
            readiness_probe=_ready,
        )


def test_default_runtime_settings_preserve_dry_run_and_feature_safety() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.dry_run is True
    assert settings.features.enabled() == []


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


def test_campaign_archive_confirmation_is_csp_compatible() -> None:
    template = Path("app/web/v2/templates/campaigns.html").read_text(encoding="utf-8")
    script = Path("app/web/static/campaigns.js").read_text(encoding="utf-8")
    csp = _client().get("/healthz").headers["content-security-policy"]
    assert "onsubmit=" not in template
    assert "data-archive-confirm=" in template
    assert "window.confirm" in script
    assert "script-src 'self'" in csp
    assert "script-src 'self' 'unsafe-inline'" not in csp


def test_campaign_archive_script_url_is_content_versioned() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        features=FeatureFlags(workbench=True),
    )
    response = _client(settings).get("/app/campaigns")
    assert response.status_code == 200
    assert re.search(r"/static/campaigns\.js\?v=[0-9a-f]{12}", response.text)


def test_smoke_treats_readyz_503_as_database_not_ready(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts import smoke

    responses = iter(
        [
            (200, {"status": "ok"}),
            (
                503,
                {
                    "status": "not_ready",
                    "checks": {"configuration": "ok", "database": "failed"},
                },
            ),
        ]
    )
    monkeypatch.setattr(smoke, "_get", lambda _url: next(responses))
    monkeypatch.setattr(sys, "argv", ["smoke.py", "http://testserver"])
    assert smoke.main() == 1
    output = capsys.readouterr().out
    assert "database not reachable" in output
    assert "/readyz failed" not in output


def test_smoke_get_parses_http_503_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import urllib.error

    from scripts import smoke

    payload = b'{"status":"not_ready","checks":{"database":"failed"}}'

    def not_ready(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise urllib.error.HTTPError(
            "http://testserver/readyz",
            503,
            "Service Unavailable",
            {},
            io.BytesIO(payload),
        )

    monkeypatch.setattr(smoke.urllib.request, "urlopen", not_ready)
    status, body = smoke._get("http://testserver/readyz")
    assert status == 503
    assert body["checks"] == {"database": "failed"}


def test_version_is_benign_and_deployment_provided() -> None:
    assert _client().get("/version").json() == {"version": "unknown"}
    settings = Settings(_env_file=None, release_id="release-2026.08.07+build.4")  # type: ignore[call-arg]
    assert _client(settings).get("/version").json() == {"version": "release-2026.08.07+build.4"}

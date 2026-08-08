"""Central HTTP hardening: correlation, logging, bounds, errors and headers."""

from __future__ import annotations

import json
import logging
import re
import secrets
import traceback
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from time import perf_counter

from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import canonical_trusted_host

REQUEST_ID_HEADER = "X-Request-ID"
MAX_REQUEST_ID_CHARS = 64
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}$")
_request_id_context: ContextVar[str | None] = ContextVar("vmr_request_id", default=None)
_logger = logging.getLogger("vmr.http")

_APPLICATION_CSP = (
    "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
    "form-action 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'"
)
_DOCS_CSP = (
    "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
    "img-src 'self' data: https://fastapi.tiangolo.com; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net"
)


def valid_request_id(value: str) -> bool:
    """Whether a caller-provided correlation ID is safe and bounded."""

    return len(value) <= MAX_REQUEST_ID_CHARS and _REQUEST_ID.fullmatch(value) is not None


def current_request_id() -> str | None:
    """Correlation ID for the current request, when called inside request handling."""

    return _request_id_context.get()


def _header_values(scope: Scope, name: bytes) -> list[str]:
    return [
        raw_value.decode("latin-1")
        for raw_name, raw_value in scope.get("headers", [])
        if raw_name.lower() == name
    ]


def _request_id(scope: Scope) -> str:
    supplied = _header_values(scope, b"x-request-id")
    if len(supplied) == 1 and valid_request_id(supplied[0]):
        return supplied[0]
    return secrets.token_hex(16)


def _parse_ip(value: str | None) -> IPv4Address | IPv6Address | None:
    if not value:
        return None
    try:
        return ip_address(value)
    except ValueError:
        return None


class RequestContext:
    """Conservative peer/proxy interpretation for logs and HTTPS detection."""

    def __init__(self, scope: Scope, trusted_proxy_cidrs: tuple[str, ...]) -> None:
        client = scope.get("client")
        self.peer = _parse_ip(client[0] if client else None)
        self.networks = tuple(ip_network(value, strict=False) for value in trusted_proxy_cidrs)
        self.trusted_proxy = bool(
            self.peer is not None and any(self.peer in network for network in self.networks)
        )
        self.client = self._forwarded_client(scope) if self.trusted_proxy else self.peer
        self.scheme = (
            self._forwarded_scheme(scope)
            if self.trusted_proxy
            else str(scope.get("scheme", "http"))
        )

    def _forwarded_client(self, scope: Scope) -> IPv4Address | IPv6Address | None:
        values = _header_values(scope, b"x-forwarded-for")
        if len(values) != 1:
            return self.peer
        raw_chain = [part.strip() for part in values[0].split(",")]
        chain = [_parse_ip(part) for part in raw_chain]
        if not chain or any(item is None for item in chain):
            return self.peer
        candidates = [item for item in chain if item is not None]
        if self.peer is not None:
            candidates.append(self.peer)
        # Walk from the immediate proxy towards the caller and stop at the
        # first address outside the configured proxy networks. This avoids
        # blindly trusting an attacker-controlled leftmost XFF value.
        for candidate in reversed(candidates):
            if any(candidate in network for network in self.networks):
                continue
            return candidate
        return candidates[0] if candidates else self.peer

    @staticmethod
    def _forwarded_scheme(scope: Scope) -> str:
        values = _header_values(scope, b"x-forwarded-proto")
        if len(values) == 1 and values[0].strip().lower() in {"http", "https"}:
            return values[0].strip().lower()
        return str(scope.get("scheme", "http"))


def _route_name(scope: Scope) -> str:
    route = scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str) and template.startswith("/") and len(template) <= 200:
        return template
    # Unmatched and middleware-rejected paths may contain arbitrary user input.
    return "/<unmatched>"


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _host_from_scope(scope: Scope) -> str | None:
    values = _header_values(scope, b"host")
    if len(values) != 1:
        return None
    raw = values[0]
    if raw.startswith("["):
        closing = raw.find("]")
        if closing < 0:
            return None
        host, suffix = raw[: closing + 1], raw[closing + 1 :]
        if suffix and (
            not suffix.startswith(":") or not suffix[1:].isascii() or not suffix[1:].isdigit()
        ):
            return None
    else:
        if raw.count(":") > 1:
            return None
        host, separator, port = raw.rpartition(":")
        if not separator:
            host = raw
        elif not host or not port.isascii() or not port.isdigit():
            return None
    try:
        return canonical_trusted_host(host)
    except ValueError:
        return None


class CanonicalTrustedHostMiddleware:
    """Exact canonical Host validation with safe local IPv6 support."""

    def __init__(self, app: ASGIApp, *, allowed_hosts: tuple[str, ...]) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        host = _host_from_scope(scope)
        allowed = host is not None and any(
            host == pattern
            or (pattern.startswith("*.") and host.endswith(pattern[1:]) and host != pattern[2:])
            for pattern in self.allowed_hosts
        )
        if not allowed:
            response = PlainTextResponse("Invalid host header", status_code=400)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _safe_exception_event(exc: BaseException, request_id: str) -> dict[str, object]:
    """Bounded stack metadata that never renders exception values or locals."""

    exceptions: list[dict[str, object]] = []
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending and len(exceptions) < 4:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        frames = []
        for frame, line in list(traceback.walk_tb(current.__traceback__))[-16:]:
            module = frame.f_globals.get("__name__", "unknown")
            frames.append(
                {
                    "function": frame.f_code.co_name[:120],
                    "line": line,
                    "module": module[:120] if isinstance(module, str) else "unknown",
                }
            )
        exceptions.append({"exception_type": type(current).__name__[:120], "frames": frames})
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        chained = current.__cause__ or (
            current.__context__ if not current.__suppress_context__ else None
        )
        if chained is not None:
            pending.append(chained)
    return {
        "event": "http_unhandled_exception",
        "exceptions": exceptions,
        "request_id": request_id,
        "timestamp": _timestamp(),
    }


class ProductionHTTPMiddleware:
    """A pure-ASGI boundary that never reads or logs request bodies."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_request_bytes: int,
        trusted_proxy_cidrs: tuple[str, ...],
        hsts_max_age_seconds: int,
    ) -> None:
        self.app = app
        self.max_request_bytes = max_request_bytes
        self.trusted_proxy_cidrs = trusted_proxy_cidrs
        self.hsts_max_age_seconds = hsts_max_age_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started_at = perf_counter()
        request_id = _request_id(scope)
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        token: Token[str | None] = _request_id_context.set(request_id)
        context = RequestContext(scope, self.trusted_proxy_cidrs)
        status_code = 500
        response_started = False

        async def send_hardened(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = int(message["status"])
                headers = list(message.get("headers", []))

                def set_header(name: str, value: str) -> None:
                    raw_name = name.lower().encode("latin-1")
                    nonlocal headers
                    headers = [(key, item) for key, item in headers if key.lower() != raw_name]
                    headers.append((raw_name, value.encode("latin-1")))

                set_header(REQUEST_ID_HEADER, request_id)
                set_header("X-Content-Type-Options", "nosniff")
                set_header("Referrer-Policy", "no-referrer")
                set_header("X-Frame-Options", "DENY")
                set_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
                path = str(scope.get("path", ""))
                set_header(
                    "Content-Security-Policy",
                    _DOCS_CSP
                    if path in {"/docs", "/docs/oauth2-redirect", "/redoc"}
                    else _APPLICATION_CSP,
                )
                if context.scheme == "https" and self.hsts_max_age_seconds > 0:
                    set_header("Strict-Transport-Security", f"max-age={self.hsts_max_age_seconds}")
                if path.startswith("/static/"):
                    set_header("Cache-Control", "public, max-age=3600")
                else:
                    set_header("Cache-Control", "no-store")
                message["headers"] = headers
            await send(message)

        try:
            length_response = self._content_length_response(scope, request_id)
            if length_response is not None:
                await length_response(scope, receive, send_hardened)
            else:
                await self.app(scope, receive, send_hardened)
        except Exception as exc:
            _logger.error(
                json.dumps(
                    _safe_exception_event(exc, request_id),
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            if response_started:
                raise
            response = JSONResponse(
                status_code=500,
                content={
                    "error": "internal_server_error",
                    "message": "The request could not be completed.",
                    "request_id": request_id,
                },
            )
            await response(scope, receive, send_hardened)
        finally:
            duration_ms = round((perf_counter() - started_at) * 1000, 3)
            _logger.info(
                json.dumps(
                    {
                        "client_ip": str(context.client) if context.client else "unknown",
                        "duration_ms": duration_ms,
                        "event": "http_request",
                        "method": str(scope.get("method", "UNKNOWN"))[:16].upper(),
                        "peer_ip": str(context.peer) if context.peer else "unknown",
                        "request_id": request_id,
                        "route": _route_name(scope),
                        "scheme": context.scheme
                        if context.scheme in {"http", "https"}
                        else "unknown",
                        "status_code": status_code,
                        "timestamp": _timestamp(),
                        "trusted_proxy": context.trusted_proxy,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            _request_id_context.reset(token)

    def _content_length_response(self, scope: Scope, request_id: str) -> JSONResponse | None:
        values = _header_values(scope, b"content-length")
        if not values:
            return None
        if (
            len(values) != 1
            or not values[0].isascii()
            or not values[0].isdigit()
            or len(values[0]) > 20
        ):
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_content_length", "request_id": request_id},
            )
        if int(values[0]) > self.max_request_bytes:
            return JSONResponse(
                status_code=413,
                content={"error": "request_too_large", "request_id": request_id},
            )
        return None

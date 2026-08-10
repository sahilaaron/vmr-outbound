"""The authentication boundary.

One pure-ASGI middleware, mounted *inside* the trusted-host check and *inside*
the production hardening boundary. Placement is deliberate and load-bearing:

* Hardening stays outermost, so a 401, a sign-in redirect and a cross-site
  refusal all carry the request ID, the security headers and the access-log line
  exactly like any other response.
* The trusted-host check stays outside this, so a request with a forged ``Host``
  is rejected before any identity is read and before a redirect URL is built
  from that host.
* This middleware runs before routing, so the decision does not depend on a
  route existing. An unmounted path, a 404 and an alternate spelling of a
  protected path are all refused the same way.

The middleware only ever *decides*. It writes its findings into the ASGI scope
state and two context variables; the CSRF dependency, the templates and the
sign-in routes read them from there.
"""

from __future__ import annotations

import json
import time
from contextvars import Token
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import quote

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.auth.config import AuthSettings
from app.core.auth.context import (
    current_operator,
    reset_current_csrf_token,
    reset_current_operator,
    set_current_csrf_token,
    set_current_operator,
)
from app.core.auth.policy import (
    REDIRECTABLE_METHODS,
    is_anonymous_path,
    is_safe_method,
    normalize_request_path,
)
from app.core.auth.session import (
    SESSION_COOKIE_NAME,
    OperatorSession,
    SessionCodec,
    SessionDecodeError,
)

__all__ = [
    "OperatorAuthenticationMiddleware",
    "clear_session_cookie_value",
    "current_operator",
]


def clear_session_cookie_value(*, secure: bool, domain: str | None) -> str:
    """A ``Set-Cookie`` value that removes the session cookie immediately."""

    return _cookie_header(
        SESSION_COOKIE_NAME,
        "",
        max_age=0,
        secure=secure,
        domain=domain,
        same_site="Lax",
    )


def _cookie_header(
    name: str,
    value: str,
    *,
    max_age: int,
    secure: bool,
    domain: str | None,
    same_site: str,
    path: str = "/",
) -> str:
    """One cookie header assembled explicitly rather than by string luck.

    ``HttpOnly`` is unconditional: no page in this application has any reason to
    read an auth cookie from script, and making it unreadable removes the entire
    class of XSS-to-session-theft escalations.
    """

    parts = [
        f"{name}={value}",
        f"Path={path}",
        f"Max-Age={max_age}",
        "HttpOnly",
        f"SameSite={same_site}",
    ]
    if secure:
        parts.append("Secure")
    if domain:
        parts.append(f"Domain={domain}")
    return "; ".join(parts)


def _headers(scope: Scope, name: bytes) -> list[str]:
    return [
        raw_value.decode("latin-1")
        for raw_name, raw_value in scope.get("headers", [])
        if raw_name.lower() == name
    ]


def _cookie(scope: Scope, name: str) -> str | None:
    """The single unambiguous value of one cookie, or ``None``.

    Two ambiguities are refused rather than resolved, because resolving either
    means letting somebody else choose which credential this boundary reads:

    * **More than one ``Cookie`` header.** A request-smuggling shape that must
      not be reassembled here. (An HTTP/2 client that legitimately splits
      cookies therefore appears anonymous; the nginx to uvicorn hop is HTTP/1.1
      and recombines, so no deployed client is affected. Recorded as L-8.)
    * **More than one morsel with the same name.** ``SimpleCookie`` silently
      keeps the *last* of a duplicate name, so an attacker able to set a
      domain-scoped cookie from a sibling host could otherwise decide which of
      two session cookies is honoured — choosing the valid one is a nuisance,
      but "first wins" or "last wins" is a decision no attacker should get to
      make on an authentication boundary.
    """

    values = _headers(scope, b"cookie")
    if len(values) != 1:
        # Zero cookies is the normal anonymous case; more than one Cookie header
        # is a request-smuggling shape that must not be reassembled here.
        return None
    header = values[0]

    # Count occurrences of this *name* before parsing, because parsing collapses
    # them. A quoted value containing `; <name>=` would over-count and be
    # refused, which is the safe direction.
    occurrences = 0
    for chunk in header.split(";"):
        candidate, separator, _ = chunk.partition("=")
        if separator and candidate.strip() == name:
            occurrences += 1
    if occurrences != 1:
        return None

    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(header)
    except Exception:  # pragma: no cover - SimpleCookie is lenient by design
        return None
    morsel = jar.get(name)
    return morsel.value if morsel is not None else None


def _request_origin(scope: Scope) -> str | None:
    """The origin this request was actually made to, per the trusted boundary."""

    hosts = _headers(scope, b"host")
    if len(hosts) != 1:
        return None
    state = scope.get("state") or {}
    scheme = state.get("forwarded_scheme") or scope.get("scheme") or "http"
    return f"{str(scheme).lower()}://{hosts[0].strip().lower()}"


def _is_cross_site(scope: Scope, settings: AuthSettings) -> bool:
    """Whether *any* supplied signal says this unsafe request is not same-site.

    Every relevant signal is evaluated and any one of them can refuse. That is
    the whole rule, and it is deliberately not a priority order: an earlier
    signal saying "same-origin" must never be able to neutralise a later one
    saying "evil.example". Two positive signals that disagree are themselves a
    reason to refuse, so the safe direction is to OR the refusals rather than
    consult the first header that happens to be present.

    Only a *positive* signal refuses. Absent headers fall through to the
    per-session token check, which fails closed on its own — that is what keeps
    a non-browser client (a script holding a valid token) working while a real
    browser, which always sends ``Origin`` on a cross-site form post, is stopped
    at this layer before the body is read.

    Duplicated headers are ambiguity, and ambiguity refuses. A front end or
    proxy that emits ``Origin`` twice would otherwise silently disable this
    entire layer, because "not exactly one" used to read as "absent".
    """

    fetch_site = _headers(scope, b"sec-fetch-site")
    if len(fetch_site) > 1:
        return True
    if len(fetch_site) == 1 and fetch_site[0].strip().lower() not in {"same-origin", "none"}:
        return True

    origins = _headers(scope, b"origin")
    if len(origins) > 1:
        return True
    if len(origins) == 1:
        presented = origins[0].strip().lower()
        if presented in {"", "null"}:
            # An opaque origin is not this site. A sandboxed frame or a document
            # loaded from a `data:` URL both produce it, and neither should write.
            return True
        accepted = {value for value in (_request_origin(scope), settings.public_base_url) if value}
        if presented not in accepted:
            return True

    return False


class OperatorAuthenticationMiddleware:
    """Refuse every request that is not an approved internal VMR operator."""

    def __init__(self, app: ASGIApp, *, settings: AuthSettings) -> None:
        self.app = app
        self.settings = settings
        self.codec = (
            SessionCodec(settings.session_secret or "")
            if settings.enabled and settings.has_session_secret()
            else None
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state: dict[str, Any] = scope.setdefault("state", {})

        if not self.settings.enabled or self.codec is None:
            # Local development and any deployment that has not turned hosted
            # authentication on. Nothing is enforced and nothing is recorded, so
            # the CSRF dependency and `csrf_field()` stay inert.
            state["auth_enforced"] = False
            await self.app(scope, receive, send)
            return

        state["auth_enforced"] = True
        now = int(time.time())
        session, revoked = self._resolve_session(scope, now=now)

        state["auth_credential"] = "cookie" if session is not None else None
        state["operator_email"] = session.email if session is not None else None
        state["csrf_token"] = self.codec.csrf_token(session.session_id) if session else None

        operator_token = set_current_operator(session)
        csrf_token: Token[str | None] = set_current_csrf_token(state["csrf_token"])
        try:
            method = str(scope.get("method", "GET")).upper()
            path = str(scope.get("path", "/"))

            if session is None and not is_anonymous_path(path):
                await self._refuse_anonymous(scope, send, method=method, revoked=revoked)
                return

            if not is_safe_method(method) and _is_cross_site(scope, self.settings):
                await self._respond(
                    scope,
                    send,
                    status=403,
                    error="cross_site_request_refused",
                    message="This request did not originate from the VMR application.",
                )
                return

            await self.app(scope, receive, send)
        finally:
            reset_current_csrf_token(csrf_token)
            reset_current_operator(operator_token)

    # --- internals ----------------------------------------------------------

    def _resolve_session(self, scope: Scope, *, now: int) -> tuple[OperatorSession | None, bool]:
        """Decode the cookie and re-apply the approval policy.

        Approval is re-checked on every request, not just at sign-in. That is
        what makes removing an address from the allow-list an immediate
        revocation of that operator's existing session, and it is the reason a
        server-side session table earns its keep nowhere in this design.
        """

        assert self.codec is not None
        raw = _cookie(scope, SESSION_COOKIE_NAME)
        if raw is None:
            return None, False
        try:
            session = self.codec.decode_session(raw, now=now)
        except SessionDecodeError:
            # Forged, malformed or expired are all one outcome: no session. The
            # stale cookie is cleared so the browser stops presenting it.
            return None, True
        if not self.settings.is_approved(session.email):
            return None, True
        return session, False

    async def _refuse_anonymous(
        self, scope: Scope, send: Send, *, method: str, revoked: bool
    ) -> None:
        clear = (
            clear_session_cookie_value(
                secure=self.settings.cookie_secure, domain=self.settings.cookie_domain
            )
            if revoked
            else None
        )

        if method in REDIRECTABLE_METHODS and _prefers_html(scope):
            target = "/auth/login"
            destination = _requested_target(scope)
            if destination:
                target = f"/auth/login?next={quote(destination, safe='')}"
            await self._respond(
                scope,
                send,
                status=303,
                error="authentication_required",
                message="Sign in to continue.",
                location=target,
                set_cookie=clear,
            )
            return

        # Never redirect a write or an API call: a 303 on a POST would be
        # followed as a GET and look like a success to a client that cannot see
        # the address bar.
        await self._respond(
            scope,
            send,
            status=401,
            error="unauthorized",
            message="An approved VMR operator session is required.",
            set_cookie=clear,
        )

    async def _respond(
        self,
        scope: Scope,
        send: Send,
        *,
        status: int,
        error: str,
        message: str,
        location: str | None = None,
        set_cookie: str | None = None,
    ) -> None:
        body = json.dumps({"error": error, "status": status, "message": message}).encode("utf-8")
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            # The decision depends on the request's own cookies and fetch
            # metadata; a shared cache must never reuse one operator's answer.
            (b"vary", b"Cookie, Accept, Origin, Sec-Fetch-Site"),
        ]
        if location is not None:
            headers.append((b"location", location.encode("latin-1")))
        if set_cookie is not None:
            headers.append((b"set-cookie", set_cookie.encode("latin-1")))
        start: Message = {"type": "http.response.start", "status": status, "headers": headers}
        await send(start)
        await send({"type": "http.response.body", "body": body})


def _prefers_html(scope: Scope) -> bool:
    """Whether this looks like a browser navigation rather than an API call."""

    accepts = _headers(scope, b"accept")
    if len(accepts) != 1:
        return False
    value = accepts[0].lower()
    if "text/html" not in value:
        return False
    mode = _headers(scope, b"sec-fetch-mode")
    if len(mode) == 1 and mode[0].strip().lower() != "navigate":
        # `fetch()` from a page sends `Accept: */*` by default but can be told
        # to ask for HTML; a non-navigation must still get the JSON refusal.
        return False
    return True


def _requested_target(scope: Scope) -> str | None:
    """The normalised path (with query) to return to after signing in."""

    path = normalize_request_path(str(scope.get("path", "/")))
    if path == "/":
        return None
    raw_query = scope.get("query_string") or b""
    query = raw_query.decode("latin-1") if isinstance(raw_query, bytes) else str(raw_query)
    if any(character in path for character in ("\r", "\n")):
        return None
    if query and not any(character in query for character in ("\r", "\n")):
        return f"{path}?{query}"
    return path

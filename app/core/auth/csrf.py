"""Per-session CSRF tokens for cookie-authenticated browser writes.

The mechanism has two independent layers, and a cross-site write has to defeat
both:

1. **An origin backstop in the middleware.** Every unsafe method is refused when
   the request carries a positive cross-site signal (``Sec-Fetch-Site`` that is
   not same-origin/none, or an ``Origin`` that is not this site). This layer
   needs no cooperation from a route or a template, so a router added later
   cannot forget it.
2. **A per-session token on the request.** Derived from the session identifier
   with a dedicated key (see ``session.SessionCodec.csrf_token``), compared in
   constant time, required on every cookie-authenticated unsafe request, and
   accepted from the ``_csrf`` form field or the ``X-CSRF-Token`` header.

Layer 1 alone would already stop a browser-driven cross-site POST; layer 2 is
what makes the refusal hold for a client that simply omits the headers. Both
fail closed.

Templates never have to thread a token through a view context: ``csrf_field()``
is a Jinja global that reads the request-scoped token, so a form only ever writes
``{{ csrf_field() }}``. When authentication is disabled — which is the default,
and what local development uses — the global renders nothing and the dependency
is a no-op, so local behaviour is byte-identical to before this slice.
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import Request
from jinja2 import Environment
from markupsafe import Markup

from app.core.auth.context import current_csrf_token, signed_in_operator_email
from app.core.auth.templating import install_csrf_form_extension

CSRF_FIELD_NAME = "_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"

_FORM_CONTENT_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


class CsrfError(Exception):
    """Raised when a cookie-authenticated write arrives without a valid token."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def csrf_field() -> Markup:
    """The hidden input every state-changing form carries.

    Returns empty markup when authentication is disabled, so the same template
    serves local development unchanged.
    """

    token = current_csrf_token()
    if not token:
        return Markup("")
    return Markup(f'<input type="hidden" name="{CSRF_FIELD_NAME}" value="{token}">')


def register_csrf(environment: Environment) -> None:
    """Wire one Jinja environment for CSRF, in two parts.

    The globals make ``csrf_field()`` callable from a template; the extension
    (see ``app/core/auth/templating.py``) inserts that call into every POST form
    at compile time, so no template has to carry the field by hand and none can
    be forgotten. Every ``Jinja2Templates`` instance in the application calls
    this, next to ``register_neutralize``.

    Registering a *global* rather than a context variable is what keeps the four
    independent ``_render`` helpers, the shared partials and the HTML fragments
    returned by POST handlers all correct without threading a value through
    several hundred call sites.
    """

    environment.globals["csrf_field"] = csrf_field
    environment.globals["csrf_token"] = current_csrf_token
    environment.globals["signed_in_operator_email"] = signed_in_operator_email
    install_csrf_form_extension(environment)


def csrf_token_for_request(request: Request) -> str | None:
    """The token bound to this request, read from the ASGI scope state."""

    state = request.scope.get("state") or {}
    token = state.get("csrf_token")
    return token if isinstance(token, str) else None


async def require_csrf(request: Request) -> None:
    """Refuse a cookie-authenticated unsafe request without a valid token.

    Installed as a router-level dependency on every router that serves a
    state-changing route, so it applies before any handler body runs.
    """

    if request.method.upper() in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return

    state: dict[str, Any] = request.scope.get("state") or {}
    if not state.get("auth_enforced"):
        # Authentication disabled (local development): unchanged behaviour.
        return
    if state.get("auth_credential") != "cookie":
        # Reserved for a future non-cookie credential — a bearer token is not
        # attached by the browser automatically and therefore is not subject to
        # cross-site request forgery. Nothing issues one today.
        return

    expected = state.get("csrf_token")
    if not isinstance(expected, str) or not expected:
        raise CsrfError("this request has no CSRF token to check against")

    presented = await _presented_token(request)
    if not presented or not hmac.compare_digest(expected, presented):
        raise CsrfError("the CSRF token is missing or does not match this session")


async def _presented_token(request: Request) -> str | None:
    header_value = request.headers.get(CSRF_HEADER_NAME)
    if header_value:
        return header_value

    content_type = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if content_type not in _FORM_CONTENT_TYPES:
        # A JSON or binary body carries the token in the header. Reading the
        # stream here would consume a body the route still needs.
        return None

    # Starlette caches the parsed form on the request, and FastAPI hands the
    # same request object to the endpoint, so the route's own `request.form()`
    # or `Form(...)` parameters still see it.
    form = await request.form()
    value = form.get(CSRF_FIELD_NAME)
    return value if isinstance(value, str) else None

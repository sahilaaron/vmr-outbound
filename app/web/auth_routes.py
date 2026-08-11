"""The sign-in surface: the only routes an anonymous caller may reach.

Five routes, and nothing that touches the database. Authentication deliberately
has no database dependency, so a readiness blip cannot lock operators out of the
screens they would use to diagnose it.

The flow is the OAuth 2.0 authorization-code flow with PKCE:

``GET  /auth/login``          the VMR sign-in page (no application data on it).
``GET  /auth/google/start``   mints the transaction and redirects to Google.
``GET  /auth/callback``       validates everything, then mints the session.
``POST /auth/logout``         clears the session (CSRF-protected when signed in).
``GET  /auth/signed-out``     the confirmation landing page.

The single-use transaction cookie carries the ``state``, the ``nonce``, the PKCE
verifier and the post-sign-in destination. Keeping all four in one signed,
short-lived, ``HttpOnly`` cookie means the browser proves it started this exact
sign-in — a callback replayed into a different browser, or replayed twice into
the same one, has no transaction to match and is refused.
"""

from __future__ import annotations

import hmac
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.core.auth.csrf import register_csrf, require_csrf
from app.core.auth.google import (
    GoogleIdentityProvider,
    code_challenge_for,
    generate_code_verifier,
)
from app.core.auth.identity import (
    IdentityAssertionError,
    IdentityProvider,
    validate_identity_claims,
)
from app.core.auth.middleware import clear_session_cookie_value, current_operator
from app.core.auth.policy import safe_next_path
from app.core.auth.session import (
    LOGIN_TRANSACTION_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    OperatorSession,
    SessionCodec,
    SessionDecodeError,
    new_session_id,
)
from app.core.config import Settings, get_settings

router = APIRouter(prefix="/auth", tags=["auth"], include_in_schema=False)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates" / "auth"))
register_csrf(templates.env)

DEFAULT_DESTINATION = "/app"

# The provider is resolved once per process and stored on the app state by
# `create_app`, so a test can inject a deterministic stub without a network.
IDENTITY_PROVIDER_STATE_KEY = "vmr_identity_provider"


def _settings(request: Request) -> Settings:
    configured = getattr(request.app.state, "vmr_settings", None)
    return configured if isinstance(configured, Settings) else get_settings()


def _codec(settings: Settings) -> SessionCodec:
    return SessionCodec(settings.auth.session_secret or "")


def _provider(request: Request, settings: Settings) -> IdentityProvider:
    configured: IdentityProvider | None = getattr(
        request.app.state, IDENTITY_PROVIDER_STATE_KEY, None
    )
    if configured is not None:
        return configured
    return GoogleIdentityProvider(settings.auth)


def _render(
    request: Request, template: str, context: dict[str, Any], *, status_code: int = 200
) -> Response:
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={"app_name": _settings(request).app_name, **context},
        status_code=status_code,
    )


@router.get("/login")
def sign_in_page(request: Request, next: str | None = None) -> Response:
    """The sign-in screen. Carries no application data of any kind."""

    settings = _settings(request)
    if not settings.auth.enabled:
        # Nothing to sign in to. Sending the caller to the app is friendlier
        # than a page explaining a feature that is off.
        return RedirectResponse(DEFAULT_DESTINATION, status_code=303)
    if current_operator() is not None:
        return RedirectResponse(safe_next_path(next, fallback=DEFAULT_DESTINATION), status_code=303)
    destination = safe_next_path(next, fallback=DEFAULT_DESTINATION)
    return _render(
        request,
        "sign_in.html",
        {
            "start_url": f"/auth/google/start?next={_quote(destination)}",
            "configured": settings.auth.has_google_client(),
        },
    )


@router.get("/google/start")
def start_google_sign_in(request: Request, next: str | None = None) -> Response:
    """Mint one sign-in transaction and hand the browser to Google."""

    settings = _settings(request)
    auth = settings.auth
    redirect_uri = auth.redirect_uri()
    if not auth.enabled or not auth.has_google_client() or redirect_uri is None:
        # Unreachable in a correctly configured deployment: the startup contract
        # refuses to boot without these. Handled anyway so a misconfiguration is
        # a clear page rather than a traceback.
        return _render(request, "unavailable.html", {}, status_code=503)

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = generate_code_verifier()
    destination = safe_next_path(next, fallback=DEFAULT_DESTINATION)
    now = int(time.time())

    transaction = _codec(settings).encode_login_transaction(
        {
            "state": state,
            "nonce": nonce,
            "verifier": verifier,
            "next": destination,
            "exp": now + auth.login_transaction_max_age_seconds,
        }
    )

    provider = _provider(request, settings)
    target = provider.authorization_url(
        redirect_uri=redirect_uri,
        state=state,
        nonce=nonce,
        code_challenge=code_challenge_for(verifier),
    )

    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        LOGIN_TRANSACTION_COOKIE_NAME,
        transaction,
        max_age=auth.login_transaction_max_age_seconds,
        httponly=True,
        secure=auth.cookie_secure,
        samesite="lax",
        path="/auth",
        domain=auth.cookie_domain,
    )
    return response


@router.get("/callback")
async def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    """Validate the whole round trip, then mint the operator session."""

    settings = _settings(request)
    auth = settings.auth
    redirect_uri = auth.redirect_uri()
    if not auth.enabled or redirect_uri is None:
        return _render(request, "unavailable.html", {}, status_code=503)

    codec = _codec(settings)
    now = int(time.time())

    # The transaction cookie is consumed regardless of outcome. One sign-in
    # attempt, one transaction: this is what makes a replayed callback fail.
    raw_transaction = request.cookies.get(LOGIN_TRANSACTION_COOKIE_NAME)

    if error is not None or not code or not state:
        return _denied(request, auth, reason="sign_in_cancelled")

    try:
        transaction = codec.decode_login_transaction(raw_transaction, now=now)
    except SessionDecodeError:
        return _denied(request, auth, reason="expired_request")

    expected_state = transaction.get("state")
    if not isinstance(expected_state, str) or not _constant_equal(expected_state, state):
        return _denied(request, auth, reason="invalid_request")

    nonce = transaction.get("nonce")
    verifier = transaction.get("verifier")
    if not isinstance(nonce, str) or not isinstance(verifier, str):
        return _denied(request, auth, reason="invalid_request")

    provider = _provider(request, settings)
    try:
        claims = await provider.exchange_code(
            code=code, redirect_uri=redirect_uri, code_verifier=verifier
        )
        email = validate_identity_claims(
            claims,
            client_id=auth.google_client_id or "",
            accepted_issuers=auth.google_issuers,
            expected_nonce=nonce,
            now=now,
        )
    except IdentityAssertionError:
        return _denied(request, auth, reason="invalid_identity")

    if not auth.is_approved(email):
        # A real, fully verified Google identity that is simply not approved.
        # The page says so plainly and shows nothing else.
        return _denied(request, auth, reason="not_approved", email=email)

    session = OperatorSession(
        email=email,
        subject=claims.subject,
        # Never taken from a request field: this is the provider's own claim.
        display_name=claims.display_name[:120],
        # A fresh identifier on every sign-in — the session rotation.
        session_id=new_session_id(),
        issued_at=now,
        expires_at=now + auth.session_max_age_seconds,
    )

    destination = safe_next_path(
        transaction.get("next") if isinstance(transaction.get("next"), str) else None,
        fallback=DEFAULT_DESTINATION,
    )
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        codec.encode_session(session),
        max_age=auth.session_max_age_seconds,
        httponly=True,
        secure=auth.cookie_secure,
        samesite="lax",
        path="/",
        domain=auth.cookie_domain,
    )
    _consume_transaction(response, auth)
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    """Clear the session. CSRF-protected whenever there is one to protect."""

    settings = _settings(request)
    if current_operator() is not None:
        # Only meaningful with a live session; without one there is nothing an
        # attacker could forge away, and demanding a token would strand an
        # operator whose session had already expired.
        await require_csrf(request)
    response = RedirectResponse("/auth/signed-out", status_code=303)
    response.headers.append(
        "set-cookie",
        clear_session_cookie_value(
            secure=settings.auth.cookie_secure, domain=settings.auth.cookie_domain
        ),
    )
    _consume_transaction(response, settings.auth)
    return response


@router.get("/signed-out")
def signed_out(request: Request) -> Response:
    return _render(request, "signed_out.html", {})


def _denied(request: Request, auth: Any, *, reason: str, email: str | None = None) -> Response:
    """Refuse a sign-in without leaking anything about the deployment.

    The page never names the allow-list, never says whether the address exists
    in it, and shows no operator data. A stale session cookie is cleared on the
    way out so a refused browser stops presenting one.
    """

    response = _render(
        request,
        "denied.html",
        {"reason": reason, "email": email or ""},
        status_code=403,
    )
    response.headers.append(
        "set-cookie",
        clear_session_cookie_value(secure=auth.cookie_secure, domain=auth.cookie_domain),
    )
    _consume_transaction(response, auth)
    return response


def _consume_transaction(response: Response, auth: Any) -> None:
    """Delete the single-use sign-in transaction cookie."""

    response.delete_cookie(
        LOGIN_TRANSACTION_COOKIE_NAME,
        path="/auth",
        domain=auth.cookie_domain,
        httponly=True,
        secure=auth.cookie_secure,
        samesite="lax",
    )


def _constant_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _quote(value: str) -> str:
    return quote(value, safe="")

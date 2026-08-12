"""The sign-in surface: the only routes an anonymous caller may reach.

Two ways in, one account behind them:

``GET  /auth/login``          the VMR sign-in page — password form and Google.
``POST /auth/password``       email/password sign-in. Rate-limited.
``GET  /auth/google/start``   mints the OAuth transaction and redirects to Google.
``GET  /auth/callback``       validates everything, then mints the session.
``GET  /auth/setup``          the first-login password form, opened from a link.
``POST /auth/setup``          consumes the link and stores the password hash.
``POST /auth/logout``         clears the session (CSRF-protected when signed in).
``GET  /auth/signed-out``     the confirmation landing page.

Google flow
-----------
The OAuth 2.0 authorization-code flow with PKCE. The single-use transaction
cookie carries the ``state``, the ``nonce``, the PKCE verifier and the
post-sign-in destination. Keeping all four in one signed, short-lived,
``HttpOnly`` cookie means the browser proves it started this exact sign-in — a
callback replayed into a different browser, or replayed twice into the same one,
has no transaction to match and is refused.

What changed with user accounts (#270)
--------------------------------------
Google still proves *identity* exactly as before — same scopes, same audience,
issuer, nonce and ``email_verified`` checks, same refusals. What changed is what
happens after: the validated address is resolved against the ``users`` table
instead of against a configuration allow-list, and a fully valid Google identity
with no active account is refused. Both sign-in paths mint the same session for
the same account row, so nobody ends up with two identities.

These routes now touch the database, which the previous slice deliberately
avoided. The reason is unavoidable — access is granted by an account record — and
the failure is handled explicitly rather than as a traceback: a login attempted
while the database is unreachable renders the sign-in page with a "try again"
notice, and the probes, the sign-in page itself and the static assets stay
database-free so a deployment can still be diagnosed.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.auth import passwords
from app.core.auth.accounts import AccountSnapshot, snapshot_of
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
from app.core.auth.passwords import PASSWORD_RULES, PasswordPolicyError
from app.core.auth.policy import safe_next_path
from app.core.auth.ratelimit import LoginRateLimiter, client_fingerprint
from app.core.auth.session import (
    LOGIN_TRANSACTION_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    OperatorSession,
    SessionCodec,
    SessionDecodeError,
    new_session_id,
)
from app.core.config import Settings, get_settings
from app.services.users import service as user_service
from app.services.users import tokens as token_service

_logger = logging.getLogger("vmr.auth")

router = APIRouter(prefix="/auth", tags=["auth"], include_in_schema=False)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates" / "auth"))
register_csrf(templates.env)

DEFAULT_DESTINATION = "/app"

# The provider is resolved once per process and stored on the app state by
# `create_app`, so a test can inject a deterministic stub without a network.
IDENTITY_PROVIDER_STATE_KEY = "vmr_identity_provider"

# One limiter per process, shared by every request. Module scope rather than app
# state so that it survives a `create_app` in a test without the test having to
# know it exists; `reset()` is available for the tests that do.
login_rate_limiter = LoginRateLimiter()

#: The single message every failed password sign-in produces. One string, used on
#: every refusal path, is what makes the endpoint non-enumerating: an unknown
#: address, a wrong password, a disabled account and an account whose password has
#: never been set are indistinguishable from outside.
SIGN_IN_REFUSED_MESSAGE = (
    "That email address and password combination was not recognised, or the "
    "account cannot sign in with a password. Check with your VMR administrator "
    "if you have not set a password yet."
)


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
    return _sign_in_response(request, next=next)


def _sign_in_response(
    request: Request,
    *,
    next: str | None,
    error: str | None = None,
    email: str = "",
    status_code: int = 200,
) -> Response:
    """The sign-in page, with or without a refusal on it.

    One helper so that the failed-login render and the first render cannot drift
    apart — in particular so that a refusal never accidentally ships a different
    set of form attributes than the page a browser's password manager first saw.
    """

    settings = _settings(request)
    destination = safe_next_path(next, fallback=DEFAULT_DESTINATION)
    return _render(
        request,
        "sign_in.html",
        {
            "start_url": f"/auth/google/start?next={_quote(destination)}",
            "configured": settings.auth.has_google_client(),
            "next": destination,
            "error": error,
            "email": email,
        },
        status_code=status_code,
    )


@router.post("/password")
def password_sign_in(
    request: Request,
    email: str = Form(default=""),
    password: str = Form(default=""),
    next: str = Form(default=DEFAULT_DESTINATION),
    session: Session = Depends(get_db),
) -> Response:
    """Sign in with an email address and a password.

    Every refusal below renders the *same* page with the *same* message and the
    same 401. The distinctions the server makes internally — unknown address,
    wrong password, disabled account, password never set — exist for the log and
    for the rate limiter, and are deliberately invisible to the caller. An
    endpoint that says "no such user" is an account-enumeration API with a login
    form on top of it.

    Timing is equalised in the service layer: an address with no account still
    pays for one Argon2id verification, so the two cases cannot be told apart by
    a stopwatch either.
    """

    settings = _settings(request)
    if not settings.auth.enabled:
        return RedirectResponse(DEFAULT_DESTINATION, status_code=303)

    now = int(time.time())
    normalized = _normalized_form_email(email)
    # The caller's address as the hardening boundary resolved it, not the raw
    # ASGI peer: behind nginx the peer is always 127.0.0.1, so keying on it would
    # put the whole deployment in one bucket that any anonymous caller could
    # exhaust.
    client = client_fingerprint(request.scope.get("state") or {})
    # Bounded before it reaches Argon2. `validate_password` enforces the ceiling
    # on the setup path, but this path does not validate — it verifies — so
    # without this an attacker could post megabytes and get two things for free:
    # server CPU per unauthenticated request, and a timing oracle, because the
    # no-account branch spends a *fixed* dummy verification while this one would
    # scale with the input. Truncating one character past the maximum keeps an
    # over-long value a mismatch rather than silently making it a shorter,
    # possibly-correct password.
    presented = password[: passwords.MAX_PASSWORD_CHARS + 1]

    if login_rate_limiter.is_blocked(email=normalized, client=client, now=now):
        # Throttled, never locked: this clears itself when the window rolls over,
        # so an attacker cannot use it to keep a named colleague out.
        wait = login_rate_limiter.retry_after_seconds(email=normalized, client=client, now=now)
        response = _sign_in_response(
            request,
            next=next,
            error=(
                "Too many sign-in attempts. Wait about "
                f"{max(1, round(wait / 60))} minute(s) and try again."
            ),
            email=email.strip()[:320],
            status_code=429,
        )
        response.headers["Retry-After"] = str(max(1, wait))
        return response

    try:
        outcome = user_service.authenticate_password(session, email=normalized, password=presented)
    except Exception:
        # The account directory is unreachable. Say so plainly rather than
        # rendering a refusal that would tell somebody their password was wrong
        # when it was not.
        _logger.warning('{"event":"password_login_unavailable"}')
        return _sign_in_response(
            request,
            next=next,
            error="Sign-in is temporarily unavailable. Try again in a moment.",
            email=email.strip()[:320],
            status_code=503,
        )

    if not outcome.succeeded or outcome.snapshot is None:
        login_rate_limiter.record_failure(email=normalized, client=client, now=now)
        # The reason reaches the log and nothing else. No address is logged: the
        # log line is for spotting a campaign, not for building one.
        _logger.info('{"event":"password_login_refused","reason":"%s"}', outcome.reason or "")
        return _sign_in_response(
            request,
            next=next,
            error=SIGN_IN_REFUSED_MESSAGE,
            email=email.strip()[:320],
            status_code=401,
        )

    login_rate_limiter.record_success(email=normalized, now=now)
    return _signed_in_redirect(
        request,
        settings=settings,
        account=outcome.snapshot,
        subject="",
        destination=safe_next_path(next, fallback=DEFAULT_DESTINATION),
        now=now,
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


def _signed_in_redirect(
    request: Request,
    *,
    settings: Settings,
    account: AccountSnapshot,
    subject: str,
    destination: str,
    now: int,
) -> Response:
    """Mint one session cookie for one account and send the browser onward.

    Both sign-in paths end here, which is the point: a password sign-in and a
    Google sign-in for the same person produce the same session, carrying the
    same ``user_id`` and the same ``auth_version``. There is no second kind of
    session and no second set of rules about what one means.

    ``auth_version`` is copied from the account *as read during this sign-in*.
    That is what binds the cookie to a revocation generation: the next time an
    administrator disables the account or its password is reset, the counter
    moves and this cookie stops verifying.
    """

    auth = settings.auth
    session_payload = OperatorSession(
        email=account.email,
        subject=subject,
        display_name=account.display_name[:120],
        # A fresh identifier on every sign-in — the session rotation, which also
        # rotates the derived CSRF token.
        session_id=new_session_id(),
        issued_at=now,
        expires_at=now + auth.session_max_age_seconds,
        user_id=str(account.user_id),
        auth_version=account.auth_version,
    )
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        _codec(settings).encode_session(session_payload),
        max_age=auth.session_max_age_seconds,
        httponly=True,
        secure=auth.cookie_secure,
        samesite="lax",
        path="/",
        domain=auth.cookie_domain,
    )
    _consume_transaction(response, auth)
    return response


@router.get("/callback")
async def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    session: Session = Depends(get_db),
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

    # Identity is proved. Authorization is a separate question with a separate
    # answer, and the answer is a row in `users` — never the fact that Google was
    # willing to authenticate somebody. An unknown, disabled or ambiguously
    # linked identity is refused here with nothing created.
    try:
        user = user_service.resolve_google_identity(
            session,
            email=email,
            subject=claims.subject,
            display_name=claims.display_name,
        )
    except Exception:
        _logger.warning('{"event":"google_login_directory_unavailable"}')
        return _denied(request, auth, reason="unavailable")

    if user is None:
        # A real, fully verified Google identity with no active VMR account.
        # The page says so plainly and shows nothing else.
        return _denied(request, auth, reason="not_approved", email=email)

    destination = safe_next_path(
        transaction.get("next") if isinstance(transaction.get("next"), str) else None,
        fallback=DEFAULT_DESTINATION,
    )
    return _signed_in_redirect(
        request,
        settings=settings,
        account=snapshot_of(user),
        subject=claims.subject,
        destination=destination,
        now=now,
    )


@router.get("/setup")
def password_setup_page(
    request: Request,
    token: str = "",
    session: Session = Depends(get_db),
) -> Response:
    """The first-login password form, opened from an administrator's link.

    The link is validated but **not** consumed here. Consuming on ``GET`` would
    mean a link preview in a chat client, a mail scanner or a browser prefetch
    silently burned somebody's only way in, and they would then need an
    administrator before they could even try.
    """

    settings = _settings(request)
    if not settings.auth.enabled:
        return RedirectResponse(DEFAULT_DESTINATION, status_code=303)

    try:
        _, user = token_service.resolve_token(session, token)
    except token_service.CredentialTokenError:
        return _render(request, "password_setup_invalid.html", {}, status_code=400)
    except Exception:
        _logger.warning('{"event":"password_setup_directory_unavailable"}')
        return _render(request, "unavailable.html", {}, status_code=503)

    return _render(
        request,
        "password_setup.html",
        {
            "token": token,
            "email": user.email_normalized,
            "is_reset": user.has_password,
            "minimum_characters": PASSWORD_RULES.minimum_characters,
            "error": None,
        },
    )


@router.post("/setup")
def complete_password_setup(
    request: Request,
    token: str = Form(default=""),
    password: str = Form(default=""),
    password_confirm: str = Form(default=""),
    session: Session = Depends(get_db),
) -> Response:
    """Consume the link, store the hash, and send the person to sign in.

    Three properties of this handler are load-bearing and none of them is
    obvious from the outside:

    * **It does not sign anybody in.** Setting a password proves possession of a
      link, not of the password — the person types it once and could have
      mistyped both fields identically. Requiring a real sign-in immediately
      afterwards makes the credential prove itself, and it means a link that
      leaked cannot be turned into a live session in one step.
    * **A rejected password does not burn the link.** Validation happens after
      the token resolves and before it is consumed, so "fourteen characters" is a
      retry rather than a support request.
    * **It invalidates earlier sessions.** Setting or resetting a password bumps
      the account's ``auth_version``, so anything already signed in as that
      account stops working.
    """

    settings = _settings(request)
    if not settings.auth.enabled:
        return RedirectResponse(DEFAULT_DESTINATION, status_code=303)

    def _reject(message: str, *, status_code: int = 400) -> Response:
        try:
            _, user = token_service.resolve_token(session, token)
        except token_service.CredentialTokenError:
            return _render(request, "password_setup_invalid.html", {}, status_code=400)
        except Exception:
            # Same branch the GET has. Without it, a database that drops between
            # rendering the form and submitting a password that fails the policy
            # turns a retryable refusal into a 500.
            _logger.warning('{"event":"password_setup_directory_unavailable"}')
            return _render(request, "unavailable.html", {}, status_code=503)
        return _render(
            request,
            "password_setup.html",
            {
                "token": token,
                "email": user.email_normalized,
                "is_reset": user.has_password,
                "minimum_characters": PASSWORD_RULES.minimum_characters,
                "error": message,
            },
            status_code=status_code,
        )

    if password != password_confirm:
        return _reject("The two passwords do not match.")

    try:
        user_service.complete_password_setup(session, raw_token=token, new_password=password)
    except token_service.CredentialTokenError:
        # Expired, replayed, superseded, or the account was disabled between the
        # form being rendered and submitted. One outcome, one page.
        return _render(request, "password_setup_invalid.html", {}, status_code=400)
    except PasswordPolicyError as exc:
        return _reject(str(exc))
    except Exception:
        _logger.warning('{"event":"password_setup_directory_unavailable"}')
        return _render(request, "unavailable.html", {}, status_code=503)

    return _render(request, "password_setup_done.html", {})


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


def _normalized_form_email(raw: str) -> str:
    """The comparable form of a typed address.

    The *same* function the configured allow-list, the Google claim and the
    account column all go through. One rule everywhere is what stops
    ``Sahil@VMR.example`` typed into the form failing to match the account
    created from ``sahil@vmr.example``, and equally what stops a lookalike
    address matching one that was never configured.

    Returns ``""`` for an unusable address, which the login path treats exactly
    like an address that has no account: the same message, the same status, and
    the same Argon2id work spent before answering.
    """

    from app.core.auth.config import normalize_operator_email

    return normalize_operator_email(raw[:400])


def _quote(value: str) -> str:
    return quote(value, safe="")

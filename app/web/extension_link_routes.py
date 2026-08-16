"""The account-linking surface: how an extension becomes an authorized client.

Four routes, and the shape of the whole feature is visible in who may call them.

``GET  /extension/authorize``   a signed-in operator's page. Anonymous callers are
                                sent to ``/auth/login?next=…`` by the default-deny
                                middleware — that redirect *is* the one "Sign in
                                to VMR Outbound" action the product is allowed to
                                ask for, and there is deliberately no anonymous
                                path here to make it unnecessary.
``POST /extension/authorize``   the consent button. CSRF-protected, session
                                authenticated, mints one 60-second code.
``POST /extension/token``       the public-client endpoint. No cookie: it is
                                authorised by a single-use PKCE code or by a
                                rotating refresh secret, plus an approved
                                ``chrome-extension://`` origin.
``POST /extension/revoke``      disconnect, from either side.

What an authorization is worth
------------------------------
Exactly the four routes in ``EXTENSION_CAPTURE_CONTRACT`` and nothing else. This
module issues tokens; it does not widen anything. The contract table in
``app/core/auth/extension.py`` is untouched by this feature and remains the only
statement of what a capture credential may reach — an account-linked token is
checked against precisely that table by precisely the same middleware code path.
No admin API, no Gmail, no sending, no user management, no other application API
becomes reachable because somebody connected an extension.

Why the automatic path is a redirect and not a second endpoint
--------------------------------------------------------------
``GET /extension/authorize`` issues a code immediately when a live link already
exists for this (account, extension, install). That is what makes
``launchWebAuthFlow({interactive:false})`` succeed silently on a browser restart:
Chrome loads the URL invisibly, the session cookie is present, the redirect
lands, and the extension is connected without anybody seeing anything. When there
is no live link the same URL renders a consent page instead, so the interactive
and silent paths are one address with one set of validation rules rather than two
that can drift.

Failures never redirect
-----------------------
Every validation failure on the authorize routes renders a plain refusal page.
Redirecting on a bad ``redirect_uri`` is the classic way an authorization
endpoint becomes an open redirect, and an open redirect next to an authorization
code is an account takeover. The ``redirect_uri`` is compared for equality
against ``https://<extension_id>.chromiumapp.org/`` and the ``extension_id`` must
already be in the approved set, so there is exactly one destination a code can
ever be delivered to.

That page is served to a **browser navigation** with HTTP 200 and to everything
else with its original 4xx -- see ``_refusal`` for why, and for the live UAT
failure that made the difference matter. Nothing about what is refused changed.

The token endpoint answers in one voice
---------------------------------------
``invalid_grant``, ``invalid_request``, ``unauthorized``. Nothing distinguishes
an unknown code from an expired one, from one already used, from one presented
with the wrong verifier, from a link whose owner has been disabled. No response
ever echoes a presented secret.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.auth.accounts import session_account_id
from app.core.auth.context import current_operator
from app.core.auth.csrf import register_csrf, require_csrf
from app.core.auth.extension import ExtensionAuthSettings, single_request_origin
from app.core.auth.extension_link import (
    ACCESS_TOKEN_SCHEME,
    CODE_CHALLENGE_METHOD,
    exchange_authorization_code,
    is_exact_redirect_uri,
    is_valid_code_challenge,
    is_valid_installation_id,
    is_valid_state,
    issue_authorization_code,
    live_link_for,
    parse_link_token,
    redirect_uri_for,
    revoke_link_for_session,
    revoke_links_for_user,
    rotate_refresh_token,
)
from app.core.auth.middleware import is_browser_navigation
from app.core.config import Settings, get_settings

router = APIRouter(
    prefix="/extension",
    tags=["extension"],
    include_in_schema=False,
    dependencies=[Depends(require_csrf)],
)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates" / "auth"))
register_csrf(templates.env)

#: The plain-words description of the four contract routes, shown on the consent
#: page. Written out here rather than generated from the contract table because a
#: human consenting needs "save the contact you captured", not
#: ``POST /api/intake/contact-captures`` — and because a generated list would
#: silently change wording if the table ever did.
CONSENT_PERMISSIONS: tuple[str, ...] = (
    "Save a contact you have captured in Sales Navigator to VMR Outbound.",
    "Read your contact labels, so the panel can show them.",
    "Look up whether a profile you are viewing is already a VMR contact.",
    "Read your campaign list, so you can file a capture into one.",
)

#: What the consent page promises is *not* granted. Stated on the page because a
#: consent screen that lists only permissions teaches nobody what the boundary is.
CONSENT_EXCLUSIONS: tuple[str, ...] = (
    "It cannot read or send email, and cannot reach Gmail.",
    "It cannot reach any administration screen, user account or provider setting.",
    "It cannot change a campaign, a sequence or anything already saved.",
)


def _settings(request: Request) -> Settings:
    configured = getattr(request.app.state, "vmr_settings", None)
    return configured if isinstance(configured, Settings) else get_settings()


def _extension_settings(request: Request) -> ExtensionAuthSettings:
    return _settings(request).extension_auth


def _refusal(request: Request, *, reason: str, detail: str, status_code: int = 400) -> Response:
    """One refusal page for every failure on the authorize routes.

    Never a redirect, and never an echo of the value that failed: a refusal page
    that reflected the submitted ``redirect_uri`` would be a way to put an
    attacker's text on a VMR-origin page.

    Why a browser navigation gets this page with HTTP 200
    -----------------------------------------------------
    This page is the *only* thing that ever tells an operator why their sign-in
    did not work, and until now it had never once been shown to one.

    ``chrome.identity.launchWebAuthFlow`` does not render the authorization URL
    the way a tab does. Chromium's ``WebAuthFlow`` watches the main-frame
    navigation it started and treats **any** response with a status of 400 or
    above as a failed load: it tears the window down before paint and rejects
    the extension's call with ``Authorization page could not be loaded.`` So
    every refusal here -- "this browser extension is not approved for this
    deployment", the most useful sentence in the whole feature -- was rendered,
    counted, logged and then thrown away unread. What reached the operator
    instead was the extension's classification of Chrome's message, which is the
    same message a genuinely unreachable server produces, so the panel said
    "VMR Outbound could not be reached." about a deployment that had just
    answered in under a hundred milliseconds.

    The status code is therefore chosen by *who is reading*, and nothing else
    changes:

    * a **browser navigation** -- which is what the authorization window is, and
      what the consent form posts -- gets the page at ``200``, because the page
      IS the answer and a human is about to read it;
    * everything else (``fetch``, XHR, a probe) keeps the original
      ``400``/``401``, because a program checks the status.

    This grants nothing. A refusal is still a refusal: no authorization code is
    issued, no redirect to the extension happens, no session is created, and the
    page echoes no parameter it was given. Only the envelope changed, so that the
    sentence inside it can finally be read.
    """

    return templates.TemplateResponse(
        request=request,
        name="extension_link_refused.html",
        context={"reason": reason, "detail": detail},
        status_code=200 if is_browser_navigation(request.scope) else status_code,
    )


def _json_error(error: str, status_code: int) -> JSONResponse:
    """The uniform token-endpoint failure.

    One body, three names, no detail. A caller must not be able to tell an
    unknown code from a wrong verifier from a revoked link, and no client needs
    to: every one of them means "start the authorization again".
    """

    return JSONResponse(status_code=status_code, content={"error": error})


class _AuthorizationRequest:
    """The validated parameters of one authorization request."""

    __slots__ = ("code_challenge", "extension_id", "installation_id", "redirect_uri", "state")

    def __init__(
        self,
        *,
        extension_id: str,
        installation_id: str,
        code_challenge: str,
        state: str,
        redirect_uri: str,
    ) -> None:
        self.extension_id = extension_id
        self.installation_id = installation_id
        self.code_challenge = code_challenge
        self.state = state
        self.redirect_uri = redirect_uri


def _validate(
    settings: ExtensionAuthSettings,
    *,
    extension_id: str | None,
    installation_id: str | None,
    code_challenge: str | None,
    code_challenge_method: str | None,
    state: str | None,
    redirect_uri: str | None,
) -> tuple[_AuthorizationRequest | None, str]:
    """Every parameter, checked before anything is issued or rendered.

    Returns the validated request or a short, non-echoing reason. The checks are
    ordered cheapest-first and none of them is a pattern where an exact match
    would do.
    """

    if not settings.link_enabled:
        return None, "not_enabled"
    if not settings.is_allowed_extension_id(extension_id):
        return None, "unknown_extension"
    assert extension_id is not None  # narrowed by the check above
    if not is_valid_installation_id(installation_id):
        return None, "bad_installation"
    assert installation_id is not None
    if (code_challenge_method or "") != CODE_CHALLENGE_METHOD:
        return None, "bad_challenge_method"
    if not is_valid_code_challenge(code_challenge):
        return None, "bad_challenge"
    assert code_challenge is not None
    if not is_valid_state(state):
        return None, "bad_state"
    assert state is not None
    if not is_exact_redirect_uri(presented=redirect_uri, extension_id=extension_id):
        return None, "bad_redirect"
    return (
        _AuthorizationRequest(
            extension_id=extension_id,
            installation_id=installation_id,
            code_challenge=code_challenge,
            state=state,
            redirect_uri=redirect_uri_for(extension_id),
        ),
        "",
    )


_REFUSAL_DETAIL: dict[str, str] = {
    "not_enabled": "This deployment is not set up to connect browser extensions.",
    "unknown_extension": "This browser extension is not approved for this deployment.",
    "bad_installation": "This connection request was not formed correctly.",
    "bad_challenge_method": "This connection request was not formed correctly.",
    "bad_challenge": "This connection request was not formed correctly.",
    "bad_state": "This connection request was not formed correctly.",
    "bad_redirect": "This connection request names a destination VMR will not use.",
    "no_account": "Your VMR account could not be identified. Sign in again.",
}


def _current_user_id() -> uuid.UUID | None:
    """The signed-in account, as the durable ``users.id``.

    Read from the session the middleware already verified on this request, which
    is what makes "the account this authorization belongs to" the same account
    the boundary re-checks on every later capture.
    """

    return session_account_id(current_operator())


def _issued_redirect(request_: _AuthorizationRequest, code: str) -> RedirectResponse:
    """303 back to the extension with the code and the client's own ``state``.

    ``state`` was validated as base64url before it reached here, so it cannot
    change the meaning of the URL; it is percent-encoded anyway, because a value
    placed into a URL should be encoded at the point it is placed rather than
    because something upstream promised it was safe.
    """

    query = f"code={quote(code, safe='')}&state={quote(request_.state, safe='')}"
    return RedirectResponse(f"{request_.redirect_uri}?{query}", status_code=303)


@router.get("/authorize")
def authorize_page(
    request: Request,
    db: Session = Depends(get_db),
    extension_id: str | None = None,
    installation_id: str | None = None,
    code_challenge: str | None = None,
    code_challenge_method: str | None = None,
    state: str | None = None,
    redirect_uri: str | None = None,
) -> Response:
    """Connect automatically when already linked; otherwise ask."""

    settings = _extension_settings(request)
    validated, reason = _validate(
        settings,
        extension_id=extension_id,
        installation_id=installation_id,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        state=state,
        redirect_uri=redirect_uri,
    )
    if validated is None:
        return _refusal(request, reason=reason, detail=_REFUSAL_DETAIL[reason])

    user_id = _current_user_id()
    if user_id is None:
        return _refusal(
            request, reason="no_account", detail=_REFUSAL_DETAIL["no_account"], status_code=401
        )

    existing = live_link_for(
        db,
        user_id=user_id,
        extension_id=validated.extension_id,
        installation_id=validated.installation_id,
    )
    if existing is not None:
        # The silent path. A live link is standing consent for this install, so
        # re-asking would be a dialog with one possible answer — and it is what
        # makes a browser restart cost nothing.
        code = issue_authorization_code(
            db,
            user_id=user_id,
            extension_id=validated.extension_id,
            installation_id=validated.installation_id,
            code_challenge=validated.code_challenge,
            redirect_uri=validated.redirect_uri,
        )
        return _issued_redirect(validated, code)

    operator = current_operator()
    return templates.TemplateResponse(
        request=request,
        name="extension_link_consent.html",
        context={
            "account_email": operator.email if operator is not None else "",
            "extension_id": validated.extension_id,
            "installation_id": validated.installation_id,
            "code_challenge": validated.code_challenge,
            "code_challenge_method": CODE_CHALLENGE_METHOD,
            "state": validated.state,
            "redirect_uri": validated.redirect_uri,
            "permissions": CONSENT_PERMISSIONS,
            "exclusions": CONSENT_EXCLUSIONS,
        },
    )


@router.post("/authorize")
def authorize_submit(
    request: Request,
    db: Session = Depends(get_db),
    extension_id: str = Form(default=""),
    installation_id: str = Form(default=""),
    code_challenge: str = Form(default=""),
    code_challenge_method: str = Form(default=""),
    state: str = Form(default=""),
    redirect_uri: str = Form(default=""),
) -> Response:
    """The consent button. Re-validates everything the GET validated."""

    settings = _extension_settings(request)
    validated, reason = _validate(
        settings,
        extension_id=extension_id,
        installation_id=installation_id,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        state=state,
        redirect_uri=redirect_uri,
    )
    if validated is None:
        return _refusal(request, reason=reason, detail=_REFUSAL_DETAIL[reason])

    user_id = _current_user_id()
    if user_id is None:
        return _refusal(
            request, reason="no_account", detail=_REFUSAL_DETAIL["no_account"], status_code=401
        )

    code = issue_authorization_code(
        db,
        user_id=user_id,
        extension_id=validated.extension_id,
        installation_id=validated.installation_id,
        code_challenge=validated.code_challenge,
        redirect_uri=validated.redirect_uri,
    )
    return _issued_redirect(validated, code)


async def _json_body(request: Request) -> dict[str, Any]:
    """The request body, or an empty mapping. Malformed JSON is a refusal."""

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - any parse failure is one outcome
        return {}
    return payload if isinstance(payload, dict) else {}


def _text(payload: dict[str, Any], key: str, *, limit: int = 256) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or len(value) > limit:
        return ""
    return value


@router.post("/token")
async def token(request: Request, db: Session = Depends(get_db)) -> Response:
    """Exchange a code, or rotate a refresh token. No cookie, ever.

    The approved ``Origin`` is mandatory here even though the endpoint is not
    cookie-authenticated: it is what binds a stolen code or refresh secret to the
    one browser extension that may use it, and the Fetch standard puts ``Origin``
    on every ``POST`` regardless of mode, so a real caller always has one.
    """

    settings = _extension_settings(request)
    if not settings.link_enabled:
        return _json_error("unauthorized", 401)

    origin = single_request_origin(request.scope)
    if not settings.is_allowed_origin(origin):
        return _json_error("unauthorized", 401)

    payload = await _json_body(request)
    grant_type = _text(payload, "grant_type", limit=64)
    extension_id = _text(payload, "extension_id", limit=64)
    installation_id = _text(payload, "installation_id", limit=64)

    if not settings.is_allowed_extension_id(extension_id):
        return _json_error("invalid_request", 400)
    if origin is not None and origin.rstrip("/") != f"chrome-extension://{extension_id}":
        # The body must name the same install the request came from. Without
        # this an approved extension could redeem a code issued to a different
        # approved extension.
        return _json_error("invalid_request", 400)
    if not is_valid_installation_id(installation_id):
        return _json_error("invalid_request", 400)

    if grant_type == "authorization_code":
        issued = exchange_authorization_code(
            db,
            code=_text(payload, "code", limit=128),
            code_verifier=_text(payload, "code_verifier", limit=128),
            extension_id=extension_id,
            installation_id=installation_id,
            label=_text(payload, "label", limit=120) or None,
        )
    elif grant_type == "refresh_token":
        issued = rotate_refresh_token(
            db,
            refresh_token=_text(payload, "refresh_token", limit=256),
            extension_id=extension_id,
            installation_id=installation_id,
        )
    else:
        return _json_error("invalid_request", 400)

    if issued is None:
        return _json_error("invalid_grant", 400)

    db.commit()
    return JSONResponse(
        status_code=200,
        content={
            "access_token": issued.access_token,
            "token_type": "Bearer",
            "expires_in": issued.expires_in,
            "refresh_token": issued.refresh_token,
            "scope": issued.scope,
            # The one piece of account information the panel shows, so an
            # operator can see which VMR account their captures are going to.
            # Nothing else about the account travels to the extension.
            "account": {"email": issued.account_email},
        },
    )


@router.post("/revoke")
async def revoke(request: Request, db: Session = Depends(get_db)) -> Response:
    """Disconnect, from either side of the link.

    Two callers, two credentials, one effect:

    * the **extension**, presenting its own access token from an approved origin.
      Answered ``204`` whether or not the token named a live link, because
      "that token was already dead" is not something a caller needs to be told
      and is something an attacker would like to learn.
    * a **signed-in operator**, disconnecting their own install from the VMR app.
      Only ever their own links: the account comes from the verified session and
      never from the request body.
    """

    settings = _extension_settings(request)
    payload = await _json_body(request)

    presented = request.headers.get("authorization")
    parsed = parse_link_token(
        (presented or "").partition(" ")[2].strip() if presented else None,
        scheme=ACCESS_TOKEN_SCHEME,
    )
    if parsed is not None:
        if not settings.link_enabled:
            return _json_error("unauthorized", 401)
        if not settings.is_allowed_origin(single_request_origin(request.scope)):
            return _json_error("unauthorized", 401)
        revoke_link_for_session(db, access_token=(presented or "").partition(" ")[2].strip())
        db.commit()
        return Response(status_code=204)

    user_id = _current_user_id()
    if user_id is None:
        return _json_error("unauthorized", 401)

    extension_id = _text(payload, "extension_id", limit=64) or None
    installation_id = _text(payload, "installation_id", limit=64) or None
    revoke_links_for_user(
        db,
        user_id=user_id,
        extension_id=extension_id,
        installation_id=installation_id,
    )
    db.commit()
    return Response(status_code=204)

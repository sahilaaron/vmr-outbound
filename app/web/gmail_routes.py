"""The Gmail mailbox authorization surface (#267).

Three routes, and none of them is anonymous. That is the first thing to say
about this file: ``/gmail/*`` is **not** on the anonymous allow-list in
``app/core/auth/policy.py`` and must never be added to it. Connecting a mailbox
is an action an already-approved operator takes; the OAuth callback is not a
sign-in path and has nothing to grant to a stranger.

``POST /gmail/connect``     mints one authorization transaction and hands the
                            browser to Google's Gmail consent screen.
``GET  /gmail/callback``    validates the whole round trip and binds the mailbox.
``POST /gmail/disconnect``  forgets the authorization, and asks Google to revoke.

Connecting is a ``POST`` rather than a link. A ``GET`` that starts an OAuth
consent is reachable from any page on the internet, and while the consent screen
itself is a human decision, the requirement in #267 is that Gmail permission is
requested *only* from an explicit Connect Gmail click. A CSRF-protected,
same-origin ``POST`` is what makes that assertable rather than assumed.

Why the callback binds to the session and not to the transaction alone
----------------------------------------------------------------------
The signed transaction cookie carries the ``state``, the ``nonce``, the PKCE
verifier, the return path **and the operator subject it was started by**. The
callback requires all of:

* a live, approved operator session;
* a transaction cookie that decodes, verifies and has not expired;
* ``state`` equal to the transaction's, compared in constant time;
* the transaction's operator subject equal to the *signed-in* operator's.

The last one is the check that makes "an OAuth callback cannot bind a mailbox to
the wrong operator" true rather than merely likely. Without it, a callback URL
captured from one operator's browser and opened in another's -- who is also
signed in -- would attach the first operator's consent to the second's account.

Two separate authorities, stated once more
------------------------------------------
Nothing here reads the hosted session cookie *as* a Gmail credential, reuses the
Google identity token, or accepts the extension bearer credential. The operator
session proves who is asking; the Gmail grant is a separate consent with its own
client, its own secret and its own consent screen, and it is stored encrypted.
"""

from __future__ import annotations

import hmac
import secrets
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.auth.context import current_operator
from app.core.auth.csrf import require_csrf
from app.core.auth.session import SessionCodec, SessionDecodeError
from app.core.config import Settings, get_settings
from app.core.gmail_config import GMAIL_CALLBACK_PATH
from app.services.gmail import mailbox as gmail_mailbox
from app.services.gmail.oauth import (
    GmailAuthorizationError,
    GmailOAuthClient,
    GoogleGmailOAuthClient,
    verify_mailbox_identity,
)

router = APIRouter(
    prefix="/gmail",
    tags=["gmail"],
    include_in_schema=False,
    dependencies=[Depends(require_csrf)],
)

#: The single-use authorization transaction. A cookie of its own rather than a
#: reuse of ``vmr_login``: the sign-in transaction is minted before there is a
#: session and this one only ever exists after, and letting one be presented as
#: the other is exactly the confusion this feature must not create. It is signed
#: with a key of its own (``SessionCodec.encode_gmail_transaction``), so neither
#: token verifies as the other in *either* direction; the ``kind`` field below
#: is a second, cheaper statement of the same thing rather than the mechanism.
GMAIL_TRANSACTION_COOKIE_NAME = "vmr_gmail_auth"
_TRANSACTION_KIND = "gmail-mailbox-authorization"

#: Where an operator lands when nothing better was submitted.
DEFAULT_RETURN_PATH = "/app/review"

#: The client is resolved once per process and stored on app state, so a test
#: can inject a deterministic stub without a network.
GMAIL_OAUTH_CLIENT_STATE_KEY = "vmr_gmail_oauth_client"


def _settings(request: Request) -> Settings:
    configured = getattr(request.app.state, "vmr_settings", None)
    return configured if isinstance(configured, Settings) else get_settings()


def _codec(settings: Settings) -> SessionCodec:
    return SessionCodec(settings.auth.session_secret or "")


def oauth_client(request: Request, settings: Settings) -> GmailOAuthClient:
    configured: GmailOAuthClient | None = getattr(
        request.app.state, GMAIL_OAUTH_CLIENT_STATE_KEY, None
    )
    if configured is not None:
        return configured
    return GoogleGmailOAuthClient(settings.gmail)


def gmail_enabled(settings: Settings) -> bool:
    """Whether the Gmail draft feature exists in this deployment at all.

    Both switches, because the feature acts on a sequence: without
    ``email_sequences`` there is nothing to draft from, and a Connect Gmail
    button leading to a dead end is worse than no button.
    """

    return bool(settings.features.gmail_drafts and settings.features.email_sequences)


def _safe_return(raw: str | None) -> str:
    """Constrain the submitted return path to this application's own pages.

    Same rule the sequence write routes use: the value is echoed into a
    ``Location`` header, so a value that is not a plain in-app path is replaced
    outright rather than repaired.
    """

    candidate = (raw or "").strip()
    if not candidate.startswith("/app") or candidate.startswith("//") or "\\" in candidate:
        return DEFAULT_RETURN_PATH
    if any(character in candidate for character in ("\r", "\n", "\t")) or "://" in candidate:
        return DEFAULT_RETURN_PATH
    return candidate[:512]


def _redirect(target: str, *, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    separator = "&" if "?" in target else "?"
    if ok:
        target = f"{target}{separator}ok={quote(ok, safe='')}"
    elif err:
        target = f"{target}{separator}err={quote(err, safe='')}"
    return RedirectResponse(target, status_code=303)


def _not_available() -> Response:
    """The shape a switched-off feature answers with.

    A 404 rather than a 403: while the switch is off the area does not exist,
    which is the FND-007 default-off pattern every other feature here follows.
    """

    return JSONResponse({"error": "not_found"}, status_code=404)


def _set_transaction(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        GMAIL_TRANSACTION_COOKIE_NAME,
        token,
        max_age=settings.gmail.authorization_transaction_max_age_seconds,
        httponly=True,
        secure=settings.auth.cookie_secure,
        samesite="lax",
        path="/gmail",
        domain=settings.auth.cookie_domain,
    )


def _consume_transaction(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        GMAIL_TRANSACTION_COOKIE_NAME,
        path="/gmail",
        domain=settings.auth.cookie_domain,
        httponly=True,
        secure=settings.auth.cookie_secure,
        samesite="lax",
    )


def _constant_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(
        left.encode("utf-8", "surrogatepass"), right.encode("utf-8", "surrogatepass")
    )


@router.post("/connect")
def connect_gmail(
    request: Request,
    back: str = Form(DEFAULT_RETURN_PATH),
) -> Response:
    """Start one Gmail consent, for the signed-in operator only."""

    settings = _settings(request)
    if not gmail_enabled(settings):
        return _not_available()
    target = _safe_return(back)

    if not settings.gmail.is_configured():
        return _redirect(
            target,
            err=(
                "Gmail is not configured in this environment, so no mailbox can be connected. "
                "A Gmail OAuth client and a token encryption key are both required."
            ),
        )
    operator = current_operator()
    if operator is None:
        # Local development has no operator session. Binding a mailbox to
        # nobody would produce a grant no request could ever find again, so the
        # feature says so rather than pretending.
        return _redirect(
            target,
            err=(
                "Connecting a Gmail mailbox requires a signed-in operator, and this "
                "environment has no operator sign-in."
            ),
        )
    redirect_uri = _redirect_uri(settings)
    if redirect_uri is None:
        return _redirect(
            target,
            err=(
                "This deployment has no canonical public address configured, so the Gmail "
                "redirect cannot be built."
            ),
        )

    from app.core.auth.google import code_challenge_for, generate_code_verifier

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = generate_code_verifier()
    now = int(time.time())
    token = _codec(settings).encode_gmail_transaction(
        {
            "kind": _TRANSACTION_KIND,
            "state": state,
            "nonce": nonce,
            "verifier": verifier,
            "operator": operator.subject,
            "next": target,
            "exp": now + settings.gmail.authorization_transaction_max_age_seconds,
        }
    )

    client = oauth_client(request, settings)
    authorization_url = client.authorization_url(
        redirect_uri=redirect_uri,
        state=state,
        nonce=nonce,
        code_challenge=code_challenge_for(verifier),
        login_hint=operator.email or None,
    )
    response = RedirectResponse(authorization_url, status_code=303)
    _set_transaction(response, settings, token)
    return response


@router.get("/callback")
def gmail_callback(
    request: Request,
    db: Session = Depends(get_db),
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    """Validate the round trip and bind one verified mailbox to this operator."""

    settings = _settings(request)
    if not gmail_enabled(settings):
        return _not_available()

    codec = _codec(settings)
    now = int(time.time())
    raw_transaction = request.cookies.get(GMAIL_TRANSACTION_COOKIE_NAME)

    try:
        transaction = codec.decode_gmail_transaction(raw_transaction, now=now)
    except SessionDecodeError:
        return _finish(
            settings,
            DEFAULT_RETURN_PATH,
            err=(
                "That Gmail authorization has expired or did not start here. Nothing was "
                "connected. Click Connect Gmail again."
            ),
        )

    target = _safe_return(
        transaction.get("next") if isinstance(transaction.get("next"), str) else None
    )
    if transaction.get("kind") != _TRANSACTION_KIND:
        # A token minted for a different purpose, presented here.
        return _finish(settings, target, err=_REFUSED)

    operator = current_operator()
    expected_operator = transaction.get("operator")
    if operator is None or not isinstance(expected_operator, str):
        return _finish(settings, target, err=_REFUSED)
    if not _constant_equal(expected_operator, operator.subject):
        # The authorization was started by somebody else. Binding it here would
        # attach one operator's mailbox consent to another operator's account.
        return _finish(
            settings,
            target,
            err=(
                "That Gmail authorization was started by a different operator, so nothing "
                "was connected."
            ),
        )

    if error is not None or not code or not state:
        return _finish(
            settings, target, err="Gmail was not connected: the consent was cancelled or refused."
        )

    expected_state = transaction.get("state")
    if not isinstance(expected_state, str) or not _constant_equal(expected_state, state):
        return _finish(settings, target, err=_REFUSED)

    nonce = transaction.get("nonce")
    verifier = transaction.get("verifier")
    if not isinstance(nonce, str) or not isinstance(verifier, str):
        return _finish(settings, target, err=_REFUSED)

    redirect_uri = _redirect_uri(settings)
    if redirect_uri is None:
        return _finish(settings, target, err=_REFUSED)

    client = oauth_client(request, settings)
    try:
        grant = client.exchange_code(code=code, redirect_uri=redirect_uri, code_verifier=verifier)
        address, account_subject = verify_mailbox_identity(
            grant, settings=settings.gmail, expected_nonce=nonce, now=now
        )
    except GmailAuthorizationError as exc:
        return _finish(settings, target, err=str(exc))

    try:
        row = gmail_mailbox.bind_mailbox(
            db,
            operator_subject=operator.subject,
            operator_email=operator.email,
            mailbox_address=address,
            mailbox_account_subject=account_subject,
            grant=grant,
            settings=settings.gmail,
        )
    except gmail_mailbox.GmailMailboxError as exc:
        db.rollback()
        return _finish(settings, target, err=str(exc))
    db.commit()

    return _finish(
        settings,
        target,
        ok=(
            f"Gmail connected: drafts will be created in {row.mailbox_address}. VMR can create "
            "drafts in this mailbox and cannot send from it."
        ),
    )


@router.post("/disconnect")
def disconnect_gmail(
    request: Request,
    db: Session = Depends(get_db),
    back: str = Form(DEFAULT_RETURN_PATH),
) -> Response:
    """Forget this operator's mailbox authorization."""

    settings = _settings(request)
    if not gmail_enabled(settings):
        return _not_available()
    target = _safe_return(back)
    operator = current_operator()
    if operator is None:
        return _redirect(target, err="There is no signed-in operator, so nothing was changed.")

    client: GmailOAuthClient | None
    try:
        client = oauth_client(request, settings) if settings.gmail.has_client() else None
    except ValueError:
        client = None

    disconnected, revoked = gmail_mailbox.disconnect(
        db, operator_subject=operator.subject, settings=settings.gmail, client=client
    )
    db.commit()
    if not disconnected:
        return _redirect(target, err="No Gmail mailbox was connected, so nothing was changed.")
    if revoked:
        return _redirect(
            target,
            ok=(
                "Gmail disconnected. The stored authorization has been deleted and Google has "
                "been asked to revoke it. Drafts already in your mailbox are untouched."
            ),
        )
    return _redirect(
        target,
        ok=(
            "Gmail disconnected and the stored authorization deleted. Google could not be "
            "reached to revoke it, so remove VMR from your Google account permissions as well. "
            "Drafts already in your mailbox are untouched."
        ),
    )


#: One refusal sentence for every distinguishable failure of the round trip. The
#: operator gets the same wording whether the state mismatched, the transaction
#: was minted for another purpose or the nonce was wrong, because telling them
#: apart tells an attacker which check they defeated.
_REFUSED = (
    "That Gmail authorization could not be verified, so nothing was connected. "
    "Click Connect Gmail again."
)


def _redirect_uri(settings: Settings) -> str | None:
    base = settings.auth.public_base_url
    if base is None:
        return None
    return f"{base}{GMAIL_CALLBACK_PATH}"


def _finish(
    settings: Settings, target: str, *, ok: str | None = None, err: str | None = None
) -> Response:
    """Redirect back to the operator's page, consuming the transaction cookie."""

    response = _redirect(target, ok=ok, err=err)
    _consume_transaction(response, settings)
    return response

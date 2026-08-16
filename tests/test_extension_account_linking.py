"""Account-linked extension authorization, written from the attacker's side.

The product problem this replaces was small and awful: an ordinary user had to
paste a backend URL and a permanent ``vmrx1.<key_id>.<secret>`` shared credential
into a browser extension, and a Chrome restart made them do it again. The
replacement is a first-party PKCE authorization-code flow that binds one browser
install to one VMR account, and this file is the proof that the replacement did
not buy convenience with authority.

Three claims are worth more than the rest, and each is an enumeration rather than
an example:

* **Section E — the contract did not widen.** An account-linked token reaches
  exactly the four routes in ``EXTENSION_CAPTURE_CONTRACT`` and is refused
  everywhere else, including on Gmail, on every administrative surface and on the
  rest of the programmatic tree. Both halves are driven as real requests.
* **Section G — rotation and reuse detection.** The old refresh token is dead
  after a rotation, and replaying it revokes the whole link rather than merely
  failing.
* **Section H — the legacy credential is inert in hosted mode.** The same
  ``vmrx1`` credential that captures successfully under ``APP_ENV=local`` is
  worth nothing against a staging build. That is what makes "no reusable shared
  secret authorises a hosted capture" a fact rather than a policy.

Everything here runs over the real hosted middleware stack against the real
database. No provider is contacted and no Google endpoint is reached.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import secrets
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from app.core.auth.extension import EXTENSION_CAPTURE_CONTRACT, credential_digest
from app.core.auth.extension_link import (
    ACCESS_TOKEN_SCHEME,
    ACCESS_TOKEN_TTL_SECONDS,
    REFRESH_TOKEN_SCHEME,
)
from app.core.auth.session import SESSION_COOKIE_NAME, SessionCodec
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.main import create_app
from app.models.enums import UserState
from app.models.extension_session import ExtensionSession
from app.models.user import User
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.hosted_auth_factory import TEST_CLIENT_ID, seed_account

REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE_SUBMISSION = json.loads(
    (
        REPO_ROOT
        / "extensions"
        / "salesnav-capture"
        / "docs"
        / "fixtures"
        / "contact-capture.profile.example.json"
    ).read_text("utf-8")
)

HOST = "srv1885453.hstgr.cloud"
ORIGIN = f"https://{HOST}"
SESSION_SECRET = "test-session-secret-value-at-least-32-chars"
ADMIN_EMAIL = "sahil@verifiedmarketresearch.com"
STAGING_DATABASE_URL = "postgresql+psycopg://vmr:secret@db.internal.example:5432/vmr_staging"

EXTENSION_ID = "abcdefghijklmnopabcdefghijklmnop"
EXTENSION_ORIGIN = f"chrome-extension://{EXTENSION_ID}"
OTHER_EXTENSION_ID = "ponmlkjihgfedcbaponmlkjihgfedcba"
OTHER_EXTENSION_ORIGIN = f"chrome-extension://{OTHER_EXTENSION_ID}"
HOSTILE_ORIGIN = "https://evil.example"
INSTALLATION_ID = "install-0f2b6a1c-4d3e-11ef-9d2a-3f0f8c6b1a55"
REDIRECT_URI = f"https://{EXTENSION_ID}.chromiumapp.org/"

#: The legacy shared credential, in the shape `tests/test_extension_capture_auth.py`
#: mints it. Present only so section H can prove it is worth nothing here.
LEGACY_KEY_ID = "beta-laptop"
LEGACY_SECRET = "3fVQx8Zk2nLp7Rw6TyUiOaSdFgHjKlZxCvBnM4qWeRt"
LEGACY_CREDENTIAL = f"vmrx1.{LEGACY_KEY_ID}.{LEGACY_SECRET}"

SAMPLE_ID = "00000000-0000-4000-8000-000000000001"

CAPTURE_URL = "/api/intake/contact-captures"
LABELS_URL = "/api/contact-labels"
LOOKUP_URL = "/api/contacts/lookup?linkedin_profile_url=https%3A%2F%2Fwww.linkedin.com%2Fin%2Fx"
CAMPAIGNS_URL = "/api/campaigns"


class _AlwaysReadyProbe:
    def __call__(self) -> None:
        return None


def _env(**overrides: str) -> dict[str, str]:
    """A complete hosted staging configuration with account linking switched on.

    The legacy ``EXTENSION_AUTH__*`` credential is configured here *deliberately*,
    even though the product no longer uses it: section H's claim is that a fully
    and correctly configured legacy credential still cannot capture against a
    hosted build, and a test that simply left it unconfigured would prove nothing
    but that an empty list is empty.
    """

    env = {
        "APP_ENV": "staging",
        "DEBUG": "false",
        "DRY_RUN": "true",
        "TRUSTED_HOSTS": f'["{HOST}"]',
        "DATABASE_URL": STAGING_DATABASE_URL,
        "FEATURES__WORKBENCH": "true",
        "FEATURES__AGENT_WORKBENCH": "true",
        "FEATURES__EMAIL_SEQUENCES": "true",
        "FEATURES__GMAIL_DRAFTS": "true",
        "FEATURES__MILLIONVERIFIER": "true",
        "FEATURES__CONTACT_CAPTURE_INTAKE": "true",
        "AUTH__ENABLED": "true",
        "AUTH__SESSION_SECRET": SESSION_SECRET,
        "AUTH__ALLOWED_OPERATOR_EMAILS": "[]",
        "AUTH__BOOTSTRAP_ADMIN_EMAIL": ADMIN_EMAIL,
        "AUTH__GOOGLE_CLIENT_ID": TEST_CLIENT_ID,
        "AUTH__GOOGLE_CLIENT_SECRET": "test-client-secret",
        "AUTH__PUBLIC_BASE_URL": ORIGIN,
        "EXTENSION_AUTH__LINK_ENABLED": "true",
        "EXTENSION_AUTH__ENABLED": "true",
        "EXTENSION_AUTH__CREDENTIALS": json.dumps(
            [f"{LEGACY_KEY_ID}:{credential_digest(LEGACY_SECRET)}"]
        ),
        "EXTENSION_AUTH__ALLOWED_ORIGINS": json.dumps([EXTENSION_ORIGIN, OTHER_EXTENSION_ORIGIN]),
    }
    env.update(overrides)
    return env


def _local_env(**overrides: str) -> dict[str, str]:
    """The one configuration in which the legacy ``vmrx1`` credential still works.

    A developer machine, with hosted authentication deliberately switched on so
    that the boundary is actually enforced — otherwise the middleware returns
    early and the comparison in section H would be between "accepted" and "not
    checked at all", which proves nothing about the environment gate.
    """

    env = {
        "APP_ENV": "local",
        "DEBUG": "false",
        "DRY_RUN": "true",
        "TRUSTED_HOSTS": '["localhost"]',
        "FEATURES__WORKBENCH": "true",
        "FEATURES__CONTACT_CAPTURE_INTAKE": "true",
        "AUTH__ENABLED": "true",
        "AUTH__SESSION_SECRET": SESSION_SECRET,
        "AUTH__ALLOWED_OPERATOR_EMAILS": "[]",
        "AUTH__BOOTSTRAP_ADMIN_EMAIL": ADMIN_EMAIL,
        "AUTH__GOOGLE_CLIENT_ID": TEST_CLIENT_ID,
        "AUTH__GOOGLE_CLIENT_SECRET": "test-client-secret",
        "AUTH__PUBLIC_BASE_URL": "http://localhost",
        "AUTH__COOKIE_SECURE": "false",
        "EXTENSION_AUTH__ENABLED": "true",
        "EXTENSION_AUTH__CREDENTIALS": json.dumps(
            [f"{LEGACY_KEY_ID}:{credential_digest(LEGACY_SECRET)}"]
        ),
        "EXTENSION_AUTH__ALLOWED_ORIGINS": json.dumps([EXTENSION_ORIGIN]),
    }
    env.update(overrides)
    return env


def _apply(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def _build(monkeypatch: pytest.MonkeyPatch, env: dict[str, str], *, base_url: str) -> TestClient:
    _apply(monkeypatch, env)
    app = create_app(readiness_probe=_AlwaysReadyProbe())
    return TestClient(app, base_url=base_url, follow_redirects=False)


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The hosted application, built exactly as staging builds it.

    No ``get_db`` override: the whole point of this feature is a table, so the
    routes, the middleware seam and the assertions must all be looking at the
    same rows. The suite's autouse truncation sweep cleans up afterwards.
    """

    built = _build(monkeypatch, _env(), base_url=ORIGIN)
    try:
        yield built
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _pkce() -> tuple[str, str]:
    """One ``(verifier, challenge)`` pair, exactly as the extension builds it."""

    verifier = secrets.token_urlsafe(32)
    return verifier, _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def _attach_session(client: TestClient, user_id: str, email: str, *, auth_version: int = 1) -> str:
    """Sign an account in through the real cookie codec and return its CSRF token."""

    from app.core.auth.session import OperatorSession, new_session_id

    now = int(time.time())
    session_id = new_session_id()
    codec = SessionCodec(SESSION_SECRET)
    client.cookies.set(
        SESSION_COOKIE_NAME,
        codec.encode_session(
            OperatorSession(
                email=email,
                subject="",
                display_name="",
                session_id=session_id,
                issued_at=now,
                expires_at=now + 3600,
                user_id=user_id,
                auth_version=auth_version,
            )
        ),
    )
    return codec.csrf_token(session_id)


def _authorize_url(
    challenge: str,
    *,
    extension_id: str = EXTENSION_ID,
    installation_id: str = INSTALLATION_ID,
    state: str = "state-abcdefghijklmnop",
    method: str = "S256",
    redirect_uri: str | None = None,
) -> str:
    from urllib.parse import urlencode

    return "/extension/authorize?" + urlencode(
        {
            "extension_id": extension_id,
            "installation_id": installation_id,
            "code_challenge": challenge,
            "code_challenge_method": method,
            "state": state,
            "redirect_uri": redirect_uri or f"https://{extension_id}.chromiumapp.org/",
        }
    )


def _code_from(location: str) -> str:
    return parse_qs(urlparse(location).query)["code"][0]


def _consent(client: TestClient, csrf: str, challenge: str, **kwargs: str) -> Any:
    """Press the consent button, exactly as the rendered form does."""

    form = {
        "extension_id": kwargs.get("extension_id", EXTENSION_ID),
        "installation_id": kwargs.get("installation_id", INSTALLATION_ID),
        "code_challenge": challenge,
        "code_challenge_method": kwargs.get("method", "S256"),
        "state": kwargs.get("state", "state-abcdefghijklmnop"),
        "redirect_uri": kwargs.get(
            "redirect_uri", f"https://{kwargs.get('extension_id', EXTENSION_ID)}.chromiumapp.org/"
        ),
        "_csrf": csrf,
    }
    return client.post("/extension/authorize", data=form, headers={"Sec-Fetch-Site": "same-origin"})


def _token(client: TestClient, payload: dict[str, Any], *, origin: str | None = EXTENSION_ORIGIN):
    headers = {"Content-Type": "application/json"}
    if origin is not None:
        headers["Origin"] = origin
    return client.post("/extension/token", json=payload, headers=headers)


def _connect(
    client: TestClient,
    *,
    email: str = "operator@vmr.example",
    installation_id: str = INSTALLATION_ID,
) -> dict[str, Any]:
    """The whole happy path once: sign in, consent, exchange, sign out again.

    Returns the token response body. The session cookie is cleared afterwards so
    that every later assertion in a test is about the *extension's* authority and
    can never be satisfied by the operator's cookie riding along.
    """

    account = seed_account(email=email)
    csrf = _attach_session(client, account.user_id, account.email)
    verifier, challenge = _pkce()

    landing = client.get(_authorize_url(challenge, installation_id=installation_id))
    assert landing.status_code == 200, landing.text[:300]

    granted = _consent(client, csrf, challenge, installation_id=installation_id)
    assert granted.status_code == 303, granted.text[:300]
    code = _code_from(granted.headers["location"])
    client.cookies.clear()

    exchanged = _token(
        client,
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "extension_id": EXTENSION_ID,
            "installation_id": installation_id,
        },
    )
    assert exchanged.status_code == 200, exchanged.text
    body: dict[str, Any] = exchanged.json()
    body["user_id"] = account.user_id
    return body


def _fresh_capture() -> dict[str, Any]:
    payload = copy.deepcopy(PROFILE_SUBMISSION)
    payload["client_submission_id"] = str(uuid.uuid4())
    for capture in payload["contacts"]:
        capture["client_capture_id"] = str(uuid.uuid4())
    return payload


def _capture(client: TestClient, access_token: str, *, origin: str = EXTENSION_ORIGIN):
    return client.post(
        CAPTURE_URL,
        json=_fresh_capture(),
        headers={"Authorization": f"Bearer {access_token}", "Origin": origin},
    )


def _disable(user_id: str) -> None:
    with SessionLocal() as session:
        user = session.get(User, uuid.UUID(user_id))
        assert user is not None
        user.state = UserState.DISABLED
        session.commit()


def _link_rows() -> list[ExtensionSession]:
    with SessionLocal() as session:
        return list(session.scalars(select(ExtensionSession)).all())


# ---------------------------------------------------------------------------
# A. An approved operator can link an approved extension            (spec 1)
# ---------------------------------------------------------------------------


def test_a_signed_in_operator_can_authorize_the_extension_and_exchange_a_code(
    client: TestClient,
) -> None:
    """The whole point of the feature, end to end, with nothing pasted by hand.

    Note what the operator did: pressed one button. No backend URL, no
    credential, no key id, and nothing they could copy out of the page and reuse
    somewhere else — the code in the redirect is single-use, sixty seconds old,
    and worthless without the verifier that never left the extension.
    """

    issued = _connect(client)

    assert issued["token_type"] == "Bearer"
    assert issued["scope"] == "capture"
    assert issued["expires_in"] == ACCESS_TOKEN_TTL_SECONDS == 900
    assert issued["access_token"].startswith(f"{ACCESS_TOKEN_SCHEME}.")
    assert issued["refresh_token"].startswith(f"{REFRESH_TOKEN_SCHEME}.")
    assert issued["account"]["email"] == "operator@vmr.example"

    # And it is a real authorization: the capture route accepts it.
    captured = _capture(client, issued["access_token"])
    assert captured.status_code == 201, captured.text

    # One row, owned by that account, with only digests stored.
    rows = _link_rows()
    assert len(rows) == 1
    assert str(rows[0].user_id) == issued["user_id"]
    assert rows[0].extension_id == EXTENSION_ID
    assert rows[0].scope == "capture"
    stored = f"{rows[0].access_token_hash}{rows[0].refresh_token_hash}"
    assert issued["access_token"].split(".")[2] not in stored
    assert issued["refresh_token"].split(".")[2] not in stored


def test_an_already_linked_install_reconnects_with_no_consent_page(client: TestClient) -> None:
    """The "connect automatically" path, which is what makes a restart silent.

    A live link is standing consent for this install, so the authorize page
    issues a code and redirects immediately instead of asking again. That is
    exactly the shape ``launchWebAuthFlow({interactive: false})`` needs: no
    rendered page means no human, and the extension reconnects invisibly.
    """

    issued = _connect(client, email="reconnect@vmr.example")
    _attach_session(client, issued["user_id"], "reconnect@vmr.example")

    _, second_challenge = _pkce()
    silent = client.get(_authorize_url(second_challenge))
    client.cookies.clear()
    assert silent.status_code == 303
    assert silent.headers["location"].startswith(REDIRECT_URI)
    assert "code=" in silent.headers["location"]
    # No consent page was rendered, which is the whole claim: a live link is
    # standing consent and re-asking would be a dialog with one possible answer.
    assert silent.text.strip() == ""


# ---------------------------------------------------------------------------
# B. An unknown or anonymous caller cannot                          (spec 2)
# ---------------------------------------------------------------------------


def test_an_anonymous_caller_gets_no_code_and_no_page(client: TestClient) -> None:
    """No session, no authorization. The sign-in redirect is the whole flow.

    A browser navigation is sent to ``/auth/login`` — which is the one "Sign in
    to VMR Outbound" action the product is allowed to ask for — and an API-shaped
    call gets a bare 401. Neither answer contains a code, and neither is a
    redirect to the extension.
    """

    _, challenge = _pkce()

    navigation = client.get(_authorize_url(challenge), headers={"Accept": "text/html"})
    assert navigation.status_code == 303
    assert navigation.headers["location"].startswith("/auth/login")
    assert "code=" not in navigation.headers["location"]

    api_call = client.get(_authorize_url(challenge))
    assert api_call.status_code == 401
    assert api_call.json()["error"] == "unauthorized"

    posted = client.post(
        "/extension/authorize",
        data={"extension_id": EXTENSION_ID, "code_challenge": challenge},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert posted.status_code == 401


def test_an_invented_code_buys_nothing_at_the_token_endpoint(client: TestClient) -> None:
    """The public endpoint is authorised by a code, and it does not have one."""

    verifier, _ = _pkce()
    refused = _token(
        client,
        {
            "grant_type": "authorization_code",
            "code": secrets.token_urlsafe(32),
            "code_verifier": verifier,
            "extension_id": EXTENSION_ID,
            "installation_id": INSTALLATION_ID,
        },
    )
    assert refused.status_code == 400
    assert refused.json() == {"error": "invalid_grant"}
    assert _link_rows() == []


def test_a_session_for_an_account_that_does_not_exist_cannot_authorize(
    client: TestClient,
) -> None:
    """A cookie naming no account is not a signed-in operator."""

    _attach_session(client, str(uuid.uuid4()), "nobody@vmr.example")
    _, challenge = _pkce()
    refused = client.get(_authorize_url(challenge))
    assert refused.status_code == 401
    assert _link_rows() == []


# ---------------------------------------------------------------------------
# C. A disabled account, on both sides of the door                  (spec 3)
# ---------------------------------------------------------------------------


def test_a_disabled_account_cannot_authorize_the_extension(client: TestClient) -> None:
    """Disabling somebody removes their ability to grant, not just to browse."""

    account = seed_account(email="disabled@vmr.example")
    _attach_session(client, account.user_id, account.email)
    _disable(account.user_id)

    _, challenge = _pkce()
    refused = client.get(_authorize_url(challenge))
    assert refused.status_code == 401
    assert refused.json()["error"] == "unauthorized"
    assert _link_rows() == []


def test_disabling_an_account_kills_an_already_issued_access_token(client: TestClient) -> None:
    """The claim the previous credential could not make at all.

    An access token that was minted while the account was healthy stops working
    on the **next request** after the account is disabled, because the boundary
    re-reads the owning account every time rather than trusting the token. No
    expiry is waited for, nothing is restarted, and the refresh token is dead
    too — so the extension cannot quietly mint itself a new one.
    """

    issued = _connect(client, email="soon-disabled@vmr.example")
    assert _capture(client, issued["access_token"]).status_code == 201

    _disable(issued["user_id"])

    refused = _capture(client, issued["access_token"])
    assert refused.status_code == 401
    assert refused.json()["error"] == "unauthorized"

    refreshed = _token(
        client,
        {
            "grant_type": "refresh_token",
            "refresh_token": issued["refresh_token"],
            "extension_id": EXTENSION_ID,
            "installation_id": INSTALLATION_ID,
        },
    )
    assert refreshed.status_code == 400
    assert refreshed.json() == {"error": "invalid_grant"}


# ---------------------------------------------------------------------------
# D. The wrong extension, and the wrong origin                      (spec 4)
# ---------------------------------------------------------------------------


def test_an_unapproved_extension_id_cannot_authorize(client: TestClient) -> None:
    """A refusal page, never a redirect. This is where open redirects are born."""

    account = seed_account(email="wrong-extension@vmr.example")
    _attach_session(client, account.user_id, account.email)
    _, challenge = _pkce()

    refused = client.get(_authorize_url(challenge, extension_id="a" * 32))
    assert refused.status_code == 400
    assert "location" not in refused.headers
    assert "code=" not in refused.text
    assert _link_rows() == []


def test_a_redirect_uri_that_is_not_the_extensions_own_is_refused(client: TestClient) -> None:
    """Exact match against ``https://<id>.chromiumapp.org/`` and nothing else."""

    account = seed_account(email="redirect@vmr.example")
    _attach_session(client, account.user_id, account.email)
    _, challenge = _pkce()

    for hostile in (
        "https://evil.example/",
        f"https://{EXTENSION_ID}.chromiumapp.org/../evil",
        f"https://{EXTENSION_ID}.chromiumapp.org",
        f"https://{OTHER_EXTENSION_ID}.chromiumapp.org/",
        f"https://{EXTENSION_ID}.chromiumapp.org.evil.example/",
    ):
        refused = client.get(_authorize_url(challenge, redirect_uri=hostile))
        assert refused.status_code == 400, hostile
        assert "location" not in refused.headers, hostile
    assert _link_rows() == []


@pytest.mark.parametrize("origin", [HOSTILE_ORIGIN, ORIGIN, None])
def test_the_token_endpoint_requires_an_approved_extension_origin(
    client: TestClient, origin: str | None
) -> None:
    """A stolen code replayed from anywhere else is worth nothing.

    ``Origin`` is mandatory here even though there is no cookie to protect: it is
    what binds the code to the one extension that may redeem it, and the Fetch
    standard puts it on every ``POST`` regardless of mode, so a real caller
    always has one.
    """

    account = seed_account(email="origin@vmr.example")
    csrf = _attach_session(client, account.user_id, account.email)
    verifier, challenge = _pkce()
    granted = _consent(client, csrf, challenge)
    code = _code_from(granted.headers["location"])
    client.cookies.clear()

    refused = _token(
        client,
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "extension_id": EXTENSION_ID,
            "installation_id": INSTALLATION_ID,
        },
        origin=origin,
    )
    # Two different refusals, both correct and neither a token. A web origin is
    # stopped one layer earlier, by the cross-site backstop, because the narrow
    # exemption that lets this endpoint be called at all applies only to an
    # approved `chrome-extension://` origin; the others reach the handler and are
    # refused there.
    assert refused.status_code in {401, 403}, refused.text
    assert "access_token" not in refused.text
    assert _link_rows() == []


def test_one_approved_extension_cannot_redeem_another_approved_extensions_code(
    client: TestClient,
) -> None:
    """Two approved installs are still two, and a code belongs to one of them.

    Both origins in this deployment's allow-list are genuinely approved, so this
    is not "an unapproved origin was refused" — it is the narrower claim that the
    code is bound to the install it was issued to.
    """

    account = seed_account(email="two-installs@vmr.example")
    csrf = _attach_session(client, account.user_id, account.email)
    verifier, challenge = _pkce()
    granted = _consent(client, csrf, challenge)
    code = _code_from(granted.headers["location"])
    client.cookies.clear()

    refused = _token(
        client,
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "extension_id": OTHER_EXTENSION_ID,
            "installation_id": INSTALLATION_ID,
        },
        origin=OTHER_EXTENSION_ORIGIN,
    )
    assert refused.status_code == 400
    assert refused.json() == {"error": "invalid_grant"}
    assert _link_rows() == []


def test_a_capture_from_an_unapproved_origin_is_refused(client: TestClient) -> None:
    """The token is bound to an origin at use time as well as at issue time."""

    issued = _connect(client, email="capture-origin@vmr.example")
    for origin in (HOSTILE_ORIGIN, ORIGIN, OTHER_EXTENSION_ORIGIN):
        refused = _capture(client, issued["access_token"], origin=origin)
        assert refused.status_code == 401, origin


# ---------------------------------------------------------------------------
# E. Exactly four routes, and nothing else                          (spec 5, 6)
# ---------------------------------------------------------------------------

#: Everything an account-linked token must NOT reach, chosen to cover each of the
#: authorities the contract in the design deliberately withholds: administration,
#: the operator product, user management, Gmail, sending, provider spend, agent
#: control, campaign execution, and the schema that maps them all.
REFUSED_SURFACE: tuple[tuple[str, str], ...] = (
    ("GET", "/admin"),
    ("GET", "/app"),
    ("GET", "/app/admin/users"),
    ("POST", "/gmail/connect"),
    ("POST", f"/app/review/sequence/{SAMPLE_ID}/gmail-drafts"),
    ("POST", "/verification/bulk"),
    ("PUT", f"/api/agents/{SAMPLE_ID}/control"),
    ("POST", f"/api/campaigns/{SAMPLE_ID}/execution"),
    ("GET", "/openapi.json"),
)


def test_the_authorization_reaches_exactly_the_four_contract_routes(client: TestClient) -> None:
    """The positive half, driven as real requests rather than read off a table."""

    issued = _connect(client, email="contract@vmr.example")
    headers = {"Authorization": f"Bearer {issued['access_token']}", "Origin": EXTENSION_ORIGIN}

    for url in (LABELS_URL, LOOKUP_URL, CAMPAIGNS_URL):
        response = client.get(url, headers=headers)
        assert response.status_code == 200, f"{url} -> {response.status_code} {response.text[:200]}"

    captured = client.post(CAPTURE_URL, json=_fresh_capture(), headers=headers)
    assert captured.status_code == 201, captured.text


@pytest.mark.parametrize(("method", "path"), REFUSED_SURFACE, ids=lambda value: str(value))
def test_the_authorization_reaches_nothing_else(client: TestClient, method: str, path: str) -> None:
    """The negative half. A token good for four routes is good for four routes.

    Every one of these is refused *before routing*, in the middleware, so no
    handler that could reach Gmail, a provider or an administrative action is
    ever entered.
    """

    issued = _connect(client, email=f"narrow-{abs(hash(path)) % 9999}@vmr.example")
    response = client.request(
        method,
        path,
        headers={"Authorization": f"Bearer {issued['access_token']}", "Origin": EXTENSION_ORIGIN},
        json={},
    )
    assert response.status_code in {401, 403}, f"{method} {path} -> {response.status_code}"


def test_the_refused_surface_is_enumerated_rather_than_sampled() -> None:
    """Anti-vacuity: an emptied list would make the parametrised test pass."""

    assert len(REFUSED_SURFACE) == 9, sorted(REFUSED_SURFACE)


def test_gmail_admin_and_sending_authority_did_not_expand() -> None:
    """The contract table itself, asserted key by key and method by method.

    Account linking changed *who* a capture belongs to and *how* the credential
    is obtained. It did not add a route, a method, or a capability, and this is
    the statement of that read from the object the boundary actually consults.
    """

    assert dict(EXTENSION_CAPTURE_CONTRACT) == {
        "/api/intake/contact-captures": frozenset({"POST"}),
        "/api/contact-labels": frozenset({"GET"}),
        "/api/contacts/lookup": frozenset({"GET"}),
        "/api/campaigns": frozenset({"GET"}),
    }
    every_method = {method for methods in EXTENSION_CAPTURE_CONTRACT.values() for method in methods}
    assert every_method == {"GET", "POST"}
    assert not [path for path in EXTENSION_CAPTURE_CONTRACT if "gmail" in path.lower()]


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/gmail/connect"), ("GET", "/gmail/callback"), ("POST", "/gmail/disconnect")],
)
def test_no_gmail_route_is_reachable_with_an_extension_authorization(
    client: TestClient, method: str, path: str
) -> None:
    """Named separately from the list above because Gmail is the named worry."""

    issued = _connect(client, email=f"gmail-{method.lower()}@vmr.example")
    response = client.request(
        method,
        path,
        headers={"Authorization": f"Bearer {issued['access_token']}", "Origin": EXTENSION_ORIGIN},
        json={},
    )
    assert response.status_code in {401, 403}, f"{method} {path} -> {response.status_code}"


# ---------------------------------------------------------------------------
# F. Revocation                                                     (spec 7)
# ---------------------------------------------------------------------------


def test_revoking_a_link_stops_the_very_next_request(client: TestClient) -> None:
    """Disconnect from the extension's side, with its own access token."""

    issued = _connect(client, email="revoke@vmr.example")
    assert _capture(client, issued["access_token"]).status_code == 201

    revoked = client.post(
        "/extension/revoke",
        headers={
            "Authorization": f"Bearer {issued['access_token']}",
            "Origin": EXTENSION_ORIGIN,
        },
    )
    assert revoked.status_code == 204

    refused = _capture(client, issued["access_token"])
    assert refused.status_code == 401
    assert refused.json()["error"] == "unauthorized"

    # And the refresh token cannot resurrect it.
    refreshed = _token(
        client,
        {
            "grant_type": "refresh_token",
            "refresh_token": issued["refresh_token"],
            "extension_id": EXTENSION_ID,
            "installation_id": INSTALLATION_ID,
        },
    )
    assert refreshed.status_code == 400

    rows = _link_rows()
    assert len(rows) == 1
    assert rows[0].revoked_at is not None
    assert rows[0].revoked_reason == "extension_disconnect"


def test_an_operator_can_disconnect_their_own_link_from_the_application(
    client: TestClient,
) -> None:
    """The other side of the same door, and only ever one's own links.

    The account comes from the verified session and never from the request body,
    so this route cannot be pointed at somebody else's install.
    """

    issued = _connect(client, email="self-revoke@vmr.example")
    other = _connect(client, email="bystander@vmr.example", installation_id="install-bystander-01")

    csrf = _attach_session(client, issued["user_id"], "self-revoke@vmr.example")
    disconnected = client.post(
        "/extension/revoke",
        json={},
        headers={"X-CSRF-Token": csrf, "Sec-Fetch-Site": "same-origin"},
    )
    client.cookies.clear()
    assert disconnected.status_code == 204

    assert _capture(client, issued["access_token"]).status_code == 401
    # The bystander's link is untouched: this revoked an account's own links.
    assert _capture(client, other["access_token"]).status_code == 201


# ---------------------------------------------------------------------------
# G. Restart, rotation, and reuse detection                         (spec 8, 9)
# ---------------------------------------------------------------------------


def test_a_browser_restart_reconnects_from_the_refresh_token_alone(client: TestClient) -> None:
    """The product blocker, stated as a test.

    A restart is simulated the only honest way: the in-memory access token is
    thrown away and *only* the persisted refresh token is used. No page is
    opened, no consent is given, no human is involved, and nothing resembling a
    shared secret is entered — and the extension captures again.
    """

    issued = _connect(client, email="restart@vmr.example")
    persisted_refresh = issued["refresh_token"]
    del issued["access_token"]  # the restart: whatever was in memory is gone

    resumed = _token(
        client,
        {
            "grant_type": "refresh_token",
            "refresh_token": persisted_refresh,
            "extension_id": EXTENSION_ID,
            "installation_id": INSTALLATION_ID,
        },
    )
    assert resumed.status_code == 200, resumed.text
    body = resumed.json()
    assert body["access_token"].startswith(f"{ACCESS_TOKEN_SCHEME}.")
    assert body["account"]["email"] == "restart@vmr.example"
    assert _capture(client, body["access_token"]).status_code == 201

    # Still one link. A restart reconnects; it does not accumulate rows.
    assert len(_link_rows()) == 1


def test_refresh_rotates_both_tokens_and_a_replay_revokes_the_link(client: TestClient) -> None:
    """Rotation, then the reason for rotating.

    The old refresh token is dead the moment a new one is issued. Presenting it
    anyway can only mean it was copied before the rotation, so the link is
    revoked outright rather than merely refused — the thief and the real install
    both lose it, and the operator has to reconnect deliberately.
    """

    issued = _connect(client, email="rotation@vmr.example")
    first_refresh = issued["refresh_token"]

    rotated = _token(
        client,
        {
            "grant_type": "refresh_token",
            "refresh_token": first_refresh,
            "extension_id": EXTENSION_ID,
            "installation_id": INSTALLATION_ID,
        },
    )
    assert rotated.status_code == 200
    second = rotated.json()
    assert second["refresh_token"] != first_refresh
    assert second["access_token"] != issued["access_token"]
    # The new access token works, so rotation is not just invalidation.
    assert _capture(client, second["access_token"]).status_code == 201

    replayed = _token(
        client,
        {
            "grant_type": "refresh_token",
            "refresh_token": first_refresh,
            "extension_id": EXTENSION_ID,
            "installation_id": INSTALLATION_ID,
        },
    )
    assert replayed.status_code == 400
    assert replayed.json() == {"error": "invalid_grant"}

    rows = _link_rows()
    assert len(rows) == 1
    assert rows[0].revoked_at is not None
    assert rows[0].revoked_reason == "refresh_token_reuse"

    # The whole family is dead: the token that was legitimately current is gone
    # too, which is the point of revoking rather than refusing.
    assert _capture(client, second["access_token"]).status_code == 401
    assert (
        _token(
            client,
            {
                "grant_type": "refresh_token",
                "refresh_token": second["refresh_token"],
                "extension_id": EXTENSION_ID,
                "installation_id": INSTALLATION_ID,
            },
        ).status_code
        == 400
    )


# ---------------------------------------------------------------------------
# H. PKCE, and single use                                           (spec 10)
# ---------------------------------------------------------------------------


def test_a_wrong_code_verifier_is_refused(client: TestClient) -> None:
    """The property that makes an observed code worthless.

    The verifier never leaves the extension, so somebody who reads the code out
    of the redirect — from a log, from history, from a shoulder — still has
    nothing.
    """

    account = seed_account(email="pkce@vmr.example")
    csrf = _attach_session(client, account.user_id, account.email)
    _, challenge = _pkce()
    granted = _consent(client, csrf, challenge)
    code = _code_from(granted.headers["location"])
    client.cookies.clear()

    wrong_verifier, _ = _pkce()
    refused = _token(
        client,
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": wrong_verifier,
            "extension_id": EXTENSION_ID,
            "installation_id": INSTALLATION_ID,
        },
    )
    assert refused.status_code == 400
    assert refused.json() == {"error": "invalid_grant"}
    assert _link_rows() == []


def test_a_code_presented_with_a_wrong_verifier_cannot_be_retried(client: TestClient) -> None:
    """Single use means single *presentation*, not single success.

    A code that survived a failed attempt would let somebody holding a stolen
    code grind at the verifier. It does not: the first presentation consumes it,
    so even the rightful owner's correct verifier is too late.
    """

    account = seed_account(email="pkce-retry@vmr.example")
    csrf = _attach_session(client, account.user_id, account.email)
    verifier, challenge = _pkce()
    granted = _consent(client, csrf, challenge)
    code = _code_from(granted.headers["location"])
    client.cookies.clear()

    wrong_verifier, _ = _pkce()
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": wrong_verifier,
        "extension_id": EXTENSION_ID,
        "installation_id": INSTALLATION_ID,
    }
    assert _token(client, body).status_code == 400
    assert _token(client, {**body, "code_verifier": verifier}).status_code == 400
    assert _link_rows() == []


def test_a_code_is_single_use_on_the_happy_path_too(client: TestClient) -> None:
    """A replayed code, with the right verifier, still only works once."""

    account = seed_account(email="replay@vmr.example")
    csrf = _attach_session(client, account.user_id, account.email)
    verifier, challenge = _pkce()
    granted = _consent(client, csrf, challenge)
    code = _code_from(granted.headers["location"])
    client.cookies.clear()

    body = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "extension_id": EXTENSION_ID,
        "installation_id": INSTALLATION_ID,
    }
    assert _token(client, body).status_code == 200
    replayed = _token(client, body)
    assert replayed.status_code == 400
    assert replayed.json() == {"error": "invalid_grant"}
    assert len(_link_rows()) == 1


def test_a_challenge_method_other_than_s256_is_refused(client: TestClient) -> None:
    """``plain`` is not PKCE, and no client of this application needs it."""

    account = seed_account(email="plain@vmr.example")
    _attach_session(client, account.user_id, account.email)
    _, challenge = _pkce()
    refused = client.get(_authorize_url(challenge, method="plain"))
    assert refused.status_code == 400
    assert "location" not in refused.headers


# ---------------------------------------------------------------------------
# I. The legacy credential is development-only                      (spec 11)
# ---------------------------------------------------------------------------


def test_the_legacy_shared_credential_is_inert_in_hosted_mode(client: TestClient) -> None:
    """A correctly configured ``vmrx1`` credential is worth nothing in staging.

    This deployment lists the credential and the origin, the switch is on, and
    the request is perfectly formed. It is refused anyway, because the whole
    scheme is gated on ``APP_ENV=local`` — which is what makes "no reusable
    shared secret authorises a hosted capture" a property of the code rather than
    a promise about the environment file.
    """

    headers = {"Authorization": f"Bearer {LEGACY_CREDENTIAL}", "Origin": EXTENSION_ORIGIN}
    captured = client.post(CAPTURE_URL, json=_fresh_capture(), headers=headers)
    assert captured.status_code == 401
    assert captured.json()["error"] == "unauthorized"

    for url in (LABELS_URL, LOOKUP_URL, CAMPAIGNS_URL):
        assert client.get(url, headers=headers).status_code == 401, url


def test_the_legacy_shared_credential_still_works_under_app_env_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half, so the test above is an environment gate and not a break.

    Same credential, same origin, same request — and it captures, because this
    build says ``APP_ENV=local``. Local development keeps the compatibility path
    it has always had; nothing hosted depends on it.
    """

    local = _build(monkeypatch, _local_env(), base_url="http://localhost")
    try:
        headers = {"Authorization": f"Bearer {LEGACY_CREDENTIAL}", "Origin": EXTENSION_ORIGIN}
        captured = local.post(CAPTURE_URL, json=_fresh_capture(), headers=headers)
        assert captured.status_code == 201, captured.text
        assert local.get(LABELS_URL, headers=headers).status_code == 200
    finally:
        get_settings.cache_clear()


def test_the_hosted_deployment_needs_no_legacy_credential_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Account linking alone is a complete hosted capture boundary.

    Stated as a startup test because the alternative — a runtime rule that still
    demanded a ``vmrx1`` entry — would have forced every hosted deployment to
    mint the very shared secret this work exists to stop issuing.
    """

    linked_only = _env(
        EXTENSION_AUTH__ENABLED="false",
        EXTENSION_AUTH__CREDENTIALS="[]",
    )
    client = _build(monkeypatch, linked_only, base_url=ORIGIN)
    try:
        issued = _connect(client, email="link-only@vmr.example")
        assert _capture(client, issued["access_token"]).status_code == 201
        legacy = client.post(
            CAPTURE_URL,
            json=_fresh_capture(),
            headers={"Authorization": f"Bearer {LEGACY_CREDENTIAL}", "Origin": EXTENSION_ORIGIN},
        )
        assert legacy.status_code == 401
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# J. Hostile input never becomes an exception
# ---------------------------------------------------------------------------

# A fixed identifier, deliberately not generated.
#
# These parameter lists used `uuid.uuid4()`, which is evaluated at *collection*
# time. `scripts/ci_shards.py verify` collects the suite once for the whole run and
# once per shard, then proves the union of the shards is exactly the suite — so a
# value minted during collection makes the same test a different test on the second
# pass, and nine cases appeared as both "omitted" and "extra" (CI #378). That is a
# test-identity defect, not a product one, and the accounting was right to fail.
#
# Nothing here ever needed randomness. Each case pins a *shape* the parser must
# refuse — a dashed UUID where 32 hex characters are required, uppercase hex, a
# secret one character short or long, a non-base64url character, the sibling scheme
# presented under the wrong one — and a shape is stated better by a constant, which
# also makes the node id readable and the failure reproducible.
#
# The value is a sentinel no test ever inserts, so the "this session does not
# exist" property the HTTP cases below rely on still holds.
UNKNOWN_SESSION_UUID = uuid.UUID("00000000-0000-4000-8000-00000000dead")
#: The 32-character lowercase hex form this scheme actually mints.
UNKNOWN_SESSION_HEX = UNKNOWN_SESSION_UUID.hex
#: The 36-character dashed form, which the scheme must refuse.
UNKNOWN_SESSION_DASHED = str(UNKNOWN_SESSION_UUID)


@pytest.mark.parametrize(
    "presented",
    [
        "",
        "Bearer",
        f"Bearer {ACCESS_TOKEN_SCHEME}",
        f"Bearer {ACCESS_TOKEN_SCHEME}.deadbeef",
        f"Bearer {ACCESS_TOKEN_SCHEME}.{'z' * 32}.{'a' * 43}",
        f"Bearer {ACCESS_TOKEN_SCHEME}.{UNKNOWN_SESSION_DASHED}.{'a' * 43}",
        f"Bearer {REFRESH_TOKEN_SCHEME}.{UNKNOWN_SESSION_HEX}.{'a' * 43}",
        "Bearer " + "x" * 5000,
        f"Basic {ACCESS_TOKEN_SCHEME}.{UNKNOWN_SESSION_HEX}.{'a' * 43}",
    ],
)
def test_a_malformed_token_is_a_refusal_and_never_an_exception(
    client: TestClient, presented: str
) -> None:
    """Attacker-controlled text must produce a decision, not a 500.

    A refresh token presented as a bearer is in this list on purpose: the two
    schemes are versioned separately so that a token minted under one can never
    be read under the other's rules, in either direction.
    """

    response = client.get(
        LABELS_URL, headers={"Authorization": presented, "Origin": EXTENSION_ORIGIN}
    )
    assert response.status_code == 401, presented[:40]
    assert response.json()["error"] == "unauthorized"


@pytest.mark.parametrize(
    "presented",
    [
        None,
        "",
        ".",
        "..",
        f"{ACCESS_TOKEN_SCHEME}.{UNKNOWN_SESSION_HEX}.{'a' * 43}é",
        f"{ACCESS_TOKEN_SCHEME}.{UNKNOWN_SESSION_HEX}.{'a' * 42}",
        f"{ACCESS_TOKEN_SCHEME}.{UNKNOWN_SESSION_HEX}.{'a' * 65}",
        f"{ACCESS_TOKEN_SCHEME}.{UNKNOWN_SESSION_DASHED}.{'a' * 43}",
        f"{ACCESS_TOKEN_SCHEME}.{UNKNOWN_SESSION_HEX.upper()}.{'a' * 43}",
        f"{ACCESS_TOKEN_SCHEME}.{UNKNOWN_SESSION_HEX}.{'a' * 40}+/=",
        "x" * 100_000,
    ],
)
def test_parsing_hostile_input_returns_none_rather_than_raising(presented: str | None) -> None:
    """Asserted against the parser directly, where no HTTP client can have helped.

    ``httpx`` refuses to put a non-ASCII byte in a header at all, so the only
    honest place to pin the non-ASCII case — and the multi-megabyte case — is the
    function that has to survive it. Same discipline as
    ``parse_presented_credential``: every shape this scheme did not mint is
    ``None``, and none of them is an exception.
    """

    from app.core.auth.extension_link import parse_link_token

    assert parse_link_token(presented, scheme=ACCESS_TOKEN_SCHEME) is None


def test_no_response_on_this_boundary_echoes_a_secret(client: TestClient) -> None:
    """Nothing that was issued may come back out of a refusal."""

    issued = _connect(client, email="echo@vmr.example")
    access_secret = issued["access_token"].split(".")[2]
    refresh_secret = issued["refresh_token"].split(".")[2]

    responses = [
        _capture(client, issued["access_token"], origin=HOSTILE_ORIGIN),
        _token(
            client,
            {
                "grant_type": "refresh_token",
                "refresh_token": issued["refresh_token"],
                "extension_id": EXTENSION_ID,
                "installation_id": "unknown-installation-9999",
            },
        ),
        client.get(LABELS_URL, headers={"Authorization": "Bearer nonsense"}),
    ]
    for response in responses:
        haystack = response.text + json.dumps(dict(response.headers))
        assert access_secret not in haystack
        assert refresh_secret not in haystack


# ---------------------------------------------------------------------------
# K. The authorization window can actually read the refusal
# ---------------------------------------------------------------------------
#
# Live Chrome UAT, 2026-08-16, against the hosted deployment. An operator signed
# in to VMR Outbound in the same profile clicked "Sign in to VMR Outbound" and
# was told, ~90ms later, "VMR Outbound could not be reached." The deployment was
# up and answered every request put to it.
#
# `chrome.identity.launchWebAuthFlow` does not render the authorization URL the
# way a tab does. Chromium's `WebAuthFlow` watches the main-frame navigation it
# started and treats ANY response of 400 or above as a failed load: it destroys
# the window before paint and rejects with "Authorization page could not be
# loaded." Every refusal this router renders carried such a status, so the
# refusal page -- which says, in plain words, that this install is not approved
# for this deployment -- had never once been shown to an operator. What reached
# them was the extension's reading of Chrome's message, and Chrome uses those
# same words for a server that is not there at all.
#
# So the page a human is about to read is served with 200, and a program that
# checks a status still gets the 4xx. Nothing about WHAT is refused changed, and
# section D above still holds every one of those refusals closed.

#: Exactly the shape Chrome puts on the authorization navigation.
NAVIGATION_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,*/*;q=0.8",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Site": "cross-site",
}

#: An extension id of the right shape that this deployment does not approve.
UNAPPROVED_EXTENSION_ID = "qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"

#: Every refusal reachable from a well-formed-looking authorization request.
REFUSAL_CASES = [
    ("unknown_extension", {"extension_id": UNAPPROVED_EXTENSION_ID}),
    ("bad_redirect", {"redirect_uri": "https://attacker.example/"}),
    ("bad_challenge_method", {"method": "plain"}),
    # Both values are deliberately long and distinctive rather than minimal:
    # the echo assertion below searches the rendered page for them, and a
    # two-character value would match ordinary page text ("noindex") and
    # fail for a reason that has nothing to do with echoing.
    ("bad_installation", {"installation_id": "installation~id~with~bad~characters"}),
    ("bad_state", {"state": "state~with~characters~this~scheme~never~mints"}),
]


@pytest.mark.parametrize(("case", "kwargs"), REFUSAL_CASES)
def test_a_refusal_reaches_the_authorization_window_instead_of_killing_it(
    client: TestClient, case: str, kwargs: dict[str, str]
) -> None:
    """The live defect. A refusal must be readable, and must still refuse.

    Two properties, and the second is why the first is safe: the operator gets a
    page they can act on, and getting one grants nothing.
    """

    account = seed_account(email=f"window-{case}@vmr.example")
    _attach_session(client, account.user_id, account.email)
    _, challenge = _pkce()

    refused = client.get(_authorize_url(challenge, **kwargs), headers=NAVIGATION_HEADERS)

    # Chromium destroys the auth window for anything at or above 400, so this is
    # the whole fix: below 400 the page renders and the operator reads it.
    assert refused.status_code < 400, (
        f"{case}: a status of {refused.status_code} is destroyed unread by "
        "launchWebAuthFlow, and the operator is told the deployment is unreachable"
    )
    assert "text/html" in refused.headers["content-type"]
    # It is still a refusal: no code, no redirect to the extension, no link.
    assert "location" not in refused.headers
    assert "code=" not in refused.text
    assert ".chromiumapp.org" not in refused.text
    assert _link_rows() == []
    # And it does not echo the value that failed back onto a VMR-origin page.
    for value in kwargs.values():
        assert value not in refused.text, case


@pytest.mark.parametrize(("case", "kwargs"), REFUSAL_CASES)
def test_a_programmatic_caller_still_gets_the_error_status(
    client: TestClient, case: str, kwargs: dict[str, str]
) -> None:
    """The status did not go away. It is chosen by who is reading.

    A ``fetch``/XHR caller checks a status rather than reading a page, so it
    keeps the one it always got. This is the half that proves the change above
    is about rendering and not about relaxing a refusal.
    """

    account = seed_account(email=f"api-{case}@vmr.example")
    _attach_session(client, account.user_id, account.email)
    _, challenge = _pkce()

    refused = client.get(
        _authorize_url(challenge, **kwargs),
        headers={"Accept": "*/*", "Sec-Fetch-Mode": "cors"},
    )
    assert refused.status_code == 400, case
    assert _link_rows() == []


def test_an_html_fetch_that_is_not_a_navigation_still_gets_the_error_status(
    client: TestClient,
) -> None:
    """``fetch()`` can ask for HTML. Asking for it is not navigating to it.

    The same distinction the sign-in redirect already makes, decided by the same
    predicate, so the two cannot drift apart.
    """

    account = seed_account(email="htmlfetch@vmr.example")
    _attach_session(client, account.user_id, account.email)
    _, challenge = _pkce()

    refused = client.get(
        _authorize_url(challenge, method="plain"),
        headers={"Accept": "text/html", "Sec-Fetch-Mode": "cors"},
    )
    assert refused.status_code == 400


def test_a_signed_in_operator_with_a_valid_request_reaches_consent(
    client: TestClient,
) -> None:
    """Experience A, driven with the real navigation headers.

    Covered by section A through a bare client already; repeated here as a
    navigation so the success path is pinned under the same conditions as the
    refusals -- a consent page Chrome cannot render is as broken as a refusal it
    cannot render.
    """

    account = seed_account(email="consent-nav@vmr.example")
    _attach_session(client, account.user_id, account.email)
    _, challenge = _pkce()

    landing = client.get(_authorize_url(challenge), headers=NAVIGATION_HEADERS)
    assert landing.status_code == 200
    assert "text/html" in landing.headers["content-type"]
    # The consent form, carrying the request forward -- not a code.
    assert 'name="code_challenge"' in landing.text
    assert "code=" not in landing.text


def test_a_signed_out_operator_is_sent_through_sign_in_and_comes_back(
    client: TestClient,
) -> None:
    """Experience B, end to end through the real middleware and the real route.

    The pre-login entrypoint has to *start* a sign-in rather than refuse, and the
    authorization request has to survive it byte for byte -- including the
    percent-encoded ``redirect_uri`` that #286 stopped ``safe_next_path`` from
    discarding. Both halves are asserted here because either one alone leaves the
    authorization window with nowhere to go.
    """

    from app.core.auth.policy import safe_next_path

    _, challenge = _pkce()
    target = _authorize_url(challenge)

    # 1. Signed out: a redirect INTO sign-in, never a bare 401 page.
    started = client.get(target, headers=NAVIGATION_HEADERS)
    assert started.status_code == 303
    location = started.headers["location"]
    assert location.startswith("/auth/login?next=")

    # 2. The destination survives the round trip exactly.
    handed_back = unquote(location.partition("next=")[2])
    assert handed_back == target
    assert safe_next_path(handed_back, fallback="/app") == target
    assert "redirect_uri=https%3A%2F%2F" in handed_back

    # 3. Sign in, then follow the preserved destination back to authorization.
    account = seed_account(email="roundtrip@vmr.example")
    _attach_session(client, account.user_id, account.email)
    landed = client.get(safe_next_path(handed_back, fallback="/app"), headers=NAVIGATION_HEADERS)
    assert landed.status_code == 200
    assert 'name="code_challenge"' in landed.text


@pytest.mark.parametrize(
    "unsafe",
    [
        "//evil.example/app",
        "/\\evil.example",
        "https://evil.example/extension/authorize",
        "/extension/authorize%2f%2fevil.example",
        "/auth/login?next=/app",
        "/healthz",
    ],
)
def test_an_unsafe_destination_is_still_discarded(unsafe: str) -> None:
    """#286's hardening is not relaxed by anything above.

    The narrowing that let the encoded ``redirect_uri`` survive applies to the
    query string only. Every rule still applies to the whole value, and an
    encoded separator in the PATH is still refused.
    """

    from app.core.auth.policy import safe_next_path

    assert safe_next_path(unsafe, fallback="/app") == "/app", unsafe

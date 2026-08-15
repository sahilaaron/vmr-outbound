"""Adversarial coverage for the hosted-operator authentication boundary.

The suite is written from the attacker's side. Every test below names something
a real caller would try — an anonymous write, a Google account that is valid but
not approved, a replayed callback, a forged signature, a cross-site form post, a
path spelled to dodge a prefix match — and asserts the refusal. The
happy-path tests exist mostly to prove the refusals are not vacuous.

No test contacts Google. Signatures are real RS256 signatures over keys the
suite generates; the JWKS and the token endpoint are served through an
``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import unquote, urlencode

import httpx
import pytest
from app.core.auth.config import AuthSettings, normalize_operator_email
from app.core.auth.identity import IdentityAssertionError, validate_identity_claims
from app.core.auth.jwks import JwksClient, verify_id_token
from app.core.auth.policy import is_anonymous_path, normalize_request_path, safe_next_path
from app.core.auth.session import (
    LOGIN_TRANSACTION_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    OperatorSession,
    SessionCodec,
    SessionDecodeError,
    new_session_id,
)
from app.core.auth.startup import HostedAuthConfigurationError
from app.core.config import Settings, get_settings
from app.db.session import SessionLocal
from app.main import create_app
from app.models.enums import UserState
from app.models.user import User
from app.services.users import service as user_service
from fastapi.testclient import TestClient

from tests.hosted_auth_factory import (
    TEST_CLIENT_ID,
    TEST_ISSUER,
    TEST_JWKS_URI,
    RecordingIdentityProvider,
    SeededAccount,
    SigningKey,
    b64url_json,
    google_claims,
    id_token,
    jwks_transport,
    operator_claims,
    seed_account,
    subject_for,
)

STAGING_HOST = "srv1885453.hstgr.cloud"
STAGING_ORIGIN = f"https://{STAGING_HOST}"
APPROVED_EMAIL = "operator@vmr.example"
# The `sub` the identity fixtures mint for the approved address. The account is
# seeded already linked to it so that the Google path resolves to the same row
# every time — and so that a *different* address, which mints a different
# subject, cannot resolve to it.
GOOGLE_SUBJECT = subject_for(APPROVED_EMAIL)
SESSION_SECRET = "test-session-secret-value-at-least-32-chars"

# A staging database URL that satisfies the production-like runtime rules. No
# test connects to it: the ORM session factory was bound to the real test
# database when `app.db.session` was first imported.
STAGING_DATABASE_URL = "postgresql+psycopg://vmr:secret@db.internal.example:5432/vmr_staging"


class _AlwaysReadyProbe:
    def __call__(self) -> None:
        return None


def _base_env(**overrides: str) -> dict[str, str]:
    env = {
        "APP_ENV": "staging",
        "DEBUG": "false",
        "DRY_RUN": "true",
        "TRUSTED_HOSTS": f'["{STAGING_HOST}"]',
        "DATABASE_URL": STAGING_DATABASE_URL,
        "FEATURES__WORKBENCH": "true",
        "AUTH__ENABLED": "true",
        "AUTH__SESSION_SECRET": SESSION_SECRET,
        "AUTH__ALLOWED_OPERATOR_EMAILS": f'["{APPROVED_EMAIL}"]',
        "AUTH__GOOGLE_CLIENT_ID": TEST_CLIENT_ID,
        "AUTH__GOOGLE_CLIENT_SECRET": "test-client-secret",
        "AUTH__PUBLIC_BASE_URL": STAGING_ORIGIN,
    }
    env.update(overrides)
    return env


def _apply(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def _build(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    *,
    identity_provider: Any = None,
) -> TestClient:
    _apply(monkeypatch, env)
    app = create_app(readiness_probe=_AlwaysReadyProbe(), identity_provider=identity_provider)
    return TestClient(app, base_url=STAGING_ORIGIN, follow_redirects=False)


@pytest.fixture
def provider() -> RecordingIdentityProvider:
    return RecordingIdentityProvider()


@pytest.fixture(autouse=True)
def approved_account() -> SeededAccount:
    """The approved operator, as an account row.

    Autouse for the whole module because authorization moved from the configured
    allow-list to the ``users`` table in #270: without this row, every test that
    used to pass by virtue of ``AUTH__ALLOWED_OPERATOR_EMAILS`` would now be
    asserting a refusal it did not intend. Seeding it here keeps each test about
    the one thing it was written to prove.

    The row is committed and the suite's autouse truncation sweep removes it, so
    every test gets a fresh account with ``auth_version = 1``.

    An administrator, for the same reason the row exists at all. This file is
    about *authentication* -- sessions, CSRF, cross-site handling, revocation --
    and it exercises those through writes to ``/api/...``, which is now
    administrator-only for session callers. An ordinary operator would be
    refused on authorization before reaching the behaviour each test was written
    to prove, which is the same failure mode #270 caused and this fixture was
    added to fix. Role itself is covered in ``tests/test_user_accounts.py`` and
    ``tests/test_route_authorization.py``.
    """

    # A second administrator, so that the "last active administrator" guard
    # is not silently under test here. Two tests in this file disable and
    # reactivate the approved account to prove a session dies with it; with
    # only one administrator in the database the user service refuses the
    # disable outright and the session assertion is never reached. That
    # guard has its own coverage in tests/test_user_accounts.py.
    seed_account(
        email="second-admin@vmr.example",
        google_subject="google-sub-second-admin",
        role="admin",
    )
    return seed_account(email=APPROVED_EMAIL, google_subject=GOOGLE_SUBJECT, role="admin")


@pytest.fixture
def staging_client(
    monkeypatch: pytest.MonkeyPatch, provider: RecordingIdentityProvider
) -> Iterator[TestClient]:
    client = _build(monkeypatch, _base_env(), identity_provider=provider)
    try:
        yield client
    finally:
        get_settings.cache_clear()


def _settings_for(**overrides: Any) -> Settings:
    return Settings(app_env="staging", auth=AuthSettings(**overrides))


def _codec() -> SessionCodec:
    return SessionCodec(SESSION_SECRET)


def _session_cookie(
    account: SeededAccount,
    *,
    email: str | None = None,
    issued_at: int | None = None,
    expires_at: int | None = None,
    session_id: str | None = None,
    auth_version: int | None = None,
) -> tuple[str, str]:
    """A validly signed session cookie plus its matching CSRF token.

    Takes the account explicitly since #270: a cookie now names the account it
    belongs to and the revocation generation it was minted under, and the
    boundary refuses one whose ``uid`` resolves to nothing.
    """

    now = int(time.time())
    sid = session_id or new_session_id()
    session = OperatorSession(
        email=email or account.email,
        subject=GOOGLE_SUBJECT,
        display_name="VMR Operator",
        session_id=sid,
        issued_at=now if issued_at is None else issued_at,
        expires_at=now + 3600 if expires_at is None else expires_at,
        user_id=account.user_id,
        auth_version=account.auth_version if auth_version is None else auth_version,
    )
    codec = _codec()
    return codec.encode_session(session), codec.csrf_token(sid)


def _sign_in(client: TestClient, provider: RecordingIdentityProvider, **claim_overrides: Any):
    """Drive the real sign-in round trip and return the callback response."""

    started = client.get("/auth/google/start?next=%2Fapp%2Fcampaigns")
    assert started.status_code == 303
    transaction = provider.authorization_calls[-1]
    provider.claims = operator_claims(nonce=transaction["nonce"], **claim_overrides)
    return client.get(f"/auth/callback?code=test-code&state={transaction['state']}"), transaction


# ---------------------------------------------------------------------------
# A. The startup contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected_fragment"),
    [
        ({"AUTH__ENABLED": "false"}, "AUTH__ENABLED must be true"),
        ({"AUTH__SESSION_SECRET": ""}, "AUTH__SESSION_SECRET"),
        ({"AUTH__SESSION_SECRET": "too-short"}, "AUTH__SESSION_SECRET"),
        # The allow-list may now legitimately be empty — it became a one-time
        # seed rather than a gate in #270. What must not be empty is the
        # bootstrap administrator, because access is granted by an account row
        # and a deployment with no administrator cannot create the first one.
        ({"AUTH__BOOTSTRAP_ADMIN_EMAIL": ""}, "AUTH__BOOTSTRAP_ADMIN_EMAIL"),
        ({"AUTH__GOOGLE_CLIENT_ID": ""}, "AUTH__GOOGLE_CLIENT_ID"),
        ({"AUTH__GOOGLE_CLIENT_SECRET": ""}, "AUTH__GOOGLE_CLIENT_SECRET"),
        ({"AUTH__PUBLIC_BASE_URL": ""}, "AUTH__PUBLIC_BASE_URL"),
        ({"AUTH__PUBLIC_BASE_URL": f"http://{STAGING_HOST}"}, "must use HTTPS"),
        ({"AUTH__COOKIE_SECURE": "false"}, "AUTH__COOKIE_SECURE"),
    ],
)
def test_staging_refuses_to_start_with_an_incomplete_auth_boundary(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, str], expected_fragment: str
) -> None:
    """Every missing half of the boundary is a refusal, not a warning."""

    _apply(monkeypatch, _base_env(**overrides))
    try:
        with pytest.raises(HostedAuthConfigurationError) as caught:
            create_app(readiness_probe=_AlwaysReadyProbe())
        assert expected_fragment in str(caught.value)
    finally:
        get_settings.cache_clear()


def test_staging_workbench_with_complete_auth_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the slice: staging + Workbench is now legal when safe."""

    _apply(monkeypatch, _base_env())
    try:
        app = create_app(readiness_probe=_AlwaysReadyProbe())
        client = TestClient(app, base_url=STAGING_ORIGIN, follow_redirects=False)
        # 401 rather than 404 is the assertion that matters: the surfaces exist
        # and are protected, rather than simply not being mounted.
        assert client.get("/app").status_code == 401
        assert client.get("/admin").status_code == 401
    finally:
        get_settings.cache_clear()


def test_with_the_workbench_off_the_ui_is_absent_not_merely_hidden(
    monkeypatch: pytest.MonkeyPatch, provider: RecordingIdentityProvider
) -> None:
    """The complement to the anonymous 401: an approved operator gets a real 404.

    Anonymous callers cannot distinguish an unmounted path from a protected one,
    which is deliberate. This is the test that proves the feature switch still
    genuinely controls what exists.
    """

    client = _build(monkeypatch, _base_env(FEATURES__WORKBENCH="false"), identity_provider=provider)
    try:
        response, _ = _sign_in(client, provider)
        assert response.status_code == 303
        assert client.get("/admin").status_code == 404
        assert client.get("/app").status_code == 404
        # The API surface behind the boundary is mounted and reachable.
        assert client.get("/api/agents").status_code == 200
    finally:
        get_settings.cache_clear()


def test_startup_reports_every_problem_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator setting this up should need one restart, not four."""

    _apply(
        monkeypatch,
        _base_env(
            AUTH__SESSION_SECRET="",
            AUTH__BOOTSTRAP_ADMIN_EMAIL="",
            AUTH__GOOGLE_CLIENT_ID="",
            AUTH__PUBLIC_BASE_URL="",
        ),
    )
    try:
        with pytest.raises(HostedAuthConfigurationError) as caught:
            create_app(readiness_probe=_AlwaysReadyProbe())
        message = str(caught.value)
        assert message.count("\n- ") == 4
    finally:
        get_settings.cache_clear()


def test_production_refuses_the_workbench_outright(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production access policy is undecided, so it fails closed rather than
    inheriting the staging rule by accident."""

    _apply(monkeypatch, _base_env(APP_ENV="production"))
    try:
        with pytest.raises(HostedAuthConfigurationError) as caught:
            create_app(readiness_probe=_AlwaysReadyProbe())
        assert "may not be enabled in production" in str(caught.value)
    finally:
        get_settings.cache_clear()


def test_production_still_requires_authentication_for_the_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _apply(
        monkeypatch,
        _base_env(APP_ENV="production", FEATURES__WORKBENCH="false", AUTH__ENABLED="false"),
    )
    try:
        with pytest.raises(HostedAuthConfigurationError) as caught:
            create_app(readiness_probe=_AlwaysReadyProbe())
        assert "AUTH__ENABLED must be true" in str(caught.value)
    finally:
        get_settings.cache_clear()


def test_startup_refusal_never_echoes_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply(
        monkeypatch,
        _base_env(
            AUTH__SESSION_SECRET="TOP-SECRET-VALUE-THAT-IS-LONG-ENOUGH-XX",
            AUTH__GOOGLE_CLIENT_SECRET="TOP-SECRET-CLIENT",
            AUTH__PUBLIC_BASE_URL="",
        ),
    )
    try:
        with pytest.raises(HostedAuthConfigurationError) as caught:
            create_app(readiness_probe=_AlwaysReadyProbe())
        assert "TOP-SECRET" not in str(caught.value)
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# B. Local development is unchanged
# ---------------------------------------------------------------------------


def test_local_development_needs_no_auth_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No developer is pushed through Google to use localhost."""

    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    get_settings.cache_clear()
    try:
        app = create_app()
        with TestClient(app, follow_redirects=False) as client:
            assert client.get("/app").status_code == 200
            assert client.get("/admin").status_code == 200
    finally:
        get_settings.cache_clear()


def test_local_forms_emit_no_csrf_field_when_auth_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local markup is byte-identical to before this slice."""

    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    get_settings.cache_clear()
    try:
        with TestClient(create_app(), follow_redirects=False) as client:
            body = client.get("/app/campaigns/new").text
        assert "<form" in body
        assert 'name="_csrf"' not in body
    finally:
        get_settings.cache_clear()


def test_local_writes_are_accepted_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    get_settings.cache_clear()
    try:
        with TestClient(create_app(), follow_redirects=False) as client:
            response = client.post("/api/campaigns", json={"name": "Local campaign"})
        assert response.status_code == 201
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# C. Anonymous access policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/app", "/app/campaigns", "/admin", "/admin/campaigns", "/"])
def test_anonymous_browser_navigation_is_redirected_to_sign_in(
    staging_client: TestClient, path: str
) -> None:
    response = staging_client.get(path, headers={"accept": "text/html"})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/login")


@pytest.mark.parametrize(
    "path",
    [
        "/app",
        "/admin",
        "/api/campaigns",
        "/api/agents",
        "/api/collections",
        "/docs",
        "/redoc",
        "/openapi.json",
    ],
)
def test_anonymous_non_browser_reads_are_refused(staging_client: TestClient, path: str) -> None:
    response = staging_client.get(path)
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/campaigns", {"name": "Anonymous"}),
        ("post", "/campaigns", {"name": "Anonymous"}),
        ("post", "/api/collections", {"name": "Anonymous"}),
        ("post", "/api/campaigns/1/execution", {"enabled": True}),
        ("patch", "/api/campaigns/1", {"name": "Anonymous"}),
        ("put", "/api/agents/research/control", {"status": "paused"}),
        ("delete", "/api/collections/1/contacts/1", None),
        ("post", "/app/campaigns/new", None),
        ("post", "/admin/jobs/1/retry", None),
        ("post", "/campaigns/create", None),
        ("post", "/knowledge-base/company", None),
        ("post", "/admin/companies/1/intelligence/run", None),
    ],
)
def test_anonymous_writes_are_refused_across_the_whole_surface(
    staging_client: TestClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    """Not only the two endpoints named in #247 — the entire write surface."""

    call = getattr(staging_client, method)
    response = call(path, json=body) if body is not None else call(path)
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


def test_an_anonymous_write_is_never_answered_with_a_redirect(
    staging_client: TestClient,
) -> None:
    """A 303 on a POST is followed as a GET and can look like success."""

    response = staging_client.post(
        "/api/campaigns", json={"name": "X"}, headers={"accept": "text/html"}
    )
    assert response.status_code == 401
    assert "location" not in response.headers


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/version", "/health", "/ready"])
def test_probes_remain_anonymous_and_unchanged(staging_client: TestClient, path: str) -> None:
    assert staging_client.get(path).status_code == 200


def test_health_and_readiness_contracts_are_unchanged(staging_client: TestClient) -> None:
    assert staging_client.get("/healthz").json() == {"status": "ok"}
    assert staging_client.get("/readyz").json() == {
        "status": "ready",
        "checks": {"configuration": "ok", "database": "ok"},
    }


def test_the_sign_in_page_is_reachable_and_carries_no_application_data(
    staging_client: TestClient,
) -> None:
    response = staging_client.get("/auth/login")
    assert response.status_code == 200
    body = response.text
    assert "Sign in with Google" in body
    # Nothing about who is approved, and no navigation into the application.
    assert APPROVED_EMAIL not in body
    assert "/app/campaigns" not in body


# ---------------------------------------------------------------------------
# D. Path-form bypass attempts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/admin/", "/admin"),
        ("//admin", "/admin"),
        ("///admin//", "/admin"),
        ("/admin/./", "/admin"),
        ("/healthz/../admin", "/admin"),
        ("/./healthz/..//app", "/app"),
        ("/", "/"),
    ],
)
def test_alternate_path_spellings_normalise_to_the_protected_form(raw: str, expected: str) -> None:
    assert normalize_request_path(raw) == expected


@pytest.mark.parametrize(
    "path",
    [
        "/healthz/../admin",
        "/version/../app",
        "/static/../admin",
        "/auth/../admin",
        "//admin",
        "/admin/",
    ],
)
def test_traversal_through_an_anonymous_prefix_is_still_protected(path: str) -> None:
    assert is_anonymous_path(path) is False


@pytest.mark.parametrize("path", ["/healthz", "/auth/login", "/auth/callback", "/static/app.css"])
def test_the_anonymous_allow_list_is_exactly_what_it_claims(path: str) -> None:
    assert is_anonymous_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/auth",
        "/auth/",
        "/auth/x",
        "/auth/x/y",
        "/auth/future-route",
        "/auth/..;/app",
        "/static",
        "/authx",
        "/staticky",
    ],
)
def test_an_unmounted_path_under_a_former_anonymous_prefix_is_protected(path: str) -> None:
    """M-3: anonymity is granted by exact path, so a prefix cannot leak one.

    `/auth/*` and `/static/*` used to be anonymous *prefixes*, which meant two
    things at once: anything ever mounted under them would be public without
    anyone deciding it, and an unmounted path under them answered 404 while every
    other unknown path answered 401 — a route-enumeration difference the slice's
    own handoff claimed was impossible.
    """

    assert is_anonymous_path(path) is False


def test_an_alternate_spelling_cannot_reach_a_protected_route(
    staging_client: TestClient,
) -> None:
    for path in ("//admin", "/admin/", "/healthz/../admin"):
        response = staging_client.get(path, headers={"accept": "application/json"})
        assert response.status_code in {401, 404}, path
        if response.status_code == 401:
            assert response.json()["error"] == "unauthorized"


@pytest.mark.parametrize(
    "candidate",
    [
        "https://evil.example/steal",
        "//evil.example/steal",
        "/\\evil.example",
        "/app\\..\\admin",
        "/app\r\nSet-Cookie: x=1",
        "/auth/login",
        "/healthz",
        None,
        "",
    ],
)
def test_the_post_sign_in_destination_cannot_leave_the_site(candidate: str | None) -> None:
    assert safe_next_path(candidate, fallback="/app") == "/app"


@pytest.mark.parametrize(
    "candidate",
    [
        "/%2f%2fevil.example",
        "/%2F%2Fevil.example",
        "/app%2f..%2f..%2fadmin",
        "/%5c%5cevil.example",
        "/%5C%5Cevil.example",
    ],
)
def test_an_encoded_separator_is_not_a_destination(candidate: str) -> None:
    """Hardening, not a live defect — and worth saying which.

    `/%2f%2fevil.example` stays same-origin in every browser that resolves it,
    so the reviewer's `next` probes never actually left the site. But it *did*
    survive the filter and reach the rendered page, and a value that survives one
    more decoding step than it was checked against is how a redirect filter is
    eventually escaped. No operator destination in this application contains an
    encoded slash or backslash, so refusing one costs nothing.
    """

    assert safe_next_path(candidate, fallback="/app") == "/app"


def test_a_legitimate_destination_survives() -> None:
    assert safe_next_path("/app/campaigns?state=open", fallback="/app") == (
        "/app/campaigns?state=open"
    )
    # Ordinary percent-encoding in a query value is untouched; only an encoded
    # path *separator* is refused.
    assert safe_next_path("/app/contacts?q=a%20b", fallback="/app") == "/app/contacts?q=a%20b"


@pytest.mark.parametrize(
    "candidate",
    [
        # The real one: `redirect_uri` on the extension authorize page.
        "/extension/authorize?redirect_uri=https%3A%2F%2Fabc.chromiumapp.org%2F",
        # The general shape — an encoded separator anywhere in the query.
        "/app/campaigns?back=%2Fapp%2Fcontacts",
        "/app/imports?path=a%5Cb",
    ],
)
def test_an_encoded_separator_in_the_QUERY_does_not_discard_the_destination(
    candidate: str,
) -> None:
    """#280. The encoded-separator rule used to apply to the whole value.

    That refused a destination this application itself produces. A signed-out
    ``GET /extension/authorize?...`` is sent to ``/auth/login?next=<that URL>``
    by the default-deny middleware, and its ``redirect_uri`` parameter is
    ``https%3A%2F%2F<extension id>.chromiumapp.org%2F`` — percent-encoded
    because a query value must be. The ``%2f`` test then matched, ``next`` was
    dropped, and after signing in the operator landed on the dashboard instead
    of back at the authorization they were completing.

    For ``chrome.identity.launchWebAuthFlow`` that is indistinguishable from a
    refusal: the window never reaches ``https://<id>.chromiumapp.org/``, so the
    flow ends only when the operator closes it, and the panel reported "the
    window was closed, or VMR Outbound declined this install".

    The rule still applies in full to the path — see the test above — which is
    the only place an encoded separator could change which origin the
    destination resolves against.
    """

    assert safe_next_path(candidate, fallback="/app") == candidate


def test_the_extension_authorize_destination_survives_the_whole_sign_in_round_trip(
    staging_client: TestClient,
) -> None:
    """End to end through the middleware, not just the helper.

    An anonymous browser navigation to the authorize page must come back with a
    ``next`` that still carries every authorization parameter, and that value
    must survive ``safe_next_path`` when the sign-in page reads it back.
    """

    extension_id = "a" * 32
    query = urlencode(
        {
            "extension_id": extension_id,
            "installation_id": "3f1c8a2e-0b7d-4c66-9f21-8a0d5e6b7c31",
            "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            "code_challenge_method": "S256",
            "state": "hZ2Xk9QpR7nT4vB1cE6yUu0iOa3sDfGh5jKlZxCvBnM",
            "redirect_uri": f"https://{extension_id}.chromiumapp.org/",
        }
    )
    target = f"/extension/authorize?{query}"

    response = staging_client.get(
        target,
        headers={"accept": "text/html", "sec-fetch-mode": "navigate"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/auth/login?next=")

    # What FastAPI hands the sign-in route as `next` — one decode of the value.
    handed_back = unquote(location.partition("next=")[2])
    assert handed_back == target

    # And what the sign-in route then does with it. Before #280 this was "/app",
    # which is exactly where the sign-in window got stuck.
    assert safe_next_path(handed_back, fallback="/app") == target
    assert "redirect_uri=https%3A%2F%2F" in safe_next_path(handed_back, fallback="/app")


# ---------------------------------------------------------------------------
# E. The sign-in round trip
# ---------------------------------------------------------------------------


def test_an_approved_identity_is_accepted_and_lands_on_the_requested_page(
    staging_client: TestClient, provider: RecordingIdentityProvider
) -> None:
    response, _ = _sign_in(staging_client, provider)
    assert response.status_code == 303
    assert response.headers["location"] == "/app/campaigns"
    assert staging_client.get("/app").status_code == 200
    assert staging_client.get("/admin").status_code == 200


def test_start_sends_pkce_and_a_bound_transaction(
    staging_client: TestClient, provider: RecordingIdentityProvider
) -> None:
    response = staging_client.get("/auth/google/start")
    assert response.status_code == 303
    call = provider.authorization_calls[-1]
    assert call["redirect_uri"] == f"{STAGING_ORIGIN}/auth/callback"
    assert call["code_challenge"] and call["state"] and call["nonce"]
    assert LOGIN_TRANSACTION_COOKIE_NAME in response.cookies


def test_a_verified_google_identity_with_no_account_is_refused(
    staging_client: TestClient, provider: RecordingIdentityProvider
) -> None:
    """The identity is real and fully valid. There is simply no account for it.

    Renamed from ``..._outside_the_allow_list_is_refused``: the refusal is the
    same and is still asserted, but the *authority* changed. Google proving who
    somebody is has never been authorization here, and since #270 the thing that
    grants it is a row in ``users`` rather than a line in an environment file.
    """

    response, _ = _sign_in(staging_client, provider, email="outsider@vmr.example")
    assert response.status_code == 403
    assert "does not have a VMR Outbound account" in response.text
    assert SESSION_COOKIE_NAME not in staging_client.cookies
    # And nothing behind the boundary opened up.
    assert staging_client.get("/app").status_code == 401


def test_a_refusal_page_leaks_no_operator_data(
    staging_client: TestClient, provider: RecordingIdentityProvider
) -> None:
    response, _ = _sign_in(staging_client, provider, email="outsider@vmr.example")
    assert APPROVED_EMAIL not in response.text


def test_a_forged_callback_state_is_rejected(
    staging_client: TestClient, provider: RecordingIdentityProvider
) -> None:
    started = staging_client.get("/auth/google/start")
    assert started.status_code == 303
    provider.claims = operator_claims(nonce=provider.authorization_calls[-1]["nonce"])
    response = staging_client.get("/auth/callback?code=test-code&state=attacker-chosen-state")
    assert response.status_code == 403
    assert SESSION_COOKIE_NAME not in staging_client.cookies


def test_a_callback_without_a_transaction_cookie_is_rejected(
    staging_client: TestClient, provider: RecordingIdentityProvider
) -> None:
    """A callback captured from someone else's browser has no transaction here."""

    staging_client.get("/auth/google/start")
    state = provider.authorization_calls[-1]["state"]
    provider.claims = operator_claims(nonce=provider.authorization_calls[-1]["nonce"])
    staging_client.cookies.delete(LOGIN_TRANSACTION_COOKIE_NAME, path="/auth")
    response = staging_client.get(f"/auth/callback?code=test-code&state={state}")
    assert response.status_code == 403
    assert SESSION_COOKIE_NAME not in staging_client.cookies


def test_a_replayed_callback_is_rejected_the_second_time(
    staging_client: TestClient, provider: RecordingIdentityProvider
) -> None:
    """The transaction is single-use: it is deleted as the session is minted."""

    first, transaction = _sign_in(staging_client, provider)
    assert first.status_code == 303
    replay = staging_client.get(f"/auth/callback?code=test-code&state={transaction['state']}")
    assert replay.status_code == 403


def test_an_expired_sign_in_transaction_is_rejected(
    staging_client: TestClient, provider: RecordingIdentityProvider
) -> None:
    staging_client.get("/auth/google/start")
    state = provider.authorization_calls[-1]["state"]
    provider.claims = operator_claims(nonce=provider.authorization_calls[-1]["nonce"])
    stale = _codec().encode_login_transaction(
        {
            "state": state,
            "nonce": provider.authorization_calls[-1]["nonce"],
            "verifier": "x" * 32,
            "next": "/app",
            "exp": int(time.time()) - 1,
        }
    )
    staging_client.cookies.set(LOGIN_TRANSACTION_COOKIE_NAME, stale, path="/auth")
    response = staging_client.get(f"/auth/callback?code=test-code&state={state}")
    assert response.status_code == 403


def test_a_provider_error_never_mints_a_session(
    staging_client: TestClient, provider: RecordingIdentityProvider
) -> None:
    staging_client.get("/auth/google/start")
    state = provider.authorization_calls[-1]["state"]
    provider.error = IdentityAssertionError("identity assertion signature does not verify")
    response = staging_client.get(f"/auth/callback?code=test-code&state={state}")
    assert response.status_code == 403
    assert SESSION_COOKIE_NAME not in staging_client.cookies


def test_a_cancelled_sign_in_is_handled_without_a_session(
    staging_client: TestClient,
) -> None:
    staging_client.get("/auth/google/start")
    response = staging_client.get("/auth/callback?error=access_denied")
    assert response.status_code == 403
    assert SESSION_COOKIE_NAME not in staging_client.cookies


def test_sign_in_rotates_the_session_identifier(
    staging_client: TestClient, provider: RecordingIdentityProvider
) -> None:
    _sign_in(staging_client, provider)
    first = staging_client.cookies[SESSION_COOKIE_NAME]
    staging_client.post("/auth/logout", data={"_csrf": _read_csrf(staging_client)})
    _sign_in(staging_client, provider)
    assert staging_client.cookies[SESSION_COOKIE_NAME] != first


# ---------------------------------------------------------------------------
# F. Identity assertion rules
# ---------------------------------------------------------------------------


def _valid_claims(**overrides: Any):
    return operator_claims(nonce="expected-nonce", **overrides)


@pytest.mark.parametrize(
    ("overrides", "expected_nonce", "fragment"),
    [
        ({"issuer": "https://accounts.evil.example"}, "expected-nonce", "issuer"),
        ({"audience": "another-client.apps.googleusercontent.com"}, "expected-nonce", "client"),
        ({}, "a-different-nonce", "sign-in request"),
        ({"expires_in": -600}, "expected-nonce", "expired"),
        ({"email_verified": False}, "expected-nonce", "unverified"),
        ({"email": "not-an-email"}, "expected-nonce", "usable email"),
    ],
)
def test_an_unacceptable_assertion_is_refused(
    overrides: dict[str, Any], expected_nonce: str, fragment: str
) -> None:
    with pytest.raises(IdentityAssertionError) as caught:
        validate_identity_claims(
            _valid_claims(**overrides),
            client_id=TEST_CLIENT_ID,
            accepted_issuers=(TEST_ISSUER,),
            expected_nonce=expected_nonce,
            now=int(time.time()),
        )
    assert fragment in str(caught.value)


def test_a_valid_assertion_yields_the_normalised_address() -> None:
    email = validate_identity_claims(
        _valid_claims(email="Operator@VMR.Example"),
        client_id=TEST_CLIENT_ID,
        accepted_issuers=(TEST_ISSUER,),
        expected_nonce="expected-nonce",
        now=int(time.time()),
    )
    assert email == APPROVED_EMAIL


# ---------------------------------------------------------------------------
# G. Signature verification against the published key set
# ---------------------------------------------------------------------------


def _verify(token: str, *, jwks: JwksClient) -> dict[str, Any]:
    """Run the async verifier from a synchronous test.

    `asyncio.run` rather than a plugin: the suite has no async test framework
    and adding one for four tests would be a dependency nobody asked for.
    """

    return asyncio.run(verify_id_token(token, jwks=jwks))


def _jwks_client(*keys: SigningKey) -> JwksClient:
    return JwksClient(
        jwks_uri=TEST_JWKS_URI,
        client=httpx.AsyncClient(transport=jwks_transport(*keys)),
    )


def test_a_genuine_signature_verifies() -> None:
    key = SigningKey(kid="key-1")
    token = id_token(key, claims=google_claims(nonce="n"))
    payload = _verify(token, jwks=_jwks_client(key))
    assert payload["email"] == APPROVED_EMAIL
    assert payload["nonce"] == "n"


def test_a_token_signed_by_the_wrong_key_is_refused() -> None:
    published = SigningKey(kid="key-1")
    attacker = SigningKey(kid="key-1")
    token = id_token(published, claims=google_claims(nonce="n"), signing_key=attacker)
    with pytest.raises(IdentityAssertionError) as caught:
        _verify(token, jwks=_jwks_client(published))
    assert "signature does not verify" in str(caught.value)


def test_a_tampered_payload_is_refused() -> None:
    key = SigningKey(kid="key-1")
    token = id_token(key, claims=google_claims(nonce="n"), tamper_payload=True)
    with pytest.raises(IdentityAssertionError):
        _verify(token, jwks=_jwks_client(key))


@pytest.mark.parametrize("algorithm", ["none", "HS256", "RS512", "ES256", ""])
def test_an_unaccepted_algorithm_is_refused(algorithm: str) -> None:
    """Algorithm confusion: there is exactly one accepted value."""

    key = SigningKey(kid="key-1")
    token = id_token(key, claims=google_claims(nonce="n"), algorithm=algorithm)
    with pytest.raises(IdentityAssertionError) as caught:
        _verify(token, jwks=_jwks_client(key))
    assert "algorithm" in str(caught.value)


def test_a_token_may_not_nominate_its_own_key() -> None:
    key = SigningKey(kid="key-1")
    token = id_token(
        key,
        claims=google_claims(nonce="n"),
        header_extra={"jwk": key.jwk()},
    )
    with pytest.raises(IdentityAssertionError) as caught:
        _verify(token, jwks=_jwks_client(key))
    assert "own signing key" in str(caught.value)


def test_an_unknown_key_identifier_is_refused() -> None:
    published = SigningKey(kid="key-1")
    token = id_token(published, claims=google_claims(nonce="n"), kid="key-unknown")
    with pytest.raises(IdentityAssertionError) as caught:
        _verify(token, jwks=_jwks_client(published))
    assert "unknown signing key" in str(caught.value)


def test_a_token_without_a_key_identifier_is_refused() -> None:
    key = SigningKey(kid="key-1")
    token = id_token(key, claims=google_claims(nonce="n"))
    _, payload, signature = token.split(".")
    stripped = b64url_json({"alg": "RS256", "typ": "JWT"})
    with pytest.raises(IdentityAssertionError) as caught:
        _verify(f"{stripped}.{payload}.{signature}", jwks=_jwks_client(key))
    assert "does not name a signing key" in str(caught.value)


@pytest.mark.parametrize("token", ["", "a.b", "not-a-jwt", "a.b.c.d", "..", "a..c"])
def test_a_structurally_invalid_token_is_refused(token: str) -> None:
    key = SigningKey(kid="key-1")
    with pytest.raises(IdentityAssertionError):
        _verify(token, jwks=_jwks_client(key))


def test_key_rotation_is_picked_up_by_one_refresh() -> None:
    """A genuinely rotated key must still work — the refusal above must not be
    achieved by simply never refreshing."""

    old = SigningKey(kid="key-1")
    new = SigningKey(kid="key-2")
    client = JwksClient(
        jwks_uri=TEST_JWKS_URI, client=httpx.AsyncClient(transport=jwks_transport(old, new))
    )
    token = id_token(new, claims=google_claims(nonce="n"))
    payload = _verify(token, jwks=client)
    assert payload["nonce"] == "n"


# ---------------------------------------------------------------------------
# H. The session cookie
# ---------------------------------------------------------------------------


def test_the_session_cookie_carries_the_required_flags(
    staging_client: TestClient, provider: RecordingIdentityProvider
) -> None:
    response, _ = _sign_in(staging_client, provider)
    raw = next(
        value
        for value in response.headers.get_list("set-cookie")
        if value.startswith(f"{SESSION_COOKIE_NAME}=")
    )
    assert "HttpOnly" in raw
    assert "Secure" in raw
    assert "SameSite=lax" in raw.replace("SameSite=Lax", "SameSite=lax")
    assert "Max-Age=43200" in raw
    assert "Path=/" in raw


def test_no_token_ever_appears_in_a_url(
    staging_client: TestClient, provider: RecordingIdentityProvider
) -> None:
    response, _ = _sign_in(staging_client, provider)
    assert response.headers["location"] == "/app/campaigns"
    body = staging_client.get("/app").text
    assert staging_client.cookies[SESSION_COOKIE_NAME] not in body


def test_an_expired_session_is_refused(
    staging_client: TestClient, approved_account: SeededAccount
) -> None:
    now = int(time.time())
    cookie, _ = _session_cookie(approved_account, issued_at=now - 7200, expires_at=now - 1)
    staging_client.cookies.set(SESSION_COOKIE_NAME, cookie)
    response = staging_client.get("/app")
    assert response.status_code == 401


def test_a_forged_session_signature_is_refused(staging_client: TestClient) -> None:
    forged = SessionCodec("a-completely-different-secret-value-32ch")
    session = OperatorSession(
        email=APPROVED_EMAIL,
        subject="1",
        display_name="Attacker",
        session_id=new_session_id(),
        issued_at=int(time.time()),
        expires_at=int(time.time()) + 3600,
        user_id="00000000-0000-4000-8000-000000000001",
        auth_version=1,
    )
    staging_client.cookies.set(SESSION_COOKIE_NAME, forged.encode_session(session))
    assert staging_client.get("/app").status_code == 401


@pytest.mark.parametrize("value", ["", "garbage", "v1.abc", "v2.abc.def", "v1..", "x" * 5000])
def test_a_malformed_session_cookie_is_refused(staging_client: TestClient, value: str) -> None:
    staging_client.cookies.set(SESSION_COOKIE_NAME, value)
    assert staging_client.get("/app").status_code == 401


def test_disabling_the_account_revokes_a_live_session(
    staging_client: TestClient,
    provider: RecordingIdentityProvider,
    approved_account: SeededAccount,
) -> None:
    """The revocation story, asserted rather than claimed.

    This test replaces ``test_removing_an_operator_from_the_allow_list_revokes_a
    _live_session``, which asserted the previous slice's mechanism: an address
    removed from ``AUTH__ALLOWED_OPERATOR_EMAILS`` stopped verifying on the next
    request *after a restart*, because the allow-list was configuration.

    The mechanism it asserted is gone, deliberately, because it could not do what
    #270 needs: it required editing a file and restarting the service, and it
    could not express "this person's password was reset" at all. What replaces it
    is strictly stronger — one ``UPDATE`` on one row, no restart, effective on the
    very next request — and this test asserts exactly that, on the same session
    cookie, with nothing rebuilt in between.
    """

    _sign_in(staging_client, provider)
    assert staging_client.get("/app").status_code == 200

    with SessionLocal() as session:
        user = session.get(User, uuid.UUID(approved_account.user_id))
        assert user is not None
        user.state = UserState.DISABLED
        user.auth_version += 1
        session.commit()

    assert staging_client.get("/app").status_code == 401


def test_reactivating_an_account_does_not_resurrect_its_old_sessions(
    staging_client: TestClient,
    provider: RecordingIdentityProvider,
    approved_account: SeededAccount,
) -> None:
    """Reactivation means "may sign in again", never "the open tab is live again"."""

    _sign_in(staging_client, provider)
    assert staging_client.get("/app").status_code == 200

    with SessionLocal() as session:
        user = session.get(User, uuid.UUID(approved_account.user_id))
        assert user is not None
        user_service.set_state(
            session, user=user, state=UserState.DISABLED, actor="admin@vmr.example"
        )
        session.commit()
    assert staging_client.get("/app").status_code == 401

    with SessionLocal() as session:
        user = session.get(User, uuid.UUID(approved_account.user_id))
        assert user is not None
        user_service.set_state(
            session, user=user, state=UserState.ACTIVE, actor="admin@vmr.example"
        )
        session.commit()

    # Still refused: the counter moved twice, and the cookie was minted before
    # either move.
    assert staging_client.get("/app").status_code == 401


def test_logout_invalidates_the_authenticated_state(
    staging_client: TestClient, provider: RecordingIdentityProvider
) -> None:
    _sign_in(staging_client, provider)
    assert staging_client.get("/app").status_code == 200
    response = staging_client.post("/auth/logout", data={"_csrf": _read_csrf(staging_client)})
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/signed-out"
    assert staging_client.get("/app").status_code == 401


def test_logout_without_a_token_is_refused_while_signed_in(
    staging_client: TestClient, provider: RecordingIdentityProvider
) -> None:
    _sign_in(staging_client, provider)
    assert staging_client.post("/auth/logout").status_code == 403
    assert staging_client.get("/app").status_code == 200


def test_logout_without_a_session_still_lands_on_the_signed_out_page(
    staging_client: TestClient,
) -> None:
    response = staging_client.post("/auth/logout")
    assert response.status_code == 303


def test_a_session_stamped_in_the_future_is_refused() -> None:
    codec = _codec()
    now = int(time.time())
    session = OperatorSession(
        email=APPROVED_EMAIL,
        subject="1",
        display_name="X",
        session_id=new_session_id(),
        issued_at=now + 600,
        expires_at=now + 4000,
        user_id="00000000-0000-4000-8000-000000000001",
        auth_version=1,
    )
    with pytest.raises(SessionDecodeError):
        codec.decode_session(codec.encode_session(session), now=now)


# ---------------------------------------------------------------------------
# I. CSRF
# ---------------------------------------------------------------------------


def _read_csrf(client: TestClient) -> str:
    """Pull the token out of a rendered page, the way a browser would."""

    body = client.get("/app/campaigns/new").text
    marker = 'name="_csrf" value="'
    start = body.index(marker) + len(marker)
    return body[start : body.index('"', start)]


def test_every_post_form_is_rendered_with_a_token(
    staging_client: TestClient, provider: RecordingIdentityProvider
) -> None:
    """The Jinja extension, observed end to end rather than in isolation."""

    _sign_in(staging_client, provider)
    body = staging_client.get("/app/campaigns/new").text
    assert body.count('name="_csrf"') >= 1
    assert body.count("<form") >= 1


def test_a_valid_token_is_accepted(
    staging_client: TestClient, provider: RecordingIdentityProvider
) -> None:
    _sign_in(staging_client, provider)
    response = staging_client.post(
        "/api/campaigns",
        json={"name": "Authorised campaign"},
        headers={"X-CSRF-Token": _read_csrf(staging_client)},
    )
    assert response.status_code == 201


def test_a_missing_token_is_refused(
    staging_client: TestClient, provider: RecordingIdentityProvider
) -> None:
    _sign_in(staging_client, provider)
    response = staging_client.post("/api/campaigns", json={"name": "No token"})
    assert response.status_code == 403
    assert response.json()["error"] == "csrf_failed"


def test_a_wrong_token_is_refused(
    staging_client: TestClient, provider: RecordingIdentityProvider
) -> None:
    _sign_in(staging_client, provider)
    response = staging_client.post(
        "/api/campaigns",
        json={"name": "Wrong token"},
        headers={"X-CSRF-Token": "not-the-right-token"},
    )
    assert response.status_code == 403


def test_another_sessions_token_is_refused(
    staging_client: TestClient,
    provider: RecordingIdentityProvider,
    approved_account: SeededAccount,
) -> None:
    """Tokens are bound to one session, not shared across all of them."""

    _sign_in(staging_client, provider)
    _, foreign_token = _session_cookie(approved_account, session_id="a-different-session")
    response = staging_client.post(
        "/api/campaigns",
        json={"name": "Foreign token"},
        headers={"X-CSRF-Token": foreign_token},
    )
    assert response.status_code == 403


def test_a_form_post_carries_its_token_in_the_body(
    staging_client: TestClient, provider: RecordingIdentityProvider
) -> None:
    _sign_in(staging_client, provider)
    token = _read_csrf(staging_client)
    accepted = staging_client.post(
        "/app/campaigns/new", data={"name": "Form campaign", "_csrf": token}
    )
    assert accepted.status_code == 303
    refused = staging_client.post("/app/campaigns/new", data={"name": "No token"})
    assert refused.status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": "https://evil.example"},
        {"origin": "null"},
        {"sec-fetch-site": "cross-site"},
        {"sec-fetch-site": "same-site"},
        {"origin": "http://srv1885453.hstgr.cloud"},
    ],
)
def test_a_cross_site_write_cannot_bypass_csrf_even_with_a_valid_token(
    staging_client: TestClient,
    provider: RecordingIdentityProvider,
    headers: dict[str, str],
) -> None:
    """The origin backstop refuses before the token is even consulted."""

    _sign_in(staging_client, provider)
    token = _read_csrf(staging_client)
    response = staging_client.post(
        "/api/campaigns",
        json={"name": "Cross site"},
        headers={"X-CSRF-Token": token, **headers},
    )
    assert response.status_code == 403
    assert response.json()["error"] == "cross_site_request_refused"


def test_a_same_origin_write_is_allowed(
    staging_client: TestClient, provider: RecordingIdentityProvider
) -> None:
    _sign_in(staging_client, provider)
    response = staging_client.post(
        "/api/campaigns",
        json={"name": "Same origin"},
        headers={
            "X-CSRF-Token": _read_csrf(staging_client),
            "origin": STAGING_ORIGIN,
            "sec-fetch-site": "same-origin",
        },
    )
    assert response.status_code == 201


# ---------------------------------------------------------------------------
# I2. The shape a real browser actually sends (#264)
# ---------------------------------------------------------------------------

# `Origin: <site>` above is the tidy shape, and it is not the shape this
# deployment receives. The hardening boundary sends `Referrer-Policy:
# no-referrer`, and under that policy the Fetch standard serialises `Origin` as
# `null` on every non-GET/HEAD, non-CORS request — a genuine same-origin form
# post included. Layer 1 read that `null` as an opaque origin and refused every
# write the hosted UI made, which is what operators hit on the sign-out button.
#
# The tests in this section are written with the browser's shape rather than the
# tidy one, because a suite that only ever sends the tidy one cannot see this
# class of defect: every cross-site test in the section above passed throughout.
REAL_BROWSER_FORM_POST = {
    "origin": "null",
    "sec-fetch-site": "same-origin",
    "sec-fetch-mode": "navigate",
    "sec-fetch-dest": "document",
    "sec-fetch-user": "?1",
}


def test_the_response_really_carries_the_policy_that_opaques_the_origin(
    staging_client: TestClient, provider: RecordingIdentityProvider
) -> None:
    """The premise of `REAL_BROWSER_FORM_POST`, asserted instead of assumed.

    `Origin: null` is not an arbitrary hostile value in these tests; it is the
    consequence of a header this application chooses to send. If that policy is
    ever relaxed the browser resumes sending a real origin, and the shape below
    stops describing reality — so it must be re-derived from the new policy
    rather than left quietly passing.
    """

    _sign_in(staging_client, provider)
    assert staging_client.get("/app").headers["referrer-policy"] == "no-referrer"


def test_clicking_sign_out_in_a_real_browser_succeeds(
    staging_client: TestClient, provider: RecordingIdentityProvider
) -> None:
    """#264 end to end: the live sign-out click, in the shape the browser sends."""

    _sign_in(staging_client, provider)
    assert staging_client.get("/app").status_code == 200

    response = staging_client.post(
        "/auth/logout",
        data={"_csrf": _read_csrf(staging_client)},
        headers=REAL_BROWSER_FORM_POST,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/auth/signed-out"
    cleared = [
        value
        for value in response.headers.get_list("set-cookie")
        if value.startswith(f"{SESSION_COOKIE_NAME}=") and "Max-Age=0" in value
    ]
    assert cleared, response.headers.get_list("set-cookie")
    assert SESSION_COOKIE_NAME not in staging_client.cookies
    assert staging_client.get("/app").status_code == 401


def test_an_ordinary_write_survives_the_opaque_origin_too(
    staging_client: TestClient, provider: RecordingIdentityProvider
) -> None:
    """Sign-out was the click that surfaced #264, not the extent of it.

    The same opaque origin accompanies every write the hosted UI makes, so a
    repair that rescued only `/auth/logout` would have left the rest of the
    application refusing its own forms.
    """

    _sign_in(staging_client, provider)
    response = staging_client.post(
        "/api/campaigns",
        json={"name": "Opaque origin"},
        headers={"X-CSRF-Token": _read_csrf(staging_client), **REAL_BROWSER_FORM_POST},
    )
    assert response.status_code == 201


@pytest.mark.parametrize(
    "fetch_site",
    [
        pytest.param(None, id="no-fetch-metadata"),
        pytest.param("none", id="typed-url-or-bookmark"),
        pytest.param("same-site", id="sibling-host"),
        pytest.param("cross-site", id="another-site"),
    ],
)
def test_an_opaque_origin_without_a_positive_same_origin_signal_is_still_refused(
    staging_client: TestClient,
    provider: RecordingIdentityProvider,
    fetch_site: str | None,
) -> None:
    """Only a positive `same-origin` clears an opaque origin. Nothing weaker does.

    This is the boundary the repair must not move. `Sec-Fetch-Site` is a
    forbidden header name, so no page script may set, clear or alter it and a
    real cross-site post cannot arrive wearing `same-origin`; these four cases
    are every other shape an opaque origin can reach the boundary in, and each
    is still refused before the token is consulted. A non-browser client that
    forges both headers remains layer 2's problem, which fails closed on its own.
    """

    _sign_in(staging_client, provider)
    headers = {"X-CSRF-Token": _read_csrf(staging_client), "origin": "null"}
    if fetch_site is not None:
        headers["sec-fetch-site"] = fetch_site

    response = staging_client.post("/api/campaigns", json={"name": "Opaque"}, headers=headers)

    assert response.status_code == 403
    assert response.json()["error"] == "cross_site_request_refused"


def test_a_hostile_origin_is_still_refused_alongside_same_origin_metadata(
    staging_client: TestClient, provider: RecordingIdentityProvider
) -> None:
    """The named origin is still read literally; only the opaque one is not.

    Relaxing `null` must not have relaxed `https://evil.example`, which arrives
    with exactly the same fetch metadata a genuine click carries.
    """

    _sign_in(staging_client, provider)
    response = staging_client.post(
        "/api/campaigns",
        json={"name": "Hostile"},
        headers={
            "X-CSRF-Token": _read_csrf(staging_client),
            **REAL_BROWSER_FORM_POST,
            "origin": "https://evil.example",
        },
    )
    assert response.status_code == 403
    assert response.json()["error"] == "cross_site_request_refused"


@pytest.mark.parametrize("method", ["get", "head", "options"])
def test_safe_methods_never_require_a_token(
    staging_client: TestClient, provider: RecordingIdentityProvider, method: str
) -> None:
    _sign_in(staging_client, provider)
    response = getattr(staging_client, method)("/app")
    assert response.status_code != 403


# ---------------------------------------------------------------------------
# J. Production hardening must survive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/app", "/api/campaigns", "/auth/login", "/healthz"])
def test_security_headers_are_present_on_every_auth_outcome(
    staging_client: TestClient, path: str
) -> None:
    response = staging_client.get(path)
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-request-id"]
    assert "content-security-policy" in response.headers


def test_a_forged_host_is_rejected_before_authentication(
    staging_client: TestClient,
) -> None:
    """The trusted-host guard still runs outside the auth boundary."""

    response = staging_client.get("/app", headers={"host": "evil.example.com"})
    assert response.status_code == 400
    assert response.headers["x-request-id"]


def test_hsts_is_emitted_for_the_hosted_origin(staging_client: TestClient) -> None:
    response = staging_client.get("/healthz")
    assert response.headers["strict-transport-security"] == "max-age=31536000"


def test_the_refusal_response_varies_on_the_inputs_it_depends_on(
    staging_client: TestClient,
) -> None:
    response = staging_client.get("/app")
    assert "Cookie" in response.headers["vary"]


# ---------------------------------------------------------------------------
# K. The extension boundary stays separate
# ---------------------------------------------------------------------------


def test_a_chrome_extension_origin_gets_no_session_access(
    staging_client: TestClient,
) -> None:
    """The extension's credential is a bearer token, never this cookie.

    This deployment configures no ``EXTENSION_AUTH__*`` at all, so an
    extension-origin request is refused exactly like any other anonymous caller.
    The boundary that *does* admit one, and everything it refuses, lives in
    ``tests/test_extension_capture_auth.py``.
    """

    response = staging_client.post(
        "/api/intake/contact-captures",
        json={},
        headers={"origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop"},
    )
    assert response.status_code == 401


def test_no_bearer_credential_is_accepted_without_one_being_configured(
    staging_client: TestClient,
) -> None:
    """Extension capture authentication defaults to off, and off means off.

    A deployment that has not deliberately enabled and configured it accepts no
    bearer credential on any path — including the enumerated capture contract,
    which is otherwise the only place one is ever read.
    """

    for path in ("/api/campaigns", "/api/contact-labels", "/api/contacts/lookup"):
        response = staging_client.get(
            path,
            headers={
                "authorization": "Bearer vmrx1.beta-laptop.anything-at-all-long-enough-to-parse",
                "origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
            },
        )
        assert response.status_code == 401, path


# ---------------------------------------------------------------------------
# L. Email normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Operator@VMR.Example  ", "operator@vmr.example"),
        ("operator@vmr.example", "operator@vmr.example"),
        ("op erator@vmr.example", ""),
        ("operator", ""),
        ("@vmr.example", ""),
        ("operator@", ""),
        ("a@b@c", ""),
        ("", ""),
    ],
)
def test_operator_email_normalisation(raw: str, expected: str) -> None:
    assert normalize_operator_email(raw) == expected


def test_dot_and_plus_forms_are_not_folded_into_an_approval() -> None:
    """Folding Gmail's delivery conveniences here would widen the allow-list."""

    settings = AuthSettings(
        enabled=True,
        session_secret=SESSION_SECRET,
        allowed_operator_emails=("operator@vmr.example",),
    )
    assert settings.is_approved("operator@vmr.example") is True
    assert settings.is_approved("oper.ator@vmr.example") is False
    assert settings.is_approved("operator+staging@vmr.example") is False


def test_an_empty_allow_list_approves_nobody() -> None:
    settings = AuthSettings(enabled=True, session_secret=SESSION_SECRET)
    assert settings.is_approved(APPROVED_EMAIL) is False


def test_an_optional_workspace_domain_is_an_extra_gate_not_a_replacement() -> None:
    settings = AuthSettings(
        enabled=True,
        session_secret=SESSION_SECRET,
        allowed_operator_emails=("operator@vmr.example", "contractor@other.example"),
        allowed_google_domain="vmr.example",
    )
    assert settings.is_approved("operator@vmr.example") is True
    assert settings.is_approved("contractor@other.example") is False


def test_a_malformed_allow_list_entry_refuses_at_load_time() -> None:
    with pytest.raises(ValueError):
        AuthSettings(allowed_operator_emails=("not an email",))


# ---------------------------------------------------------------------------
# L-6 — normalisation must never widen authorisation
# ---------------------------------------------------------------------------
#
# This function used to NFKC-normalise. NFKC is a *widening* transform: it folds
# compatibility characters onto their ASCII counterparts, so a fullwidth spelling
# of an approved address normalised onto that address and was approved. The
# review reproduced it. The contract now is ASCII-in-or-nothing, applied to both
# sides of the comparison.


def _allow_listed() -> AuthSettings:
    return AuthSettings(
        enabled=True,
        session_secret=SESSION_SECRET,
        allowed_operator_emails=("operator@vmr.example",),
    )


# Written as escapes on purpose. A lookalike pasted as a literal is exactly the
# thing an editor, a diff viewer or a copy-paste silently flattens back to ASCII,
# and a flattened literal turns this into a test asserting that the *approved*
# address is refused - which is wrong, and wrong in a direction that still looks
# plausible. The reviewer's own suite lost two cases that way.
@pytest.mark.parametrize(
    ("presented", "why"),
    [
        ("\uff4fperator@vmr.example", "fullwidth o in the local part"),
        ("\uff2f\uff30\uff25\uff32\uff21\uff34\uff2f\uff32@vmr.example", "fullwidth local part"),
        ("operator@\uff36\uff2d\uff32.example", "fullwidth domain"),
        ("operator@vmr.exampl\u0435", "cyrillic e"),
        ("\u043eperator@vmr.example", "cyrillic o"),
        ("operator@vmr.example\u200b", "zero-width space"),
        ("operator\u00a0@vmr.example", "non-breaking space"),
        ("operator@vmr.example\ufeff", "byte-order mark"),
    ],
)
def test_a_non_ascii_lookalike_is_never_folded_into_an_approval(presented: str, why: str) -> None:
    settings = _allow_listed()
    assert normalize_operator_email(presented) == "", why
    assert settings.is_approved(presented) is False, why


@pytest.mark.parametrize(
    "presented",
    [
        "operator@vmr.example",
        "OPERATOR@VMR.EXAMPLE",
        "Operator@Vmr.Example",
        "  operator@vmr.example  ",
        "\toperator@vmr.example\n",
    ],
)
def test_ascii_case_and_surrounding_whitespace_still_match(presented: str) -> None:
    """Anti-vacuity, and the documented behaviour the ASCII rule must preserve.

    Google issues lower-cased ASCII addresses; treating `A@x` and `a@x` as
    different would produce an allow-list that silently fails to match. Interior
    whitespace remains unusable — it is stripped only from the ends.
    """

    assert _allow_listed().is_approved(presented) is True


def test_a_non_ascii_allow_list_entry_refuses_at_load_time() -> None:
    """The other side of the comparison, refused where an operator will see it.

    A lookalike address pasted into `/etc/vmr/vmr.env` from a document can never
    be matched by a Google identity, so the process refuses to start rather than
    booting with an entry that looks configured and approves nobody.
    """

    for entry in ("ｏperator@vmr.example", "operator@vmr.examplе"):
        with pytest.raises(ValueError):
            AuthSettings(enabled=True, allowed_operator_emails=(entry,))


def test_a_non_ascii_workspace_domain_refuses_at_load_time() -> None:
    """The optional domain gate folds the same way and is closed the same way."""

    with pytest.raises(ValueError):
        AuthSettings(enabled=True, allowed_google_domain="ＶＭＲ.example")


def test_secrets_never_reach_a_dump() -> None:
    settings = AuthSettings(
        enabled=True,
        session_secret="TOP-SECRET-SESSION-VALUE-LONG-ENOUGH-XX",
        google_client_secret="TOP-SECRET-CLIENT",
        google_client_id=TEST_CLIENT_ID,
    )
    assert "TOP-SECRET" not in repr(settings)
    assert "TOP-SECRET" not in str(settings.model_dump())

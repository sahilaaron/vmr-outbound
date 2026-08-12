"""The extension capture credential boundary, written from the attacker's side.

Every refusal named in the acceptance criteria has a test here, and each one is
paired with the positive case that proves the refusal is not vacuous: a real
capture, over the real hosted middleware stack, against the real database,
returning a real 201.

The hosted application is built exactly the way ``tests/test_hosted_auth.py``
builds it — staging environment, real authentication middleware, an always-ready
probe, and a staging database URL that satisfies the production-like runtime
rules but is never connected because ``get_db`` is overridden with the suite's
own transactional session.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import logging
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from app.api.deps import get_db
from app.core.auth.extension import (
    EXTENSION_CAPTURE_CONTRACT,
    ExtensionAuthSettings,
    credential_digest,
    parse_presented_credential,
)
from app.core.auth.session import SESSION_COOKIE_NAME, OperatorSession, SessionCodec, new_session_id
from app.core.auth.startup import HostedAuthConfigurationError
from app.core.config import get_settings
from app.core.runtime import RuntimeConfigurationError
from app.main import create_app
from app.models.contact_capture import ContactCaptureSubmission
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.hosted_auth_factory import seed_account

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = REPO_ROOT / "extensions" / "salesnav-capture" / "docs"
PROFILE_SUBMISSION = json.loads(
    (CONTRACT_DIR / "fixtures" / "contact-capture.profile.example.json").read_text("utf-8")
)

STAGING_HOST = "srv1885453.hstgr.cloud"
STAGING_ORIGIN = f"https://{STAGING_HOST}"
APPROVED_EMAIL = "operator@vmr.example"
SESSION_SECRET = "test-session-secret-value-at-least-32-chars"
STAGING_DATABASE_URL = "postgresql+psycopg://vmr:secret@db.internal.example:5432/vmr_staging"

# A real-shaped Chrome extension id: exactly 32 characters from `a`-`p`.
EXTENSION_ID = "abcdefghijklmnopabcdefghijklmnop"
EXTENSION_ORIGIN = f"chrome-extension://{EXTENSION_ID}"
OTHER_EXTENSION_ORIGIN = "chrome-extension://ponmlkjihgfedcbaponmlkjihgfedcba"
HOSTILE_ORIGIN = "https://evil.example"

KEY_ID = "beta-laptop"
SECRET = "3fVQx8Zk2nLp7Rw6TyUiOaSdFgHjKlZxCvBnM4qWeRt"
CREDENTIAL = f"vmrx1.{KEY_ID}.{SECRET}"
BEARER = {"Authorization": f"Bearer {CREDENTIAL}"}

REVOKED_KEY_ID = "beta-old-laptop"
REVOKED_SECRET = "9pLmNbVcXzAsDfGhJkQwErTyUiOp1234567890abcdE"
REVOKED_CREDENTIAL = f"vmrx1.{REVOKED_KEY_ID}.{REVOKED_SECRET}"


def _load_mint_script() -> Any:
    """The operator's minting script, loaded the way `tests/test_dev_tooling.py` does.

    `scripts/` is a directory of entry points rather than a package, so it is
    loaded by spec instead of imported.
    """

    spec = importlib.util.spec_from_file_location(
        "mint_extension_credential", REPO_ROOT / "scripts" / "mint_extension_credential.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CAPTURE_URL = "/api/intake/contact-captures"
LABELS_URL = "/api/contact-labels"
LOOKUP_URL = "/api/contacts/lookup?linkedin_profile_url=https%3A%2F%2Fwww.linkedin.com%2Fin%2Fx"
CAMPAIGNS_URL = "/api/campaigns"


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
        "FEATURES__CONTACT_CAPTURE_INTAKE": "true",
        "AUTH__ENABLED": "true",
        "AUTH__SESSION_SECRET": SESSION_SECRET,
        "AUTH__ALLOWED_OPERATOR_EMAILS": f'["{APPROVED_EMAIL}"]',
        "AUTH__GOOGLE_CLIENT_ID": "vmr-test-client.apps.googleusercontent.com",
        "AUTH__GOOGLE_CLIENT_SECRET": "test-client-secret",
        "AUTH__PUBLIC_BASE_URL": STAGING_ORIGIN,
        "EXTENSION_AUTH__ENABLED": "true",
        "EXTENSION_AUTH__CREDENTIALS": json.dumps([f"{KEY_ID}:{credential_digest(SECRET)}"]),
        "EXTENSION_AUTH__ALLOWED_ORIGINS": json.dumps([EXTENSION_ORIGIN]),
    }
    env.update(overrides)
    return env


def _apply(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def _build(monkeypatch: pytest.MonkeyPatch, env: dict[str, str], db: Session) -> TestClient:
    _apply(monkeypatch, env)
    app = create_app(readiness_probe=_AlwaysReadyProbe())

    def _override() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_db] = _override
    return TestClient(app, base_url=STAGING_ORIGIN, follow_redirects=False)


@pytest.fixture()
def hosted(monkeypatch: pytest.MonkeyPatch, db_session: Session) -> Iterator[TestClient]:
    client = _build(monkeypatch, _base_env(), db_session)
    try:
        yield client
    finally:
        get_settings.cache_clear()


def _fresh(base: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(base)
    payload["client_submission_id"] = str(uuid.uuid4())
    for capture in payload["contacts"]:
        capture["client_capture_id"] = str(uuid.uuid4())
    return payload


def _session_cookie(*, email: str = APPROVED_EMAIL) -> tuple[str, str]:
    """A signed operator cookie for the account seeded by ``approved_account``.

    Since #270 a cookie names the account it belongs to, so this helper reads the
    seeded row rather than inventing an identifier: a cookie whose ``uid``
    resolves to nothing is refused, and these tests need the operator half of the
    extension-versus-operator comparison to actually be signed in.
    """

    account = seed_account(email=email)
    now = int(time.time())
    session_id = new_session_id()
    session = OperatorSession(
        email=email,
        subject=f"google-sub-{email}",
        display_name="VMR Operator",
        session_id=session_id,
        issued_at=now,
        expires_at=now + 3600,
        user_id=account.user_id,
        auth_version=account.auth_version,
    )
    codec = SessionCodec(SESSION_SECRET)
    return codec.encode_session(session), codec.csrf_token(session_id)


def _capture(
    client: TestClient,
    *,
    headers: dict[str, str] | None = None,
    origin: str | None = EXTENSION_ORIGIN,
):
    sent = dict(headers or {})
    if origin is not None:
        sent["Origin"] = origin
    return client.post(CAPTURE_URL, json=_fresh(PROFILE_SUBMISSION), headers=sent)


def _submission_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(ContactCaptureSubmission)) or 0


# ---------------------------------------------------------------------------
# A. The credential itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "presented",
    [
        None,
        "",
        "Bearer",
        "Basic vmrx1.beta-laptop.secret",
        f"Token {CREDENTIAL}",
        "Bearer vmrx1.beta-laptop",
        "Bearer vmrx1.beta-laptop.short",
        f"Bearer vmrx0.{KEY_ID}.{SECRET}",
        f"Bearer vmrx1.{KEY_ID}.{SECRET}.extra",
        f"Bearer vmrx1.UPPERCASE.{SECRET}",
        f"Bearer vmrx1.has space.{SECRET}",
        "Bearer " + "x" * 5000,
        f"Bearer vmrx1.{KEY_ID}.{SECRET}é",
    ],
)
def test_a_malformed_credential_is_a_refusal_and_never_an_exception(presented: str | None) -> None:
    """Attacker-controlled text must produce a decision, not a 500."""

    settings = ExtensionAuthSettings(
        enabled=True,
        credentials=(f"{KEY_ID}:{credential_digest(SECRET)}",),
        allowed_origins=(EXTENSION_ORIGIN,),
    )
    assert settings.authenticate(presented) is None


def test_a_valid_credential_names_its_key_id() -> None:
    settings = ExtensionAuthSettings(
        enabled=True,
        credentials=(f"{KEY_ID}:{credential_digest(SECRET)}",),
        allowed_origins=(EXTENSION_ORIGIN,),
    )
    assert settings.authenticate(f"Bearer {CREDENTIAL}") == KEY_ID
    assert settings.authenticate(f"bearer {CREDENTIAL}") == KEY_ID


def test_a_right_secret_under_the_wrong_key_id_is_refused() -> None:
    """The key id is part of what is verified, not just an index."""

    settings = ExtensionAuthSettings(
        enabled=True,
        credentials=(f"{KEY_ID}:{credential_digest(SECRET)}",),
        allowed_origins=(EXTENSION_ORIGIN,),
    )
    assert settings.authenticate(f"Bearer vmrx1.other-laptop.{SECRET}") is None


def test_the_disabled_switch_refuses_an_otherwise_valid_credential() -> None:
    settings = ExtensionAuthSettings(
        enabled=False,
        credentials=(f"{KEY_ID}:{credential_digest(SECRET)}",),
        allowed_origins=(EXTENSION_ORIGIN,),
    )
    assert settings.authenticate(f"Bearer {CREDENTIAL}") is None


def test_revocation_beats_a_credential_entry_that_outlived_it() -> None:
    """A revoked key id stays dead even when still listed as a credential."""

    settings = ExtensionAuthSettings(
        enabled=True,
        credentials=(
            f"{KEY_ID}:{credential_digest(SECRET)}",
            f"{REVOKED_KEY_ID}:{credential_digest(REVOKED_SECRET)}",
        ),
        revoked_key_ids=(REVOKED_KEY_ID,),
        allowed_origins=(EXTENSION_ORIGIN,),
    )
    assert settings.authenticate(f"Bearer {REVOKED_CREDENTIAL}") is None
    assert settings.authenticate(f"Bearer {CREDENTIAL}") == KEY_ID


def test_removing_the_entry_also_revokes() -> None:
    settings = ExtensionAuthSettings(
        enabled=True,
        credentials=(f"{KEY_ID}:{credential_digest(SECRET)}",),
        allowed_origins=(EXTENSION_ORIGIN,),
    )
    assert settings.authenticate(f"Bearer {REVOKED_CREDENTIAL}") is None


def test_the_mint_script_produces_a_credential_this_boundary_accepts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The operator's one-time step, proved end to end.

    The script prints two lines that go to two different places. If they ever
    stopped matching — a changed scheme, a different digest, a key id the
    settings model refuses — the failure would land on the operator during live
    setup with nothing to distinguish it from a mistyped paste.
    """

    assert _load_mint_script().main(["--key-id", "beta-round-trip"]) == 0
    printed = capsys.readouterr().out.splitlines()
    credential = next(line.strip() for line in printed if line.strip().startswith("vmrx1."))
    entry = next(line.strip() for line in printed if line.strip().startswith("beta-round-trip:"))

    settings = ExtensionAuthSettings(
        enabled=True, credentials=(entry,), allowed_origins=(EXTENSION_ORIGIN,)
    )
    assert settings.authenticate(f"Bearer {credential}") == "beta-round-trip"
    # And the digest is not the credential: pasting the wrong line fails.
    assert settings.authenticate(f"Bearer {entry}") is None
    # Two mints never collide.
    assert _load_mint_script().main(["--key-id", "beta-round-trip"]) == 0
    second = capsys.readouterr().out
    assert credential not in second


def test_the_mint_script_refuses_an_unusable_key_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _load_mint_script().main(["--key-id", "Has Space"]) == 2
    assert "refusing to mint" in capsys.readouterr().err


@pytest.mark.parametrize(
    "entry",
    [
        "no-colon-here",
        f"{KEY_ID}:not-a-digest",
        f"{KEY_ID}:{'a' * 63}",
        f"UPPER:{credential_digest(SECRET)}".replace("UPPER", "Has Space"),
        f":{credential_digest(SECRET)}",
    ],
)
def test_an_unusable_credential_entry_refuses_to_load(entry: str) -> None:
    """A malformed entry must not be silently dropped into a healthy-looking boot."""

    with pytest.raises(ValueError):
        ExtensionAuthSettings(enabled=True, credentials=(entry,))


def test_one_key_id_with_two_different_digests_refuses_to_load() -> None:
    with pytest.raises(ValueError):
        ExtensionAuthSettings(
            credentials=(
                f"{KEY_ID}:{credential_digest(SECRET)}",
                f"{KEY_ID}:{credential_digest(REVOKED_SECRET)}",
            )
        )


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "chrome-extension://short",
        "chrome-extension://" + "z" * 32,
        "chrome-extension://" + "a" * 33,
        "chrome-extension://ABCDEFGHIJKLMNOPABCDEFGHIJKLMNOP",
        "chrome-extension://аbcdefghijklmnopabcdefghijklmnop",
    ],
)
def test_an_unusable_allowed_origin_refuses_to_load(origin: str) -> None:
    """A typo or a lookalike must fail at startup, not silently match nothing."""

    with pytest.raises(ValueError):
        ExtensionAuthSettings(enabled=True, allowed_origins=(origin,))


def test_the_origin_allow_list_is_exact_and_not_a_scheme_rule() -> None:
    settings = ExtensionAuthSettings(enabled=True, allowed_origins=(EXTENSION_ORIGIN,))
    assert settings.is_allowed_origin(EXTENSION_ORIGIN)
    assert settings.is_allowed_origin(f"{EXTENSION_ORIGIN}/")
    assert not settings.is_allowed_origin(OTHER_EXTENSION_ORIGIN)
    assert not settings.is_allowed_origin(HOSTILE_ORIGIN)
    assert not settings.is_allowed_origin(None)
    assert not settings.is_allowed_origin("chrome-extension://abcdefghijklmnopé")


def test_parsing_never_returns_a_secret_for_a_shape_it_did_not_mint() -> None:
    assert parse_presented_credential(f"Bearer {CREDENTIAL}") == (KEY_ID, SECRET)
    assert parse_presented_credential("Bearer not.a.credential") is None


# ---------------------------------------------------------------------------
# B. The hosted boundary: what reaches the capture route
# ---------------------------------------------------------------------------


def test_anonymous_hosted_capture_is_refused(hosted: TestClient, db_session: Session) -> None:
    before = _submission_count(db_session)
    response = _capture(hosted)
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"
    assert _submission_count(db_session) == before


def test_a_missing_credential_with_an_approved_origin_is_refused(hosted: TestClient) -> None:
    assert _capture(hosted, headers={}).status_code == 401


def test_an_invalid_credential_is_refused(hosted: TestClient) -> None:
    wrong = {"Authorization": f"Bearer vmrx1.{KEY_ID}.{'q' * len(SECRET)}"}
    assert _capture(hosted, headers=wrong).status_code == 401


def test_a_revoked_credential_is_refused(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    """Revocation is proved end to end: the same credential works, then does not."""

    issued = json.dumps(
        [
            f"{KEY_ID}:{credential_digest(SECRET)}",
            f"{REVOKED_KEY_ID}:{credential_digest(REVOKED_SECRET)}",
        ]
    )
    revoked_bearer = {"Authorization": f"Bearer {REVOKED_CREDENTIAL}"}

    live = _build(monkeypatch, _base_env(EXTENSION_AUTH__CREDENTIALS=issued), db_session)
    assert _capture(live, headers=revoked_bearer).status_code == 201

    dead = _build(
        monkeypatch,
        _base_env(
            EXTENSION_AUTH__CREDENTIALS=issued,
            EXTENSION_AUTH__REVOKED_KEY_IDS=json.dumps([REVOKED_KEY_ID]),
        ),
        db_session,
    )
    assert _capture(dead, headers=revoked_bearer).status_code == 401
    # The credential that was not revoked is untouched.
    assert _capture(dead, headers=BEARER).status_code == 201
    get_settings.cache_clear()


def test_a_valid_credential_from_the_approved_origin_captures(
    hosted: TestClient, db_session: Session
) -> None:
    """The positive case, all the way to persisted evidence."""

    before = _submission_count(db_session)
    response = _capture(hosted, headers=BEARER)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["counts"]["submitted"] == 1
    assert response.headers["access-control-allow-origin"] == EXTENSION_ORIGIN
    assert "access-control-allow-credentials" not in response.headers
    assert _submission_count(db_session) == before + 1


@pytest.mark.parametrize("origin", [HOSTILE_ORIGIN, OTHER_EXTENSION_ORIGIN, STAGING_ORIGIN])
def test_a_valid_credential_from_an_unapproved_origin_is_refused(
    hosted: TestClient, db_session: Session, origin: str
) -> None:
    """A stolen credential replayed from anywhere else is worth nothing."""

    before = _submission_count(db_session)
    response = _capture(hosted, headers=BEARER, origin=origin)
    assert response.status_code == 401
    assert "access-control-allow-origin" not in response.headers
    assert _submission_count(db_session) == before


def test_a_capture_post_without_an_origin_is_refused(hosted: TestClient) -> None:
    """Every real capture carries `Origin`; a write that does not is not one."""

    assert _capture(hosted, headers=BEARER, origin=None).status_code == 401


def test_a_session_cookie_alone_is_not_an_extension_request(
    hosted: TestClient, db_session: Session
) -> None:
    """The acceptance rule stated directly: a signed-in operator is not the extension."""

    cookie, csrf = _session_cookie()
    hosted.cookies.set(SESSION_COOKIE_NAME, cookie)
    before = _submission_count(db_session)
    response = hosted.post(
        CAPTURE_URL,
        json=_fresh(PROFILE_SUBMISSION),
        headers={"X-CSRF-Token": csrf, "Sec-Fetch-Site": "same-origin"},
    )
    hosted.cookies.clear()
    assert response.status_code == 403
    assert response.json()["error"] == "unauthorized"
    assert _submission_count(db_session) == before


def test_a_session_cookie_does_not_rescue_an_invalid_credential(
    hosted: TestClient, db_session: Session
) -> None:
    cookie, csrf = _session_cookie()
    hosted.cookies.set(SESSION_COOKIE_NAME, cookie)
    before = _submission_count(db_session)
    response = hosted.post(
        CAPTURE_URL,
        json=_fresh(PROFILE_SUBMISSION),
        headers={
            "Authorization": "Bearer vmrx1.beta-laptop.wrongwrongwrongwrongwrongwrongwrong",
            "Origin": EXTENSION_ORIGIN,
            "X-CSRF-Token": csrf,
        },
    )
    hosted.cookies.clear()
    assert response.status_code == 403
    assert _submission_count(db_session) == before


def test_two_authorization_headers_are_ambiguity_and_refuse(hosted: TestClient) -> None:
    """A proxy or client must not get to choose which credential is read."""

    response = hosted.post(
        CAPTURE_URL,
        json=_fresh(PROFILE_SUBMISSION),
        headers=[
            ("Origin", EXTENSION_ORIGIN),
            ("Authorization", f"Bearer {CREDENTIAL}"),
            ("Authorization", f"Bearer {CREDENTIAL}"),
            ("Content-Type", "application/json"),
        ],
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# C. Narrowness: the credential is not a bearer-token mode for the application
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/admin"),
        ("GET", "/app"),
        ("GET", "/openapi.json"),
        ("POST", "/api/campaigns"),
        ("POST", "/campaigns"),
        ("POST", "/api/intake/sales-navigator/stage"),
        ("POST", "/api/intake/linkedin-profile/stage"),
        ("POST", "/api/intake/linkedin-company/stage"),
        ("GET", "/api/contact-captures/pending"),
        ("DELETE", CAPTURE_URL),
        ("GET", CAPTURE_URL),
    ],
)
def test_the_credential_authorizes_nothing_outside_the_enumerated_contract(
    hosted: TestClient, method: str, path: str
) -> None:
    """A valid credential on any other route is worth exactly nothing."""

    response = hosted.request(method, path, headers={**BEARER, "Origin": EXTENSION_ORIGIN}, json={})
    assert response.status_code in {401, 403}, f"{method} {path} -> {response.status_code}"


def test_a_path_spelled_around_the_contract_is_still_refused(hosted: TestClient) -> None:
    """Normalisation must not become a way to reach a route the table does not list."""

    for spelling in (
        "/api/intake/../intake/contact-captures/../../admin",
        "/api/intake/contact-captures/",
        "//api//intake//contact-captures",
    ):
        response = hosted.post(
            spelling,
            json=_fresh(PROFILE_SUBMISSION),
            headers={**BEARER, "Origin": EXTENSION_ORIGIN},
        )
        # The two spellings that normalise to the contract path may be served;
        # the one that walks out of it may never be.
        if "admin" in spelling:
            assert response.status_code in {401, 403}
        else:
            assert response.status_code in {201, 401, 403, 404, 405, 307}


def test_the_three_contract_reads_work_with_the_credential(hosted: TestClient) -> None:
    for url in (LABELS_URL, LOOKUP_URL, CAMPAIGNS_URL):
        response = hosted.get(url, headers={**BEARER, "Origin": EXTENSION_ORIGIN})
        assert response.status_code == 200, f"{url} -> {response.status_code} {response.text}"
        assert response.headers.get("access-control-allow-origin") == EXTENSION_ORIGIN


def test_the_contract_reads_are_refused_without_the_credential(hosted: TestClient) -> None:
    for url in (LABELS_URL, LOOKUP_URL, CAMPAIGNS_URL):
        response = hosted.get(url, headers={"Origin": EXTENSION_ORIGIN})
        assert response.status_code == 401, f"{url} -> {response.status_code}"


def test_the_contract_table_matches_what_the_routes_actually_serve() -> None:
    """A path added to the contract must exist; the table is not a wish list."""

    app = create_app(readiness_probe=_AlwaysReadyProbe())
    served: dict[str, set[str]] = {}

    def collect(routes: Any) -> None:
        # This FastAPI version wraps an included router in a single
        # `_IncludedRouter` object rather than flattening its routes onto the
        # app, and the wrapper exposes the real router as `original_router`
        # rather than as `routes`. The walk handles both shapes so it keeps
        # working across that difference. Reading the live table is the whole
        # point of this test: a second hand-written list would drift exactly
        # like the first one did.
        for route in routes:
            wrapped = getattr(route, "original_router", None)
            nested = getattr(wrapped, "routes", None) or getattr(route, "routes", None)
            if nested:
                collect(nested)
                continue
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if path and methods:
                served.setdefault(path, set()).update(methods)

    collect(app.routes)
    assert served, "no routes were collected; the walk is wrong, not the contract"
    for path, methods in EXTENSION_CAPTURE_CONTRACT.items():
        assert path in served, f"{path} is in the contract but no route serves it"
        assert methods <= served[path], f"{path} does not serve {methods - served[path]}"


# ---------------------------------------------------------------------------
# D. Preflight
# ---------------------------------------------------------------------------


def _preflight(client: TestClient, url: str, *, origin: str, method: str):
    return client.options(
        url,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )


def test_preflight_succeeds_for_the_enumerated_contract(hosted: TestClient) -> None:
    response = _preflight(hosted, CAPTURE_URL, origin=EXTENSION_ORIGIN, method="POST")
    assert response.status_code == 204
    assert response.content == b""
    assert response.headers["access-control-allow-origin"] == EXTENSION_ORIGIN
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "Authorization" in response.headers["access-control-allow-headers"]
    # Never a credentialed CORS grant: the extension sets its own header and has
    # no business carrying the operator's cookie.
    assert "access-control-allow-credentials" not in response.headers


def test_preflight_is_refused_for_an_unapproved_origin(hosted: TestClient) -> None:
    for origin in (HOSTILE_ORIGIN, OTHER_EXTENSION_ORIGIN):
        response = _preflight(hosted, CAPTURE_URL, origin=origin, method="POST")
        assert response.status_code == 401
        assert "access-control-allow-origin" not in response.headers


def test_preflight_is_refused_for_a_method_outside_the_contract(hosted: TestClient) -> None:
    for method in ("DELETE", "PUT", "GET"):
        response = _preflight(hosted, CAPTURE_URL, origin=EXTENSION_ORIGIN, method=method)
        assert response.status_code == 401, f"{method} preflight -> {response.status_code}"


def test_preflight_is_refused_for_a_path_outside_the_contract(hosted: TestClient) -> None:
    for url in ("/admin", "/api/campaigns/x/imports", "/api/intake/linkedin-company/stage"):
        response = _preflight(hosted, url, origin=EXTENSION_ORIGIN, method="POST")
        assert response.status_code in {401, 403}, f"{url} -> {response.status_code}"


def test_a_bare_options_without_a_requested_method_is_refused(hosted: TestClient) -> None:
    """Only a real preflight is answered, not any OPTIONS from an approved origin."""

    response = hosted.options(CAPTURE_URL, headers={"Origin": EXTENSION_ORIGIN})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# E. Capture semantics are unchanged by the new boundary
# ---------------------------------------------------------------------------


def test_idempotent_resubmission_is_unchanged_over_the_credential(
    hosted: TestClient, db_session: Session
) -> None:
    """The same submission id replays; it does not duplicate."""

    payload = _fresh(PROFILE_SUBMISSION)
    before = _submission_count(db_session)
    first = hosted.post(CAPTURE_URL, json=payload, headers={**BEARER, "Origin": EXTENSION_ORIGIN})
    second = hosted.post(CAPTURE_URL, json=payload, headers={**BEARER, "Origin": EXTENSION_ORIGIN})
    assert first.status_code == 201
    assert second.status_code in {200, 201}
    assert second.json()["already_received"] is True
    assert second.json()["submission_id"] == first.json()["submission_id"]
    assert _submission_count(db_session) == before + 1


def test_a_capture_stays_contact_first_with_no_campaign(
    hosted: TestClient, db_session: Session
) -> None:
    payload = _fresh(PROFILE_SUBMISSION)
    payload.pop("campaign_id", None)
    response = hosted.post(
        CAPTURE_URL, json=payload, headers={**BEARER, "Origin": EXTENSION_ORIGIN}
    )
    assert response.status_code == 201
    assert response.json()["results"][0]["campaign_filing"] is None


# ---------------------------------------------------------------------------
# F. The credential never appears anywhere it could be read
# ---------------------------------------------------------------------------


def test_no_response_on_the_boundary_echoes_the_credential(hosted: TestClient) -> None:
    responses = [
        _capture(hosted),
        _capture(hosted, headers=BEARER),
        _capture(hosted, headers=BEARER, origin=HOSTILE_ORIGIN),
        _preflight(hosted, CAPTURE_URL, origin=EXTENSION_ORIGIN, method="POST"),
        hosted.get(LABELS_URL, headers={**BEARER, "Origin": EXTENSION_ORIGIN}),
    ]
    for response in responses:
        haystack = response.text + json.dumps(dict(response.headers))
        assert SECRET not in haystack
        assert CREDENTIAL not in haystack


def test_the_access_log_records_no_credential(
    hosted: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="vmr.http"):
        _capture(hosted, headers=BEARER)
        _capture(hosted, headers={"Authorization": f"Bearer vmrx1.{KEY_ID}.{'z' * 44}"})
    recorded = "\n".join(record.getMessage() for record in caplog.records)
    assert recorded, "the hardening middleware should have logged the requests"
    assert SECRET not in recorded
    assert CREDENTIAL not in recorded
    assert "Bearer" not in recorded


def test_the_settings_dump_carries_no_credential_material() -> None:
    settings = ExtensionAuthSettings(
        enabled=True,
        credentials=(f"{KEY_ID}:{credential_digest(SECRET)}",),
        allowed_origins=(EXTENSION_ORIGIN,),
    )
    dumped = json.dumps(settings.model_dump(mode="json"))
    assert credential_digest(SECRET) not in dumped
    assert SECRET not in dumped
    assert credential_digest(SECRET) not in repr(settings)


def test_no_rendered_page_carries_the_credential(hosted: TestClient) -> None:
    """The operator UI must not have grown a place that prints it."""

    cookie, _ = _session_cookie()
    hosted.cookies.set(SESSION_COOKIE_NAME, cookie)
    for path in ("/app", "/admin"):
        response = hosted.get(path, headers={"Accept": "text/html"})
        assert SECRET not in response.text
        assert KEY_ID not in response.text
    hosted.cookies.clear()


# ---------------------------------------------------------------------------
# G. The startup contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"EXTENSION_AUTH__CREDENTIALS": "[]"}, "EXTENSION_AUTH__CREDENTIALS"),
        ({"EXTENSION_AUTH__ALLOWED_ORIGINS": "[]"}, "EXTENSION_AUTH__ALLOWED_ORIGINS"),
        ({"FEATURES__CONTACT_CAPTURE_INTAKE": "false"}, "FEATURES__CONTACT_CAPTURE_INTAKE"),
        (
            {"APP_ENV": "production", "FEATURES__WORKBENCH": "false"},
            "may not be true in production",
        ),
    ],
)
def test_a_half_configured_extension_boundary_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, str], fragment: str
) -> None:
    _apply(monkeypatch, _base_env(**overrides))
    try:
        with pytest.raises(HostedAuthConfigurationError) as caught:
            create_app(readiness_probe=_AlwaysReadyProbe())
        assert fragment in str(caught.value)
    finally:
        get_settings.cache_clear()


def test_hosted_capture_without_the_credential_boundary_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rule the old local-only list was really protecting: no ungated hosted intake."""

    _apply(monkeypatch, _base_env(EXTENSION_AUTH__ENABLED="false"))
    try:
        with pytest.raises(RuntimeConfigurationError) as caught:
            create_app(readiness_probe=_AlwaysReadyProbe())
        assert "contact_capture_intake" in str(caught.value)
    finally:
        get_settings.cache_clear()


def test_the_other_intakes_are_still_local_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only contact capture became credential-gated; the rest did not move."""

    _apply(monkeypatch, _base_env(FEATURES__LINKEDIN_COMPANY_INTAKE="true"))
    try:
        with pytest.raises(RuntimeConfigurationError) as caught:
            create_app(readiness_probe=_AlwaysReadyProbe())
        assert "linkedin_company_intake" in str(caught.value)
    finally:
        get_settings.cache_clear()


def test_the_extension_credential_does_not_replace_operator_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _apply(monkeypatch, _base_env(AUTH__ENABLED="false"))
    try:
        with pytest.raises(HostedAuthConfigurationError) as caught:
            create_app(readiness_probe=_AlwaysReadyProbe())
        assert "AUTH__ENABLED" in str(caught.value)
    finally:
        get_settings.cache_clear()


def test_a_complete_hosted_capture_configuration_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply(monkeypatch, _base_env())
    try:
        assert create_app(readiness_probe=_AlwaysReadyProbe()) is not None
    finally:
        get_settings.cache_clear()

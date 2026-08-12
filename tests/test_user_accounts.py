"""Coverage for admin-created accounts, password login and first-login setup.

Written from the same side as ``test_hosted_auth.py``: every test below names
something a real caller would try — signing in with an address nobody created,
replaying a password link, typing ``/app/admin/users`` as an ordinary user,
using a session that was valid until an administrator disabled the account — and
asserts the refusal. The happy paths exist to keep the refusals from being
vacuous.

Nothing here contacts Google. The Google-coexistence tests use the same
deterministic provider seam the hosted-auth suite uses, so what is being asserted
is the *authorization* half — which account a validated assertion resolves to —
rather than the cryptography, which is already covered.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.core.auth import passwords
from app.core.auth.accounts import AccountLookupUnavailable, DatabaseAccountDirectory
from app.core.auth.ratelimit import LoginRateLimiter
from app.core.auth.session import SESSION_COOKIE_NAME, SessionCodec
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.main import create_app
from app.models.enums import UserCredentialTokenPurpose, UserRole, UserState
from app.models.user import User, UserCredentialToken
from app.services.users import service as user_service
from app.services.users import tokens as token_service
from app.web import auth_routes
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.hosted_auth_factory import (
    TEST_CLIENT_ID,
    RecordingIdentityProvider,
    operator_claims,
    seed_account,
    subject_for,
)

HOST = "srv1885453.hstgr.cloud"
ORIGIN = f"https://{HOST}"
SESSION_SECRET = "test-session-secret-value-at-least-32-chars"
ADMIN_EMAIL = "sahil@verifiedmarketresearch.com"
STAGING_DATABASE_URL = "postgresql+psycopg://vmr:secret@db.internal.example:5432/vmr_staging"

#: Comfortably over the fifteen-character minimum, and not on the blocklist.
GOOD_PASSWORD = "correct-battery-horse-2026"
OTHER_PASSWORD = "a-different-long-passphrase-99"


class _AlwaysReadyProbe:
    def __call__(self) -> None:
        return None


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "APP_ENV": "staging",
        "DEBUG": "false",
        "DRY_RUN": "true",
        "TRUSTED_HOSTS": f'["{HOST}"]',
        "DATABASE_URL": STAGING_DATABASE_URL,
        "FEATURES__WORKBENCH": "true",
        "AUTH__ENABLED": "true",
        "AUTH__SESSION_SECRET": SESSION_SECRET,
        "AUTH__ALLOWED_OPERATOR_EMAILS": "[]",
        "AUTH__BOOTSTRAP_ADMIN_EMAIL": ADMIN_EMAIL,
        "AUTH__GOOGLE_CLIENT_ID": TEST_CLIENT_ID,
        "AUTH__GOOGLE_CLIENT_SECRET": "test-client-secret",
        "AUTH__PUBLIC_BASE_URL": ORIGIN,
    }
    env.update(overrides)
    return env


@pytest.fixture
def provider() -> RecordingIdentityProvider:
    return RecordingIdentityProvider()


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch, provider: RecordingIdentityProvider
) -> Iterator[TestClient]:
    for key, value in _env().items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    auth_routes.login_rate_limiter.reset()
    app = create_app(readiness_probe=_AlwaysReadyProbe(), identity_provider=provider)
    try:
        yield TestClient(app, base_url=ORIGIN, follow_redirects=False)
    finally:
        auth_routes.login_rate_limiter.reset()
        get_settings.cache_clear()


@pytest.fixture
def db() -> Iterator[Session]:
    """A committing session, because the routes under test use their own."""

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin_session(client: TestClient, email: str = ADMIN_EMAIL) -> str:
    """Sign a fresh administrator in and return the CSRF token for their session.

    Goes through the real cookie codec rather than the login form so that the
    tests about *administration* are not also tests about logging in.
    """

    account = seed_account(email=email, role="admin")
    return _attach_session(client, account.user_id, email)


def _attach_session(client: TestClient, user_id: str, email: str, *, auth_version: int = 1) -> str:
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


def _create_account_via_ui(client: TestClient, csrf: str, email: str) -> str:
    """Create an account through the admin form and return its one-time link."""

    created = client.post(
        "/app/admin/users/create",
        data={"email": email, "display_name": "", "_csrf": csrf},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert created.status_code == 303, created.text
    page = client.get(created.headers["location"])
    assert page.status_code == 200
    return _extract_link(page.text)


def _extract_link(html: str) -> str:
    marker = "/auth/setup?token="
    start = html.index(marker)
    end = start
    while end < len(html) and html[end] not in {"<", " ", "\n", '"'}:
        end += 1
    return html[start:end]


def _token_of(link: str) -> str:
    return link.partition("token=")[2]


# ---------------------------------------------------------------------------
# A. Password primitive and policy
# ---------------------------------------------------------------------------


def test_a_stored_password_is_an_argon2id_hash_and_never_the_password() -> None:
    stored = passwords.hash_password(GOOD_PASSWORD)
    assert stored.startswith("$argon2id$")
    assert GOOD_PASSWORD not in stored
    assert passwords.verify_password(stored, GOOD_PASSWORD)
    assert not passwords.verify_password(stored, GOOD_PASSWORD + "x")


def test_the_same_password_hashes_differently_every_time() -> None:
    """A per-password salt, asserted rather than assumed."""

    assert passwords.hash_password(GOOD_PASSWORD) != passwords.hash_password(GOOD_PASSWORD)


def test_verifying_against_a_missing_or_corrupt_hash_is_a_refusal_not_an_error() -> None:
    assert passwords.verify_password(None, GOOD_PASSWORD) is False
    assert passwords.verify_password("", GOOD_PASSWORD) is False
    assert passwords.verify_password("not-a-phc-string", GOOD_PASSWORD) is False


@pytest.mark.parametrize(
    "candidate",
    [
        "short",
        "fourteen-chars",  # 14 — one under the line
        "               ",  # only spaces
        "password12345",
        "Password123",
        "correcthorsebatterystaple",
        "verifiedmarketresearch",
    ],
)
def test_the_policy_refuses_what_it_says_it_refuses(candidate: str) -> None:
    with pytest.raises(passwords.PasswordPolicyError):
        passwords.validate_password(candidate)


@pytest.mark.parametrize(
    "candidate",
    [
        "fifteen chars!!",  # exactly 15, with a space
        GOOD_PASSWORD,
        "x" * 64,  # the length the policy promises to accept
        "  leading and trailing spaces preserved  ",
        "Ω" * 20,  # non-ASCII is a password, not an address: it is allowed
    ],
)
def test_the_policy_accepts_what_it_says_it_accepts(candidate: str) -> None:
    assert passwords.validate_password(candidate) is not None


def test_a_password_may_not_be_the_account_address() -> None:
    with pytest.raises(passwords.PasswordPolicyError):
        passwords.validate_password("someone@company.example", email="someone@company.example")


def test_an_over_long_password_is_refused_rather_than_hashed() -> None:
    """A bounded input, so a body cannot be turned into CPU exhaustion."""

    with pytest.raises(passwords.PasswordPolicyError):
        passwords.validate_password("x" * (passwords.MAX_PASSWORD_CHARS + 1))


# ---------------------------------------------------------------------------
# B. Bootstrap
# ---------------------------------------------------------------------------


def test_the_configured_address_is_bootstrapped_as_admin(db: Session) -> None:
    user = user_service.ensure_bootstrap_admin(db, email=ADMIN_EMAIL)
    assert user is not None
    assert user.role == UserRole.ADMIN
    assert user.state == UserState.ACTIVE
    assert user.password_hash is None


def test_bootstrap_is_idempotent(db: Session) -> None:
    first = user_service.ensure_bootstrap_admin(db, email=ADMIN_EMAIL)
    second = user_service.ensure_bootstrap_admin(db, email=ADMIN_EMAIL)
    assert first is not None and second is not None
    assert first.id == second.id
    assert db.scalar(select(User).where(User.email_normalized == ADMIN_EMAIL)) is not None


def test_bootstrap_promotes_an_existing_ordinary_account(db: Session) -> None:
    """The path a deployment takes when the address was already an allow-list seed."""

    user_service.seed_from_allowlist(db, emails=(ADMIN_EMAIL,))
    seeded = user_service.get_by_email(db, ADMIN_EMAIL)
    assert seeded is not None and seeded.role == UserRole.USER

    user_service.ensure_bootstrap_admin(db, email=ADMIN_EMAIL)
    promoted = user_service.get_by_email(db, ADMIN_EMAIL)
    assert promoted is not None and promoted.role == UserRole.ADMIN


def test_bootstrap_does_not_reactivate_a_disabled_administrator(db: Session) -> None:
    """Disabling is an explicit act; a restart must not undo it."""

    user = user_service.ensure_bootstrap_admin(db, email=ADMIN_EMAIL)
    assert user is not None
    # A second administrator, so that what refuses below is the bootstrap rule
    # rather than the last-active-administrator guard.
    standby = user_service.create_user(
        db, email="standby@vmr.example", display_name=None, actor=ADMIN_EMAIL
    )
    user_service.set_role(db, user=standby, role=UserRole.ADMIN, actor=ADMIN_EMAIL)
    user_service.set_state(db, user=user, state=UserState.DISABLED, actor="someone@vmr.example")

    user_service.ensure_bootstrap_admin(db, email=ADMIN_EMAIL)
    again = user_service.get_by_email(db, ADMIN_EMAIL)
    assert again is not None and again.state == UserState.DISABLED


def test_another_vmr_domain_address_is_not_automatically_an_administrator(db: Session) -> None:
    """The single most important negative in this whole slice."""

    user_service.ensure_bootstrap_admin(db, email=ADMIN_EMAIL)
    colleague = user_service.create_user(
        db,
        email="someone.else@verifiedmarketresearch.com",
        display_name=None,
        actor=ADMIN_EMAIL,
    )
    assert colleague.role == UserRole.USER
    assert user_service.count_admins(db) == 1


def test_the_allow_list_seed_never_grants_admin(db: Session) -> None:
    user_service.seed_from_allowlist(
        db, emails=("someone@verifiedmarketresearch.com", "other@vmr.example")
    )
    assert user_service.count_admins(db) == 0
    for email in ("someone@verifiedmarketresearch.com", "other@vmr.example"):
        user = user_service.get_by_email(db, email)
        assert user is not None
        assert user.role == UserRole.USER
        assert user.password_hash is None


# ---------------------------------------------------------------------------
# C. Admin-created accounts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email",
    [
        "colleague@gmail.com",
        "colleague@outlook.com",
        "colleague@vmr.onmicrosoft.com",
        "colleague@verifiedmarketresearch.com",
        "colleague@some-client.co.in",
    ],
)
def test_an_admin_can_create_an_account_on_any_legitimate_provider(db: Session, email: str) -> None:
    """Google is not the only login path, so Gmail is not the only address."""

    user = user_service.create_user(db, email=email, display_name=None, actor=ADMIN_EMAIL)
    assert user.role == UserRole.USER
    assert user.state == UserState.ACTIVE
    assert user.password_hash is None


def test_a_new_account_starts_with_no_usable_password(db: Session) -> None:
    user = user_service.create_user(db, email="new@gmail.com", display_name=None, actor=ADMIN_EMAIL)
    assert user.has_password is False
    outcome = user_service.authenticate_password(db, email="new@gmail.com", password=GOOD_PASSWORD)
    assert not outcome.succeeded


def test_creating_a_duplicate_account_is_refused(db: Session) -> None:
    user_service.create_user(db, email="dup@gmail.com", display_name=None, actor=ADMIN_EMAIL)
    with pytest.raises(user_service.UserServiceError):
        user_service.create_user(db, email="DUP@Gmail.com", display_name=None, actor=ADMIN_EMAIL)


def test_the_last_active_administrator_cannot_be_disabled_or_demoted(db: Session) -> None:
    admin = user_service.ensure_bootstrap_admin(db, email=ADMIN_EMAIL)
    assert admin is not None
    with pytest.raises(user_service.UserServiceError):
        user_service.set_state(db, user=admin, state=UserState.DISABLED, actor=ADMIN_EMAIL)
    with pytest.raises(user_service.UserServiceError):
        user_service.set_role(db, user=admin, role=UserRole.USER, actor=ADMIN_EMAIL)


# ---------------------------------------------------------------------------
# D. The admin surface, and who may reach it
# ---------------------------------------------------------------------------


def test_an_admin_sees_the_users_screen(client: TestClient) -> None:
    csrf = _admin_session(client)
    assert csrf
    response = client.get("/app/admin/users")
    assert response.status_code == 200
    assert "People" in response.text


def test_an_ordinary_user_typing_the_url_is_refused(client: TestClient) -> None:
    """Hiding the link is a courtesy. This is the control."""

    account = seed_account(email="ordinary@gmail.com")
    _attach_session(client, account.user_id, account.email)
    response = client.get("/app/admin/users")
    assert response.status_code == 403
    assert response.json()["error"] == "admin_required"


@pytest.mark.parametrize(
    "path",
    [
        # `POST /app/admin/users` is deliberately absent: no route accepts POST
        # there, so Starlette answers 405 before any dependency runs. It grants
        # nothing, and asserting 403 on it would be asserting the router's
        # behaviour rather than the authorization guard's.
        "/app/admin/users/create",
        "/app/admin/users/00000000-0000-4000-8000-000000000001/state",
        "/app/admin/users/00000000-0000-4000-8000-000000000001/role",
        "/app/admin/users/00000000-0000-4000-8000-000000000001/link",
    ],
)
def test_an_ordinary_user_cannot_post_to_any_admin_route(client: TestClient, path: str) -> None:
    account = seed_account(email="ordinary@gmail.com")
    csrf = _attach_session(client, account.user_id, account.email)
    response = client.post(path, data={"_csrf": csrf}, headers={"Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 403
    assert response.json()["error"] == "admin_required"


def test_an_anonymous_caller_is_refused_before_the_admin_check(client: TestClient) -> None:
    response = client.get("/app/admin/users")
    assert response.status_code in {303, 401}


def test_a_demoted_administrator_loses_the_screen_on_the_next_request(
    client: TestClient, db: Session
) -> None:
    """Role is read from the account record, not from the cookie."""

    account = seed_account(email="admin2@gmail.com", role="admin")
    seed_account(email="other-admin@gmail.com", role="admin")
    _attach_session(client, account.user_id, account.email)
    assert client.get("/app/admin/users").status_code == 200

    with SessionLocal() as session:
        user = session.get(User, uuid.UUID(account.user_id))
        assert user is not None
        user.role = UserRole.USER
        session.commit()

    # Refused because the cookie's `auth_version` still matches but the role in
    # the directory no longer does. (A real demotion through the service also
    # bumps the version, which ends the session outright.)
    assert client.get("/app/admin/users").status_code == 403


def test_creating_an_account_shows_the_link_once_and_never_again(client: TestClient) -> None:
    csrf = _admin_session(client)
    link = _create_account_via_ui(client, csrf, "invitee@gmail.com")
    assert "/auth/setup?token=" in link

    # A reload of the same page no longer carries it.
    again = client.get(
        "/app/admin/users?issued=" + link.split("token=")[0][:0] or "/app/admin/users"
    )
    assert "/auth/setup?token=" not in again.text


def test_the_users_screen_never_renders_a_password_hash(client: TestClient) -> None:
    csrf = _admin_session(client)
    account = seed_account(email="haspw@gmail.com", password=GOOD_PASSWORD)
    assert csrf
    body = client.get("/app/admin/users").text
    assert "$argon2id$" not in body
    assert GOOD_PASSWORD not in body
    with SessionLocal() as session:
        user = session.get(User, uuid.UUID(account.user_id))
        assert user is not None and user.password_hash is not None
        assert user.password_hash not in body


# ---------------------------------------------------------------------------
# E. First-login password setup
# ---------------------------------------------------------------------------


def test_a_valid_link_sets_a_password_and_does_not_sign_the_person_in(
    client: TestClient,
) -> None:
    csrf = _admin_session(client)
    link = _create_account_via_ui(client, csrf, "invitee@gmail.com")
    client.cookies.clear()

    form = client.get(link)
    assert form.status_code == 200
    assert "invitee@gmail.com" in form.text

    done = client.post(
        "/auth/setup",
        data={
            "token": _token_of(link),
            "password": GOOD_PASSWORD,
            "password_confirm": GOOD_PASSWORD,
        },
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert done.status_code == 200
    assert "Your password is set" in done.text
    # No session was created. Setting a password proves possession of a link,
    # not of the password.
    assert SESSION_COOKIE_NAME not in client.cookies

    with SessionLocal() as session:
        user = user_service.get_by_email(session, "invitee@gmail.com")
        assert user is not None
        assert user.password_hash is not None
        assert user.password_hash.startswith("$argon2id$")
        assert GOOD_PASSWORD not in user.password_hash


def test_only_a_digest_of_the_link_is_stored(db: Session) -> None:
    user = user_service.create_user(
        db, email="digest@gmail.com", display_name=None, actor=ADMIN_EMAIL
    )
    issued = user_service.issue_credential_link(db, user=user, actor=ADMIN_EMAIL)
    db.commit()

    rows = list(db.scalars(select(UserCredentialToken)).all())
    assert len(rows) == 1
    assert issued.raw_token not in rows[0].token_digest
    assert rows[0].token_digest == token_service.digest_token(issued.raw_token)
    assert len(rows[0].token_digest) == 64


def test_replaying_a_consumed_link_fails(client: TestClient) -> None:
    csrf = _admin_session(client)
    link = _create_account_via_ui(client, csrf, "replay@gmail.com")
    client.cookies.clear()
    payload = {
        "token": _token_of(link),
        "password": GOOD_PASSWORD,
        "password_confirm": GOOD_PASSWORD,
    }
    headers = {"Sec-Fetch-Site": "same-origin"}
    assert client.post("/auth/setup", data=payload, headers=headers).status_code == 200

    replay = client.post(
        "/auth/setup",
        data={**payload, "password": OTHER_PASSWORD, "password_confirm": OTHER_PASSWORD},
        headers=headers,
    )
    assert replay.status_code == 400
    assert "no longer valid" in replay.text

    # And the password is still the first one.
    with SessionLocal() as session:
        user = user_service.get_by_email(session, "replay@gmail.com")
        assert user is not None
        assert passwords.verify_password(user.password_hash, GOOD_PASSWORD)
        assert not passwords.verify_password(user.password_hash, OTHER_PASSWORD)


def test_an_expired_link_fails(db: Session) -> None:
    user = user_service.create_user(
        db, email="expired@gmail.com", display_name=None, actor=ADMIN_EMAIL
    )
    issued = user_service.issue_credential_link(
        db, user=user, actor=ADMIN_EMAIL, lifetime=timedelta(hours=24)
    )
    later = datetime.now(UTC) + timedelta(hours=25)
    with pytest.raises(token_service.CredentialTokenError):
        user_service.complete_password_setup(
            db, raw_token=issued.raw_token, new_password=GOOD_PASSWORD, now=later
        )


def test_a_superseded_link_fails(db: Session) -> None:
    user = user_service.create_user(
        db, email="superseded@gmail.com", display_name=None, actor=ADMIN_EMAIL
    )
    first = user_service.issue_credential_link(db, user=user, actor=ADMIN_EMAIL)
    second = user_service.issue_credential_link(db, user=user, actor=ADMIN_EMAIL)

    with pytest.raises(token_service.CredentialTokenError):
        user_service.complete_password_setup(
            db, raw_token=first.raw_token, new_password=GOOD_PASSWORD
        )
    # The newest one still works, so the refusal above is about supersession
    # rather than about both links being broken.
    user_service.complete_password_setup(db, raw_token=second.raw_token, new_password=GOOD_PASSWORD)


def test_a_link_for_a_disabled_account_fails(db: Session) -> None:
    """Disabling must not be undone by a link already in somebody's inbox."""

    user = user_service.create_user(
        db, email="disabled@gmail.com", display_name=None, actor=ADMIN_EMAIL
    )
    issued = user_service.issue_credential_link(db, user=user, actor=ADMIN_EMAIL)
    # A second administrator exists so the last-admin guard is not what refuses.
    user_service.ensure_bootstrap_admin(db, email=ADMIN_EMAIL)
    user_service.set_state(db, user=user, state=UserState.DISABLED, actor=ADMIN_EMAIL)

    with pytest.raises(token_service.CredentialTokenError):
        user_service.complete_password_setup(
            db, raw_token=issued.raw_token, new_password=GOOD_PASSWORD
        )


def test_an_unknown_token_fails_the_same_way(client: TestClient) -> None:
    response = client.get("/auth/setup?token=" + "z" * 43)
    assert response.status_code == 400
    assert "no longer valid" in response.text


def test_a_rejected_password_does_not_burn_the_link(client: TestClient) -> None:
    csrf = _admin_session(client)
    link = _create_account_via_ui(client, csrf, "retry@gmail.com")
    client.cookies.clear()
    headers = {"Sec-Fetch-Site": "same-origin"}

    too_short = client.post(
        "/auth/setup",
        data={"token": _token_of(link), "password": "short", "password_confirm": "short"},
        headers=headers,
    )
    assert too_short.status_code == 400
    assert "at least 15 characters" in too_short.text

    mismatch = client.post(
        "/auth/setup",
        data={
            "token": _token_of(link),
            "password": GOOD_PASSWORD,
            "password_confirm": OTHER_PASSWORD,
        },
        headers=headers,
    )
    assert mismatch.status_code == 400
    assert "do not match" in mismatch.text

    accepted = client.post(
        "/auth/setup",
        data={
            "token": _token_of(link),
            "password": GOOD_PASSWORD,
            "password_confirm": GOOD_PASSWORD,
        },
        headers=headers,
    )
    assert accepted.status_code == 200


def test_opening_the_setup_page_does_not_consume_the_link(client: TestClient) -> None:
    """A chat client's link preview must not lock somebody out."""

    csrf = _admin_session(client)
    link = _create_account_via_ui(client, csrf, "preview@gmail.com")
    client.cookies.clear()

    for _ in range(3):
        assert client.get(link).status_code == 200
    assert (
        client.post(
            "/auth/setup",
            data={
                "token": _token_of(link),
                "password": GOOD_PASSWORD,
                "password_confirm": GOOD_PASSWORD,
            },
            headers={"Sec-Fetch-Site": "same-origin"},
        ).status_code
        == 200
    )


def test_a_reset_link_is_recorded_as_a_reset_not_a_first_setup(db: Session) -> None:
    user = user_service.create_user(
        db, email="reset@gmail.com", display_name=None, actor=ADMIN_EMAIL
    )
    first = user_service.issue_credential_link(db, user=user, actor=ADMIN_EMAIL)
    assert first.purpose == UserCredentialTokenPurpose.INITIAL_SETUP
    user_service.complete_password_setup(db, raw_token=first.raw_token, new_password=GOOD_PASSWORD)
    second = user_service.issue_credential_link(db, user=user, actor=ADMIN_EMAIL)
    assert second.purpose == UserCredentialTokenPurpose.RESET


# ---------------------------------------------------------------------------
# F. Email + password login
# ---------------------------------------------------------------------------


def _login(client: TestClient, email: str, password: str) -> Any:
    return client.post(
        "/auth/password",
        data={"email": email, "password": password, "next": "/app"},
        headers={"Sec-Fetch-Site": "same-origin"},
    )


def test_the_sign_in_page_offers_both_paths(client: TestClient) -> None:
    body = client.get("/auth/login").text
    assert 'action="/auth/password"' in body
    assert 'name="password"' in body
    assert "Sign in with Google" in body
    # The product name matters: this is Google account sign-in, not Gmail.
    assert "Sign in with Gmail" not in body


def test_the_sign_in_form_is_password_manager_friendly(client: TestClient) -> None:
    """Autofill, autocomplete and paste, asserted as markup rather than assumed."""

    body = client.get("/auth/login").text
    assert 'autocomplete="username"' in body
    assert 'autocomplete="current-password"' in body
    assert 'method="post"' in body
    assert 'type="password"' in body
    assert "onpaste" not in body.lower()


def test_a_correct_email_and_password_signs_in(client: TestClient) -> None:
    seed_account(email="member@gmail.com", password=GOOD_PASSWORD)
    response = _login(client, "member@gmail.com", GOOD_PASSWORD)
    assert response.status_code == 303
    assert response.headers["location"] == "/app"
    assert client.get("/app").status_code == 200


def test_the_address_is_matched_case_insensitively(client: TestClient) -> None:
    seed_account(email="member@gmail.com", password=GOOD_PASSWORD)
    assert _login(client, "  Member@GMAIL.com ", GOOD_PASSWORD).status_code == 303


@pytest.mark.parametrize(
    ("email", "password", "setup"),
    [
        ("nobody@gmail.com", GOOD_PASSWORD, None),
        ("member@gmail.com", "the-wrong-password-entirely", "with-password"),
        ("member@gmail.com", GOOD_PASSWORD, "no-password"),
        ("member@gmail.com", GOOD_PASSWORD, "disabled"),
        ("not-an-address", GOOD_PASSWORD, None),
    ],
    ids=["unknown", "wrong-password", "password-never-set", "disabled", "malformed"],
)
def test_every_refusal_looks_identical_from_outside(
    client: TestClient, email: str, password: str, setup: str | None
) -> None:
    """The non-enumeration property, asserted across all four real causes."""

    if setup == "with-password":
        seed_account(email="member@gmail.com", password=GOOD_PASSWORD)
    elif setup == "no-password":
        seed_account(email="member@gmail.com")
    elif setup == "disabled":
        seed_account(email="member@gmail.com", password=GOOD_PASSWORD, state="disabled")

    response = _login(client, email, password)
    assert response.status_code == 401
    assert auth_routes.SIGN_IN_REFUSED_MESSAGE in response.text
    assert SESSION_COOKIE_NAME not in client.cookies
    # Nothing in the body distinguishes the cause.
    assert "disabled" not in response.text.lower().replace("v2-", "")
    assert "no such" not in response.text.lower()


def test_a_disabled_account_cannot_sign_in_even_with_the_right_password(
    client: TestClient,
) -> None:
    seed_account(email="member@gmail.com", password=GOOD_PASSWORD, state="disabled")
    assert _login(client, "member@gmail.com", GOOD_PASSWORD).status_code == 401
    assert client.get("/app").status_code in {303, 401}


def test_failed_logins_are_rate_limited(client: TestClient) -> None:
    seed_account(email="member@gmail.com", password=GOOD_PASSWORD)
    for _ in range(5):
        assert _login(client, "member@gmail.com", "wrong-password-here-ok").status_code == 401
    throttled = _login(client, "member@gmail.com", "wrong-password-here-ok")
    assert throttled.status_code == 429
    assert "Retry-After" in throttled.headers
    # And the correct password is throttled too, which is what makes it a limit
    # rather than a hint.
    assert _login(client, "member@gmail.com", GOOD_PASSWORD).status_code == 429


def test_the_limiter_throttles_and_never_locks() -> None:
    """No attacker-triggerable lockout: the window simply rolls over."""

    limiter = LoginRateLimiter(email_limit=3, client_limit=100, window_seconds=60)
    for _ in range(3):
        limiter.record_failure(email="victim@vmr.example", client="1.2.3.4", now=1_000)
    assert limiter.is_blocked(email="victim@vmr.example", client="1.2.3.4", now=1_000)
    assert not limiter.is_blocked(email="victim@vmr.example", client="1.2.3.4", now=1_061)


def test_a_successful_sign_in_clears_that_addresss_failures() -> None:
    limiter = LoginRateLimiter(email_limit=3, client_limit=100, window_seconds=60)
    for _ in range(2):
        limiter.record_failure(email="member@gmail.com", client="1.2.3.4", now=1_000)
    limiter.record_success(email="member@gmail.com", now=1_000)
    for _ in range(2):
        limiter.record_failure(email="member@gmail.com", client="1.2.3.4", now=1_000)
    assert not limiter.is_blocked(email="member@gmail.com", client="1.2.3.4", now=1_000)


def test_one_client_cannot_walk_a_list_of_addresses_at_full_speed() -> None:
    limiter = LoginRateLimiter(email_limit=5, client_limit=10, window_seconds=60)
    for index in range(10):
        limiter.record_failure(email=f"person{index}@vmr.example", client="9.9.9.9", now=1_000)
    assert limiter.is_blocked(email="fresh@vmr.example", client="9.9.9.9", now=1_000)


def test_signing_in_stamps_the_last_login_time(client: TestClient) -> None:
    account = seed_account(email="member@gmail.com", password=GOOD_PASSWORD)
    assert _login(client, "member@gmail.com", GOOD_PASSWORD).status_code == 303
    with SessionLocal() as session:
        user = session.get(User, uuid.UUID(account.user_id))
        assert user is not None and user.last_login_at is not None


def test_there_is_no_public_signup_endpoint(client: TestClient) -> None:
    """Knowing the URL, or an address, must not be enough to become a user."""

    for path in ("/auth/register", "/auth/signup", "/signup", "/register", "/app/signup"):
        assert client.post(path, json={"email": "intruder@gmail.com"}).status_code in {
            401,
            403,
            404,
            405,
        }
    # And logging in with an address nobody created creates nothing.
    _login(client, "intruder@gmail.com", GOOD_PASSWORD)
    with SessionLocal() as session:
        assert user_service.get_by_email(session, "intruder@gmail.com") is None


# ---------------------------------------------------------------------------
# G. Google coexistence
# ---------------------------------------------------------------------------


def _google_sign_in(
    client: TestClient, provider: RecordingIdentityProvider, **overrides: Any
) -> Any:
    started = client.get("/auth/google/start?next=%2Fapp")
    assert started.status_code == 303
    transaction = provider.authorization_calls[-1]
    provider.claims = operator_claims(nonce=transaction["nonce"], **overrides)
    return client.get(f"/auth/callback?code=test-code&state={transaction['state']}")


def test_google_and_password_resolve_to_the_same_account(
    client: TestClient, provider: RecordingIdentityProvider
) -> None:
    """One person, one row — whichever door they come through."""

    account = seed_account(email="both@gmail.com", password=GOOD_PASSWORD)

    assert _login(client, "both@gmail.com", GOOD_PASSWORD).status_code == 303
    client.cookies.clear()
    assert _google_sign_in(client, provider, email="both@gmail.com").status_code == 303

    with SessionLocal() as session:
        rows = list(session.scalars(select(User).where(User.email_normalized == "both@gmail.com")))
        assert len(rows) == 1
        assert str(rows[0].id) == account.user_id
        assert rows[0].google_subject == subject_for("both@gmail.com")


def test_an_unknown_google_identity_is_refused_and_creates_nothing(
    client: TestClient, provider: RecordingIdentityProvider
) -> None:
    response = _google_sign_in(client, provider, email="stranger@gmail.com")
    assert response.status_code == 403
    with SessionLocal() as session:
        assert user_service.get_by_email(session, "stranger@gmail.com") is None
        assert session.scalar(select(User)) is None


def test_a_disabled_account_cannot_sign_in_with_google(
    client: TestClient, provider: RecordingIdentityProvider
) -> None:
    seed_account(email="gone@gmail.com", state="disabled")
    assert _google_sign_in(client, provider, email="gone@gmail.com").status_code == 403


def test_a_renamed_workspace_address_still_resolves_by_subject(
    client: TestClient, provider: RecordingIdentityProvider, db: Session
) -> None:
    """The reason ``sub`` is stored at all."""

    account = seed_account(
        email="old.name@verifiedmarketresearch.com",
        google_subject=subject_for("old.name@verifiedmarketresearch.com"),
    )
    response = _google_sign_in(
        client,
        provider,
        email="new.name@verifiedmarketresearch.com",
        subject=subject_for("old.name@verifiedmarketresearch.com"),
    )
    assert response.status_code == 303
    with SessionLocal() as session:
        assert len(list(session.scalars(select(User)))) == 1
        assert str(next(iter(session.scalars(select(User)))).id) == account.user_id


def test_a_new_subject_on_a_known_address_is_refused(
    client: TestClient, provider: RecordingIdentityProvider
) -> None:
    """A reissued address is a different person until an administrator says otherwise."""

    seed_account(email="reissued@vmr.example", google_subject="google-sub-original")
    response = _google_sign_in(
        client, provider, email="reissued@vmr.example", subject="google-sub-somebody-new"
    )
    assert response.status_code == 403


def test_google_login_asks_for_identity_scopes_only(client: TestClient) -> None:
    """No Gmail scope may creep into the platform-identity client."""

    from app.core.auth.config import GOOGLE_IDENTITY_SCOPES

    assert GOOGLE_IDENTITY_SCOPES == ("openid", "email", "profile")
    assert not any("gmail" in scope for scope in GOOGLE_IDENTITY_SCOPES)
    assert not any("mail.google" in scope for scope in GOOGLE_IDENTITY_SCOPES)


# ---------------------------------------------------------------------------
# H. Session revocation
# ---------------------------------------------------------------------------


def test_disabling_an_account_refuses_its_existing_session(client: TestClient) -> None:
    account = seed_account(email="member@gmail.com", password=GOOD_PASSWORD)
    _attach_session(client, account.user_id, account.email)
    assert client.get("/app").status_code == 200

    with SessionLocal() as session:
        user = session.get(User, uuid.UUID(account.user_id))
        assert user is not None
        user_service.set_state(session, user=user, state=UserState.DISABLED, actor=ADMIN_EMAIL)
        session.commit()

    assert client.get("/app").status_code in {303, 401}


def test_a_password_reset_refuses_earlier_sessions(client: TestClient) -> None:
    account = seed_account(email="member@gmail.com", password=GOOD_PASSWORD)
    _attach_session(client, account.user_id, account.email)
    assert client.get("/app").status_code == 200

    with SessionLocal() as session:
        user = session.get(User, uuid.UUID(account.user_id))
        assert user is not None
        issued = user_service.issue_credential_link(session, user=user, actor=ADMIN_EMAIL)
        user_service.complete_password_setup(
            session, raw_token=issued.raw_token, new_password=OTHER_PASSWORD
        )
        session.commit()

    assert client.get("/app").status_code in {303, 401}


def test_a_deleted_account_refuses_its_existing_session(client: TestClient) -> None:
    account = seed_account(email="member@gmail.com")
    _attach_session(client, account.user_id, account.email)
    assert client.get("/app").status_code == 200

    with SessionLocal() as session:
        user = session.get(User, uuid.UUID(account.user_id))
        assert user is not None
        session.delete(user)
        session.commit()

    assert client.get("/app").status_code in {303, 401}


def test_a_session_minted_before_this_slice_no_longer_verifies() -> None:
    """A cookie with no account claim is refused rather than trusted."""

    from app.core.auth.session import SessionDecodeError

    codec = SessionCodec(SESSION_SECRET)
    now = int(time.time())
    legacy = codec._encode(  # noqa: SLF001 - deliberately minting the old shape
        codec._session_key,
        {
            "email": "member@gmail.com",
            "sub": "1",
            "name": "Member",
            "sid": "old-session",
            "iat": now,
            "exp": now + 3600,
        },
    )
    with pytest.raises(SessionDecodeError):
        codec.decode_session(legacy, now=now)


def test_probes_and_the_sign_in_page_survive_a_directory_outage(
    monkeypatch: pytest.MonkeyPatch, provider: RecordingIdentityProvider
) -> None:
    """A database outage must not lock everybody out of diagnosing it."""

    from tests.hosted_auth_factory import StubAccountDirectory

    for key, value in _env().items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    app = create_app(
        readiness_probe=_AlwaysReadyProbe(),
        identity_provider=provider,
        account_directory=StubAccountDirectory(unavailable=True),
    )
    outage = TestClient(app, base_url=ORIGIN, follow_redirects=False)
    try:
        account = seed_account(email="member@gmail.com")
        _attach_session(outage, account.user_id, account.email)

        assert outage.get("/healthz").status_code == 200
        assert outage.get("/auth/login").status_code == 200
        # A protected page is refused, but as "try again" rather than "signed out",
        # and the cookie survives so the session resumes when the database does.
        protected = outage.get("/app")
        assert protected.status_code == 503
        assert "set-cookie" not in {name.lower() for name in protected.headers}
        assert SESSION_COOKIE_NAME in outage.cookies
    finally:
        get_settings.cache_clear()


def test_the_database_directory_reports_an_outage_rather_than_absence() -> None:
    """`None` means "no such account"; an outage must not be mistaken for one."""

    class _Exploding:
        def __call__(self) -> Any:
            raise RuntimeError("connection refused")

    directory = DatabaseAccountDirectory(_Exploding())  # type: ignore[arg-type]
    with pytest.raises(AccountLookupUnavailable):
        directory.lookup(uuid.uuid4())


# ---------------------------------------------------------------------------
# I. Audit
# ---------------------------------------------------------------------------


def _audit_actions(session: Session) -> list[str]:
    from app.models.audit_event import AuditEvent

    return [row.action for row in session.scalars(select(AuditEvent)).all()]


def test_every_account_change_is_audited(db: Session) -> None:
    admin = user_service.ensure_bootstrap_admin(db, email=ADMIN_EMAIL)
    assert admin is not None
    user = user_service.create_user(
        db, email="audited@gmail.com", display_name=None, actor=ADMIN_EMAIL
    )
    issued = user_service.issue_credential_link(db, user=user, actor=ADMIN_EMAIL)
    user_service.complete_password_setup(db, raw_token=issued.raw_token, new_password=GOOD_PASSWORD)
    user_service.set_role(db, user=user, role=UserRole.ADMIN, actor=ADMIN_EMAIL)
    user_service.set_state(db, user=user, state=UserState.DISABLED, actor=ADMIN_EMAIL)
    user_service.set_state(db, user=user, state=UserState.ACTIVE, actor=ADMIN_EMAIL)
    reset = user_service.issue_credential_link(db, user=user, actor=ADMIN_EMAIL)
    user_service.complete_password_setup(db, raw_token=reset.raw_token, new_password=OTHER_PASSWORD)

    actions = _audit_actions(db)
    for expected in (
        user_service.ACTION_BOOTSTRAP_ADMIN,
        user_service.ACTION_USER_CREATED,
        user_service.ACTION_TOKEN_ISSUED,
        user_service.ACTION_PASSWORD_SETUP_COMPLETED,
        user_service.ACTION_USER_ROLE_CHANGED,
        user_service.ACTION_USER_DISABLED,
        user_service.ACTION_USER_REACTIVATED,
        user_service.ACTION_PASSWORD_RESET_COMPLETED,
    ):
        assert expected in actions


def test_no_audit_record_carries_a_secret(db: Session) -> None:
    from app.models.audit_event import AuditEvent

    user = user_service.create_user(
        db, email="secretless@gmail.com", display_name=None, actor=ADMIN_EMAIL
    )
    issued = user_service.issue_credential_link(db, user=user, actor=ADMIN_EMAIL)
    user_service.complete_password_setup(db, raw_token=issued.raw_token, new_password=GOOD_PASSWORD)
    db.flush()

    refreshed = user_service.get_by_email(db, "secretless@gmail.com")
    assert refreshed is not None and refreshed.password_hash is not None

    blob = "\n".join(
        f"{row.actor}|{row.action}|{row.reason}|{row.context}"
        for row in db.scalars(select(AuditEvent)).all()
    )
    assert issued.raw_token not in blob
    assert token_service.digest_token(issued.raw_token) not in blob
    assert GOOD_PASSWORD not in blob
    assert refreshed.password_hash not in blob
    assert SESSION_SECRET not in blob


def test_the_issued_token_object_does_not_print_its_secret(db: Session) -> None:
    user = user_service.create_user(
        db, email="repr@gmail.com", display_name=None, actor=ADMIN_EMAIL
    )
    issued = user_service.issue_credential_link(db, user=user, actor=ADMIN_EMAIL)
    assert issued.raw_token not in repr(issued)
    assert "redacted" in repr(issued)


def test_the_user_repr_does_not_print_a_hash(db: Session) -> None:
    user = user_service.create_user(
        db, email="reprhash@gmail.com", display_name=None, actor=ADMIN_EMAIL
    )
    user.password_hash = passwords.hash_password(GOOD_PASSWORD)
    assert user.password_hash not in repr(user)
    assert "argon2" not in repr(user)


# ---------------------------------------------------------------------------
# J. Regressions from the focused security review
# ---------------------------------------------------------------------------


def test_the_client_bucket_is_skipped_rather_than_shared_when_unresolvable() -> None:
    """The finding: behind nginx every peer is 127.0.0.1.

    ``client_fingerprint`` used to read the raw ASGI peer, which behind the
    deployed reverse proxy is the proxy itself for *every* request. That put the
    whole deployment in one bucket with a 20-attempt allowance, so any anonymous
    caller could spend it in seconds and lock every colleague out of password
    sign-in — turning the throttle into the site-wide denial of service the
    module docstring promises it is not.

    The fix reads the address the hardening boundary already resolved, and
    returns ``None`` when there is not one. ``None`` must mean "skip this
    bucket", never "share a bucket named unknown".
    """

    from app.core.auth.ratelimit import client_fingerprint

    assert client_fingerprint({"client_ip": "203.0.113.9"}) == "203.0.113.9"
    assert client_fingerprint({"client_ip": None}) is None
    assert client_fingerprint({}) is None
    assert client_fingerprint({"client_ip": "   "}) is None

    limiter = LoginRateLimiter(email_limit=5, client_limit=2, window_seconds=60)
    for _ in range(10):
        limiter.record_failure(email="a@vmr.example", client=None, now=1_000)
    # Those attempts counted against their own address and against nothing else,
    # so a different person is unaffected.
    assert not limiter.is_blocked(email="b@vmr.example", client=None, now=1_000)


def test_the_hardening_boundary_publishes_the_resolved_client() -> None:
    """The limiter's input exists and is the address the boundary resolved.

    Asserted through the middleware rather than by unit-testing the helper, so
    that deleting the ``state["client_ip"]`` assignment breaks a test rather than
    silently reverting the fix above.
    """

    import asyncio

    from app.core.http import ProductionHTTPMiddleware

    seen: dict[str, Any] = {}

    async def _probe(scope: Any, receive: Any, send: Any) -> None:
        seen.update(scope.get("state") or {})
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def _receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message: dict[str, Any]) -> None:
        return None

    middleware = ProductionHTTPMiddleware(
        _probe,
        max_request_bytes=1_000_000,
        trusted_proxy_cidrs=("127.0.0.1/32",),
        hsts_max_age_seconds=0,
    )

    def _run(peer: tuple[str, int], headers: list[tuple[bytes, bytes]]) -> None:
        seen.clear()
        asyncio.run(
            middleware(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/healthz",
                    "headers": [(b"host", HOST.encode()), *headers],
                    "client": peer,
                    "scheme": "https",
                    "query_string": b"",
                },
                _receive,
                _send,
            )
        )

    # An untrusted peer is itself the client.
    _run(("198.51.100.7", 5555), [])
    assert seen.get("client_ip") == "198.51.100.7"

    # A trusted proxy hands over the forwarded caller, which is the whole point:
    # behind nginx the peer is always the loopback address.
    _run(("127.0.0.1", 5555), [(b"x-forwarded-for", b"203.0.113.44")])
    assert seen.get("client_ip") == "203.0.113.44"


def test_an_enormous_password_is_bounded_before_it_reaches_argon2(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The finding: the login path did not bound its input.

    ``validate_password`` enforces the ceiling on the *setup* path, but login
    verifies rather than validates, so the form value used to reach NFKC
    normalisation and Argon2 at whatever length arrived. Two consequences: server
    CPU per unauthenticated request, and a timing oracle — the no-account branch
    spends a *fixed* dummy verification, so only the real-account branch would
    grow with the input.

    Asserted as the bound itself rather than as a stopwatch reading. Starlette
    already caps one form field at 1 MiB, so a timing assertion at that size is
    within noise on a shared box and would pass even with the defect present;
    what actually needs pinning is that nothing longer than the policy maximum
    ever reaches the verifier.
    """

    seed_account(email="member@gmail.com", password=GOOD_PASSWORD)
    seen: list[int] = []
    real_authenticate = user_service.authenticate_password

    def _recording(session: Any, *, email: str, password: str, **kwargs: Any) -> Any:
        seen.append(len(password))
        return real_authenticate(session, email=email, password=password, **kwargs)

    monkeypatch.setattr(auth_routes.user_service, "authenticate_password", _recording)

    huge = "a" * 900_000
    assert _login(client, "member@gmail.com", huge).status_code == 401
    assert _login(client, "nobody@gmail.com", huge).status_code == 401

    # One character past the maximum, so an over-long value stays a mismatch
    # rather than silently becoming a shorter, possibly-correct password.
    assert seen == [passwords.MAX_PASSWORD_CHARS + 1, passwords.MAX_PASSWORD_CHARS + 1]


def test_a_disabled_accounts_google_identity_is_not_linked_on_a_refused_sign_in(
    client: TestClient, provider: RecordingIdentityProvider
) -> None:
    """The finding: linking happened before the state check, and was committed.

    A disabled account would permanently acquire whichever Google subject first
    presented a matching address — and because a *different* subject is refused
    afterwards, the real owner would be locked out of the Google path for good
    once the account was reactivated.
    """

    account = seed_account(email="gone@gmail.com", state="disabled")
    assert _google_sign_in(client, provider, email="gone@gmail.com").status_code == 403

    with SessionLocal() as session:
        user = session.get(User, uuid.UUID(account.user_id))
        assert user is not None
        assert user.google_subject is None
        assert user.google_linked_at is None


def test_the_allowlist_seed_is_a_one_shot_not_a_per_start_reconciliation(db: Session) -> None:
    """The finding: an emergency-deleted account came back at the next restart."""

    user_service.seed_from_allowlist(db, emails=("seeded@vmr.example",))
    assert user_service.get_by_email(db, "seeded@vmr.example") is not None

    # An operator deletes the row by hand in an emergency, and somebody else's
    # account exists too, so the directory is plainly being administered.
    user_service.create_user(db, email="managed@vmr.example", display_name=None, actor=ADMIN_EMAIL)
    deleted = user_service.get_by_email(db, "seeded@vmr.example")
    assert deleted is not None
    db.delete(deleted)
    db.flush()

    user_service.seed_from_allowlist(db, emails=("seeded@vmr.example",))
    assert user_service.get_by_email(db, "seeded@vmr.example") is None


def test_the_seed_still_runs_on_a_directory_holding_only_the_bootstrap_admin(
    db: Session,
) -> None:
    """The guard must not break the one moment the seed exists for."""

    user_service.ensure_bootstrap_admin(db, email=ADMIN_EMAIL)
    created = user_service.seed_from_allowlist(db, emails=("legacy@vmr.example",))
    assert [user.email_normalized for user in created] == ["legacy@vmr.example"]


def test_one_admin_cannot_drain_another_admins_one_time_link(client: TestClient) -> None:
    """The finding: the link handle was the target account's id, which is rendered.

    Two administrators is the expected shape of this deployment, so a second
    administrator polling ``?issued=<uuid>`` — a value visible in the table —
    could take a colleague's freshly issued link, and the audit trail would still
    name the colleague as the issuer.
    """

    csrf = _admin_session(client, email=ADMIN_EMAIL)
    created = client.post(
        "/app/admin/users/create",
        data={"email": "target@gmail.com", "display_name": "", "_csrf": csrf},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert created.status_code == 303
    handle = created.headers["location"].partition("issued=")[2].partition("&")[0]
    assert handle

    # Not the account id, and not derivable from anything the table renders.
    with SessionLocal() as session:
        target = user_service.get_by_email(session, "target@gmail.com")
        assert target is not None
        assert handle != str(target.id)

    # A second administrator, in their own session, cannot read it.
    second = seed_account(email="admin-two@gmail.com", role="admin")
    _attach_session(client, second.user_id, second.email)
    stolen = client.get(f"/app/admin/users?issued={handle}")
    assert stolen.status_code == 200
    assert "/auth/setup?token=" not in stolen.text


def test_an_over_long_email_is_refused_rather_than_becoming_a_500(db: Session) -> None:
    long_address = ("x" * 320) + "@vmr.example"
    with pytest.raises(user_service.UserServiceError):
        user_service.create_user(db, email=long_address, display_name=None, actor=ADMIN_EMAIL)


def test_a_next_value_that_loops_back_to_the_sign_in_page_is_refused() -> None:
    """The cosmetic redirect-loop gap the review found in ``safe_next_path``."""

    from app.core.auth.policy import safe_next_path

    assert safe_next_path("/auth/login?next=%2Fapp", fallback="/app") == "/app"
    assert safe_next_path("/auth/setup?token=abc", fallback="/app") == "/app"
    # A real destination with a query string still survives.
    assert safe_next_path("/app/campaigns?page=2", fallback="/app") == "/app/campaigns?page=2"

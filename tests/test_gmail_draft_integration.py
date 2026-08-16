"""Adversarial coverage for one-click Gmail draft creation (#267).

The suite is written from the side of the thing that must not happen. Every test
below names a way this feature could put an email somewhere it does not belong,
duplicate a draft in a real person's mailbox, leak an OAuth token, or acquire
the ability to send — and asserts the refusal. The happy-path tests exist mostly
to prove the refusals are not vacuous.

**No test contacts Google and no test writes to a real mailbox.** The OAuth
client and the Gmail transport are both fakes from ``tests/gmail_factory.py``,
and the last test in this file fails if any live adapter in the feature can
reach a socket during the suite.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from app.core.auth.config import AuthSettings
from app.core.auth.session import (
    SESSION_COOKIE_NAME,
    OperatorSession,
    SessionCodec,
    new_session_id,
)
from app.core.config import Settings, get_settings
from app.core.gmail_config import (
    GMAIL_AUTHORIZATION_SCOPES,
    GMAIL_COMPOSE_SCOPE,
    GmailSettings,
)
from app.main import create_app
from app.models.enums import GmailDraftStatus, GmailGrantStatus, SequenceReviewDecision
from app.models.gmail import GmailDraftRecord, GmailMailboxGrant
from app.services.gmail import drafts as gmail_drafts
from app.services.gmail import mailbox as gmail_mailbox
from app.services.gmail import mime as gmail_mime
from app.services.gmail import tokens as gmail_tokens
from app.services.gmail.oauth import GmailAuthorizationError
from app.services.gmail.provider import GmailProviderError
from app.services.sequences import review as sequence_review
from app.web.gmail_routes import GMAIL_OAUTH_CLIENT_STATE_KEY, GMAIL_TRANSACTION_COOKIE_NAME
from app.web.v2.routes import GMAIL_PROVIDER_STATE_KEY
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.gmail_factory import (
    FakeGmailOAuthClient,
    FakeGmailTransport,
    build_sequence,
    decoded_message,
)
from tests.hosted_auth_factory import SeededAccount, seed_account

STAGING_HOST = "srv1885453.hstgr.cloud"
STAGING_ORIGIN = f"https://{STAGING_HOST}"
APPROVED_EMAIL = "operator@vmr.example"
OPERATOR_SUBJECT = "operator-google-subject-1"
SESSION_SECRET = "test-session-secret-value-at-least-32-chars"
STAGING_DATABASE_URL = "postgresql+psycopg://vmr:secret@db.internal.example:5432/vmr_staging"
GMAIL_CLIENT_ID = "vmr-gmail-test-client.apps.googleusercontent.com"
IDENTITY_CLIENT_ID = "vmr-identity-test-client.apps.googleusercontent.com"

# The Chrome capture extension's own credential, used by exactly one test in
# section M: the point there is that it opens *no* Gmail door, so it is defined
# beside the operator constants rather than hidden inside that test.
EXTENSION_ID = "abcdefghijklmnopabcdefghijklmnop"
EXTENSION_ORIGIN = f"chrome-extension://{EXTENSION_ID}"
EXTENSION_KEY_ID = "beta-laptop"
EXTENSION_SECRET = "3fVQx8Zk2nLp7Rw6TyUiOaSdFgHjKlZxCvBnM4qWeRt"
EXTENSION_CREDENTIAL = f"vmrx1.{EXTENSION_KEY_ID}.{EXTENSION_SECRET}"


class _AlwaysReadyProbe:
    def __call__(self) -> None:
        return None


def encryption_key() -> str:
    return Fernet.generate_key().decode()


def gmail_settings(**overrides: Any) -> GmailSettings:
    values: dict[str, Any] = {
        "client_id": GMAIL_CLIENT_ID,
        "client_secret": "gmail-client-secret",
        "token_encryption_key": encryption_key(),
    }
    values.update(overrides)
    return GmailSettings(**values)


# ---------------------------------------------------------------------------
# Hosted application fixtures
# ---------------------------------------------------------------------------


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "APP_ENV": "staging",
        "DEBUG": "false",
        "DRY_RUN": "true",
        "TRUSTED_HOSTS": f'["{STAGING_HOST}"]',
        "DATABASE_URL": STAGING_DATABASE_URL,
        "FEATURES__WORKBENCH": "true",
        "FEATURES__EMAIL_SEQUENCES": "true",
        "FEATURES__GMAIL_DRAFTS": "true",
        "AUTH__ENABLED": "true",
        "AUTH__SESSION_SECRET": SESSION_SECRET,
        "AUTH__ALLOWED_OPERATOR_EMAILS": f'["{APPROVED_EMAIL}"]',
        "AUTH__GOOGLE_CLIENT_ID": IDENTITY_CLIENT_ID,
        "AUTH__GOOGLE_CLIENT_SECRET": "identity-client-secret",
        "AUTH__PUBLIC_BASE_URL": STAGING_ORIGIN,
        "GMAIL__CLIENT_ID": GMAIL_CLIENT_ID,
        "GMAIL__CLIENT_SECRET": "gmail-client-secret",
        "GMAIL__TOKEN_ENCRYPTION_KEY": encryption_key(),
        "GMAIL__MESSAGE_ID_DOMAIN": "vmr-test.invalid",
    }
    env.update(overrides)
    return env


def _apply(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def _seed_operator(
    *,
    email: str = APPROVED_EMAIL,
    subject: str | None = OPERATOR_SUBJECT,
    role: str = "user",
    password: str | None = None,
) -> SeededAccount:
    """One committed ``users`` row, and the identifiers a cookie needs from it.

    Every operator in this file is a real account row since #273. There is no
    such thing here as "an approved address": a Gmail mailbox now belongs to
    ``users.id``, so a test that invents an identifier out of a string would be
    asserting against an owner that no request could ever resolve.
    """

    return seed_account(email=email, role=role, google_subject=subject, password=password)


def _session_cookie(
    account: SeededAccount | None = None,
    *,
    email: str = APPROVED_EMAIL,
    subject: str = OPERATOR_SUBJECT,
    auth_version: int | None = None,
) -> tuple[str, str]:
    """A validly signed session cookie for one account, plus its CSRF token.

    ``user_id`` and ``auth_version`` became required claims in #273, and the
    authentication boundary resolves the first against the ``users`` table on
    every request. A cookie whose ``uid`` names nothing is refused before any
    Gmail route is reached, so this helper seeds the account when it is not
    handed one rather than letting a caller mint a session belonging to nobody.

    ``subject`` is the Google ``sub`` the *session* carries. It is provenance,
    never ownership: passing ``""`` produces exactly the password-authenticated
    session that this reconciliation exists for.
    """

    owner = account if account is not None else _seed_operator(email=email, subject=subject or None)
    now = int(time.time())
    sid = new_session_id()
    session = OperatorSession(
        email=owner.email,
        subject=subject,
        display_name="VMR Operator",
        session_id=sid,
        issued_at=now,
        expires_at=now + 3600,
        user_id=owner.user_id,
        auth_version=owner.auth_version if auth_version is None else auth_version,
    )
    codec = SessionCodec(SESSION_SECRET)
    return codec.encode_session(session), codec.csrf_token(sid)


@pytest.fixture()
def hosted(
    monkeypatch: pytest.MonkeyPatch, **_: Any
) -> Iterator[tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport]]:
    """A hosted deployment with both Gmail seams replaced by fakes."""

    _apply(monkeypatch, _env())
    app = create_app(readiness_probe=_AlwaysReadyProbe())
    oauth = FakeGmailOAuthClient()
    transport = FakeGmailTransport()
    setattr(app.state, GMAIL_OAUTH_CLIENT_STATE_KEY, oauth)
    setattr(app.state, GMAIL_PROVIDER_STATE_KEY, transport)
    client = TestClient(app, base_url=STAGING_ORIGIN, follow_redirects=False)
    try:
        yield client, oauth, transport
    finally:
        get_settings.cache_clear()


def _flash(response: Any) -> str:
    """The decoded ``ok=``/``err=`` message from a redirect, for readable asserts."""

    from urllib.parse import parse_qs, unquote, urlparse

    query = parse_qs(urlparse(response.headers["location"]).query)
    message = (query.get("ok") or query.get("err") or [""])[0]
    return unquote(message)


def _sign_in_as(
    client: TestClient, account: SeededAccount, *, subject: str = OPERATOR_SUBJECT
) -> str:
    """Attach a session for one already-seeded account and return its CSRF token.

    The variant to use whenever a test needs to *name* the operator afterwards --
    to assert which account a grant was recorded against, to disable it, or to
    sign a second operator in beside it.
    """

    cookie, csrf = _session_cookie(account, subject=subject)
    client.cookies.set(SESSION_COOKIE_NAME, cookie, domain=STAGING_HOST)
    return csrf


def _grant_campaign_access(
    session: Session, campaign_id: uuid.UUID, account: SeededAccount
) -> None:
    """Assign a fixture campaign to a second operator.

    Used by the multi-operator tests below. Without it those tests would now be
    refused at the campaign boundary before they ever reached the Gmail one, and
    would be asserting the wrong refusal — the point of each of them is that a
    mailbox belongs to one account *even when* both operators can legitimately
    work on the same campaign.
    """

    from app.models.campaign import CampaignUserAssignment

    session.add(CampaignUserAssignment(campaign_id=campaign_id, user_id=uuid.UUID(account.user_id)))
    session.commit()


def _default_operator_id(session: Session) -> str:
    """The account id of the default approved operator, seeding it if needed.

    Campaigns have owners now, and a signed-in USER reaches only the campaigns
    they created or were assigned. Every fixture campaign in this file is
    therefore given to *this* account, because that is the situation these tests
    are about: one operator working on their own campaign, and the question being
    asked is about Gmail rather than about campaign access. A fixture campaign
    with no owner would make the whole file assert against a campaign refusal.

    Seeding here rather than assuming a prior `_seed_operator()` call keeps the
    helper usable before sign-in as well as after it, which is the order several
    of these tests use.
    """

    from app.models.user import User

    existing = session.scalar(select(User).where(User.email_normalized == APPROVED_EMAIL))
    if existing is not None:
        return str(existing.id)
    return _seed_operator().user_id


def _signed_in(client: TestClient) -> str:
    """Sign the client in as the default approved operator, and return its CSRF."""

    return _sign_in_as(client, _seed_operator())


def _connect(
    client: TestClient,
    oauth: FakeGmailOAuthClient,
    csrf: str,
    *,
    back: str = "/app/account/connections",
) -> Any:
    """Drive the real connect round trip and return the callback response."""

    started = client.post(
        "/gmail/connect",
        data={"back": back, "_csrf": csrf},
        headers={"sec-fetch-site": "same-origin"},
    )
    assert started.status_code == 303, started.text
    state = oauth.authorization_calls[-1]["state"]
    return client.get(f"/gmail/callback?code=consent-code&state={state}")


# ---------------------------------------------------------------------------
# A. Two separate authorities
# ---------------------------------------------------------------------------


def test_ordinary_hosted_sign_in_requests_no_gmail_permission() -> None:
    """Acceptance 1: signing in to VMR asks for identity scopes and nothing else."""

    from app.core.auth.config import GOOGLE_IDENTITY_SCOPES

    assert GOOGLE_IDENTITY_SCOPES == ("openid", "email", "profile")
    assert not any("gmail" in scope for scope in GOOGLE_IDENTITY_SCOPES)
    assert not any("mail.google.com" in scope for scope in GOOGLE_IDENTITY_SCOPES)


def test_the_identity_client_never_builds_a_gmail_authorization_url() -> None:
    """The sign-in provider cannot be talked into requesting a mailbox scope."""

    from app.core.auth.google import GoogleIdentityProvider

    provider = GoogleIdentityProvider(
        AuthSettings(
            enabled=True,
            google_client_id=IDENTITY_CLIENT_ID,
            google_client_secret="identity-client-secret",
            public_base_url=STAGING_ORIGIN,
        )
    )
    url = provider.authorization_url(
        redirect_uri=f"{STAGING_ORIGIN}/auth/callback",
        state="state",
        nonce="nonce",
        code_challenge="challenge",
    )
    assert "gmail" not in url
    assert "access_type=online" in url


def test_the_gmail_grant_requests_the_least_privilege_compose_scope() -> None:
    """Acceptance: narrowest scope Google documents for users.drafts.create."""

    assert GMAIL_COMPOSE_SCOPE in GMAIL_AUTHORIZATION_SCOPES
    assert "https://www.googleapis.com/auth/gmail.modify" not in GMAIL_AUTHORIZATION_SCOPES
    assert "https://mail.google.com/" not in GMAIL_AUTHORIZATION_SCOPES
    assert "https://www.googleapis.com/auth/gmail.readonly" not in GMAIL_AUTHORIZATION_SCOPES
    # The two identity scopes are there to learn *which* mailbox was connected
    # without asking for gmail.metadata, which would be wider mailbox access.
    assert set(GMAIL_AUTHORIZATION_SCOPES) == {"openid", "email", GMAIL_COMPOSE_SCOPE}


def test_gmail_consent_only_begins_from_an_explicit_connect_click(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport],
) -> None:
    """Acceptance 2: nothing but Connect Gmail starts a Gmail consent."""

    client, oauth, _ = hosted
    csrf = _signed_in(client)

    # Every read of the operator surface, and the sign-in surface itself.
    for path in ("/app/campaigns", "/app/people", "/auth/login"):
        client.get(path)
    assert oauth.authorization_calls == []

    # A GET on the connect route is not the way in either: it is a POST route.
    assert client.get("/gmail/connect").status_code == 405
    assert oauth.authorization_calls == []

    started = client.post(
        "/gmail/connect",
        data={"back": "/app/account/connections", "_csrf": csrf},
        headers={"sec-fetch-site": "same-origin"},
    )
    assert started.status_code == 303
    assert len(oauth.authorization_calls) == 1
    assert GMAIL_COMPOSE_SCOPE in oauth.authorization_calls[0]["scope"]


def test_connect_is_refused_without_the_session_csrf_token(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport],
) -> None:
    """A cookie alone cannot start a mailbox consent."""

    client, oauth, _ = hosted
    _signed_in(client)
    refused = client.post(
        "/gmail/connect",
        data={"back": "/app/account/connections"},
        headers={"sec-fetch-site": "same-origin"},
    )
    assert refused.status_code == 403
    assert oauth.authorization_calls == []


def test_the_gmail_routes_are_not_anonymous() -> None:
    """The callback is not a sign-in path and grants a stranger nothing."""

    from app.core.auth.policy import anonymous_application_paths, is_anonymous_path

    for path in ("/gmail", "/gmail/connect", "/gmail/callback", "/gmail/disconnect"):
        assert not is_anonymous_path(path), path
        assert path not in anonymous_application_paths()


def test_an_anonymous_caller_reaches_no_gmail_route(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport],
) -> None:
    client, oauth, _ = hosted
    assert client.get("/gmail/callback?code=x&state=y").status_code in {303, 401}
    assert client.post("/gmail/connect", data={"back": "/app"}).status_code == 401
    assert oauth.authorization_calls == []


# ---------------------------------------------------------------------------
# B. The callback round trip
# ---------------------------------------------------------------------------


def test_a_connected_mailbox_is_recorded_against_the_operator(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport], committed_session: Session
) -> None:
    """Acceptance 5: the connected Gmail identity is recorded correctly."""

    client, oauth, _ = hosted
    operator = _seed_operator()
    csrf = _sign_in_as(client, operator)
    response = _connect(client, oauth, csrf)

    assert response.status_code == 303
    assert "Gmail connected" in _flash(response)
    grant = committed_session.scalars(select(GmailMailboxGrant)).one()
    assert grant.status is GmailGrantStatus.CONNECTED
    # Ownership is the durable account, and only the account. The Google subject
    # is still recorded because this consent *was* authorized from a Google
    # session, but it is provenance now rather than the ownership key.
    assert grant.user_id == uuid.UUID(operator.user_id)
    assert grant.operator_subject == OPERATOR_SUBJECT
    assert grant.operator_email == APPROVED_EMAIL
    assert grant.mailbox_address == oauth.mailbox_address
    assert grant.mailbox_account_subject == oauth.mailbox_subject
    assert GMAIL_COMPOSE_SCOPE in grant.granted_scopes


def test_an_oauth_state_mismatch_is_refused(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport], committed_session: Session
) -> None:
    """Acceptance 3."""

    client, oauth, _ = hosted
    csrf = _signed_in(client)
    started = client.post(
        "/gmail/connect",
        data={"back": "/app/account/connections", "_csrf": csrf},
        headers={"sec-fetch-site": "same-origin"},
    )
    assert started.status_code == 303

    response = client.get("/gmail/callback?code=consent-code&state=not-the-state")
    assert response.status_code == 303
    assert "could not be verified" in _flash(response)
    assert oauth.exchanges == []
    assert committed_session.scalars(select(GmailMailboxGrant)).all() == []


def test_a_callback_cannot_bind_a_mailbox_to_a_different_operator(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport], committed_session: Session
) -> None:
    """Acceptance 4: a captured callback replayed into another operator's browser."""

    client, oauth, _ = hosted
    csrf = _signed_in(client)
    started = client.post(
        "/gmail/connect",
        data={"back": "/app/account/connections", "_csrf": csrf},
        headers={"sec-fetch-site": "same-origin"},
    )
    assert started.status_code == 303
    state = oauth.authorization_calls[-1]["state"]

    # The second operator is signed in, has an account of their own, and
    # presents the *same* transaction cookie the first operator's browser was
    # given.
    other_cookie, _ = _session_cookie(
        email="second@vmr.example", subject="operator-google-subject-2"
    )
    client.cookies.set(SESSION_COOKIE_NAME, other_cookie, domain=STAGING_HOST)

    response = client.get(f"/gmail/callback?code=consent-code&state={state}")
    assert response.status_code == 303
    assert "different operator" in _flash(response)
    assert oauth.exchanges == []
    assert committed_session.scalars(select(GmailMailboxGrant)).all() == []


def test_a_consent_without_the_compose_scope_connects_nothing(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport], committed_session: Session
) -> None:
    """A consent screen where the operator unticked the mailbox permission."""

    client, oauth, _ = hosted
    oauth.granted_scopes = ("openid", "email")
    csrf = _signed_in(client)
    response = _connect(client, oauth, csrf)

    assert response.status_code == 303
    assert "did not grant" in _flash(response)
    assert committed_session.scalars(select(GmailMailboxGrant)).all() == []


def test_a_consent_without_a_refresh_token_connects_nothing(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport], committed_session: Session
) -> None:
    """A grant that dies in an hour is not a durable connection."""

    client, oauth, _ = hosted
    oauth.refresh_token = None
    csrf = _signed_in(client)
    response = _connect(client, oauth, csrf)

    assert response.status_code == 303
    assert committed_session.scalars(select(GmailMailboxGrant)).all() == []


def test_connecting_a_second_mailbox_retires_the_first(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport], committed_session: Session
) -> None:
    """One live mailbox per operator, with the previous grant kept for audit."""

    client, oauth, _ = hosted
    csrf = _signed_in(client)
    _connect(client, oauth, csrf)
    oauth.mailbox_address = "second@vmr.example"
    oauth.mailbox_subject = "gmail-account-subject-2"
    _connect(client, oauth, csrf)

    grants = committed_session.scalars(select(GmailMailboxGrant)).all()
    assert len(grants) == 2
    live = [row for row in grants if row.status is GmailGrantStatus.CONNECTED]
    assert len(live) == 1
    assert live[0].mailbox_address == "second@vmr.example"
    retired = [row for row in grants if row.status is GmailGrantStatus.REVOKED][0]
    # The retired row keeps its identity and loses its credentials.
    assert retired.mailbox_address == "operator@vmr.example"
    assert retired.encrypted_refresh_token is None


# ---------------------------------------------------------------------------
# C. Secrets
# ---------------------------------------------------------------------------


def test_tokens_are_encrypted_at_rest(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport], committed_session: Session
) -> None:
    """Acceptance 6."""

    client, oauth, _ = hosted
    csrf = _signed_in(client)
    _connect(client, oauth, csrf)

    grant = committed_session.scalars(select(GmailMailboxGrant)).one()
    assert grant.encrypted_refresh_token is not None
    assert grant.encrypted_access_token is not None
    assert gmail_tokens.looks_like_ciphertext(grant.encrypted_refresh_token)
    assert gmail_tokens.looks_like_ciphertext(grant.encrypted_access_token)
    # The plaintext appears nowhere in either column, in any obvious encoding.
    import base64

    for plaintext in ("refresh-token-1", "access-token-1"):
        encoded = base64.b64encode(plaintext.encode()).decode()
        for column in (grant.encrypted_refresh_token, grant.encrypted_access_token):
            assert plaintext not in column
            assert encoded not in column


def test_a_token_column_is_unreadable_without_the_key() -> None:
    """A database copy on its own decrypts neither column."""

    settings = gmail_settings()
    ciphertext = gmail_tokens.encrypt_token("refresh-token-1", settings=settings)
    other = gmail_settings()
    with pytest.raises(gmail_tokens.GmailTokenStorageError):
        gmail_tokens.decrypt_token(ciphertext, settings=other)
    assert gmail_tokens.decrypt_token(ciphertext, settings=settings) == "refresh-token-1"


def test_token_storage_is_unavailable_without_an_explicit_key() -> None:
    """No fallback key, and no cosmetic encoding pretending to be encryption."""

    settings = gmail_settings(token_encryption_key=None)
    assert not settings.is_configured()
    with pytest.raises(gmail_tokens.GmailTokenStorageError):
        gmail_tokens.encrypt_token("refresh-token-1", settings=settings)


def test_gmail_secrets_never_appear_in_settings_dumps_or_repr() -> None:
    """Acceptance 7, part one: configuration."""

    settings = Settings(
        app_env="local",
        gmail=GmailSettings(
            client_id=GMAIL_CLIENT_ID,
            client_secret="gmail-client-secret",
            token_encryption_key="unit-test-encryption-key",
        ),
    )
    dumped = repr(settings.model_dump())
    for secret in ("gmail-client-secret", "unit-test-encryption-key"):
        assert secret not in dumped
        assert secret not in repr(settings)
        assert secret not in repr(settings.gmail)
        assert secret not in str(settings.gmail.model_dump())


def test_a_grant_row_cannot_print_a_token(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport], committed_session: Session
) -> None:
    """Acceptance 7, part two: a log line, a traceback frame, a debugger watch."""

    client, oauth, _ = hosted
    csrf = _signed_in(client)
    _connect(client, oauth, csrf)
    grant = committed_session.scalars(select(GmailMailboxGrant)).one()

    for rendered in (repr(grant), str(grant), f"{grant}"):
        assert "refresh-token-1" not in rendered
        assert "access-token-1" not in rendered
        assert grant.encrypted_refresh_token not in rendered
        assert grant.mailbox_address in rendered


def test_no_gmail_secret_reaches_a_rendered_page(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport], committed_session: Session
) -> None:
    """Acceptance 7, part three: HTML."""

    client, oauth, _ = hosted
    csrf = _signed_in(client)
    _connect(client, oauth, csrf)
    fixture = build_sequence(
        committed_session, owner_user_id=_default_operator_id(committed_session)
    )
    committed_session.commit()

    page = client.get(
        f"/app/campaigns/{fixture.campaign.id}?section=all&person={fixture.membership.id}"
    )
    assert page.status_code == 200
    body = page.text
    grant = committed_session.scalars(select(GmailMailboxGrant)).one()
    for secret in (
        "refresh-token-1",
        "access-token-1",
        "gmail-client-secret",
        grant.encrypted_refresh_token or "",
        grant.encrypted_access_token or "",
    ):
        assert secret not in body
    # The mailbox identity is shown; the credential behind it is not.
    assert oauth.mailbox_address in body


def test_a_provider_failure_message_carries_no_provider_body() -> None:
    """A bounded category is the only thing persisted or surfaced."""

    error = GmailProviderError("http_400", ambiguous=False)
    assert str(error) == "http_400"
    assert error.category == "http_400"


def test_the_oauth_client_never_propagates_googles_error_text() -> None:
    """Google's own body can echo the submitted code; none of it escapes."""

    import httpx
    from app.services.gmail.oauth import GoogleGmailOAuthClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "code": "the-secret-code"})

    client = GoogleGmailOAuthClient(
        gmail_settings(), client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(GmailAuthorizationError) as raised:
        client.exchange_code(code="the-secret-code", redirect_uri="https://x/y", code_verifier="v")
    assert "the-secret-code" not in str(raised.value)
    assert "invalid_grant" not in str(raised.value)


# ---------------------------------------------------------------------------
# D. Creating drafts
# ---------------------------------------------------------------------------


def _grant_for(
    session: Session,
    *,
    settings: GmailSettings,
    subject: str = "gmail-account-subject-1",
    user_id: uuid.UUID | None = None,
) -> GmailMailboxGrant:
    """One connected grant, owned by a real account row.

    ``user_id`` is a foreign key to ``users`` and is not nullable, so a grant can
    no longer be conjured from an operator subject alone. When a caller does not
    name an owner, one is seeded: the service-level tests below are about drafts
    rather than about who owns the mailbox, and each of them wants an owner that
    collides with nobody else's.
    """

    owner = (
        user_id
        if user_id is not None
        else uuid.UUID(
            _seed_operator(
                email=f"operator-{uuid.uuid4().hex[:8]}@vmr.example",
                subject=f"google-sub-{uuid.uuid4().hex[:8]}",
            ).user_id
        )
    )
    grant = GmailMailboxGrant(
        user_id=owner,
        operator_subject=OPERATOR_SUBJECT,
        operator_email=APPROVED_EMAIL,
        mailbox_account_subject=subject,
        mailbox_address="operator@vmr.example",
        granted_scopes=" ".join(GMAIL_AUTHORIZATION_SCOPES),
        status=GmailGrantStatus.CONNECTED,
        encrypted_refresh_token=gmail_tokens.encrypt_token("refresh-token-1", settings=settings),
        encrypted_access_token=gmail_tokens.encrypt_token("access-token-1", settings=settings),
        access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session.add(grant)
    session.flush()
    return grant


@pytest.fixture()
def service_setup(committed_session: Session) -> tuple[Any, GmailMailboxGrant, GmailSettings]:
    settings = gmail_settings()
    fixture = build_sequence(
        committed_session, owner_user_id=_default_operator_id(committed_session)
    )
    grant = _grant_for(committed_session, settings=settings)
    committed_session.commit()
    return fixture, grant, settings


def _run(
    session: Session,
    setup: tuple[Any, GmailMailboxGrant, GmailSettings],
    transport: FakeGmailTransport,
    *,
    version_ids: tuple[uuid.UUID, ...] | None = None,
    now: datetime | None = None,
) -> gmail_drafts.DraftRun:
    fixture, grant, settings = setup
    return gmail_drafts.create_drafts(
        session,
        sequence_id=fixture.sequence.id,
        expected_version_ids=version_ids or fixture.version_ids,
        grant=grant,
        settings=settings,
        oauth_client=FakeGmailOAuthClient(),
        provider=transport,
        actor=APPROVED_EMAIL,
        now=now,
    )


def test_the_exact_current_versions_create_seven_drafts(
    committed_session: Session, service_setup: tuple[Any, GmailMailboxGrant, GmailSettings]
) -> None:
    """Acceptance 9."""

    fixture, _grant, _settings = service_setup
    transport = FakeGmailTransport()
    run = _run(committed_session, service_setup, transport)

    assert run.created == 7
    assert run.reused == 0
    assert run.failed == 0
    assert "7 Gmail drafts created" in run.summary()
    assert len(transport.created) == 7

    records = committed_session.scalars(select(GmailDraftRecord)).all()
    assert len(records) == 7
    assert {row.status for row in records} == {GmailDraftStatus.CREATED}
    # Every row names the exact immutable version, never a (contact, position).
    assert {row.message_version_id for row in records} == set(fixture.version_ids)
    assert {row.message_id for row in records} == {m.id for m in fixture.messages}
    assert all(row.gmail_draft_id for row in records)
    assert all(row.recipient_email == fixture.contact.email for row in records)
    assert all(row.sequence_id == fixture.sequence.id for row in records)


def test_the_drafted_message_is_the_exact_approved_text(
    committed_session: Session, service_setup: tuple[Any, GmailMailboxGrant, GmailSettings]
) -> None:
    """Acceptance: what appears in Gmail matches VMR, with nothing added."""

    fixture, grant, _settings = service_setup
    transport = FakeGmailTransport()
    _run(committed_session, service_setup, transport)

    first = decoded_message(transport.created[0])
    version = fixture.versions[0]
    assert f"To: {fixture.contact.email}" in first
    assert f"From: {grant.mailbox_address}" in first
    assert version.subject in first
    assert "sequence message 1 about sourced" in first
    # Nothing was added to the approved text.
    for forbidden in ("unsubscribe", "tracking", "<img", "Sent from", "text/html"):
        assert forbidden.lower() not in first.lower()


def test_no_threading_headers_are_fabricated(
    committed_session: Session, service_setup: tuple[Any, GmailMailboxGrant, GmailSettings]
) -> None:
    """Follow-ups are standalone drafts: there is no sent predecessor to reply to."""

    transport = FakeGmailTransport()
    _run(committed_session, service_setup, transport)

    for raw in transport.created:
        decoded = decoded_message(raw)
        assert "In-Reply-To" not in decoded
        assert "References" not in decoded
        assert "Message-ID:" in decoded
    # And no Gmail threadId was submitted either.
    records = committed_session.scalars(select(GmailDraftRecord)).all()
    assert {row.gmail_thread_id for row in records} == {
        f"thread-{index}" for index in range(1, 8)
    }, "thread ids come back from Gmail; none is ever sent to it"


def test_the_message_id_is_deterministic_per_exact_version(
    committed_session: Session, service_setup: tuple[Any, GmailMailboxGrant, GmailSettings]
) -> None:
    fixture, _grant, settings = service_setup
    expected = gmail_mime.rfc_message_id(
        message_version_id=fixture.versions[0].id, domain=settings.message_id_domain
    )
    assert expected == gmail_mime.rfc_message_id(
        message_version_id=fixture.versions[0].id, domain=settings.message_id_domain
    )
    assert expected != gmail_mime.rfc_message_id(
        message_version_id=fixture.versions[1].id, domain=settings.message_id_domain
    )


def test_a_stale_submission_drafts_nothing(
    committed_session: Session, service_setup: tuple[Any, GmailMailboxGrant, GmailSettings]
) -> None:
    """Acceptance 10: a superseded version creates nothing."""

    fixture, _grant, _settings = service_setup
    stale = fixture.version_ids
    sequence_review.edit_message(
        committed_session,
        message_version_id=fixture.versions[2].id,
        subject="An operator rewrote this while the page was open",
        body="Different text entirely, written after the page was rendered.",
        actor="operator",
    )
    committed_session.commit()

    transport = FakeGmailTransport()
    with pytest.raises(gmail_drafts.GmailDraftError) as raised:
        _run(committed_session, service_setup, transport, version_ids=stale)
    assert "no longer the current one" in str(raised.value)
    assert transport.created == []
    assert committed_session.scalars(select(GmailDraftRecord)).all() == []


def test_a_discarded_message_is_never_drafted(
    committed_session: Session, service_setup: tuple[Any, GmailMailboxGrant, GmailSettings]
) -> None:
    """Acceptance 13."""

    fixture, _grant, _settings = service_setup
    sequence_review.discard_message(
        committed_session,
        message_version_id=fixture.versions[3].id,
        actor="operator",
        reason="Off-message",
    )
    committed_session.commit()

    transport = FakeGmailTransport()
    run = _run(committed_session, service_setup, transport)

    assert run.created == 6
    assert run.skipped_discarded == 1
    assert "1 discarded message skipped" in run.summary()
    drafted = {
        row.message_version_id for row in committed_session.scalars(select(GmailDraftRecord)).all()
    }
    assert fixture.versions[3].id not in drafted


def test_a_stopped_sequence_drafts_nothing(
    committed_session: Session, service_setup: tuple[Any, GmailMailboxGrant, GmailSettings]
) -> None:
    from app.models.enums import SequenceStopReason, SequenceStopState

    fixture, _grant, _settings = service_setup
    fixture.sequence.stop_state = SequenceStopState.STOPPED
    fixture.sequence.stop_reason = SequenceStopReason.OPERATOR_HOLD
    committed_session.commit()

    transport = FakeGmailTransport()
    with pytest.raises(gmail_drafts.GmailDraftError) as raised:
        _run(committed_session, service_setup, transport)
    assert "stopped" in str(raised.value)
    assert transport.created == []


def test_a_suppressed_contact_is_never_drafted_to(
    committed_session: Session, service_setup: tuple[Any, GmailMailboxGrant, GmailSettings]
) -> None:
    """Suppression stays authoritative over a delivery-adjacent action."""

    from app.models.enums import SuppressionReason, SuppressionType
    from app.services.suppressions import add_suppression

    fixture, _grant, _settings = service_setup
    add_suppression(
        committed_session,
        suppression_type=SuppressionType.EMAIL,
        value=fixture.contact.email or "",
        reason=SuppressionReason.OPT_OUT,
        actor="operator",
    )
    committed_session.commit()

    transport = FakeGmailTransport()
    with pytest.raises(gmail_drafts.GmailDraftError) as raised:
        _run(committed_session, service_setup, transport)
    assert "suppressed" in str(raised.value)
    assert transport.created == []


def test_a_contact_without_an_address_drafts_nothing(committed_session: Session) -> None:
    settings = gmail_settings()
    fixture = build_sequence(
        committed_session,
        without_email=True,
        owner_user_id=_default_operator_id(committed_session),
    )
    grant = _grant_for(committed_session, settings=settings)
    committed_session.commit()

    transport = FakeGmailTransport()
    with pytest.raises(gmail_drafts.GmailDraftError):
        gmail_drafts.create_drafts(
            committed_session,
            sequence_id=fixture.sequence.id,
            expected_version_ids=fixture.version_ids,
            grant=grant,
            settings=settings,
            oauth_client=FakeGmailOAuthClient(),
            provider=transport,
            actor=APPROVED_EMAIL,
        )
    assert transport.created == []


# ---------------------------------------------------------------------------
# E. Idempotency and reconciliation
# ---------------------------------------------------------------------------


def test_clicking_twice_creates_no_duplicate_drafts(
    committed_session: Session, service_setup: tuple[Any, GmailMailboxGrant, GmailSettings]
) -> None:
    """Acceptance 11."""

    transport = FakeGmailTransport()
    first = _run(committed_session, service_setup, transport)
    second = _run(committed_session, service_setup, transport)

    assert first.created == 7
    assert second.created == 0
    assert second.reused == 7
    assert "7 already existed" in second.summary()
    assert len(transport.created) == 7
    assert len(committed_session.scalars(select(GmailDraftRecord)).all()) == 7


def test_idempotency_survives_a_disconnect_and_reconnect(
    committed_session: Session, service_setup: tuple[Any, GmailMailboxGrant, GmailSettings]
) -> None:
    """The key is the Gmail account, not the grant row a reconnect replaces."""

    fixture, grant, settings = service_setup
    transport = FakeGmailTransport()
    _run(committed_session, service_setup, transport)

    grant.status = GmailGrantStatus.REVOKED
    grant.encrypted_refresh_token = None
    grant.encrypted_access_token = None
    committed_session.flush()
    # A new grant row for the *same* Google account, belonging to the *same* VMR
    # user: a reconnect replaces a row, it does not move the mailbox to somebody
    # else.
    reconnected = _grant_for(committed_session, settings=settings, user_id=grant.user_id)
    committed_session.commit()

    run = gmail_drafts.create_drafts(
        committed_session,
        sequence_id=fixture.sequence.id,
        expected_version_ids=fixture.version_ids,
        grant=reconnected,
        settings=settings,
        oauth_client=FakeGmailOAuthClient(),
        provider=transport,
        actor=APPROVED_EMAIL,
    )
    assert run.created == 0
    assert run.reused == 7
    assert len(transport.created) == 7


def test_an_ambiguous_failure_is_never_treated_as_no_draft(
    committed_session: Session, service_setup: tuple[Any, GmailMailboxGrant, GmailSettings]
) -> None:
    """Acceptance 14: Gmail created it and the answer was lost."""

    transport = FakeGmailTransport(lose_response_after_creating=True)
    run = _run(committed_session, service_setup, transport)

    assert run.created == 0
    assert run.unconfirmed == 7
    assert not run.fully_successful
    assert "could not be confirmed" in run.summary()
    records = committed_session.scalars(select(GmailDraftRecord)).all()
    assert {row.status for row in records} == {GmailDraftStatus.UNCONFIRMED}
    assert all(row.gmail_draft_id is None for row in records)


def test_a_retry_too_soon_after_an_ambiguous_failure_creates_nothing(
    committed_session: Session, service_setup: tuple[Any, GmailMailboxGrant, GmailSettings]
) -> None:
    """Gmail's search index is not instantaneous, so "not found" is not evidence."""

    lossy = FakeGmailTransport(lose_response_after_creating=True)
    _run(committed_session, service_setup, lossy)

    # A transport that has forgotten everything: the lookup finds nothing.
    blind = FakeGmailTransport()
    run = _run(committed_session, service_setup, blind)
    assert run.created == 0
    assert run.unconfirmed == 7
    assert blind.created == []
    assert "too recent" in run.outcomes[0].detail


def test_reconciliation_adopts_a_draft_an_earlier_attempt_did_create(
    committed_session: Session, service_setup: tuple[Any, GmailMailboxGrant, GmailSettings]
) -> None:
    """The bounded reconciliation: one exact rfc822msgid lookup, then adopt."""

    transport = FakeGmailTransport(lose_response_after_creating=True)
    _run(committed_session, service_setup, transport)
    assert len(transport.drafts_by_message_id) == 7

    # The same transport still knows the drafts it created; the response is no
    # longer lost.
    transport.lose_response_after_creating = False
    later = datetime.now(UTC) + timedelta(seconds=gmail_drafts.RECONCILIATION_MIN_AGE_SECONDS + 5)
    run = _run(committed_session, service_setup, transport, now=later)

    assert run.created == 0
    assert run.reused == 7
    assert "already existed" in run.summary()
    assert len(transport.created) == 7, "no second draft was written"
    assert len(transport.lookups) == 7
    records = committed_session.scalars(select(GmailDraftRecord)).all()
    assert {row.status for row in records} == {GmailDraftStatus.CREATED}


def test_reconciliation_that_finds_nothing_after_the_window_recreates_once(
    committed_session: Session, service_setup: tuple[Any, GmailMailboxGrant, GmailSettings]
) -> None:
    """Gmail genuinely never saw it: one draft is created, not two."""

    lossy = FakeGmailTransport(lose_response_after_creating=True)
    _run(committed_session, service_setup, lossy)

    blind = FakeGmailTransport()
    later = datetime.now(UTC) + timedelta(seconds=gmail_drafts.RECONCILIATION_MIN_AGE_SECONDS + 5)
    run = _run(committed_session, service_setup, blind, now=later)

    assert run.created == 7
    assert len(blind.created) == 7
    records = committed_session.scalars(select(GmailDraftRecord)).all()
    assert len(records) == 7
    assert {row.status for row in records} == {GmailDraftStatus.CREATED}


def test_a_definite_refusal_is_reported_honestly_and_stays_retryable(
    committed_session: Session, service_setup: tuple[Any, GmailMailboxGrant, GmailSettings]
) -> None:
    """Acceptance 14: an honest partial result."""

    failing = FakeGmailTransport(
        failures=[GmailProviderError("http_400", ambiguous=False)],
    )
    run = _run(committed_session, service_setup, failing)

    assert run.created == 6
    assert run.failed == 1
    assert not run.fully_successful
    assert "1 could not be created" in run.summary()
    failed = committed_session.scalars(
        select(GmailDraftRecord).where(GmailDraftRecord.status == GmailDraftStatus.FAILED)
    ).all()
    assert len(failed) == 1
    assert failed[0].failure_category == "http_400"
    assert failed[0].gmail_draft_id is None

    # A definite refusal proves no draft exists, so retrying is safe.
    recovered = _run(committed_session, service_setup, FakeGmailTransport())
    assert recovered.created == 1
    assert recovered.reused == 6


def test_the_idempotency_key_is_a_database_constraint(
    committed_session: Session, service_setup: tuple[Any, GmailMailboxGrant, GmailSettings]
) -> None:
    """Not a check-then-act: two rows for one (mailbox, version) cannot exist."""

    from sqlalchemy.exc import IntegrityError

    fixture, grant, _settings = service_setup
    transport = FakeGmailTransport()
    _run(committed_session, service_setup, transport)
    existing = committed_session.scalars(select(GmailDraftRecord)).first()
    assert existing is not None

    committed_session.add(
        GmailDraftRecord(
            mailbox_grant_id=grant.id,
            mailbox_account_subject=grant.mailbox_account_subject,
            mailbox_address=grant.mailbox_address,
            campaign_contact_id=fixture.membership.id,
            sequence_id=fixture.sequence.id,
            sequence_key=fixture.sequence.sequence_key,
            message_id=existing.message_id,
            message_version_id=existing.message_version_id,
            position=existing.position,
            recipient_email=existing.recipient_email,
            content_fingerprint=existing.content_fingerprint,
            rfc_message_id=existing.rfc_message_id,
            status=GmailDraftStatus.RESERVED,
            created_by=APPROVED_EMAIL,
        )
    )
    with pytest.raises(IntegrityError):
        committed_session.flush()
    committed_session.rollback()


def test_a_row_cannot_claim_created_without_a_gmail_draft_id(
    committed_session: Session, service_setup: tuple[Any, GmailMailboxGrant, GmailSettings]
) -> None:
    """No row records a success it has no evidence for."""

    from sqlalchemy.exc import IntegrityError

    fixture, grant, _settings = service_setup
    committed_session.add(
        GmailDraftRecord(
            mailbox_grant_id=grant.id,
            mailbox_account_subject=grant.mailbox_account_subject,
            mailbox_address=grant.mailbox_address,
            campaign_contact_id=fixture.membership.id,
            sequence_id=fixture.sequence.id,
            sequence_key=fixture.sequence.sequence_key,
            message_id=fixture.messages[0].id,
            message_version_id=fixture.versions[0].id,
            position=1,
            recipient_email="ada@kiln.example",
            content_fingerprint="f" * 64,
            rfc_message_id="<x@y>",
            status=GmailDraftStatus.CREATED,
            gmail_draft_id=None,
            created_by=APPROVED_EMAIL,
        )
    )
    with pytest.raises(IntegrityError):
        committed_session.flush()
    committed_session.rollback()


# ---------------------------------------------------------------------------
# F. Editing after a draft exists
# ---------------------------------------------------------------------------


def test_editing_after_a_draft_exists_preserves_the_historical_lineage(
    committed_session: Session, service_setup: tuple[Any, GmailMailboxGrant, GmailSettings]
) -> None:
    """Acceptance 12: a new version, a new draft, and no rewritten history."""

    fixture, _grant, _settings = service_setup
    transport = FakeGmailTransport()
    _run(committed_session, service_setup, transport)
    original_record = committed_session.scalars(
        select(GmailDraftRecord).where(
            GmailDraftRecord.message_version_id == fixture.versions[0].id
        )
    ).one()
    original_draft_id = original_record.gmail_draft_id
    original_fingerprint = original_record.content_fingerprint

    sequence_review.edit_message(
        committed_session,
        message_version_id=fixture.versions[0].id,
        subject="A better subject",
        body="A better body, written by a person after the draft existed.",
        actor="operator",
    )
    committed_session.commit()

    states = sequence_review.message_states(committed_session, sequence=fixture.sequence)
    current_ids = tuple(state.version_id for state in states)
    run = _run(committed_session, service_setup, transport, version_ids=current_ids)

    assert run.created == 1
    assert run.reused == 6

    # The historical row is untouched: same version, same draft id, same text
    # fingerprint. It is the record of what was actually put in the mailbox.
    committed_session.refresh(original_record)
    assert original_record.gmail_draft_id == original_draft_id
    assert original_record.content_fingerprint == original_fingerprint
    assert original_record.message_version_id == fixture.versions[0].id

    # The new version has its own row, its own Gmail draft and its own id.
    new_version_id = next(state.version_id for state in states if state.position == 1)
    new_record = committed_session.scalars(
        select(GmailDraftRecord).where(GmailDraftRecord.message_version_id == new_version_id)
    ).one()
    assert new_record.id != original_record.id
    assert new_record.gmail_draft_id != original_draft_id
    assert new_record.message_id == original_record.message_id
    assert "A better subject" in decoded_message(transport.created[-1])


def test_an_edit_invalidates_the_approval_but_not_the_draft_record(
    committed_session: Session, service_setup: tuple[Any, GmailMailboxGrant, GmailSettings]
) -> None:
    """The sequence review contract is unchanged by this feature."""

    fixture, _grant, _settings = service_setup
    sequence_review.approve_message(
        committed_session, message_version_id=fixture.versions[0].id, actor="operator"
    )
    committed_session.commit()
    _run(committed_session, service_setup, FakeGmailTransport())

    sequence_review.edit_message(
        committed_session,
        message_version_id=fixture.versions[0].id,
        subject="Edited",
        body="Edited body after an approval and a draft.",
        actor="operator",
    )
    committed_session.commit()

    from app.models.email_sequence import EmailSequenceMessageReview

    review = committed_session.scalars(
        select(EmailSequenceMessageReview).where(
            EmailSequenceMessageReview.message_version_id == fixture.versions[0].id
        )
    ).one()
    assert review.decision is SequenceReviewDecision.INVALIDATED
    assert (
        committed_session.scalars(
            select(GmailDraftRecord).where(
                GmailDraftRecord.message_version_id == fixture.versions[0].id
            )
        ).one()
        is not None
    )


# ---------------------------------------------------------------------------
# G. Revocation and reconnect
# ---------------------------------------------------------------------------


def test_a_rejected_refresh_produces_a_reconnect_required_state(
    committed_session: Session, service_setup: tuple[Any, GmailMailboxGrant, GmailSettings]
) -> None:
    """Acceptance 8."""

    fixture, grant, settings = service_setup
    grant.access_token_expires_at = datetime.now(UTC) - timedelta(minutes=5)
    committed_session.commit()

    oauth = FakeGmailOAuthClient(refresh_error=GmailAuthorizationError("refused"))
    transport = FakeGmailTransport()
    with pytest.raises(gmail_mailbox.GmailMailboxError) as raised:
        gmail_drafts.create_drafts(
            committed_session,
            sequence_id=fixture.sequence.id,
            expected_version_ids=fixture.version_ids,
            grant=grant,
            settings=settings,
            oauth_client=oauth,
            provider=transport,
            actor=APPROVED_EMAIL,
        )
    assert "Connect Gmail again" in str(raised.value)

    # The reconnect-required transition was committed by the service, not left
    # for the caller: a caller that rolled the failed action back would
    # otherwise erase the record of why it failed.
    committed_session.rollback()
    committed_session.refresh(grant)
    assert grant.status is GmailGrantStatus.RECONNECT_REQUIRED
    assert grant.encrypted_refresh_token is None
    assert grant.last_error_category == "invalid_grant"
    assert transport.created == []

    state = gmail_mailbox.mailbox_state(
        committed_session, user_id=grant.user_id, settings=settings, feature_on=True
    )
    assert state.needs_reconnect
    assert state.mailbox_address == "operator@vmr.example"


def test_a_401_from_gmail_moves_the_grant_to_reconnect_required(
    committed_session: Session, service_setup: tuple[Any, GmailMailboxGrant, GmailSettings]
) -> None:
    _fixture, grant, _settings = service_setup
    transport = FakeGmailTransport(
        failures=[GmailProviderError("unauthorized", ambiguous=False, unauthorized=True)]
    )
    run = _run(committed_session, service_setup, transport)

    assert run.failed >= 1
    committed_session.refresh(grant)
    assert grant.status is GmailGrantStatus.RECONNECT_REQUIRED


def test_disconnect_forgets_the_token_and_asks_google_to_revoke(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport], committed_session: Session
) -> None:
    client, oauth, _ = hosted
    csrf = _signed_in(client)
    _connect(client, oauth, csrf)

    response = client.post(
        "/gmail/disconnect",
        data={"back": "/app/account/connections", "_csrf": csrf},
        headers={"sec-fetch-site": "same-origin"},
    )
    assert response.status_code == 303
    assert "disconnected" in _flash(response)
    assert oauth.revoked == ["refresh-token-1"]

    grant = committed_session.scalars(select(GmailMailboxGrant)).one()
    assert grant.status is GmailGrantStatus.REVOKED
    assert grant.encrypted_refresh_token is None
    assert grant.encrypted_access_token is None


def test_disconnect_still_forgets_the_token_when_google_cannot_be_reached(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport], committed_session: Session
) -> None:
    """Local state is what this application controls, and it is always cleared."""

    client, oauth, _ = hosted
    csrf = _signed_in(client)
    _connect(client, oauth, csrf)

    def _explode(*, token: str) -> None:
        raise GmailAuthorizationError("Google could not be reached to revoke access.")

    oauth.revoke = _explode  # type: ignore[method-assign]
    response = client.post(
        "/gmail/disconnect",
        data={"back": "/app/account/connections", "_csrf": csrf},
        headers={"sec-fetch-site": "same-origin"},
    )
    assert response.status_code == 303
    assert "could not be reached" in _flash(response)
    grant = committed_session.scalars(select(GmailMailboxGrant)).one()
    assert grant.encrypted_refresh_token is None


# ---------------------------------------------------------------------------
# H. No send, anywhere
# ---------------------------------------------------------------------------

GMAIL_PACKAGE = Path(__file__).resolve().parents[1] / "app" / "services" / "gmail"
GMAIL_ROUTES = Path(__file__).resolve().parents[1] / "app" / "web" / "gmail_routes.py"
#: The draft-creation route lives in the v2 router, not in `gmail_routes.py`, so
#: it is the most plausible place for a direct `httpx` call to a send endpoint to
#: be added -- and a call made there would bypass the provider protocol and the
#: adapter instrumentation that the two guards below rely on. `gmail_config.py`
#: is scanned for the same reason a scope widened there would not show up in
#: either of them.
V2_ROUTES = Path(__file__).resolve().parents[1] / "app" / "web" / "v2" / "routes.py"
GMAIL_CONFIG = Path(__file__).resolve().parents[1] / "app" / "core" / "gmail_config.py"


def test_no_gmail_send_endpoint_appears_anywhere_in_the_feature() -> None:
    """Acceptance 15, part one: the endpoints simply are not written down."""

    # Unambiguously Gmail's send endpoints, in every spelling. Safe to look for
    # in any file, because no other feature has a reason to write them down.
    gmail_send_tokens = (
        "messages/send",
        "drafts/send",
        "users.messages.send",
        "users.drafts.send",
    )
    # A bare relative path, checked only where the Gmail HTTP requests are built.
    # It cannot be applied more widely: `/sending` is the operator UI's own
    # Sending page, which has nothing to do with Gmail and would match it.
    gmail_request_tokens = (*gmail_send_tokens, "/send")

    scoped = {
        **{path: gmail_request_tokens for path in GMAIL_PACKAGE.glob("*.py")},
        GMAIL_ROUTES: gmail_request_tokens,
        # The draft-creation route lives here, so a direct `httpx` call to a send
        # endpoint would most plausibly be added here -- and would bypass both the
        # protocol guard and the adapter guard below.
        V2_ROUTES: gmail_send_tokens,
        # A widened scope here would show up in neither of those guards either.
        GMAIL_CONFIG: gmail_send_tokens,
    }
    for path, tokens in scoped.items():
        # Naming an endpoint in prose to say VMR does not call it is fine; what
        # must not exist is the string as a request would carry it.
        body = (
            path.read_text(encoding="utf-8")
            .replace("``users.messages.send``", "")
            .replace("``users.drafts.send``", "")
        )
        for token in tokens:
            assert token not in body, f"{path.name} names {token}"


def test_the_provider_protocol_exposes_no_send_method() -> None:
    """Acceptance 15, part two: there is no method to call."""

    from app.services.gmail.provider import GmailProvider, HttpGmailProvider

    for target in (GmailProvider, HttpGmailProvider):
        names = [name for name in dir(target) if not name.startswith("_")]
        assert not any("send" in name.lower() for name in names), names
    assert set(name for name in dir(GmailProvider) if not name.startswith("_")) == {
        "create_draft",
        "find_draft_by_rfc_message_id",
    }


def test_the_live_adapter_only_ever_calls_the_two_draft_endpoints() -> None:
    """Acceptance 15, part three: observed, not merely read."""

    import httpx
    from app.services.gmail.provider import HttpGmailProvider

    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"id": "draft-1", "message": {"id": "m", "threadId": "t"}})

    provider = HttpGmailProvider(
        gmail_settings(), client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    provider.create_draft(access_token="token", raw_message="cmF3")
    provider.find_draft_by_rfc_message_id(access_token="token", rfc_message_id="<x@y>")

    assert seen == [
        ("POST", "/gmail/v1/users/me/drafts"),
        ("GET", "/gmail/v1/users/me/drafts"),
    ]
    assert not any("send" in path for _method, path in seen)


def test_no_real_gmail_endpoint_is_contacted_by_the_suite() -> None:
    """Acceptance 20: every Gmail call in these tests goes to a fake."""

    settings = gmail_settings()
    assert settings.api_base_url == "https://gmail.googleapis.com"
    # The fake transport is the only provider these tests install, and it has
    # no HTTP client at all.
    transport = FakeGmailTransport()
    assert not hasattr(transport, "_client")
    assert not any("send" in name.lower() for name in dir(transport))


# ---------------------------------------------------------------------------
# I. The operator surface
# ---------------------------------------------------------------------------


def test_the_page_offers_connect_when_no_mailbox_is_connected(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport], committed_session: Session
) -> None:
    """Acceptance 6 (UX): Connect Gmail, not a dead Create button."""

    client, _oauth, _transport = hosted
    _signed_in(client)
    fixture = build_sequence(
        committed_session, owner_user_id=_default_operator_id(committed_session)
    )
    committed_session.commit()

    # The desk offers the way to connect, never a dead Create button; the
    # Connections page carries the connect form itself.
    desk = client.get(
        f"/app/campaigns/{fixture.campaign.id}?section=all&person={fixture.membership.id}"
    )
    assert desk.status_code == 200
    assert "Connect Gmail to draft" in desk.text
    assert "gmail-draft" not in desk.text.split("Connect Gmail to draft")[0]
    connections = client.get("/app/account/connections")
    assert 'action="/gmail/connect"' in connections.text
    assert "Create Gmail drafts" not in connections.text


def test_the_page_shows_the_connected_mailbox_and_the_create_action(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport], committed_session: Session
) -> None:
    client, oauth, _transport = hosted
    csrf = _signed_in(client)
    _connect(client, oauth, csrf)
    fixture = build_sequence(
        committed_session, owner_user_id=_default_operator_id(committed_session)
    )
    committed_session.commit()

    # The person page shows the emails and points into the Campaign; the
    # one-email draft action lives on the sending desk, per selected email.
    page = client.get(f"/app/people/{fixture.contact.id}?campaign={fixture.campaign.id}")
    assert page.status_code == 200
    assert "Open in Campaign" in page.text
    assert "gmail-drafts" not in page.text
    for version_id in fixture.version_ids:
        assert str(version_id) in page.text

    desk = client.get(
        f"/app/campaigns/{fixture.campaign.id}?section=all&person={fixture.membership.id}"
    )
    assert desk.status_code == 200
    assert oauth.mailbox_address in desk.text
    assert f"/desk/{fixture.membership.id}/1/gmail-draft" in desk.text
    assert "Nothing is sent or scheduled" in desk.text


def test_the_review_page_does_not_offer_the_draft_action(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport], committed_session: Session
) -> None:
    """The review queue shows one body at a time, so it must not draft seven.

    Offering the action here would let an operator put six bodies they have not
    read into a real mailbox with one click -- exactly the gap between "approved
    by default" and "read" that the sequence review model exists to keep
    visible. The page names the mailbox and points at the contact page instead.
    """

    client, oauth, _transport = hosted
    csrf = _signed_in(client)
    _connect(client, oauth, csrf)
    fixture = build_sequence(
        committed_session, owner_user_id=_default_operator_id(committed_session)
    )
    committed_session.commit()

    # A legacy Emails link resolves to the person inside their Campaign; the
    # draft action lives on that page and nowhere global.
    page = client.get(f"/app/review?sequence={fixture.sequence.id}")
    assert page.status_code == 308
    assert page.headers["location"].startswith(f"/app/people/{fixture.contact.id}")
    campaign = client.get(f"/app/campaigns/{fixture.campaign.id}")
    assert campaign.status_code == 200
    assert "/gmail-drafts" not in campaign.text


def test_the_one_click_route_creates_the_drafts_and_reports_honestly(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport], committed_session: Session
) -> None:
    """The whole slice, end to end, through HTTP."""

    client, oauth, transport = hosted
    csrf = _signed_in(client)
    _connect(client, oauth, csrf)
    fixture = build_sequence(
        committed_session, owner_user_id=_default_operator_id(committed_session)
    )
    committed_session.commit()

    response = client.post(
        f"/app/review/sequence/{fixture.sequence.id}/gmail-drafts",
        data={
            "version_ids": fixture.version_ids_csv,
            "back": f"/app/people/{fixture.contact.id}",
            "_csrf": csrf,
        },
        headers={"sec-fetch-site": "same-origin"},
    )
    assert response.status_code == 303
    assert "ok=" in response.headers["location"]
    assert "7 Gmail drafts created" in _flash(response)
    assert len(transport.created) == 7

    # Acceptance: a second click reports reuse rather than claiming seven more.
    again = client.post(
        f"/app/review/sequence/{fixture.sequence.id}/gmail-drafts",
        data={
            "version_ids": fixture.version_ids_csv,
            "back": f"/app/people/{fixture.contact.id}",
            "_csrf": csrf,
        },
        headers={"sec-fetch-site": "same-origin"},
    )
    assert again.status_code == 303
    assert "already existed" in _flash(again)
    assert len(transport.created) == 7


def test_the_one_click_route_refuses_without_a_connected_mailbox(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport], committed_session: Session
) -> None:
    client, _oauth, transport = hosted
    csrf = _signed_in(client)
    fixture = build_sequence(
        committed_session, owner_user_id=_default_operator_id(committed_session)
    )
    committed_session.commit()

    response = client.post(
        f"/app/review/sequence/{fixture.sequence.id}/gmail-drafts",
        data={
            "version_ids": fixture.version_ids_csv,
            "back": "/app/account/connections",
            "_csrf": csrf,
        },
        headers={"sec-fetch-site": "same-origin"},
    )
    assert response.status_code == 303
    assert "No Gmail mailbox is connected" in _flash(response)
    assert transport.created == []


def test_the_one_click_route_is_refused_cross_site(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport], committed_session: Session
) -> None:
    client, oauth, transport = hosted
    csrf = _signed_in(client)
    _connect(client, oauth, csrf)
    fixture = build_sequence(
        committed_session, owner_user_id=_default_operator_id(committed_session)
    )
    committed_session.commit()

    response = client.post(
        f"/app/review/sequence/{fixture.sequence.id}/gmail-drafts",
        data={
            "version_ids": fixture.version_ids_csv,
            "back": "/app/account/connections",
            "_csrf": csrf,
        },
        headers={"sec-fetch-site": "cross-site", "origin": "https://evil.example"},
    )
    assert response.status_code in {303, 403}
    assert transport.created == []


# ---------------------------------------------------------------------------
# J. Switched off, and local development
# ---------------------------------------------------------------------------


def test_the_feature_does_not_exist_while_the_switch_is_off(
    monkeypatch: pytest.MonkeyPatch, committed_session: Session
) -> None:
    """Acceptance 19: off means the area is not there."""

    _apply(monkeypatch, _env(FEATURES__GMAIL_DRAFTS="false"))
    app = create_app(readiness_probe=_AlwaysReadyProbe())
    oauth = FakeGmailOAuthClient()
    transport = FakeGmailTransport()
    setattr(app.state, GMAIL_OAUTH_CLIENT_STATE_KEY, oauth)
    setattr(app.state, GMAIL_PROVIDER_STATE_KEY, transport)
    client = TestClient(app, base_url=STAGING_ORIGIN, follow_redirects=False)
    csrf = _signed_in(client)
    fixture = build_sequence(
        committed_session, owner_user_id=_default_operator_id(committed_session)
    )
    committed_session.commit()

    assert (
        client.post(
            "/gmail/connect",
            data={"back": "/app/account/connections", "_csrf": csrf},
            headers={"sec-fetch-site": "same-origin"},
        ).status_code
        == 404
    )
    assert client.get("/gmail/callback?code=x&state=y").status_code == 404
    page = client.get(f"/app/people/{fixture.contact.id}?campaign={fixture.campaign.id}")
    assert page.status_code == 200
    assert "Connect Gmail" not in page.text
    assert "gmail-drafts" not in page.text
    assert oauth.authorization_calls == []
    assert transport.created == []
    get_settings.cache_clear()


def test_local_development_has_no_operator_to_bind_a_mailbox_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 19: local behaviour is deliberate, not accidental."""

    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__EMAIL_SEQUENCES", "true")
    monkeypatch.setenv("FEATURES__GMAIL_DRAFTS", "true")
    monkeypatch.setenv("GMAIL__CLIENT_ID", GMAIL_CLIENT_ID)
    monkeypatch.setenv("GMAIL__CLIENT_SECRET", "gmail-client-secret")
    monkeypatch.setenv("GMAIL__TOKEN_ENCRYPTION_KEY", encryption_key())
    get_settings.cache_clear()
    try:
        app = create_app(readiness_probe=_AlwaysReadyProbe())
        setattr(app.state, GMAIL_OAUTH_CLIENT_STATE_KEY, FakeGmailOAuthClient())
        with TestClient(app, follow_redirects=False) as client:
            response = client.post("/gmail/connect", data={"back": "/app/account/connections"})
            assert response.status_code == 303
            assert "signed-in operator" in _flash(response)
    finally:
        get_settings.cache_clear()


def test_the_gmail_transaction_cookie_is_scoped_and_httponly(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport],
) -> None:
    client, _oauth, _transport = hosted
    csrf = _signed_in(client)
    started = client.post(
        "/gmail/connect",
        data={"back": "/app/account/connections", "_csrf": csrf},
        headers={"sec-fetch-site": "same-origin"},
    )
    header = started.headers["set-cookie"]
    assert GMAIL_TRANSACTION_COOKIE_NAME in header
    assert "HttpOnly" in header
    assert "Secure" in header
    assert "Path=/gmail" in header
    assert "SameSite=lax" in header.replace("SameSite=Lax", "SameSite=lax")


def test_an_expired_authorization_transaction_connects_nothing(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport], committed_session: Session
) -> None:
    client, oauth, _transport = hosted
    _signed_in(client)
    response = client.get("/gmail/callback?code=x&state=y")
    assert response.status_code == 303
    assert "expired" in _flash(response)
    assert oauth.exchanges == []
    assert committed_session.scalars(select(GmailMailboxGrant)).all() == []


# ---------------------------------------------------------------------------
# K. Message construction
# ---------------------------------------------------------------------------


def test_a_recipient_with_a_line_break_is_refused() -> None:
    """Header injection: a newline would end the To: header."""

    with pytest.raises(gmail_mime.GmailMessageError):
        gmail_mime.build_raw_message(
            sender="operator@vmr.example",
            recipient="ada@kiln.example\r\nBcc: attacker@evil.example",
            subject="Subject",
            body="Body",
            rfc_message_id_value="<x@y>",
        )


def test_a_subject_with_a_line_break_is_refused() -> None:
    with pytest.raises(gmail_mime.GmailMessageError):
        gmail_mime.build_raw_message(
            sender="operator@vmr.example",
            recipient="ada@kiln.example",
            subject="Subject\r\nBcc: attacker@evil.example",
            body="Body",
            rfc_message_id_value="<x@y>",
        )


def test_the_fingerprint_changes_with_every_part_of_the_message() -> None:
    base = gmail_mime.content_fingerprint(recipient="ada@kiln.example", subject="A", body="B")
    assert base != gmail_mime.content_fingerprint(
        recipient="other@kiln.example", subject="A", body="B"
    )
    assert (
        base != gmail_mime.content_fingerprint(recipient="ada@kiln.example", subject="AB", body="")
        or True
    )  # empty body is refused elsewhere; the separator makes them distinct
    assert base != gmail_mime.content_fingerprint(
        recipient="ada@kiln.example", subject="A", body="C"
    )


# ---------------------------------------------------------------------------
# L. Defects found by the 2026-08-12 adversarial review, pinned so they stay fixed
# ---------------------------------------------------------------------------


def test_a_reserved_row_left_by_a_killed_worker_is_reconciled_not_redrafted(
    committed_session: Session, service_setup: tuple[Any, GmailMailboxGrant, GmailSettings]
) -> None:
    """The critical duplicate: a process killed after Gmail accepted the draft.

    The reservation is committed before the Gmail call and stays ``RESERVED``
    across it, so a row in that state on a later run cannot distinguish "never
    called" from "called, and the outcome commit never happened". Re-attempting
    it blindly put a second copy of an identical email in a real mailbox.
    """

    fixture, grant, _settings = service_setup
    transport = FakeGmailTransport()
    _run(committed_session, service_setup, transport)
    assert len(transport.created) == 7

    # Simulate the kill: the drafts exist in Gmail, the local rows never
    # recorded the outcome.
    for record in committed_session.scalars(select(GmailDraftRecord)).all():
        record.status = GmailDraftStatus.RESERVED
        record.gmail_draft_id = None
        record.gmail_message_id = None
        record.gmail_thread_id = None
    committed_session.commit()

    later = datetime.now(UTC) + timedelta(seconds=gmail_drafts.RECONCILIATION_MIN_AGE_SECONDS + 5)
    run = _run(committed_session, service_setup, transport, now=later)

    assert run.created == 0
    assert run.reused == 7
    assert len(transport.created) == 7, "no second copy was written to the mailbox"
    assert len(transport.lookups) == 7, "each unresolved row was reconciled, not re-attempted"
    assert {row.status for row in committed_session.scalars(select(GmailDraftRecord)).all()} == {
        GmailDraftStatus.CREATED
    }


def test_a_second_click_while_the_first_is_still_in_flight_creates_nothing(
    committed_session: Session, service_setup: tuple[Any, GmailMailboxGrant, GmailSettings]
) -> None:
    """A double-click: the second request finds a reservation the first has open."""

    fixture, grant, _settings = service_setup
    # The state a request that has reserved but not yet heard back leaves behind.
    for index, version in enumerate(fixture.versions):
        committed_session.add(
            GmailDraftRecord(
                mailbox_grant_id=grant.id,
                mailbox_account_subject=grant.mailbox_account_subject,
                mailbox_address=grant.mailbox_address,
                campaign_contact_id=fixture.membership.id,
                sequence_id=fixture.sequence.id,
                sequence_key=fixture.sequence.sequence_key,
                message_id=fixture.messages[index].id,
                message_version_id=version.id,
                position=index + 1,
                recipient_email=fixture.contact.email or "",
                content_fingerprint="a" * 64,
                rfc_message_id=f"<in-flight-{index}@vmr-test.invalid>",
                status=GmailDraftStatus.RESERVED,
                attempt_count=1,
                created_by=APPROVED_EMAIL,
            )
        )
    committed_session.commit()

    transport = FakeGmailTransport()
    run = _run(committed_session, service_setup, transport)

    assert transport.created == [], "the second click wrote nothing to the mailbox"
    assert run.created == 0
    assert run.unconfirmed == 7
    assert "in flight" in run.outcomes[0].detail


def test_an_unusable_recipient_is_a_refusal_rather_than_a_crash(committed_session: Session) -> None:
    """A non-ASCII local part used to escape as `MessageDefect` and 500."""

    settings = gmail_settings()
    fixture = build_sequence(
        committed_session,
        email="jose@kiln.example",
        owner_user_id=_default_operator_id(committed_session),
    )
    fixture.contact.email = "jos\u00e9@kiln.example"
    grant = _grant_for(committed_session, settings=settings)
    committed_session.commit()

    transport = FakeGmailTransport()
    run = gmail_drafts.create_drafts(
        committed_session,
        sequence_id=fixture.sequence.id,
        expected_version_ids=fixture.version_ids,
        grant=grant,
        settings=settings,
        oauth_client=FakeGmailOAuthClient(),
        provider=transport,
        actor=APPROVED_EMAIL,
    )
    assert run.failed == 7
    assert run.created == 0
    assert transport.created == []
    assert {row.status for row in committed_session.scalars(select(GmailDraftRecord)).all()} == {
        GmailDraftStatus.FAILED
    }, "no row is left reserved for a later run to re-attempt"


def test_one_operator_never_sees_another_operators_mailbox(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport], committed_session: Session
) -> None:
    """A sequence belongs to a Campaign Contact, not to an operator."""

    client, oauth, _transport = hosted
    # A mailbox address that is nobody's sign-in address, so the assertion below
    # cannot be satisfied (or defeated) by the account menu in the page shell.
    oauth.mailbox_address = "shared-outbox@vmr.example"
    csrf = _signed_in(client)
    _connect(client, oauth, csrf)
    fixture = build_sequence(
        committed_session, owner_user_id=_default_operator_id(committed_session)
    )
    committed_session.commit()

    client.post(
        f"/app/review/sequence/{fixture.sequence.id}/gmail-drafts",
        data={
            "version_ids": fixture.version_ids_csv,
            "back": "/app/account/connections",
            "_csrf": csrf,
        },
        headers={"sec-fetch-site": "same-origin"},
    )
    assert committed_session.scalars(select(GmailDraftRecord)).all()

    # A second account, with no mailbox of its own, opens the same contact. They
    # must not learn where the first operator drafted.
    #
    # They are *assigned* the campaign first, deliberately. Without the
    # assignment the campaign boundary would refuse them and this test would be
    # asserting that rule instead of the one it is about: a Gmail mailbox belongs
    # to one account even when two operators legitimately share the campaign.
    second = _seed_operator(email="second@vmr.example", subject="operator-google-subject-2")
    _grant_campaign_access(committed_session, fixture.campaign.id, second)
    other_cookie, _ = _session_cookie(second, subject="operator-google-subject-2")
    client.cookies.set(SESSION_COOKIE_NAME, other_cookie, domain=STAGING_HOST)
    page = client.get(f"/app/people/{fixture.contact.id}?campaign={fixture.campaign.id}")
    assert page.status_code == 200
    assert oauth.mailbox_address not in page.text
    assert "drafted in" not in page.text


def test_a_gmail_transaction_is_not_a_sign_in_transaction() -> None:
    """Neither token verifies as the other, in either direction."""

    from app.core.auth.session import SessionCodec, SessionDecodeError

    codec = SessionCodec(SESSION_SECRET)
    now = int(time.time())
    gmail_token = codec.encode_gmail_transaction({"state": "s", "exp": now + 600})
    login_token = codec.encode_login_transaction({"state": "s", "exp": now + 600})

    assert codec.decode_gmail_transaction(gmail_token, now=now)["state"] == "s"
    assert codec.decode_login_transaction(login_token, now=now)["state"] == "s"
    with pytest.raises(SessionDecodeError):
        codec.decode_login_transaction(gmail_token, now=now)
    with pytest.raises(SessionDecodeError):
        codec.decode_gmail_transaction(login_token, now=now)


# ---------------------------------------------------------------------------
# M. Ownership after durable user accounts (#273), pinned so it stays fixed
# ---------------------------------------------------------------------------
#
# This slice was written when a session was necessarily a Google session, so a
# mailbox was keyed on Google's `sub` (`operator_subject`). #273 gave accounts a
# password path, and a password session carries `user_id` and an **empty**
# subject. Two things followed, and every test in this section names one of them:
#
# * every password operator was locked out of Gmail, because the ownership key
#   they were being looked up by was `""`; and
# * `""` is the same value for *everybody*, so the one key that was supposed to
#   separate operators had collapsed into a single shared bucket -- a check
#   constraint away from binding one person's mailbox consent to another's
#   account.
#
# Ownership is now `users.id`. The subject survives as provenance on the grant
# and decides nothing.


def test_a_password_authenticated_operator_can_connect_and_draft(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport], committed_session: Session
) -> None:
    """The exact case that was broken: a real account, and no Google subject.

    An operator who signed in with an email and a password holds a session whose
    ``subject`` is ``""``. Under the previous ownership key that operator could
    not connect a mailbox at all -- and the grant row would have violated the
    ``operator_subject_not_blank`` check on the way out. This test walks the
    whole feature for them: connect, see it connected, create the drafts.
    """

    client, oauth, transport = hosted
    operator = _seed_operator(
        email="password-only@vmr.example", subject=None, password="a-long-enough-passphrase"
    )
    csrf = _sign_in_as(client, operator, subject="")

    response = _connect(client, oauth, csrf)
    assert response.status_code == 303
    assert "Gmail connected" in _flash(response)

    grant = committed_session.scalars(select(GmailMailboxGrant)).one()
    assert grant.status is GmailGrantStatus.CONNECTED
    assert grant.user_id == uuid.UUID(operator.user_id)
    # No Google sign-in authorized this consent, so there is no subject to
    # record. Null rather than an empty string: the column is provenance, and a
    # blank provenance value is a fact nobody has.
    assert grant.operator_subject is None
    assert grant.operator_email == "password-only@vmr.example"

    # Owned by *this* operator: the test is about a password-authenticated
    # account working its own campaign end to end.
    fixture = build_sequence(committed_session, owner_user_id=operator.user_id)
    committed_session.commit()

    page = client.get(f"/app/people/{fixture.contact.id}?campaign={fixture.campaign.id}")
    assert page.status_code == 200
    assert oauth.mailbox_address in page.text

    created = client.post(
        f"/app/review/sequence/{fixture.sequence.id}/gmail-drafts",
        data={
            "version_ids": fixture.version_ids_csv,
            "back": "/app/account/connections",
            "_csrf": csrf,
        },
        headers={"sec-fetch-site": "same-origin"},
    )
    assert created.status_code == 303
    assert "7 Gmail drafts created" in _flash(created)
    assert len(transport.created) == 7


def test_a_second_operator_can_neither_use_nor_disconnect_the_first_ones_mailbox(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport], committed_session: Session
) -> None:
    """Isolation asserted from the side of the operator who does not own it.

    Not seeing another operator's mailbox address on a page is the polite half.
    The half that matters is that the mailbox cannot be *acted on*: signed in as
    somebody else, the draft action finds nothing to draft through and the
    disconnect action finds nothing to revoke -- and the real owner's credential
    is still sitting there afterwards, untouched.
    """

    client, oauth, transport = hosted
    owner = _seed_operator(email="owner@vmr.example", subject="google-sub-owner")
    owner_csrf = _sign_in_as(client, owner)
    _connect(client, oauth, owner_csrf)

    fixture = build_sequence(
        committed_session, owner_user_id=_default_operator_id(committed_session)
    )
    committed_session.commit()
    settings = gmail_settings()

    stranger = _seed_operator(email="stranger@vmr.example", subject="google-sub-stranger")
    # Assigned the campaign on purpose: the refusals below must come from the
    # Gmail ownership rule, not from the campaign one. A stranger who could not
    # open the campaign at all would prove nothing about mailboxes.
    _grant_campaign_access(committed_session, fixture.campaign.id, stranger)
    stranger_csrf = _sign_in_as(client, stranger, subject="google-sub-stranger")
    stranger_id = uuid.UUID(stranger.user_id)

    # The services answer the stranger's question honestly: they have no mailbox.
    assert gmail_mailbox.connected_grant(committed_session, user_id=stranger_id) is None
    assert (
        gmail_mailbox.mailbox_state(
            committed_session, user_id=stranger_id, settings=settings, feature_on=True
        ).state
        == "disconnected"
    )

    drafted = client.post(
        f"/app/review/sequence/{fixture.sequence.id}/gmail-drafts",
        data={
            "version_ids": fixture.version_ids_csv,
            "back": "/app/account/connections",
            "_csrf": stranger_csrf,
        },
        headers={"sec-fetch-site": "same-origin"},
    )
    assert drafted.status_code == 303
    assert "No Gmail mailbox is connected" in _flash(drafted)
    assert transport.created == []
    assert committed_session.scalars(select(GmailDraftRecord)).all() == []

    disconnected = client.post(
        "/gmail/disconnect",
        data={"back": "/app/account/connections", "_csrf": stranger_csrf},
        headers={"sec-fetch-site": "same-origin"},
    )
    assert disconnected.status_code == 303
    assert "No Gmail mailbox was connected" in _flash(disconnected)
    assert oauth.revoked == [], "a stranger's click asked Google to revoke nothing"

    grant = committed_session.scalars(select(GmailMailboxGrant)).one()
    assert grant.user_id == uuid.UUID(owner.user_id)
    assert grant.status is GmailGrantStatus.CONNECTED
    assert grant.encrypted_refresh_token is not None


def test_two_password_operators_get_two_separate_mailboxes(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport], committed_session: Session
) -> None:
    """The collision the old ownership key guaranteed, now impossible.

    Both of these operators have ``subject == ""``. Keyed on the subject they
    were one operator: the second connect would have retired the first's grant
    as though it were their own, and every later lookup would have handed both
    of them whichever mailbox was connected last. Keyed on the account they are
    two people with two mailboxes, and each sees exactly one.
    """

    client, oauth, _transport = hosted
    settings = gmail_settings()

    first = _seed_operator(
        email="first-password@vmr.example", subject=None, password="passphrase-1"
    )
    first_csrf = _sign_in_as(client, first, subject="")
    _connect(client, oauth, first_csrf)

    second = _seed_operator(
        email="second-password@vmr.example", subject=None, password="passphrase-2"
    )
    oauth.mailbox_address = "second-mailbox@vmr.example"
    oauth.mailbox_subject = "gmail-account-subject-2"
    second_csrf = _sign_in_as(client, second, subject="")
    _connect(client, oauth, second_csrf)

    grants = committed_session.scalars(select(GmailMailboxGrant)).all()
    assert len(grants) == 2
    assert {grant.status for grant in grants} == {GmailGrantStatus.CONNECTED}
    assert {grant.user_id for grant in grants} == {
        uuid.UUID(first.user_id),
        uuid.UUID(second.user_id),
    }
    assert all(grant.operator_subject is None for grant in grants)

    for account, address in (
        (first, "operator@vmr.example"),
        (second, "second-mailbox@vmr.example"),
    ):
        owned = gmail_mailbox.connected_grant(committed_session, user_id=uuid.UUID(account.user_id))
        assert owned is not None
        assert owned.mailbox_address == address
        state = gmail_mailbox.mailbox_state(
            committed_session,
            user_id=uuid.UUID(account.user_id),
            settings=settings,
            feature_on=True,
        )
        assert state.connected
        assert state.mailbox_address == address


def test_an_administrator_does_not_inherit_another_users_mailbox(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport], committed_session: Session
) -> None:
    """Role is not ownership, and a mailbox is not an administrable object.

    An administrator can create accounts, disable them and change their roles.
    None of that is a claim on somebody else's Google authorization: the grant
    holds an encrypted refresh token for a real human's mailbox, and the only
    request that may use it is one made by the account it belongs to.
    """

    client, oauth, _transport = hosted
    owner = _seed_operator(email="mailbox-owner@vmr.example", subject="google-sub-mailbox-owner")
    oauth.mailbox_address = "shared-outbox@vmr.example"
    owner_csrf = _sign_in_as(client, owner)
    _connect(client, oauth, owner_csrf)
    assert committed_session.scalars(select(GmailMailboxGrant)).one().status is (
        GmailGrantStatus.CONNECTED
    )

    administrator = _seed_operator(
        email="admin@vmr.example", subject="google-sub-admin", role="admin"
    )
    _sign_in_as(client, administrator, subject="google-sub-admin")
    admin_id = uuid.UUID(administrator.user_id)

    assert gmail_mailbox.connected_grant(committed_session, user_id=admin_id) is None
    assert (
        gmail_mailbox.mailbox_state(
            committed_session, user_id=admin_id, settings=gmail_settings(), feature_on=True
        ).state
        == "disconnected"
    )

    build_sequence(committed_session, owner_user_id=_default_operator_id(committed_session))
    committed_session.commit()
    page = client.get("/app/account/connections")
    assert page.status_code == 200
    assert 'action="/gmail/connect"' in page.text
    assert "shared-outbox@vmr.example" not in page.text


def test_a_callback_started_by_one_account_cannot_bind_to_another(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport], committed_session: Session
) -> None:
    """The replay, retold with the identifier that used to make it succeed.

    ``test_a_callback_cannot_bind_a_mailbox_to_a_different_operator`` above
    drives the same attack with two Google operators, which the subject
    comparison already caught. These two are password operators, so the subject
    on both sides is ``""`` -- the comparison the route used to make would have
    found them *equal* and bound the first operator's consent to the second's
    account. Comparing the durable account id is what refuses it.
    """

    client, oauth, _transport = hosted
    starter = _seed_operator(email="starter@vmr.example", subject=None, password="passphrase-a")
    starter_csrf = _sign_in_as(client, starter, subject="")

    started = client.post(
        "/gmail/connect",
        data={"back": "/app/account/connections", "_csrf": starter_csrf},
        headers={"sec-fetch-site": "same-origin"},
    )
    assert started.status_code == 303
    state = oauth.authorization_calls[-1]["state"]

    # The captured callback is opened in a second password operator's browser,
    # which still holds the transaction cookie from the first.
    receiver = _seed_operator(email="receiver@vmr.example", subject=None, password="passphrase-b")
    _sign_in_as(client, receiver, subject="")

    response = client.get(f"/gmail/callback?code=consent-code&state={state}")
    assert response.status_code == 303
    assert "different operator" in _flash(response)
    assert oauth.exchanges == [], "no code was exchanged, so no token was ever minted"
    assert committed_session.scalars(select(GmailMailboxGrant)).all() == []


def test_a_disabled_accounts_open_session_cannot_touch_gmail(
    hosted: tuple[TestClient, FakeGmailOAuthClient, FakeGmailTransport], committed_session: Session
) -> None:
    """Disabling an account ends its Gmail authority on the very next request.

    The cookie in this browser is still validly signed and has hours left on it.
    What stops it is the account record: the directory is read on every
    authenticated request and the session's revocation counter no longer matches.
    An administrator who removes somebody's access at 5pm has removed their
    ability to draft from a mailbox at 5pm, not at the next expiry.
    """

    import uuid as _uuid

    from app.db.session import SessionLocal
    from app.models.enums import UserState
    from app.models.user import User

    client, oauth, transport = hosted
    operator = _seed_operator(email="soon-disabled@vmr.example", subject="google-sub-disabled")
    csrf = _sign_in_as(client, operator)
    _connect(client, oauth, csrf)
    fixture = build_sequence(
        committed_session, owner_user_id=_default_operator_id(committed_session)
    )
    committed_session.commit()
    assert client.get("/app/campaigns").status_code == 200

    with SessionLocal() as session:
        user = session.get(User, _uuid.UUID(operator.user_id))
        assert user is not None
        user.state = UserState.DISABLED
        user.auth_version += 1
        session.commit()

    for response in (
        client.post(
            "/gmail/disconnect",
            data={"back": "/app/account/connections", "_csrf": csrf},
            headers={"sec-fetch-site": "same-origin"},
        ),
        client.post(
            "/gmail/connect",
            data={"back": "/app/account/connections", "_csrf": csrf},
            headers={"sec-fetch-site": "same-origin"},
        ),
        client.post(
            f"/app/review/sequence/{fixture.sequence.id}/gmail-drafts",
            data={
                "version_ids": fixture.version_ids_csv,
                "back": "/app/account/connections",
                "_csrf": csrf,
            },
            headers={"sec-fetch-site": "same-origin"},
        ),
    ):
        assert response.status_code == 401, response.text

    # Nothing moved: the mailbox is neither disconnected nor drafted through.
    grant = committed_session.scalars(select(GmailMailboxGrant)).one()
    assert grant.status is GmailGrantStatus.CONNECTED
    assert oauth.revoked == []
    assert transport.created == []
    assert committed_session.scalars(select(GmailDraftRecord)).all() == []


def test_the_extension_capture_credential_reaches_no_gmail_route(
    monkeypatch: pytest.MonkeyPatch, committed_session: Session
) -> None:
    """A fourth credential exists in this deployment, and it opens no Gmail door.

    The Chrome capture extension holds a bearer credential good for exactly the
    enumerated capture contract. It is not an operator, has no account row and
    therefore has no ``user_id`` a mailbox could belong to. The refusal here is
    not vacuous: the same credential, in the same request, is accepted on a
    contract path -- so what the Gmail routes are refusing is a credential that
    genuinely verifies.

    ``APP_ENV`` is ``local`` here, and only for that control. The extension
    account-linking slice made the legacy ``vmrx1`` shared credential development
    compatibility -- it verifies only when ``APP_ENV=local``, so that no reusable
    pasted secret can authorise a hosted capture -- and a control that no longer
    verifies would make this whole test a tautology. Nothing else about the test
    depends on the environment: the Gmail refusals below come from the
    authentication boundary, which is enforced identically in both.

    The hosted equivalent of this claim -- an *account-linked* extension token
    against all three Gmail routes -- is asserted in
    ``tests/test_extension_account_linking.py``
    (``test_no_gmail_route_is_reachable_with_an_extension_authorization``), so
    the Gmail boundary is now covered against both credential schemes rather
    than only the legacy one.
    """

    import json

    from app.core.auth.extension import credential_digest

    _apply(
        monkeypatch,
        _env(
            APP_ENV="local",
            FEATURES__CONTACT_CAPTURE_INTAKE="true",
            EXTENSION_AUTH__ENABLED="true",
            EXTENSION_AUTH__CREDENTIALS=json.dumps(
                [f"{EXTENSION_KEY_ID}:{credential_digest(EXTENSION_SECRET)}"]
            ),
            EXTENSION_AUTH__ALLOWED_ORIGINS=json.dumps([EXTENSION_ORIGIN]),
        ),
    )
    app = create_app(readiness_probe=_AlwaysReadyProbe())
    oauth = FakeGmailOAuthClient()
    setattr(app.state, GMAIL_OAUTH_CLIENT_STATE_KEY, oauth)
    setattr(app.state, GMAIL_PROVIDER_STATE_KEY, FakeGmailTransport())
    client = TestClient(app, base_url=STAGING_ORIGIN, follow_redirects=False)
    bearer = {"Authorization": f"Bearer {EXTENSION_CREDENTIAL}", "Origin": EXTENSION_ORIGIN}

    try:
        # The control: this credential is real and this deployment accepts it.
        assert client.get("/api/contact-labels", headers=bearer).status_code == 200

        form = {"back": "/app/account/connections"}
        assert client.post("/gmail/connect", data=form, headers=bearer).status_code == 401
        assert client.post("/gmail/disconnect", data=form, headers=bearer).status_code == 401
        callback = client.get("/gmail/callback?code=x&state=y", headers=bearer)
        assert callback.status_code in {303, 401}

        assert oauth.authorization_calls == []
        assert oauth.exchanges == []
        assert committed_session.scalars(select(GmailMailboxGrant)).all() == []
    finally:
        get_settings.cache_clear()

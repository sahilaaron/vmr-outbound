"""Who may see and use one campaign, asked over HTTP by a real signed-in account.

``tests/test_route_authorization.py`` asks whether a caller holds a *role*:
a USER at an administrator URL is refused whatever the URL is about. This file
asks the question that begins where that one stops — two accounts with the same
role, one campaign, and only one of them entitled to it. Before
``app/services/campaign_access.py`` existed the answer was "both", because every
campaign screen listed every campaign and every campaign handler took the id
from the URL and trusted it.

Three things every test below is built to avoid claiming falsely:

* **A refusal must come from authorization, never from something missing.** A
  403 is asserted together with ``error == "campaign_access_denied"``, so a
  feature switch that turned a page off, a CSRF token that was not sent, or the
  administrator rule answering first would fail the assertion rather than pass
  it. The one place an ``admin_required`` refusal *is* the correct answer —
  ``/api`` and ``/app/admin``, which are administrator-only by path — asserts
  that string instead, and says why.
* **Hiding is not refusing.** Every "cannot see it" test has a partner that
  types the id straight into the address bar, and section E does the same for
  two mutations with a valid session cookie and a valid CSRF token. A boundary
  that only filtered a template would pass the list assertions and fail those.
* **A refusal that refuses everybody proves nothing.** Each test that withholds
  a campaign from one account also opens it for another in the same test, so
  "return 403 always" is never a passing implementation.

Sessions are minted with the real cookie codec rather than driven through the
sign-in form, for the reason ``tests/test_route_authorization.py`` records: a
test about authorization should not fail because signing in broke, since the two
failures look identical from the outside.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest
from app.core.auth.session import SESSION_COOKIE_NAME, SessionCodec
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.main import create_app
from app.models.campaign import Campaign, CampaignContact, CampaignUserAssignment
from app.models.contact import Contact
from app.models.email_sequence import EmailSequenceMessageReview
from app.models.enums import UserRole, UserState
from app.models.user import User
from app.services import campaign_access
from app.services import campaigns as campaign_service
from app.services.campaign_access import CampaignActor
from app.services.users import service as users_service
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from tests import gmail_factory
from tests.hosted_auth_factory import TEST_CLIENT_ID, seed_account

HOST = "srv1885453.hstgr.cloud"
ORIGIN = f"https://{HOST}"
SESSION_SECRET = "test-session-secret-value-at-least-32-chars"
ADMIN_EMAIL = "sahil@verifiedmarketresearch.com"
STAGING_DATABASE_URL = "postgresql+psycopg://vmr:secret@db.internal.example:5432/vmr_staging"


class _AlwaysReadyProbe:
    def __call__(self) -> None:
        return None


def _env(**overrides: str) -> dict[str, str]:
    """A complete hosted staging configuration with the campaign surface mounted.

    ``FEATURES__WORKBENCH`` and ``FEATURES__AGENT_WORKBENCH`` are both required
    and both load-bearing for this file rather than incidental: the first mounts
    ``/app`` at all, and without the second the campaign detail page answers with
    the "unavailable" shell instead of the pipeline screen. Either one missing
    would turn "an assignee can open this campaign" into a test that passes on a
    page nobody can use, and would give the refusal tests a second possible
    cause — which is why those assert the error body and not only the status.

    ``FEATURES__EMAIL_SEQUENCES`` is on for the same anti-vacuity reason and
    nothing else. With it off, a write to a sequence route is turned away by
    ``_sequence_write_refusal`` with a flash message, which is a refusal for a
    reason section H is not about; leaving it on means the only thing left that
    can refuse a sequence write is the campaign rule.
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
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The hosted application, built exactly as staging builds it.

    ``base_url`` must be the trusted host, because the canonical-host middleware
    sits outside the authentication boundary and would reject any other host
    before the campaign decision this file is about was ever reached.

    Requests are issued against the application's own database session, not an
    overridden one, which is why every fixture below commits through
    ``SessionLocal``: a row written inside a rolled-back transaction does not
    exist as far as a ``TestClient`` request is concerned, and the page would
    404 in a way that looks like a routing bug.
    """

    for key, value in _env().items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    app = create_app(readiness_probe=_AlwaysReadyProbe())
    try:
        yield TestClient(
            app, base_url=ORIGIN, follow_redirects=False, raise_server_exceptions=False
        )
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Operator:
    """One signed-in account, and everything a later request needs to be it.

    ``tests/test_route_authorization.py`` returns only the CSRF token from its
    session helpers, because a role is all that file ever asks about. Ownership
    is per account, so the tests here also need the account's id — to write it
    into ``Campaign.created_by_user_id``, to name it in an assignment form — and
    the raw cookie, so that a session can be *resumed* rather than reissued. See
    ``_resume`` for why that distinction carries a test on its own.
    """

    user_id: str
    email: str
    csrf: str
    cookie: str


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


def _user_session(client: TestClient, email: str = "operator@vmr.example") -> _Operator:
    """An ordinary approved operator: a real account row, role USER."""

    account = seed_account(email=email)
    csrf = _attach_session(client, account.user_id, account.email)
    return _Operator(account.user_id, account.email, csrf, client.cookies[SESSION_COOKIE_NAME])


def _admin_session(client: TestClient, email: str = ADMIN_EMAIL) -> _Operator:
    account = seed_account(email=email, role="admin")
    csrf = _attach_session(client, account.user_id, account.email)
    return _Operator(account.user_id, account.email, csrf, client.cookies[SESSION_COOKIE_NAME])


def _resume(client: TestClient, operator: _Operator) -> None:
    """Put an operator's existing cookie back on the client, unchanged.

    Not the same as calling ``_user_session`` again, and the difference is the
    whole point of the revocation test: reissuing a cookie would prove that a
    *fresh* sign-in reflects the change, which is the easy half. What has to hold
    is that the cookie already in the browser stops working, because access is
    recomputed per request rather than copied into the session at sign-in.
    """

    client.cookies.set(SESSION_COOKIE_NAME, operator.cookie)


def _seed_campaign(name: str, *, owner: _Operator | None = None) -> uuid.UUID:
    """One committed campaign, owned by ``owner`` or by nobody.

    ``owner=None`` writes ``created_by_user_id IS NULL``, which is not a
    contrivance: every campaign that existed before this slice has exactly that,
    because nothing in the database recorded who made it.
    """

    with SessionLocal() as session:
        campaign = campaign_service.create_campaign(
            session,
            name=name,
            created_by_user_id=uuid.UUID(owner.user_id) if owner is not None else None,
        )
        session.commit()
        return campaign.id


def _seed_membership(campaign_id: uuid.UUID) -> uuid.UUID:
    """One committed Campaign Contact, whose id names campaign work without naming the campaign."""

    with SessionLocal() as session:
        contact = Contact(
            first_name="Grace",
            last_name="Hopper",
            email=f"grace-{uuid.uuid4().hex[:8]}@kiln.example",
            natural_key=f"grace|hopper|{uuid.uuid4()}",
        )
        session.add(contact)
        session.flush()
        membership = CampaignContact(campaign_id=campaign_id, contact_id=contact.id)
        session.add(membership)
        session.commit()
        return membership.id


def _scoped_request(**path_params: str) -> Request:
    """A request carrying nothing but the path parameters and a signed-in USER.

    The router-level gate reads the actor out of ``request.scope["state"]`` — the
    same place the authentication middleware writes it — and the path parameters
    out of ``request.scope["path_params"]``, which Starlette fills in during
    routing. Building the scope by hand is how a test can ask the dependency the
    question a real request asks it, without needing a route that a signed-in
    USER is allowed to reach in the first place. The keys below are exactly the
    ones ``actor_from_request`` reads.
    """

    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/campaign-contacts",
            "headers": [],
            "query_string": b"",
            "path_params": dict(path_params),
            "state": {},
        }
    )


def _as_user(request: Request, operator: _Operator) -> Request:
    """Put an enforced, signed-in USER into a hand-built request scope."""

    request.scope["state"] = {
        "auth_enforced": True,
        "operator_role": UserRole.USER.value,
        "operator_user_id": operator.user_id,
    }
    return request


@dataclass(frozen=True)
class _Sequence:
    """The ids a sequence review write names, detached from the session that made them."""

    campaign_id: uuid.UUID
    sequence_id: uuid.UUID
    version_ids_csv: str


def _seed_sequence(owner: _Operator) -> _Sequence:
    """One committed seven-message sequence on a campaign ``owner`` created.

    ``tests/gmail_factory.py`` is reused rather than reimplemented because it
    builds the whole chain the sequence routes validate — messages linked by
    predecessor, one current version each — and a hand-built pair of rows would
    be refused by the sequence service for reasons that have nothing to do with
    authorization.
    """

    with SessionLocal() as session:
        built = gmail_factory.build_sequence(session, owner_user_id=owner.user_id)
        detached = _Sequence(
            campaign_id=built.campaign.id,
            sequence_id=built.sequence.id,
            version_ids_csv=built.version_ids_csv,
        )
        session.commit()
        return detached


def _post(client: TestClient, path: str, operator: _Operator, **fields: str) -> httpx.Response:
    """A mutation carrying everything a real browser of ``operator`` would send.

    The per-session CSRF token and a same-origin ``Sec-Fetch-Site``, so that a
    403 below is the campaign decision rather than either layer of the
    cross-site defence answering first. ``require_csrf`` is declared *before*
    the campaign gate on the same router, so a missing token here would produce
    ``csrf_failed`` and the assertions would say so.
    """

    return client.post(
        path,
        data={"_csrf": operator.csrf, **fields},
        headers={"Sec-Fetch-Site": "same-origin"},
    )


def _assign(
    client: TestClient, admin: _Operator, campaign_id: uuid.UUID, subject: _Operator
) -> httpx.Response:
    return _post(
        client, f"/app/admin/campaigns/{campaign_id}/assign", admin, user_id=subject.user_id
    )


def _unassign(
    client: TestClient, admin: _Operator, campaign_id: uuid.UUID, subject: _Operator
) -> httpx.Response:
    return _post(
        client, f"/app/admin/campaigns/{campaign_id}/unassign", admin, user_id=subject.user_id
    )


def _assert_campaign_refused(response: httpx.Response, where: str) -> None:
    """A refusal that is the campaign rule, named as such.

    Status alone would be satisfied by three other refusals this application can
    produce at the same URL — ``admin_required``, ``csrf_failed`` and an expired
    session — so the body is what makes the assertion mean what it says.
    """

    assert response.status_code == 403, f"{where} -> {response.status_code} {response.text[:200]}"
    assert response.json()["error"] == "campaign_access_denied", f"{where} -> {response.text[:200]}"


# ---------------------------------------------------------------------------
# A. An administrator reaches every campaign
# ---------------------------------------------------------------------------


def test_an_administrator_sees_every_campaign_including_ones_they_did_not_create(
    client: TestClient,
) -> None:
    """Both list surfaces, and both of the cases an administrator is for.

    A campaign somebody else created and a campaign nobody owns are the two rows
    a per-user rule would hide, and hiding either would leave work that only an
    administrator can unblock with nobody able to see it.
    """

    creator = _user_session(client, email="creator@vmr.example")
    someone_elses = _seed_campaign("Nordic pharma outreach", owner=creator)
    ownerless = _seed_campaign("Legacy manufacturing list")

    admin = _admin_session(client)
    page = client.get("/app/campaigns")
    assert page.status_code == 200
    assert "Nordic pharma outreach" in page.text
    assert "Legacy manufacturing list" in page.text

    api = client.get("/api/campaigns")
    assert api.status_code == 200, api.text[:200]
    listed = {row["id"] for row in api.json()["campaigns"]}
    assert listed == {str(someone_elses), str(ownerless)}

    # Neither row could have been reached by ownership, which is what makes the
    # two assertions above about the administrator's role: one belongs to
    # somebody else and one belongs to nobody.
    with SessionLocal() as session:
        owners = {
            campaign.id: campaign.created_by_user_id
            for campaign in session.scalars(select(Campaign)).all()
        }
    assert owners[someone_elses] == uuid.UUID(creator.user_id)
    assert owners[ownerless] is None
    assert admin.user_id != creator.user_id


def test_a_historical_campaign_with_no_owner_is_an_administrators_alone(
    client: TestClient,
) -> None:
    """``created_by_user_id IS NULL`` grants nothing to a normal operator.

    This is the migration's shape rather than an edge case: every campaign that
    predates ownership has a NULL owner, and the failure direction chosen for
    them was refusal. A rule written as "the owner column does not match, so fall
    through" rather than "the owner column must match" would hand every
    historical campaign to every account in the deployment, and this is the test
    that fails when somebody writes it that way.
    """

    ownerless = _seed_campaign("Legacy manufacturing list")
    with SessionLocal() as session:
        seeded = session.get(Campaign, ownerless)
        assert seeded is not None
        assert seeded.created_by_user_id is None, "the row under test is not the historical shape"

    _admin_session(client)
    listed = client.get("/app/campaigns")
    assert "Legacy manufacturing list" in listed.text
    assert client.get(f"/app/campaigns/{ownerless}").status_code == 200

    _user_session(client, email="stranger@vmr.example")
    hidden = client.get("/app/campaigns")
    assert hidden.status_code == 200
    assert "Legacy manufacturing list" not in hidden.text
    _assert_campaign_refused(
        client.get(f"/app/campaigns/{ownerless}"), f"GET /app/campaigns/{ownerless}"
    )


# ---------------------------------------------------------------------------
# B. The two ways an ordinary operator earns a campaign
# ---------------------------------------------------------------------------


def test_a_user_who_created_a_campaign_sees_it_and_can_open_it(client: TestClient) -> None:
    """Creating one is the first of the two durable grants.

    Nothing else was written for this operator: no assignment row exists, so the
    only thing that can be answering is ``Campaign.created_by_user_id``.
    """

    creator = _user_session(client, email="creator@vmr.example")
    campaign_id = _seed_campaign("Nordic pharma outreach", owner=creator)

    listed = client.get("/app/campaigns")
    assert listed.status_code == 200
    assert "Nordic pharma outreach" in listed.text

    opened = client.get(f"/app/campaigns/{campaign_id}")
    assert opened.status_code == 200, opened.text[:200]
    assert "Nordic pharma outreach" in opened.text

    with SessionLocal() as session:
        assignments = session.scalars(select(CampaignUserAssignment)).all()
    assert assignments == [], "an assignment row would make this test prove the wrong grant"


def test_a_user_assigned_to_a_campaign_they_did_not_create_sees_it_and_can_open_it(
    client: TestClient,
) -> None:
    """The second grant, made by an administrator through the real screen.

    The assignment is posted rather than inserted, because the thing being
    proved is that the administrator's action is what opens the campaign — an
    inserted row would prove the query and skip the surface that writes it.
    """

    creator = _user_session(client, email="creator@vmr.example")
    campaign_id = _seed_campaign("Nordic pharma outreach", owner=creator)
    colleague = _user_session(client, email="colleague@vmr.example")

    # Before the assignment the colleague is a stranger to this campaign, which
    # is what makes the "after" assertions attributable to the assignment.
    _assert_campaign_refused(client.get(f"/app/campaigns/{campaign_id}"), "before the assignment")

    admin = _admin_session(client)
    granted = _assign(client, admin, campaign_id, colleague)
    assert granted.status_code == 303, granted.text[:200]

    _resume(client, colleague)
    listed = client.get("/app/campaigns")
    assert listed.status_code == 200
    assert "Nordic pharma outreach" in listed.text
    opened = client.get(f"/app/campaigns/{campaign_id}")
    assert opened.status_code == 200, opened.text[:200]
    assert "Nordic pharma outreach" in opened.text


def test_two_users_assigned_to_the_same_campaign_both_see_it(client: TestClient) -> None:
    """Assignment is many-to-many, and the second grant does not displace the first.

    Written as a row per person rather than a column on the campaign, so the
    interesting failure is the one where assigning a second colleague quietly
    replaces the first — which a shape holding one assignee would do without any
    error to show for it.
    """

    creator = _user_session(client, email="creator@vmr.example")
    campaign_id = _seed_campaign("Nordic pharma outreach", owner=creator)
    first = _user_session(client, email="first@vmr.example")
    second = _user_session(client, email="second@vmr.example")

    admin = _admin_session(client)
    assert _assign(client, admin, campaign_id, first).status_code == 303
    assert _assign(client, admin, campaign_id, second).status_code == 303

    for colleague in (first, second):
        _resume(client, colleague)
        listed = client.get("/app/campaigns")
        assert "Nordic pharma outreach" in listed.text, colleague.email
        opened = client.get(f"/app/campaigns/{campaign_id}")
        assert opened.status_code == 200, f"{colleague.email} -> {opened.status_code}"

    # And the creator did not lose anything by other people being let in.
    _resume(client, creator)
    assert client.get(f"/app/campaigns/{campaign_id}").status_code == 200


# ---------------------------------------------------------------------------
# C. Everybody else is refused, by list and by direct URL
# ---------------------------------------------------------------------------


def test_a_user_cannot_open_a_campaign_they_were_never_assigned(client: TestClient) -> None:
    """The direct URL, which is the only refusal that counts.

    Leaving the campaign out of the list is a courtesy to somebody who has no
    business with it. What has to hold is the second half: typing the id into
    the address bar is answered by the campaign rule, and the body says which
    rule it was so that a missing feature switch or a stale session cannot pass
    for a boundary.
    """

    creator = _user_session(client, email="creator@vmr.example")
    campaign_id = _seed_campaign("Nordic pharma outreach", owner=creator)

    stranger = _user_session(client, email="stranger@vmr.example")
    listed = client.get("/app/campaigns")
    assert listed.status_code == 200, listed.text[:200]
    assert "Nordic pharma outreach" not in listed.text

    _assert_campaign_refused(
        client.get(f"/app/campaigns/{campaign_id}"), f"GET /app/campaigns/{campaign_id}"
    )
    assert stranger.user_id != creator.user_id

    # The same request from the owner succeeds, so the refusal above is about
    # this account rather than about the campaign, the page or the switch.
    _resume(client, creator)
    assert client.get(f"/app/campaigns/{campaign_id}").status_code == 200


def test_a_user_is_refused_when_they_post_to_a_campaign_they_were_never_assigned(
    client: TestClient,
) -> None:
    """The proof that this is a server-side gate and not a filtered template.

    Both requests carry a live session cookie and that session's own CSRF token,
    with a same-origin fetch signal — everything a legitimate operator has
    except entitlement to this campaign. Turning execution on and saving the
    campaign's settings are the two mutations a hidden-in-HTML defence would
    leave wide open, since neither needs the page to have been rendered first.

    ``csrf_failed`` is the failure worth naming: ``require_csrf`` is declared
    ahead of the campaign gate on the same router, so a test that forgot the
    token would get a 403 for the wrong reason and look like a pass. Asserting
    the error body is what tells the two apart.
    """

    creator = _user_session(client, email="creator@vmr.example")
    campaign_id = _seed_campaign("Nordic pharma outreach", owner=creator)
    stranger = _user_session(client, email="stranger@vmr.example")

    _assert_campaign_refused(
        _post(client, f"/app/campaigns/{campaign_id}/lifecycle", stranger, action="start"),
        "POST lifecycle",
    )
    _assert_campaign_refused(
        _post(
            client, f"/app/campaigns/{campaign_id}/setup", stranger, name="Renamed by a stranger"
        ),
        "POST setup",
    )

    # Nothing was changed on the way to being refused, and the owner can still
    # perform the very same mutation — so the gate refused a caller, not a verb.
    with SessionLocal() as session:
        campaign = session.get(Campaign, campaign_id)
        assert campaign is not None
        assert campaign.name == "Nordic pharma outreach"
        assert campaign.execution_enabled is False

    _resume(client, creator)
    allowed = _post(client, f"/app/campaigns/{campaign_id}/lifecycle", creator, action="start")
    assert allowed.status_code == 303, allowed.text[:200]


# ---------------------------------------------------------------------------
# D. Revocation
# ---------------------------------------------------------------------------


def test_unassigning_takes_effect_on_the_very_next_request_without_a_new_sign_in(
    client: TestClient,
) -> None:
    """Access is recomputed per request, so revocation does not wait for an expiry.

    The colleague's cookie is never reissued between the two halves: the same
    twelve-hour session that opened the campaign is the one refused afterwards.
    If entitlement were copied into the session at sign-in — the obvious and
    wrong optimisation — this test would keep passing for as long as the cookie
    lived, which is exactly the window it exists to close.
    """

    creator = _user_session(client, email="creator@vmr.example")
    campaign_id = _seed_campaign("Nordic pharma outreach", owner=creator)
    colleague = _user_session(client, email="colleague@vmr.example")

    admin = _admin_session(client)
    assert _assign(client, admin, campaign_id, colleague).status_code == 303

    _resume(client, colleague)
    assert client.get(f"/app/campaigns/{campaign_id}").status_code == 200

    _resume(client, admin)
    revoked = _unassign(client, admin, campaign_id, colleague)
    assert revoked.status_code == 303, revoked.text[:200]

    _resume(client, colleague)
    _assert_campaign_refused(
        client.get(f"/app/campaigns/{campaign_id}"), "after the assignment was revoked"
    )
    assert "Nordic pharma outreach" not in client.get("/app/campaigns").text


# ---------------------------------------------------------------------------
# E. Only an administrator decides who is assigned
# ---------------------------------------------------------------------------


def test_only_an_administrator_can_change_who_a_campaign_is_assigned_to(
    client: TestClient, committed_session: Session
) -> None:
    """Refused twice, and both refusals are asserted because they cover different callers.

    Over HTTP the answer is ``admin_required`` rather than
    ``campaign_access_denied``, and that is correct rather than a leak: the
    assignment routes live under ``/app/admin``, which the authentication
    boundary withholds by path before routing, so the request never reaches the
    campaign rule. The service call underneath is what protects a *future*
    caller that arrives some other way — a worker, a script, a handler somebody
    mounts outside ``/app/admin`` — and it refuses on the actor rather than on
    the path.
    """

    creator = _user_session(client, email="creator@vmr.example")
    campaign_id = _seed_campaign("Nordic pharma outreach", owner=creator)
    stranger = _user_session(client, email="stranger@vmr.example")

    for verb in ("assign", "unassign"):
        refused = _post(
            client,
            f"/app/admin/campaigns/{campaign_id}/{verb}",
            stranger,
            user_id=stranger.user_id,
        )
        assert refused.status_code == 403, f"{verb} -> {refused.status_code}"
        assert refused.json()["error"] == "admin_required", refused.text[:200]

    with SessionLocal() as session:
        assert session.scalars(select(CampaignUserAssignment)).all() == []

    campaign = committed_session.get(Campaign, campaign_id)
    assert campaign is not None
    with pytest.raises(campaign_access.CampaignAccessError):
        campaign_access.assign_user(
            committed_session,
            campaign=campaign,
            user_id=uuid.UUID(stranger.user_id),
            actor=CampaignActor(user_id=uuid.UUID(stranger.user_id), role=UserRole.USER),
            actor_label=stranger.email,
        )

    # The same call from an administrator actor is accepted, so the refusal is
    # about the role and not about the arguments.
    granted = campaign_access.assign_user(
        committed_session,
        campaign=campaign,
        user_id=uuid.UUID(stranger.user_id),
        actor=CampaignActor(user_id=None, role=UserRole.ADMIN),
        actor_label="admin@vmr.example",
    )
    assert granted.user_id == uuid.UUID(stranger.user_id)


def test_assigning_the_same_person_twice_is_not_an_error_and_a_disabled_account_is_refused(
    committed_session: Session,
) -> None:
    """The two answers the assignment screen has to give a double-clicking operator.

    Idempotence is a product decision rather than a database accident: a form
    submitted twice must not produce an error page, so the second call returns
    the row the first one wrote instead of failing the unique constraint. The
    disabled account is the opposite case — assigning one would grant nothing,
    because the account cannot sign in — and it has to say so rather than write
    a row that looks like access.
    """

    owner = seed_account(email="creator@vmr.example")
    colleague = seed_account(email="colleague@vmr.example")
    disabled = seed_account(email="former@vmr.example", state="disabled")
    campaign = campaign_service.create_campaign(
        committed_session,
        name="Nordic pharma outreach",
        created_by_user_id=uuid.UUID(owner.user_id),
    )
    committed_session.commit()

    admin_actor = CampaignActor(user_id=None, role=UserRole.ADMIN)
    first = campaign_access.assign_user(
        committed_session,
        campaign=campaign,
        user_id=uuid.UUID(colleague.user_id),
        actor=admin_actor,
        actor_label="admin@vmr.example",
    )
    second = campaign_access.assign_user(
        committed_session,
        campaign=campaign,
        user_id=uuid.UUID(colleague.user_id),
        actor=admin_actor,
        actor_label="admin@vmr.example",
    )
    assert second.id == first.id
    committed_session.commit()
    assert (
        len(
            committed_session.scalars(
                select(CampaignUserAssignment).where(
                    CampaignUserAssignment.campaign_id == campaign.id
                )
            ).all()
        )
        == 1
    )

    with pytest.raises(campaign_access.CampaignAssignmentError):
        campaign_access.assign_user(
            committed_session,
            campaign=campaign,
            user_id=uuid.UUID(disabled.user_id),
            actor=admin_actor,
            actor_label="admin@vmr.example",
        )
    committed_session.rollback()
    assert (
        committed_session.scalar(
            select(CampaignUserAssignment).where(
                CampaignUserAssignment.campaign_id == campaign.id,
                CampaignUserAssignment.user_id == uuid.UUID(disabled.user_id),
            )
        )
        is None
    )


# ---------------------------------------------------------------------------
# F. The JSON API, and the layer the extension will reuse
# ---------------------------------------------------------------------------


def test_the_json_campaign_api_answers_to_the_same_authority_as_the_pages(
    client: TestClient,
) -> None:
    """Asserted in two places, because ``/api`` is guarded twice and only one is the point.

    Over HTTP a session-bearing USER is refused with ``admin_required``: the
    whole ``/api`` prefix is administrator-only, so the campaign rule is never
    consulted for them and pretending otherwise would be reading the wrong
    refusal. The claim that matters for this route is one layer down —
    ``campaigns.list_campaigns`` takes the actor as a required argument, and it
    is the same call the handler makes and the same seam the account-linked
    extension will arrive on. So the scoping is asserted directly there, where
    no path rule can be answering instead.
    """

    creator = _user_session(client, email="creator@vmr.example")
    theirs = _seed_campaign("Nordic pharma outreach", owner=creator)
    somebody_elses = _seed_campaign(
        "Benelux logistics", owner=_user_session(client, "other@vmr.example")
    )

    _admin_session(client)
    as_admin = client.get("/api/campaigns")
    assert as_admin.status_code == 200, as_admin.text[:200]
    assert {row["id"] for row in as_admin.json()["campaigns"]} == {
        str(theirs),
        str(somebody_elses),
    }

    _resume(client, creator)
    refused = client.get("/api/campaigns")
    assert refused.status_code == 403
    assert refused.json()["error"] == "admin_required", refused.text[:200]

    with SessionLocal() as session:
        as_user = campaign_service.list_campaigns(
            session,
            actor=CampaignActor(user_id=uuid.UUID(creator.user_id), role=UserRole.USER),
        )
        assert [overview.campaign.id for overview in as_user] == [theirs]
        as_admin_actor = campaign_service.list_campaigns(
            session, actor=CampaignActor(user_id=None, role=UserRole.ADMIN)
        )
        assert {overview.campaign.id for overview in as_admin_actor} == {theirs, somebody_elses}


# ---------------------------------------------------------------------------
# G. Ownership is recorded when the campaign is made
# ---------------------------------------------------------------------------


def test_creating_a_campaign_records_the_operator_who_created_it(client: TestClient) -> None:
    """Everything above rests on this column being written, so it is asserted directly.

    Every ownership rule in this file reads ``created_by_user_id``. If the create
    handler left it NULL — which is what it did before this slice, and what a
    worker or a local session still legitimately produces — a USER would create
    a campaign and immediately be unable to open it, and every other test here
    would still pass.
    """

    creator = _user_session(client, email="creator@vmr.example")
    created = _post(client, "/app/campaigns/new", creator, name="Nordic pharma outreach")
    assert created.status_code == 303, created.text[:200]

    with SessionLocal() as session:
        campaign = session.scalar(select(Campaign).where(Campaign.name == "Nordic pharma outreach"))
        assert campaign is not None
        assert campaign.created_by_user_id == uuid.UUID(creator.user_id)

    # And the operator can open what they just made, which is the behaviour the
    # column exists to produce.
    assert client.get(f"/app/campaigns/{campaign.id}").status_code == 200


# ---------------------------------------------------------------------------
# H. Review writes, which name a draft or a sequence rather than a campaign
# ---------------------------------------------------------------------------
# This section exists because of a real hole, found while writing the sections
# above and fixed afterwards.
#
# The router-level gate only fires on a ``{campaign_id}`` path parameter, and
# every route here is keyed by a draft version id or a sequence id instead. The
# review *page* was scoped from the start — its lists and its ``?draft=`` and
# ``?sequence=`` deep links all go through ``accessible_campaign_ids`` — so the
# ids were never on offer to somebody outside the campaign. That is the whole
# defence a filtered template gives, and it was the whole defence these writes
# had: a USER who obtained a draft id could POST it and the approval was
# recorded, with a valid session, a valid CSRF token and no relationship to the
# campaign at all.
#
# It matters more here than at most of the surface. Approval is the human
# authorisation the pipeline waits for — an approved draft asserts that a named
# person read this exact version and is willing for it to go out — so a wrong
# answer forges a signature rather than merely leaking a page.


def test_a_user_cannot_approve_a_sequence_from_a_campaign_they_were_never_assigned(
    client: TestClient,
) -> None:
    """The same rule where the id names a sequence, which resolves its campaign indirectly.

    Worth its own test rather than trusting the draft one: the sequence routes
    reach the campaign through two hops — version to sequence to campaign — and
    they run three other refusals of their own first, so a check placed after any
    of them would look like it worked while answering for another reason.

    ``version_ids`` carries the real current versions, so the bulk approval's own
    "this changed while you were reading it" refusal cannot be what answers, and
    ``FEATURES__EMAIL_SEQUENCES`` is on so the read-only refusal cannot either.
    Both would be a 303 with a flash rather than a 403, which is precisely why
    the assertion is on the status and the error body together.
    """

    creator = _user_session(client, email="creator@vmr.example")
    sequence = _seed_sequence(creator)
    stranger = _user_session(client, email="stranger@vmr.example")

    _assert_campaign_refused(
        _post(
            client,
            f"/app/review/sequence/{sequence.sequence_id}/approve",
            stranger,
            version_ids=sequence.version_ids_csv,
        ),
        f"POST /app/review/sequence/{sequence.sequence_id}/approve",
    )

    with SessionLocal() as session:
        assert session.scalars(select(EmailSequenceMessageReview)).all() == [], (
            "a human decision was recorded against a sequence in somebody else's campaign"
        )

    # The creator's own request goes through, so the seven messages were
    # approvable all along and the refusal above was about the caller.
    _resume(client, creator)
    approved = _post(
        client,
        f"/app/review/sequence/{sequence.sequence_id}/approve",
        creator,
        version_ids=sequence.version_ids_csv,
    )
    assert approved.status_code == 303, approved.text[:200]
    with SessionLocal() as session:
        assert len(session.scalars(select(EmailSequenceMessageReview)).all()) == 7


# ---------------------------------------------------------------------------
# I. A membership id names a campaign one hop away
# ---------------------------------------------------------------------------


def test_a_membership_keyed_route_is_scoped_by_the_campaign_it_belongs_to(
    client: TestClient,
) -> None:
    """Seven routes name campaign work by a Campaign Contact id and never spell the campaign out.

    ``/api/campaign-contacts/{id}`` and its pause, resume, archive, retry,
    pipeline and stage-skip siblings are the same class of surface as the review
    writes: an id that identifies one campaign's work without carrying the
    campaign id the router-level gate matches on. The gate now resolves the
    membership to its campaign, and this is the test that says so.

    **The main claim is asserted against the dependency rather than through
    ``/api``, and that is not a shortcut.** The whole ``/api`` prefix is
    administrator-only, so a signed-in USER is turned away by the authentication
    boundary before routing — the request never reaches the gate, and asserting
    a 403 there would be asserting ``admin_required`` and calling it campaign
    scoping. The layer below is where the rule actually lives, and it is also
    the layer that starts mattering the moment the extension carries a user
    identity, which is the change these routes are waiting on.

    The HTTP half is still asserted, honestly: an administrator session drives
    one of the routes and gets through, and a USER session gets the
    administrator refusal by name, so what each layer is answering is recorded
    rather than assumed.
    """

    creator = _user_session(client, email="creator@vmr.example")
    campaign_id = _seed_campaign("Nordic pharma outreach", owner=creator)
    membership_id = _seed_membership(campaign_id)
    colleague = _user_session(client, email="colleague@vmr.example")

    admin = _admin_session(client)
    as_admin = client.get(f"/api/campaign-contacts/{membership_id}")
    assert as_admin.status_code == 200, as_admin.text[:200]
    assert as_admin.json()["campaign_id"] == str(campaign_id)

    _resume(client, colleague)
    as_user = client.get(f"/api/campaign-contacts/{membership_id}")
    assert as_user.status_code == 403
    assert as_user.json()["error"] == "admin_required", as_user.text[:200]

    # The rule itself, asked the way a routed request asks it.
    request = _as_user(_scoped_request(campaign_contact_id=str(membership_id)), colleague)
    actor = CampaignActor(user_id=uuid.UUID(colleague.user_id), role=UserRole.USER)
    with SessionLocal() as session:
        assert campaign_access.may_access_campaign(session, campaign_id, actor) is False
        with pytest.raises(campaign_access.CampaignAccessError):
            campaign_access.require_campaign_path_access(request, session)

    _resume(client, admin)
    assert _assign(client, admin, campaign_id, colleague).status_code == 303

    with SessionLocal() as session:
        assert campaign_access.may_access_campaign(session, campaign_id, actor) is True
        # No exception: the same membership id, the same account, one assignment
        # later. A gate that refused everybody would fail here.
        campaign_access.require_campaign_path_access(request, session)


# ---------------------------------------------------------------------------
# J. What an account change does not do
# ---------------------------------------------------------------------------


def test_a_creator_keeps_access_when_an_assignment_row_for_them_is_removed(
    client: TestClient,
) -> None:
    """Unassigning must not take away access that never came from the assignment.

    Assigning somebody who already created the campaign is allowed and adds
    nothing, and an administrator tidying up afterwards is an ordinary thing to
    do. The trap is that "unassign" reads like "remove their access" — an
    implementation that deleted the row and then recomputed access from
    assignments alone would lock the campaign's own author out of it, and
    nothing in the UI would say why.

    The assignment row is deliberately gone by the end, so the access asserted
    here can only be coming from ``Campaign.created_by_user_id``.
    """

    creator = _user_session(client, email="creator@vmr.example")
    campaign_id = _seed_campaign("Nordic pharma outreach", owner=creator)

    admin = _admin_session(client)
    assert _assign(client, admin, campaign_id, creator).status_code == 303
    assert _unassign(client, admin, campaign_id, creator).status_code == 303

    with SessionLocal() as session:
        assert session.scalars(select(CampaignUserAssignment)).all() == [], (
            "the assignment row is meant to be gone — otherwise this proves the wrong grant"
        )

    _resume(client, creator)
    listed = client.get("/app/campaigns")
    assert listed.status_code == 200
    assert "Nordic pharma outreach" in listed.text
    assert client.get(f"/app/campaigns/{campaign_id}").status_code == 200
    # And a write, not only a read: creator access is whole rather than partial.
    assert (
        _post(
            client, f"/app/campaigns/{campaign_id}/lifecycle", creator, action="start"
        ).status_code
        == 303
    )


def test_disabling_an_account_transfers_neither_ownership_nor_assignments(
    client: TestClient,
) -> None:
    """Disabling revokes signing in. It does not touch who a campaign belongs to.

    The two are separate on purpose, and conflating them would be destructive in
    a way nobody could undo from the screen that caused it. If disabling an
    account cleared its ownership, the campaigns that person made would become
    ownerless — reachable by administrators, invisible to the colleague who
    takes the work over — and reactivating them tomorrow would not bring the
    access back. So the rule is that the rows are facts about the account and
    survive it being switched off, and reactivation restores exactly what was
    there.

    The revocation is asserted first, with the account's still-live cookie
    getting ``unauthorized`` rather than the campaign refusal, because otherwise
    "the data is untouched" would be a claim about a disabling that might not
    have happened.
    """

    creator = _user_session(client, email="creator@vmr.example")
    owned = _seed_campaign("Nordic pharma outreach", owner=creator)
    somebody_elses = _seed_campaign("Benelux logistics")

    admin = _admin_session(client)
    assert _assign(client, admin, somebody_elses, creator).status_code == 303

    with SessionLocal() as session:
        user = session.get(User, uuid.UUID(creator.user_id))
        assert user is not None
        users_service.set_state(session, user=user, state=UserState.DISABLED, actor=ADMIN_EMAIL)
        session.commit()

    # Signing in is what was revoked, and it was revoked on the next request:
    # a disabled account is not "signed in without a role", it is not signed in.
    _resume(client, creator)
    refused = client.get(f"/app/campaigns/{owned}")
    assert refused.status_code == 401
    assert refused.json()["error"] == "unauthorized", refused.text[:200]

    with SessionLocal() as session:
        campaign = session.get(Campaign, owned)
        assert campaign is not None
        assert campaign.created_by_user_id == uuid.UUID(creator.user_id), (
            "disabling the account moved the campaign's ownership"
        )
        assignments = session.scalars(
            select(CampaignUserAssignment).where(
                CampaignUserAssignment.user_id == uuid.UUID(creator.user_id)
            )
        ).all()
        assert [row.campaign_id for row in assignments] == [somebody_elses], (
            "disabling the account removed the assignments it held"
        )

    # Reactivation restores exactly the access the account had, which is the
    # claim the two assertions above are only worth making for. The cookie is
    # reissued because `set_state` bumps `auth_version` in both directions on
    # purpose — a reactivated account may sign in again, but the tab somebody
    # left open does not come back to life.
    with SessionLocal() as session:
        user = session.get(User, uuid.UUID(creator.user_id))
        assert user is not None
        users_service.set_state(session, user=user, state=UserState.ACTIVE, actor=ADMIN_EMAIL)
        session.commit()
        restored_version = user.auth_version

    _attach_session(client, creator.user_id, creator.email, auth_version=restored_version)
    reopened = client.get("/app/campaigns")
    assert reopened.status_code == 200, reopened.text[:200]
    assert "Nordic pharma outreach" in reopened.text
    assert "Benelux logistics" in reopened.text
    assert client.get(f"/app/campaigns/{owned}").status_code == 200
    assert client.get(f"/app/campaigns/{somebody_elses}").status_code == 200


# ---------------------------------------------------------------------------
# K. Where there are no accounts, nothing is withheld
# ---------------------------------------------------------------------------


def test_where_authentication_is_off_nothing_is_refused(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local development and the rest of the suite must be unaffected.

    With ``AUTH__ENABLED`` off there is no account directory, no role and no user
    id, so there is nobody for a campaign to belong to and nobody to withhold it
    from — the whole application is already unauthenticated, and a campaign rule
    cannot be the one thing holding a line nothing else holds. This mirrors the
    trade ``require_admin`` already made, and it is what keeps the thousands of
    existing tests that open campaign pages meaningful.

    Built with the ordinary unauthenticated client used by
    ``tests/test_v2_customer_ui.py`` rather than the hosted one above, because
    "no accounts" is a property of that configuration and not something a test
    can simulate on a hosted app.
    """

    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__AGENT_WORKBENCH", "true")
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    from app.api.deps import get_db

    app.dependency_overrides[get_db] = _override

    campaign = campaign_service.create_campaign(
        db_session, name="Nordic pharma outreach", created_by_user_id=None
    )
    other = campaign_service.create_campaign(db_session, name="Benelux logistics")
    db_session.commit()

    with TestClient(app) as plain:
        listed = plain.get("/app/campaigns")
        assert listed.status_code == 200
        assert "Nordic pharma outreach" in listed.text
        assert "Benelux logistics" in listed.text
        opened = plain.get(f"/app/campaigns/{campaign.id}")
        assert opened.status_code == 200
        assert plain.get(f"/app/campaigns/{other.id}").status_code == 200

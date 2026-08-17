"""Where account-linked extension authorization meets per-user campaign access.

Two slices land on the same request and neither one is the whole answer.

PR #275 made the extension's credential name a **VMR account** instead of an
installation: a ``vmre1`` token resolves to a row that resolves to a user, and
the middleware deliberately records that owner under
``EXTENSION_USER_ID_STATE_KEY`` rather than under ``operator_role`` /
``operator_user_id`` — so a capture token can never assert an operator's
authority anywhere. This branch made campaigns per-user: a normal ``USER`` sees a
campaign they created or were explicitly assigned, an ``ADMIN`` sees every one,
and everybody else sees nothing.

The composition of the two is the thing neither file proves on its own, and it
is what this file is for:

* ``tests/test_extension_account_linking.py`` proves the token is a real,
  revocable authorization for exactly four routes. It never asks *whose*
  campaigns come back.
* the campaign-access tests prove the scoping rules against session callers.
  They never ask what happens when the caller is a bearer token with no
  ``operator_role`` at all.

Read together, the risk is a specific and quiet one: an extension request has no
``operator_role``, so a reader could reasonably conclude the actor is
unidentified — which is exactly what ``UNIDENTIFIED_EXTENSION`` is, and what
``GET /api/campaigns`` still hands the *unscoped* list to for the legacy
``vmrx1`` path. If ``_extension_actor`` stopped reading the linked user, every
account-linked extension in the fleet would silently see every campaign in the
deployment, and every test in both files above would still pass. Test B below is
the one that fails.

Everything here runs over the real hosted middleware stack against the real
database, and reuses the harness in ``tests/test_extension_account_linking.py``
so that "a linked token" means the same thing here as it does there: a token
obtained by pressing consent through the real PKCE flow, never a fabricated row.

Additive only. Nothing in ``app/`` and no other test file is touched.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.core.auth.extension import EXTENSION_CAPTURE_CONTRACT
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.campaign import Campaign
from app.models.contact_capture import ContactCaptureSubmission
from app.models.enums import CaptureCampaignFilingStatus, UserRole
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.models.pipeline import CaptureCampaignFiling
from app.models.user import User
from app.services import campaigns as campaigns_service
from app.services.campaign_access import CampaignActor, assign_user, unassign_user
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.hosted_auth_factory import seed_account
from tests.test_extension_account_linking import (
    CAMPAIGNS_URL,
    CAPTURE_URL,
    EXTENSION_ORIGIN,
    LEGACY_CREDENTIAL,
    ORIGIN,
    _build,
    _connect,
    _disable,
    _env,
    _fresh_capture,
    _local_env,
)

LOCAL_ORIGIN = "http://localhost"

#: The refusal body the campaign layer produces, from ``app/main.py``'s handler.
#: Named here because half the assertions below are about *not* seeing it: a
#: refusal that came from the authentication boundary and a refusal that came
#: from the campaign rules mean opposite things, and a test that accepted either
#: one would pass for the wrong reason.
CAMPAIGN_REFUSAL_ERROR = "campaign_access_denied"

#: What the middleware says when an extension authorization is not (or is no
#: longer) good for anything at all.
MIDDLEWARE_REFUSAL = {
    "error": "unauthorized",
    "status": 401,
    "message": "An approved VMR operator session is required.",
}


@pytest.fixture()
def hosted(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The staging application, built exactly as the linking tests build it.

    No ``get_db`` override, for the reason that file gives: the link table, the
    ``users`` table and the campaign tables all have to be the same rows the
    routes read, or the composition under test is not being exercised at all.
    """

    client = _build(monkeypatch, _env(), base_url=ORIGIN)
    try:
        yield client
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}", "Origin": EXTENSION_ORIGIN}


def _make_campaign(name: str, *, owner_id: str | None = None) -> Campaign:
    """One campaign, owned by ``owner_id`` or by nobody, committed.

    Written through the campaign service rather than by inserting a row, so the
    ownership column these tests turn on is populated by the same code path the
    application uses.
    """

    with SessionLocal() as session:
        campaign = campaigns_service.create_campaign(
            session,
            name=name,
            created_by_user_id=uuid.UUID(owner_id) if owner_id else None,
        )
        session.commit()
        session.refresh(campaign)
        session.expunge(campaign)
        return campaign


def _admin_actor(admin_user_id: str) -> CampaignActor:
    return CampaignActor(user_id=uuid.UUID(admin_user_id), role=UserRole.ADMIN, enforced=True)


def _assign(campaign_id: uuid.UUID, *, user_id: str, by_admin_id: str) -> None:
    with SessionLocal() as session:
        campaign = session.get(Campaign, campaign_id)
        assert campaign is not None
        assign_user(
            session,
            campaign=campaign,
            user_id=uuid.UUID(user_id),
            actor=_admin_actor(by_admin_id),
            actor_label="assignment-test",
        )
        session.commit()


def _unassign(campaign_id: uuid.UUID, *, user_id: str, by_admin_id: str) -> bool:
    with SessionLocal() as session:
        campaign = session.get(Campaign, campaign_id)
        assert campaign is not None
        removed = unassign_user(
            session,
            campaign=campaign,
            user_id=uuid.UUID(user_id),
            actor=_admin_actor(by_admin_id),
            actor_label="assignment-test",
        )
        session.commit()
        return removed


def _promote_to_admin(user_id: str) -> None:
    """Make an account an administrator, after its extension was already linked.

    Done to the account rather than to the token deliberately: the role is read
    from the ``users`` table on every request, so this is the honest way to say
    "this token belongs to an administrator" — and it doubles as evidence that
    nothing about the role was baked into the token at issue time.
    """

    with SessionLocal() as session:
        user = session.get(User, uuid.UUID(user_id))
        assert user is not None
        user.role = UserRole.ADMIN
        session.commit()


def _campaign_names(response: Any) -> set[str]:
    return {row["name"] for row in response.json()["campaigns"]}


def _campaign_ids(response: Any) -> set[str]:
    return {str(row["id"]) for row in response.json()["campaigns"]}


def _list_campaigns(client: TestClient, access_token: str) -> Any:
    return client.get(CAMPAIGNS_URL, headers=_headers(access_token))


def _count(model: Any) -> int:
    with SessionLocal() as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def _capture_into(client: TestClient, access_token: str, campaign_id: uuid.UUID | None) -> Any:
    payload = _fresh_capture()
    payload["campaign_id"] = str(campaign_id) if campaign_id is not None else None
    return client.post(CAPTURE_URL, json=payload, headers=_headers(access_token))


def _body(response: Any) -> dict[str, Any]:
    """The response body as a dict, or a readable stand-in for a non-JSON one."""

    try:
        parsed = response.json()
    except ValueError:
        return {"error": "<not json>", "text": response.text[:200]}
    return parsed if isinstance(parsed, dict) else {"error": "<not an object>", "body": parsed}


# ---------------------------------------------------------------------------
# A. The baseline, so that every refusal below means something
# ---------------------------------------------------------------------------


def test_an_account_linked_token_still_authenticates_and_captures(hosted: TestClient) -> None:
    """The composition did not break the feature it composes with.

    Stated first and on purpose. Every other test in this file asserts a
    *refusal*, and a refusal is only evidence if the same token, the same
    origin and the same payload succeed when they are supposed to.
    """

    issued = _connect(hosted, email="baseline@vmr.example")

    listed = _list_campaigns(hosted, issued["access_token"])
    assert listed.status_code == 200, listed.text
    assert listed.json() == {"campaigns": []}

    captured = _capture_into(hosted, issued["access_token"], None)
    assert captured.status_code == 201, captured.text
    assert captured.json()["counts"]["submitted"] == 1
    assert _count(ContactCaptureSubmission) == 1


# ---------------------------------------------------------------------------
# B. A linked USER sees their own campaigns and nobody else's
# ---------------------------------------------------------------------------


def test_the_campaign_list_for_a_linked_user_omits_somebody_elses_campaign(
    hosted: TestClient,
) -> None:
    """The claim this whole file exists for.

    Three campaigns, one of each kind that matters: created by the linked
    operator, assigned to them, and belonging to a stranger. The extension asks
    the same route the panel asks, over the same middleware, with the same
    token — and the stranger's campaign is absent by id *and* by name, because a
    list that leaked either would be a list that let the operator name it in a
    capture.
    """

    issued = _connect(hosted, email="scoped-user@vmr.example")
    stranger = seed_account(email="stranger@vmr.example")
    admin = seed_account(email="scoping-admin@vmr.example", role="admin")

    mine = _make_campaign("Owned by the linked operator", owner_id=issued["user_id"])
    shared = _make_campaign("Assigned to the linked operator", owner_id=stranger.user_id)
    theirs = _make_campaign("Somebody else's campaign", owner_id=stranger.user_id)
    _assign(shared.id, user_id=issued["user_id"], by_admin_id=admin.user_id)

    listed = _list_campaigns(hosted, issued["access_token"])
    assert listed.status_code == 200, listed.text

    assert _campaign_names(listed) == {mine.name, shared.name}
    assert _campaign_ids(listed) == {str(mine.id), str(shared.id)}
    # The negative half, stated against the raw body: neither the id nor the
    # name of a campaign this operator cannot reach appears anywhere in it.
    assert str(theirs.id) not in listed.text
    assert theirs.name not in listed.text


# ---------------------------------------------------------------------------
# C. A linked ADMIN stays global
# ---------------------------------------------------------------------------


def test_the_campaign_list_for_a_linked_administrator_returns_every_campaign(
    hosted: TestClient,
) -> None:
    """ADMIN is global, and the role is read from the account on this request.

    The account is promoted *after* the link was issued, so nothing about the
    answer can have come from the token. It is also the narrow claim: the same
    request that returns every campaign still buys nothing outside the capture
    contract, which section G drives directly.
    """

    issued = _connect(hosted, email="scoped-admin@vmr.example")
    stranger = seed_account(email="admin-stranger@vmr.example")
    assigner = seed_account(email="admin-assigner@vmr.example", role="admin")

    mine = _make_campaign("Admin's own campaign", owner_id=issued["user_id"])
    shared = _make_campaign("Assigned to the admin", owner_id=stranger.user_id)
    theirs = _make_campaign("Belongs to a stranger entirely", owner_id=stranger.user_id)
    _assign(shared.id, user_id=issued["user_id"], by_admin_id=assigner.user_id)

    # As a normal USER, the stranger's campaign is not there.
    as_user = _list_campaigns(hosted, issued["access_token"])
    assert as_user.status_code == 200
    assert theirs.name not in _campaign_names(as_user)

    _promote_to_admin(issued["user_id"])

    as_admin = _list_campaigns(hosted, issued["access_token"])
    assert as_admin.status_code == 200, as_admin.text
    assert _campaign_names(as_admin) == {mine.name, shared.name, theirs.name}
    assert str(theirs.id) in _campaign_ids(as_admin)


# ---------------------------------------------------------------------------
# D. Visibility is computed per request, not baked into the token
# ---------------------------------------------------------------------------


def test_assigning_then_unassigning_changes_the_next_call_with_the_same_token(
    hosted: TestClient,
) -> None:
    """One token, three calls, three different answers.

    No refresh, no reconnect, no new consent: the same access token is presented
    every time. That is what makes "access is a row, evaluated now" a property of
    the code rather than a description of the happy path — a token that carried
    its own campaign list would answer all three calls identically.
    """

    issued = _connect(hosted, email="live-scope@vmr.example")
    stranger = seed_account(email="live-owner@vmr.example")
    admin = seed_account(email="live-admin@vmr.example", role="admin")
    campaign = _make_campaign("Assigned mid-session", owner_id=stranger.user_id)
    token = issued["access_token"]

    before = _list_campaigns(hosted, token)
    assert before.status_code == 200
    assert before.json() == {"campaigns": []}

    _assign(campaign.id, user_id=issued["user_id"], by_admin_id=admin.user_id)

    during = _list_campaigns(hosted, token)
    assert during.status_code == 200
    assert _campaign_names(during) == {campaign.name}
    assert _campaign_ids(during) == {str(campaign.id)}

    assert _unassign(campaign.id, user_id=issued["user_id"], by_admin_id=admin.user_id) is True

    after = _list_campaigns(hosted, token)
    assert after.status_code == 200
    assert after.json() == {"campaigns": []}
    assert campaign.name not in after.text


# ---------------------------------------------------------------------------
# E. Filing a capture fails closed
# ---------------------------------------------------------------------------


def test_filing_a_capture_into_an_unreachable_campaign_is_refused_and_writes_nothing(
    hosted: TestClient,
) -> None:
    """Hiding a campaign from a list is a courtesy; this is the control.

    The operator does not need the list to name a campaign — they can type the
    id. So the same submission is sent twice, differing in exactly one field: the
    campaign it names. The unreachable one is refused in the intake's own error
    shape (not a bare 403, so the extension renders it like any other rejected
    submission rather than discarding a good refresh token), and leaves no
    submission, no snapshot and no filing row behind. The reachable one is
    accepted and files.
    """

    issued = _connect(hosted, email="filing@vmr.example")
    stranger = seed_account(email="filing-owner@vmr.example")
    mine = _make_campaign("Filing target the operator owns", owner_id=issued["user_id"])
    theirs = _make_campaign("Filing target they may not use", owner_id=stranger.user_id)

    refused = _capture_into(hosted, issued["access_token"], theirs.id)
    assert refused.status_code == 403, refused.text
    # The intake contract's own body, and deliberately not the middleware's:
    # this is a capture that was authenticated and then refused on the campaign,
    # so it carries no `message` key and it is not a campaign-layer 403 either.
    assert refused.json() == {"error": "unauthorized", "status": 403}

    assert _count(ContactCaptureSubmission) == 0
    assert _count(LinkedInProfileSnapshot) == 0
    assert _count(CaptureCampaignFiling) == 0

    accepted = _capture_into(hosted, issued["access_token"], mine.id)
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["counts"]["campaign_filings_pending"] == 1
    assert _count(ContactCaptureSubmission) == 1

    with SessionLocal() as session:
        filing = session.scalars(select(CaptureCampaignFiling)).one()
        assert filing.requested_campaign_id == mine.id
        assert filing.status is CaptureCampaignFilingStatus.PENDING


# ---------------------------------------------------------------------------
# F. Killing the authority, not the campaign
# ---------------------------------------------------------------------------


def test_revoking_the_link_or_disabling_the_account_refuses_at_the_boundary(
    hosted: TestClient,
) -> None:
    """Two ways to end an extension's authority, and both are the same refusal.

    The assertion worth having is not "it was refused" but *who* refused it. A
    revoked link and a disabled account must be stopped by the authentication
    boundary — before any handler, with no campaign resolved and no campaign
    named — because a campaign-shaped refusal here would mean the token was still
    authenticating and merely lost a campaign.
    """

    revoked_link = _connect(hosted, email="revoke-scope@vmr.example")
    disabled_owner = _connect(
        hosted, email="disable-scope@vmr.example", installation_id="install-b"
    )
    _make_campaign("Visible to the revoked operator", owner_id=revoked_link["user_id"])
    _make_campaign("Visible to the disabled operator", owner_id=disabled_owner["user_id"])

    # Both are working authorizations first, or the refusals prove nothing.
    for issued in (revoked_link, disabled_owner):
        alive = _list_campaigns(hosted, issued["access_token"])
        assert alive.status_code == 200, alive.text
        assert len(alive.json()["campaigns"]) == 1

    ended = hosted.post("/extension/revoke", headers=_headers(revoked_link["access_token"]))
    assert ended.status_code == 204
    _disable(disabled_owner["user_id"])

    for issued in (revoked_link, disabled_owner):
        refused = _list_campaigns(hosted, issued["access_token"])
        assert refused.status_code == 401, refused.text
        assert refused.json() == MIDDLEWARE_REFUSAL
        assert CAMPAIGN_REFUSAL_ERROR not in refused.text

        capture = _capture_into(hosted, issued["access_token"], None)
        assert capture.status_code == 401, capture.text
        assert capture.json() == MIDDLEWARE_REFUSAL


# ---------------------------------------------------------------------------
# G. The token still buys exactly four routes
# ---------------------------------------------------------------------------

#: Surfaces a linked token must not reach, one per authority the capture
#: contract withholds: the operator admin workbench, its configuration screen,
#: Gmail, the account directory (read and write), agent control, and sending.
FORBIDDEN_SURFACE: tuple[tuple[str, str], ...] = (
    ("GET", "/admin"),
    ("GET", "/admin/configuration"),
    ("POST", "/gmail/connect"),
    ("GET", "/app/admin/users"),
    ("POST", "/app/admin/users/create"),
    ("POST", f"/app/admin/agents/{uuid.UUID(int=1)}/control"),
    ("POST", "/workbench/agents/sending/stop"),
)


def test_the_linked_token_reaches_the_four_contract_routes_and_nothing_else(
    hosted: TestClient,
) -> None:
    """Campaign scoping widened nothing, and the contract is still four rows.

    The table is asserted whole rather than sampled, so adding a fifth route —
    or a method to an existing one — fails here before it can be discovered in
    production. Then the interesting half: a token belonging to an
    **administrator**, who can see every campaign in the deployment, is driven at
    seven surfaces an administrator's *session* could reach. Every one is
    refused, and none of the refusals is a campaign refusal — which is the point.
    A campaign-shaped 403 would mean the handler had been entered and the
    extension had been treated as an operator whose campaign access simply fell
    short. These are refused before routing, for having no operator session at
    all.
    """

    assert dict(EXTENSION_CAPTURE_CONTRACT) == {
        "/api/intake/contact-captures": frozenset({"POST"}),
        "/api/contact-labels": frozenset({"GET"}),
        "/api/contacts/lookup": frozenset({"GET"}),
        "/api/campaigns": frozenset({"GET"}),
    }
    assert set(EXTENSION_CAPTURE_CONTRACT) == {
        "/api/intake/contact-captures",
        "/api/contact-labels",
        "/api/contacts/lookup",
        "/api/campaigns",
    }
    assert len(FORBIDDEN_SURFACE) == 7, sorted(FORBIDDEN_SURFACE)

    issued = _connect(hosted, email="contract-scope@vmr.example")
    _promote_to_admin(issued["user_id"])
    _make_campaign("Reachable by this administrator")

    # The contract route this file is about answers, so the refusals below are
    # about the *surface* and not about a token that stopped working.
    listed = _list_campaigns(hosted, issued["access_token"])
    assert listed.status_code == 200
    assert len(listed.json()["campaigns"]) == 1

    for method, path in FORBIDDEN_SURFACE:
        response = hosted.request(method, path, headers=_headers(issued["access_token"]), json={})
        assert response.status_code in {401, 403}, f"{method} {path} -> {response.status_code}"
        body = _body(response)
        assert body.get("error") != CAMPAIGN_REFUSAL_ERROR, f"{method} {path} -> {body}"
        assert "do not have access to this campaign" not in response.text, f"{method} {path}"
        assert body.get("error") in {
            "unauthorized",
            "admin_required",
            "cross_site_request_refused",
        }, f"{method} {path} -> {body}"


# ---------------------------------------------------------------------------
# H. The deliberate exception: the legacy local credential
# ---------------------------------------------------------------------------


def test_a_legacy_vmrx1_credential_still_gets_the_historical_unscoped_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one caller that is still handed every campaign, and why that is a test.

    A ``vmrx1`` credential names an installation and no account, so it resolves
    to ``UNIDENTIFIED_EXTENSION`` — which every restrictive rule in
    ``campaign_access`` refuses. ``GET /api/campaigns`` exempts exactly this case
    and keeps the pre-account answer, because the credential verifies only under
    ``APP_ENV=local`` and narrowing it would break the extension in the one place
    it is still allowed to be used.

    That exemption is a decision, not an accident, so it is pinned: campaigns
    owned by two different strangers and one owned by nobody all come back. If a
    later change narrows or removes it, this test fails and somebody reads this
    docstring instead of debugging a developer's extension.
    """

    local = _build(monkeypatch, _local_env(), base_url=LOCAL_ORIGIN)
    try:
        one = seed_account(email="legacy-one@vmr.example")
        two = seed_account(email="legacy-two@vmr.example")
        owned = _make_campaign("Legacy: owned by one", owner_id=one.user_id)
        other = _make_campaign("Legacy: owned by two", owner_id=two.user_id)
        orphan = _make_campaign("Legacy: owned by nobody")

        listed = local.get(
            CAMPAIGNS_URL,
            headers={"Authorization": f"Bearer {LEGACY_CREDENTIAL}", "Origin": EXTENSION_ORIGIN},
        )
        assert listed.status_code == 200, listed.text
        assert _campaign_names(listed) == {owned.name, other.name, orphan.name}
        assert _campaign_ids(listed) == {str(owned.id), str(other.id), str(orphan.id)}
    finally:
        get_settings.cache_clear()

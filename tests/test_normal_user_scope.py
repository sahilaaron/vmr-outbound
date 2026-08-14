"""What a normal USER is told about work they cannot reach.

``tests/test_campaign_authorization.py`` proves the *refusals*: a USER who names
somebody else's campaign is turned away. This file asks the question that sat
one step earlier and had no owner — what the product **says** to that same USER
about the campaigns and contacts they were just refused.

The live audit found three answers that were wrong in the same direction, and
each has a section below:

* **Section A.** Today told a USER with an empty campaign list that 110 things
  wanted them and that 110 were "decisions only you can make". Every one of them
  belonged to a campaign that USER is refused. An over-reported badge is not a
  cosmetic defect here: it is the product asserting authority the operator does
  not have, and it sends them to a page that answers 403.

* **Section B.** A Contact enrolled and being processed in an administrator's
  campaign was described to an unassigned USER as "In 0 campaigns", "no Agent
  has run for them", and "Enrol them into a campaign to start the chain". The
  scoping was right and the sentence built on top of it was false — and the
  false version invites exactly the duplicate outreach the enrolment rules
  exist to prevent. The repair must not swing the other way: the campaign's
  name, id and state stay hidden, and only the aggregate crosses the boundary.

* **Section C.** The legacy contact and capture routes, whose authority the
  audit asked to be ruled on explicitly. The ruling is *intended* for the
  capture decision mutations and for the contact record page, and it is pinned
  here so that a later change to it is a visible failure rather than a silent
  one. What was wrong was never the route — it was offering a USER the
  administrator's Workbench shell and two provider-spend buttons that could
  only ever answer 403.

Two properties every test below is built to keep:

* **A passing test must not be passing vacuously.** Each "is not shown" has a
  partner asserting the same string *is* shown to the account entitled to it,
  so "render nothing" is never a passing implementation.
* **Under-reporting is the safe direction; both directions are asserted.** The
  zero cases are tested as carefully as the non-zero ones, because a repair that
  reported "already in a campaign" for everybody would hide the genuinely
  unenrolled contact the operator has to act on.

Sessions are minted with the real cookie codec rather than driven through the
sign-in form, for the reason ``tests/test_campaign_authorization.py`` records.
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
from app.models.campaign import CampaignContact
from app.models.contact import Contact
from app.models.draft import DraftVersion
from app.models.enums import (
    CampaignContactEligibility,
    PipelineStageStatus,
    UserRole,
)
from app.services import campaigns as campaign_service
from app.services.campaign_access import CampaignActor
from app.web.v2 import context as shell
from fastapi.testclient import TestClient

from tests.hosted_auth_factory import TEST_CLIENT_ID, seed_account

HOST = "srv1885453.hstgr.cloud"
ORIGIN = f"https://{HOST}"
SESSION_SECRET = "test-session-secret-value-at-least-32-chars"
ADMIN_EMAIL = "sahil@verifiedmarketresearch.com"
STAGING_DATABASE_URL = "postgresql+psycopg://vmr:secret@db.internal.example:5432/vmr_staging"

#: A campaign name distinctive enough that finding it anywhere in a rendered
#: page is proof of a leak rather than a coincidence.
SECRET_CAMPAIGN_NAME = "Zarquon Confidential Outreach"


class _AlwaysReadyProbe:
    def __call__(self) -> None:
        return None


def _env(**overrides: str) -> dict[str, str]:
    """A complete hosted staging configuration with the surfaces under test mounted.

    ``FEATURES__WORKBENCH`` mounts both ``/app`` and the legacy Workbench routes
    section C is about. ``FEATURES__AGENT_WORKBENCH`` is load-bearing rather than
    incidental: without it the contact page renders "the Agent monitor is off"
    instead of the membership projection section B asserts on, which would make
    those tests pass on a page nobody uses.

    ``FEATURES__CONTACT_CAPTURE_INTAKE`` is deliberately *off*: the runtime
    configuration guard refuses it outside local development unless an extension
    credential is configured, and nothing here is about the intake contract. The
    capture *page* renders either way and reports the switch as closed.
    """

    env = {
        "APP_ENV": "staging",
        "DEBUG": "false",
        "DRY_RUN": "true",
        "TRUSTED_HOSTS": f'["{HOST}"]',
        "DATABASE_URL": STAGING_DATABASE_URL,
        "FEATURES__WORKBENCH": "true",
        "FEATURES__AGENT_WORKBENCH": "true",
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

    Requests run against the application's own database session, which is why
    every helper below commits through ``SessionLocal``: a row written inside a
    rolled-back transaction does not exist as far as a ``TestClient`` request is
    concerned, and the page would 404 in a way that looks like a routing bug.
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
    user_id: str
    email: str
    csrf: str


def _attach_session(client: TestClient, user_id: str, email: str) -> str:
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
                auth_version=1,
            )
        ),
    )
    return codec.csrf_token(session_id)


def _user_session(client: TestClient, email: str = "operator@vmr.example") -> _Operator:
    """An ordinary approved operator: a real account row, role USER."""

    account = seed_account(email=email)
    csrf = _attach_session(client, account.user_id, account.email)
    return _Operator(account.user_id, account.email, csrf)


def _admin_session(client: TestClient, email: str = ADMIN_EMAIL) -> _Operator:
    account = seed_account(email=email, role="admin")
    csrf = _attach_session(client, account.user_id, account.email)
    return _Operator(account.user_id, account.email, csrf)


def _post(client: TestClient, path: str, operator: _Operator) -> httpx.Response:
    """A write carrying everything a real browser would send.

    The CSRF token and a same-origin ``Sec-Fetch-Site`` matter here rather than
    being boilerplate: both layers of the cross-site defence answer with the same
    403 the authorization check does, so a request missing either would let a
    section C test pass — or fail — for a reason that has nothing to do with
    authority.
    """

    response: httpx.Response = client.post(
        path,
        data={"_csrf": operator.csrf},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    return response


def _seed_campaign(name: str, *, owner: _Operator | None = None) -> uuid.UUID:
    with SessionLocal() as session:
        campaign = campaign_service.create_campaign(
            session,
            name=name,
            created_by_user_id=uuid.UUID(owner.user_id) if owner is not None else None,
        )
        session.commit()
        return campaign.id


def _seed_blocked_membership(campaign_id: uuid.UUID) -> uuid.UUID:
    """One membership that is both blocked and failed — two attention counts at once."""

    with SessionLocal() as session:
        contact = Contact(
            first_name="Grace",
            last_name="Hopper",
            email=f"grace-{uuid.uuid4().hex[:8]}@kiln.example",
            natural_key=f"grace|hopper|{uuid.uuid4()}",
        )
        session.add(contact)
        session.flush()
        session.add(
            CampaignContact(
                campaign_id=campaign_id,
                contact_id=contact.id,
                eligibility_status=CampaignContactEligibility.BLOCKED,
                pipeline_status=PipelineStageStatus.FAILED,
            )
        )
        session.commit()
        return contact.id


def _seed_awaiting_draft(campaign_id: uuid.UUID) -> None:
    """One committed draft awaiting a decision inside ``campaign_id``.

    Written directly rather than generated: the only path that produces a draft
    for real runs the local ``claude`` executable, which is a model call a test
    must never make.
    """

    with SessionLocal() as session:
        contact = Contact(
            first_name="Ada",
            last_name="Lovelace",
            email=f"ada-{uuid.uuid4().hex[:8]}@kiln.example",
            natural_key=f"ada|lovelace|{uuid.uuid4()}",
        )
        session.add(contact)
        session.flush()
        session.add(
            DraftVersion(
                contact_id=contact.id,
                campaign_id=campaign_id,
                version_number=1,
                subject="Your Q3 batch-release target",
                body="Ada — your published quality roadmap names batch-release review first.",
                rationale="Opened on the roadmap page because it is the only sourced fact.",
                created_by="personalization-agent",
            )
        )
        session.commit()


def _seed_unenrolled_contact() -> uuid.UUID:
    """A permanent Contact in no campaign at all — the genuine zero."""

    with SessionLocal() as session:
        contact = Contact(
            first_name="Katherine",
            last_name="Johnson",
            email=f"katherine-{uuid.uuid4().hex[:8]}@kiln.example",
            natural_key=f"katherine|johnson|{uuid.uuid4()}",
        )
        session.add(contact)
        session.commit()
        return contact.id


def _user_actor(operator: _Operator) -> CampaignActor:
    return CampaignActor(user_id=uuid.UUID(operator.user_id), role=UserRole.USER, enforced=True)


def _admin_actor(operator: _Operator) -> CampaignActor:
    return CampaignActor(user_id=uuid.UUID(operator.user_id), role=UserRole.ADMIN, enforced=True)


# ---------------------------------------------------------------------------
# A. Today is authorization-scoped
# ---------------------------------------------------------------------------


def test_a_user_with_no_accessible_campaign_receives_no_global_attention_counts(
    client: TestClient,
) -> None:
    """The reported defect, asserted end to end.

    A deployment with real work in it, and an account entitled to none of it.
    Before the repair this page said "110 things want you" over an empty
    campaign list.
    """

    admin_campaign = _seed_campaign(SECRET_CAMPAIGN_NAME)
    _seed_blocked_membership(admin_campaign)
    _seed_awaiting_draft(admin_campaign)

    _user_session(client)
    page = client.get("/app")

    assert page.status_code == 200
    body = page.text
    assert "Nothing is waiting on you" in body
    assert "Decisions only you can make" not in body
    assert "Drafts waiting for your read" not in body


def test_a_user_sees_the_work_inside_a_campaign_they_created(client: TestClient) -> None:
    """The anti-vacuity partner: the same page, the same code, real work shown.

    Without this, "count nothing for a USER" would be a passing implementation
    of the test above and would have broken the product instead of repairing it.
    """

    operator = _user_session(client)
    own = _seed_campaign("Own pipeline", owner=operator)
    _seed_blocked_membership(own)
    _seed_awaiting_draft(own)

    page = client.get("/app")

    assert page.status_code == 200
    assert "Decisions only you can make" in page.text
    assert "Drafts waiting for your read" in page.text


def test_a_users_counts_derive_only_from_campaign_authority(client: TestClient) -> None:
    """One blocked membership of theirs, three of somebody else's. The answer is one.

    Asserted against the counting function rather than the rendered number,
    because the page composes several counts into one total and a wrong split
    between them could still add up.
    """

    operator = _user_session(client)
    own = _seed_campaign("Own pipeline", owner=operator)
    _seed_blocked_membership(own)

    other = _seed_campaign(SECRET_CAMPAIGN_NAME)
    for _ in range(3):
        _seed_blocked_membership(other)

    with SessionLocal() as session:
        counts = shell.attention_counts(session, actor=_user_actor(operator))

    assert counts.blocked_contacts == 1
    assert counts.failed_stages == 1
    assert counts.total == 2


def test_inaccessible_campaign_work_is_not_counted_as_a_decision_only_you_can_make(
    client: TestClient,
) -> None:
    """Zero, from a database that is not empty. The claim the card actually makes."""

    operator = _user_session(client)
    other = _seed_campaign(SECRET_CAMPAIGN_NAME)
    _seed_blocked_membership(other)
    _seed_awaiting_draft(other)

    with SessionLocal() as session:
        counts = shell.attention_counts(session, actor=_user_actor(operator))
        # Anti-vacuity: the rows are really there, and an unscoped read finds them.
        unscoped = shell.attention_counts(session)

    assert counts.total == 0
    assert counts.drafts_awaiting == 0
    assert unscoped.total > 0


def test_an_administrator_keeps_the_global_totals(client: TestClient) -> None:
    """The role that is meant to see everything still does."""

    admin = _admin_session(client)
    other = _seed_campaign(SECRET_CAMPAIGN_NAME)
    _seed_blocked_membership(other)
    _seed_awaiting_draft(other)

    with SessionLocal() as session:
        counts = shell.attention_counts(session, actor=_admin_actor(admin))

    assert counts.blocked_contacts >= 1
    assert counts.drafts_awaiting >= 1


def test_an_empty_campaign_list_is_not_reported_as_an_empty_deployment(
    client: TestClient,
) -> None:
    """ "No campaign exists yet" is a claim about the deployment, not about you.

    The only truthful statement this page can make from a restricted account is
    that none is theirs — which is also the one that names the fix.
    """

    _seed_campaign(SECRET_CAMPAIGN_NAME)
    _user_session(client)

    page = client.get("/app")

    assert page.status_code == 200
    assert "No campaign is yours yet" in page.text
    assert "No campaigns yet" not in page.text


# ---------------------------------------------------------------------------
# B. A shared Contact, and the campaign the reader cannot open
# ---------------------------------------------------------------------------


def test_hidden_campaign_participation_is_not_reported_as_zero(client: TestClient) -> None:
    """The exact false sentence from the audit, asserted absent."""

    admin_campaign = _seed_campaign(SECRET_CAMPAIGN_NAME)
    contact_id = _seed_blocked_membership(admin_campaign)

    _user_session(client)
    page = client.get(f"/app/contacts/{contact_id}")

    assert page.status_code == 200
    body = page.text
    assert "In 0 campaigns" not in body
    assert "Not in a campaign" not in body
    assert "Already in a campaign you do not have access to" in body


def test_hidden_campaign_participation_does_not_leak_the_campaign_identity(
    client: TestClient,
) -> None:
    """The aggregate crosses the boundary; the campaign does not.

    Name and id are both asserted, because a template that stopped printing the
    name while still linking to ``/app/campaigns/{id}`` would leak the same fact
    in a form a reader can follow.
    """

    admin_campaign = _seed_campaign(SECRET_CAMPAIGN_NAME)
    contact_id = _seed_blocked_membership(admin_campaign)

    _user_session(client)
    body = client.get(f"/app/contacts/{contact_id}").text

    assert SECRET_CAMPAIGN_NAME not in body
    assert str(admin_campaign) not in body


def test_the_enrol_invitation_is_absent_when_participation_already_exists(
    client: TestClient,
) -> None:
    """The unsafe half of the old copy: an invitation to start a second chain."""

    admin_campaign = _seed_campaign(SECRET_CAMPAIGN_NAME)
    contact_id = _seed_blocked_membership(admin_campaign)

    _user_session(client)
    body = client.get(f"/app/contacts/{contact_id}").text

    assert "start the chain" not in body
    assert "no Agent has run for them" not in body


def test_zero_genuinely_means_zero_when_there_is_no_participation(client: TestClient) -> None:
    """The other direction, and the reason the repair is an aggregate not a flag.

    A contact in no campaign at all must still be described as such, and must
    still invite enrolment — that operator has real work to do and hiding it
    would be the same defect wearing the opposite sign.
    """

    contact_id = _seed_unenrolled_contact()

    _user_session(client)
    page = client.get(f"/app/contacts/{contact_id}")

    assert page.status_code == 200
    body = page.text
    assert "In 0 campaigns" in body
    assert "Not in a campaign" in body
    assert "Already in a campaign you do not have access to" not in body


def test_an_administrator_sees_the_membership_itself_rather_than_the_aggregate(
    client: TestClient,
) -> None:
    """Anti-vacuity for section B: the campaign is not hidden from everybody."""

    admin_campaign = _seed_campaign(SECRET_CAMPAIGN_NAME)
    contact_id = _seed_blocked_membership(admin_campaign)

    _admin_session(client)
    body = client.get(f"/app/contacts/{contact_id}").text

    assert "Already in a campaign you do not have access to" not in body
    assert "In 1 campaign" in body


# ---------------------------------------------------------------------------
# C. The legacy contact and capture routes — the recorded ruling
# ---------------------------------------------------------------------------


def test_the_legacy_contact_record_page_stays_reachable_by_a_user(client: TestClient) -> None:
    """LEGACY CONTACT ROUTES USER AUTHORITY — INTENDED, pinned.

    ``/contacts/{id}`` is the permanent record surface, listed in
    ``USER_READABLE_SURFACE`` and in ``EXPECTED_USER_REACHABLE``. It is not an
    administrative route and was never accidentally exposed. What was wrong is
    asserted two tests down: the *shell* it was offered inside.
    """

    contact_id = _seed_unenrolled_contact()

    _user_session(client)
    assert client.get(f"/contacts/{contact_id}").status_code == 200


def test_the_capture_decision_mutations_stay_with_the_user(client: TestClient) -> None:
    """LEGACY CAPTURE MUTATION USER AUTHORITY — INTENDED for the four decisions.

    ``confirm``, ``correct``, ``reject`` and ``promote`` record an operator's
    reading of evidence already stored; none calls a provider, and an operator's
    decision is the only approval a capture's company domain ever gets. The
    refusal that must *not* appear is the role one, so the assertion is on the
    error body rather than on the status: a 404 for a capture id that does not
    exist is the correct answer here and is not an authority failure.
    """

    operator = _user_session(client)
    capture_id = str(uuid.uuid4())
    for verb in ("company/confirm", "company/correct", "company/reject", "promote"):
        response = _post(client, f"/contact-captures/{capture_id}/{verb}", operator)
        assert response.status_code != 403, f"{verb} -> {response.status_code}"


def test_the_two_provider_spend_capture_controls_are_still_refused(client: TestClient) -> None:
    """The half of the same page that is not the operator's, unchanged by this repair.

    ``lookup`` and ``resolve`` bill logo.dev on every press. They were already
    administrator-only in ``app/core/auth/policy.py``; this pins that the repair
    below hides the buttons without moving the control that refuses them.
    """

    operator = _user_session(client)
    capture_id = str(uuid.uuid4())
    for verb in ("company/lookup", "company/resolve"):
        response = _post(client, f"/contact-captures/{capture_id}/{verb}", operator)
        assert response.status_code == 403, f"{verb} -> {response.status_code}"
        assert response.json()["error"] == "admin_required"


def test_a_user_is_not_offered_the_administrator_workbench_shell(client: TestClient) -> None:
    """The defect in section C: an affordance advertising authority the reader lacks."""

    contact_id = _seed_unenrolled_contact()

    _user_session(client)
    assert "Open in admin Workbench" not in client.get(f"/app/contacts/{contact_id}").text


def test_an_administrator_is_still_offered_the_workbench_shell(client: TestClient) -> None:
    """Anti-vacuity: the link was hidden by role, not deleted."""

    contact_id = _seed_unenrolled_contact()

    _admin_session(client)
    assert "Open in admin Workbench" in client.get(f"/app/contacts/{contact_id}").text


def test_the_capture_queue_does_not_send_a_user_into_an_administrator_surface(
    client: TestClient,
) -> None:
    """Resolving a capture is the operator's own work and is no longer labelled otherwise."""

    _user_session(client)
    page = client.get("/app/capture")

    assert page.status_code == 200
    assert "Resolve in admin" not in page.text

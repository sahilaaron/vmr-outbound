"""Who may reach what, once signed in.

``tests/test_hosted_auth.py`` asks whether a caller is *anybody*.
``tests/test_user_accounts.py`` asks whether an administrator's own screen is
guarded. This file asks the question that sat between them and had no owner:
a real, approved, signed-in USER — with a live session cookie and a valid CSRF
token — types an administrator URL, or posts to one. Every router in the
application used to be session-gated and none was role-gated, so the honest
answer was "everything".

Everything here is a direct request. Hiding a link is a courtesy to somebody who
has no business on a page; the control is the refusal that comes back when they
ask for it anyway. So no test below asserts anything about navigation markup.

Two properties are worth more than the rest, and both are enumerations rather
than examples:

* **Section B, the provider-spend lockout.** A USER must not be able to spend
  money the deployment pays for: MillionVerifier credits, the credential that
  spends them, logo.dev company lookups, or metered model budget.
* **Section K, classification conformance.** The live router table is walked and
  compared against a hand-written list of what a USER may reach, so a router
  added next month fails this file until somebody decides which side it is on.

No test contacts a provider, and none contacts Google. The verification routes
are refused at the authorization boundary, which runs before routing, so no
handler that could reach MillionVerifier is ever entered.
"""

from __future__ import annotations

import copy
import json
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.core.auth.extension import EXTENSION_CAPTURE_CONTRACT, credential_digest
from app.core.auth.policy import (
    is_admin_only_request,
    is_anonymous_path,
    normalize_request_path,
)
from app.core.auth.session import SESSION_COOKIE_NAME, SessionCodec
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.main import create_app
from app.models.enums import UserRole
from app.models.user import User
from fastapi.testclient import TestClient

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

#: A syntactically valid identifier for the ``{param}`` segments in a route
#: template. Nothing is ever looked up by it: every assertion in sections A, B
#: and F is decided before routing, so the value only has to parse.
SAMPLE_ID = "00000000-0000-4000-8000-000000000001"

# The extension capture credential, in the shape `tests/test_extension_capture_auth.py`
# mints it. Copied rather than imported so that a change to that suite's fixtures
# cannot silently change what this one proves about the credential's narrowness.
EXTENSION_ID = "abcdefghijklmnopabcdefghijklmnop"
EXTENSION_ORIGIN = f"chrome-extension://{EXTENSION_ID}"
KEY_ID = "beta-laptop"
KEY_SECRET = "3fVQx8Zk2nLp7Rw6TyUiOaSdFgHjKlZxCvBnM4qWeRt"
BEARER = {"Authorization": f"Bearer vmrx1.{KEY_ID}.{KEY_SECRET}"}


class _AlwaysReadyProbe:
    def __call__(self) -> None:
        return None


def _env(**overrides: str) -> dict[str, str]:
    """A complete hosted staging configuration with the routers under test mounted.

    The feature switches are the ones that decide which routers exist, not which
    behaviour they have: without them the administrator surface this file is
    about would answer 404 and every assertion below would be vacuously true for
    the wrong reason. ``FEATURES__WORKBENCH`` carries both the customer-facing
    ``/app`` product and the operator Workbench, which is why there is no
    separate switch for the former.
    """

    env = {
        "APP_ENV": "staging",
        "DEBUG": "false",
        "DRY_RUN": "true",
        "TRUSTED_HOSTS": f'["{HOST}"]',
        "DATABASE_URL": STAGING_DATABASE_URL,
        "FEATURES__WORKBENCH": "true",
        "FEATURES__AGENT_WORKBENCH": "true",
        "FEATURES__COMPANY_INTELLIGENCE": "true",
        "FEATURES__MILLIONVERIFIER": "true",
        "FEATURES__EMAIL_SEQUENCES": "true",
        "FEATURES__GMAIL_DRAFTS": "true",
        "FEATURES__SELLER_KNOWLEDGE_BASE": "true",
        "FEATURES__CONTACT_CAPTURE_INTAKE": "true",
        "AUTH__ENABLED": "true",
        "AUTH__SESSION_SECRET": SESSION_SECRET,
        "AUTH__ALLOWED_OPERATOR_EMAILS": "[]",
        "AUTH__BOOTSTRAP_ADMIN_EMAIL": ADMIN_EMAIL,
        "AUTH__GOOGLE_CLIENT_ID": TEST_CLIENT_ID,
        "AUTH__GOOGLE_CLIENT_SECRET": "test-client-secret",
        "AUTH__PUBLIC_BASE_URL": ORIGIN,
        "EXTENSION_AUTH__ENABLED": "true",
        "EXTENSION_AUTH__CREDENTIALS": json.dumps([f"{KEY_ID}:{credential_digest(KEY_SECRET)}"]),
        "EXTENSION_AUTH__ALLOWED_ORIGINS": json.dumps([EXTENSION_ORIGIN]),
        # Account-linked extension authorization, which is what a hosted
        # deployment actually uses. The legacy `vmrx1` block above stays
        # configured on purpose: section G now asserts that it is inert here.
        "EXTENSION_AUTH__LINK_ENABLED": "true",
    }
    env.update(overrides)
    return env


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The hosted application, built exactly as staging builds it.

    ``base_url`` must be the trusted host: ``CanonicalTrustedHostMiddleware``
    sits outside the authentication boundary, so a request to any other host is
    rejected before the authorization decision this file is about is ever
    reached.

    ``raise_server_exceptions=False`` so that a handler blowing up on a
    deliberately empty administrator form body is reported as a 500 response
    rather than as an exception. Every assertion here is about *refusal*, and a
    500 is not one — turning it into a raised exception would make sections D and
    E fail for a reason neither is testing.
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


def _attach_session(client: TestClient, user_id: str, email: str, *, auth_version: int = 1) -> str:
    """Sign an account in through the real cookie codec and return its CSRF token.

    Deliberately not the login form: a test about *authorization* should not also
    be a test about signing in, and a form failure would look identical to the
    refusal this file exists to assert.
    """

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


def _user_session(client: TestClient, email: str = "operator@vmr.example") -> str:
    """An ordinary approved operator: a real account row, role USER."""

    account = seed_account(email=email)
    return _attach_session(client, account.user_id, account.email)


def _admin_session(client: TestClient, email: str = ADMIN_EMAIL) -> str:
    account = seed_account(email=email, role="admin")
    return _attach_session(client, account.user_id, account.email)


def _call(client: TestClient, method: str, path: Any, csrf: str) -> httpx.Response:
    """One request, with whatever a real browser would send for that method.

    A mutating request carries the per-session CSRF token and a same-origin
    ``Sec-Fetch-Site``, so that a refusal below is the administrator check rather
    than either layer of the cross-site defence answering first.
    """

    if method in {"GET", "HEAD"}:
        return client.request(method, path)
    return client.request(
        method,
        path,
        data={"_csrf": csrf},
        headers={"Sec-Fetch-Site": "same-origin"},
    )


def _fresh_capture() -> dict[str, Any]:
    payload = copy.deepcopy(PROFILE_SUBMISSION)
    payload["client_submission_id"] = str(uuid.uuid4())
    for capture in payload["contacts"]:
        capture["client_capture_id"] = str(uuid.uuid4())
    return payload


def _raw_url(spelling: str) -> httpx.URL:
    """A URL carrying ``spelling`` as written, rather than as the client prefers it.

    ``httpx`` resolves ``.`` and ``..`` segments while building a URL, so a
    plainly-written ``client.get("/app/../admin")`` never puts that spelling on
    the wire at all. Setting ``raw_path`` is what lets the shapes it *does*
    transmit — an empty leading segment, a trailing slash — reach the boundary
    unchanged.
    """

    return httpx.URL(ORIGIN).copy_with(raw_path=spelling.encode("ascii"))


# ---------------------------------------------------------------------------
# The enumerations this file asserts against
# ---------------------------------------------------------------------------

#: The administrator surface, as a caller would type it. Every entry must refuse
#: a USER and admit an ADMIN — sections A and E run the same list from both
#: sides, which is what stops "refuse everybody" from passing as a fix.
ADMIN_SURFACE: tuple[tuple[str, str], ...] = (
    # The Agent monitor inside the operator product. See
    # `test_the_agent_monitor_inside_the_product_is_administrator_only` for why
    # the read moved here alongside the control POST.
    ("GET", "/app/agents"),
    ("GET", "/admin"),
    ("GET", "/admin/configuration"),
    ("GET", "/admin/providers"),
    ("GET", "/admin/system"),
    ("GET", "/admin/diagnostics"),
    ("GET", "/admin/agents/studio"),
    ("POST", "/admin/agents/studio/verification/credentials/millionverifier"),
    ("POST", "/admin/agents/studio/verification/test"),
    ("POST", "/admin/agents/studio/verification/waterfalls"),
    ("POST", "/admin/agents/studio/personalization/policies"),
    ("POST", "/admin/company-intelligence/backfill"),
    ("GET", "/workbench"),
    ("POST", "/workbench/agents/sending/stop"),
    ("POST", f"/workbench/jobs/{SAMPLE_ID}/retry"),
    ("GET", "/docs"),
    ("GET", "/redoc"),
    ("GET", "/openapi.json"),
    ("GET", "/api/campaigns"),
    ("POST", "/api/campaigns"),
    ("PUT", "/api/agents/research/control"),
    ("GET", "/imports"),
    ("GET", "/local-tools"),
    ("POST", "/campaigns"),
    # The Campaign's website-research switch: the one verb on the operator's own
    # Setup page that authorises real outside work — website fetches and model
    # budget — for a whole cohort at once. The page around it stays
    # operator-readable.
    ("POST", f"/app/campaigns/{SAMPLE_ID}/setup/research"),
    # The Admin section inside the product: Agent controls, per-Campaign
    # diagnostics with re-runs and live opt-ins, and the suppression list.
    ("GET", "/app/admin"),
    ("GET", "/app/admin/agents"),
    ("POST", "/app/admin/agents/research/control"),
    ("GET", f"/app/admin/campaigns/{SAMPLE_ID}/diagnostics"),
    ("POST", f"/app/admin/campaigns/{SAMPLE_ID}/agents/research/live"),
    ("POST", f"/app/admin/campaigns/{SAMPLE_ID}/agents/research/rerun"),
    ("GET", "/app/admin/suppressions"),
)

#: Every route that can reach a paid provider, or rotate the credential that
#: pays it. Kept as its own list rather than folded into ``ADMIN_SURFACE``
#: because this one is about money rather than about tidiness.
#:
#: Three providers, not one. The list began as MillionVerifier only, which is
#: how the last three entries went unnoticed for a release: logo.dev is reached
#: from a capture page and the local Claude CLI from the Knowledge Base, and
#: neither looks like "verification" from the outside. Both are counted here now,
#: and ``tests/test_provider_spend_authorization.py`` proves the refusal with the
#: provider boundaries stubbed and counted rather than by reading this table.
PROVIDER_SPEND_SURFACE: tuple[tuple[str, str], ...] = (
    ("POST", "/verification/bulk"),
    ("POST", "/verification/run"),
    ("POST", "/verification/recover"),
    ("POST", f"/contacts/{SAMPLE_ID}/verify"),
    ("POST", "/admin/agents/studio/verification/test"),
    ("POST", "/admin/agents/studio/verification/credentials/millionverifier"),
    # logo.dev. Both send `force=True`, so each press bypasses the
    # one-lookup-per-company cache and buys a fresh billed lookup; nothing rate
    # limits them.
    ("POST", f"/contact-captures/{SAMPLE_ID}/company/lookup"),
    ("POST", f"/contact-captures/{SAMPLE_ID}/company/resolve"),
    # Metered model spend, through a subprocess spawned with operator-supplied
    # URLs and `WebSearch` enabled.
    ("POST", "/knowledge-base/generate"),
)

#: Reads the operator product links to from pages a USER works on every day.
#: Withholding these would break the product without protecting anything: none
#: of them spends money, changes a guardrail, or names an administrator.
USER_READABLE_SURFACE: tuple[str, ...] = (
    "/verification",
    "/knowledge-base/restricted-claims",
    f"/contacts/{SAMPLE_ID}",
    "/companies",
)

#: The customer-facing product. A USER is the intended audience for all of it,
#: so a 403 anywhere here is a regression in the feature rather than a boundary.
OPERATOR_PRODUCT_SURFACE: tuple[tuple[str, str], ...] = (
    ("GET", "/app"),
    ("GET", "/app/campaigns"),
    ("GET", "/app/campaigns/new"),
    ("GET", "/app/people"),
    ("GET", "/app/companies"),
    ("GET", "/app/add-people"),
    ("GET", "/app/library"),
    ("GET", "/app/account/connections"),
    ("POST", "/gmail/connect"),
    ("POST", "/gmail/disconnect"),
)


# ---------------------------------------------------------------------------
# A. A USER at the administrative surface, by direct URL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), ADMIN_SURFACE, ids=lambda value: str(value))
def test_an_ordinary_operator_is_refused_from_the_administrative_surface(
    client: TestClient, method: str, path: str
) -> None:
    """Knowing the URL is not authority to use it.

    The session is real, the CSRF token is real, and the request is same-origin —
    everything an ordinary operator would legitimately have. What is missing is
    the role, and that is the only thing that may decide this.
    """

    csrf = _user_session(client)
    response = _call(client, method, path, csrf)
    assert response.status_code == 403, f"{method} {path} -> {response.status_code}"
    assert response.json()["error"] == "admin_required"


def test_the_administrative_surface_is_enumerated_rather_than_sampled() -> None:
    """Anti-vacuity: a list that quietly emptied would make section A pass."""

    assert ADMIN_SURFACE, "the administrator surface list is empty — the test proves nothing"
    for method, path in ADMIN_SURFACE:
        assert is_admin_only_request(path, method), f"{method} {path} is not administrator-only"


# ---------------------------------------------------------------------------
# B. The provider-spend lockout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), PROVIDER_SPEND_SURFACE, ids=lambda value: str(value))
def test_an_ordinary_operator_cannot_spend_provider_credits(
    client: TestClient, method: str, path: str
) -> None:
    """The most expensive thing a wrong answer here could cost.

    The staging host already holds a live MillionVerifier key, so this is a money
    boundary rather than a theoretical one: ``bulk`` enqueues up to 500 contacts
    in a single request, ``run`` drains the queue, ``recover`` returns leased
    jobs for another pass, and the credential route rotates the key all three
    spend against. Before the administrator surface existed, any signed-in
    account could reach every one of them.

    The same is true of the two other providers this list now covers: a capture's
    company lookup and resolve buttons bill logo.dev on every press, and Knowledge
    Base generation spends metered model budget through a spawned subprocess.

    Nothing here reaches a provider. The refusal happens in the authentication
    middleware, which runs before routing, so no handler that could open an
    outbound connection is ever entered. ``tests/test_provider_spend_authorization.py``
    proves that second claim directly, by counting calls at the provider
    boundaries instead of reasoning about where the refusal happened.
    """

    csrf = _user_session(client)
    response = _call(client, method, path, csrf)
    assert response.status_code == 403, f"{method} {path} -> {response.status_code}"
    assert response.json()["error"] == "admin_required"


def test_every_provider_spend_route_is_classified_as_administrator_only() -> None:
    """Anti-vacuity, and the same claim stated against the policy directly."""

    assert PROVIDER_SPEND_SURFACE, "the provider-spend list is empty — the test proves nothing"
    for method, path in PROVIDER_SPEND_SURFACE:
        assert is_admin_only_request(path, method), f"{method} {path} is not administrator-only"


def test_the_classification_counts_are_the_ones_deliberately_recorded() -> None:
    """The two enumerations, counted, so a silent edit to either is a failure.

    Both numbers moved once, in one direction, for one reason. The provider-spend
    list went from 6 to 9 and the operator-reachable list from 90 to 87, and it is
    the same three routes in both cases:

    * ``POST /contact-captures/{capture_id}/company/lookup``
    * ``POST /contact-captures/{capture_id}/company/resolve``
    * ``POST /knowledge-base/generate``

    The first two bill logo.dev on every press — both pass ``force=True``, so the
    one-lookup-per-company cache does not stand in the way and nothing rate-limits
    them. The third spawns the local Claude CLI with operator-supplied URLs and
    ``WebSearch`` enabled. All three were reachable by any signed-in account.

    Nothing else was reclassified. ``confirm``, ``correct``, ``reject`` and
    ``promote`` on the same capture page, and every manual knowledge-base write,
    stayed with the USER — a capture's company domain and the seller knowledge
    base have no approval other than an operator's, so withholding those would
    have broken the product rather than protected it.

    The operator-reachable count then moved a second time, 87 to 89, and in the
    other direction: ``GET`` and ``POST /extension/authorize`` were added by the
    extension account-linking slice. Both are per-operator consent pages, exactly
    like ``POST /gmail/connect``, and what they grant is strictly narrower than
    what the operator granting it already holds — the four routes of
    ``EXTENSION_CAPTURE_CONTRACT``, delegated, revocable, and dead the moment the
    account is disabled. The other two routes on that router,
    ``POST /extension/token`` and ``POST /extension/revoke``, are not counted
    here because they are classified as anonymous rather than as USER-reachable;
    ``tests/test_hosted_auth_templates.py`` records why.
    """

    assert len(PROVIDER_SPEND_SURFACE) == 9, sorted(PROVIDER_SPEND_SURFACE)
    # And a third time, 89 to 88, in the withholding direction: `GET /app/agents`
    # joined the control POST on the administrator surface. The monitor names
    # every campaign carrying an Agent override and lists jobs across all of
    # them, so it is not scoped to one operator's campaigns and cannot be
    # without rewriting the reader the administrator surfaces share. See
    # `test_the_agent_monitor_inside_the_product_is_administrator_only`, and note
    # that per-campaign Agent work is untouched under `/app/campaigns/{id}/...`.
    # And a fourth time, 88 to 94, with the Pass 2 shell: the Campaign workspace
    # tabs (people, setup, activity, add-people, lifecycle, setup POST), the
    # People/Library destinations and the account Connections page joined; the
    # legacy Emails/Contacts/Knowledge/Capture URLs stayed as redirects; and
    # `POST .../setup/research`, the Admin section, the global Agent controls
    # and the per-Campaign re-run / live routes are withheld under `/app/admin`.
    # And 94 to 100 with the inline sending desk: five explicit manual acts on
    # one email (actioned, edit, gmail-draft, skip, undo) and Today's dismiss.
    # And 100 to 101: adding existing people to a Campaign from the People list.
    assert len(EXPECTED_USER_REACHABLE) == 101, len(EXPECTED_USER_REACHABLE)


def test_reading_the_verification_page_is_not_spending(client: TestClient) -> None:
    """The split that makes section B narrow rather than a blanket ban.

    The operator agents page links to ``/verification``, so the page is product.
    Only the verbs that cost money were taken away.
    """

    csrf = _user_session(client)
    assert client.get("/verification").status_code != 403
    assert _call(client, "POST", "/verification/run", csrf).status_code == 403


# ---------------------------------------------------------------------------
# C. Reads stay with the operator where the product links to them
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", USER_READABLE_SURFACE)
def test_an_ordinary_operator_can_still_read_what_the_product_links_to(
    client: TestClient, path: str
) -> None:
    """Not asserted as 200 on purpose.

    Whether a contact record renders or 404s depends on data this test does not
    seed, and pinning that here would make an authorization test fail whenever a
    page's content changed. The claim is narrower and is the one that matters:
    the boundary does not stand in the way.
    """

    _user_session(client)
    response = client.get(path)
    assert response.status_code != 403, f"{path} -> {response.status_code} {response.text[:200]}"


def test_the_user_readable_surface_is_enumerated_rather_than_sampled() -> None:
    assert USER_READABLE_SURFACE, "the user-readable list is empty — the test proves nothing"
    for path in USER_READABLE_SURFACE:
        assert not is_admin_only_request(path, "GET"), f"GET {path} became administrator-only"


def test_restricted_claims_may_be_read_but_not_changed(client: TestClient) -> None:
    """KB-001's one asymmetry, stated in a single test.

    Restricted claims are the control that stops the product making a prohibited
    claim. Reading the list is ordinary operator work; deactivating an entry is
    lowering a guardrail. Every other knowledge-base section stays writable,
    because operator entry is the only approval the seller knowledge base has.
    """

    csrf = _user_session(client)
    assert client.get("/knowledge-base/restricted-claims").status_code != 403
    refused = _call(client, "POST", "/knowledge-base/restricted-claims", csrf)
    assert refused.status_code == 403
    assert refused.json()["error"] == "admin_required"
    # The neighbouring section is untouched, so the refusal above is about
    # restricted claims rather than about the knowledge base as a whole.
    assert _call(client, "POST", "/knowledge-base/personas", csrf).status_code != 403


# ---------------------------------------------------------------------------
# D. The operator product still works
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), OPERATOR_PRODUCT_SURFACE, ids=lambda value: str(value))
def test_the_operator_product_is_not_refused_to_the_operator(
    client: TestClient, method: str, path: str
) -> None:
    """The half of an authorization change that nobody writes tests for.

    A boundary that refuses everyone passes every test in sections A and B and
    ships a broken product. This is the counterweight: the whole customer-facing
    surface, asserted from an ordinary account.
    """

    csrf = _user_session(client)
    response = _call(client, method, path, csrf)
    assert response.status_code != 403, f"{method} {path} -> {response.status_code}"


def test_the_operator_product_surface_is_enumerated_rather_than_sampled() -> None:
    assert OPERATOR_PRODUCT_SURFACE, "the product list is empty — the test proves nothing"
    for method, path in OPERATOR_PRODUCT_SURFACE:
        assert not is_admin_only_request(path, method), f"{method} {path} became administrator-only"


def test_the_agent_monitor_inside_the_product_is_administrator_only(
    client: TestClient,
) -> None:
    """``/app/agents`` is withheld whole, read as well as write.

    This supersedes an earlier, narrower decision that withheld only the control
    POST and left the page readable so an operator could *see* which Agents were
    enabled. Two things changed under it.

    First, campaigns now have owners and assignees, and this page is not scoped
    to them: it names every campaign that carries an Agent override, and its job
    list carries contact and campaign rows from campaigns the reader may have no
    access to. Scoping the monitor would mean rewriting the reader that the
    administrator surfaces share; withholding it costs a normal operator nothing
    they cannot get from their own campaign page, which shows that campaign's
    stages, its Agents and its jobs.

    Second, "see which Agents are enabled" is being answered properly elsewhere:
    the Admin Configuration screen is where operational switches are read and
    changed, and it is administrator-only by the same rule.

    Per-campaign Agent work is deliberately untouched — rerun, override and stage
    actions live under ``/app/campaigns/{id}/...`` and stay with whoever the
    campaign is assigned to.
    """

    csrf = _user_session(client)
    for path in ("/app/agents", "/app/admin/agents"):
        read = client.get(path)
        assert read.status_code == 403, path
        assert read.json()["error"] == "admin_required"
    refused = _call(client, "POST", f"/app/admin/agents/{SAMPLE_ID}/control", csrf)
    assert refused.status_code == 403
    assert refused.json()["error"] == "admin_required"


# ---------------------------------------------------------------------------
# E. An administrator keeps everything
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), ADMIN_SURFACE, ids=lambda value: str(value))
def test_an_administrator_still_reaches_the_administrative_surface(
    client: TestClient, method: str, path: str
) -> None:
    """The proof that section A refuses a role rather than a route.

    Deliberately not asserted as 200: several of these are forms posted here with
    an empty body, so the honest expectation is "anything except the
    administrator refusal".
    """

    csrf = _admin_session(client)
    response = _call(client, method, path, csrf)
    assert response.status_code != 403, f"{method} {path} -> {response.status_code}"


# ---------------------------------------------------------------------------
# F. Alternate spellings of an administrator path
# ---------------------------------------------------------------------------

#: Spellings of ``/admin`` that a path-prefix check written against the literal
#: string would miss. ``normalize_request_path`` resolves every one of them
#: before the decision is made, which is why the policy can be written against a
#: single form.
ADMIN_SPELLINGS: tuple[str, ...] = (
    "//admin",
    "/admin/",
    "/app/../admin",
    "/static/../admin",
    "/admin/./configuration",
    "/./admin",
)


@pytest.mark.parametrize("spelling", ADMIN_SPELLINGS)
def test_an_alternate_spelling_does_not_reach_the_administrative_surface(
    client: TestClient, spelling: str
) -> None:
    """Over the wire, from an account that is signed in and still not an administrator.

    ``httpx`` resolves dot segments while building the URL, so some of these
    arrive already normalised — which is itself the point being pinned from the
    other side by the test below: a client that does *not* normalise must get the
    same answer, and that is the server's job rather than the client's.
    """

    _user_session(client)
    response = client.get(_raw_url(spelling))
    assert response.status_code == 403, f"{spelling} -> {response.status_code}"
    assert response.json()["error"] == "admin_required"


@pytest.mark.parametrize("spelling", ADMIN_SPELLINGS)
def test_the_policy_resolves_an_alternate_spelling_before_deciding(spelling: str) -> None:
    """The same claim where no HTTP client can have helped.

    A non-browser caller can put any of these on the wire verbatim, so the
    boundary must not depend on the client having tidied them up first.
    """

    assert normalize_request_path(spelling).startswith("/admin")
    assert is_admin_only_request(spelling, "GET"), f"{spelling} is not administrator-only"


def test_the_spelling_list_is_not_empty() -> None:
    assert ADMIN_SPELLINGS, "the spelling list is empty — the test proves nothing"


def test_an_unmounted_path_under_an_administrator_prefix_is_still_refused(
    client: TestClient,
) -> None:
    """The reason the administrator rule is a prefix while anonymity is exact.

    A prefix *withholds* access from routes that do not exist yet, so a router
    mounted under ``/admin`` next month is administrator-only the moment it is
    mounted. The decision runs before routing, so there is no window in which a
    new page is reachable and unguarded.
    """

    csrf = _user_session(client)
    for path in ("/admin/not-a-page", "/api/not-a-route", "/workbench/nothing/here"):
        response = _call(client, "GET", path, csrf)
        assert response.status_code == 403, f"{path} -> {response.status_code}"
        assert response.json()["error"] == "admin_required"


# ---------------------------------------------------------------------------
# G. The extension credential is unchanged, and still narrow
# ---------------------------------------------------------------------------


def _linked_extension_token(client: TestClient, *, email: str = "extension@vmr.example") -> str:
    """One account-linked extension access token, obtained the way a user does.

    Driven as real requests, like everything else in this file: an operator signs
    in, presses the consent button on ``/extension/authorize``, and the extension
    redeems the resulting single-use PKCE code at ``/extension/token``. The
    session cookie is cleared afterwards, so every assertion that follows is
    about the *extension's* authority and cannot be satisfied by the operator's
    cookie riding along.

    This replaced a hard-coded ``vmrx1`` credential here. The credential was not
    weakened, it was superseded: a `vmrx1` token is now inert outside
    ``APP_ENV=local`` (see ``test_the_legacy_shared_credential_is_inert_here``
    below), so the honest way to state "the capture contract still answers the
    extension" is with the credential the extension actually holds.
    """

    import base64
    import hashlib
    import secrets
    from urllib.parse import parse_qs, urlparse

    account = seed_account(email=email)
    csrf = _attach_session(client, account.user_id, account.email)
    verifier = secrets.token_urlsafe(32)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    installation_id = "install-route-authorization-0001"
    granted = client.post(
        "/extension/authorize",
        data={
            "extension_id": EXTENSION_ID,
            "installation_id": installation_id,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "state-routeauthorization",
            "redirect_uri": f"https://{EXTENSION_ID}.chromiumapp.org/",
            "_csrf": csrf,
        },
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert granted.status_code == 303, granted.text[:300]
    code = parse_qs(urlparse(granted.headers["location"]).query)["code"][0]
    client.cookies.clear()

    exchanged = client.post(
        "/extension/token",
        json={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "extension_id": EXTENSION_ID,
            "installation_id": installation_id,
        },
        headers={"Origin": EXTENSION_ORIGIN},
    )
    assert exchanged.status_code == 200, exchanged.text
    token: str = exchanged.json()["access_token"]
    return token


def test_the_capture_contract_still_answers_a_verified_extension_credential(
    client: TestClient,
) -> None:
    """``/api`` became administrator-only for *sessions*, not for the extension.

    An extension authorization carries no role — it is delegated from one account
    for one narrow purpose — so refusing it here for "not being an administrator"
    would have broken capture without making anything safer. It is checked before
    the administrator rule and is a narrower authority than any session.
    """

    headers = {
        "Authorization": f"Bearer {_linked_extension_token(client)}",
        "Origin": EXTENSION_ORIGIN,
    }
    for url in (
        "/api/contact-labels",
        "/api/contacts/lookup?linkedin_profile_url=https%3A%2F%2Fwww.linkedin.com%2Fin%2Fx",
        "/api/campaigns",
    ):
        response = client.get(url, headers=headers)
        assert response.status_code == 200, f"{url} -> {response.status_code} {response.text[:200]}"
        assert response.json() != {"error": "admin_required"}

    captured = client.post("/api/intake/contact-captures", json=_fresh_capture(), headers=headers)
    assert captured.status_code == 201, captured.text


def test_the_capture_contract_is_enumerated_and_not_empty() -> None:
    """Anti-vacuity for the loop above, read from the contract the boundary uses."""

    assert EXTENSION_CAPTURE_CONTRACT, "the capture contract is empty — the walk is broken"
    assert set(EXTENSION_CAPTURE_CONTRACT) == {
        "/api/intake/contact-captures",
        "/api/contact-labels",
        "/api/contacts/lookup",
        "/api/campaigns",
    }


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/admin"),
        ("POST", "/api/intake/linkedin-company/stage"),
    ],
)
def test_the_extension_credential_did_not_widen_with_the_administrator_rule(
    client: TestClient, method: str, path: str
) -> None:
    """A credential good for four routes is still good for exactly four routes.

    ``/api/intake/linkedin-company/stage`` is the interesting one: it sits under
    the administrator ``/api`` prefix, the extension does call it, and it is
    deliberately outside the bearer contract — the extension refuses to send it
    to a hosted backend at all, because company evidence capture is a local
    development path. Adding it to the contract to "fix" that would have widened
    extension authority for no live caller.
    """

    token = _linked_extension_token(client, email=f"narrow-{method}@x.test")
    headers = {"Authorization": f"Bearer {token}", "Origin": EXTENSION_ORIGIN}
    response = client.request(method, path, headers=headers, json={})
    assert response.status_code in {401, 403}, f"{method} {path} -> {response.status_code}"
    assert response.json()["error"] != "admin_required"


def test_the_legacy_shared_credential_is_inert_here(client: TestClient) -> None:
    """The `vmrx1` credential this file still configures is worth nothing.

    ``EXTENSION_AUTH__ENABLED``, the credential digest and the approved origin
    are all set in ``_env()`` above, and the request below is perfectly formed.
    It is refused anyway, because the legacy shared-secret scheme is gated on
    ``APP_ENV=local`` — so nothing in this hosted build's capture path depends on
    a reusable secret a human could paste. ``tests/test_extension_account_linking.py``
    proves the other half: the same credential still works under ``APP_ENV=local``.
    """

    refused = client.get("/api/contact-labels", headers={**BEARER, "Origin": EXTENSION_ORIGIN})
    assert refused.status_code == 401
    assert refused.json()["error"] == "unauthorized"


# ---------------------------------------------------------------------------
# H. Role is read from the account record, per request
# ---------------------------------------------------------------------------


def test_a_demoted_administrator_loses_the_surface_on_the_very_next_request(
    client: TestClient,
) -> None:
    """A cookie must never be able to assert a privilege the directory withdrew.

    Role comes from the account row read on *this* request, never from the
    session, so a demotion applies immediately rather than at the next expiry and
    without anybody having to sign out. (A demotion through the service also
    bumps ``auth_version``, which ends the session outright; this test takes the
    role away by hand precisely so that the version stays valid and the role is
    the only thing that can decide.)
    """

    account = seed_account(email="admin-demoted@vmr.example", role="admin")
    seed_account(email="admin-standby@vmr.example", role="admin")
    _attach_session(client, account.user_id, account.email)
    assert client.get("/admin").status_code != 403

    with SessionLocal() as session:
        user = session.get(User, uuid.UUID(account.user_id))
        assert user is not None
        user.role = UserRole.USER
        session.commit()

    refused = client.get("/admin")
    assert refused.status_code == 403
    assert refused.json()["error"] == "admin_required"


# ---------------------------------------------------------------------------
# I. Authentication still comes first
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/admin", "/api/campaigns", "/openapi.json", "/workbench"])
def test_an_anonymous_caller_is_refused_before_the_administrator_check(
    client: TestClient, path: str
) -> None:
    """401, never 403 — the ordering the two questions have to keep.

    Answering ``admin_required`` to a stranger would tell them the path exists
    and is administrative, and would send an operator whose session merely
    expired to a page telling them to ask for a promotion. The existing
    assertions in ``tests/test_hosted_auth.py`` say the same thing about the
    anonymous half; this pins that the new rule did not get in front of them.
    """

    response = client.get(path)
    assert response.status_code == 401, f"{path} -> {response.status_code}"
    assert response.json()["error"] == "unauthorized"


def test_an_anonymous_browser_navigation_is_still_sent_to_sign_in(client: TestClient) -> None:
    """The other anonymous shape, so the ordering holds for a real browser too."""

    response = client.get("/admin", headers={"accept": "text/html"})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/login")


# ---------------------------------------------------------------------------
# J. An account that no longer holds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["disabled", "unknown"])
def test_a_disabled_or_unknown_account_is_refused_on_an_administrator_path(
    client: TestClient, state: str
) -> None:
    """Neither may be answered with the administrator refusal.

    A cookie whose account is disabled, or names a row that does not exist, is
    not "a signed-in operator lacking a role" — it is not signed in at all, and
    the response has to say so and clear the cookie rather than imply that a
    promotion would help.
    """

    if state == "disabled":
        account = seed_account(email="gone@vmr.example", role="admin", state="disabled")
        _attach_session(client, account.user_id, account.email)
    else:
        _attach_session(client, str(uuid.uuid4()), "nobody@vmr.example")

    response = client.get("/admin")
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


# ---------------------------------------------------------------------------
# K. Classification conformance against the live router table
# ---------------------------------------------------------------------------

# Every route/method pair an ordinary operator may reach, written out here by
# hand. Changing this list is a security decision, which is exactly why it is
# recorded in a test rather than derived from the thing it is supposed to check
# — a derived list would agree with any policy, including a broken one.
#
# The residual risk the administrator rule leaves is that a brand-new *top-level*
# surface defaults to USER, because it matches no administrator prefix. This is
# what catches that: a route added without a decision appears here as a failure
# naming the exact path, and somebody has to choose a side before it ships.
EXPECTED_USER_REACHABLE: frozenset[str] = frozenset(
    {
        # The landing redirect into the customer-facing product.
        "GET /",
        # `/app` and everything under it, which is the product itself:
        # Today · Campaigns · People · Library, Add people, the account's own
        # connections, and the legacy redirects that resolve old bookmarks.
        # `/app/admin/**` and `POST .../setup/research` are deliberately absent.
        "GET /app",
        "GET /app/",
        "GET /app/account/connections",
        "GET /app/add-people",
        "GET /app/campaigns",
        "GET /app/campaigns/new",
        "POST /app/campaigns/new",
        "GET /app/campaigns/{campaign_id}",
        "GET /app/campaigns/{campaign_id}/activity",
        "GET /app/campaigns/{campaign_id}/add-people",
        "GET /app/campaigns/{campaign_id}/edit",
        "GET /app/campaigns/{campaign_id}/imports",
        "POST /app/campaigns/{campaign_id}/imports",
        "GET /app/campaigns/{campaign_id}/imports/staged/{staged_id}",
        "POST /app/campaigns/{campaign_id}/imports/staged/{staged_id}/confirm",
        "POST /app/campaigns/{campaign_id}/imports/staged/{staged_id}/discard",
        "GET /app/campaigns/{campaign_id}/imports/{batch_id}",
        "POST /app/campaigns/{campaign_id}/lifecycle",
        # The inline sending desk: explicit manual acts on one email of one
        # ready person, all scoped by the Campaign path guard.
        "POST /app/campaigns/{campaign_id}/desk/{membership_id}/{position}/actioned",
        "POST /app/campaigns/{campaign_id}/desk/{membership_id}/{position}/edit",
        "POST /app/campaigns/{campaign_id}/desk/{membership_id}/{position}/gmail-draft",
        "POST /app/campaigns/{campaign_id}/desk/{membership_id}/{position}/skip",
        "POST /app/campaigns/{campaign_id}/desk/{membership_id}/{position}/undo",
        # Today's per-user "hide this card until tomorrow".
        "POST /app/today/dismiss",
        "GET /app/campaigns/{campaign_id}/people",
        "GET /app/campaigns/{campaign_id}/setup",
        "POST /app/campaigns/{campaign_id}/setup",
        "GET /app/companies",
        "GET /app/companies/{company_id}",
        "GET /app/library",
        "GET /app/library/{section}",
        "GET /app/people",
        "POST /app/people/add-to-campaign",
        "GET /app/people/{contact_id}",
        "POST /app/review/sequence/messages/{version_id}/approve",
        "POST /app/review/sequence/messages/{version_id}/discard",
        "POST /app/review/sequence/messages/{version_id}/edit",
        "POST /app/review/sequence/{sequence_id}/approve",
        "POST /app/review/sequence/{sequence_id}/gmail-drafts",
        # Legacy customer URLs, all redirects into the destinations above.
        "GET /app/analytics",
        "GET /app/capture",
        "GET /app/contacts",
        "GET /app/contacts/{contact_id}",
        "GET /app/knowledge",
        "GET /app/knowledge/{section}",
        "GET /app/replies",
        "GET /app/review",
        "GET /app/sending",
        "GET /app/sequences",
        "GET /app/suppressions",
        # Gmail mailbox authorization. Connecting a mailbox is a per-operator
        # consent, not an administrative act, and the callback belongs to
        # whoever started it. `/app/admin/**` and `POST /app/agents/{id}/control`
        # are deliberately absent from the block above for the opposite reason.
        "POST /gmail/connect",
        "GET /gmail/callback",
        "POST /gmail/disconnect",
        # Extension account linking. Connecting a browser extension to one's own
        # VMR account is a per-operator consent, exactly like connecting a
        # mailbox above, and it is not an administrative act: what it grants is
        # strictly narrower than what the operator granting it already has — the
        # four routes of `EXTENSION_CAPTURE_CONTRACT`, delegated from their own
        # account, revocable by them, and dead the moment their account is
        # disabled. Withholding it from a USER would mean only an administrator
        # could use the capture extension, which is the opposite of the product.
        #
        # `POST /extension/token` and `POST /extension/revoke` are absent because
        # they are classified as anonymous (see
        # `tests/test_hosted_auth_templates.py`, which records exactly why) and
        # the walk below skips anonymous paths. Both are authorised by a
        # presented token plus an approved extension origin rather than by a
        # session, and neither can grant anything the authorize pages did not.
        "GET /extension/authorize",
        "POST /extension/authorize",
        # Extension capture review: the operator's own queue of what they
        # captured, and the labels and notes they put on it.
        "GET /captures/{capture_id}",
        "POST /captures/{capture_id}/labels",
        "POST /captures/{capture_id}/labels/{slug}/remove",
        "POST /captures/{capture_id}/notes",
        "GET /contact-captures/pending",
        "GET /contact-captures/submissions/{submission_id}",
        "GET /contact-captures/{capture_id}",
        # `company/lookup` and `company/resolve` are absent on purpose, and are
        # the reason this list is three entries shorter than it was: both call
        # logo.dev with `force=True`, so a USER holding the capture page could
        # bill the deployment once per click with nothing in the way. What is
        # left here is the operator's own judgement about evidence already
        # stored -- confirming a candidate, correcting a decision, rejecting one,
        # and promoting the capture. Each was read before being left: none of
        # them reaches a provider, and a capture's company domain has no approval
        # other than an operator's.
        "POST /contact-captures/{capture_id}/company/confirm",
        "POST /contact-captures/{capture_id}/company/correct",
        "POST /contact-captures/{capture_id}/company/reject",
        "POST /contact-captures/{capture_id}/promote",
        # Contacts. `POST /contacts/{contact_id}/verify` is absent on purpose:
        # it is the one route here that spends a MillionVerifier credit.
        "GET /contacts",
        "POST /contacts/add-to-campaign",
        "GET /contacts/{contact_id}",
        "POST /contacts/{contact_id}/generate-candidates",
        "POST /contacts/{contact_id}/labels",
        "POST /contacts/{contact_id}/labels/{slug}/remove",
        "POST /contacts/{contact_id}/notes",
        # Companies and the immutable evidence snapshots behind them, all reads.
        "GET /companies",
        "GET /companies/{company_id}",
        "GET /company-profiles/{snapshot_id}",
        "GET /profiles/{snapshot_id}",
        # The verification page is linked from the operator agents screen. Only
        # the page: `bulk`, `run` and `recover` are administrator-only.
        "GET /verification",
        # The seller knowledge base. Operator entry is the only approval it has,
        # so a USER may write every section of it except restricted claims,
        # whose GET stays here and whose writes do not.
        #
        # `POST /knowledge-base/generate` is the second exception and is also
        # absent deliberately. It is not operator entry at all: it spawns the
        # local Claude CLI with operator-supplied URLs and `WebSearch` enabled,
        # which is metered spend, a fetch primitive, and a prompt-injection sink
        # whose output lands in the knowledge base outreach copy is written from.
        # Everything a USER needs in order to fill the knowledge base by hand
        # stays below.
        "GET /knowledge-base",
        "GET /knowledge-base/company",
        "POST /knowledge-base/company",
        "GET /knowledge-base/offerings",
        "POST /knowledge-base/offerings",
        "GET /knowledge-base/offerings/{offering_id}",
        "POST /knowledge-base/offerings/{offering_id}",
        "POST /knowledge-base/offerings/{offering_id}/links",
        "POST /knowledge-base/offerings/{offering_id}/state",
        "GET /knowledge-base/personas",
        "POST /knowledge-base/personas",
        "POST /knowledge-base/personas/{persona_id}",
        "POST /knowledge-base/personas/{persona_id}/state",
        "GET /knowledge-base/proof-points",
        "POST /knowledge-base/proof-points",
        "POST /knowledge-base/proof-points/{proof_point_id}",
        "POST /knowledge-base/proof-points/{proof_point_id}/state",
        "GET /knowledge-base/restricted-claims",
        # Ambiguous-import triage. Reached from a decision card on the
        # operator's own campaign page, and the confirmation is deliberately
        # a human's because merging the wrong two records is not reversible
        # by a retry. Not the legacy twin of `/app/review` it resembles.
        "GET /review",
        "GET /review/rows/{row_id}",
        "POST /review/rows/{row_id}/preview",
        "POST /review/rows/{row_id}/resolve",
    }
)


def _concrete_path(template: str) -> str:
    """A route template with a value substituted for every ``{param}`` segment.

    The policy answers for a *request*, so it has to be asked about a path a
    caller could actually send. Substituting keeps the question honest: a rule
    written against a literal ``{contact_id}`` would match nothing real.
    """

    concrete = template
    while "{" in concrete:
        opening = concrete.index("{")
        closing = concrete.index("}", opening)
        concrete = concrete[:opening] + SAMPLE_ID + concrete[closing + 1 :]
    return concrete


def _live_route_table() -> dict[str, set[str]]:
    """Every path and method the built application actually serves.

    This FastAPI version wraps an included router in a single ``_IncludedRouter``
    object rather than flattening its routes onto the app, and the wrapper
    exposes the real router as ``original_router`` rather than as ``routes``. A
    naive ``app.routes`` walk therefore returns roughly twenty routes and misses
    everything this file is about, which would make the conformance test below
    pass while checking almost nothing.
    """

    app = create_app(readiness_probe=_AlwaysReadyProbe())
    served: dict[str, set[str]] = {}

    def collect(routes: Any) -> None:
        for route in routes:
            wrapped = getattr(route, "original_router", None)
            nested = getattr(wrapped, "routes", None) or getattr(route, "routes", None)
            if nested:
                collect(nested)
                continue
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if path and methods:
                served.setdefault(str(path), set()).update(str(m) for m in methods)

    collect(app.routes)
    return served


@pytest.fixture
def routes(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, set[str]]]:
    """The live table, built from the same configuration the client fixture uses."""

    for key, value in _env().items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    try:
        yield _live_route_table()
    finally:
        get_settings.cache_clear()


def test_the_route_walk_reaches_the_included_routers(routes: dict[str, set[str]]) -> None:
    """Anti-vacuity for the walk itself, before anything is asserted from it.

    Both checks exist because both have failed before: a broken unwrap returns a
    handful of top-level routes, and a route that is present but nested one level
    deeper than expected disappears silently.
    """

    assert routes, "no routes discovered — the walk is broken, not the app"
    assert len(routes) > 100, f"only {len(routes)} routes discovered — the unwrap is not working"
    for expected in ("/app/campaigns/{campaign_id}", "/admin/agents/studio", "/verification/bulk"):
        assert expected in routes, f"{expected} is served but the walk did not find it"


def test_every_live_route_is_classified_and_the_user_side_is_the_recorded_one(
    routes: dict[str, set[str]],
) -> None:
    """The test that fails when somebody adds a route and decides nothing.

    Every route/method pair the application serves is put to the policy, and the
    pairs it calls reachable by an ordinary operator are compared against the
    hand-written list above. There is no third answer: a pair is either
    administrator-only or recorded here as deliberately USER-reachable, and a new
    top-level surface — which matches no administrator prefix and therefore
    defaults to USER — shows up as an unexpected entry naming its own path.
    """

    assert routes, "no routes discovered — the walk is broken, not the app"

    reachable: set[str] = set()
    for template in sorted(routes):
        concrete = _concrete_path(template)
        for method in sorted(routes[template]):
            if is_anonymous_path(concrete):
                # Anonymity is a different question with its own conformance
                # test in `tests/test_hosted_auth_templates.py`. A probe and the
                # sign-in surface are reachable by everyone, which says nothing
                # about roles.
                continue
            if not is_admin_only_request(concrete, method):
                reachable.add(f"{method} {template}")

    assert reachable, "nothing is reachable by an operator — the classification is inverted"
    unexpected = reachable - EXPECTED_USER_REACHABLE
    missing = EXPECTED_USER_REACHABLE - reachable
    assert not unexpected, (
        "these routes are reachable by an ordinary operator and no decision was "
        f"recorded for them: {sorted(unexpected)}"
    )
    assert not missing, (
        "these routes were recorded as operator-reachable but the policy now "
        f"withholds them: {sorted(missing)}"
    )


def test_the_administrator_prefixes_and_exact_paths_are_the_ones_recorded_here() -> None:
    """The administrator surface named as a set, so a deletion is visible.

    Removing a prefix would silently hand a whole area back to every signed-in
    account, and the conformance test above would report it only as a long list
    of newly reachable routes. Naming the sets makes the cause obvious.
    """

    from app.core.auth.policy import admin_only_exact_paths, admin_only_prefixes

    assert admin_only_prefixes() == {
        "/admin",
        "/app/admin",
        "/app/agents",
        "/api",
        "/workbench",
        "/imports",
        "/campaigns",
        "/local-tools",
        "/docs",
    }
    assert admin_only_exact_paths() == {
        "/redoc",
        "/openapi.json",
        "/scoring",
        "/research",
        "/drafts",
        "/sequences",
        "/activity",
        "/settings",
    }


# ---------------------------------------------------------------------------
# L. Control characters in the path
# ---------------------------------------------------------------------------
# This section exists because of a real bypass, found by adversarial review
# after the boundary was written and before it was published.
#
# The policy decides by string comparison. Starlette's router decides by
# `re.match("^/admin$", path)`. Python's `$` matches at end-of-string *or*
# immediately before a single trailing newline, and uvicorn percent-decodes the
# request target before either matcher runs. So `GET /admin%0A` arrived as
# `/admin\n`, which `==` called "not the admin path" and the router called
# "/admin" -- and served the Workbench to any signed-in account.
#
# The fix refuses the whole control-character class rather than normalising the
# one spelling, because the bug is a disagreement between two matchers that were
# never written to agree, and a newline is only the spelling that happens to be
# reachable today.


CONTROL_CHARACTER_SUFFIXES: tuple[str, ...] = ("\n", "\r\n", "\t", "\x00", "\x0b", "\x0c", "\x7f")


@pytest.mark.parametrize("suffix", CONTROL_CHARACTER_SUFFIXES)
@pytest.mark.parametrize(
    "path",
    ["/admin", "/docs", "/redoc", "/openapi.json", "/campaigns", "/workbench", "/imports"],
)
def test_a_control_character_cannot_smuggle_a_path_past_the_admin_classification(
    path: str, suffix: str
) -> None:
    """The classification must not disagree with the router about what a path is.

    Asserted against the policy directly rather than over the wire, because a
    client library will not send these bytes: `httpx` rejects a raw newline in a
    URL, so the only honest place to pin this is where the decision is made.
    """

    assert is_admin_only_request(path + suffix, "GET") is True, (
        f"{path + suffix!r} was not classified as administrator-only; "
        "the router would still route it to the administrator surface"
    )


@pytest.mark.parametrize("suffix", CONTROL_CHARACTER_SUFFIXES)
def test_the_middleware_refuses_a_control_character_outright(suffix: str) -> None:
    """Refused as malformed, before any access decision is reached.

    Belt and braces with the normalisation above: one of them makes the two
    matchers agree, the other stops the request before it matters which.
    """

    from app.core.auth.middleware import _has_control_character

    assert _has_control_character("/admin" + suffix) is True


def test_an_ordinary_path_carries_no_control_character() -> None:
    """Anti-vacuity: the refusal must not be refusing everything."""

    from app.core.auth.middleware import _has_control_character

    ordinary = ["/app", "/app/review", "/admin", "/gmail/connect", "/static/app.css", "/healthz"]
    assert ordinary, "no paths checked — the guard is vacuous"
    for path in ordinary:
        assert _has_control_character(path) is False, path

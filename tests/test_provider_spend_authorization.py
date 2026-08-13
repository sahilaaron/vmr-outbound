"""What an ordinary operator can make the deployment pay for, proved by counting.

``tests/test_route_authorization.py`` asks the policy what it thinks. This file
asks the provider boundaries what happened, which is a different and stronger
question: a 403 alone does not prove nothing was bought, only that something was
refused *somewhere*. Every test here patches the last function before the wire —
``logodev._urllib_transport`` for logo.dev, ``subprocess.run`` for the local
Claude CLI — records every call, and asserts the list is empty on the USER path.

The two findings this file pins were both reachable by any signed-in account:

* **H-1, logo.dev.** ``POST /contact-captures/{id}/company/lookup`` and
  ``.../company/resolve`` both call the provider with ``force=True``, which is
  correct for a button an operator pressed deliberately and is exactly what makes
  them expensive: ``force`` bypasses the one-lookup-per-company cache, so N
  presses are N billed lookups. Nothing in the application rate-limits anything
  except sign-in.
* **H-2, the Claude CLI.** ``POST /knowledge-base/generate`` spawns the operator's
  ``claude`` executable with operator-supplied URLs and ``WebSearch`` enabled. It
  is metered spend, a fetch primitive, and a prompt-injection sink whose output
  is written into the knowledge base outreach copy is generated from.

Each refusal is paired with an administrator making the identical request and the
counter moving. Without that half, "zero calls" would also be satisfied by a
misconfigured fixture, an unmounted route, or a feature switch left off — and the
test would pass while proving nothing.

The neighbouring operator actions are asserted from the other side for the same
reason: ``confirm``, ``correct``, ``reject`` and ``promote`` are an operator's
judgement about evidence already stored, and a capture's company domain has no
approval other than theirs.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from collections.abc import Iterator
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from app.core.auth.extension import credential_digest
from app.core.auth.session import SESSION_COOKIE_NAME, SessionCodec
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.main import create_app
from app.services.enrichment import logodev
from app.services.thinking import claude_cli
from app.services.thinking.contracts import ThinkingRequest
from fastapi.testclient import TestClient

from tests.capture_factory import salesnav_capture
from tests.hosted_auth_factory import TEST_CLIENT_ID, seed_account

HOST = "srv1885453.hstgr.cloud"
ORIGIN = f"https://{HOST}"
SESSION_SECRET = "test-session-secret-value-at-least-32-chars"
ADMIN_EMAIL = "sahil@verifiedmarketresearch.com"
STAGING_DATABASE_URL = "postgresql+psycopg://vmr:secret@db.internal.example:5432/vmr_staging"

#: The extension capture credential, in the shape the hosted configuration check
#: demands. Nothing here presents it; see the note in ``_env``.
EXTENSION_ID = "abcdefghijklmnopabcdefghijklmnop"
EXTENSION_KEY_ID = "beta-laptop"
EXTENSION_KEY_SECRET = "3fVQx8Zk2nLp7Rw6TyUiOaSdFgHjKlZxCvBnM4qWeRt"

#: Not a credential. It is never sent anywhere: the transport that would carry it
#: is replaced in every test below, and its only job is to get past the
#: "is a key configured" guard so the *authorization* boundary is the thing being
#: measured rather than a missing key.
FAKE_LOGO_DEV_KEY = "not-a-real-key-transport-is-stubbed"

#: The one website the generation tests offer. Never fetched: the subprocess that
#: would fetch it is replaced.
SELLER_WEBSITE = "https://seller.example"


class _AlwaysReadyProbe:
    def __call__(self) -> None:
        return None


def _env() -> dict[str, str]:
    """Hosted staging, with every switch these two findings need turned on.

    The switches matter more here than in a pure policy test. With capture
    promotion off, or domain enrichment off, or no logo.dev key configured, the
    lookup handler returns early and never reaches the provider — so "zero
    outbound calls" would be true for a reason that has nothing to do with
    authorization, and the administrator half of each pair would prove it.
    """

    return {
        "APP_ENV": "staging",
        "DEBUG": "false",
        "DRY_RUN": "true",
        "TRUSTED_HOSTS": f'["{HOST}"]',
        "DATABASE_URL": STAGING_DATABASE_URL,
        "FEATURES__WORKBENCH": "true",
        "FEATURES__SELLER_KNOWLEDGE_BASE": "true",
        "FEATURES__CONTACT_CAPTURE_INTAKE": "true",
        "FEATURES__CONTACT_CAPTURE_PROMOTION": "true",
        "FEATURES__SALESNAV_DOMAIN_ENRICHMENT": "true",
        "FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION": "true",
        "LOGO_DEV_API_KEY": FAKE_LOGO_DEV_KEY,
        "AUTH__ENABLED": "true",
        "AUTH__SESSION_SECRET": SESSION_SECRET,
        "AUTH__ALLOWED_OPERATOR_EMAILS": "[]",
        "AUTH__BOOTSTRAP_ADMIN_EMAIL": ADMIN_EMAIL,
        "AUTH__GOOGLE_CLIENT_ID": TEST_CLIENT_ID,
        "AUTH__GOOGLE_CLIENT_SECRET": "test-client-secret",
        "AUTH__PUBLIC_BASE_URL": ORIGIN,
        # Not exercised here — no test in this file presents a bearer credential.
        # A hosted deployment refuses to start with capture intake enabled and no
        # extension credential configured, and capture intake is what puts a
        # capture in front of the operator in the first place, so the switch has
        # to be satisfiable for the application to build at all.
        "EXTENSION_AUTH__ENABLED": "true",
        "EXTENSION_AUTH__CREDENTIALS": json.dumps(
            [f"{EXTENSION_KEY_ID}:{credential_digest(EXTENSION_KEY_SECRET)}"]
        ),
        "EXTENSION_AUTH__ALLOWED_ORIGINS": json.dumps([f"chrome-extension://{EXTENSION_ID}"]),
    }


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The hosted application, built exactly as staging builds it."""

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
# The provider boundaries, replaced and counted
# ---------------------------------------------------------------------------


@pytest.fixture
def logo_dev_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every outbound logo.dev request this test would have made.

    Patched at ``_urllib_transport``, which is the function that opens the
    socket — not at the service above it. A stub higher up would count
    *intentions*; this counts the call itself, so anything that reaches the
    provider by any route through the enrichment services appears here.

    ``urlopen`` is replaced as well, as a backstop rather than as the mechanism:
    a future caller that passes its own transport would bypass the line above,
    and this file must never be able to make a real request by accident.
    """

    calls: list[str] = []

    def transport(url: str, headers: Any, timeout: float) -> logodev.RawResponse:
        calls.append(url)
        return logodev.RawResponse(
            status_code=200,
            body=json.dumps([{"domain": "quanthealth.example", "name": "QuantHealth"}]),
        )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        calls.append(str(args[0] if args else "unknown"))
        raise AssertionError("a real HTTP request escaped the stubbed transport")

    monkeypatch.setattr(logodev, "_urllib_transport", transport)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    return calls


@pytest.fixture
def claude_cli_spawns(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every process the Claude CLI thinker would have started.

    ``shutil.which`` is stubbed too, and deliberately: without it the thinker
    raises ``ThinkingUnavailable`` before ``subprocess.run`` is reached on a
    machine with no ``claude`` on PATH, and the spawn counter would read zero for
    a reason that has nothing to do with who asked.
    """

    spawns: list[list[str]] = []

    def run(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        spawns.append([str(part) for part in argv])
        return subprocess.CompletedProcess(argv, 0, stdout='{"unknowns": []}', stderr="")

    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(claude_cli.subprocess, "run", run)
    return spawns


# ---------------------------------------------------------------------------
# Sessions and requests
# ---------------------------------------------------------------------------


def _attach_session(client: TestClient, user_id: str, email: str) -> str:
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
                auth_version=1,
            )
        ),
    )
    return codec.csrf_token(session_id)


def _user_session(client: TestClient, email: str = "operator@vmr.example") -> str:
    account = seed_account(email=email)
    return _attach_session(client, account.user_id, account.email)


def _admin_session(client: TestClient, email: str = ADMIN_EMAIL) -> str:
    account = seed_account(email=email, role="admin")
    return _attach_session(client, account.user_id, account.email)


def _post(client: TestClient, path: str, csrf: str, **fields: str) -> httpx.Response:
    """One write, carrying what a real browser would send.

    Same-origin fetch metadata and the per-session token, so that every refusal
    below is the authorization decision rather than either layer of the
    cross-site defence answering first.
    """

    return client.post(
        path,
        data={**fields, "_csrf": csrf},
        headers={"Sec-Fetch-Site": "same-origin"},
    )


def _seed_capture() -> str:
    """One real capture with a company name, so a lookup has something to look up.

    Committed rather than flushed: the routes run through the application's own
    ``get_db`` dependency on a different session. The suite's truncation sweep
    removes the row afterwards.
    """

    with SessionLocal() as session:
        snapshot = salesnav_capture(session, company_name="QuantHealth")
        capture_id = str(snapshot.id)
        session.commit()
    return capture_id


# ---------------------------------------------------------------------------
# H-1 — logo.dev
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["lookup", "resolve"])
def test_an_ordinary_operator_cannot_buy_a_company_lookup(
    client: TestClient, logo_dev_calls: list[str], action: str
) -> None:
    """The finding, stated as money rather than as a status code.

    The session is real, the capture is real, the feature switches are on and a
    key is configured — everything the handler needs in order to spend. The only
    thing missing is the role, and the provider must never be reached.
    """

    csrf = _user_session(client)
    capture_id = _seed_capture()

    response = _post(client, f"/contact-captures/{capture_id}/company/{action}", csrf)

    assert response.status_code == 403, response.text[:200]
    assert response.json()["error"] == "admin_required"
    assert logo_dev_calls == [], f"the refusal still cost {len(logo_dev_calls)} lookup(s)"


def test_an_administrator_still_reaches_the_lookup_and_it_is_billed(
    client: TestClient, logo_dev_calls: list[str]
) -> None:
    """The other half, and the thing that makes the zero above mean something.

    Identical request, identical capture, identical switches — an administrator
    session instead of an ordinary one. The provider is called exactly once, which
    is what proves the assertion above is measuring a boundary rather than a
    fixture that could never have spent anything.
    """

    csrf = _admin_session(client)
    capture_id = _seed_capture()

    response = _post(client, f"/contact-captures/{capture_id}/company/lookup", csrf)

    assert response.status_code != 403, response.text[:200]
    assert len(logo_dev_calls) == 1, logo_dev_calls
    assert "QuantHealth" in unquote(logo_dev_calls[0])
    # The handler's own answer, not the boundary's: it redirects back to the
    # capture with a summary of what the lookup found.
    assert response.headers["location"].startswith(f"/contact-captures/{capture_id}")


def test_an_administrator_reaches_the_resolve_handler(
    client: TestClient, logo_dev_calls: list[str]
) -> None:
    """The second route, asserted on handler entry rather than on a call count.

    Automatic resolution is allowed to decide from stored evidence and skip the
    provider entirely — that is the whole point of DAT-017A — so pinning a call
    count here would pin policy internals this file has no business owning. What
    it does pin is that the route belongs to an administrator and answers them
    from the handler: the redirect target is one the boundary never produces.
    """

    csrf = _admin_session(client)
    capture_id = _seed_capture()

    response = _post(client, f"/contact-captures/{capture_id}/company/resolve", csrf)

    assert response.status_code != 403, response.text[:200]
    assert response.headers["location"].startswith(f"/contact-captures/{capture_id}")


def test_the_operator_keeps_every_decision_on_the_same_capture(
    client: TestClient, logo_dev_calls: list[str]
) -> None:
    """The counterweight, and the half of an authorization change nobody writes.

    Taking the whole capture page away from a USER would pass every assertion
    above and ship a product where the operator cannot do the one thing only they
    can do. ``confirm``, ``correct`` and ``reject`` record a judgement about
    candidates already stored, and ``promote`` acts on a decision already made;
    each was read before being left alone and none of them reaches a provider,
    which the empty call list at the end states rather than assumes.
    """

    csrf = _user_session(client)
    capture_id = _seed_capture()
    base = f"/contact-captures/{capture_id}/company"

    decisions = (
        (f"{base}/confirm", {"decision": "manual", "domain": "quanthealth.example"}),
        (f"{base}/correct", {"domain": "quanthealth.io", "note": "the right one"}),
        (f"{base}/reject", {"domain": "wrong.example", "reason": "different company"}),
        (f"/contact-captures/{capture_id}/promote", {}),
    )
    for path, fields in decisions:
        response = _post(client, path, csrf, **fields)
        assert response.status_code != 403, f"{path} -> {response.status_code}"

    assert logo_dev_calls == [], "an operator decision reached the provider"


# ---------------------------------------------------------------------------
# H-2 — the Claude CLI
# ---------------------------------------------------------------------------


def test_an_ordinary_operator_cannot_spawn_the_claude_cli(
    client: TestClient, claude_cli_spawns: list[list[str]]
) -> None:
    """Refused before a process exists, not after one has already fetched a URL.

    The executable is resolvable (``shutil.which`` is stubbed) and the form is
    the one the page submits, so a handler that ran would spawn. The empty list
    is the assertion; the 403 only says where the refusal came from.
    """

    csrf = _user_session(client)

    response = _post(client, "/knowledge-base/generate", csrf, websites=SELLER_WEBSITE)

    assert response.status_code == 403, response.text[:200]
    assert response.json()["error"] == "admin_required"
    assert claude_cli_spawns == [], f"{len(claude_cli_spawns)} subprocess(es) were started"


def test_an_administrator_still_reaches_generation_and_the_process_runs(
    client: TestClient, claude_cli_spawns: list[list[str]]
) -> None:
    """Identical request from an administrator: exactly one process is started.

    Without this the test above would also pass with the route unmounted, the
    Knowledge Base switched off, or the thinker never constructed.
    """

    csrf = _admin_session(client)

    response = _post(client, "/knowledge-base/generate", csrf, websites=SELLER_WEBSITE)

    assert response.status_code != 403, response.text[:200]
    assert len(claude_cli_spawns) == 1, claude_cli_spawns
    assert response.headers["location"].startswith("/knowledge-base")


def test_the_operator_can_still_fill_the_knowledge_base_by_hand(
    client: TestClient, claude_cli_spawns: list[list[str]]
) -> None:
    """KB-001's actual approval model, protected from the repair.

    Operator entry is the only approval the seller knowledge base has. Generation
    moved; typing did not, and neither did reading — a knowledge base a USER
    cannot fill is an empty one, and the personalization agent writes vague copy
    from an empty knowledge base rather than refusing.
    """

    csrf = _user_session(client)

    assert client.get("/knowledge-base").status_code != 403
    assert _post(client, "/knowledge-base/company", csrf, name="VMR").status_code != 403
    assert _post(client, "/knowledge-base/personas", csrf, name="Head of Ops").status_code != 403
    assert claude_cli_spawns == []


# ---------------------------------------------------------------------------
# Anti-vacuity for the file itself
# ---------------------------------------------------------------------------


def test_the_counted_boundaries_are_the_ones_the_application_uses(
    logo_dev_calls: list[str], claude_cli_spawns: list[list[str]]
) -> None:
    """The stubs are proved to be on the real path, without any HTTP involved.

    Every "zero calls" assertion above depends on the patch landing where the
    application would actually call out. If a refactor moved either boundary, the
    counters would read zero forever and every test in this file would pass while
    measuring nothing. So each one is exercised directly, once.
    """

    result = logodev.search_brands("QuantHealth", api_key=FAKE_LOGO_DEV_KEY)
    assert len(logo_dev_calls) == 1, logo_dev_calls
    assert result.candidates, "the stubbed transport was not parsed as candidates"

    answer = claude_cli.ClaudeCliThinker().think(
        ThinkingRequest(prompt="{}", purpose="boundary-check", timeout_seconds=5.0)
    )
    assert len(claude_cli_spawns) == 1, claude_cli_spawns
    assert answer.payload == {"unknowns": []}

"""Byte-level regressions for the authentication boundary.

Why this module exists separately from ``test_hosted_auth.py``
--------------------------------------------------------------
``TestClient`` speaks through ``httpx``, and ``httpx`` refuses to *construct*
several of the request shapes an attacker can trivially put on the wire: a
non-ASCII header value raises ``UnicodeEncodeError`` in the client before the
application is ever called. A suite that can only send what ``httpx`` will build
therefore cannot see an entire class of defect — which is exactly how the
independent hostile review reached an unhandled 500 (H-1) that 304 dedicated
tests had missed.

Everything here drives the ASGI application directly, so the bytes in the test
are the bytes the middleware reads. Each test names the review finding it pins.

The shared rule these tests exist to hold: **an attacker-controlled security
token that is malformed, oversized, duplicated or non-ASCII must produce an
ordinary refusal — 401 or 403 — and never an unhandled exception.** A control
that raises is a control that returns 500 instead of doing its job, and 500 is
one handler-ordering change away from becoming something worse.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from app.core.auth.config import AuthSettings
from app.core.auth.session import SESSION_COOKIE_NAME, OperatorSession, SessionCodec
from app.core.config import Settings
from app.main import create_app

SECRET = "raw-asgi-secret-raw-asgi-secret-raw-asgi-secret"
OPERATOR = "operator@vmr.example"
HOST = "vmr.raw.invalid"
BASE = f"https://{HOST}"
SESSION_ID = "sid-raw-asgi-1"

_APP: Any = None


def _app() -> Any:
    """One application instance for the module; nothing here mutates it."""

    global _APP
    if _APP is None:
        _APP = create_app(
            Settings(
                app_env="ci",
                trusted_hosts=(HOST,),
                features={"workbench": True},
                auth=AuthSettings(
                    enabled=True,
                    session_secret=SECRET,
                    google_client_id="raw.apps.googleusercontent.com",
                    google_client_secret="raw-client-secret",
                    allowed_operator_emails=(OPERATOR,),
                    public_base_url=BASE,
                    cookie_secure=True,
                ),
            ),
            readiness_probe=lambda: None,
        )
    return _APP


def call(
    method: str,
    path: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    body: bytes = b"",
) -> dict[str, Any]:
    """Drive the ASGI app with exact header bytes and report what came back.

    An exception escaping the application is captured rather than raised,
    because "unhandled exception" is the finding these tests are looking for and
    a test that merely errors would report it as a broken test.
    """

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", HOST.encode("ascii"))] + list(headers or []),
        "client": ("203.0.113.9", 55555),
        "server": (HOST, 443),
        "state": {},
    }

    messages: list[dict[str, Any]] = []
    delivered = {"done": False}

    async def receive() -> dict[str, Any]:
        if delivered["done"]:
            return {"type": "http.disconnect"}
        delivered["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    error: BaseException | None = None
    try:
        asyncio.run(_app()(scope, receive, send))
    except BaseException as exc:  # noqa: BLE001 - capturing it is the point
        error = exc

    start = next((m for m in messages if m["type"] == "http.response.start"), None)
    return {
        "status": start["status"] if start else None,
        "body": b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"),
        "error": error,
    }


def _session_cookie(*, email: str = OPERATOR, session_id: str = SESSION_ID, ttl: int = 3600) -> str:
    now = int(time.time())
    return SessionCodec(SECRET).encode_session(
        OperatorSession(
            email=email,
            subject="sub-raw-1",
            display_name="Operator",
            session_id=session_id,
            issued_at=now,
            expires_at=now + ttl,
        )
    )


def _csrf(session_id: str = SESSION_ID) -> str:
    return SessionCodec(SECRET).csrf_token(session_id)


def _signed_in_write(
    *, extra: list[tuple[bytes, bytes]], token: bytes | None = None
) -> dict[str, Any]:
    headers = [
        (b"cookie", SESSION_COOKIE_NAME.encode("ascii") + b"=" + _session_cookie().encode("ascii")),
        (b"content-type", b"application/json"),
    ]
    if token is not None:
        headers.append((b"x-csrf-token", token))
    return call("POST", "/api/campaigns", headers=headers + extra, body=b'{"name":"raw"}')


# ---------------------------------------------------------------------------
# H-1 — comparison paths fail closed, never 500
# ---------------------------------------------------------------------------

# Every one of these is a byte string `httpx` refuses to put in a header, and
# every one used to reach `hmac.compare_digest` as latin-1-decoded text.
HOSTILE_CSRF_TOKENS = [
    pytest.param("ééé".encode(), id="utf8-accented"),
    pytest.param(b"\xe9\xe9\xe9", id="latin1-high-bytes"),
    pytest.param(b"\x80\x81\x82", id="continuation-bytes"),
    pytest.param(b"\xff\xfe", id="byte-order-mark"),
    pytest.param("ｖ１".encode(), id="fullwidth"),
    pytest.param(b"\x00", id="nul"),
    pytest.param("é".encode() * 5000, id="oversized-non-ascii"),
    pytest.param(b"A" * 100_000, id="oversized-ascii"),
    pytest.param(b"", id="empty"),
]


@pytest.mark.parametrize("token", HOSTILE_CSRF_TOKENS)
def test_hostile_csrf_header_is_refused_not_a_crash(token: bytes) -> None:
    """H-1: the exact defect the review reproduced, plus its neighbours.

    403 specifically — the CSRF refusal — because the session is valid and it is
    the token that is wrong. A 500 here is the finding.
    """

    result = _signed_in_write(extra=[(b"origin", BASE.encode("ascii"))], token=token)
    assert result["error"] is None, f"unhandled {result['error']!r}"
    assert result["status"] == 403, result["status"]


# The session path survived H-1 only because `SimpleCookie` happens to drop
# morsels with non-ASCII values — incidental, not designed. These pin the
# designed behaviour so a future parser change cannot quietly reintroduce it.
HOSTILE_SESSION_COOKIES = [
    pytest.param(b"v1.aaaa.\xe9\xe9", id="non-ascii-signature"),
    pytest.param(b"v1.\xe9.aaaa", id="non-ascii-payload"),
    pytest.param(b"v1.aaaa.\x80\x81\x82", id="continuation-bytes"),
    pytest.param(b"v1.aaaa.\xff", id="high-byte"),
    pytest.param("v1.aaaa.é".encode(), id="utf8-accented"),
    pytest.param("v1.ｐayload.ｓig".encode(), id="fullwidth"),
    pytest.param(b"v1." + b"A" * 9000 + b".B", id="oversized"),
    pytest.param(b"v1.aaaa", id="two-part"),
    pytest.param(b"v2.aaaa.bbbb", id="wrong-version"),
    pytest.param(b"...", id="empty-segments"),
]


@pytest.mark.parametrize("value", HOSTILE_SESSION_COOKIES)
def test_hostile_session_cookie_is_refused_not_a_crash(value: bytes) -> None:
    """H-1, session half. A malformed cookie is anonymity, not an exception."""

    result = call(
        "GET",
        "/app",
        headers=[(b"cookie", SESSION_COOKIE_NAME.encode("ascii") + b"=" + value)],
    )
    assert result["error"] is None, f"unhandled {result['error']!r}"
    assert result["status"] in (401, 303), result["status"]


def test_a_non_ascii_token_is_never_folded_into_a_valid_one() -> None:
    """The repair must refuse, not normalise.

    Comparing encoded bytes would be worthless if the encoding first mapped a
    fullwidth or compatibility character onto the ASCII one it resembles. A
    fullwidth spelling of a genuinely valid token must still be refused.
    """

    valid = _csrf()
    fullwidth = valid.translate({ord(c): ord(c) + 0xFEE0 for c in valid if "!" <= c <= "~"})
    assert fullwidth != valid
    result = _signed_in_write(
        extra=[(b"origin", BASE.encode("ascii"))], token=fullwidth.encode("utf-8")
    )
    assert result["error"] is None
    assert result["status"] == 403


def test_the_valid_token_still_works() -> None:
    """Anti-vacuity: the refusals above must not be passing because everything fails."""

    result = _signed_in_write(
        extra=[(b"origin", BASE.encode("ascii"))], token=_csrf().encode("ascii")
    )
    assert result["error"] is None
    assert result["status"] not in (401, 403, 500), result["status"]


# ---------------------------------------------------------------------------
# L-9 — the same robustness class in the ID-token verifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("héader.payload.signature", id="non-ascii-header"),
        pytest.param("header.pÃ¥yload.signature", id="non-ascii-payload"),
        pytest.param("header.payload.sïgnature", id="non-ascii-signature"),
        pytest.param("ｈeader.payload.signature", id="fullwidth"),
        pytest.param("header.payload.\U0001f600", id="astral"),
    ],
)
def test_non_ascii_jwt_is_an_assertion_error_not_a_unicode_error(token: str) -> None:
    """L-9: `signing_input.encode("ascii")` used to raise `UnicodeEncodeError`."""

    from app.core.auth.identity import IdentityAssertionError
    from app.core.auth.jwks import split_jwt

    with pytest.raises(IdentityAssertionError):
        split_jwt(token)


def test_verify_signature_cannot_raise_a_unicode_error_either() -> None:
    """Defence in depth: the signature step is safe even called directly."""

    from app.core.auth.identity import IdentityAssertionError
    from app.core.auth.jwks import verify_signature
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    with pytest.raises(IdentityAssertionError):
        verify_signature(signing_input="hé.påy", signature="AAAA", key=key)


# ---------------------------------------------------------------------------
# M-2 — the OPTIONS contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/api/campaigns", "/app", "/admin", "/api/salesnav/profiles", "/nope", "/auth", "/auth/x"],
)
def test_anonymous_options_is_refused(path: str) -> None:
    """M-2: the decided contract, stated once and asserted directly.

    `OPTIONS` is a *safe* method — the cross-site backstop does not apply to it —
    but it is not an *anonymous* one. Three shipped documents used to claim the
    opposite; the code never implemented it, and the claim is now gone rather
    than implemented. If a future change exempts preflights, this test fails and
    the exemption has to be an explicit decision with its own contract.
    """

    result = call("OPTIONS", path)
    assert result["error"] is None
    assert result["status"] == 401, (path, result["status"])
    assert b"unauthorized" in result["body"]


def test_anonymous_options_carries_no_credentialed_cors_headers() -> None:
    """A refusal must not hand an arbitrary origin a usable preflight answer."""

    result = call("OPTIONS", "/api/campaigns", headers=[(b"origin", b"https://evil.example")])
    assert result["status"] == 401
    assert b"access-control-allow-credentials" not in result["body"].lower()


# ---------------------------------------------------------------------------
# M-3 — no anonymous prefix, and no 404-vs-401 tell
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/auth",
        "/auth/",
        "/auth/x",
        "/auth/x/y",
        "/auth/..;/app",
        "/auth/future-route",
        "/static",
        "/staticky",
        "/authx",
        "/nope",
        "/docs",
        "/openapi.json",
    ],
)
def test_unmounted_paths_are_indistinguishable_from_protected_ones(path: str) -> None:
    """M-3: every unknown path answers the same way, with no 404 tell.

    `/auth/x` used to answer 404 while `/nope` answered 401, so an anonymous
    caller could map which prefixes existed — in the same slice whose handoff
    claimed route enumeration was impossible.
    """

    result = call("GET", path)
    assert result["error"] is None
    assert result["status"] in (401, 303), (path, result["status"])


@pytest.mark.parametrize(
    "path",
    ["/healthz", "/health", "/readyz", "/ready", "/version", "/auth/login", "/auth/signed-out"],
)
def test_the_intended_anonymous_paths_still_answer(path: str) -> None:
    """Anti-vacuity for the test above: narrowing must not have closed the door."""

    result = call("GET", path)
    assert result["error"] is None
    assert result["status"] not in (401, 303, 500), (path, result["status"])


@pytest.mark.parametrize(
    "path",
    ["/auth/../admin", "/static/../admin", "/healthz/../app", "//app", "/./app", "/%2e%2e/admin"],
)
def test_traversal_out_of_an_anonymous_path_is_still_refused(path: str) -> None:
    """The behaviour the review proved correct, held in place by the M-3 change."""

    result = call("GET", path)
    assert result["error"] is None
    assert result["status"] in (401, 303), (path, result["status"])


# ---------------------------------------------------------------------------
# L-4 — one positive signal must not neutralise another
# ---------------------------------------------------------------------------


def test_same_origin_fetch_metadata_cannot_neutralise_a_hostile_origin() -> None:
    """L-4: the exact regression the repair brief asks for.

    Valid session, valid CSRF token, `Sec-Fetch-Site: same-origin`, and
    `Origin: https://evil.example`. `Sec-Fetch-Site` used to short-circuit, so
    the request reached the handler. Signals are OR-ed for refusal now: any
    trustworthy positive signal that the request is cross-site refuses, and two
    signals that disagree are themselves a reason to refuse.
    """

    result = _signed_in_write(
        extra=[(b"sec-fetch-site", b"same-origin"), (b"origin", b"https://evil.example")],
        token=_csrf().encode("ascii"),
    )
    assert result["error"] is None
    assert result["status"] == 403, result["status"]
    assert b"cross_site_request_refused" in result["body"]


def test_consistent_same_origin_signals_are_still_accepted() -> None:
    """Anti-vacuity: the honest browser case must not have been broken."""

    result = _signed_in_write(
        extra=[(b"sec-fetch-site", b"same-origin"), (b"origin", BASE.encode("ascii"))],
        token=_csrf().encode("ascii"),
    )
    assert result["error"] is None
    assert result["status"] != 403, result["status"]


# ---------------------------------------------------------------------------
# L-5 — ambiguous Origin fails closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origins",
    [
        pytest.param([BASE.encode(), b"https://evil.example"], id="valid-then-evil"),
        pytest.param([b"https://evil.example", BASE.encode()], id="evil-then-valid"),
        pytest.param([BASE.encode(), BASE.encode()], id="identical-duplicates"),
    ],
)
def test_duplicate_origin_headers_are_refused(origins: list[bytes]) -> None:
    """L-5: `len(origins) != 1` used to mean "absent", which disabled layer 1."""

    result = _signed_in_write(
        extra=[(b"origin", value) for value in origins], token=_csrf().encode("ascii")
    )
    assert result["error"] is None
    assert result["status"] == 403, result["status"]
    assert b"cross_site_request_refused" in result["body"]


def test_duplicate_fetch_metadata_headers_are_refused() -> None:
    """Same ambiguity class on the other signal, fixed the same way."""

    result = _signed_in_write(
        extra=[(b"sec-fetch-site", b"same-origin"), (b"sec-fetch-site", b"cross-site")],
        token=_csrf().encode("ascii"),
    )
    assert result["error"] is None
    assert result["status"] == 403, result["status"]


# ---------------------------------------------------------------------------
# #264 — an opaque Origin is disambiguated, and only by fetch metadata
# ---------------------------------------------------------------------------

# `Referrer-Policy: no-referrer` makes the browser serialise `Origin` as `null`
# on every same-origin write, so `null` alongside `Sec-Fetch-Site: same-origin`
# is the ordinary shape and is accepted. Ambiguity in the signal that does the
# disambiguating must still fail closed, and only this module can put a
# duplicated header on the wire.


def test_a_duplicated_fetch_site_cannot_clear_an_opaque_origin() -> None:
    """The #264 relaxation must not have created a way to smuggle one through.

    An attacker who can get a second `Sec-Fetch-Site` onto the request would
    otherwise only have to make one of the two say `same-origin` to have an
    opaque origin waved through. Duplication is ambiguity and ambiguity refuses,
    before the origin is even looked at.
    """

    for pair in ([b"same-origin", b"cross-site"], [b"cross-site", b"same-origin"]):
        result = _signed_in_write(
            extra=[(b"origin", b"null"), *((b"sec-fetch-site", value) for value in pair)],
            token=_csrf().encode("ascii"),
        )
        assert result["error"] is None
        assert result["status"] == 403, (pair, result["status"])
        assert b"cross_site_request_refused" in result["body"]


def test_a_duplicated_opaque_origin_is_still_refused() -> None:
    """`len(origins) != 1` stays ambiguity, whatever the fetch metadata says."""

    result = _signed_in_write(
        extra=[(b"origin", b"null"), (b"origin", b"null"), (b"sec-fetch-site", b"same-origin")],
        token=_csrf().encode("ascii"),
    )
    assert result["error"] is None
    assert result["status"] == 403, result["status"]
    assert b"cross_site_request_refused" in result["body"]


def test_the_real_browser_write_shape_is_accepted() -> None:
    """Anti-vacuity for the two above: the shape a genuine click sends still passes."""

    result = _signed_in_write(
        extra=[(b"origin", b"null"), (b"sec-fetch-site", b"same-origin")],
        token=_csrf().encode("ascii"),
    )
    assert result["error"] is None
    assert result["status"] != 403, result["status"]


# ---------------------------------------------------------------------------
# L-7 — ambiguous session cookie fails closed
# ---------------------------------------------------------------------------


def test_duplicate_session_cookie_names_valid_last_are_refused() -> None:
    """L-7, the order that used to authenticate.

    `SimpleCookie` keeps the *last* morsel, so `junk; vmr_session=<valid>`
    authenticated. An attacker who can set a domain-scoped cookie from a sibling
    host must not get to choose which credential the boundary reads.
    """

    name = SESSION_COOKIE_NAME.encode("ascii")
    result = call(
        "GET",
        "/app",
        headers=[(b"cookie", name + b"=junk; " + name + b"=" + _session_cookie().encode("ascii"))],
    )
    assert result["error"] is None
    assert result["status"] in (401, 303), result["status"]


def test_duplicate_session_cookie_names_valid_first_are_refused() -> None:
    """L-7, the order that already refused — held so the fix is symmetric."""

    name = SESSION_COOKIE_NAME.encode("ascii")
    result = call(
        "GET",
        "/app",
        headers=[
            (b"cookie", name + b"=" + _session_cookie().encode("ascii") + b"; " + name + b"=junk")
        ],
    )
    assert result["error"] is None
    assert result["status"] in (401, 303), result["status"]


def test_multiple_cookie_headers_are_still_not_reassembled() -> None:
    """The pre-existing smuggling refusal the L-7 repair must not have loosened."""

    name = SESSION_COOKIE_NAME.encode("ascii")
    result = call(
        "GET",
        "/app",
        headers=[
            (b"cookie", b"other=1"),
            (b"cookie", name + b"=" + _session_cookie().encode("ascii")),
        ],
    )
    assert result["error"] is None
    assert result["status"] in (401, 303), result["status"]


def test_one_session_cookie_beside_other_cookies_still_authenticates() -> None:
    """Anti-vacuity: an ordinary browser jar has more than one cookie in it."""

    name = SESSION_COOKIE_NAME.encode("ascii")
    result = call(
        "GET",
        "/app",
        headers=[
            (
                b"cookie",
                b"theme=dark; " + name + b"=" + _session_cookie().encode("ascii") + b"; a=b",
            )
        ],
    )
    assert result["error"] is None
    assert result["status"] == 200, result["status"]

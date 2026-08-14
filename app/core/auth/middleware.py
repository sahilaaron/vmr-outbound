"""The authentication boundary.

One pure-ASGI middleware, mounted *inside* the trusted-host check and *inside*
the production hardening boundary. Placement is deliberate and load-bearing:

* Hardening stays outermost, so a 401, a sign-in redirect and a cross-site
  refusal all carry the request ID, the security headers and the access-log line
  exactly like any other response.
* The trusted-host check stays outside this, so a request with a forged ``Host``
  is rejected before any identity is read and before a redirect URL is built
  from that host.
* This middleware runs before routing, so the decision does not depend on a
  route existing. An unmounted path, a 404 and an alternate spelling of a
  protected path are all refused the same way.

The middleware only ever *decides*. It writes its findings into the ASGI scope
state and two context variables; the CSRF dependency, the templates and the
sign-in routes read them from there.
"""

from __future__ import annotations

import json
import time
import uuid
from contextvars import Token
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import quote

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.auth.accounts import AccountDirectory, AccountLookupUnavailable, AccountSnapshot
from app.core.auth.config import AuthSettings
from app.core.auth.context import (
    current_operator,
    reset_current_csrf_token,
    reset_current_operator,
    set_current_csrf_token,
    set_current_operator,
)
from app.core.auth.extension import (
    EXTENSION_CREDENTIAL_LABEL,
    EXTENSION_KEY_ID_STATE_KEY,
    EXTENSION_USER_ID_STATE_KEY,
    ExtensionAuthSettings,
    authenticate_capture_request,
    capture_preflight_headers,
    is_contract_request,
    single_request_origin,
)
from app.core.auth.extension_link import (
    ExtensionLinkDirectory,
    ExtensionLinkUnavailable,
    authorize_capture_request,
)
from app.core.auth.policy import (
    EXTENSION_LINK_PUBLIC_PATHS,
    REDIRECTABLE_METHODS,
    is_admin_only_request,
    is_anonymous_path,
    is_identity_free_path,
    is_safe_method,
    normalize_request_path,
)
from app.core.auth.session import (
    SESSION_COOKIE_NAME,
    OperatorSession,
    SessionCodec,
    SessionDecodeError,
)
from app.models.enums import UserRole

__all__ = [
    "OperatorAuthenticationMiddleware",
    "clear_session_cookie_value",
    "current_operator",
]

# Outcomes of resolving one request's session. Named constants rather than a
# pair of booleans because the three cases are genuinely different and the
# difference between "refused" and "could not tell" decides whether the browser
# keeps its cookie.
_NO_SESSION = 0  # No cookie presented, or an anonymous path that needs no identity.
_SESSION_ACCEPTED = 1  # Cookie verified and the account still agrees.
_SESSION_REFUSED = 2  # Decided refusal: forged, expired, unknown, disabled, superseded.
_DIRECTORY_UNAVAILABLE = 3  # Undecided: the account directory could not be consulted.


def clear_session_cookie_value(*, secure: bool, domain: str | None) -> str:
    """A ``Set-Cookie`` value that removes the session cookie immediately."""

    return _cookie_header(
        SESSION_COOKIE_NAME,
        "",
        max_age=0,
        secure=secure,
        domain=domain,
        same_site="Lax",
    )


def _cookie_header(
    name: str,
    value: str,
    *,
    max_age: int,
    secure: bool,
    domain: str | None,
    same_site: str,
    path: str = "/",
) -> str:
    """One cookie header assembled explicitly rather than by string luck.

    ``HttpOnly`` is unconditional: no page in this application has any reason to
    read an auth cookie from script, and making it unreadable removes the entire
    class of XSS-to-session-theft escalations.
    """

    parts = [
        f"{name}={value}",
        f"Path={path}",
        f"Max-Age={max_age}",
        "HttpOnly",
        f"SameSite={same_site}",
    ]
    if secure:
        parts.append("Secure")
    if domain:
        parts.append(f"Domain={domain}")
    return "; ".join(parts)


def _headers(scope: Scope, name: bytes) -> list[str]:
    return [
        raw_value.decode("latin-1")
        for raw_name, raw_value in scope.get("headers", [])
        if raw_name.lower() == name
    ]


def _cookie(scope: Scope, name: str) -> str | None:
    """The single unambiguous value of one cookie, or ``None``.

    Two ambiguities are refused rather than resolved, because resolving either
    means letting somebody else choose which credential this boundary reads:

    * **More than one ``Cookie`` header.** A request-smuggling shape that must
      not be reassembled here. (An HTTP/2 client that legitimately splits
      cookies therefore appears anonymous; the nginx to uvicorn hop is HTTP/1.1
      and recombines, so no deployed client is affected. Recorded as L-8.)
    * **More than one morsel with the same name.** ``SimpleCookie`` silently
      keeps the *last* of a duplicate name, so an attacker able to set a
      domain-scoped cookie from a sibling host could otherwise decide which of
      two session cookies is honoured — choosing the valid one is a nuisance,
      but "first wins" or "last wins" is a decision no attacker should get to
      make on an authentication boundary.
    """

    values = _headers(scope, b"cookie")
    if len(values) != 1:
        # Zero cookies is the normal anonymous case; more than one Cookie header
        # is a request-smuggling shape that must not be reassembled here.
        return None
    header = values[0]

    # Count occurrences of this *name* before parsing, because parsing collapses
    # them. A quoted value containing `; <name>=` would over-count and be
    # refused, which is the safe direction.
    occurrences = 0
    for chunk in header.split(";"):
        candidate, separator, _ = chunk.partition("=")
        if separator and candidate.strip() == name:
            occurrences += 1
    if occurrences != 1:
        return None

    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(header)
    except Exception:  # pragma: no cover - SimpleCookie is lenient by design
        return None
    morsel = jar.get(name)
    return morsel.value if morsel is not None else None


def _request_origin(scope: Scope) -> str | None:
    """The origin this request was actually made to, per the trusted boundary."""

    hosts = _headers(scope, b"host")
    if len(hosts) != 1:
        return None
    state = scope.get("state") or {}
    scheme = state.get("forwarded_scheme") or scope.get("scheme") or "http"
    return f"{str(scheme).lower()}://{hosts[0].strip().lower()}"


def _is_cross_site(scope: Scope, settings: AuthSettings) -> bool:
    """Whether *any* supplied signal says this unsafe request is not same-site.

    Every relevant signal is evaluated and any one of them can refuse. That is
    the whole rule, and it is deliberately not a priority order: an earlier
    signal saying "same-origin" must never be able to neutralise a later one
    saying "evil.example". Two positive signals that disagree are themselves a
    reason to refuse, so the safe direction is to OR the refusals rather than
    consult the first header that happens to be present.

    Only a *positive* signal refuses. Absent headers fall through to the
    per-session token check, which fails closed on its own — that is what keeps
    a non-browser client (a script holding a valid token) working while a real
    browser, which always sends ``Origin`` on a cross-site form post, is stopped
    at this layer before the body is read.

    Duplicated headers are ambiguity, and ambiguity refuses. A front end or
    proxy that emits ``Origin`` twice would otherwise silently disable this
    entire layer, because "not exactly one" used to read as "absent".

    The one signal that is *not* read literally is an opaque ``Origin``; see the
    comment on that branch. Everything else is taken exactly as presented.
    """

    fetch_site = _headers(scope, b"sec-fetch-site")
    if len(fetch_site) > 1:
        return True
    site = fetch_site[0].strip().lower() if len(fetch_site) == 1 else None
    if site is not None and site not in {"same-origin", "none"}:
        return True

    origins = _headers(scope, b"origin")
    if len(origins) > 1:
        return True
    if len(origins) == 1:
        presented = origins[0].strip().lower()
        if presented in {"", "null"}:
            # An opaque origin. A sandboxed frame and a document loaded from a
            # `data:` URL both produce one, and neither should write.
            #
            # It is *also* what an ordinary same-origin form post looks like on
            # this deployment, which is why refusing it outright refused every
            # write in the hosted UI, sign-out included (#264). The application
            # sends `Referrer-Policy: no-referrer` from its hardening boundary,
            # and the Fetch standard serialises `Origin` as `null` on every
            # non-GET/HEAD, non-CORS request made under that policy — a genuine
            # same-origin post included. The header is therefore not evidence of
            # anything on its own here, in either direction.
            #
            # `Sec-Fetch-Site` is what separates the two cases, and it is the
            # signal an attacker cannot supply: it is a forbidden header name,
            # so no page script may set, clear or alter it, and the browser
            # computes it from the request's real initiator rather than from the
            # referrer policy. A cross-site post consequently arrives as
            # `cross-site` or `same-site` and was already refused above; a
            # sandboxed frame's opaque origin is same-origin with nothing, so it
            # is refused there too. `none` (a typed URL or a bookmark) does not
            # clear an opaque origin either — only a positive `same-origin`
            # does, which is exactly and only the shape a real click produces.
            #
            # A non-browser client can of course write both headers itself, but
            # that client has always been layer 2's problem: every
            # cookie-authenticated write still has to present the per-session
            # CSRF token, which fails closed on its own.
            return site != "same-origin"
        accepted = {value for value in (_request_origin(scope), settings.public_base_url) if value}
        if presented not in accepted:
            return True

    return False


class OperatorAuthenticationMiddleware:
    """Refuse every request that is not an approved internal VMR operator."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: AuthSettings,
        extension_settings: ExtensionAuthSettings | None = None,
        account_directory: AccountDirectory | None = None,
        extension_link_directory: ExtensionLinkDirectory | None = None,
        app_env: str | None = None,
    ) -> None:
        self.app = app
        self.settings = settings
        # The legacy `vmrx1` shared capture credential is development
        # compatibility and nothing else. It verifies only when this deployment
        # says it is local, so a credential that leaks out of a developer's
        # machine — or one still listed in a hosted environment file after the
        # move to account linking — is worth exactly nothing against staging or
        # production. `None` (an unknown environment) is *not* local: the
        # unstated case has to be the closed one.
        self.app_env = (app_env or "").strip().lower()
        self.legacy_credentials_enabled = self.app_env == "local"
        # The account directory is a seam for the same reason `IdentityProvider`
        # is: the boundary must be testable without a database, and the live
        # implementation must not be constructed at import time. `None` here means
        # "resolve the default lazily on first use", which keeps importing this
        # module free of any connection side effect.
        self._account_directory = account_directory
        self._account_directory_resolved = account_directory is not None
        # The extension boundary is a second, much narrower credential and is
        # deliberately its own object rather than a field on `AuthSettings`: the
        # two are configured separately, revoked separately, and must never be
        # able to satisfy each other.
        self.extension_settings = extension_settings or ExtensionAuthSettings()
        # The account-linked extension authority, resolved through the same kind
        # of lazy, injectable seam as `AccountDirectory` above and for the same
        # two reasons: importing this module must not open a connection, and the
        # boundary must be exercisable without a database.
        self._extension_link_directory = extension_link_directory
        self._extension_link_directory_resolved = extension_link_directory is not None
        self.codec = (
            SessionCodec(settings.session_secret or "")
            if settings.enabled and settings.has_session_secret()
            else None
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state: dict[str, Any] = scope.setdefault("state", {})

        if _has_control_character(str(scope.get("path", "/"))):
            # Refused before any policy decision, and before the
            # authentication-disabled shortcut, because this is a malformed
            # request rather than an unauthorised one.
            #
            # The reason it matters here specifically: this policy decides by
            # string comparison, and Starlette's router decides by
            # `re.match("^/admin$", path)`. Python's `$` also matches just
            # before a single trailing newline, so `/admin\n` is NOT `/admin` to
            # `==` and IS `/admin` to the router. uvicorn percent-decodes the
            # target before either sees it, so `GET /admin%0A` arrived here as
            # `/admin\n`, was classified as not-administrator-only, and was then
            # routed to the Workbench. The same trick reached `/docs`,
            # `/openapi.json`, `/campaigns` and every other path matched by
            # whole-string equality rather than by prefix.
            #
            # Normalising the newline away would fix that one spelling. Refusing
            # the whole character class kills the family: no route in this
            # application has a control character in its path, so anything
            # carrying one is either a probe or a mismatch waiting to be found
            # between two matchers that were never written to agree.
            await self._respond(
                scope,
                send,
                status=400,
                error="malformed_path",
                message="This request could not be processed.",
            )
            return

        if not self.settings.enabled or self.codec is None:
            # Local development and any deployment that has not turned hosted
            # authentication on. Nothing is enforced and nothing is recorded, so
            # the CSRF dependency and `csrf_field()` stay inert.
            state["auth_enforced"] = False
            await self.app(scope, receive, send)
            return

        state["auth_enforced"] = True
        now = int(time.time())
        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", "/"))
        session, account, outcome = self._resolve_session(scope, now=now, path=path)

        # The extension capture credential. Consulted only for the enumerated
        # capture contract (see `app/core/auth/extension.py`), so this call
        # returns `None` for every other path in the application and no amount of
        # a valid credential can widen that.
        #
        # Resolved *before* the directory verdict is acted on, because it does not
        # depend on the directory at all: a capture is authorised by a bearer
        # credential and a `chrome-extension://` origin, neither of which needs an
        # account row. Answering 503 to a perfectly valid capture merely because
        # the same browser also carried a stale session cookie would be an outage
        # invented out of an irrelevant fact.
        try:
            extension_key, extension_user_id = self._resolve_extension(
                scope, path=path, method=method
            )
        except ExtensionLinkUnavailable:
            # A presented `vmre1` token that could not be checked. Refused, and
            # refused as *unknown* rather than as *unauthorized*: telling the
            # extension its link is dead would make it discard a perfectly good
            # refresh token and demand a human sign-in over a database blip.
            await self._respond(
                scope,
                send,
                status=503,
                error="extension_link_unavailable",
                message=(
                    "This extension authorization could not be verified right now. "
                    "This is temporary — try again in a moment."
                ),
            )
            return

        if outcome == _DIRECTORY_UNAVAILABLE and extension_key is None:
            # The account directory could not answer. This is *unknown*, not
            # *refused*: the session cookie stays exactly where it is, so the
            # browser is signed in again the moment the database is reachable.
            # Written as its own branch rather than folded into the anonymous
            # refusal so that a database outage can never silently log everybody
            # out and then look like a wave of expired sessions.
            await self._respond(
                scope,
                send,
                status=503,
                error="account_directory_unavailable",
                message=(
                    "Your account could not be verified right now. "
                    "This is temporary — try again in a moment."
                ),
            )
            return

        # One credential decides one request, and an explicitly presented bearer
        # outranks an ambient cookie. That ordering is what makes the acceptance
        # rule true in both directions: a session cookie alone never becomes an
        # extension request (no credential verified, so no key id is recorded),
        # and a verified capture credential is treated as the extension rather
        # than as whichever operator happened to be signed in to the same
        # browser. An extension is not an operator, so it gets no operator email
        # and no CSRF token — a bearer credential is not attached automatically
        # by a browser and therefore is not forgeable cross-site.
        if extension_key is not None:
            session = None
            account = None
        state[EXTENSION_KEY_ID_STATE_KEY] = extension_key
        state[EXTENSION_USER_ID_STATE_KEY] = extension_user_id
        state["auth_credential"] = (
            EXTENSION_CREDENTIAL_LABEL
            if extension_key is not None
            else ("cookie" if session is not None else None)
        )
        state["operator_email"] = session.email if session is not None else None
        state["csrf_token"] = self.codec.csrf_token(session.session_id) if session else None
        # Role comes from the account record read this request, never from the
        # cookie. A demotion therefore takes effect immediately, and a cookie
        # cannot assert a privilege the directory no longer grants. The admin
        # dependency in `app/core/auth/admin.py` reads exactly this key.
        state["operator_role"] = account.role.value if account is not None else None
        state["operator_user_id"] = str(account.user_id) if account is not None else None

        operator_token = set_current_operator(session)
        csrf_token: Token[str | None] = set_current_csrf_token(state["csrf_token"])
        try:
            if session is None and extension_key is None:
                # The one preflight exemption this application grants, and only
                # for the enumerated capture contract from an approved extension
                # origin. It answers with CORS headers, no body, and no
                # authentication implication: the request that follows still has
                # to present a credential.
                preflight = (
                    capture_preflight_headers(scope, self.extension_settings, path=path)
                    if method == "OPTIONS"
                    else None
                )
                if preflight is not None:
                    await self._respond_preflight(scope, send, headers=preflight)
                    return
                if not is_anonymous_path(path):
                    await self._refuse_anonymous(
                        scope, send, method=method, revoked=outcome == _SESSION_REFUSED
                    )
                    return

            if (
                extension_key is None
                and not is_safe_method(method)
                and not self._is_extension_link_call(scope, path)
                and _is_cross_site(scope, self.settings)
            ):
                # Skipped for an authenticated extension write on purpose. This
                # backstop exists to stop a *browser* replaying an ambient cookie
                # from another site, and it reads `Origin` against this site's own
                # origin — which a legitimate `chrome-extension://` capture can
                # never match. The equivalent protection for the extension is
                # stronger and already applied above: the credential is bound to
                # an explicitly approved extension origin.
                await self._respond(
                    scope,
                    send,
                    status=403,
                    error="cross_site_request_refused",
                    message="This request did not originate from the VMR application.",
                )
                return

            if (
                extension_key is None
                and session is not None
                and is_admin_only_request(path, method)
                and state["operator_role"] != UserRole.ADMIN.value
            ):
                # Authorization, after authentication and after the cross-site
                # backstop. Checked here rather than as a router dependency for
                # the same reason the anonymity check lives here: this runs
                # *before routing*, so an alternate spelling, an unmounted path
                # under an administrator prefix, and a route somebody forgets to
                # decorate are all refused identically. The administrator
                # surface is spread across three routers, one of which also
                # serves normal operator routes, so no per-router dependency
                # could express it anyway.
                #
                # Skipped for a verified extension credential, which has no
                # account and therefore no role: the capture contract is
                # authorised by the bearer credential and its approved origin,
                # and refusing it here for "not being an administrator" would
                # break capture without making anything safer.
                #
                # `session is None` at this point means an anonymous path, which
                # is never administrator-only; a protected path with no session
                # was already refused above.
                #
                # The role is read from `state`, which the directory lookup
                # above wrote from the account record on *this* request, so a
                # demotion applies immediately. The shape matches the
                # `AdminRequiredError` handler in `app/main.py` exactly, so a
                # refusal looks the same whether it came from here or from the
                # dependency on the account-directory router.
                await self._respond(
                    scope,
                    send,
                    status=403,
                    error="admin_required",
                    message="This area is limited to platform administrators.",
                )
                return

            await self.app(scope, receive, send)
        finally:
            reset_current_csrf_token(csrf_token)
            reset_current_operator(operator_token)

    # --- internals ----------------------------------------------------------

    def _resolve_extension(
        self, scope: Scope, *, path: str, method: str
    ) -> tuple[str | None, str | None]:
        """The extension credential authorising this request, if any.

        Two schemes, one contract, and the contract is checked first for both so
        that neither can widen it. Order matters only in that an explicitly
        presented account-linked token is resolved before the legacy credential
        is even considered — the two token formats cannot be confused (each
        parser refuses the other's version segment), so this is about not doing
        database work for a request that carries a ``vmrx1`` header.

        ``vmrx1`` is additionally gated on this being a local deployment. In a
        hosted environment it verifies nothing, which is the whole point: no
        reusable shared secret may authorise a hosted capture.
        """

        if not is_contract_request(path, method):
            return None, None

        link = authorize_capture_request(
            scope,
            self.extension_settings,
            self._link_directory() if self.extension_settings.link_enabled else None,
            method=method,
        )
        if link is not None:
            return link.key_id, str(link.user_id)

        if not self.legacy_credentials_enabled:
            return None, None
        return (
            authenticate_capture_request(scope, self.extension_settings, path=path, method=method),
            None,
        )

    def _is_extension_link_call(self, scope: Scope, path: str) -> bool:
        """Whether this is one of the two link endpoints an extension calls directly.

        The cross-site backstop above exists to stop a *browser* replaying an
        ambient cookie from another site, and it compares ``Origin`` against this
        site's own origin — which a legitimate ``chrome-extension://`` caller can
        never match. These two endpoints are called by the extension's service
        worker with no cookie at all, so the backstop would refuse every one of
        them for a property they cannot have.

        The exemption is deliberately narrow in three ways at once: two exact
        paths, only while account linking is switched on, and only for a request
        whose ``Origin`` is an approved extension install. It grants no
        authority — each endpoint still has to verify a code, a refresh secret or
        an access token before it does anything.
        """

        if not self.extension_settings.link_enabled:
            return False
        if normalize_request_path(path) not in EXTENSION_LINK_PUBLIC_PATHS:
            return False
        return self.extension_settings.is_allowed_origin(single_request_origin(scope))

    def _link_directory(self) -> ExtensionLinkDirectory:
        """The extension link directory, resolved once and then reused."""

        if not self._extension_link_directory_resolved:
            from app.core.auth.extension_link import default_extension_link_directory

            self._extension_link_directory = default_extension_link_directory()
            self._extension_link_directory_resolved = True
        assert self._extension_link_directory is not None
        return self._extension_link_directory

    def _directory(self) -> AccountDirectory:
        """The account directory, resolved once and then reused.

        Lazy rather than eager so that importing this module, or building an app
        whose authentication is switched off, never constructs a database session
        factory as a side effect.
        """

        if not self._account_directory_resolved:
            from app.core.auth.accounts import default_account_directory

            self._account_directory = default_account_directory()
            self._account_directory_resolved = True
        assert self._account_directory is not None
        return self._account_directory

    def _resolve_session(
        self, scope: Scope, *, now: int, path: str
    ) -> tuple[OperatorSession | None, AccountSnapshot | None, int]:
        """Decode the cookie, then re-check the account behind it.

        Two independent gates, in this order:

        1. **The cookie must verify.** Signature, version, and absolute lifetime.
           A forged, malformed or expired cookie is one outcome — no session —
           and the stale cookie is cleared.
        2. **The account must still agree.** It must exist, be active, and carry
           the ``auth_version`` the cookie was minted under. This is the gate that
           makes disabling an account and resetting a password take effect on
           already-issued sessions rather than at the next expiry.

        Gate 2 is skipped entirely for anonymous paths. A probe, the sign-in page
        and the stylesheet must keep working when the database does not, and none
        of them needs to know who is asking.
        """

        assert self.codec is not None
        raw = _cookie(scope, SESSION_COOKIE_NAME)
        if raw is None:
            return None, None, _NO_SESSION
        try:
            session = self.codec.decode_session(raw, now=now)
        except SessionDecodeError:
            return None, None, _SESSION_REFUSED

        if is_identity_free_path(path):
            # A probe or a static asset. The answer does not depend on who is
            # asking, so the directory is not consulted and this request carries
            # no operator — which is what keeps those paths answering while the
            # database is down. Note the deliberately *narrower* test than
            # `is_anonymous_path`: the sign-in surface is anonymous but still
            # needs the account resolved, because `/auth/login` redirects a
            # signed-in operator and `/auth/logout` must demand a CSRF token
            # exactly when there is a session to protect.
            return None, None, _NO_SESSION

        try:
            account = self._directory().lookup(uuid.UUID(session.user_id))
        except AccountLookupUnavailable:
            if is_anonymous_path(path):
                # The sign-in surface must keep working when the database does
                # not: it is where an operator ends up when everything else is
                # refused. Serving it as anonymous is the graceful degradation —
                # a signed-in operator sees a sign-in form rather than a 503, and
                # nothing is granted that a session would have granted.
                return None, None, _NO_SESSION
            return None, None, _DIRECTORY_UNAVAILABLE
        except ValueError:
            # A `uid` claim that is not a UUID cannot have been minted here.
            return None, None, _SESSION_REFUSED

        if account is None or not account.is_active:
            return None, None, _SESSION_REFUSED
        if account.auth_version != session.auth_version:
            # Disabled and re-enabled, password reset, role changed, or an
            # administrator invalidated everything. Whatever the cause, this
            # cookie predates it.
            return None, None, _SESSION_REFUSED
        return session, account, _SESSION_ACCEPTED

    async def _refuse_anonymous(
        self, scope: Scope, send: Send, *, method: str, revoked: bool
    ) -> None:
        clear = (
            clear_session_cookie_value(
                secure=self.settings.cookie_secure, domain=self.settings.cookie_domain
            )
            if revoked
            else None
        )

        if method in REDIRECTABLE_METHODS and _prefers_html(scope):
            target = "/auth/login"
            destination = _requested_target(scope)
            if destination:
                target = f"/auth/login?next={quote(destination, safe='')}"
            await self._respond(
                scope,
                send,
                status=303,
                error="authentication_required",
                message="Sign in to continue.",
                location=target,
                set_cookie=clear,
            )
            return

        # Never redirect a write or an API call: a 303 on a POST would be
        # followed as a GET and look like a success to a client that cannot see
        # the address bar.
        await self._respond(
            scope,
            send,
            status=401,
            error="unauthorized",
            message="An approved VMR operator session is required.",
            set_cookie=clear,
        )

    async def _respond_preflight(
        self, scope: Scope, send: Send, *, headers: dict[str, str]
    ) -> None:
        """204, no body, and only the CORS headers the contract allows.

        Written here rather than delegated to a route so the exemption cannot be
        widened by a handler: this response never carries
        ``Access-Control-Allow-Credentials``, never reflects an unapproved
        origin, and never depends on a route existing.
        """

        raw_headers: list[tuple[bytes, bytes]] = [
            (b"content-length", b"0"),
        ]
        raw_headers.extend(
            (name.lower().encode("ascii"), value.encode("latin-1"))
            for name, value in headers.items()
        )
        await send({"type": "http.response.start", "status": 204, "headers": raw_headers})
        await send({"type": "http.response.body", "body": b""})

    async def _respond(
        self,
        scope: Scope,
        send: Send,
        *,
        status: int,
        error: str,
        message: str,
        location: str | None = None,
        set_cookie: str | None = None,
    ) -> None:
        body = json.dumps({"error": error, "status": status, "message": message}).encode("utf-8")
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            # The decision depends on the request's own cookies and fetch
            # metadata; a shared cache must never reuse one operator's answer.
            (b"vary", b"Cookie, Accept, Origin, Sec-Fetch-Site"),
        ]
        if location is not None:
            headers.append((b"location", location.encode("latin-1")))
        if set_cookie is not None:
            headers.append((b"set-cookie", set_cookie.encode("latin-1")))
        start: Message = {"type": "http.response.start", "status": status, "headers": headers}
        await send(start)
        await send({"type": "http.response.body", "body": body})


def _has_control_character(path: str) -> bool:
    """Whether the request path carries a C0 control character or DEL.

    Kept separate from the policy module because it is not an access decision:
    a path like this is refused outright rather than classified.
    """

    return any(character < " " or character == "\x7f" for character in path)


def _prefers_html(scope: Scope) -> bool:
    """Whether this looks like a browser navigation rather than an API call."""

    accepts = _headers(scope, b"accept")
    if len(accepts) != 1:
        return False
    value = accepts[0].lower()
    if "text/html" not in value:
        return False
    mode = _headers(scope, b"sec-fetch-mode")
    if len(mode) == 1 and mode[0].strip().lower() != "navigate":
        # `fetch()` from a page sends `Accept: */*` by default but can be told
        # to ask for HTML; a non-navigation must still get the JSON refusal.
        return False
    return True


def _requested_target(scope: Scope) -> str | None:
    """The normalised path (with query) to return to after signing in."""

    path = normalize_request_path(str(scope.get("path", "/")))
    if path == "/":
        return None
    raw_query = scope.get("query_string") or b""
    query = raw_query.decode("latin-1") if isinstance(raw_query, bytes) else str(raw_query)
    if any(character in path for character in ("\r", "\n")):
        return None
    if query and not any(character in query for character in ("\r", "\n")):
        return f"{path}?{query}"
    return path

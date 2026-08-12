"""The route access policy: what an anonymous caller may reach, and nothing more.

The policy is **default-deny**. A path is anonymous only if it appears below;
everything else — every UI surface, every API, every state-changing route, the
OpenAPI schema and the docs — requires an approved operator session. That
direction matters: a router added next month is protected the moment it is
mounted, without anyone remembering to guard it.

Path matching runs against a normalised form so that alternate spellings of the
same path cannot be used to slip past the boundary. Normalisation only ever
makes a path *more* protected: ``/healthz/../app`` normalises to ``/app`` and is
refused, while a decorated spelling of an anonymous path that no router actually
serves can at worst reach a 404.

Anonymity is granted by **exact path**, not by prefix, with exactly one
exception: the ``/static/`` mount. A prefix rule grants anonymity to routes that
do not exist yet, which is the opposite of default-deny — and it also leaks,
because an unmounted path under an anonymous prefix answers 404 while every
other unknown path answers 401. Both are fixed here and both are pinned by
tests.

The method makes no difference to this decision. ``OPTIONS`` is a *safe* method
(see ``SAFE_METHODS``) but it is not an *anonymous* one: an anonymous ``OPTIONS``
on a protected or unmounted path is refused exactly like an anonymous ``GET``.
"""

from __future__ import annotations

# Deployment probes. nginx already restricts these at the network edge
# (`vmr-probe-access.conf` ships as `deny all;`), but the application must not
# depend on that: a probe is what tells the deploy script the release is alive,
# and it must answer before any operator has signed in.
_ANONYMOUS_EXACT_PATHS: frozenset[str] = frozenset(
    {
        "/healthz",
        "/health",
        "/readyz",
        "/ready",
        "/version",
    }
)

# The sign-in surface itself, enumerated exactly. These are the routes on
# `app/web/auth_routes.py:router`, written out one by one rather than matched by
# a `/auth/` prefix.
#
# The prefix form was a latent hole rather than a live one: nothing protected was
# ever mounted under `/auth/`, but the invariant it encoded was "anything ever
# mounted here is anonymous, forever, silently", and a future
# `app.include_router(x, prefix="/auth")` would have been publicly reachable with
# every gate green. It also made the boundary leak: an unmounted `/auth/x`
# answered 404 while every other unknown path answered 401, so an anonymous
# caller could tell the two apart. Both properties are pinned by the conformance
# test in `tests/test_hosted_auth_templates.py`, which fails if a route is added
# to the auth router without a decision being recorded here.
_ANONYMOUS_AUTH_ROUTES: frozenset[str] = frozenset(
    {
        "/auth/login",
        "/auth/google/start",
        "/auth/callback",
        "/auth/logout",
        "/auth/signed-out",
        # Added by the user-accounts slice (#270). Both must be reachable without
        # a session by definition — one is where a session is obtained and the
        # other is where somebody who has never signed in sets their password.
        #
        # Neither is a hole in default-deny:
        #
        # * `/auth/password` is the email/password form's target. It creates a
        #   session only for an account that already exists, is active and has a
        #   password; no branch on it creates an account. It is rate-limited, and
        #   the cross-site backstop applies to it exactly as to any other unsafe
        #   method.
        # * `/auth/setup` renders and accepts the first-login password form. It is
        #   authorized by a one-time token rather than by a session, refuses every
        #   consumed, expired, superseded or disabled-account token, and never
        #   signs anybody in — a successful setup redirects to the sign-in page.
        #
        # The one-time token travels in the query string on the `GET`. The
        # application access log records the *route template* and never the query
        # string (see `_route_name` in `app/core/http.py`), so no raw token
        # reaches it; keeping it out of the reverse proxy's own access log is a
        # deployment note in `docs/HOSTED_AUTH.md`.
        "/auth/password",
        "/auth/setup",
    }
)

# The one intentional mount exception, and deliberately *not* a general prefix
# rule. `/static/...` is served by a `StaticFiles` mount over a fixed directory —
# compiled CSS, one SVG mark and two progressive-enhancement scripts, all of
# which are already public in every operator's browser cache. Application routers
# are never mounted here, which is what makes an exception for a mount different
# in kind from an exception for a path prefix.
#
# Bare `/static` is *not* anonymous: it is not an asset, and Starlette answers it
# with a 307 to `/static/`, which would tell an anonymous caller the mount exists.
_ANONYMOUS_STATIC_MOUNT_PREFIX = "/static/"

# Methods that never change state, and therefore the methods the cross-site
# backstop does not apply to.
#
# This set says nothing about anonymity. Every method, `OPTIONS` included, needs
# an approved operator session on a protected path — see the module docstring
# above and `docs/HOSTED_AUTH.md`.
#
# There is now exactly one exception, and it is deliberately not expressed here.
# Extension capture authentication (`app/core/auth/extension.py`) brought the
# credential-less preflight this comment used to defer: the middleware answers
# `OPTIONS` with CORS headers, no body and no authentication implication, but
# only for the enumerated capture contract and only from an approved
# `chrome-extension://` origin. It is not a *path* exemption and so does not
# belong in `_ANONYMOUS_EXACT_PATHS`: the enumerated paths stay protected for
# every other method and for every other origin, and the preflight answer grants
# nothing — the request that follows still has to present a credential.
#
# The `@router.options` handlers in `app/api/routes.py` remain for local
# development, where this middleware is inert.
SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})

# The subset of safe methods for which an unauthenticated *browser* navigation
# should be answered with a sign-in redirect rather than a bare 401.
REDIRECTABLE_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})


def normalize_request_path(raw: str) -> str:
    """Collapse a request path to the single form the policy is written against.

    Removes empty segments (so ``//admin`` and ``/admin/`` both become
    ``/admin``), drops ``.`` segments and resolves ``..`` segments. The ASGI
    path has already been percent-decoded by the server, so an encoded traversal
    arrives here as literal ``..`` and is resolved rather than matched as an
    opaque segment.
    """

    segments: list[str] = []
    for segment in raw.split("/"):
        if not segment or segment == ".":
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    return "/" + "/".join(segments)


def anonymous_application_paths() -> frozenset[str]:
    """Every exact path an anonymous caller may reach, static mount excluded.

    Exposed so the conformance test can assert this set against the live router
    table rather than against a second hand-written copy of it.
    """

    return _ANONYMOUS_EXACT_PATHS | _ANONYMOUS_AUTH_ROUTES


def is_identity_free_path(path: str) -> bool:
    """Whether this path can be served without knowing who the caller is.

    A narrower question than :func:`is_anonymous_path`, and the two must not be
    confused — conflating them is how ``POST /auth/logout`` quietly stopped being
    CSRF-protected during the user-accounts slice.

    *Anonymous* means "may be reached without a session". *Identity-free* means
    "the answer does not depend on who is asking". The sign-in surface is
    anonymous but not identity-free: ``/auth/login`` sends an already-signed-in
    operator onward instead of showing them a form, and ``/auth/logout`` demands
    a CSRF token precisely when there *is* a session to protect. Both of those
    need the account resolved.

    The probes and the static mount are both. They carry no per-caller content,
    and keeping them out of the account lookup is what lets a deployment whose
    database is down still answer ``/readyz``, still serve its stylesheet, and
    still render a sign-in page.
    """

    normalized = normalize_request_path(path)
    return normalized in _ANONYMOUS_EXACT_PATHS or normalized.startswith(
        _ANONYMOUS_STATIC_MOUNT_PREFIX
    )


def is_anonymous_path(path: str) -> bool:
    """Whether ``path`` may be served without an authenticated operator."""

    normalized = normalize_request_path(path)
    if normalized in _ANONYMOUS_EXACT_PATHS or normalized in _ANONYMOUS_AUTH_ROUTES:
        return True
    # Normalisation has already resolved `..` and `.`, so `/static/../admin`
    # arrives here as `/admin` and does not match. A path that merely *starts*
    # with the string `/static` — `/staticky` — does not match either, because
    # the trailing slash is part of the prefix.
    return normalized.startswith(_ANONYMOUS_STATIC_MOUNT_PREFIX)


def is_safe_method(method: str) -> bool:
    return method.upper() in SAFE_METHODS


def safe_next_path(raw: str | None, *, fallback: str) -> str:
    """A post-sign-in destination that can only ever be a path on this site.

    Refuses anything that is not a single-slash-rooted local path. ``//host``
    and ``/\\host`` are protocol-relative URLs that browsers follow off-site, and
    a scheme anywhere in the value means it was never a path to begin with. On
    any doubt the caller lands on ``fallback``.
    """

    if not raw or len(raw) > 512:
        return fallback
    if not raw.startswith("/"):
        return fallback
    if raw.startswith("//") or raw.startswith("/\\"):
        return fallback
    if "\\" in raw or "://" in raw:
        return fallback
    if any(character in raw for character in ("\r", "\n", "\t")):
        return fallback
    lowered = raw.lower()
    if "%2f" in lowered or "%5c" in lowered:
        # An encoded separator. `/%2f%2fevil.example` stays same-origin in every
        # browser that resolves it, so this is hardening rather than a fix — but
        # no operator destination in this application needs an encoded slash or
        # backslash, and a value that survives one more decoding step than it was
        # checked against is exactly how a redirect filter is eventually escaped.
        return fallback
    if is_anonymous_path(raw):
        # Bouncing back to the sign-in page after signing in is a loop, and a
        # probe path is not an operator destination.
        return fallback
    return raw

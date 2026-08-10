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

# The sign-in surface itself, and the stylesheet/mark the sign-in page needs.
# `/static` carries no operator data — it is the compiled CSS, one SVG mark and
# two progressive-enhancement scripts, all of which are already public in every
# operator's browser cache.
_ANONYMOUS_PATH_PREFIXES: tuple[str, ...] = (
    "/auth/",
    "/static/",
)

# Methods that never change state. OPTIONS is included deliberately: a CORS
# preflight is issued by the browser *without* credentials by specification, so
# requiring a session on it would break every future authenticated cross-origin
# client at the preflight, before it ever gets to present a credential. The
# preflight handlers in `app/api/routes.py` return CORS headers and no body.
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


def is_anonymous_path(path: str) -> bool:
    """Whether ``path`` may be served without an authenticated operator."""

    normalized = normalize_request_path(path)
    if normalized in _ANONYMOUS_EXACT_PATHS:
        return True
    return any(
        normalized == prefix.rstrip("/") or normalized.startswith(prefix)
        for prefix in _ANONYMOUS_PATH_PREFIXES
    )


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
    if is_anonymous_path(raw):
        # Bouncing back to the sign-in page after signing in is a loop, and a
        # probe path is not an operator destination.
        return fallback
    return raw

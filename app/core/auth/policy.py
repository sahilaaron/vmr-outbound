"""The route access policy: who may reach what.

Two questions, answered separately and in this order.

**May this caller in at all?** The policy is **default-deny**. A path is
anonymous only if it appears below; everything else needs an approved operator
session.

**Which operators may reach this?** A second, narrower set names the
administrator-only surface — see ``is_admin_only_request`` further down. Until
that existed every router in the application was session-gated and none was
role-gated, so any signed-in account could reach the Workbench, the Agent
Studio and the provider-spend routes.

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

import re

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

# --------------------------------------------------------------------------
# The administrator-only surface.
# --------------------------------------------------------------------------
# Authentication and authorization are different questions, and until now this
# module only answered the first. Every router above was session-gated and none
# was role-gated, so a normal operator with a valid cookie and a valid CSRF
# token could reach the Agent Studio, rotate the MillionVerifier credential,
# start a bulk verification run, or halt the sending agent. `require_admin`
# existed but was declared on exactly one router.
#
# Why prefixes here, when anonymity is granted by exact path
# ----------------------------------------------------------
# The two rules point in opposite directions, and that is the whole reason the
# asymmetry is safe. An anonymous *prefix* would grant access to routes that do
# not exist yet — the opposite of default-deny. An admin prefix *withholds*
# access from routes that do not exist yet, so a router mounted under `/admin`
# next month is administrator-only the moment it is mounted, which is the same
# property the anonymity rule gets from being default-deny.
#
# The residual risk runs the other way: a brand-new *top-level* surface defaults
# to USER, because it matches nothing here. That is what the classification
# conformance test in `tests/test_route_authorization.py` is for — it walks the
# live router table and fails until every route has been recorded as one or the
# other, so the decision is made deliberately rather than by omission.
#
# Matching runs on the normalised path, so `/app/../admin` is `/admin` here.
_ADMIN_PATH_PREFIXES: frozenset[str] = frozenset(
    {
        # The Workbench, Agent Studio and Company Intelligence, all three of
        # which are mounted on different routers but share this one prefix.
        "/admin",
        # The Agent monitor inside the operator product. It is the third face of
        # the same surface `/workbench` and `/admin` already withhold: the global
        # Agent controls (enable, pause, disable, stop sending) and a job list
        # that spans every campaign in the deployment. Neither is scoped to one
        # person's campaigns and neither could be — a global control is a
        # deployment decision, not campaign work.
        #
        # It was USER-reachable by omission rather than by decision, which meant
        # any signed-in account could halt every Agent for everybody. Per-campaign
        # Agent work is unaffected: rerun, override and stage actions live under
        # `/app/campaigns/{id}/...` and are reached by whoever the campaign is
        # assigned to.
        "/app/agents",
        # The account directory. Already refused by the `require_admin`
        # dependency on its own router, and named here as well so that this set
        # is a complete statement of the administrator surface rather than a
        # partial one that has to be read alongside a router declaration. The
        # two cannot disagree: both read the single role that the directory
        # lookup writes into the request scope.
        "/app/admin",
        # The programmatic tree. The server-rendered operator product makes no
        # calls to it at all -- no `fetch`, no htmx, no XHR -- so withholding it
        # from a normal session costs the product nothing and removes campaign,
        # agent-control and job APIs from a USER's reach. The extension is
        # unaffected: a verified capture credential is checked before this rule
        # and carries no role, so the enumerated capture contract in
        # `app/core/auth/extension.py` still answers.
        #
        # `/api/intake/linkedin-company/stage` sits under here too. It is called
        # by the extension but is deliberately outside the credential contract,
        # and the extension refuses to send it to a hosted backend at all
        # (`company_capture_local_only`, service-worker.js) -- company evidence
        # capture is a local-development path, where this middleware is inert.
        # So it is covered here rather than being added to the bearer contract,
        # which would have widened extension authority for no live caller.
        "/api",
        "/workbench",
        # Spreadsheet import lineage and staging. The operator product has its
        # own campaign-scoped import surface under `/app/campaigns/{id}/imports`.
        "/imports",
        # Legacy root-level twin of the campaign surface the operator product
        # now owns at `/app/campaigns`.
        "/campaigns",
        #
        # NOT here, deliberately: `/review`. It looks like a legacy twin of
        # `/app/review` and is not one -- `/review/rows/{id}` is ambiguous-import
        # triage, where an operator confirms whether two records are the same
        # person. Nothing is merged automatically because merging the wrong two
        # is not reversible by a retry, so the confirmation is the operator's by
        # design. It is also reached from a first-class decision card on the
        # operator's own campaign page. Gating it made that card answer 403 and
        # left the operator no route to the work at all, since no equivalent
        # exists under `/app`.
        # Already refused outside local development by `_local_tools_available`;
        # named anyway so that the boundary does not depend on a second check
        # elsewhere continuing to exist.
        "/local-tools",
        # The machine-readable inventory of every route above. Reachable by any
        # signed-in account today, which makes it a map for everything else.
        "/docs",
    }
)

_ADMIN_EXACT_PATHS: frozenset[str] = frozenset(
    {
        "/redoc",
        "/openapi.json",
        # Legacy Workbench navigation stubs that render "unavailable". Harmless
        # in themselves, and grouped with the surface they belong to rather than
        # left as the one part of it a normal operator can see.
        "/scoring",
        "/research",
        "/drafts",
        "/sequences",
        "/activity",
        "/settings",
    }
)

# Surfaces the operator product legitimately links to, where reading is normal
# operator work but writing spends money or lowers a guardrail. The page stays
# reachable; the dangerous verb does not.
#
# Written as patterns rather than prefixes because the split is finer than a
# path segment. Three separate paid dependencies are reachable from pages a USER
# is meant to use every day, and in each case only one or two verbs on the page
# spend anything:
#
# * MillionVerifier, through `/verification/...` and `/contacts/{id}/verify` --
#   while `/contacts/{id}` itself is the contact record a USER works with all
#   day and `/verification` is a page the agents screen links to;
# * logo.dev, through the two company-domain buttons on a capture, while
#   `confirm`, `correct`, `reject` and `promote` on the same page are decisions
#   recorded against evidence already stored and call nothing out;
# * metered model spend, through `/knowledge-base/generate`, while every other
#   knowledge-base write is an operator typing into a form.
_ADMIN_ONLY_UNSAFE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # `/verification` is linked from the operator agents page, so the page is
    # product. `bulk` enqueues up to 500 contacts in one request, `run` drains
    # the queue and `recover` returns leased jobs for another pass -- all three
    # spend real MillionVerifier credits, and the staging host already holds a
    # live key.
    re.compile(r"^/verification(/.*)?$"),
    # One live verification per POST, against a credential the same surface can
    # rotate.
    re.compile(r"^/contacts/[^/]+/verify$"),
    # The two buttons on a capture that call logo.dev, and the only two on that
    # page that call anything at all. Both pass `force=True` deliberately -- the
    # operator pressed a button and a silent no-op would read as a broken one --
    # which also means each press bypasses the one-lookup-per-company cache and
    # buys a fresh lookup. Nothing anywhere rate-limits them, so N presses are N
    # billed calls against a key the deployment holds and the operator does not.
    # The same provider is already administrator-only on
    # `/imports/{id}/enrich/refresh`, by virtue of the `/imports` prefix. A
    # capture page reached it by a route that had simply never been classified,
    # which is the shape this whole section exists to catch.
    #
    # `confirm`, `correct`, `reject` and `promote` on the same capture stay with
    # the USER, and were read before being left alone: `confirm_domain` and
    # `reject_candidate` write a decision onto the stored enrichment record,
    # `resolution.correct` records an operator correction with
    # `provider_call_made=False`, and `promote` evaluates already-stored state --
    # `evaluate_company` never calls the provider and never picks a candidate.
    # Deciding what captured evidence means is the operator's own work, and it
    # is the only approval a capture's company domain ever gets.
    re.compile(r"^/contact-captures/[^/]+/company/(lookup|resolve)$"),
    # KB-001 restricted claims are the control that stops the product making a
    # prohibited claim. Reading them is operator work; deactivating one is not.
    # Every other knowledge-base section stays writable by a USER, because
    # operator entry is the only approval the seller knowledge base has.
    re.compile(r"^/knowledge-base/restricted-claims(/.*)?$"),
    # Generation is the one knowledge-base write that is not an operator typing.
    # It spawns the local Claude CLI as a subprocess with operator-supplied URLs
    # and `WebSearch` enabled, which makes it three things at once: metered model
    # spend, a fetch primitive that will retrieve whatever address it is handed,
    # and a prompt-injection sink whose output is written into the knowledge base
    # the personalization agent draws outreach copy from. A page the seller
    # controls is the assumption the feature is built on; nothing on this route
    # enforces it. The manual entry and read surfaces around it stay with the
    # USER -- operator entry is the only approval KB-001 has, and withholding it
    # would leave the knowledge base empty rather than safe.
    re.compile(r"^/knowledge-base/generate$"),
    # Agent controls are platform-wide, not campaign-wide: posting here with no
    # campaign names one reaches `set_global_agent_status` and can halt or
    # resume every campaign's pipeline, the sending agent included. Reading
    # `/app/agents` stays operator work -- an operator needs to see which agents
    # are enabled, and the resume preflight tells them what is missing -- but
    # changing a global control is administration.
    re.compile(r"^/app/agents/[^/]+/control$"),
)


def is_admin_only_request(path: str, method: str) -> bool:
    """Whether this request needs an administrator rather than any operator.

    Answers for the *request*, not the path alone, because several surfaces are
    deliberately split by verb — see ``_ADMIN_ONLY_UNSAFE_PATTERNS``.
    """

    normalized = normalize_request_path(path)
    if normalized in _ADMIN_EXACT_PATHS:
        return True
    for prefix in _ADMIN_PATH_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix + "/"):
            return True
    if not is_safe_method(method):
        return any(pattern.match(normalized) for pattern in _ADMIN_ONLY_UNSAFE_PATTERNS)
    return False


def admin_only_prefixes() -> frozenset[str]:
    """Exposed for the conformance test, which asserts against the live routes."""

    return _ADMIN_PATH_PREFIXES


def admin_only_exact_paths() -> frozenset[str]:
    """Exposed for the conformance test, which asserts against the live routes."""

    return _ADMIN_EXACT_PATHS


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

    # Trailing C0 control characters are stripped so this function cannot
    # disagree with the router about what a path *is*. Starlette matches with
    # `re.match("^/admin$", path)`, and Python's `$` also matches immediately
    # before a single trailing newline -- so `/admin\n` routes to `/admin` while
    # `==` says it is something else. The middleware refuses control characters
    # outright before this is ever called; this is the second half of that fix,
    # kept here so the two matchers agree on their own terms rather than only
    # because something upstream filtered the input.
    raw = raw.rstrip("\x00\t\n\r\x0b\x0c\x1c\x1d\x1e\x1f\x7f")

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
    if is_anonymous_path(raw.partition("?")[0]):
        # Bouncing back to the sign-in page after signing in is a loop, and a
        # probe path is not an operator destination. The query string is split
        # off first: `?next=/auth/login%3Fnext=/app` would otherwise slip past
        # this check and produce exactly the loop it exists to prevent. (Not a
        # redirect off-site — every rule above still holds — just a page that
        # bounces.)
        return fallback
    return raw

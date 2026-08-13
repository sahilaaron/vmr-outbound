"""Conformance tests for the centralised authentication wiring points.

Two of them are CSRF: a dependency on each router and a Jinja extension on each
environment. These tests assert the *coverage* of those two declarations rather
than re-checking 111 forms by hand. That is the whole point of the design: if
someone adds a router or a template later, one of these fails.

The third is anonymity. `app/core/auth/policy.py` names the exact paths an
anonymous caller may reach, and the tests at the end of this module assert that
list against the live router table. The independent hostile review (M-3) found
that the previous `/auth/*` prefix rule made the opposite invariant true —
anything ever mounted under it would have been public, silently — so the set is
pinned here rather than trusted.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest
from app.core.auth.csrf import CSRF_FIELD_NAME, csrf_field, require_csrf
from app.core.auth.templating import CSRF_CALL, inject_csrf_fields, post_form_tags
from fastapi import APIRouter
from fastapi.templating import Jinja2Templates

TEMPLATE_ROOTS = (
    Path("app/web/templates"),
    Path("app/web/v2/templates"),
)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}

# `POST /auth/logout` checks the token inside the handler instead, because it
# must still work for a caller whose session has already expired — a router-level
# dependency would give that person a 403 instead of a signed-out page. It is the
# only exemption, and its behaviour is asserted directly in `test_hosted_auth.py`
# (`test_logout_without_a_token_is_refused_while_signed_in` and
# `test_logout_without_a_session_still_lands_on_the_signed_out_page`).
ROUTER_CSRF_EXEMPTIONS = {"app.web.auth_routes"}


def _template_files() -> list[Path]:
    files: list[Path] = []
    for root in TEMPLATE_ROOTS:
        files.extend(sorted(root.rglob("*.html")))
    return files


def test_the_repository_still_has_the_templates_this_suite_guards() -> None:
    """A guard against the tests below passing because they found nothing."""

    files = _template_files()
    assert len(files) > 50
    assert sum(len(post_form_tags(path.read_text(encoding="utf-8"))) for path in files) > 100


@pytest.mark.parametrize("path", _template_files(), ids=lambda path: str(path))
def test_every_post_form_receives_a_token_at_compile_time(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    expected = len(post_form_tags(source))
    rewritten = inject_csrf_fields(source)
    assert rewritten.count(CSRF_CALL) == expected


def test_get_forms_are_left_alone() -> None:
    source = '<form method="get" action="/contacts"><input name="q"></form>'
    assert inject_csrf_fields(source) == source


@pytest.mark.parametrize(
    "tag",
    [
        '<form method="post">',
        "<form method=post>",
        "<form METHOD='POST' action='/x'>",
        '<form class="v2-form-stack" method="post" action="/app/campaigns/new">',
        '<form\n  method="post"\n  action="/x"\n>',
    ],
)
def test_every_spelling_of_a_post_form_is_covered(tag: str) -> None:
    assert inject_csrf_fields(tag) == f"{tag}{CSRF_CALL}"


def test_injection_is_idempotent_on_already_injected_source() -> None:
    """Re-running the rewrite must not stack duplicate fields."""

    source = '<form method="post"></form>'
    once = inject_csrf_fields(source)
    assert inject_csrf_fields(once).count(CSRF_CALL) == 1


def test_the_rendered_field_is_inert_without_a_session() -> None:
    assert str(csrf_field()) == ""


def _application_routers() -> dict[str, APIRouter]:
    """Every module-level ``router`` under ``app.api`` and ``app.web``."""

    found: dict[str, APIRouter] = {}
    for package in ("app.api", "app.web", "app.web.v2"):
        module = importlib.import_module(package)
        for info in pkgutil.iter_modules(list(module.__path__)):
            name = f"{package}.{info.name}"
            candidate = getattr(importlib.import_module(name), "router", None)
            if isinstance(candidate, APIRouter):
                found[name] = candidate
    return found


def _declares_csrf(router: APIRouter) -> bool:
    return any(
        getattr(dependency, "dependency", None) is require_csrf
        for dependency in router.dependencies
    )


def test_every_router_with_a_write_declares_the_csrf_dependency() -> None:
    """The invariant that makes the two-line declaration trustworthy.

    A router added later that serves a POST and forgets the dependency fails
    here, at the point where it is cheapest to notice.
    """

    routers = _application_routers()
    assert routers, "no routers discovered — the walk is broken, not the app"

    unguarded = []
    for name, router in routers.items():
        if name in ROUTER_CSRF_EXEMPTIONS:
            continue
        methods: set[str] = set()
        for route in router.routes:
            methods.update(getattr(route, "methods", set()) or set())
        if methods - SAFE_METHODS and not _declares_csrf(router):
            unguarded.append(name)
    assert unguarded == []


def test_the_auth_router_is_the_only_write_that_checks_csrf_in_the_handler() -> None:
    """`/auth/logout` is the one deliberate exception, and it is exercised."""

    from app.web import auth_routes

    assert not _declares_csrf(auth_routes.router)
    assert "require_csrf" in Path("app/web/auth_routes.py").read_text(encoding="utf-8")


def test_every_template_environment_is_wired_for_csrf() -> None:
    """All five Jinja environments, including the sign-in one."""

    from app.web import admin_workbench, auth_routes, company_intelligence, routes
    from app.web.v2 import routes as v2_routes

    environments = [
        routes.templates,
        admin_workbench.templates,
        company_intelligence.templates,
        v2_routes.templates,
        auth_routes.templates,
    ]
    for templates in environments:
        assert isinstance(templates, Jinja2Templates)
        assert "csrf_field" in templates.env.globals
        assert any(key.endswith("CsrfFormExtension") for key in templates.env.extensions), (
            templates.env.loader
        )


def test_the_field_name_is_the_one_the_dependency_reads() -> None:
    assert CSRF_FIELD_NAME in str(_rendered_field())


def _rendered_field() -> str:
    from app.core.auth.context import reset_current_csrf_token, set_current_csrf_token

    token = set_current_csrf_token("a-token")
    try:
        return str(csrf_field())
    finally:
        reset_current_csrf_token(token)


# ---------------------------------------------------------------------------
# Anonymity conformance (review finding M-3)
# ---------------------------------------------------------------------------

# The exact set of application paths an anonymous caller may reach. Changing
# this list is a security decision, which is the reason it is written out here
# in a test rather than derived from the thing it is supposed to check.
EXPECTED_ANONYMOUS_PATHS = {
    # Deployment probes.
    "/healthz",
    "/health",
    "/readyz",
    "/ready",
    "/version",
    # The sign-in surface.
    "/auth/login",
    "/auth/google/start",
    "/auth/callback",
    "/auth/logout",
    "/auth/signed-out",
    # Added by the user-accounts slice (#270), as a deliberate decision recorded
    # here as well as in the policy. `/auth/password` is where a session is
    # obtained with an email address and a password; `/auth/setup` is where
    # somebody who has never signed in sets one, authorized by a one-time token
    # rather than by a session. Both create a session only for an account that
    # already exists, and neither can create an account.
    "/auth/password",
    "/auth/setup",
    # Added by the extension account-linking slice. Neither is "public" in the
    # ordinary sense and neither is on the sign-in router; both are OAuth-style
    # public-client endpoints on `app/web/extension_link_routes.py`, authorised
    # by a presented token rather than by a cookie:
    #
    # * `POST /extension/token` redeems a single-use PKCE authorization code, or
    #   rotates a refresh secret, and requires an approved `chrome-extension://`
    #   origin. A caller with neither a code nor a refresh secret gets
    #   `invalid_grant` and learns nothing; a session cookie would add nothing,
    #   because the endpoint never reads one.
    # * `POST /extension/revoke` is authorised by the access token being revoked
    #   (or, for an operator disconnecting from the VMR app, by their own
    #   session) and can only ever *reduce* authority. It is here so that the
    #   extension's own Disconnect works with no cookie: otherwise the middleware
    #   would refuse the call before the handler could check the token, and a
    #   disconnected install would keep a live server-side link for 30 days.
    #
    # `GET`/`POST /extension/authorize` are deliberately NOT here. They are
    # ordinary signed-in operator pages, and an anonymous caller is sent to
    # `/auth/login` — which is the single sign-in action the product asks for.
    "/extension/token",
    "/extension/revoke",
}

#: The one router other than the sign-in router that deliberately serves an
#: anonymous path, named exactly. Anything else turning up in the walk below is
#: the failure that test exists to catch.
DELIBERATELY_ANONYMOUS_ROUTES = {
    "app.web.extension_link_routes:/extension/token",
    "app.web.extension_link_routes:/extension/revoke",
}


def test_the_policy_names_exactly_the_intended_anonymous_paths() -> None:
    from app.core.auth.policy import anonymous_application_paths

    assert anonymous_application_paths() == EXPECTED_ANONYMOUS_PATHS


def test_every_auth_router_route_is_named_in_the_anonymous_set() -> None:
    """A route added to the sign-in router must be an explicit decision.

    The sign-in surface is the one router whose routes are anonymous. Adding a
    sixth route to it without adding the path to `policy._ANONYMOUS_AUTH_ROUTES`
    now fails here instead of shipping a route nobody can reach.
    """

    from app.core.auth.policy import is_anonymous_path
    from app.web import auth_routes

    paths = {getattr(route, "path", None) for route in auth_routes.router.routes}
    paths.discard(None)
    assert paths, "the auth router has no routes — the walk is broken, not the app"
    for path in sorted(paths):
        assert is_anonymous_path(str(path)), (
            f"{path} is served anonymously but is not in the policy"
        )


def test_no_other_router_mounts_under_an_anonymous_path() -> None:
    """The inverse, and the one M-3 was actually about.

    `app.include_router(x, prefix="/auth")` used to make `x` publicly reachable
    with every gate green. Anonymity is granted by exact path now, so that can
    no longer happen silently — and this test says so out loud, so the next
    person to try it gets a failure that explains itself.

    Two changes were made here by the extension account-linking slice, and both
    make the test stricter rather than looser:

    * The path is read as ``route.path``, which on this FastAPI version already
      carries the router's prefix. The previous line concatenated the prefix
      again, so a prefixed router's `/x/y` was checked as `/x/x/y` — a spelling
      that matches nothing — and every prefixed router was silently exempt from
      this walk. Fixing that is what lets the assertion below be an equality.
    * The result is compared against a named set of deliberate exceptions rather
      than against the empty list, so the two account-linking endpoints are
      recorded as decisions instead of hidden behind a skip.
    """

    from app.core.auth.policy import is_anonymous_path

    leaked: set[str] = set()
    for name, router in _application_routers().items():
        if name == "app.web.auth_routes":
            continue
        for route in router.routes:
            # Already prefixed by FastAPI when the route was declared on a
            # router carrying a prefix; concatenating `router.prefix` again
            # produces a path no policy rule can ever match.
            full = str(getattr(route, "path", "") or "")
            if full and is_anonymous_path(full):
                leaked.add(f"{name}:{full}")
    assert leaked == DELIBERATELY_ANONYMOUS_ROUTES


def test_the_static_mount_is_the_only_prefix_exception() -> None:
    """`/static/` is a mount, not a route prefix, and bare `/static` is not anonymous."""

    from app.core.auth.policy import is_anonymous_path

    assert is_anonymous_path("/static/app.css")
    assert is_anonymous_path("/static/nested/mark.svg")
    # Bare `/static` would be answered with a 307 to `/static/`, which tells an
    # anonymous caller the mount exists.
    assert not is_anonymous_path("/static")
    # A path that merely starts with the same characters is not the mount.
    assert not is_anonymous_path("/staticky")
    # Traversal out of the mount lands on the protected form.
    assert not is_anonymous_path("/static/../admin")


def test_no_anonymous_prefix_survives_for_a_future_route() -> None:
    """The regression M-3 asks for: unmounted paths under `/auth` are protected."""

    from app.core.auth.policy import is_anonymous_path

    for path in (
        "/auth",
        "/auth/",
        "/auth/x",
        "/auth/x/y",
        "/auth/..;/app",
        "/auth/anything-added-next-month",
    ):
        assert not is_anonymous_path(path), path

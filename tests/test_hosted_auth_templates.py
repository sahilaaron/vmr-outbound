"""Conformance tests for the two centralised CSRF wiring points.

The enforcement is declared in exactly two places — a dependency on each router
and a Jinja extension on each environment — so these tests assert the *coverage*
of those two declarations rather than re-checking 111 forms by hand. That is the
whole point of the design: if someone adds a router or a template later, one of
these fails.
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

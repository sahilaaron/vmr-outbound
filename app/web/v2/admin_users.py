"""The administrator's user-management surface.

Five routes under ``/app/admin/users``, and every one of them is refused for a
caller who is not an active administrator.

Why this is its own router rather than five more handlers on ``routes.py``
--------------------------------------------------------------------------
Because the guard belongs to the *router*, not to the handlers. Declaring
``require_admin`` once, here, means a route added to this module next month is
authorized the moment it is registered rather than when somebody remembers to
decorate it — the same reasoning that put ``require_csrf`` on the v2 router and
the anonymous allow-list in ``app/core/auth/policy.py``. A per-handler decorator
is a guard you can forget; a router-level dependency is one you cannot.

The page shell, the navigation and every Jinja filter are reused from
``app.web.v2.routes`` rather than reconstructed. Building a second
``Jinja2Templates`` over the same directory would give this screen its own copy
of the filter registry, and the first time somebody added a filter to one and not
the other, the two halves of the same interface would start rendering dates
differently.

What this surface deliberately does not do
------------------------------------------
No deletion — disabling is the revocation, and a deleted row would take its audit
history and any future attribution with it. No password field — an administrator
never sets, sees or types somebody else's password, and there is no temporary
password to leak. No bulk import — one account at a time is the right speed for a
Beta with a handful of people, and it keeps the audit trail one deliberate act per
row.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.auth.admin import require_admin
from app.core.auth.context import current_operator
from app.core.auth.csrf import require_csrf
from app.core.config import get_settings
from app.models.enums import UserRole, UserState
from app.services.users import service as user_service

# Imported rather than rebuilt. See the module docstring: one page shell and one
# filter registry for the whole customer-facing interface, of which this screen
# is a part.
from app.web.v2.routes import _redirect, _render

router = APIRouter(
    prefix="/app/admin",
    include_in_schema=False,
    dependencies=[Depends(require_csrf), Depends(require_admin)],
)

USERS_URL = "/app/admin/users"

#: Where the one-time link is stashed between the POST that creates it and the
#: GET that renders it. The raw secret is *not* put in the redirect URL, because a
#: URL lands in browser history, in a bookmark and in any proxy log between here
#: and the operator. It is not put in a cookie either, for the same reason plus
#: one more: a cookie would be sent on every subsequent request.
#:
#: It lives in process memory, keyed by the account it belongs to, and is removed
#: the first time it is rendered. That makes "shown exactly once" a property of
#: the code rather than a promise in the copy. A restart between the two requests
#: loses the link, which is the safe direction: the administrator issues another.
_pending_links: dict[str, str] = {}


def _actor() -> str:
    """Who is making this change, for the audit trail.

    The signed-in administrator's address when authentication is enabled, and a
    plainly-labelled local marker when it is not. Never a value from a form field
    or a header: an audit trail an actor can choose is not an audit trail.
    """

    session = current_operator()
    if session is not None and session.email:
        return session.email
    return "local:unauthenticated-development"


def _row(user: Any) -> dict[str, Any]:
    """One account projected for the table.

    The password *hash* never enters this dictionary — only whether one exists.
    That is the whole difference between a screen an administrator can safely
    share on a call and an offline-cracking starting point.
    """

    return {
        "id": str(user.id),
        "email": user.email_normalized,
        "display_name": user.display_name or "",
        "role": user.role,
        "state": user.state,
        "is_admin": user.role == UserRole.ADMIN,
        "is_active": user.state == UserState.ACTIVE,
        "password_state": "set" if user.has_password else "not set",
        "has_password": user.has_password,
        "google_linked": bool(user.google_subject),
        "created_at": user.created_at,
        "last_login_at": user.last_login_at,
    }


def _users_page(
    request: Request,
    db: Session,
    *,
    status_code: int = 200,
    issued_link: str | None = None,
    issued_for: str | None = None,
) -> HTMLResponse:
    settings = get_settings()
    users = user_service.list_users(db)
    return _render(
        request,
        db,
        "admin_users.html",
        {
            "page_title": "People",
            "active_nav": "",
            "rows": [_row(user) for user in users],
            "issued_link": issued_link,
            "issued_for": issued_for,
            "auth_enabled": settings.auth.enabled,
            "bootstrap_admin": settings.auth.bootstrap_admin_email,
        },
        status_code=status_code,
    )


@router.get("/users")
def users_index(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """The account list, plus the one-time link if one was just issued.

    ``_pending_links`` is drained here rather than read: the link is rendered on
    exactly this response and is gone from the process afterwards, so a reload of
    this page shows the table without it.
    """

    issued_for = request.query_params.get("issued")
    issued_link = _pending_links.pop(issued_for, None) if issued_for else None
    return _users_page(request, db, issued_link=issued_link, issued_for=issued_for)


@router.post("/users/create")
def create_user(
    request: Request,
    email: str = Form(default=""),
    display_name: str = Form(default=""),
    db: Session = Depends(get_db),
) -> Response:
    """Create one account and issue its first password link in one action.

    The two are deliberately one action rather than two clicks. An account with
    no password and no link is an account nobody can use, and an administrator
    who had to remember a second step would sooner or later hand somebody an
    address and no way in.

    The new account is always ``USER``. There is no role selector on the creation
    form: promoting somebody is a separate, separately audited act.
    """

    actor = _actor()
    try:
        user = user_service.create_user(
            db, email=email, display_name=display_name or None, actor=actor
        )
        issued = user_service.issue_credential_link(db, user=user, actor=actor)
    except user_service.UserServiceError as exc:
        db.rollback()
        return _error_page(request, db, str(exc))

    _pending_links[str(user.id)] = _setup_url(request, issued.raw_token)
    return _redirect(
        f"{USERS_URL}?issued={user.id}",
        ok=f"Account created for {user.email_normalized}.",
    )


@router.post("/users/{user_id}/state")
def change_state(
    request: Request,
    user_id: uuid.UUID,
    state: str = Form(default=""),
    db: Session = Depends(get_db),
) -> Response:
    """Disable or reactivate an account. Either way, its sessions stop working."""

    user = user_service.get_by_id(db, user_id)
    if user is None:
        return _error_page(request, db, "That account no longer exists.")
    try:
        target = UserState(state)
    except ValueError:
        return _error_page(request, db, "Unrecognised account state.")

    try:
        user_service.set_state(db, user=user, state=target, actor=_actor())
    except user_service.UserServiceError as exc:
        db.rollback()
        return _error_page(request, db, str(exc))

    message = (
        f"{user.email_normalized} is disabled. Any session it had is now refused."
        if target == UserState.DISABLED
        else f"{user.email_normalized} is active again. They will need to sign in."
    )
    return _redirect(USERS_URL, ok=message)


@router.post("/users/{user_id}/role")
def change_role(
    request: Request,
    user_id: uuid.UUID,
    role: str = Form(default=""),
    db: Session = Depends(get_db),
) -> Response:
    """Grant or remove the administrator role.

    The service refuses to remove the last active administrator, which is the one
    mistake on this screen that cannot be undone from this screen.
    """

    user = user_service.get_by_id(db, user_id)
    if user is None:
        return _error_page(request, db, "That account no longer exists.")
    try:
        target = UserRole(role)
    except ValueError:
        return _error_page(request, db, "Unrecognised role.")

    try:
        user_service.set_role(db, user=user, role=target, actor=_actor())
    except user_service.UserServiceError as exc:
        db.rollback()
        return _error_page(request, db, str(exc))

    return _redirect(
        USERS_URL,
        ok=f"{user.email_normalized} is now {target.value}. Their existing sessions were ended.",
    )


@router.post("/users/{user_id}/link")
def issue_link(
    request: Request,
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Issue a fresh one-time password link, invalidating any earlier one.

    This is the whole of "password reset" until transactional email exists. It
    never reveals an existing password — there is nothing to reveal, only a hash —
    and it never creates a temporary one.
    """

    user = user_service.get_by_id(db, user_id)
    if user is None:
        return _error_page(request, db, "That account no longer exists.")
    try:
        issued = user_service.issue_credential_link(db, user=user, actor=_actor())
    except user_service.UserServiceError as exc:
        db.rollback()
        return _error_page(request, db, str(exc))

    _pending_links[str(user.id)] = _setup_url(request, issued.raw_token)
    return _redirect(
        f"{USERS_URL}?issued={user.id}",
        ok=f"A new link was created for {user.email_normalized}. Earlier links no longer work.",
    )


def _error_page(request: Request, db: Session, message: str) -> HTMLResponse:
    """Re-render the list with the refusal on it, at 400.

    A redirect with a flash would be simpler, but a 400 is the honest status for a
    refused write, and re-rendering keeps the administrator on the page they were
    looking at rather than bouncing them through a GET.
    """

    settings = get_settings()
    return _render(
        request,
        db,
        "admin_users.html",
        {
            "page_title": "People",
            "active_nav": "",
            "rows": [_row(user) for user in user_service.list_users(db)],
            "issued_link": None,
            "issued_for": None,
            "auth_enabled": settings.auth.enabled,
            "bootstrap_admin": settings.auth.bootstrap_admin_email,
            "flash_err": message,
        },
        status_code=400,
    )


def _setup_url(request: Request, raw_token: str) -> str:
    """The absolute link an administrator sends out of band.

    Built from the configured public origin rather than from the ``Host`` header:
    a URL derived from an attacker-influenced header is how a password-setup link
    ends up pointing at somebody else's site. Falls back to a relative path when
    no public origin is configured, which is the local-development case.
    """

    from urllib.parse import quote

    base = get_settings().auth.public_base_url or ""
    return f"{base}/auth/setup?token={quote(raw_token, safe='')}"

"""Server-side authorization for the administrator-only surface.

One dependency, declared on the admin router, and it reads the role the
authentication middleware wrote into the request scope — not a role from the
session cookie, and not a role recomputed from an email domain.

Why the role comes from the scope
---------------------------------
The middleware resolves the account record on every authenticated request and
puts the *current* role in ``scope["state"]["operator_role"]``. A dependency that
re-derived the role from the cookie would be trusting a value minted up to twelve
hours ago, so demoting an administrator would not take effect until their session
expired. Reading it from the scope means the demotion applies to the very next
request, and it means there is exactly one place in the application that decides
what somebody's role is.

Why hiding the link is not enough
---------------------------------
The navigation only shows the users screen to an administrator, and that is a
courtesy, not a control. This dependency is what actually refuses: an ordinary
user who types ``/app/admin/users`` into the address bar, or POSTs to
``/app/admin/users/create`` with a valid session and a valid CSRF token, is
refused here before the handler body runs.

Local development
-----------------
When hosted authentication is switched off there is no session, no account record
and no role — the middleware records ``auth_enforced = False`` and every operator
surface is already open. The dependency is inert in that case, exactly like the
CSRF dependency next to it, so a developer running locally sees the screen and
the behaviour is unchanged from before this slice.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from app.models.enums import UserRole


class AdminRequiredError(Exception):
    """Raised when a non-administrator reaches an administrator-only route.

    Rendered as a 403 by the application's handler. The message deliberately says
    what is required rather than what the caller is, and it names no account.
    """

    def __init__(self) -> None:
        super().__init__("This area is limited to platform administrators.")


def operator_role(request: Request) -> UserRole | None:
    """The signed-in account's current role, or ``None`` when there is none."""

    state: dict[str, Any] = request.scope.get("state") or {}
    raw = state.get("operator_role")
    if not isinstance(raw, str):
        return None
    try:
        return UserRole(raw)
    except ValueError:  # pragma: no cover - the middleware only writes valid values
        return None


def is_admin_request(request: Request) -> bool:
    """Whether this request may use the administrator surface.

    ``True`` when authentication is disabled, because there is then no account
    directory to consult and the whole application is already unauthenticated —
    see the module docstring.
    """

    state: dict[str, Any] = request.scope.get("state") or {}
    if not state.get("auth_enforced"):
        return True
    return operator_role(request) == UserRole.ADMIN


async def require_admin(request: Request) -> None:
    """Refuse any request that is not an active administrator's.

    Installed as a router-level dependency so that a route added to the admin
    router later is protected the moment it is registered, rather than when
    somebody remembers to decorate it.
    """

    if not is_admin_request(request):
        raise AdminRequiredError

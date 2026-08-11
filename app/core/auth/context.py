"""Request-scoped authentication state.

Two context variables, set by the authentication middleware and read by the CSRF
dependency, the templates and the sign-in routes. They live in their own module
so that ``csrf`` and ``middleware`` can both use them without importing each
other — the alternative was an import cycle, and the alternative to *that* was
threading two values through several hundred call sites.

The pattern matches ``current_request_id()`` in ``app/core/http.py``, which is
already how this application carries per-request state.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.core.auth.session import OperatorSession

_csrf_token_context: ContextVar[str | None] = ContextVar("vmr_csrf_token", default=None)
_operator_context: ContextVar[OperatorSession | None] = ContextVar("vmr_operator", default=None)


def set_current_csrf_token(token: str | None) -> Token[str | None]:
    """Bind the CSRF token for the current request; the caller resets it."""

    return _csrf_token_context.set(token)


def reset_current_csrf_token(token: Token[str | None]) -> None:
    _csrf_token_context.reset(token)


def current_csrf_token() -> str | None:
    """The CSRF token for the in-flight request, when there is one."""

    return _csrf_token_context.get()


def set_current_operator(
    session: OperatorSession | None,
) -> Token[OperatorSession | None]:
    return _operator_context.set(session)


def reset_current_operator(token: Token[OperatorSession | None]) -> None:
    _operator_context.reset(token)


def current_operator() -> OperatorSession | None:
    """The signed-in operator for the in-flight request, when there is one."""

    return _operator_context.get()


def signed_in_operator_email() -> str:
    """The signed-in operator's address, or ``""`` — for templates.

    Returns a plain string rather than ``None`` so a shell can write
    ``{{ signed_in_operator_email() }}`` without a guard, and renders nothing at
    all when authentication is disabled.
    """

    session = current_operator()
    return session.email if session is not None else ""

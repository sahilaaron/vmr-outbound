"""The customer shell: navigation, rendering and the helpers every page shares.

Four customer destinations — Today, Campaigns, People, Library — plus a
role-gated Admin entry. No badges, no counts, no machinery in the header. See
``docs/CUSTOMER_OPERATING_MODEL.md`` and the Pass 2 IA.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.auth.accounts import session_account_id
from app.core.auth.admin import is_admin_request
from app.core.auth.context import current_operator
from app.core.auth.csrf import register_csrf, require_csrf
from app.core.config import Settings, get_settings
from app.services.campaign_access import require_campaign_path_access
from app.services.gmail import mailbox as gmail_mailbox
from app.services.imports import display
from app.services.operations import settings as operational
from app.services.seller import profile as seller_profile

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent.parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

#: The one customer router. Every page module registers on it. Every
#: state-changing route is refused without the session's CSRF token, and every
#: route with a ``{campaign_id}`` path parameter is scoped to Campaigns the
#: caller may use — both declared once, here.
router = APIRouter(
    prefix="/app",
    include_in_schema=False,
    dependencies=[Depends(require_csrf), Depends(require_campaign_path_access)],
)

PAGE_SIZE = 25


def _asset_version(name: str) -> str:
    """A content hash, so a deploy that changes an asset changes its URL."""

    return sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:12]


V2_CSS_VERSION = _asset_version("v2.css")
LIVE_JS_VERSION = _asset_version("live.js")
SEQUENCE_JS_VERSION = _asset_version("sequence.js")
CAMPAIGNS_JS_VERSION = _asset_version("campaigns.js")
DESK_JS_VERSION = _asset_version("desk.js")


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NavItem:
    key: str
    label: str
    href: str


#: The whole customer navigation. Emails, Review, Agents and Capture are not
#: destinations: emails are Campaign output, review is an optional action on
#: an email, and the rest is machinery that lives in Admin.
PRIMARY_NAV: tuple[NavItem, ...] = (
    NavItem("today", "Today", "/app"),
    NavItem("campaigns", "Campaigns", "/app/campaigns"),
    NavItem("people", "People", "/app/people"),
    NavItem("library", "Library", "/app/library"),
)

ADMIN_NAV = NavItem("admin", "Admin", "/app/admin")


def primary_nav() -> tuple[NavItem, ...]:
    return PRIMARY_NAV


# ---------------------------------------------------------------------------
# Template filters
# ---------------------------------------------------------------------------


def _fmt_dt(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%d %b %Y %H:%M")
    return "—" if value is None else str(value)


def _fmt_time(value: Any) -> str:
    return value.strftime("%H:%M") if isinstance(value, datetime) else "—"


def _fmt_day(value: Any) -> str:
    return value.strftime("%d %b") if isinstance(value, datetime) else "—"


def _ago(value: Any) -> str:
    """A relative age in the compact vocabulary (4m, 3h, 2d)."""

    if not isinstance(value, datetime):
        return "—"
    moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    seconds = max(0, int((datetime.now(UTC) - moment).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _when(value: Any) -> str:
    """A human relative phrase: 'just now', '3 hours ago', '2 days ago', or a date."""

    if not isinstance(value, datetime):
        return "—"
    moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    seconds = max(0, int((datetime.now(UTC) - moment).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = seconds // 86400
    if days < 14:
        return f"{days} day{'s' if days != 1 else ''} ago"
    return moment.strftime("%d %b %Y")


def _thousands(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def _titlecase(value: Any) -> str:
    return str(value).replace("_", " ").replace("-", " ").strip().capitalize()


def _initials(value: Any) -> str:
    words = [word for word in str(value).replace("-", " ").split() if word]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    if words:
        return words[0][:2].capitalize()
    return "—"


def _plural(count: Any, singular: str, plural: str | None = None) -> str:
    try:
        number = int(count)
    except (TypeError, ValueError):
        number = 0
    word = singular if number == 1 else (plural or f"{singular}s")
    return f"{number:,} {word}"


display.register_neutralize(templates.env)
register_csrf(templates.env)
templates.env.filters["dt"] = _fmt_dt
templates.env.filters["clock"] = _fmt_time
templates.env.filters["day"] = _fmt_day
templates.env.filters["ago"] = _ago
templates.env.filters["when"] = _when
templates.env.filters["thousands"] = _thousands
templates.env.filters["nice"] = _titlecase
templates.env.filters["initials"] = _initials
templates.env.globals["plural"] = _plural


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def operator_identity(session: Session, settings: Settings) -> tuple[str, str, str]:
    """What the account chip says: name, address (or environment), initials."""

    operator = current_operator()
    if operator is not None:
        signed_in = operator.display_name or operator.email
        parts = [word for word in signed_in.replace("-", " ").replace("@", " ").split() if word]
        marks = "".join(word[0] for word in parts[:2]).upper() or "VM"
        return signed_in, operator.email, marks

    name = "Operator"
    try:
        profile = seller_profile.get_profile(session)
        if profile is not None and profile.name:
            name = profile.name
    except Exception:
        pass
    context = f"{settings.app_env.upper()} · no sign-in in this environment"
    words = [word for word in name.replace("-", " ").split() if word]
    initials = "".join(word[0] for word in words[:2]).upper() or "VM"
    return name, context, initials


def database_ok(db: Session) -> bool:
    try:
        db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def render(
    request: Request,
    db: Session,
    template: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    settings = get_settings()
    features = operational.effective_flags(db, settings)
    name, email, initials = operator_identity(db, settings)
    shared: dict[str, Any] = {
        "app_env": settings.app_env,
        "features_enabled": features.enabled(),
        "database_ok": database_ok(db),
        "primary_nav": primary_nav(),
        "admin_nav": ADMIN_NAV,
        "operator_name": name,
        "operator_email": email,
        "operator_initials": initials,
        "is_admin": is_admin_request(request),
        "signed_in": current_operator() is not None,
        "v2_css_version": V2_CSS_VERSION,
        "live_js_version": LIVE_JS_VERSION,
        "sequence_js_version": SEQUENCE_JS_VERSION,
        "campaigns_js_version": CAMPAIGNS_JS_VERSION,
        "desk_js_version": DESK_JS_VERSION,
        "flash_ok": request.query_params.get("ok"),
        "flash_err": request.query_params.get("err"),
    }
    shared.update(context)
    return templates.TemplateResponse(
        request=request, name=template, context=shared, status_code=status_code
    )


def redirect(url: str, *, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    """Redirect with a flash message, appended correctly to any query/fragment."""

    params = {key: value for key, value in (("ok", ok), ("err", err)) if value}
    if not params:
        return RedirectResponse(url, status_code=303)
    base, marker, fragment = url.partition("#")
    separator = "&" if "?" in base else "?"
    return RedirectResponse(
        f"{base}{separator}{urlencode(params)}{marker}{fragment}", status_code=303
    )


def not_found(request: Request, db: Session, message: str) -> HTMLResponse:
    return render(
        request,
        db,
        "not_found.html",
        {"message": message, "active_nav": "", "page_title": "Not found"},
        status_code=404,
    )


def uuid_or_none(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def pages(total: int, size: int = PAGE_SIZE) -> int:
    return max(1, (total + size - 1) // size)


def checkbox(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def in_app_path(target: str | None, fallback: str) -> str:
    """Constrain a submitted redirect target to this application."""

    candidate = (target or "").strip()
    if not candidate.startswith("/app") or candidate.startswith("//"):
        return fallback
    return candidate


# ---------------------------------------------------------------------------
# Feature switches
# ---------------------------------------------------------------------------


def kb_on(db: Session, settings: Settings) -> bool:
    return operational.enabled(db, "seller_knowledge_base", settings)


def sequences_on(db: Session, settings: Settings) -> bool:
    return operational.enabled(db, "email_sequences", settings)


def import_on(db: Session, settings: Settings) -> bool:
    return operational.enabled(db, "csv_import", settings)


def agent_workbench_on(db: Session, settings: Settings) -> bool:
    return operational.enabled(db, "agent_workbench", settings)


def gmail_drafts_on(db: Session, settings: Settings) -> bool:
    enabled = operational.effective_flags(db, settings).enabled()
    return "gmail_drafts" in enabled and "email_sequences" in enabled


def sheets_on(db: Session, settings: Settings) -> bool:
    return "google_sheets_integration" in operational.effective_flags(db, settings).enabled()


def capture_on(db: Session, settings: Settings) -> bool:
    return "contact_capture_intake" in operational.effective_flags(db, settings).enabled()


def mailbox_state(db: Session, settings: Settings) -> gmail_mailbox.MailboxState:
    """The connected-mailbox state for the operator making this request."""

    if not gmail_drafts_on(db, settings):
        return gmail_mailbox.UNAVAILABLE
    owner = session_account_id(current_operator())
    if owner is None:
        return gmail_mailbox.UNAVAILABLE
    return gmail_mailbox.mailbox_state(db, user_id=owner, settings=settings.gmail, feature_on=True)


__all__ = [
    "ADMIN_NAV",
    "router",
    "CAMPAIGNS_JS_VERSION",
    "LIVE_JS_VERSION",
    "NavItem",
    "PAGE_SIZE",
    "PRIMARY_NAV",
    "SEQUENCE_JS_VERSION",
    "V2_CSS_VERSION",
    "agent_workbench_on",
    "capture_on",
    "checkbox",
    "database_ok",
    "gmail_drafts_on",
    "import_on",
    "in_app_path",
    "kb_on",
    "mailbox_state",
    "not_found",
    "operator_identity",
    "pages",
    "primary_nav",
    "redirect",
    "render",
    "sequences_on",
    "sheets_on",
    "templates",
    "uuid_or_none",
]

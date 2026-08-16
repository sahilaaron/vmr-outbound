"""The customer-facing application at ``/app``.

One router, declared in :mod:`app.web.v2.shell`, populated by one module per
destination in :mod:`app.web.v2.pages`. Importing the page modules is what
registers their routes.
"""

from __future__ import annotations

from app.web.v2 import shell
from app.web.v2.pages import (  # noqa: F401 - registration by import
    account,
    admin,
    campaigns,
    emails,
    imports,
    legacy,
    library,
    people,
    today,
)
from app.web.v2.pages.emails import GMAIL_PROVIDER_STATE_KEY
from app.web.v2.pages.imports import _sheet_index
from app.web.v2.shell import (
    CAMPAIGNS_JS_VERSION,
    LIVE_JS_VERSION,
    SEQUENCE_JS_VERSION,
    V2_CSS_VERSION,
    templates,
)

router = shell.router

__all__ = [
    "CAMPAIGNS_JS_VERSION",
    "GMAIL_PROVIDER_STATE_KEY",
    "LIVE_JS_VERSION",
    "SEQUENCE_JS_VERSION",
    "V2_CSS_VERSION",
    "_sheet_index",
    "router",
    "shell",
    "templates",
]

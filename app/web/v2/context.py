"""Shell context for the customer-facing interface.

One thing lives here and nowhere else: the navigation model.

It used to carry a second thing — a set of "what wants you" counts that the nav
badged and the Today page summed into a single number. That number is gone, and
its absence is the product decision rather than a tidy-up. VMR Outbound is
autonomous until Ready for Sending: failed Agent jobs, blocked stages,
unresolved enrichment and unreviewed messages are the system's work, not a
backlog the customer owes it. A badge counting them told the customer they were
behind on work they were never given. See ``docs/CUSTOMER_OPERATING_MODEL.md``.

Customer navigation therefore carries no counts at all. Admin diagnostics still
count every one of those things — see ``app/web/admin_workbench.py``, which has
its own attention model and keeps it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.auth.context import current_operator
from app.core.config import Settings
from app.models.company_domain_resolution import CompanyDomainResolution
from app.models.enums import (
    DomainResolutionState,
)
from app.services.seller import profile as seller_profile

#: Nav items whose section the design names, in the design's order.
NAV_MODEL: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
    (
        "Work",
        (
            ("today", "Today", "/app"),
            ("campaigns", "Campaigns", "/app/campaigns"),
            # Not "Review": under the customer contract this is where the
            # generated emails are read, copied and optionally edited, not a
            # queue anybody has to clear.
            ("emails", "Emails", "/app/review"),
        ),
    ),
    (
        "Records",
        (
            ("contacts", "Contacts", "/app/contacts"),
            ("companies", "Companies", "/app/companies"),
        ),
    ),
    (
        "Setup",
        (("knowledge", "Knowledge Base", "/app/knowledge"),),
    ),
)


@dataclass(frozen=True)
class NavItem:
    key: str
    label: str
    href: str


@dataclass(frozen=True)
class NavGroup:
    title: str
    items: tuple[NavItem, ...]


def _safe(fn: Any, default: int = 0) -> int:
    """A count must never take a page down.

    A count that cannot be read (a missing table on a half-migrated database, a
    dropped connection) renders as zero rather than raising: the page's job is to
    show what is happening, and it cannot do that from a stack trace.
    """

    try:
        return int(fn())
    except Exception:
        return default


def provisional_domain_count(session: Session) -> int:
    """Companies running on a domain the policy only accepted provisionally.

    Counted directly against the current decision rows: provisional and unresolved
    are different states with different consequences, and one must never be
    reported as the other.
    """

    def _count() -> int:
        return (
            session.scalar(
                select(func.count(CompanyDomainResolution.id)).where(
                    CompanyDomainResolution.is_current.is_(True),
                    CompanyDomainResolution.state == DomainResolutionState.PROVISIONAL,
                )
            )
            or 0
        )

    return _safe(_count)


def nav_groups() -> tuple[NavGroup, ...]:
    """The design's three nav groups, and no badges.

    Takes no counts, deliberately. A badge here can only ever say "you are behind
    on N things", and under the customer operating model there is no N: the
    system does the work. Making the function unable to receive a count is what
    stops the badge growing back.
    """

    groups: list[NavGroup] = []
    for title, items in NAV_MODEL:
        built = tuple(NavItem(key=key, label=label, href=href) for key, label, href in items)
        groups.append(NavGroup(title=title, items=built))
    return tuple(groups)


def operator_identity(session: Session, settings: Settings) -> tuple[str, str, str]:
    """What the account chip says.

    Since the user-accounts slice there *is* a person to name on a hosted
    deployment, so the chip names them: the display name on their account, or
    their address when the account has no name, with the address underneath.

    Local development still has no session, and the old behaviour is kept exactly
    for it — the chip carries the seller identity from the Knowledge Base and says
    plainly that nobody is signed in. Inventing an operator name there would make
    an unauthenticated environment look authenticated, which is the one thing this
    chip must never do.
    """

    operator = current_operator()
    if operator is not None:
        signed_in = operator.display_name or operator.email
        parts = [word for word in signed_in.replace("-", " ").replace("@", " ").split() if word]
        marks = "".join(word[0] for word in parts[:2]).upper() or "VM"
        return signed_in, operator.email, marks

    name = "Operator"

    def _profile_name() -> str:
        profile = seller_profile.get_profile(session)
        return profile.name if profile is not None and profile.name else ""

    try:
        entered = _profile_name()
    except Exception:
        entered = ""
    if entered:
        name = entered

    context = f"{settings.app_env.upper()} · no sign-in in this environment"
    words = [word for word in name.replace("-", " ").split() if word]
    initials = "".join(word[0] for word in words[:2]).upper() or "VM"
    return name, context, initials

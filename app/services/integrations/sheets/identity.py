"""From a verified Google assertion to the VMR account it is allowed to act as.

Three questions are answered in three places and this module answers only the
middle one. ``app/core/auth/sheets_assertion.py`` decides whether the assertion
is trustworthy. This decides which **active** VMR account it names, if any.
``app/services/campaign_access.py`` then decides what that account may reach.

Nothing here creates an account. "Google authenticated them" is not
authorization: an assertion for a Google identity with no VMR account is refused
exactly like a forged one, and the caller cannot tell the two apart.

The account lookup deliberately reuses ``users.resolve_google_identity`` — the
same function the browser sign-in uses — rather than a parallel copy. That gives
this surface, for free and by construction, every refusal that path already
proves: an unknown identity, a disabled account, a Google subject already linked
to a different account, and an account already carrying a different subject. The
one visible side effect is the one the shared function performs: the first
successful call links the Google subject to the account and stamps
``last_login_at``, so an operator's add-on and browser resolve to one account
rather than to two.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.auth.sheets_assertion import VerifiedAssertion
from app.models.user import User
from app.services import campaign_access
from app.services.users import service as users


class IntegrationAccountError(Exception):
    """The assertion verified but names no account this deployment will act as.

    One message for every cause, on purpose. "No such account", "account
    disabled" and "that Google identity belongs to someone else" are different
    facts about the deployment, and telling a caller which one applies turns this
    endpoint into an account-enumeration oracle.
    """


def resolve_account(session: Session, assertion: VerifiedAssertion) -> User:
    """Return the active VMR account the assertion names, or refuse."""

    user = users.resolve_google_identity(
        session,
        email=assertion.email,
        subject=assertion.subject,
        display_name=assertion.display_name,
    )
    if user is None:
        raise IntegrationAccountError(
            "this Google account is not connected to an active VMR Outbound account"
        )
    return user


def actor_for(user: User) -> campaign_access.CampaignActor:
    """The Campaign-access actor for an authenticated add-on caller.

    ``enforced=True`` always. The unenforced actor exists for deployments running
    with authentication switched off entirely, and an external HTTP client must
    never be able to reach it: this surface is reachable from the public internet
    whatever the local development switches say.
    """

    return campaign_access.CampaignActor(user_id=user.id, role=user.role, enforced=True)


def actor_label(user: User) -> str:
    """The audit/provenance actor string for work this account asked for."""

    return f"google-sheets:{user.email_normalized}"


__all__ = ["IntegrationAccountError", "actor_for", "actor_label", "resolve_account"]

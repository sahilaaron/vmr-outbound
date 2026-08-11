"""The startup contract: unsafe hosted states must be unstartable.

This replaces the temporary rule ``FEATURES__WORKBENCH is only permitted when
APP_ENV=local``. That rule was correct for what it guarded — an operator UI with
no authentication — but it guarded the wrong thing. It said nothing about the
25 state-changing API routes that mount in *every* environment, including
``POST /api/campaigns`` and ``POST /campaigns``, which is what issue #247 is
about.

The replacement contract is stated in one place:

Development (``local`` / ``development`` / ``test`` / ``ci``)
    Unchanged. Authentication defaults off, the workbench mounts as it always
    did, and no developer is pushed through Google to use localhost. A developer
    who *wants* to exercise the real sign-in locally may enable it, and then the
    same completeness checks apply.

``staging``
    Hosted. Authentication is mandatory — not because the workbench is on, but
    because the application is reachable from the Internet at all. Every part of
    the boundary must be present and coherent: a signing secret, a non-empty
    approved-operator list, a complete Google identity client, an HTTPS public
    origin, and secure cookies.

``production``
    No operator surface. The production access policy has not been decided, so
    the workbench is refused outright rather than inheriting the staging rule by
    accident. Authentication is still mandatory for the API surface.

Everything is accumulated and reported at once: an operator setting this up for
the first time should learn about all four missing values in one restart, not
four.

One dependency worth stating rather than leaving implicit
---------------------------------------------------------
An ``APP_ENV`` this module does not recognise — ``prod``, ``stage``, ``beta``, a
typo — is treated as development here, which on its own would mean a typo in one
environment variable silently disables the entire boundary. It does not, because
``create_app`` calls ``validate_runtime_settings`` immediately after this
function and that check refuses any environment name outside the six known ones.
The safety of *this* module therefore depends on that call site ordering. Do not
reorder them, and do not call ``validate_hosted_auth_settings`` on its own and
conclude from a clean return that a configuration is safe.
"""

from __future__ import annotations

from app.core.auth.config import MIN_SESSION_SECRET_CHARS
from app.core.config import Settings

DEVELOPMENT_ENVIRONMENTS: frozenset[str] = frozenset({"local", "development", "test", "ci"})
HOSTED_ENVIRONMENTS: frozenset[str] = frozenset({"staging", "production"})


class HostedAuthConfigurationError(RuntimeError):
    """Raised at startup when the hosted authentication boundary is incomplete.

    Refusing to start is the point. A skipped guard or a warning in a log would
    leave operator surfaces and campaign writes reachable by anyone on the
    Internet, and the symptom — a working site — looks exactly like success.
    """


def validate_hosted_auth_settings(settings: Settings) -> None:
    """Refuse any configuration that would expose operator surfaces anonymously."""

    environment = settings.app_env.strip().lower()
    auth = settings.auth
    issues: list[str] = []

    hosted = environment in HOSTED_ENVIRONMENTS

    if hosted and not auth.enabled:
        issues.append(
            "AUTH__ENABLED must be true in staging and production: the campaign write "
            "endpoints and every operator surface are otherwise reachable anonymously"
        )

    if environment == "production" and settings.features.workbench:
        issues.append(
            "FEATURES__WORKBENCH may not be enabled in production: the production "
            "operator-access policy is not defined yet, so it fails closed"
        )

    if settings.features.workbench and environment not in (DEVELOPMENT_ENVIRONMENTS | {"staging"}):
        issues.append("FEATURES__WORKBENCH is permitted only in local development or staging")

    if auth.enabled:
        if not auth.has_session_secret():
            issues.append(
                "AUTH__SESSION_SECRET must be set and at least "
                f"{MIN_SESSION_SECRET_CHARS} characters long when authentication is enabled"
            )
        if not auth.allowed_operator_emails:
            issues.append(
                "AUTH__ALLOWED_OPERATOR_EMAILS must name at least one approved operator: "
                "an empty allow-list means nobody, and starting with one would refuse "
                "every sign-in while looking healthy"
            )
        if not auth.has_google_client():
            issues.append(
                "AUTH__GOOGLE_CLIENT_ID and AUTH__GOOGLE_CLIENT_SECRET are both required "
                "when authentication is enabled"
            )
        if auth.public_base_url is None:
            issues.append(
                "AUTH__PUBLIC_BASE_URL must name the canonical external origin: the OAuth "
                "redirect URI is built from it and must match Google Cloud Console exactly"
            )
        elif hosted and not auth.public_base_url.startswith("https://"):
            issues.append("AUTH__PUBLIC_BASE_URL must use HTTPS in staging and production")
        if hosted and not auth.cookie_secure:
            issues.append(
                "AUTH__COOKIE_SECURE may not be false in staging or production: the session "
                "cookie would be sent over plaintext HTTP"
            )

    if issues:
        detail = "\n".join(f"- {issue}" for issue in issues)
        raise HostedAuthConfigurationError(
            f"Unsafe {environment or 'unset'} hosted-authentication configuration:\n{detail}"
        )

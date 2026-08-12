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


def _extension_auth_issues(settings: Settings, *, environment: str, hosted: bool) -> list[str]:
    """Refuse every half-configured shape of the extension capture boundary.

    The failure this prevents is the quiet one. A capture credential that is
    enabled but has no issued credential, or no approved origin, produces a
    deployment that starts cleanly, serves every operator screen, and refuses
    every capture — with nothing to tell the operator whether the credential is
    wrong, the origin is wrong, or the boundary was never configured at all.

    Production is refused outright rather than inheriting the staging rule. It
    has no operator surface (``FEATURES__WORKBENCH`` is already refused there),
    and capture is an operator activity, so a capture credential in production
    would be a credential with nowhere to send its work and no screen to review
    it on. The production access policy is a decision, not a default.
    """

    extension = settings.extension_auth
    issues: list[str] = []

    if not extension.enabled:
        # Configuration present but switched off is fine and is how a credential
        # is staged before it is turned on. Nothing is accepted while it is off.
        return issues

    if environment == "production":
        issues.append(
            "EXTENSION_AUTH__ENABLED may not be true in production: capture is an "
            "operator activity and the production operator-access policy is not defined yet"
        )

    if not extension.credentials:
        issues.append(
            "EXTENSION_AUTH__CREDENTIALS must list at least one issued credential when "
            "extension capture authentication is enabled: an empty list means nobody, and "
            "starting with one would refuse every capture while looking healthy"
        )
    if not extension.allowed_origins:
        issues.append(
            "EXTENSION_AUTH__ALLOWED_ORIGINS must name at least one approved "
            "chrome-extension:// origin: a credential with no approved origin can never "
            "complete a capture"
        )
    if hosted and not settings.auth.enabled:
        issues.append(
            "EXTENSION_AUTH__ENABLED requires AUTH__ENABLED in staging and production: the "
            "capture credential authorises one narrow intake contract and is not a "
            "substitute for the operator session protecting everything else"
        )
    if not settings.features.contact_capture_intake:
        issues.append(
            "FEATURES__CONTACT_CAPTURE_INTAKE must be true when extension capture "
            "authentication is enabled: the intake route does not exist otherwise, so every "
            "authenticated capture would answer 404"
        )

    return issues


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
        if not auth.bootstrap_admin_email:
            # Replaces the old "the allow-list must not be empty" rule, for the
            # same reason it existed: a hosted deployment that nobody can sign in
            # to starts cleanly and looks healthy, and the operator finds out at
            # the door with no way to tell whether the fault is theirs.
            #
            # The rule moved because the authority moved. Access now comes from
            # the `users` table, so "at least one account exists" is the property
            # that matters, and the bootstrap administrator is what guarantees it.
            # `AUTH__ALLOWED_OPERATOR_EMAILS` may now legitimately be empty: it is
            # a one-time seed for deployments that predate accounts, not a gate.
            issues.append(
                "AUTH__BOOTSTRAP_ADMIN_EMAIL must name the platform administrator: "
                "access is granted by an account record, and a deployment with no "
                "administrator has no way to create the first one"
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

    issues.extend(_extension_auth_issues(settings, environment=environment, hosted=hosted))

    if issues:
        detail = "\n".join(f"- {issue}" for issue in issues)
        raise HostedAuthConfigurationError(
            f"Unsafe {environment or 'unset'} hosted-authentication configuration:\n{detail}"
        )

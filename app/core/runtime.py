"""Startup validation for local, staging, and production runtime settings."""

from __future__ import annotations

from ipaddress import IPv4Address, ip_address, ip_network

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app.core.config import Settings

_ENVIRONMENTS = frozenset({"local", "development", "test", "ci", "staging", "production"})
_PRODUCTION_LIKE = frozenset({"staging", "production"})
_LOCAL_HOSTS = frozenset({"localhost", "postgres", "db", "database"})
_LOCAL_ONLY_FEATURES = (
    "salesnav_intake",
    "linkedin_profile_intake",
    "linkedin_company_intake",
)

# Contact capture used to sit in the list above, because the intake route had no
# authentication of its own and "local only" was the entire boundary. It now has
# one — a per-install bearer credential bound to the enumerated capture contract
# and to an approved extension origin (``app/core/auth/extension.py``) — so the
# rule becomes conditional rather than absolute: the feature may be enabled in a
# hosted environment exactly when that boundary is configured and would actually
# admit a request. Enabling it hosted *without* the credential boundary is still
# refused, and that is the case the old rule was really protecting against.
_CREDENTIAL_GATED_FEATURES = ("contact_capture_intake",)


# --- The hosted Beta's one promotion exception --------------------------------
#
# ``contact_capture_promotion`` also used to sit in the local-only list, and for
# a better reason than the intakes did: promotion is the step that turns
# extension evidence into a canonical Contact, a Company association and — when
# the capture carried an explicit filing request — a Campaign Contact. Doing
# that in an environment reachable from the Internet, with nothing stated about
# what has to be true first, is exactly what "local only" existed to refuse.
#
# The hosted Beta needs it, so the rule becomes conditional rather than absolute
# — the same move the capture credential made above, held to the same standard.
# Deleting the name from the list would not have been that move: it would have
# permitted promotion in every hosted environment, in every state of
# configuration, including the half-configured one this boundary exists to
# refuse. So the exception is written out instead, and it is narrow in three
# separate ways.
#
# **One environment.** Staging, named here rather than derived from "hosted", so
# production does not inherit the Beta by accident. Production has no operator
# surface (``FEATURES__WORKBENCH`` is already refused there), and a promoted
# Contact nobody can review is not a product decision anyone has taken. When a
# production promotion path is wanted it is a design, not a default.
#
# **Every dependency, not just the switch.** Automatic promotion is the whole
# point of enabling it hosted, and automatic promotion is four things, not one:
# the promotion switch, the automatic-resolution switch, the provider switch and
# a provider key. ``app/services/resolution/pending.py`` and
# ``app/services/captures/intake.py`` both fail closed when any of them is
# missing — correctly, and silently, because a capture that is left untouched is
# the safe outcome and looks identical to one that was never submitted. That is
# the failure this refuses: a staging deployment that starts cleanly, accepts
# captures, files campaign requests, reports every Capture job as succeeded, and
# promotes nothing, with no error anywhere to explain why.
#
# **Nothing weakened to get there.** This grants no promotion authority. The
# DAT-014 rules are untouched: a domain is never fabricated, a provider rank is
# never confirmation, an ambiguous identity never merges, a suppressed person is
# never promoted, and a Campaign Contact appears only where the immutable
# capture already carried an explicit filing request. This decides which
# environments may *run* that service, and nothing about what it may conclude.
_STAGING_PROMOTION_ENVIRONMENTS = frozenset({"staging"})

# The switches automatic promotion cannot work without, paired with the exact
# environment-variable name to set. The variable name is carried here rather
# than spelled inside each message for the reason
# ``app/services/resolution/pending.py`` states about its operator-facing
# blockers: a name someone has to retype is worth naming exactly once.
_PROMOTION_REQUIRED_FEATURES: tuple[tuple[str, str], ...] = (
    (
        "automatic_company_domain_resolution",
        "FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION",
    ),
    ("salesnav_domain_enrichment", "FEATURES__SALESNAV_DOMAIN_ENRICHMENT"),
)


class RuntimeConfigurationError(RuntimeError):
    """Raised before serving when configuration is known to be unsafe."""


def _legacy_ipv4_address(host: str) -> IPv4Address | None:
    """Parse the legacy numeric IPv4 forms accepted by libc/libpq, without DNS."""

    def component(raw: str) -> int:
        if not raw:
            raise ValueError
        lowered = raw.lower()
        if lowered.startswith("0x"):
            if len(lowered) == 2:
                raise ValueError
            return int(lowered[2:], 16)
        if len(lowered) > 1 and lowered.startswith("0"):
            return int(lowered[1:] or "0", 8)
        return int(lowered, 10)

    if not host.isascii() or any(
        character not in "0123456789abcdefABCDEFxX." for character in host
    ):
        return None
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    try:
        values = [component(part) for part in parts]
    except ValueError:
        return None
    limits = {
        1: (0xFFFFFFFF,),
        2: (0xFF, 0xFFFFFF),
        3: (0xFF, 0xFF, 0xFFFF),
        4: (0xFF, 0xFF, 0xFF, 0xFF),
    }[len(values)]
    if any(value < 0 or value > limit for value, limit in zip(values, limits, strict=True)):
        return None
    if len(values) == 1:
        packed = values[0]
    elif len(values) == 2:
        packed = (values[0] << 24) | values[1]
    elif len(values) == 3:
        packed = (values[0] << 24) | (values[1] << 16) | values[2]
    else:
        packed = (values[0] << 24) | (values[1] << 16) | (values[2] << 8) | values[3]
    return IPv4Address(packed)


def _local_database_host(host: str | None) -> bool:
    if not host:
        return True
    lowered = host.lower().removesuffix(".")
    if lowered in _LOCAL_HOSTS or lowered.endswith(".local"):
        return True
    try:
        return ip_address(lowered).is_loopback
    except ValueError:
        legacy = _legacy_ipv4_address(lowered)
        return bool(legacy and legacy.is_loopback)


def _hosted_promotion_issues(settings: Settings, *, environment: str) -> list[str]:
    """Refuse a hosted capture promotion that is unauthorised or half-configured.

    Called only for the production-like environments, so local development is
    not merely unaffected by this rule — it never reaches it.

    The production refusal returns immediately instead of accumulating the
    dependency findings alongside it. Everything else in this module reports at
    once, deliberately, so one restart teaches an operator all of their missing
    values; here that would teach the wrong thing. Listing the three
    prerequisites under a refusal that no prerequisite can lift reads as a
    checklist, and the honest answer is that production is not a configuration
    problem.
    """

    if not settings.features.contact_capture_promotion:
        # Off is always allowed and is how staging runs until the operator
        # deliberately turns the Beta path on. Nothing is promoted while it is
        # off; captures stay staged and stay promotable later.
        return []

    if environment not in _STAGING_PROMOTION_ENVIRONMENTS:
        return [
            "FEATURES__CONTACT_CAPTURE_PROMOTION may not be enabled in "
            f"{environment}: hosted capture promotion is authorised for staging only. "
            "Promotion creates canonical Contacts and Campaign memberships from "
            "extension evidence, and production has no operator surface to review "
            "them on, so it fails closed until a production path is separately designed"
        ]

    issues = [
        f"{variable} must be true when FEATURES__CONTACT_CAPTURE_PROMOTION is enabled "
        "outside local development: without it a hosted capture is never resolved "
        "automatically, so the deployment would accept captures and promote none of "
        "them with no error to explain it"
        for name, variable in _PROMOTION_REQUIRED_FEATURES
        if not bool(getattr(settings.features, name))
    ]
    if not settings.has_logo_dev_key():
        issues.append(
            "LOGO_DEV_API_KEY must be configured when FEATURES__CONTACT_CAPTURE_PROMOTION "
            "is enabled outside local development: without a provider key the resolution "
            "policy declines to record a decision it could not make, so every hosted "
            "capture stays pending indefinitely"
        )
    return issues


def validate_runtime_settings(settings: Settings) -> None:
    """Refuse known-dangerous combinations without echoing secret values."""

    environment = settings.app_env.strip().lower()
    issues: list[str] = []

    if environment not in _ENVIRONMENTS:
        issues.append("APP_ENV must be local, development, test, ci, staging, or production")
    if settings.max_request_bytes < settings.max_upload_bytes:
        issues.append("MAX_REQUEST_BYTES must not be smaller than MAX_UPLOAD_BYTES")

    if not settings.trusted_hosts or any(
        not host.strip() or any(character in host for character in ("\r", "\n", "/"))
        for host in settings.trusted_hosts
    ):
        issues.append("TRUSTED_HOSTS must contain only bounded hostnames, without schemes or paths")

    proxy_networks = []
    for raw_network in settings.trusted_proxy_cidrs:
        try:
            proxy_networks.append(ip_network(raw_network, strict=False))
        except ValueError:
            issues.append("TRUSTED_PROXY_CIDRS contains an invalid network")
            break

    if environment in _PRODUCTION_LIKE:
        if settings.debug:
            issues.append("DEBUG must be false in staging and production")
        if not settings.dry_run:
            issues.append(
                "DRY_RUN must remain true until a send-capable deployment is separately approved"
            )
        if any("*" in host for host in settings.trusted_hosts):
            issues.append("TRUSTED_HOSTS may not contain wildcard hosts in staging or production")
        local_defaults = {"localhost", "127.0.0.1", "[::1]", "testserver"}
        if set(settings.trusted_hosts).issubset(local_defaults):
            issues.append("TRUSTED_HOSTS must name the deployed staging or production host")
        if any(network.prefixlen == 0 for network in proxy_networks):
            issues.append("TRUSTED_PROXY_CIDRS may not trust the entire address space")

        enabled_local_features = [
            name for name in _LOCAL_ONLY_FEATURES if bool(getattr(settings.features, name))
        ]
        if enabled_local_features:
            issues.append(
                "local-only feature switches must be disabled in staging and production: "
                + ", ".join(sorted(enabled_local_features))
            )

        if not settings.extension_auth.is_configured():
            ungated = [
                name
                for name in _CREDENTIAL_GATED_FEATURES
                if bool(getattr(settings.features, name))
            ]
            if ungated:
                issues.append(
                    "these feature switches require a configured extension capture "
                    "credential (EXTENSION_AUTH__ENABLED with at least one credential and "
                    "one approved origin) outside local development: " + ", ".join(sorted(ungated))
                )

        issues.extend(_hosted_promotion_issues(settings, environment=environment))

        try:
            database_url = make_url(settings.database_url)
        except (ArgumentError, ValueError):
            issues.append("DATABASE_URL is not a valid SQLAlchemy URL")
        else:
            if database_url.get_backend_name() != "postgresql":
                issues.append("DATABASE_URL must use PostgreSQL")
            if _local_database_host(database_url.host):
                issues.append("DATABASE_URL may not use a local or container-service host")
            if (database_url.database or "").lower() in {
                "",
                "postgres",
                "template0",
                "template1",
                "vmr_dev",
            }:
                issues.append("DATABASE_URL must name a dedicated non-development database")

    if issues:
        label = environment or "unset"
        detail = "\n".join(f"- {issue}" for issue in issues)
        raise RuntimeConfigurationError(f"Unsafe {label} runtime configuration:\n{detail}")

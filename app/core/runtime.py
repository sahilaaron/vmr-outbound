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
    "contact_capture_intake",
    "contact_capture_promotion",
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

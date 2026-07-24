"""Database connection-safety rules (FND-009).

One module owns every rule about *where* the application may connect and *how*
that connection must behave:

* **Explicit targets.** ``DATABASE_TARGET`` selects the active database mode:
  ``local`` (a loopback development Postgres — the default) or ``rds-dev``
  (the shared development RDS instance). Each mode has hard requirements and
  the configuration fails closed when they are not met, so the application can
  never drift onto a remote database by accident or onto an unencrypted
  connection deliberately.
* **Secrets stay local.** In ``rds-dev`` mode the ``DATABASE_URL`` must be
  supplied explicitly through the local environment/``.env``; the committed
  local default is refused. No RDS endpoint or credential ever lives in source.
* **Mandatory encryption.** A non-loopback URL must carry a strong ``sslmode``
  (``require`` / ``verify-ca`` / ``verify-full``) and every live connection is
  additionally checked for TLS at connect time — if encryption cannot be
  positively confirmed the connection is refused (fail closed, never fail open).
* **Masked connection details.** :func:`mask_database_url` is the only approved
  way to render a database URL for logs, errors, CLI output, HTML, or audit
  records. It always removes the password and, for any non-loopback target,
  the username and host as well.
* **Local-only operations refuse remote hosts.** Destructive or bootstrap
  helpers (``scripts/dev_up.py``, workbench reset/fixtures, the pytest suite,
  ``alembic downgrade``) call the guards here and refuse to touch a
  non-loopback host under any flag combination. Migrations may reach a
  non-loopback host only through the deliberate operator command
  (``scripts/rds_migrate.py``), which sets a one-shot process token.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from sqlalchemy.engine import URL, make_url

if TYPE_CHECKING:  # pragma: no cover - import cycle guard (config imports this module)
    from app.core.config import Settings

#: Hostnames considered local. Anything else is a remote/network database.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: libpq ``sslmode`` values that guarantee an encrypted channel. ``prefer`` and
#: weaker values silently fall back to cleartext, so they are refused for any
#: non-loopback target.
STRONG_SSL_MODES = frozenset({"require", "verify-ca", "verify-full"})

#: Environment variable + value that scripts/rds_migrate.py sets for its own
#: subprocesses. Alembic refuses to run against a non-loopback host without it.
RDS_MIGRATION_ENV_VAR = "VMR_RDS_MIGRATIONS"
RDS_MIGRATION_ENV_VALUE = "allow"

_MASKED_HOST = "[masked-host]"
_MASKED_USER = "[masked-user]"


class DatabaseConfigurationError(RuntimeError):
    """Raised when the database configuration violates a connection-safety rule."""


class RemoteDatabaseRefused(RuntimeError):
    """Raised when a local-only operation is pointed at a non-loopback database."""


def is_loopback_host(host: str | None) -> bool:
    """True when ``host`` is a loopback name/address (empty counts as not local)."""

    return bool(host) and host in LOOPBACK_HOSTS


def _parse(url: str | URL) -> URL:
    try:
        return make_url(str(url))
    except Exception as exc:  # noqa: BLE001 - any parse failure is a config error
        # Never echo the raw value: it may contain credentials.
        raise DatabaseConfigurationError(
            f"DATABASE_URL could not be parsed ({type(exc).__name__}); "
            "the value is not shown because it may contain credentials"
        ) from exc


def url_host(url: str | URL) -> str | None:
    """The hostname of a database URL (never logs the URL itself)."""

    return _parse(url).host


def url_sslmode(url: str | URL) -> str | None:
    """The ``sslmode`` query parameter of a database URL, if present."""

    value = _parse(url).query.get("sslmode")
    if value is None:
        return None
    if isinstance(value, str):
        return value
    # SQLAlchemy may surface repeated query params as a tuple; take the last.
    return value[-1] if value else None


def mask_database_url(url: str | URL) -> str:
    """Render a database URL safe for logs, errors, HTML, tests, and audit output.

    The password is always removed. For non-loopback hosts the username and the
    host are masked as well, so no RDS endpoint or account name can leak through
    operator output. Loopback URLs keep their (non-secret) host so local
    messages stay useful. The database name, port, and ``sslmode`` remain
    visible — the operator needs them to confirm what they are acting on.
    """

    try:
        parsed = make_url(str(url))
    except Exception:  # noqa: BLE001 - masking must never raise
        return "[unparseable-database-url]"

    host = parsed.host or ""
    local = is_loopback_host(host)
    shown_host = host if local else _MASKED_HOST
    user = parsed.username or ""
    shown_user = user if (local and user) else (_MASKED_USER if user else "")

    auth = f"{shown_user}@" if shown_user else ""
    port = f":{parsed.port}" if parsed.port is not None else ""
    database = f"/{parsed.database}" if parsed.database else ""
    sslmode = url_sslmode(parsed)
    query = f"?sslmode={sslmode}" if sslmode else ""
    return f"{parsed.drivername}://{auth}{shown_host}{port}{database}{query}"


def describe_database_error(exc: BaseException) -> str:
    """A log-safe one-line description of a database error (no DSN, no URL)."""

    return type(exc).__name__


def validate_database_settings(*, target: str, url: str | URL, url_explicitly_set: bool) -> None:
    """Enforce the mode rules for the configured target. Fail closed on violation.

    ``local``   — the host must be loopback.
    ``rds-dev`` — the URL must be explicitly supplied (never the committed
                  default), the host must be non-loopback, and a strong
                  ``sslmode`` must be present on the URL.
    """

    host = url_host(url)
    if target == "local":
        if not is_loopback_host(host):
            raise DatabaseConfigurationError(
                "DATABASE_TARGET=local requires a loopback database host "
                f"(127.0.0.1 / localhost / ::1); the configured URL is {mask_database_url(url)}. "
                "Connecting to the development RDS instance requires setting "
                "DATABASE_TARGET=rds-dev deliberately in your local .env."
            )
        return

    if target == "rds-dev":
        if not url_explicitly_set:
            raise DatabaseConfigurationError(
                "DATABASE_TARGET=rds-dev requires DATABASE_URL to be supplied "
                "explicitly through the local environment or .env; the committed "
                "local default is refused. No RDS value is ever committed to source."
            )
        if is_loopback_host(host):
            raise DatabaseConfigurationError(
                "DATABASE_TARGET=rds-dev is set but the database host is loopback "
                f"({mask_database_url(url)}). Use DATABASE_TARGET=local for a local "
                "database, or point DATABASE_URL at the development RDS endpoint."
            )
        require_strong_sslmode(url)
        return

    raise DatabaseConfigurationError(
        f"unknown DATABASE_TARGET {target!r}; expected 'local' or 'rds-dev'"
    )


def require_strong_sslmode(url: str | URL) -> None:
    """Require an explicit strong ``sslmode`` on a non-loopback URL (fail closed)."""

    mode = url_sslmode(url)
    if mode not in STRONG_SSL_MODES:
        raise DatabaseConfigurationError(
            "connections to a non-loopback database require mandatory TLS: "
            "DATABASE_URL must include sslmode=require, sslmode=verify-ca, or "
            f"sslmode=verify-full (found: {mode!r}). Weaker modes such as "
            "'prefer' can silently fall back to cleartext and are refused."
        )


def enforce_engine_url(url: str | URL, *, target: str) -> None:
    """Gate every engine the application creates (defence in depth).

    A loopback URL is permitted only under the ``local`` target; a non-loopback
    URL is permitted only under the ``rds-dev`` target and only with a strong
    ``sslmode``. Any mismatch refuses to build the engine at all.
    """

    host = url_host(url)
    if is_loopback_host(host):
        if target != "local":
            raise DatabaseConfigurationError(
                f"refusing to build a loopback engine while DATABASE_TARGET={target!r}: "
                "the configured mode and the database URL disagree."
            )
        return
    if target != "rds-dev":
        raise DatabaseConfigurationError(
            "refusing to build an engine for a non-loopback database host while "
            f"DATABASE_TARGET={target!r}: set DATABASE_TARGET=rds-dev deliberately "
            "to use the development RDS instance."
        )
    require_strong_sslmode(url)


def assert_connection_encrypted(dbapi_connection: object) -> None:
    """Refuse a live connection to a remote host unless TLS is positively in use.

    Called from an SQLAlchemy ``connect`` listener for non-loopback engines.
    If encryption cannot be *confirmed* (missing attribute, driver change, or
    ``ssl_in_use`` false) the connection is rejected — absence of proof is
    treated as absence of encryption.
    """

    pgconn = getattr(dbapi_connection, "pgconn", None)
    ssl_in_use = getattr(pgconn, "ssl_in_use", None)
    if ssl_in_use is not True:
        raise DatabaseConfigurationError(
            "refusing an unencrypted connection to a non-loopback database host: "
            "TLS could not be confirmed on the established connection "
            f"(ssl_in_use={ssl_in_use!r}). This is a fail-closed check; verify the "
            "server enforces SSL and that DATABASE_URL carries a strong sslmode."
        )


def ensure_local_only_operation(settings: Settings, *, operation: str) -> None:
    """Refuse a local-only operation unless the target and host are both local.

    Used by bootstrap/reset/fixture/test helpers. Independent of (and in
    addition to) the configuration-time validation, so a hand-built or stale
    settings object still cannot direct a destructive helper at a remote host.
    """

    if settings.database_target != "local":
        raise RemoteDatabaseRefused(
            f"{operation} is local-only and refuses to run while "
            f"DATABASE_TARGET={settings.database_target!r}."
        )
    host = url_host(settings.database_url)
    if not is_loopback_host(host):
        raise RemoteDatabaseRefused(
            f"{operation} is local-only and refuses to run against a "
            f"non-loopback database host ({mask_database_url(settings.database_url)})."
        )


def ensure_migration_allowed(
    url: str | URL, *, command: str | None, environ: dict[str, str] | None = None
) -> None:
    """Gate Alembic runs. Loopback is unrestricted; remote hosts need the token.

    * ``downgrade`` against a non-loopback host is refused unconditionally —
      there is no supported destructive migration path against RDS.
    * Any other command against a non-loopback host requires the one-shot
      process token that only ``scripts/rds_migrate.py`` sets, plus a strong
      ``sslmode`` on the URL.
    """

    host = url_host(url)
    if is_loopback_host(host):
        return

    env = environ if environ is not None else dict(os.environ)
    if command == "downgrade":
        raise RemoteDatabaseRefused(
            "alembic downgrade against a non-loopback database host is refused "
            "unconditionally. Downgrade paths are proven against a local database; "
            "recovering the development RDS instance uses backup/restore "
            "(see docs/DEVELOPMENT.md)."
        )
    if env.get(RDS_MIGRATION_ENV_VAR) != RDS_MIGRATION_ENV_VALUE:
        raise RemoteDatabaseRefused(
            "alembic refuses to run against a non-loopback database host outside "
            "the deliberate operator command. Use `python scripts/rds_migrate.py "
            "status|upgrade|prove` instead of calling alembic directly."
        )
    require_strong_sslmode(url)

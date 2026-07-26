"""Root conftest — establishes a safe test environment BEFORE ``app`` is imported.

This file exists because of an ordering problem that cannot be solved from
``tests/conftest.py``:

* ``app/db/session.py`` builds its ``engine`` at **import time** from
  ``get_settings().database_url``;
* ``app/core/config.Settings`` reads the project ``.env`` by default;
* several test modules build a ``TestClient`` at module scope.

So by the time any fixture runs, the engine and the app may already be bound to
the operator's development database with their real feature flags and provider
keys. A fixture cannot undo that — the connection pool already points at
``vmr_dev``.

pytest imports the rootdir ``conftest.py`` before it imports any test module, so
this module body is the last safe moment to set the environment. Everything here
runs at import, deliberately, before the first ``from app...`` anywhere.

What it guarantees:

* the suite talks to a dedicated database whose name identifies it as a test
  database, and **refuses to run otherwise** — the guard raises before any
  connection is opened, let alone written to;
* the project ``.env`` is not read at all (``VMR_TEST_MODE`` switches
  ``Settings`` to ``env_file=None``), so feature flags fall back to their
  default-off values and no real provider key can reach a test;
* the operator's development database is never opened, on any code path.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit, urlunsplit

# --------------------------------------------------------------------------
# 1. Test mode, set before anything can read settings.
# --------------------------------------------------------------------------
# `app.core.config` reads this to decide whether to load `.env`. Setting it here
# means the very first `Settings()` construction in the process already knows it
# is under test.
os.environ["VMR_TEST_MODE"] = "1"

# A database whose name does not match this is refused. The pattern is
# deliberately strict: `vmr_dev`, `vmr`, `postgres` and any RDS database name
# all fail it.
TEST_DB_NAME_PATTERN = re.compile(r"^vmr_test(_[a-z0-9_]+)?$")

DEFAULT_TEST_DB = "vmr_test"
DEFAULT_TEST_URL = f"postgresql+psycopg://postgres@127.0.0.1:5433/{DEFAULT_TEST_DB}"

# Hosts that must never appear in a test database URL. `postgres` is the Docker
# Compose service name for the development database; if it resolves at all, it
# is the operator's real data.
FORBIDDEN_HOSTS = {"postgres", "db", "database"}


class UnsafeTestDatabase(RuntimeError):
    """Raised when the configured test database is not provably a test database."""


def _database_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


def _assert_safe(url: str) -> None:
    """Refuse anything that is not clearly a disposable test database.

    This runs before the schema is created and before any test opens a session,
    so a misconfiguration fails loudly and immediately rather than quietly
    writing into — or truncating — the operator's development data.
    """

    parts = urlsplit(url)
    name = _database_name(url)

    if not TEST_DB_NAME_PATTERN.match(name):
        raise UnsafeTestDatabase(
            f"Refusing to run the test suite against database {name!r}.\n"
            f"The test database name must match {TEST_DB_NAME_PATTERN.pattern!r} "
            f"(for example 'vmr_test' or 'vmr_test_local').\n"
            f"Set VMR_TEST_DATABASE_URL to a dedicated test database. "
            f"The suite truncates every table between tests, so pointing it at "
            f"a development database would destroy data."
        )

    host = (parts.hostname or "").lower()
    if host in FORBIDDEN_HOSTS:
        raise UnsafeTestDatabase(
            f"Refusing to run the test suite against host {host!r}, which is a "
            f"development database service name, not a local test instance."
        )


def _maintenance_url(url: str) -> str:
    """The same server, but the always-present ``postgres`` database.

    Used only to issue ``CREATE DATABASE``; a database cannot be created from a
    connection to itself.
    """

    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/postgres", "", ""))


def _ensure_database(url: str) -> None:
    """Create the test database if it does not already exist.

    Kept dependency-light and driver-level on purpose: this runs before the
    application's engine module is imported, so it must not rely on anything in
    ``app``.
    """

    import sqlalchemy

    name = _database_name(url)
    engine = sqlalchemy.create_engine(_maintenance_url(url), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            exists = connection.execute(
                sqlalchemy.text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
            ).scalar()
            if not exists:
                # Identifier cannot be parameterised; the name has already been
                # validated against TEST_DB_NAME_PATTERN above.
                connection.execute(sqlalchemy.text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()


# --------------------------------------------------------------------------
# 2. Resolve and validate the test database URL.
# --------------------------------------------------------------------------
# Precedence: an explicit VMR_TEST_DATABASE_URL wins, so an operator can point
# the suite at their own local Postgres (different port, different credentials)
# without editing code. Otherwise the documented local default is used.
#
# DATABASE_URL from the operator's shell or `.env` is deliberately IGNORED for
# this decision — inheriting it is exactly the bug this file exists to prevent.
TEST_DATABASE_URL = os.environ.get("VMR_TEST_DATABASE_URL") or DEFAULT_TEST_URL

_assert_safe(TEST_DATABASE_URL)
_ensure_database(TEST_DATABASE_URL)

# Overwrite rather than setdefault: an inherited DATABASE_URL must not win.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

# --------------------------------------------------------------------------
# 3. Scrub anything the operator's shell may have exported.
# --------------------------------------------------------------------------
# `.env` is already excluded by VMR_TEST_MODE, but real environment variables
# outrank it and would still reach Settings. Feature flags must default off so a
# test that does not opt in genuinely sees the feature disabled, and
# `monkeypatch.delenv` must restore *off* rather than fall through to `.env`.
for _name in list(os.environ):
    if _name.startswith("FEATURES__"):
        del os.environ[_name]

# Provider credentials must never be reachable from an automated test: a live
# call would spend real MillionVerifier credits or hit the logo.dev quota.
for _name in ("MILLIONVERIFIER_API_KEY", "LOGO_DEV_API_KEY"):
    os.environ.pop(_name, None)

# Keep the suite unambiguously local.
os.environ["APP_ENV"] = "local"

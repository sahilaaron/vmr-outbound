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

* the suite talks to a database whose **name** identifies it as a test database,
  and refuses to run otherwise — the guard raises before any connection is
  opened, let alone written to;
* the project ``.env`` is not read at all (``VMR_TEST_MODE`` switches
  ``Settings`` to ``env_file=None``), so feature flags fall back to their
  default-off values and no real provider key can reach a test;
* the operator's development database is never opened, on any code path.

**Server coordinates versus database name.** The suite borrows the *server* from
``DATABASE_URL`` — scheme, credentials, host, port — but never its database
name, which is always replaced with an approved ``vmr_test*`` name and then
validated. That distinction is what lets the same code run against an operator's
local Postgres and against a CI service container without either being able to
hand the suite a database it may not touch.
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

# --------------------------------------------------------------------------
# 2. Scrub inherited configuration — before the guard, so it can assert on it.
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
PROVIDER_KEY_VARS = ("MILLIONVERIFIER_API_KEY", "LOGO_DEV_API_KEY")
for _name in PROVIDER_KEY_VARS:
    os.environ.pop(_name, None)

# Keep the suite unambiguously local.
os.environ["APP_ENV"] = "local"


# --------------------------------------------------------------------------
# 3. Safety policy
# --------------------------------------------------------------------------
# A database whose name does not match this is refused. The pattern is
# deliberately strict: `vmr_dev`, `vmr`, `postgres` and any RDS database name
# all fail it.
TEST_DB_NAME_PATTERN = re.compile(r"^vmr_test(_[a-z0-9_]+)?$")

#: The database name the suite forces, whatever server it borrows.
TEST_DB_NAME = "vmr_test"

DEFAULT_TEST_URL = f"postgresql+psycopg://postgres@127.0.0.1:5433/{TEST_DB_NAME}"

# Hosts that are a *service name* rather than a machine: on a developer box
# these resolve to the real development database, so they are refused. They are
# permitted only inside a verified GitHub Actions run, where `postgres` is the
# repository's own throwaway service container.
SERVICE_HOSTS = frozenset({"postgres", "db", "database"})


class UnsafeTestDatabase(RuntimeError):
    """Raised when the configured test database is not provably a test database."""


def is_trusted_ci() -> bool:
    """Whether this process is a verified GitHub Actions run on this repository.

    Every condition must hold. Requiring both ``CI`` and ``GITHUB_ACTIONS``
    means a developer who exports one of them by habit does not accidentally
    unlock the service-host allowance, and requiring the scrub to have taken
    effect means the allowance cannot coexist with a live provider key or an
    inherited feature flag.

    This is a narrowing of the guard, never a replacement for it: the database
    name must still match ``TEST_DB_NAME_PATTERN`` regardless of what this
    returns.
    """

    if os.environ.get("CI") != "true":
        return False
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return False
    if any(os.environ.get(name) for name in PROVIDER_KEY_VARS):
        return False
    if any(name.startswith("FEATURES__") for name in os.environ):
        return False
    return True


def _database_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


def _assert_safe(url: str, *, trusted_ci: bool | None = None) -> None:
    """Refuse anything that is not provably a disposable test database.

    Runs before the schema is created and before any test opens a session, so a
    misconfiguration fails loudly and immediately rather than quietly writing
    into — or truncating — the operator's development data.
    """

    if trusted_ci is None:
        trusted_ci = is_trusted_ci()

    parts = urlsplit(url)
    name = _database_name(url)

    # The name check is unconditional. Nothing — not CI, not an explicit
    # override — permits a database that is not clearly a test database.
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
    if host in SERVICE_HOSTS and not trusted_ci:
        raise UnsafeTestDatabase(
            f"Refusing to run the test suite against host {host!r}.\n"
            f"That is a container service name, which on a developer machine "
            f"resolves to the real development database. It is permitted only "
            f"inside a verified GitHub Actions run (CI=true, GITHUB_ACTIONS=true, "
            f"no provider credentials, no inherited feature flags)."
        )


def _with_test_database(url: str) -> str:
    """Borrow a server's coordinates, but never its database name.

    Keeps scheme, credentials, host and port; replaces the path with
    :data:`TEST_DB_NAME`. This is what lets CI work without the suite ever being
    handed a database it may not touch: the operator's ``vmr_dev`` contributes
    only the address and login, never the target.
    """

    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{TEST_DB_NAME}", "", ""))


def _maintenance_urls(url: str) -> list[str]:
    """Candidate URLs for issuing ``CREATE DATABASE``.

    A database cannot be created from a connection to itself, so this connects
    elsewhere on the same server. ``postgres`` is the conventional maintenance
    database and exists in the official image; ``template1`` is the fallback for
    a server that lacks it.
    """

    parts = urlsplit(url)
    return [
        urlunsplit((parts.scheme, parts.netloc, f"/{name}", "", ""))
        for name in ("postgres", "template1")
    ]


def _ensure_database(url: str) -> None:
    """Create the test database if it does not already exist.

    Kept dependency-light and driver-level on purpose: this runs before the
    application's engine module is imported, so it must not rely on anything in
    ``app``.
    """

    import sqlalchemy

    name = _database_name(url)
    last_error: Exception | None = None

    for maintenance in _maintenance_urls(url):
        engine = sqlalchemy.create_engine(maintenance, isolation_level="AUTOCOMMIT")
        try:
            with engine.connect() as connection:
                exists = connection.execute(
                    sqlalchemy.text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
                ).scalar()
                if not exists:
                    # The identifier cannot be parameterised; the name has
                    # already been validated against TEST_DB_NAME_PATTERN.
                    connection.execute(sqlalchemy.text(f'CREATE DATABASE "{name}"'))
            return
        except Exception as exc:  # try the next maintenance database
            last_error = exc
        finally:
            engine.dispose()

    raise RuntimeError(
        f"Could not create or reach the test database {name!r} on this server. "
        f"Check that the credentials in DATABASE_URL (or VMR_TEST_DATABASE_URL) "
        f"can connect. Last error: {last_error}"
    )


def resolve_test_database_url(
    *,
    explicit: str | None = None,
    inherited: str | None = None,
) -> str:
    """Decide which database the suite will use.

    Precedence:

    1. ``VMR_TEST_DATABASE_URL`` — an explicit, deliberate choice by whoever is
       running the suite. Still guarded.
    2. ``DATABASE_URL``, with its **database name replaced**. This is what makes
       the suite work unchanged on a CI runner whose Postgres service has its
       own credentials, and on an operator's machine whose Postgres uses a
       different port or login — while keeping it impossible for either to
       nominate the database.
    3. The documented local default.

    Note what step 2 does *not* do: it never uses the inherited database name.
    Inheriting that name was the original defect this whole file exists to
    prevent.
    """

    if explicit:
        return explicit
    if inherited:
        return _with_test_database(inherited)
    return DEFAULT_TEST_URL


# --------------------------------------------------------------------------
# 4. Resolve, validate, create.
# --------------------------------------------------------------------------
TEST_DATABASE_URL = resolve_test_database_url(
    explicit=os.environ.get("VMR_TEST_DATABASE_URL"),
    inherited=os.environ.get("DATABASE_URL"),
)

_assert_safe(TEST_DATABASE_URL)
_ensure_database(TEST_DATABASE_URL)

# Overwrite rather than setdefault: an inherited DATABASE_URL must not win.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

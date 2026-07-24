#!/usr/bin/env python3
"""Local bring-up: prepare the development database and apply the schema.

One idempotent, cross-platform step between "Postgres is running" and "the app
is ready to run". It:

  1. ensures a local ``.env`` exists (copied from ``.env.example`` if missing),
  2. waits for the configured Postgres server to accept connections,
  3. creates the target database as UTF-8 if it does not exist yet,
  4. applies all Alembic migrations (``upgrade head``) and verifies no drift,
  5. prints the exact command to run the operator workbench.

This is LOCAL DEVELOPMENT tooling only. It never stores or requires any
production/RDS credential; it reads ``DATABASE_URL`` from the environment/.env
exactly like the app. Re-running it is safe.

Usage:
    python scripts/dev_up.py            # prepare DB + migrate + verify
    python scripts/dev_up.py --no-wait  # fail fast if the server is not up yet
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time
from pathlib import Path

from app.db.safety import mask_database_url
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

REPO_ROOT = Path(__file__).resolve().parents[1]
# Enabled together, these unlock the local operator workbench + both import
# paths (spreadsheet and Sales Navigator capture). All default off in code.
RUN_FLAGS = "FEATURES__WORKBENCH=true FEATURES__CSV_IMPORT=true FEATURES__SALESNAV_INTAKE=true"


def _say(msg: str) -> None:
    print(f"[dev_up] {msg}", flush=True)


def ensure_env_file() -> None:
    env = REPO_ROOT / ".env"
    example = REPO_ROOT / ".env.example"
    if env.exists():
        _say(".env present.")
        return
    if example.exists():
        shutil.copyfile(example, env)
        _say("created .env from .env.example (edit it only if your Postgres differs).")
    else:
        _say("no .env or .env.example found; relying on process environment.")


def database_url() -> str:
    # Import lazily so a missing .env or install surfaces a clear earlier error.
    from app.core.config import get_settings
    from app.db.safety import ensure_local_only_operation

    get_settings.cache_clear()
    settings = get_settings()
    # FND-009: this script creates databases and drives migrations without
    # confirmation — strictly a LOCAL bootstrap. It refuses any non-local
    # target/host outright; the development RDS instance is operated only
    # through `python scripts/rds_migrate.py`.
    ensure_local_only_operation(settings, operation="scripts/dev_up.py")
    return settings.database_url


def wait_for_server(url: str, *, wait: bool) -> None:
    """Block until the Postgres *server* (the admin 'postgres' db) is reachable."""

    admin_url = make_url(url).set(database="postgres")
    deadline = time.monotonic() + (60.0 if wait else 0.0)
    attempt = 0
    while True:
        attempt += 1
        engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            _say(f"Postgres server reachable at {admin_url.host}:{admin_url.port}.")
            return
        except OperationalError as exc:
            if time.monotonic() >= deadline:
                _say("ERROR: could not reach the Postgres server.")
                _say(f"  tried: {mask_database_url(admin_url)}")
                _say(f"  detail: {type(exc).__name__}")
                _say("  Start it first — e.g. `docker compose up -d db` — then re-run this.")
                raise SystemExit(2) from exc
            if attempt == 1:
                _say("waiting for Postgres to accept connections…")
            time.sleep(2.0)
        finally:
            engine.dispose()


def build_create_database_statement(name: str) -> str:
    """Compose a ``CREATE DATABASE`` statement with a safely-quoted identifier.

    The database name comes from configuration (``DATABASE_URL``), so it is
    composed with psycopg's SQL identifier quoting rather than interpolated into
    the statement — a name containing quotes, spaces, or SQL metacharacters can
    never break out of the identifier.
    """

    return (
        sql.SQL("CREATE DATABASE {} ENCODING 'UTF8' TEMPLATE template0")
        .format(sql.Identifier(name))
        .as_string(None)
    )


def ensure_database(url: str) -> None:
    """Create the target database as UTF-8 if it does not already exist.

    Existence is decided by querying ``pg_database`` on the admin connection —
    not by trying to connect to the target and treating any failure as "missing".
    A missing database is therefore distinguished from authentication, network,
    permission, SSL, or other connection failures: those surface as a clear error
    against the admin connection and never trigger ``CREATE DATABASE``.
    """

    target = make_url(url)
    name = target.database
    if not name:
        _say("ERROR: DATABASE_URL has no database name.")
        raise SystemExit(2)

    admin_url = make_url(url).set(database="postgres")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
            ).scalar()
            if exists:
                _say(f'database "{name}" already exists.')
                return
            conn.exec_driver_sql(build_create_database_statement(name))
            _say(f'created UTF-8 database "{name}".')
    except OperationalError as exc:
        # A failure here is a connection/auth/permission/SSL problem reaching the
        # server — NOT evidence that the target database is missing. Fail clearly
        # and never attempt to create. Credentials/URL are masked.
        _say("ERROR: could not prepare the database (could not reach the server).")
        _say(f"  server: {mask_database_url(admin_url)}")
        _say(f"  detail: {type(exc).__name__}")
        raise SystemExit(2) from exc
    finally:
        admin.dispose()


def run_alembic(args: list[str], url: str) -> None:
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(REPO_ROOT)}
    _say(f"alembic {' '.join(args)}")
    result = subprocess.run(["alembic", *args], cwd=REPO_ROOT, env=env)
    if result.returncode != 0:
        _say(f"ERROR: `alembic {' '.join(args)}` failed (exit {result.returncode}).")
        raise SystemExit(result.returncode)


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare the local dev database.")
    parser.add_argument(
        "--no-wait", action="store_true", help="fail immediately if the server is not up"
    )
    opts = parser.parse_args()

    ensure_env_file()
    url = database_url()
    _say(f"using {mask_database_url(url)}")

    wait_for_server(url, wait=not opts.no_wait)
    ensure_database(url)
    run_alembic(["upgrade", "head"], url)
    run_alembic(["check"], url)

    _say("database ready and schema up to date.")
    print()
    print("Next — enable the local operator features in your .env (they default off):")
    print("    FEATURES__WORKBENCH=true")
    print("    FEATURES__CSV_IMPORT=true")
    print("    FEATURES__SALESNAV_INTAKE=true")
    print("Then run the app (works the same on every OS once .env is set):")
    print("    uvicorn app.main:app --reload --port 8000")
    print("  (bash one-liner alternative, no .env edit:")
    print(f"     {RUN_FLAGS} uvicorn app.main:app --reload --port 8000 )")
    print()
    print("Then, in another terminal, smoke-check it:")
    print("    python scripts/smoke.py")
    print()
    print("Open http://127.0.0.1:8000/  → Imports to bring in an Excel/CSV sheet,")
    print("or open a staged Sales Navigator batch from the capture extension.")


if __name__ == "__main__":
    main()

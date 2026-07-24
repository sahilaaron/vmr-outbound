#!/usr/bin/env python3
"""Deliberate operator command for the development RDS database (FND-009).

This is the ONLY supported way to run Alembic against a non-loopback host.
It exists so that touching the shared development RDS instance is always a
deliberate, explicit, confirmed act — never a side effect of local tooling.

Subcommands:

    python scripts/rds_migrate.py status
        Read-only. Reports (masked) connection info, server version, encoding,
        timezone, TLS state, the current Alembic revision versus the repository
        head, and whether the models drift from the migrations.

    python scripts/rds_migrate.py upgrade
        Applies `alembic upgrade head`, then verifies with `alembic check`.
        Requires typing the exact database name to confirm. Never downgrades.

    python scripts/rds_migrate.py prove
        Read-only readiness checks plus a controlled write/read/cleanup proof
        inside a single transaction using a temporary table (`ON COMMIT DROP`),
        so nothing persists and no application table is mutated.

Safety properties:

* Refuses to run unless ``DATABASE_TARGET=rds-dev`` (local databases are
  operated by ``scripts/dev_up.py``); the settings layer already enforces an
  explicit ``DATABASE_URL`` with a strong ``sslmode`` for this target.
* TLS is verified on the live connection (fail closed) by the shared engine
  factory, and reported here from ``pg_stat_ssl``.
* There is no downgrade subcommand, and ``migrations/env.py`` refuses
  ``alembic downgrade`` against any non-loopback host unconditionally.
* Every printed connection detail goes through ``mask_database_url``; errors
  print exception class names only. No endpoint, username, password, or full
  URL ever appears in output.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.core.config import Settings, get_settings  # noqa: E402
from app.db.safety import (  # noqa: E402
    RDS_MIGRATION_ENV_VALUE,
    RDS_MIGRATION_ENV_VAR,
    DatabaseConfigurationError,
    RemoteDatabaseRefused,
    describe_database_error,
    mask_database_url,
)
from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import Connection, Engine  # noqa: E402

OK = "ok"
WARN = "warn"
FAIL = "fail"

#: CI runs against PostgreSQL 16; the same major version is expected on the
#: development RDS instance. Older than 15 is refused (features and behaviour
#: are unproven there); a different major than 16 is a warning.
MIN_SERVER_VERSION_NUM = 150_000
EXPECTED_MAJOR_VERSION = 16


def _say(msg: str) -> None:
    print(f"[rds_migrate] {msg}", flush=True)


def collect_server_report(conn: Connection) -> dict[str, Any]:
    """Read-only server facts used by the capability checks (no secrets)."""

    def scalar(query: str) -> Any:
        return conn.execute(text(query)).scalar()

    return {
        "server_version": scalar("SHOW server_version"),
        "server_version_num": int(scalar("SELECT current_setting('server_version_num')")),
        "server_encoding": scalar("SHOW server_encoding"),
        "client_encoding": scalar("SHOW client_encoding"),
        "timezone": scalar("SHOW TimeZone"),
        "database": scalar("SELECT current_database()"),
        "ssl_in_use": bool(scalar("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")),
        "in_recovery": bool(scalar("SELECT pg_is_in_recovery()")),
    }


def capability_findings(report: dict[str, Any], *, require_tls: bool) -> list[tuple[str, str]]:
    """Evaluate the server report against what the application requires."""

    findings: list[tuple[str, str]] = []

    encoding = str(report["server_encoding"]).upper()
    if encoding == "UTF8":
        findings.append((OK, "server_encoding is UTF8"))
    else:
        findings.append((FAIL, f"server_encoding is {encoding}; the application requires UTF8"))

    client_encoding = str(report["client_encoding"]).upper()
    if client_encoding == "UTF8":
        findings.append((OK, "client_encoding is UTF8"))
    else:
        findings.append((FAIL, f"client_encoding is {client_encoding}; the driver requires UTF8"))

    version_num = int(report["server_version_num"])
    major = version_num // 10_000
    if version_num < MIN_SERVER_VERSION_NUM:
        findings.append(
            (FAIL, f"PostgreSQL {report['server_version']} is older than the minimum (15)")
        )
    elif major != EXPECTED_MAJOR_VERSION:
        findings.append(
            (
                WARN,
                f"PostgreSQL major version is {major}; CI and local development "
                f"use {EXPECTED_MAJOR_VERSION} — behaviour differences are possible",
            )
        )
    else:
        findings.append((OK, f"PostgreSQL {report['server_version']}"))

    if require_tls:
        if report["ssl_in_use"]:
            findings.append((OK, "connection is TLS-encrypted (pg_stat_ssl)"))
        else:
            findings.append((FAIL, "connection is NOT TLS-encrypted"))

    timezone = str(report["timezone"])
    if timezone.upper() in {"UTC", "ETC/UTC"}:
        findings.append((OK, f"server TimeZone is {timezone}"))
    else:
        findings.append(
            (
                WARN,
                f"server TimeZone is {timezone} (not UTC); timestamps are stored "
                "timezone-aware, so this is informational",
            )
        )

    if report["in_recovery"]:
        findings.append((FAIL, "server is in recovery (read-only replica?); writes will fail"))
    else:
        findings.append((OK, "server accepts writes (not in recovery)"))

    return findings


def alembic_revision_state(conn: Connection) -> tuple[str | None, str]:
    """(current revision on the database, head revision in the repository)."""

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    head = script.get_current_head() or "(no head)"
    exists = conn.execute(
        text("SELECT 1 FROM information_schema.tables WHERE table_name = 'alembic_version'")
    ).scalar()
    if not exists:
        return None, head
    current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    return (str(current) if current else None), head


def run_write_proof(engine: Engine) -> list[tuple[str, str]]:
    """Controlled write/read/cleanup proof that persists nothing.

    A temporary table is created ``ON COMMIT DROP`` inside one transaction, a
    row is written and read back, and the transaction commits — at which point
    the table is gone. No application table is touched.
    """

    findings: list[tuple[str, str]] = []
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TEMPORARY TABLE _fnd009_proof "
                "(id integer PRIMARY KEY, note text NOT NULL) ON COMMIT DROP"
            )
        )
        conn.execute(
            text("INSERT INTO _fnd009_proof (id, note) VALUES (:i, :n)"),
            {"i": 1, "n": "fnd-009 readiness proof"},
        )
        row = conn.execute(text("SELECT id, note FROM _fnd009_proof WHERE id = :i"), {"i": 1}).one()
        if row.id == 1 and row.note == "fnd-009 readiness proof":
            findings.append((OK, "write/read proof succeeded (temporary table)"))
        else:  # pragma: no cover - defensive
            findings.append((FAIL, "write/read proof returned unexpected data"))
    with engine.connect() as conn:
        leftover = conn.execute(
            text("SELECT 1 FROM information_schema.tables WHERE table_name = '_fnd009_proof'")
        ).scalar()
        if leftover:
            findings.append((FAIL, "cleanup proof failed: proof table still exists"))
        else:
            findings.append((OK, "cleanup proof succeeded (nothing persisted)"))
    return findings


def _print_findings(findings: list[tuple[str, str]]) -> bool:
    """Print findings; True when none of them is a failure."""

    for level, message in findings:
        _say(f"{level.upper():4s} {message}")
    return all(level != FAIL for level, _ in findings)


def _run_alembic(args: list[str]) -> int:
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        RDS_MIGRATION_ENV_VAR: RDS_MIGRATION_ENV_VALUE,
    }
    _say(f"alembic {' '.join(args)}")
    result = subprocess.run(  # noqa: S603 - fixed command, no shell
        [sys.executable, "-m", "alembic", *args], cwd=REPO_ROOT, env=env
    )
    return result.returncode


def _require_rds_settings() -> Settings:
    settings = get_settings()
    if settings.database_target != "rds-dev":
        _say("REFUSED: this command operates the development RDS instance only.")
        _say(f"  DATABASE_TARGET is {settings.database_target!r}; set it to 'rds-dev'")
        _say("  in your local .env (with the RDS DATABASE_URL) to use this command.")
        _say("  Local databases are prepared with `python scripts/dev_up.py`.")
        raise SystemExit(2)
    return settings


def _connect_engine(settings: Settings) -> Engine:
    # The shared factory enforces target/URL agreement, strong sslmode, pool
    # limits, timeouts, and live TLS verification (fail closed).
    from app.db.session import create_db_engine

    return create_db_engine(settings.database_url, settings=settings)


def cmd_status(settings: Settings) -> int:
    engine = _connect_engine(settings)
    try:
        with engine.connect() as conn:
            report = collect_server_report(conn)
            current, head = alembic_revision_state(conn)
    finally:
        engine.dispose()

    _say(f"target   : {mask_database_url(settings.database_url)}")
    _say(f"database : {report['database']}")
    ok = _print_findings(capability_findings(report, require_tls=True))

    if current == head:
        _say(f"OK   schema is at head ({head})")
    elif current is None:
        _say(f"FAIL schema is empty (no alembic_version); head is {head} — run `upgrade`")
        ok = False
    else:
        _say(f"FAIL schema is at {current}, head is {head} — run `upgrade`")
        ok = False

    if current is not None:
        drift = _run_alembic(["check"])
        if drift == 0:
            _say("OK   no model/migration drift (alembic check)")
        else:
            _say("FAIL alembic check reported drift between models and migrations")
            ok = False

    _say("status: PASS" if ok else "status: FAIL")
    return 0 if ok else 1


def cmd_upgrade(settings: Settings) -> int:
    from sqlalchemy.engine import make_url

    database = make_url(settings.database_url).database or ""
    _say(f"target   : {mask_database_url(settings.database_url)}")
    _say("this applies ALL pending Alembic migrations to the development RDS database.")
    answer = input(f"Type the database name ({database!r}) to confirm: ").strip()
    if answer != database:
        _say("REFUSED: confirmation did not match the database name; nothing was run.")
        return 2

    rc = _run_alembic(["upgrade", "head"])
    if rc != 0:
        _say(f"FAIL alembic upgrade head exited {rc}")
        return 1
    rc = _run_alembic(["check"])
    if rc != 0:
        _say(f"FAIL alembic check exited {rc}")
        return 1

    engine = _connect_engine(settings)
    try:
        with engine.connect() as conn:
            current, head = alembic_revision_state(conn)
    finally:
        engine.dispose()
    if current == head:
        _say(f"OK   schema is at head ({head})")
        _say("upgrade: PASS")
        return 0
    _say(f"FAIL schema is at {current}, head is {head}")
    return 1


def cmd_prove(settings: Settings) -> int:
    engine = _connect_engine(settings)
    try:
        with engine.connect() as conn:
            report = collect_server_report(conn)
            current, head = alembic_revision_state(conn)
        findings = capability_findings(report, require_tls=True)
        if current == head:
            findings.append((OK, f"schema is at head ({head})"))
        else:
            findings.append((FAIL, f"schema is at {current}, head is {head}"))
        findings.extend(run_write_proof(engine))
    finally:
        engine.dispose()

    _say(f"target   : {mask_database_url(settings.database_url)}")
    ok = _print_findings(findings)
    _say("prove: PASS" if ok else "prove: FAIL")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deliberate operator command for the development RDS database."
    )
    parser.add_argument("command", choices=["status", "upgrade", "prove"])
    opts = parser.parse_args()

    try:
        settings = _require_rds_settings()
        handler = {"status": cmd_status, "upgrade": cmd_upgrade, "prove": cmd_prove}
        return handler[opts.command](settings)
    except SystemExit:
        raise
    except (DatabaseConfigurationError, RemoteDatabaseRefused) as exc:
        # Safety-layer messages are constructed masked (no endpoint, user,
        # password, or full URL), so the explanation itself is safe to show.
        _say(f"REFUSED: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - never leak connection details
        _say(f"ERROR: {describe_database_error(exc)}")
        _say("  (connection details are never printed; check your local .env values)")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

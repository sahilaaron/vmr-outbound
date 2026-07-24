"""Operator command for the development RDS instance (scripts/rds_migrate.py).

The check/proof functions are exercised against the local test database (they
are read-only or self-cleaning); the CLI-level rules — rds-dev-target-only,
typed confirmation, masked output — are exercised without any network access.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest
from app.core.config import get_settings
from app.db.session import engine
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[1]

_SYNTH_REMOTE_SSL = (
    "postgresql+psycopg://appuser:s3cret@db-dev.example.amazonaws.com:5432/vmr_dev?sslmode=require"
)


@pytest.fixture(scope="module")
def rds_migrate() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "rds_migrate_script", REPO_ROOT / "scripts" / "rds_migrate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_server_report_collects_required_facts(rds_migrate: ModuleType) -> None:
    with engine.connect() as conn:
        report = rds_migrate.collect_server_report(conn)
    assert report["server_encoding"] == "UTF8"
    assert report["server_version_num"] >= 150_000
    assert isinstance(report["ssl_in_use"], bool)
    assert report["in_recovery"] is False
    assert report["database"]


def test_capability_findings_pass_on_healthy_utf8_server(rds_migrate: ModuleType) -> None:
    with engine.connect() as conn:
        report = rds_migrate.collect_server_report(conn)
    findings = rds_migrate.capability_findings(report, require_tls=False)
    assert all(level != rds_migrate.FAIL for level, _ in findings)


def test_capability_findings_fail_on_bad_encoding_and_missing_tls(
    rds_migrate: ModuleType,
) -> None:
    report = {
        "server_version": "16.4",
        "server_version_num": 160_004,
        "server_encoding": "SQL_ASCII",
        "client_encoding": "UTF8",
        "timezone": "UTC",
        "database": "x",
        "ssl_in_use": False,
        "in_recovery": False,
    }
    findings = rds_migrate.capability_findings(report, require_tls=True)
    failures = [msg for level, msg in findings if level == rds_migrate.FAIL]
    assert any("UTF8" in msg for msg in failures)
    assert any("TLS" in msg for msg in failures)


def test_capability_findings_fail_below_minimum_version(rds_migrate: ModuleType) -> None:
    report = {
        "server_version": "14.11",
        "server_version_num": 140_011,
        "server_encoding": "UTF8",
        "client_encoding": "UTF8",
        "timezone": "UTC",
        "database": "x",
        "ssl_in_use": True,
        "in_recovery": False,
    }
    findings = rds_migrate.capability_findings(report, require_tls=True)
    assert any(level == rds_migrate.FAIL and "older" in msg for level, msg in findings)


def test_capability_findings_warn_on_non_utc_timezone(rds_migrate: ModuleType) -> None:
    report = {
        "server_version": "16.4",
        "server_version_num": 160_004,
        "server_encoding": "UTF8",
        "client_encoding": "UTF8",
        "timezone": "Asia/Kolkata",
        "database": "x",
        "ssl_in_use": True,
        "in_recovery": False,
    }
    findings = rds_migrate.capability_findings(report, require_tls=True)
    assert any(level == rds_migrate.WARN and "TimeZone" in msg for level, msg in findings)
    assert all(level != rds_migrate.FAIL for level, _ in findings)


def test_alembic_revision_state_reports_current_and_head(rds_migrate: ModuleType) -> None:
    with engine.connect() as conn:
        current, head = rds_migrate.alembic_revision_state(conn)
    assert head and head != "(no head)"
    # The shared test database may or may not have alembic_version applied;
    # either way the call reports without error and head comes from the repo.
    assert current is None or isinstance(current, str)


def test_write_proof_persists_nothing(rds_migrate: ModuleType) -> None:
    findings = rds_migrate.run_write_proof(engine)
    assert all(level == rds_migrate.OK for level, _ in findings)
    with engine.connect() as conn:
        leftover = conn.execute(
            text("SELECT 1 FROM information_schema.tables WHERE table_name = '_fnd009_proof'")
        ).scalar()
    assert leftover is None


def test_cli_refuses_local_target(rds_migrate: ModuleType) -> None:
    # The suite runs with DATABASE_TARGET=local; the operator command must
    # refuse and point at dev_up instead (exit code 2).
    assert get_settings().database_target == "local"
    with pytest.raises(SystemExit) as excinfo:
        rds_migrate._require_rds_settings()
    assert excinfo.value.code == 2


def test_upgrade_requires_typed_confirmation(
    rds_migrate: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.core.config import Settings

    fake = Settings.model_construct(
        database_target="rds-dev",
        database_url=_SYNTH_REMOTE_SSL,
        db_pool_size=5,
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "wrong-name")
    rc = rds_migrate.cmd_upgrade(fake)
    out = capsys.readouterr().out
    assert rc == 2
    assert "REFUSED" in out
    # Nothing ran, and no connection detail leaked into the output.
    assert "s3cret" not in out
    assert "appuser" not in out
    assert "example.amazonaws.com" not in out
    assert "[masked-host]" in out


def test_run_alembic_sets_the_one_shot_token(rds_migrate: ModuleType) -> None:
    # The token that migrations/env.py requires for non-loopback hosts is set
    # only inside the operator command's subprocess environment.
    from app.db.safety import RDS_MIGRATION_ENV_VAR

    assert os.environ.get(RDS_MIGRATION_ENV_VAR) is None
    source = (REPO_ROOT / "scripts" / "rds_migrate.py").read_text(encoding="utf-8")
    assert "RDS_MIGRATION_ENV_VAR: RDS_MIGRATION_ENV_VALUE" in source


def test_script_has_no_downgrade_path(rds_migrate: ModuleType) -> None:
    source = (REPO_ROOT / "scripts" / "rds_migrate.py").read_text(encoding="utf-8")
    assert '"downgrade"' not in source.replace("refuses ``alembic downgrade``", "")
    assert not hasattr(rds_migrate, "cmd_downgrade")

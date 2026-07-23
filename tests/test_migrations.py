"""Migration round-trip test (DAT-001).

Runs ``upgrade head`` -> ``check`` -> ``downgrade base`` -> ``upgrade head`` against
a throwaway database via the ``alembic`` CLI, mirroring the CI step. Using a
dedicated database keeps the destructive downgrade away from the shared test
schema.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.core.config import get_settings
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def temp_database_url() -> Iterator[str]:
    """Create and drop an isolated database for a migration round trip."""

    base = make_url(get_settings().database_url)
    name = f"vmr_mig_{uuid.uuid4().hex[:12]}"
    admin_url = base.set(database="postgres")

    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin_engine.dispose()

    try:
        yield base.set(database=name).render_as_string(hide_password=False)
    finally:
        admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin_engine.dispose()


def _alembic(args: list[str], database_url: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": database_url, "PYTHONPATH": str(REPO_ROOT)}
    return subprocess.run(
        ["alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_migration_upgrade_check_downgrade_reupgrade(temp_database_url: str) -> None:
    for args in (
        ["upgrade", "head"],
        ["check"],
        ["downgrade", "base"],
        ["upgrade", "head"],
    ):
        result = _alembic(args, temp_database_url)
        assert result.returncode == 0, (
            f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
        )

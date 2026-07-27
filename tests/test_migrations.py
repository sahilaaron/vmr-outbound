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


def test_downgrade_refuses_to_erase_an_automatic_domain_decision(
    temp_database_url: str,
) -> None:
    """DAT-017's downgrade must refuse rather than misattribute a decision.

    An ``AUTOMATIC_POLICY`` confirmation has no representation in the earlier
    schema. Rewriting it to ``MANUAL`` would claim an operator typed a domain
    they never saw, and dropping the row would delete an applied domain — so the
    migration stops and says what to do instead. The empty-database round trip
    above cannot exercise that, because there is nothing to protect.
    """

    assert _alembic(["upgrade", "head"], temp_database_url).returncode == 0

    engine = create_engine(temp_database_url)
    campaign_id, batch_id = uuid.uuid4(), uuid.uuid4()
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO campaigns (id, name, status) VALUES (:id, :n, 'DRAFT')"),
                {"id": campaign_id, "n": "migration guard"},
            )
            conn.execute(
                text(
                    "INSERT INTO import_batches "
                    "(id, campaign_id, content_hash, status, total_rows, accepted_rows, "
                    " rejected_rows, duplicate_rows, suppressed_rows, contacts_created) "
                    "VALUES (:id, :c, :h, 'PENDING', 0, 0, 0, 0, 0, 0)"
                ),
                {"id": batch_id, "c": campaign_id, "h": uuid.uuid4().hex},
            )
            conn.execute(
                text(
                    "INSERT INTO salesnav_company_enrichments "
                    "(id, batch_id, company_key, company_name, row_count, lookup_status, "
                    " lookup_attempts, confirmation_status, confirmed_domain, confirmation_source) "
                    "VALUES (:id, :b, 'acme', 'Acme', 1, 'OK', 1, 'CONFIRMED', "
                    " 'acme.example', 'AUTOMATIC_POLICY')"
                ),
                {"id": uuid.uuid4(), "b": batch_id},
            )

        result = _alembic(["downgrade", "-1"], temp_database_url)
        assert result.returncode != 0, "the downgrade must refuse while the row exists"
        assert "confirmed automatically" in (result.stdout + result.stderr)

        # Once the decision is an operator's, the downgrade proceeds.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE salesnav_company_enrichments "
                    "SET confirmation_source = 'MANUAL' "
                    "WHERE confirmation_source::text = 'AUTOMATIC_POLICY'"
                )
            )
        cleared = _alembic(["downgrade", "-1"], temp_database_url)
        assert cleared.returncode == 0, f"{cleared.stdout}\n{cleared.stderr}"
    finally:
        engine.dispose()

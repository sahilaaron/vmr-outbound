"""Shared test fixtures.

Tests run against a real PostgreSQL instance (the same engine the app uses),
because the audit model relies on Postgres-specific types (UUID, JSONB). Each
test that needs the database runs inside a transaction that is rolled back, so
tests never leave data behind.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import engine
from sqlalchemy.orm import Session

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def _create_schema() -> Iterator[None]:
    """Ensure the schema exists for the test session.

    Uses ``create_all`` (checkfirst) so the suite is self-sufficient even when
    Alembic has not been run. We deliberately do NOT drop tables afterwards:
    dropping the Alembic-managed table without resetting ``alembic_version``
    would corrupt migration state for later ``alembic`` commands on the same
    database. Per-test isolation is handled by transaction rollback below.
    """

    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture()
def db_session() -> Iterator[Session]:
    """A Session bound to a transaction that is rolled back after each test."""

    connection = engine.connect()
    transaction = connection.begin()
    session = Session(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture()
def enable_csv_import(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Enable the ``csv_import`` feature switch for the duration of a test.

    The switch defaults off (FND-007); the import service refuses to run while it
    is disabled. This flips it on via the environment and clears the settings
    cache, restoring both afterwards.
    """

    monkeypatch.setenv("FEATURES__CSV_IMPORT", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def load_fixture_csv(name: str) -> bytes:
    """Return the bytes of a CSV fixture under ``tests/fixtures``."""

    return (FIXTURES_DIR / name).read_bytes()


@pytest.fixture()
def representative_csv() -> bytes:
    """The representative import fixture (valid, invalid, duplicate, suppressed)."""

    return load_fixture_csv("contacts_representative.csv")

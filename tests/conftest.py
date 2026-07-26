"""Shared test fixtures.

Tests run against a real PostgreSQL instance (the same engine the app uses),
because the audit model relies on Postgres-specific types (UUID, JSONB).

**The safety work happens in the rootdir ``conftest.py``, not here.** By the time
this module is imported, ``app`` has already been imported and its engine is
already bound. The root conftest is what guarantees that binding points at a
dedicated test database rather than the operator's development one, and that
``.env`` was never read. Read that file before changing anything about isolation
here.

Two layers of isolation:

* every test that takes ``db_session`` runs inside a transaction that is rolled
  back, so nothing it writes survives;
* every test is followed by a truncation sweep, because tests that drive the app
  through ``TestClient`` commit through the application's own session and are
  not covered by that rollback.

The second layer is what makes counting assertions honest. Without it, a test
asserting "no contacts exist" passes only by luck of ordering.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import engine
from sqlalchemy import Connection, text
from sqlalchemy.orm import Session

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Alembic's bookkeeping table is not application data and must survive: emptying
# it would corrupt migration state for later alembic commands on the same
# database.
_PRESERVED_TABLES = frozenset({"alembic_version"})


@pytest.fixture(scope="session", autouse=True)
def _create_schema() -> Iterator[None]:
    """Ensure the schema exists for the test session.

    Uses ``create_all`` (checkfirst) so the suite is self-sufficient even when
    Alembic has not been run against the test database. Tables are deliberately
    not dropped afterwards — see ``_PRESERVED_TABLES`` — because per-test
    isolation is handled by rollback and truncation instead.
    """

    Base.metadata.create_all(bind=engine)
    yield


def _truncate_all(connection: Connection) -> None:
    """Empty every application table in one statement.

    A single ``TRUNCATE ... CASCADE`` avoids foreign-key ordering problems and
    is dramatically faster than per-table deletes. ``RESTART IDENTITY`` resets
    the suppression-event sequence so identity values stay deterministic across
    tests.
    """

    tables = [
        f'"{table.name}"'
        for table in Base.metadata.sorted_tables
        if table.name not in _PRESERVED_TABLES
    ]
    if not tables:
        return
    connection.execute(text(f"TRUNCATE TABLE {', '.join(tables)} RESTART IDENTITY CASCADE"))


@pytest.fixture(autouse=True)
def _isolate_database() -> Iterator[None]:
    """Leave the database empty after every test.

    Autouse and unconditional. Tests that go through ``TestClient`` commit via
    the application's own session, so the ``db_session`` rollback below does not
    cover them; without this sweep their rows leak into every later test's
    counts. Truncating *after* rather than before also means a failing test
    leaves a clean database for the next one.
    """

    try:
        yield
    finally:
        with engine.begin() as connection:
            _truncate_all(connection)


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    """Rebuild settings around every test.

    ``get_settings`` is ``lru_cache``d, so a test that changes an environment
    variable would otherwise be observed by unrelated later tests — or miss its
    own change because an earlier test had already populated the cache. Clearing
    on both sides keeps each test's view of configuration its own.
    """

    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


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

    The switch defaults off (FND-007); the import service refuses to run while
    it is disabled. Under test ``.env`` is not read at all, so "off" is genuinely
    off rather than whatever the operator's file says — which is what makes
    ``monkeypatch.delenv`` in a test restore the disabled default instead of
    falling back to an enabled one.
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

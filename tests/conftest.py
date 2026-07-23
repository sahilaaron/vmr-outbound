"""Shared test fixtures.

Tests run against a real PostgreSQL instance (the same engine the app uses),
because the audit model relies on Postgres-specific types (UUID, JSONB). Each
test that needs the database runs inside a transaction that is rolled back, so
tests never leave data behind.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.db.base import Base
from app.db.session import engine
from sqlalchemy.orm import Session


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

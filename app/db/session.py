"""Database engine and session management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from math import ceil

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.runtime import validate_runtime_settings


def create_db_engine(
    database_url: str | None = None,
    *,
    connect_timeout_seconds: float | None = None,
    pool_pre_ping: bool = True,
    pool_size: int | None = None,
    max_overflow: int | None = None,
    pool_timeout_seconds: float = 30.0,
) -> Engine:
    """Create a SQLAlchemy engine for the given (or configured) database URL.

    The pool bounds come from settings rather than from literals here. They used
    to be 5 and 10 with no way to change them, which made them a silent ceiling
    on Agent worker concurrency: the worker holds one pooled connection for the
    entire final transaction of a job, so fifteen was the most concurrent jobs a
    worker process could ever run regardless of ``--workers``. An explicit
    argument still wins, for a caller sizing a pool for one specific purpose.
    """

    settings = get_settings()
    # This module exposes a compatibility global below, so validation must run
    # here before SQLAlchemy constructs even a lazy Engine resource.
    validate_runtime_settings(settings)
    url = database_url or settings.database_url
    timeout = connect_timeout_seconds or settings.database_connect_timeout_seconds
    return create_engine(
        url,
        pool_pre_ping=pool_pre_ping,
        pool_size=settings.database_pool_size if pool_size is None else pool_size,
        max_overflow=(settings.database_max_overflow if max_overflow is None else max_overflow),
        pool_timeout=pool_timeout_seconds,
        future=True,
        connect_args={"connect_timeout": max(1, ceil(timeout))},
    )


engine: Engine = create_db_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional scope around a series of operations."""

    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

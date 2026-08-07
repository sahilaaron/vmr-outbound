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
    pool_size: int = 5,
    max_overflow: int = 10,
    pool_timeout_seconds: float = 30.0,
) -> Engine:
    """Create a SQLAlchemy engine for the given (or configured) database URL."""

    settings = get_settings()
    # This module exposes a compatibility global below, so validation must run
    # here before SQLAlchemy constructs even a lazy Engine resource.
    validate_runtime_settings(settings)
    url = database_url or settings.database_url
    timeout = connect_timeout_seconds or settings.database_connect_timeout_seconds
    return create_engine(
        url,
        pool_pre_ping=pool_pre_ping,
        pool_size=pool_size,
        max_overflow=max_overflow,
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

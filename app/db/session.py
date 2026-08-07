"""Database engine and session management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from math import ceil

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def create_db_engine(
    database_url: str | None = None, *, connect_timeout_seconds: float | None = None
) -> Engine:
    """Create a SQLAlchemy engine for the given (or configured) database URL."""

    settings = get_settings()
    url = database_url or settings.database_url
    timeout = connect_timeout_seconds or settings.database_connect_timeout_seconds
    return create_engine(
        url,
        pool_pre_ping=True,
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

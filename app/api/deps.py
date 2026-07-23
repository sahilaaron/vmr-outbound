"""API dependencies."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from app.db.session import SessionLocal


def get_db() -> Iterator[Session]:
    """Yield a database session and commit on success.

    Import processing commits internally; for the campaign route this commit
    persists the flushed campaign. Tests override this dependency to run inside a
    rolled-back transaction.
    """

    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

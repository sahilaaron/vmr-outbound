"""Database engine and session management.

Every engine the application uses is built through :func:`create_db_engine`,
which enforces the FND-009 connection-safety rules (loopback vs development-RDS
target agreement, mandatory TLS for non-loopback hosts — checked both on the
URL and on the live connection) and applies conservative pool and timeout
behaviour suitable for a small shared development RDS instance:

* bounded pool with limited overflow;
* ``pool_pre_ping`` so dead connections are detected before use;
* recycling below common idle-timeout windows;
* a short libpq connect timeout;
* server-side ``statement_timeout``, ``lock_timeout``, and
  ``idle_in_transaction_session_timeout`` so a runaway query or abandoned
  transaction cannot hold the shared instance hostage.

Connection details are never logged from here; use
:func:`app.db.safety.mask_database_url` wherever a URL must be shown.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.db.safety import (
    assert_connection_encrypted,
    enforce_engine_url,
    is_loopback_host,
    url_host,
)


def _server_settings_options(settings: Settings) -> str:
    """libpq ``options`` string applying the server-side timeout backstops."""

    return (
        f"-c statement_timeout={settings.db_statement_timeout_ms} "
        f"-c lock_timeout={settings.db_lock_timeout_ms} "
        f"-c idle_in_transaction_session_timeout={settings.db_idle_in_transaction_timeout_ms}"
    )


def create_db_engine(
    database_url: str | None = None, *, settings: Settings | None = None
) -> Engine:
    """Create a SQLAlchemy engine for the given (or configured) database URL.

    The URL is validated against the configured ``DATABASE_TARGET`` before any
    engine exists (fail closed), and non-loopback engines additionally verify
    TLS on every established connection.
    """

    settings = settings or get_settings()
    url = database_url or settings.database_url
    enforce_engine_url(url, target=settings.database_target)

    connect_args: dict[str, Any] = {
        "connect_timeout": settings.db_connect_timeout_seconds,
        "options": _server_settings_options(settings),
    }
    engine = create_engine(
        url,
        future=True,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_recycle=settings.db_pool_recycle_seconds,
        connect_args=connect_args,
    )

    if not is_loopback_host(url_host(url)):
        # Mandatory-encryption backstop: even if the URL's sslmode were somehow
        # satisfied by a misconfigured server, an unencrypted live connection
        # is refused at connect time (absence of proof == absence of TLS).
        @event.listens_for(engine, "connect")
        def _verify_tls(dbapi_connection: object, _record: object) -> None:
            assert_connection_encrypted(dbapi_connection)

    return engine


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

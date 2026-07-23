"""FastAPI application shell.

Phase 0 provides only a minimal, safe app shell that starts from the documented
commands and reports health. It contains no business rules and performs no
outreach actions. Later phases add API surfaces behind feature switches.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from sqlalchemy import text

from app import __version__
from app.api.routes import router as api_router
from app.core.config import Settings, get_settings
from app.db.session import engine


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory."""

    settings = settings or get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        summary="Phase 0 foundation shell — no outreach capabilities enabled.",
    )

    @app.get("/health", tags=["system"])
    def health() -> dict[str, Any]:
        """Liveness probe. Does not touch external dependencies."""

        return {
            "status": "ok",
            "app": settings.app_name,
            "version": __version__,
            "env": settings.app_env,
            "dry_run": settings.dry_run,
            "features_enabled": settings.features.enabled(),
        }

    @app.get("/ready", tags=["system"])
    def ready() -> dict[str, Any]:
        """Readiness probe. Verifies the database is reachable."""

        database_ok = True
        detail: str | None = None
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:  # pragma: no cover - exercised via error path
            database_ok = False
            detail = type(exc).__name__

        return {
            "status": "ready" if database_ok else "degraded",
            "database": "ok" if database_ok else "unavailable",
            "detail": detail,
        }

    app.include_router(api_router)
    return app


app = create_app()

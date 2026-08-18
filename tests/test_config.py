"""Configuration tests (FND-004)."""

from __future__ import annotations

import pytest
from app.core.config import Settings


def test_defaults_are_safe() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    # Dry-run must default ON so no environment sends real email by accident.
    assert settings.dry_run is True
    # No pipeline feature is enabled by default.
    assert settings.features.enabled() == []
    assert settings.app_env == "local"
    assert settings.is_production is False


def test_database_url_default_is_local_non_secret() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.database_url.startswith("postgresql+psycopg://")
    # The default must not embed a password.
    assert ":@" not in settings.database_url
    assert "password" not in settings.database_url.lower()


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("FEATURES__CSV_IMPORT", "true")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.app_env == "production"
    assert settings.is_production is True
    assert settings.dry_run is False
    assert settings.features.csv_import is True
    assert settings.features.enabled() == ["csv_import"]


def test_pool_bounds_default_to_the_previous_literals() -> None:
    """Making the pool configurable must not move any deployment off 5 + 10."""

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.database_pool_size == 5
    assert settings.database_max_overflow == 10


def _capture_engine_kwargs(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> dict[str, object]:
    """Run ``create_db_engine`` against a stubbed ``create_engine`` and report argv.

    Asserting on the arguments rather than on a constructed pool keeps the check
    at a public seam and needs no database.
    """

    from app.db import session as db_session

    captured: dict[str, object] = {}

    def _stub(url: str, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(db_session, "get_settings", lambda: settings)
    monkeypatch.setattr(db_session, "validate_runtime_settings", lambda _settings: None)
    monkeypatch.setattr(db_session, "create_engine", _stub)
    db_session.create_db_engine("postgresql+psycopg://user@127.0.0.1:5432/example")
    return captured


def test_configured_pool_bounds_reach_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The setting has to arrive at the pool, not merely parse.

    This bound is the real ceiling on Agent worker concurrency — a worker thread
    holds one pooled connection for the whole of a job's model call — so a value
    that were read into settings and then dropped would look configured while the
    queue stayed capped at fifteen concurrent jobs.
    """

    configured = Settings(  # type: ignore[call-arg]
        _env_file=None, database_pool_size=26, database_max_overflow=6
    )
    captured = _capture_engine_kwargs(monkeypatch, configured)
    assert captured["pool_size"] == 26
    assert captured["max_overflow"] == 6


def test_explicit_pool_arguments_override_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit argument wins, including an explicit zero.

    Zero overflow is a real request — a caller sizing a pool for one purpose —
    and the sentinel is ``None`` precisely so that a falsy-but-deliberate value
    is not silently replaced by the configured default.
    """

    from app.db import session as db_session

    configured = Settings(  # type: ignore[call-arg]
        _env_file=None, database_pool_size=26, database_max_overflow=6
    )
    captured: dict[str, object] = {}

    def _stub(url: str, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(db_session, "get_settings", lambda: configured)
    monkeypatch.setattr(db_session, "validate_runtime_settings", lambda _settings: None)
    monkeypatch.setattr(db_session, "create_engine", _stub)
    db_session.create_db_engine(
        "postgresql+psycopg://user@127.0.0.1:5432/example", pool_size=3, max_overflow=0
    )
    assert captured["pool_size"] == 3
    assert captured["max_overflow"] == 0

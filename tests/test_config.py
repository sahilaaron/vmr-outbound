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

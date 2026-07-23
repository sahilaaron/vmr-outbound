"""Application configuration.

All configuration is read from environment variables (optionally via a local
``.env`` file). No secrets are committed to source control — see ``.env.example``
for the required variable names.

Phase 0 scope: local development only. The RDS/production variable *names* are
documented here and in ``.env.example`` but no production credentials exist in
the repository.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.features import FeatureFlags


class Settings(BaseSettings):
    """Typed application settings loaded from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # --- Application ---------------------------------------------------------
    app_name: str = "VMR Outbound Agent"
    app_env: str = Field(
        default="local",
        description="Deployment environment label: local | ci | staging | production.",
    )
    debug: bool = False

    # --- Database ------------------------------------------------------------
    # Local dev default points at the documented local Postgres instance.
    # In staging/production this is supplied by the environment (RDS). The
    # value is a full SQLAlchemy URL; credentials never live in source.
    database_url: str = Field(
        default="postgresql+psycopg://dev@127.0.0.1:5433/vmr_dev",
        description="SQLAlchemy database URL. Supplied by the environment outside local dev.",
    )

    # --- Safety switches -----------------------------------------------------
    # Dry-run defaults ON so that no environment can schedule real email
    # without an explicit, deliberate opt-out. See GOAL.md / AGENTS.md.
    dry_run: bool = Field(
        default=True,
        description="When true, the workflow completes without scheduling real email.",
    )

    features: FeatureFlags = Field(default_factory=FeatureFlags)

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance for the process lifetime."""

    return Settings()

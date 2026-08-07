"""Bounded, dependency-minimal health checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.engine import Engine


class ReadinessProbe(Protocol):
    def __call__(self) -> None:
        """Return on success; raise on failure."""


@dataclass(frozen=True)
class DatabaseReadinessProbe:
    """One bounded PostgreSQL round trip using the application's engine."""

    engine: Engine
    timeout_seconds: float

    def __call__(self) -> None:
        timeout_ms = max(1, int(self.timeout_seconds * 1000))
        with self.engine.connect() as connection, connection.begin():
            # SET LOCAL is transaction-scoped and never leaks into a pooled
            # connection. set_config accepts a bound value, unlike SET syntax.
            connection.execute(
                text("SELECT set_config('statement_timeout', :timeout, true)"),
                {"timeout": f"{timeout_ms}ms"},
            )
            connection.execute(text("SELECT 1")).scalar_one()

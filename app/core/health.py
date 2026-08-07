"""Bounded, dependency-minimal health checks."""

from __future__ import annotations

from dataclasses import dataclass
from queue import Empty, Queue
from threading import BoundedSemaphore, Thread
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.engine import Engine


class ReadinessProbe(Protocol):
    def __call__(self) -> None:
        """Return on success; raise on failure."""


@dataclass
class DatabaseReadinessProbe:
    """One wall-clock-bounded PostgreSQL round trip with bounded concurrency."""

    engine: Engine
    timeout_seconds: float

    def __post_init__(self) -> None:
        # One process-local probe may use the dedicated one-connection pool at
        # a time. Contending callers wait only within the same total budget.
        self._permit = BoundedSemaphore(value=1)

    def __call__(self) -> None:
        results: Queue[BaseException | None] = Queue(maxsize=1)
        if not self._permit.acquire(timeout=self.timeout_seconds):
            raise TimeoutError("readiness probe is already in progress")

        def run() -> None:
            try:
                self._check_database()
            except BaseException as exc:
                results.put(exc)
            else:
                results.put(None)
            finally:
                self._permit.release()

        # Driver and server timeouts are the primary cancellation mechanisms.
        # This daemon provides a final response deadline if a driver or socket
        # violates them, without creating an unbounded number of stuck threads.
        Thread(target=run, name="vmr-readiness", daemon=True).start()
        try:
            outcome = results.get(timeout=self.timeout_seconds)
        except Empty as exc:
            raise TimeoutError("readiness probe exceeded its wall-clock budget") from exc
        if outcome is not None:
            raise outcome

    def _check_database(self) -> None:
        timeout_ms = max(1, int(self.timeout_seconds * 1000))
        connection = self.engine.connect()
        try:
            with connection.begin():
                # SET LOCAL is transaction-scoped and never leaks into a pooled
                # connection. set_config accepts a bound value, unlike SET syntax.
                connection.execute(
                    text("SELECT set_config('statement_timeout', :timeout, true)"),
                    {"timeout": f"{timeout_ms}ms"},
                )
                connection.execute(text("SELECT 1")).scalar_one()
        finally:
            # The dedicated readiness pool never reuses a socket. Every probe
            # therefore gets the driver's bounded connection-establishment path
            # instead of inheriting a potentially stalled pooled connection.
            connection.invalidate()
            connection.close()

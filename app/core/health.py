"""Bounded, dependency-minimal health checks."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from math import ceil
from typing import Protocol

from psycopg import AsyncConnection
from sqlalchemy.engine import make_url


class ReadinessProbe(Protocol):
    def __call__(self) -> None | Awaitable[None]:
        """Return on success; raise on failure."""


async def run_readiness_probe(probe: ReadinessProbe) -> None:
    """Run either a production async probe or a small synchronous test seam."""

    outcome = probe()
    if inspect.isawaitable(outcome):
        await outcome


@dataclass
class DatabaseReadinessProbe:
    """One cancellable PostgreSQL round trip under one absolute deadline."""

    database_url: str
    timeout_seconds: float
    connect_timeout_seconds: float
    connection_factory: Callable[..., Awaitable[AsyncConnection[object]]] = field(
        default=AsyncConnection.connect,
        repr=False,
    )

    def __post_init__(self) -> None:
        # asyncio.Lock creates no worker, thread, socket or database resource.
        # In production one event loop owns each application process. Waiting
        # callers share the same absolute request budget and cancellation always
        # removes them from the lock queue.
        self._permit = asyncio.Lock()
        url = make_url(self.database_url).set(drivername="postgresql")
        self._conninfo = url.render_as_string(hide_password=False)

    async def __call__(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_seconds
        acquired = False
        attempt: _ProbeAttempt | None = None
        operation: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(self._permit.acquire(), timeout=max(0.0, deadline - loop.time()))
            acquired = True
            attempt = _ProbeAttempt()
            operation = asyncio.create_task(self._check_database(attempt, deadline))
            done, _ = await asyncio.wait({operation}, timeout=max(0.0, deadline - loop.time()))
            if done:
                operation.result()
                return

            # Cancelling an active Psycopg query first makes Psycopg attempt a
            # separate server-side cancel for up to five seconds. Close this
            # probe's dedicated socket first so cancellation only cleans up
            # local coroutine state and cannot extend the outward deadline.
            await self._stop_operation(attempt, operation)
            raise TimeoutError("readiness probe exceeded its wall-clock budget")
        except TimeoutError as exc:
            raise TimeoutError("readiness probe exceeded its wall-clock budget") from exc
        finally:
            if operation is not None and not operation.done() and attempt is not None:
                await self._stop_operation(attempt, operation)
            if acquired:
                self._permit.release()

    @staticmethod
    async def _stop_operation(attempt: _ProbeAttempt, operation: asyncio.Task[None]) -> None:
        await attempt.close()
        operation.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await operation

    async def _check_database(self, attempt: _ProbeAttempt, deadline: float) -> None:
        loop = asyncio.get_running_loop()
        remaining = max(0.001, deadline - loop.time())
        # libpq accepts whole seconds. It is a connection-establishment backstop;
        # asyncio.timeout_at() remains the stricter end-to-end deadline.
        connect_timeout = max(1, ceil(min(self.connect_timeout_seconds, remaining)))
        try:
            connection = await self.connection_factory(
                self._conninfo,
                autocommit=True,
                connect_timeout=connect_timeout,
                prepare_threshold=None,
            )
            attempt.connection = connection
            remaining_ms = max(1, int((deadline - loop.time()) * 1000))
            await connection.execute(
                "SELECT set_config('statement_timeout', %s, false)",
                (f"{remaining_ms}ms",),
            )
            cursor = await connection.execute("SELECT 1")
            row = await cursor.fetchone()
            if row != (1,):
                raise RuntimeError("readiness query returned an unexpected result")
        finally:
            if attempt.connection is not None:
                # Psycopg's async close immediately finishes libpq and closes the
                # socket. Cancellation therefore cannot strand a background
                # thread, a pool permit, or a reusable readiness connection.
                await attempt.close()


@dataclass
class _ProbeAttempt:
    """Mutable handle allowing the supervisor to close one in-flight socket."""

    connection: AsyncConnection[object] | None = None

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()

"""The worker's pool ceiling, and the refusal that keeps it from being silent.

The defect this covers produced no error at all. A worker thread holds one pooled
connection for the whole of a job's final transaction — the transaction the Agent
adapter runs inside — so a Research job occupies a connection for its entire
model call, visible in ``pg_stat_activity`` as one ``idle in transaction`` row per
busy thread. With the pool fixed at 5 + 10, ``--workers`` above fifteen bought
nothing: the surplus threads blocked for the pool timeout and then failed to
claim, and the only symptom was that the queue looked slow.

Two halves are tested here, because either one alone leaves the trap standing:

* the pool has to be sizeable **for the worker specifically**, and
* the worker has to **refuse** a thread count its pool cannot serve, rather than
  starting and quietly under-delivering.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest
from app.core.config import Settings
from app.db.session import configured_pool_capacity
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKER_SCRIPT = REPO_ROOT / "scripts" / "run_agent_worker.py"
WORKER_UNIT = REPO_ROOT / "deploy" / "systemd" / "vmr-worker.service"
ENV_EXAMPLE = REPO_ROOT / "deploy" / "vmr.env.example"

#: The concurrency this sizing exists to support: 24 worker threads plus the main
#: thread's capture backfill, which holds a session of its own.
TARGET_WORKERS = 24


@pytest.fixture(scope="module")
def worker() -> ModuleType:
    """Load the worker script as a module, the way running it would."""

    spec = importlib.util.spec_from_file_location("vmr_worker_pool_under_test", WORKER_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _unit_environment(name: str) -> int:
    """One ``Environment=NAME=<int>`` value out of the shipped worker unit."""

    text = WORKER_UNIT.read_text(encoding="utf-8")
    match = re.search(rf"^Environment={name}=(\d+)$", text, flags=re.MULTILINE)
    assert match is not None, f"{name} is not set in {WORKER_UNIT.name}"
    return int(match.group(1))


def test_capacity_is_both_bounds_together() -> None:
    """Overflow counts: it is connections the pool really will hand out."""

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, database_pool_size=26, database_max_overflow=6
    )
    assert configured_pool_capacity(settings) == 32


def test_a_pool_size_below_one_is_refused() -> None:
    """A pool that can serve nobody is a configuration error, not a slow queue."""

    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_pool_size=0)  # type: ignore[call-arg]


def test_a_negative_overflow_is_refused() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_max_overflow=-1)  # type: ignore[call-arg]


def test_worker_refuses_more_threads_than_the_pool_can_serve(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal has to name what to change, because nothing else will.

    A thread that cannot check out a connection blocks and then fails to claim,
    so the over-provisioned case is indistinguishable from a slow provider unless
    startup says so.
    """

    monkeypatch.setattr(worker, "configured_pool_capacity", lambda: 15)
    refusal = worker._pool_refusal(workers=24, resolve_captures=True)

    assert refusal is not None
    # The two numbers an operator has to reconcile, and the settings that move them.
    assert "24" in refusal and "25" in refusal and "15" in refusal
    assert "DATABASE_POOL_SIZE" in refusal
    assert "DATABASE_MAX_OVERFLOW" in refusal


def test_worker_counts_the_backfill_thread_against_the_pool(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The main thread's capture pass holds a session too — exactly one more.

    Sizing for the worker threads alone leaves the pool one short, which surfaces
    as the backfill intermittently failing rather than as anything about workers.
    """

    monkeypatch.setattr(worker, "configured_pool_capacity", lambda: 15)
    assert worker._pool_refusal(workers=15, resolve_captures=True) is not None
    assert worker._pool_refusal(workers=15, resolve_captures=False) is None


def test_worker_accepts_a_thread_count_the_pool_can_serve(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker, "configured_pool_capacity", lambda: 32)
    assert worker._pool_refusal(workers=TARGET_WORKERS, resolve_captures=True) is None


def test_shipped_worker_unit_serves_the_target_concurrency() -> None:
    """The unit's own sizing must satisfy the guard it is meant to pass."""

    capacity = _unit_environment("DATABASE_POOL_SIZE") + _unit_environment("DATABASE_MAX_OVERFLOW")
    assert capacity >= TARGET_WORKERS + 1


def test_the_shared_environment_file_does_not_size_any_pool() -> None:
    """vmr.env is read by vmr-web as well, so sizing there inflates the web pool.

    Verified on systemd 255: ``EnvironmentFile=`` overrides ``Environment=``
    whatever the order in the unit, so a live key here would not merely duplicate
    the worker unit's sizing — it would override it *and* give the web process a
    pool it has no use for. The web process is one uvicorn serving a handful of
    operator sessions and holds about two connections in practice.
    """

    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not stripped.startswith("DATABASE_POOL_SIZE=")
        assert not stripped.startswith("DATABASE_MAX_OVERFLOW=")

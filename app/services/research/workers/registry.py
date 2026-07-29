"""Which research workers exist, and which ones run.

A worker is registered under a stable name. The Research Agent's control
config selects the names to run, in order, so a worker can be plugged in
or unplugged operationally without a code change:

    controls.set_global_control(
        session,
        agent_id=AgentIdentifier.RESEARCH,
        status=AgentControlStatus.ENABLED,
        config={"live": True, "workers": ["website"]},
    )

An unknown name is an error rather than a silent skip -- a research run
that quietly did less than the operator asked for is exactly the kind of
untruthful outcome this pipeline refuses elsewhere.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from app.services.research.contracts import ResearchWorker

WorkerFactory = Callable[[], ResearchWorker]

_REGISTRY: dict[str, WorkerFactory] = {}

# The order workers run in when the control config does not say. Only the
# deterministic website collector is implemented today; see
# ``docs/RESEARCH_WORKERS.md`` for what it takes to add another.
DEFAULT_WORKER_ORDER: tuple[str, ...] = ("website",)


class WorkerNotRegistered(LookupError):
    """The control config named a worker this build does not have."""


def register_worker(name: str, factory: WorkerFactory) -> None:
    """Register ``factory`` under ``name``, replacing any previous entry.

    Replacement is deliberate: it lets a test swap in a fake worker
    without reaching into module internals.
    """

    if not name or not name.strip():
        raise ValueError("worker name must not be blank")
    _REGISTRY[name] = factory


def available_workers() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def build_workers(names: Sequence[str] | None = None) -> tuple[ResearchWorker, ...]:
    """Instantiate the requested workers, preserving the requested order."""

    requested = tuple(names) if names is not None else DEFAULT_WORKER_ORDER
    missing = [name for name in requested if name not in _REGISTRY]
    if missing:
        raise WorkerNotRegistered(
            f"unregistered research worker(s): {', '.join(sorted(missing))}; "
            f"available: {', '.join(available_workers()) or 'none'}"
        )
    return tuple(_REGISTRY[name]() for name in requested)


def _register_builtin_workers() -> None:
    # Imported lazily so registering never drags the crawler's third-party
    # dependencies into a process that is not going to research anything.
    from app.services.research.workers.website import WebsiteWorker

    register_worker("website", WebsiteWorker)


_register_builtin_workers()

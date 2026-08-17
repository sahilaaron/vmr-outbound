"""Run the common durable Agent worker.

One process can execute every registered Phase 2 adapter, or a bounded subset
selected with repeated ``--agent`` arguments. Claim and Running state are durable
checkpoints. A real domain outcome and the job's terminal pipeline projection
commit atomically in the final transaction.

On concurrency
--------------
``--workers`` runs several worker threads in one process. This is not a new
execution model bolted on: the queue was built for it and had only ever had one
consumer. ``claim_next_job`` selects ``FOR UPDATE SKIP LOCKED``, every claim is a
committed checkpoint under a lease, and expired leases are recovered — so a second
consumer skips a row another is holding rather than colliding with it.

The problem it solves is head-of-line blocking. With one consumer, a Research job
that spends ninety seconds in a language-model call holds up an Email job that was
ready the whole time, because the queue is drained strictly in order. Nothing about
that job's *state* was waiting; only the single worker was. With N threads a slow
job occupies one of them and the rest keep draining.

Threads rather than processes because this workload is almost entirely waiting —
on Postgres, on logo.dev, on MillionVerifier, on a ``claude`` subprocess — and
Python releases the GIL for all of it. Each thread owns its own Session per
transaction; nothing is shared but the engine's connection pool.

Two bounds worth knowing. The pool is 5 connections plus 10 overflow, so a worker
count near that ceiling will start queueing on connections instead of on work. And
the language-model Agents each spend one ``claude`` invocation, so N workers can
mean N concurrent CLI calls: if that is too many for your subscription, run a small
pool scoped with ``--agent research --agent insights --agent personalization`` and
a larger one for the rest.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path

# Import the application that ships with *this* script, before anything else can
# resolve `app` somewhere else.
#
# `python scripts/run_agent_worker.py` puts the *script's* directory on sys.path,
# never the repository root, so `import app` falls through to whatever the
# environment provides. An editable install (`pip install -e .`, which is how this
# project is installed) records one absolute path at install time:
#
#     MAPPING = {'app': '/path/to/the/checkout/where/pip/ran/app'}
#
# Share one .venv across a git worktree — the normal way to run a UAT branch beside
# main — and every script in the worktree silently executes the *other* checkout's
# application code. That is not a stale-bytecode problem and no amount of
# reinstalling inside the worktree fixes it for the original checkout; the two
# cannot both own the mapping.
#
# It surfaced as a crash on a counter that one checkout had and the other did not,
# which was luck. The same misresolution had been silently running old services,
# old models and old migrations logic against a live database, and would have gone
# on doing so. Anchoring to `__file__` makes the answer the same regardless of the
# working directory, the launcher, or which checkout last ran pip.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.db.session import session_scope  # noqa: E402 - must follow the path anchor above
from app.models.enums import AgentIdentifier, AgentJobStatus  # noqa: E402
from app.models.verification_job import AgentJob  # noqa: E402
from app.services.agents import jobs, locking  # noqa: E402
from app.services.agents.orchestrator import (  # noqa: E402
    WorkerExecution,
    claim_next_campaign_job,
    execute_started_job,
    prepare_leased_job,
)
from app.services.company_intelligence import runner as ci_runner  # noqa: E402
from app.services.operations import settings as operational  # noqa: E402
from app.services.resolution.pending import resolve_pending  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the shared VMR Agent worker")
    parser.add_argument(
        "--worker-id",
        default=f"{socket.gethostname()}:{os.getpid()}",
        help="Lease owner written to durable jobs (default: host:pid)",
    )
    parser.add_argument(
        "--agent",
        action="append",
        choices=[agent.value for agent in AgentIdentifier],
        dest="agents",
        help="Restrict execution to an Agent; repeat to allow several",
    )
    parser.add_argument(
        "--lease-seconds",
        type=float,
        default=120.0,
        help="Job lease duration before abandoned-work recovery (default: 120)",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=2.0,
        help="Idle polling interval in continuous mode (default: 2)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Attempt one claim and exit, including when no job is due",
    )
    parser.add_argument(
        "--max-jobs",
        type=int,
        help="Exit after this many claimed jobs; useful for controlled deployments",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "Worker threads in this process (default 1). Raise this so a slow "
            "Research job stops holding up an Email job that is already ready."
        ),
    )
    parser.add_argument(
        "--resolve-limit",
        type=int,
        default=25,
        help=(
            "Pending captures to resolve per pass before claiming Agent work "
            "(default 25). Each may cost one provider lookup."
        ),
    )
    parser.add_argument(
        "--skip-capture-resolution",
        action="store_true",
        help="Drain the Agent queue only; do not resolve pending captures",
    )
    parser.add_argument(
        "--skip-company-intelligence",
        action="store_true",
        help=(
            "Do not drain the Company Intelligence queue. By default this worker "
            "processes it whenever the feature switch is on and the Agent queue "
            "is idle — the Research handoff enqueues into it automatically."
        ),
    )
    return parser


def _line(execution: WorkerExecution) -> str:
    """One job's outcome, as one line of JSON.

    ``error_class`` is included because without it a failing run is unreadable: the
    message alone repeats verbatim for every affected contact, and the code that
    distinguishes "this company has no website" from "the Agent crashed" was only in
    the database. A tail of this log should be enough to tell which of those it is.
    """

    job = execution.job
    line: dict[str, object] = {
        "job_id": str(job.id) if job else None,
        "campaign_contact_id": (
            str(execution.campaign_contact_id)
            if execution.campaign_contact_id is not None
            else None
        ),
        "agent": execution.agent_id.value if execution.agent_id else None,
        "status": execution.public_status,
        "message": execution.message,
    }
    if job is not None and job.error_class:
        line["error_class"] = job.error_class
        # Attempt/limit turns "retrying" from an open question into a countdown: an
        # operator can see whether this is about to give up or loop for another hour.
        line["attempt"] = f"{job.attempts}/{job.max_attempts}"
    return json.dumps(line, sort_keys=True)


def _current_execution(job: AgentJob, message: str) -> WorkerExecution:
    return WorkerExecution(
        job=job,
        public_status=jobs.public_status(job),
        agent_id=job.agent_id,
        campaign_contact_id=job.campaign_contact_id,
        message=message,
    )


#: Counters every backfill result has had since the pass was introduced. A result
#: missing one of these is not an older version of the contract — it is not the
#: contract at all, and saying so is more useful than printing zeros.
_BACKFILL_REQUIRED = ("considered", "promoted", "provider_calls", "failed")

#: Counters added later. Absent means "this build does not report it", which is a
#: different statement from "it reported zero" and is written differently.
_BACKFILL_OPTIONAL = ("model_calls",)


_MISSING = object()


def _counter(outcome: object, name: str) -> object:
    """One counter off a result, or :data:`_MISSING`.

    Catches every exception rather than only ``AttributeError``, which is what
    ``hasattr`` would do. A property that raises is not a counter this worker can
    report, and the distinction between "absent" and "absent because reading it
    blew up" is not one the console line needs — but it is emphatically not worth
    ending the process over, which is the mistake being corrected here.
    """

    try:
        return getattr(outcome, name)
    except Exception:  # noqa: BLE001 - a reporting read must never end the worker
        return _MISSING


def _describe_backfill(outcome: object) -> str:
    """One operator-readable line about a backfill pass, whatever it returned.

    Never raises — see :func:`_counter`. This runs at the boundary between the
    worker and a service it calls best-effort, and the worker's actual job is
    draining the Agent queue, so a reporting problem must be *reported*, never
    propagated. That is not hypothetical: a missing counter here took the whole
    worker down with an AttributeError, and the manual re-run the operator had just
    queued went unclaimed.

    Two failure shapes, deliberately told apart rather than both swallowed:

    * A result carrying every required counter but missing an optional one is an
      **older, valid** shape. Its line simply omits what that build cannot report.
      Printing ``model calls 0`` instead would be a quiet lie — indistinguishable
      from a pass that genuinely spent no model calls.
    * A result missing a required counter is **wrong**, and the line says so and
      names the class and the file it came from. That last detail is the point: the
      commonest cause is an import resolving to a different checkout, and a message
      that prints the module path answers in one line what otherwise costs an
      afternoon.
    """

    values = {name: _counter(outcome, name) for name in _BACKFILL_REQUIRED}
    missing = [name for name, value in values.items() if value is _MISSING]
    if missing:
        kind = type(outcome)
        origin = getattr(sys.modules.get(kind.__module__), "__file__", "unknown location")
        return (
            "Capture resolution returned an unusable result: "
            f"{kind.__module__}.{kind.__qualname__} is missing {', '.join(missing)}. "
            f"That class came from {origin}. If that path is not inside this checkout "
            f"({_REPO_ROOT}), the worker is running another checkout's application "
            "code. The Agent queue is unaffected and continues."
        )

    parts = [f"{name.replace('_', ' ')} {values[name]}" for name in _BACKFILL_REQUIRED]
    for name in _BACKFILL_OPTIONAL:
        value = _counter(outcome, name)
        if value is not _MISSING:
            parts.insert(-1, f"{name.replace('_', ' ')} {value}")
    return "Resolved pending captures: " + ", ".join(parts) + "."


def _resolve_pending_captures(*, limit: int) -> str | None:
    """Finish the company-domain resolution an intake request could not.

    Intake resolves what it can inside a hard share of its own request budget and
    leaves the rest untouched, because a hundred-capture submission would otherwise
    spend a hundred provider lookups inside one HTTP request. This is where the
    remainder gets done — before claiming Agent work, because a capture that is not
    yet a Contact has no Agent Job to claim, so resolving first is what puts those
    people into the pipeline at all.

    Best-effort by design: a failure here must never stop the worker from draining
    the Agent queue, which is its actual job.

    The guard covers reading and describing the result, not merely producing it.
    It used to stop at the call, which left the report itself outside the promise
    this docstring makes — and that gap is exactly where the worker died.
    """

    try:
        with session_scope() as session:
            outcome = resolve_pending(session, limit=limit)
        if not getattr(outcome, "did_work", True):
            return None
        return _describe_backfill(outcome)
    except Exception as exc:  # noqa: BLE001 - never block the queue on a backfill
        return f"Capture resolution pass failed: {type(exc).__name__}: {exc}"


def _run_once(
    *,
    worker_id: str,
    lease_seconds: float,
    agent_ids: tuple[AgentIdentifier, ...] | None,
) -> WorkerExecution:
    # Claim is its own committed checkpoint. A crash after this block leaves a
    # durable LEASED row that another worker can recover after expiry.
    with session_scope() as session:
        claimed = claim_next_campaign_job(
            session,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            agent_ids=agent_ids,
        )
        job_id = claimed.id if claimed is not None else None
    if job_id is None:
        return WorkerExecution(
            job=None,
            public_status=None,
            agent_id=None,
            campaign_contact_id=None,
            message="No due Agent job.",
        )

    # Running is a second durable checkpoint. Safety gates are evaluated before
    # this commit, so disabled, paused, or blocked work never reaches an adapter.
    with session_scope() as session:
        context = locking.lock_job_context(session, job_id)
        if context is None:
            raise jobs.AgentJobNotFound(f"job {job_id} disappeared before execution")
        job = context.job
        if job.status is not AgentJobStatus.LEASED or job.lease_owner != worker_id:
            return _current_execution(job, "Job lease changed before execution.")
        rejected = prepare_leased_job(session, job=job, worker_id=worker_id)
    if rejected is not None:
        return rejected

    # Hold the Campaign Contact and related job rows, in that order, while staging
    # the real domain outcome and terminal queue/pipeline projection. The
    # transaction either commits all of them or rolls all of them back, leaving the
    # durable Running lease recoverable.
    with session_scope() as session:
        context = locking.lock_job_context(session, job_id)
        if context is None:
            raise jobs.AgentJobNotFound(f"job {job_id} disappeared before completion")
        job = context.job
        if job.status is not AgentJobStatus.IN_PROGRESS or job.lease_owner != worker_id:
            return _current_execution(job, "Running job ownership changed before execution.")
        return execute_started_job(
            session,
            job=job,
            worker_id=worker_id,
        )


def _run_company_intelligence_once(*, worker_id: str, lease_seconds: float) -> str | None:
    """Claim and execute at most one Company Intelligence job.

    The Research Agent enqueues these automatically when it commits a usable
    dossier, and this shared worker is their normal consumer — no separate
    always-on process and no manual backfill batch is required. Campaign Agent
    work takes precedence: this runs only when the Agent queue had nothing due,
    so classification enrichment never starves the pipeline. Claiming uses the
    queue's own ``FOR UPDATE SKIP LOCKED`` lease path, so a pool of threads
    shares it safely.

    Returns one JSON line describing the outcome, or None when the control is
    off or the queue is idle.

    The control is read through ``operations.settings``, not from
    ``Settings.features``. Every other product control is already resolved that
    way, and this gate was the last place that still trusted the raw environment
    flag. The difference is not academic: on the staging deployment
    ``FEATURES__COMPANY_INTELLIGENCE`` was false while an administrator had
    turned Company Intelligence *on* from Admin → Configuration, so the Admin
    screen reported the control as effective while this worker silently declined
    to drain the queue. Twenty-four jobs enqueued by the Research handoff sat at
    ``PENDING`` with ``attempts=0`` and no lease, and every sequence written in
    that period recorded ``intelligence_lineage.status = "no_current_version"``.
    Reading the effective value makes the Admin switch mean what it says.
    """

    with session_scope() as session:
        if not operational.enabled(session, "company_intelligence"):
            return None
        outcome = ci_runner.run_next(session, worker_id=worker_id, lease_seconds=lease_seconds)
    if outcome is None:
        return None
    line: dict[str, object] = {
        "queue": "company_intelligence",
        "company_id": str(outcome.company_id),
        "status": "succeeded" if outcome.succeeded else "failed",
        "outcome": outcome.code,
        "version": outcome.version_number,
        "message": outcome.message,
    }
    return json.dumps(line, sort_keys=True)


class _Tally:
    """Shared job count and stop flag for a pool of worker threads.

    A lock rather than a bare counter because ``--max-jobs`` is a promise about the
    whole pool, not per thread. The same lock serialises printing: each line is one
    JSON object and two threads interleaving mid-line would produce output nothing
    can parse.
    """

    def __init__(self, *, max_jobs: int | None) -> None:
        self._lock = threading.Lock()
        self._max_jobs = max_jobs
        self.stop = threading.Event()
        self.processed = 0

    def emit(self, line: str) -> None:
        with self._lock:
            print(line, flush=True)

    def record_job(self) -> None:
        with self._lock:
            self.processed += 1
            if self._max_jobs is not None and self.processed >= self._max_jobs:
                self.stop.set()


def _worker_loop(
    *,
    worker_id: str,
    lease_seconds: float,
    agent_ids: tuple[AgentIdentifier, ...] | None,
    poll_seconds: float,
    tally: _Tally,
    include_intelligence: bool,
) -> None:
    """Claim and execute until asked to stop.

    An unexpected error is reported and the thread continues. One thread dying
    silently would quietly reduce the pool's capacity, and the failure would show up
    later as "the queue is slow" rather than as an error.
    """

    while not tally.stop.is_set():
        try:
            execution = _run_once(
                worker_id=worker_id,
                lease_seconds=lease_seconds,
                agent_ids=agent_ids,
            )
        except Exception as exc:  # noqa: BLE001 - a thread must not die quietly
            tally.emit(json.dumps({"worker": worker_id, "error": str(exc)}))
            if tally.stop.wait(max(poll_seconds, 1.0)):
                return
            continue

        tally.emit(_line(execution))
        if execution.job is not None:
            tally.record_job()
            continue

        # The Agent queue is idle; give the Company Intelligence queue one turn.
        # Counted against --max-jobs like any other claim: each of these is one
        # model call, and the cost bound is a promise about the whole process.
        if include_intelligence:
            try:
                note = _run_company_intelligence_once(
                    worker_id=worker_id, lease_seconds=lease_seconds
                )
            except Exception as exc:  # noqa: BLE001 - a thread must not die quietly
                tally.emit(json.dumps({"worker": worker_id, "error": str(exc)}))
                note = None
            if note is not None:
                tally.emit(note)
                tally.record_job()
                continue

        # Nothing was due. Waiting on the stop event rather than sleeping means
        # Ctrl+C is answered immediately instead of after the poll interval.
        if poll_seconds and tally.stop.wait(poll_seconds):
            return


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.lease_seconds <= 0:
        raise SystemExit("--lease-seconds must be positive")
    if args.poll_seconds < 0:
        raise SystemExit("--poll-seconds cannot be negative")
    if args.max_jobs is not None and args.max_jobs < 1:
        raise SystemExit("--max-jobs must be positive")

    if args.workers < 1:
        raise SystemExit("--workers must be positive")

    agent_ids = tuple(AgentIdentifier(value) for value in args.agents) if args.agents else None
    resolve_captures = not args.skip_capture_resolution and agent_ids is None
    # Narrowing to specific Agents is a request to do one thing only, the same
    # convention capture resolution follows.
    include_intelligence = not args.skip_company_intelligence and agent_ids is None

    def _backfill() -> None:
        """Resolve captures that are not yet Contacts, so they have jobs to claim.

        Single-flight by construction: it runs on the main thread only. N threads
        each calling it would each select the same pending rows and spend the same
        provider lookups — the SAVEPOINT isolation would keep that correct but it
        would still be paid for N times.

        Skipped when the operator narrowed the worker to specific Agents, since that
        is a request to do one thing only.
        """

        if not resolve_captures:
            return
        note = _resolve_pending_captures(limit=args.resolve_limit)
        if note:
            print(note, flush=True)

    # --once stays strictly single-threaded. It exists so an operator can watch one
    # job and read its outcome before spending another language-model call, and a
    # pool would race several jobs past them.
    if args.once or args.workers == 1:
        processed = 0
        while True:
            _backfill()
            execution = _run_once(
                worker_id=args.worker_id,
                lease_seconds=args.lease_seconds,
                agent_ids=agent_ids,
            )
            print(_line(execution), flush=True)

            claimed = execution.job is not None
            if not claimed and include_intelligence:
                note = _run_company_intelligence_once(
                    worker_id=args.worker_id, lease_seconds=args.lease_seconds
                )
                if note is not None:
                    print(note, flush=True)
                    claimed = True

            if claimed:
                processed += 1
                if args.max_jobs is not None and processed >= args.max_jobs:
                    return 0
            if args.once:
                return 0
            if not claimed and args.poll_seconds:
                time.sleep(args.poll_seconds)

    tally = _Tally(max_jobs=args.max_jobs)
    threads = [
        threading.Thread(
            # A distinct lease owner per thread. Leases are held by owner, so two
            # threads sharing an id could each believe they own the other's job.
            target=_worker_loop,
            kwargs={
                "worker_id": f"{args.worker_id}#{index}",
                "lease_seconds": args.lease_seconds,
                "agent_ids": agent_ids,
                "poll_seconds": args.poll_seconds,
                "tally": tally,
                "include_intelligence": include_intelligence,
            },
            name=f"agent-worker-{index}",
            daemon=True,
        )
        for index in range(args.workers)
    ]

    print(
        json.dumps({"workers": args.workers, "agents": [a.value for a in agent_ids or ()]}),
        flush=True,
    )
    for thread in threads:
        thread.start()

    try:
        # The main thread owns the capture backfill and nothing else, so it stays
        # responsive to Ctrl+C while the pool works.
        while not tally.stop.is_set():
            _backfill()
            if tally.stop.wait(max(args.poll_seconds, 2.0)):
                break
    except KeyboardInterrupt:
        print(json.dumps({"stopping": True}), flush=True)
    finally:
        tally.stop.set()
        for thread in threads:
            # Bounded: a thread in the middle of a lease-held job finishes its
            # committed checkpoint rather than being abandoned mid-transaction.
            thread.join(timeout=args.lease_seconds)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

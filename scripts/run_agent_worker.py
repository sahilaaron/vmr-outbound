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
import threading
import time
from collections.abc import Sequence

from app.db.session import session_scope
from app.models.enums import AgentIdentifier, AgentJobStatus
from app.models.verification_job import AgentJob
from app.services.agents import jobs
from app.services.agents.orchestrator import (
    WorkerExecution,
    claim_next_campaign_job,
    execute_started_job,
    prepare_leased_job,
)
from app.services.resolution.pending import resolve_pending
from sqlalchemy import select


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
    """

    try:
        with session_scope() as session:
            outcome = resolve_pending(session, limit=limit)
    except Exception as exc:  # noqa: BLE001 - never block the queue on a backfill
        return f"Capture resolution pass failed: {exc}"
    if not outcome.did_work:
        return None
    return (
        f"Resolved pending captures: considered {outcome.considered}, "
        f"promoted {outcome.promoted}, provider calls {outcome.provider_calls}, "
        f"failed {outcome.failed}."
    )


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
        job = session.scalars(select(AgentJob).where(AgentJob.id == job_id).with_for_update()).one()
        if job.status is not AgentJobStatus.LEASED or job.lease_owner != worker_id:
            return _current_execution(job, "Job lease changed before execution.")
        rejected = prepare_leased_job(session, job=job, worker_id=worker_id)
    if rejected is not None:
        return rejected

    # Hold the job row lock while staging the real domain outcome and terminal
    # queue/pipeline projection. The transaction either commits all of them or
    # rolls all of them back, leaving the durable Running lease recoverable.
    with session_scope() as session:
        job = session.scalars(select(AgentJob).where(AgentJob.id == job_id).with_for_update()).one()
        if job.status is not AgentJobStatus.IN_PROGRESS or job.lease_owner != worker_id:
            return _current_execution(job, "Running job ownership changed before execution.")
        return execute_started_job(
            session,
            job=job,
            worker_id=worker_id,
        )


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

            if execution.job is not None:
                processed += 1
                if args.max_jobs is not None and processed >= args.max_jobs:
                    return 0
            if args.once:
                return 0
            if execution.job is None and args.poll_seconds:
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

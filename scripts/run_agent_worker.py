"""Run the common durable Agent worker.

One process can execute every registered Phase 2 adapter, or a bounded subset
selected with repeated ``--agent`` arguments. Claim and Running state are
durable checkpoints. A real domain outcome and the job's terminal pipeline
projection commit atomically in the final transaction.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
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
    return parser


def _line(execution: WorkerExecution) -> str:
    return json.dumps(
        {
            "job_id": str(execution.job.id) if execution.job else None,
            "campaign_contact_id": (
                str(execution.campaign_contact_id)
                if execution.campaign_contact_id is not None
                else None
            ),
            "agent": execution.agent_id.value if execution.agent_id else None,
            "status": execution.public_status,
            "message": execution.message,
        },
        sort_keys=True,
    )


def _current_execution(job: AgentJob, message: str) -> WorkerExecution:
    return WorkerExecution(
        job=job,
        public_status=jobs.public_status(job),
        agent_id=job.agent_id,
        campaign_contact_id=job.campaign_contact_id,
        message=message,
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


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.lease_seconds <= 0:
        raise SystemExit("--lease-seconds must be positive")
    if args.poll_seconds < 0:
        raise SystemExit("--poll-seconds cannot be negative")
    if args.max_jobs is not None and args.max_jobs < 1:
        raise SystemExit("--max-jobs must be positive")

    agent_ids = tuple(AgentIdentifier(value) for value in args.agents) if args.agents else None
    processed = 0
    while True:
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


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the Company Intelligence worker (CI-001).

A separate process from ``run_agent_worker.py``, and separate on purpose: this
one drains a company-scoped queue that has nothing to do with Campaign Contacts,
no stage projection to advance, and no Campaign master switch to respect. Sharing
the Agent worker would have meant teaching it a second execution model.

    python scripts/run_company_intelligence_worker.py --once
    python scripts/run_company_intelligence_worker.py --max-jobs 25

Bounded by default in the ways that matter. ``--max-jobs`` caps how much a single
invocation will spend; ``--once`` claims at most one job and exits. Each job is
one model call, so an unbounded run over a large backfill is a real cost, and the
flag that limits it should be easy to reach.

Refuses to start when ``FEATURES__COMPANY_INTELLIGENCE`` is off, rather than
draining nothing and exiting zero. A worker that looks healthy while doing
nothing is how an operator spends an afternoon watching a queue that was never
going to move.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from pathlib import Path

# Import the application that ships with *this* script, before anything else can
# resolve `app` somewhere else. Same reasoning as run_agent_worker.py: an
# editable install records one absolute path, so a script run from a worktree can
# otherwise execute a different checkout's code entirely.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.config import get_settings  # noqa: E402 - must follow the path anchor
from app.db.session import session_scope  # noqa: E402
from app.services.company_intelligence import runner as ci_runner  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Company Intelligence worker")
    parser.add_argument(
        "--worker-id",
        default=f"ci:{socket.gethostname()}:{os.getpid()}",
        help="Lease owner written to durable jobs (default: ci:host:pid)",
    )
    parser.add_argument(
        "--lease-seconds",
        type=float,
        default=300.0,
        help="Job lease duration before abandoned-work recovery (default: 300)",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=5.0,
        help="Idle polling interval in continuous mode (default: 5)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Attempt one claim and exit, including when no job is due",
    )
    parser.add_argument(
        "--max-jobs",
        type=int,
        help="Exit after this many claimed jobs. Each job is one model call.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    settings = get_settings()
    if not settings.features.company_intelligence:
        print(
            "Company Intelligence is switched off "
            "(set FEATURES__COMPANY_INTELLIGENCE=true). Refusing to start: a worker "
            "that drains nothing while looking healthy is worse than one that stops.",
            file=sys.stderr,
        )
        return 2

    processed = 0
    while True:
        with session_scope() as session:
            outcome = ci_runner.run_next(
                session,
                worker_id=args.worker_id,
                lease_seconds=args.lease_seconds,
            )

        if outcome is None:
            if args.once:
                print("No due Company Intelligence job.")
                return 0
            time.sleep(max(args.poll_seconds, 0.1))
            continue

        processed += 1
        status = "ok " if outcome.succeeded else "FAIL"
        print(
            f"[{status}] company={outcome.company_id} outcome={outcome.code} "
            f"version={outcome.version_number or '-'} — {outcome.message}"
        )

        if args.once or (args.max_jobs is not None and processed >= args.max_jobs):
            print(f"Processed {processed} job(s).")
            return 0


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())

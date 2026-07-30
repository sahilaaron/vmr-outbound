"""The worker's boundary with the best-effort capture backfill.

A UAT worker died on this line::

    f"model calls {outcome.model_calls}, failed {outcome.failed}."

    AttributeError: 'BackfillResult' object has no attribute 'model_calls'

The counter existed on the branch. It did not exist on the object, because the
worker was running from a git worktree sharing one ``.venv`` with the original
checkout, and an editable install records **one absolute path** at install time::

    MAPPING = {'app': '/path/to/the/checkout/where/pip/ran/app'}

``python scripts/run_agent_worker.py`` puts the *script's* directory on
``sys.path`` — never the repository root — so ``import app`` fell through to that
mapping and the worktree's new script executed the other checkout's old services.

Two independent defects, and the tests below cover both, because fixing either
alone leaves a real problem standing:

* **The import was non-deterministic.** The crash was luck. The same
  misresolution had been running old services and old models against a live
  database with no symptom at all.
* **The best-effort guard did not cover the report.** ``try`` wrapped producing
  the result and stopped short of describing it, so a formatting failure escaped a
  function whose entire contract is that it cannot stop the queue — and the
  manual Research re-run the operator had just queued went unclaimed.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    AgentJobStatus,
    PipelineEventType,
    PipelineStageStatus,
)
from app.models.verification_job import AgentJob
from app.services import pipeline
from app.services.agents import controls
from app.services.agents import jobs as agent_jobs
from app.services.agents import rerun as agent_rerun
from app.services.agents.orchestrator import stage_job_key
from app.services.agents.registry import AGENT_SPECS, PIPELINE_ORDER
from app.services.resolution.pending import BackfillResult
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests import workbench_scenario

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKER_SCRIPT = REPO_ROOT / "scripts" / "run_agent_worker.py"


def _load_worker() -> ModuleType:
    """Load the worker script as a module, the way running it would.

    Loading the real file rather than importing a refactored helper is the point:
    the defect lived in the script, in the ``sys.path`` state it establishes and in
    the guard around its own reporting, and none of that is observable from a
    library extracted out of it.
    """

    spec = importlib.util.spec_from_file_location("vmr_agent_worker_under_test", WORKER_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def worker() -> ModuleType:
    return _load_worker()


# ---------------------------------------------------------------------------
# Deterministic import resolution
# ---------------------------------------------------------------------------


def test_the_worker_anchors_imports_to_its_own_checkout(worker: ModuleType) -> None:
    """The fix for the real cause, asserted on the mechanism rather than a symptom.

    Behaviour cannot show this in-process — the suite already has one checkout on
    the path, so a broken anchor passes — which is exactly why it is asserted on
    the state the script establishes.
    """

    assert worker._REPO_ROOT == REPO_ROOT
    assert str(REPO_ROOT) in sys.path


def test_the_application_the_worker_uses_comes_from_that_checkout(worker: ModuleType) -> None:
    """The property the anchor exists to guarantee.

    If ``app`` ever resolves outside this checkout again, the worker is running
    another tree's services and every other assertion in the suite is describing
    code that is not the code under test.
    """

    resolved = Path(sys.modules["app.services.resolution.pending"].__file__ or "").resolve()
    assert resolved.is_relative_to(REPO_ROOT), (
        f"the worker's `app` resolved to {resolved}, outside {REPO_ROOT} — "
        "the editable install is pointing at a different checkout"
    )


# ---------------------------------------------------------------------------
# The reporting contract
# ---------------------------------------------------------------------------


def test_a_current_result_reports_provider_and_model_calls(worker: ModuleType) -> None:
    line = worker._describe_backfill(
        BackfillResult(considered=7, promoted=3, provider_calls=5, model_calls=2, failed=1)
    )

    assert "considered 7" in line
    assert "promoted 3" in line
    assert "provider calls 5" in line
    assert "model calls 2" in line
    assert "failed 1" in line


def test_a_result_without_model_calls_does_not_crash_the_worker(worker: ModuleType) -> None:
    """The exact object the UAT worker received, from the other checkout.

    It omits the counter rather than printing ``model calls 0``: a build that
    cannot report a number and a pass that spent none of something are different
    facts, and one must not be able to masquerade as the other on an operator's
    console.
    """

    @dataclass(frozen=True)
    class LegacyBackfillResult:
        considered: int = 4
        promoted: int = 2
        provider_calls: int = 4
        failed: int = 0

    line = worker._describe_backfill(LegacyBackfillResult())

    assert "considered 4" in line
    assert "provider calls 4" in line
    assert "model calls" not in line
    assert "Resolved pending captures" in line


def test_a_malformed_result_is_reported_rather_than_hidden(worker: ModuleType) -> None:
    """Requirement four: compatibility must not become a blanket.

    A tolerant reader that answered "0" to everything would have turned this
    incident into a worker that ran for weeks reporting plausible zeros while
    executing another checkout's code. The line has to say something is wrong and
    say what would explain it.
    """

    class NotAResult:
        considered = 1  # and nothing else

    line = worker._describe_backfill(NotAResult())

    assert "unusable result" in line
    assert "promoted" in line and "provider_calls" in line and "failed" in line
    assert "NotAResult" in line
    # The detail that turns an afternoon into a glance: where the class came from.
    assert str(Path(__file__).resolve()) in line or "test_worker_backfill_boundary" in line
    assert "The Agent queue is unaffected" in line


def test_the_description_never_raises_whatever_it_is_handed(worker: ModuleType) -> None:
    """It runs inside the guard, but it is also the last thing that should need one."""

    for value in (None, 42, "a string", object(), {"considered": 1}):
        assert isinstance(worker._describe_backfill(value), str)


def test_a_counter_that_raises_when_read_is_treated_as_missing(worker: ModuleType) -> None:
    """`hasattr` only swallows AttributeError; a property raising anything else escapes.

    Reading a number for a console line is never worth ending a worker process
    over, so the read catches everything and the result is reported as unusable.
    """

    class Hostile:
        considered = 1

        @property
        def promoted(self) -> int:
            raise RuntimeError("counter exploded")

        def __getattr__(self, name: str) -> object:
            raise RuntimeError(f"attribute {name} exploded")

    line = worker._describe_backfill(Hostile())

    assert "unusable result" in line
    assert "promoted" in line


def test_a_failure_while_producing_the_result_is_reported_not_raised(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _explode(session: Session, **kwargs: object) -> BackfillResult:
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(worker, "resolve_pending", _explode)
    note = worker._resolve_pending_captures(limit=10)

    assert note is not None
    assert "Capture resolution pass failed" in note
    assert "RuntimeError" in note, "the exception type is what makes this searchable"


def test_reading_and_describing_the_result_are_both_inside_the_guard(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gap the crash came through, closed and pinned.

    The guard used to end at the call, leaving both the ``did_work`` read and the
    report outside the promise the docstring makes. A result whose very first
    attribute access raises exercises that gap directly: pre-fix it escaped as an
    exception, and it must now come back as a line.
    """

    class Hostile:
        def __getattr__(self, name: str) -> object:
            raise RuntimeError(f"attribute {name} exploded")

    monkeypatch.setattr(worker, "resolve_pending", lambda session, **kwargs: Hostile())
    note = worker._resolve_pending_captures(limit=10)

    assert note is not None
    assert isinstance(note, str)


# ---------------------------------------------------------------------------
# The production call path: main() -> _backfill() -> the Agent queue
# ---------------------------------------------------------------------------


def _queue_a_manual_research_rerun(session: Session) -> uuid.UUID:
    """Reproduce the operator's action: Run again on a failed Research Agent.

    Built through the real services rather than by inserting a row, because the
    claim this test makes is about the job the operator actually queued.
    """

    scenario = workbench_scenario.build(session)
    controls.set_global_control(
        session,
        agent_id=AgentIdentifier.RESEARCH,
        status=AgentControlStatus.ENABLED,
        config={"live": True},
    )
    membership = scenario.membership("healthy")
    for upstream in PIPELINE_ORDER:
        if upstream is AgentIdentifier.RESEARCH:
            break
        state = pipeline.agent_state(
            session, campaign_contact_id=membership.id, agent_id=upstream, create=True
        )
        assert state is not None
        if state.status is not PipelineStageStatus.COMPLETED:
            pipeline.transition_stage(
                session,
                membership=membership,
                agent_id=upstream,
                target=PipelineStageStatus.COMPLETED,
                event_type=PipelineEventType.STAGE_COMPLETED,
                actor="test-setup",
                reason_code="test_setup",
            )
    job, _ = agent_jobs.enqueue_job(
        session,
        agent_id=AgentIdentifier.RESEARCH,
        idempotency_key=stage_job_key(membership.id, AgentIdentifier.RESEARCH),
        task_kind="advance_campaign_contact",
        max_attempts=AGENT_SPECS[AgentIdentifier.RESEARCH].max_attempts,
        campaign_id=membership.campaign_id,
        campaign_contact_id=membership.id,
        contact_id=membership.contact_id,
        entity_type="campaign_contact",
        entity_id=membership.id,
    )
    job.attempts = job.max_attempts
    agent_jobs.mark_failed(
        session,
        job,
        error_class="unexpected_error",
        reason="The Agent encountered an unexpected operational error.",
    )
    pipeline.transition_stage(
        session,
        membership=membership,
        agent_id=AgentIdentifier.RESEARCH,
        target=PipelineStageStatus.FAILED,
        event_type=PipelineEventType.FAILED_TERMINAL,
        actor="test-setup",
        job=job,
        reason_code="unexpected_error",
    )
    membership.current_stage = AgentIdentifier.RESEARCH
    membership.next_stage = AgentIdentifier.RESEARCH
    membership.pipeline_status = PipelineStageStatus.FAILED
    session.flush()

    outcome = agent_rerun.rerun_stage(
        session,
        campaign_id=scenario.campaign.id,
        agent_id=AgentIdentifier.RESEARCH,
        reason="fixed the cause",
    )
    assert outcome.accepted, "the fixture must actually queue the re-run it is about to test"

    # Leave the re-run as the only claimable job. `--once` claims exactly one, and
    # the shared scenario ships other queued work — including an Identity job for
    # this same contact, whose stage the fixture has already completed. Without
    # this the worker would claim one of those and the test would assert nothing
    # about the job the operator was actually waiting for.
    keep = {job.id for job in _pending_research_jobs(session, membership.id)}
    assert keep, "the re-run should have produced a pending Research job"
    for other in session.scalars(
        select(AgentJob).where(
            AgentJob.status == AgentJobStatus.PENDING,
            AgentJob.id.not_in(keep),
        )
    ):
        session.delete(other)
    session.commit()
    return membership.id


def _pending_research_jobs(session: Session, membership_id: uuid.UUID) -> list[AgentJob]:
    return list(
        session.scalars(
            select(AgentJob).where(
                AgentJob.campaign_contact_id == membership_id,
                AgentJob.agent_id == AgentIdentifier.RESEARCH,
                AgentJob.status == AgentJobStatus.PENDING,
            )
        )
    )


def test_a_broken_backfill_result_does_not_stop_the_queue_draining(
    worker: ModuleType, committed_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole incident, end to end, through ``main()``.

    Not ``_describe_backfill`` in isolation: the failure was that the *worker*
    exited, so the test drives the worker and asserts on the job the operator was
    waiting for. The backfill is made to return the legacy object the UAT process
    actually received, and the run must still claim and execute the queued
    Research re-run.
    """

    membership_id = _queue_a_manual_research_rerun(committed_session)
    assert _pending_research_jobs(committed_session, membership_id), (
        "the re-run must be queued before the worker starts"
    )

    @dataclass(frozen=True)
    class LegacyBackfillResult:
        considered: int = 1
        promoted: int = 0
        provider_calls: int = 1
        failed: int = 0

        @property
        def did_work(self) -> bool:
            return True

    monkeypatch.setattr(worker, "resolve_pending", lambda session, **kwargs: LegacyBackfillResult())

    exit_code = worker.main(["--once", "--worker-id", "boundary-test"])

    assert exit_code == 0, "the worker must complete its pass, not exit on the backfill"
    committed_session.expire_all()
    assert not _pending_research_jobs(committed_session, membership_id), (
        "the queued Research re-run was still pending — the worker never claimed it"
    )


def test_a_malformed_backfill_result_also_leaves_the_queue_working(
    worker: ModuleType, committed_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compatibility handling is not what keeps the queue alive — the guard is.

    A result that is not merely older but wrong still has to leave the Agent queue
    draining, because the worker's job is the queue and the backfill is a courtesy
    it performs on the way in.
    """

    membership_id = _queue_a_manual_research_rerun(committed_session)

    class NotAResult:
        did_work = True
        considered = 1

    monkeypatch.setattr(worker, "resolve_pending", lambda session, **kwargs: NotAResult())

    assert worker.main(["--once", "--worker-id", "boundary-test"]) == 0
    committed_session.expire_all()
    assert not _pending_research_jobs(committed_session, membership_id)

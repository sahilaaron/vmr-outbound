"""Queue, runner and backfill tests for Company Intelligence (CI-001).

The guarantees under test here are operational rather than semantic:

* the feature switch is genuinely off by default, and off means nothing runs;
* the queue holds one active job per Company, at the database;
* a claim is durable, a lease expires, and abandoned work comes back;
* a retryable failure retries and a hopeless one does not;
* the runner never calls a model when the answer already exists;
* the runner asks for no tools;
* a backfill is bounded, resumable, idempotent, and truthful about skips;
* a dry run enqueues nothing and still reports the same decisions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.core.config import get_settings
from app.models.company import Company
from app.models.company_intelligence import (
    CompanyIntelligenceBackfillItem,
    CompanyIntelligenceJob,
    CompanyIntelligenceVersion,
)
from app.models.enums import (
    IntelligenceBackfillOutcome,
    IntelligenceBackfillStatus,
    IntelligenceDimension,
    IntelligenceJobStatus,
)
from app.services.company_intelligence import backfill as ci_backfill
from app.services.company_intelligence import inputs as ci_inputs
from app.services.company_intelligence import jobs as ci_jobs
from app.services.company_intelligence import read as ci_read
from app.services.company_intelligence import runner as ci_runner
from app.services.thinking.contracts import (
    ThinkingRequest,
    ThinkingResult,
    ThinkingTimeout,
    ThinkingUnavailable,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.test_company_intelligence import (
    make_company,
    make_dossier,
    make_fact,
    seeded,
)


class ScriptedThinker:
    """Answers with a fixed payload, or raises a fixed error."""

    name = "scripted"
    version = "scripted/v1"

    def __init__(
        self, payload: dict[str, Any] | None = None, *, error: Exception | None = None
    ) -> None:
        self._payload = payload or {}
        self._error = error
        self.requests: list[ThinkingRequest] = []

    def think(self, request: ThinkingRequest) -> ThinkingResult:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return ThinkingResult(
            payload=self._payload,
            producer=self.name,
            producer_version=self.version,
            duration_seconds=0.01,
            raw="{}",
        )


def factory(thinker: ScriptedThinker) -> Any:
    return lambda _settings: thinker


@pytest.fixture()
def enabled(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("FEATURES__COMPANY_INTELLIGENCE", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


INDUSTRY_ANSWER: dict[str, Any] = {
    "classifications": [
        {
            "dimension": "industry",
            "value": "Manufacturing",
            "is_primary": True,
            "evidence": ["F1"],
            "confidence": 0.8,
        }
    ]
}


def ready_company(session: Session, *, name: str = "Kiln Systems") -> Company:
    company = make_company(session, name=name)
    make_dossier(session, company=company)
    make_fact(session, company=company, claim="industries served: manufacturing")
    return company


# --- the switch -------------------------------------------------------------


def test_the_feature_is_off_by_default(db_session: Session) -> None:
    assert get_settings().features.company_intelligence is False


def test_nothing_is_produced_while_the_feature_is_off(db_session: Session) -> None:
    seeded(db_session)
    company = ready_company(db_session)
    thinker = ScriptedThinker(INDUSTRY_ANSWER)

    outcome = ci_runner.produce_for_company(
        db_session, company=company, thinker_factory=factory(thinker)
    )
    assert outcome.succeeded is False
    assert outcome.code == ci_runner.FEATURE_DISABLED_CODE
    assert thinker.requests == [], "a disabled feature must not spend a model call"
    assert (
        db_session.scalars(
            select(CompanyIntelligenceVersion).where(
                CompanyIntelligenceVersion.company_id == company.id
            )
        ).first()
        is None
    )


# --- the runner -------------------------------------------------------------


def test_the_runner_produces_a_version_and_asks_for_no_tools(
    db_session: Session, enabled: None
) -> None:
    seeded(db_session)
    company = ready_company(db_session)
    thinker = ScriptedThinker(INDUSTRY_ANSWER)

    outcome = ci_runner.produce_for_company(
        db_session, company=company, thinker_factory=factory(thinker)
    )
    assert outcome.succeeded is True
    assert outcome.created is True
    assert outcome.version_number == 1
    assert len(thinker.requests) == 1
    assert thinker.requests[0].allowed_tools == (), (
        "the classifier reasons over persisted evidence; a lookup here would cite "
        "a source that never entered the dossier"
    )
    assert thinker.requests[0].purpose == "company_intelligence"


def test_the_prompt_carries_the_evidence_handles_and_the_vocabulary(
    db_session: Session, enabled: None
) -> None:
    seeded(db_session)
    company = ready_company(db_session)
    thinker = ScriptedThinker(INDUSTRY_ANSWER)
    ci_runner.produce_for_company(db_session, company=company, thinker_factory=factory(thinker))

    prompt = thinker.requests[0].prompt
    assert "[F1]" in prompt
    assert "Pharma & Healthcare" in prompt
    assert "unknown_dimensions" in prompt


def test_the_runner_does_not_call_the_model_when_the_answer_already_exists(
    db_session: Session, enabled: None
) -> None:
    seeded(db_session)
    company = ready_company(db_session)
    first = ScriptedThinker(INDUSTRY_ANSWER)
    ci_runner.produce_for_company(db_session, company=company, thinker_factory=factory(first))

    second = ScriptedThinker(INDUSTRY_ANSWER)
    outcome = ci_runner.produce_for_company(
        db_session, company=company, thinker_factory=factory(second)
    )
    assert outcome.succeeded is True
    assert outcome.created is False
    assert outcome.code == "reused_existing_version"
    assert second.requests == [], "idempotency has to be cheap or it is not idempotency"


def test_a_company_without_research_reports_a_reason_rather_than_failing(
    db_session: Session, enabled: None
) -> None:
    seeded(db_session)
    company = make_company(db_session)
    thinker = ScriptedThinker(INDUSTRY_ANSWER)

    outcome = ci_runner.produce_for_company(
        db_session, company=company, thinker_factory=factory(thinker)
    )
    assert outcome.succeeded is False
    assert outcome.code == ci_inputs.REASON_NO_DOSSIER
    assert outcome.retryable is False
    assert thinker.requests == []


def test_model_errors_keep_their_retry_classification(db_session: Session, enabled: None) -> None:
    seeded(db_session)
    company = ready_company(db_session)

    timeout = ci_runner.produce_for_company(
        db_session,
        company=company,
        thinker_factory=factory(ScriptedThinker(error=ThinkingTimeout("took too long"))),
    )
    assert timeout.succeeded is False and timeout.retryable is True

    unavailable = ci_runner.produce_for_company(
        db_session,
        company=company,
        thinker_factory=factory(ScriptedThinker(error=ThinkingUnavailable("no executable"))),
    )
    assert unavailable.succeeded is False and unavailable.retryable is False


def test_a_malformed_answer_fails_retryably_and_stores_nothing(
    db_session: Session, enabled: None
) -> None:
    seeded(db_session)
    company = ready_company(db_session)
    outcome = ci_runner.produce_for_company(
        db_session,
        company=company,
        thinker_factory=factory(ScriptedThinker({"classifications": "not a list"})),
    )
    assert outcome.succeeded is False
    assert outcome.retryable is True
    assert (
        db_session.scalars(
            select(CompanyIntelligenceVersion).where(
                CompanyIntelligenceVersion.company_id == company.id
            )
        ).first()
        is None
    )


# --- the queue --------------------------------------------------------------


def test_enqueue_is_idempotent_for_one_company_and_input(db_session: Session) -> None:
    company = ready_company(db_session)
    first, created_first = ci_jobs.enqueue(db_session, company=company, input_digest="abc")
    second, created_second = ci_jobs.enqueue(db_session, company=company, input_digest="abc")
    assert created_first is True and created_second is False
    assert first.id == second.id


def test_only_one_job_per_company_can_be_active(db_session: Session) -> None:
    company = ready_company(db_session)
    first, _ = ci_jobs.enqueue(db_session, company=company, input_digest="abc")
    second, created = ci_jobs.enqueue(db_session, company=company, input_digest="different")
    assert created is False
    assert second.id == first.id


def test_a_failed_job_can_be_requeued_for_the_same_evidence(db_session: Session) -> None:
    """A job that ended producing nothing is finished, not queued.

    Regression for a UAT finding. The idempotency key matched regardless of
    status, so after a non-retryable failure -- the CLI absent or not yet
    authenticated, both ordinary -- pressing "Run classification" returned the
    dead row and reported "Already queued". FAILED is not claimable, so no
    worker would ever pick it up and the company was stuck permanently.
    """

    company = ready_company(db_session)
    first, created = ci_jobs.enqueue(db_session, company=company, input_digest="abc")
    assert created is True
    ci_jobs.mark_failed(
        db_session,
        job=first,
        code="thinking_unavailable",
        message="The Claude CLI executable was not found on PATH.",
        retryable=False,
    )
    assert first.status is IntelligenceJobStatus.FAILED
    assert first.status not in ci_jobs.CLAIMABLE_STATUSES

    revived, created_again = ci_jobs.enqueue(
        db_session, company=company, input_digest="abc", requested_by="operator"
    )
    assert created_again is True, "a dead job must not absorb a fresh request"
    assert revived.id == first.id, "reviving in place keeps one row per key"
    assert revived.status is IntelligenceJobStatus.PENDING
    assert revived.status in ci_jobs.CLAIMABLE_STATUSES
    assert revived.attempts == 0
    assert revived.error_class is None
    assert revived.last_error is None
    assert revived.finished_at is None

    claimed = ci_jobs.claim_next(db_session, worker_id="worker-1", lease_seconds=60)
    assert claimed is not None and claimed.id == first.id


def test_a_succeeded_job_is_never_requeued_for_the_same_evidence(db_session: Session) -> None:
    """Reviving dead work must not become re-paying for answered work."""

    company = ready_company(db_session)
    first, _ = ci_jobs.enqueue(db_session, company=company, input_digest="abc")
    ci_jobs.mark_succeeded(db_session, job=first, result={"ok": True})

    again, created = ci_jobs.enqueue(db_session, company=company, input_digest="abc")
    assert created is False
    assert again.id == first.id
    assert again.status is IntelligenceJobStatus.SUCCEEDED


def test_an_active_job_still_absorbs_a_duplicate_request(db_session: Session) -> None:
    """The guarantee the idempotency key exists for is unchanged."""

    company = ready_company(db_session)
    first, _ = ci_jobs.enqueue(db_session, company=company, input_digest="abc")
    again, created = ci_jobs.enqueue(db_session, company=company, input_digest="abc")
    assert created is False
    assert again.id == first.id
    assert again.status is IntelligenceJobStatus.PENDING


def test_a_finished_job_frees_the_company_for_new_work(db_session: Session) -> None:
    company = ready_company(db_session)
    first, _ = ci_jobs.enqueue(db_session, company=company, input_digest="abc")
    ci_jobs.mark_succeeded(db_session, job=first, result={"ok": True})

    second, created = ci_jobs.enqueue(db_session, company=company, input_digest="def")
    assert created is True
    assert second.id != first.id


def test_claiming_leases_the_job_and_counts_the_attempt(db_session: Session) -> None:
    company = ready_company(db_session)
    ci_jobs.enqueue(db_session, company=company, input_digest="abc")

    job = ci_jobs.claim_next(db_session, worker_id="worker-1", lease_seconds=60)
    assert job is not None
    assert job.status is IntelligenceJobStatus.LEASED
    assert job.attempts == 1
    assert job.lease_owner == "worker-1"
    assert job.lease_expires_at is not None

    assert ci_jobs.claim_next(db_session, worker_id="worker-2") is None


def test_an_expired_lease_returns_the_job_to_the_queue(db_session: Session) -> None:
    company = ready_company(db_session)
    ci_jobs.enqueue(db_session, company=company, input_digest="abc")
    job = ci_jobs.claim_next(db_session, worker_id="worker-1", lease_seconds=1)
    assert job is not None

    later = datetime.now(UTC) + timedelta(minutes=5)
    recovered = ci_jobs.recover_expired_leases(db_session, now=later)
    assert [item.id for item in recovered] == [job.id]
    db_session.refresh(job)
    assert job.status is IntelligenceJobStatus.PENDING
    assert job.error_class == "lease_expired"

    again = ci_jobs.claim_next(db_session, worker_id="worker-2", now=later)
    assert again is not None and again.id == job.id
    assert again.attempts == 2


def test_an_expired_lease_with_no_attempts_left_fails(db_session: Session) -> None:
    company = ready_company(db_session)
    ci_jobs.enqueue(db_session, company=company, input_digest="abc", max_attempts=1)
    job = ci_jobs.claim_next(db_session, worker_id="worker-1", lease_seconds=1)
    assert job is not None

    ci_jobs.recover_expired_leases(db_session, now=datetime.now(UTC) + timedelta(minutes=5))
    db_session.refresh(job)
    assert job.status is IntelligenceJobStatus.FAILED


def test_a_retryable_failure_schedules_a_retry_and_a_final_one_does_not(
    db_session: Session,
) -> None:
    company = ready_company(db_session)
    ci_jobs.enqueue(db_session, company=company, input_digest="abc", max_attempts=2)
    job = ci_jobs.claim_next(db_session, worker_id="worker-1")
    assert job is not None

    ci_jobs.mark_failed(
        db_session, job=job, code="thinking_timeout", message="slow", retryable=True
    )
    assert job.status is IntelligenceJobStatus.RETRY_SCHEDULED
    assert job.next_run_at > datetime.now(UTC)

    job.next_run_at = datetime.now(UTC)
    db_session.flush()
    again = ci_jobs.claim_next(db_session, worker_id="worker-1")
    assert again is not None
    ci_jobs.mark_failed(
        db_session, job=again, code="thinking_timeout", message="slow", retryable=True
    )
    assert again.status is IntelligenceJobStatus.FAILED, "attempts are bounded"


def test_a_non_retryable_failure_is_terminal_immediately(db_session: Session) -> None:
    company = ready_company(db_session)
    ci_jobs.enqueue(db_session, company=company, input_digest="abc", max_attempts=5)
    job = ci_jobs.claim_next(db_session, worker_id="worker-1")
    assert job is not None
    ci_jobs.mark_failed(
        db_session,
        job=job,
        code=ci_inputs.REASON_NO_DOSSIER,
        message="nothing to classify",
        retryable=False,
    )
    assert job.status is IntelligenceJobStatus.FAILED
    assert job.finished_at is not None


def test_running_a_claimed_job_end_to_end_records_the_outcome(
    db_session: Session, enabled: None
) -> None:
    seeded(db_session)
    company = ready_company(db_session)
    ci_jobs.enqueue(db_session, company=company, input_digest="abc")

    outcome = ci_runner.run_next(
        db_session,
        worker_id="worker-1",
        thinker_factory=factory(ScriptedThinker(INDUSTRY_ANSWER)),
    )
    assert outcome is not None and outcome.succeeded is True

    job = db_session.scalars(
        select(CompanyIntelligenceJob).where(CompanyIntelligenceJob.company_id == company.id)
    ).one()
    assert job.status is IntelligenceJobStatus.SUCCEEDED
    assert job.result is not None and job.result["outcome"] == "intelligence_produced"
    assert job.finished_at is not None

    view = ci_read.get_company_intelligence(db_session, company_id=company.id)
    assert view is not None and view.has_intelligence is True


def test_run_next_returns_none_on_an_empty_queue(db_session: Session, enabled: None) -> None:
    assert ci_runner.run_next(db_session, worker_id="idle") is None


def test_deleting_a_company_takes_its_queued_work_with_it(
    db_session: Session, enabled: None
) -> None:
    """A job can never outlive its Company, so it can never classify a ghost.

    The runner still has a ``company_missing`` branch, because a defensive check
    that costs nothing is cheaper than reasoning about every future write path —
    but the cascade is what makes that branch unreachable in practice, and the
    cascade is the guarantee worth testing.
    """

    seeded(db_session)
    company = ready_company(db_session)
    ci_jobs.enqueue(db_session, company=company, input_digest="abc")
    assert db_session.scalars(select(CompanyIntelligenceJob)).first() is not None

    db_session.delete(company)
    db_session.flush()
    assert db_session.scalars(select(CompanyIntelligenceJob)).first() is None


# --- the backfill -----------------------------------------------------------


def test_a_dry_run_enqueues_nothing_and_still_reports_each_company(
    db_session: Session, enabled: None
) -> None:
    seeded(db_session)
    ready = ready_company(db_session, name="Kiln Systems")
    bare = make_company(db_session, name="No Research Ltd")

    run = ci_backfill.create_run(db_session, label="preview", dry_run=True, batch_size=10)
    report = ci_backfill.advance(db_session, run=run, feature_enabled=True)

    assert report.considered == 2
    assert report.enqueued == 0
    assert db_session.scalars(select(CompanyIntelligenceJob)).first() is None, (
        "a preview must not queue work"
    )

    items = {item.company_id: item for item in ci_backfill.run_items(db_session, run_id=run.id)}
    assert items[ready.id].outcome is IntelligenceBackfillOutcome.PREVIEWED
    assert items[bare.id].outcome is IntelligenceBackfillOutcome.SKIPPED
    assert items[bare.id].skip_reason == ci_inputs.REASON_NO_DOSSIER
    assert run.skip_reasons == {ci_inputs.REASON_NO_DOSSIER: 1}


def test_a_live_run_enqueues_exactly_the_eligible_companies(
    db_session: Session, enabled: None
) -> None:
    seeded(db_session)
    ready = ready_company(db_session, name="Kiln Systems")
    make_company(db_session, name="No Research Ltd")

    run = ci_backfill.create_run(db_session, label="live", dry_run=False, batch_size=10)
    report = ci_backfill.advance(db_session, run=run, feature_enabled=True)

    assert report.enqueued == 1
    jobs = db_session.scalars(select(CompanyIntelligenceJob)).all()
    assert [job.company_id for job in jobs] == [ready.id]
    assert jobs[0].backfill_run_id == run.id


def test_a_live_run_with_the_feature_off_queues_nothing_and_says_why(
    db_session: Session,
) -> None:
    seeded(db_session)
    ready_company(db_session)
    run = ci_backfill.create_run(db_session, label="live", dry_run=False, batch_size=10)
    ci_backfill.advance(db_session, run=run, feature_enabled=False)

    assert db_session.scalars(select(CompanyIntelligenceJob)).first() is None
    assert run.skip_reasons == {ci_backfill.SKIP_FEATURE_DISABLED: 1}


def test_batches_are_bounded_and_resume_from_the_cursor(db_session: Session, enabled: None) -> None:
    seeded(db_session)
    for index in range(5):
        ready_company(db_session, name=f"Company {index}")

    run = ci_backfill.create_run(db_session, label="paged", dry_run=False, batch_size=2)
    first = ci_backfill.advance(db_session, run=run, feature_enabled=True)
    assert first.considered == 2 and first.exhausted is False
    assert run.cursor_company_id is not None

    second = ci_backfill.advance(db_session, run=run, feature_enabled=True)
    assert second.considered == 2

    third = ci_backfill.advance(db_session, run=run, feature_enabled=True)
    assert third.considered == 1
    assert third.exhausted is True
    assert run.status is IntelligenceBackfillStatus.COMPLETED
    assert run.considered_count == 5
    assert len(ci_backfill.run_items(db_session, run_id=run.id)) == 5


def test_a_run_never_considers_the_same_company_twice(db_session: Session, enabled: None) -> None:
    seeded(db_session)
    for index in range(3):
        ready_company(db_session, name=f"Company {index}")

    run = ci_backfill.create_run(db_session, label="resume", dry_run=False, batch_size=2)
    ci_backfill.advance(db_session, run=run, feature_enabled=True)
    # Losing the cursor must not cause a second pass over the same companies:
    # the item rows are the real guard.
    run.cursor_company_id = None
    db_session.flush()
    ci_backfill.advance(db_session, run=run, feature_enabled=True)

    items = ci_backfill.run_items(db_session, run_id=run.id)
    assert len({item.company_id for item in items}) == len(items) == 3


def test_max_companies_is_a_hard_ceiling(db_session: Session, enabled: None) -> None:
    seeded(db_session)
    for index in range(5):
        ready_company(db_session, name=f"Company {index}")

    run = ci_backfill.create_run(
        db_session, label="capped", dry_run=False, batch_size=10, max_companies=2
    )
    report = ci_backfill.advance(db_session, run=run, feature_enabled=True)
    assert report.considered == 2
    assert run.status is IntelligenceBackfillStatus.COMPLETED
    assert len(db_session.scalars(select(CompanyIntelligenceJob)).all()) == 2


def test_a_company_already_covered_for_this_exact_input_is_skipped_truthfully(
    db_session: Session, enabled: None
) -> None:
    seeded(db_session)
    company = ready_company(db_session)
    ci_runner.produce_for_company(
        db_session, company=company, thinker_factory=factory(ScriptedThinker(INDUSTRY_ANSWER))
    )

    run = ci_backfill.create_run(db_session, label="second pass", dry_run=False, batch_size=10)
    ci_backfill.advance(db_session, run=run, feature_enabled=True)

    item = ci_backfill.run_items(db_session, run_id=run.id)[0]
    assert item.outcome is IntelligenceBackfillOutcome.SKIPPED
    assert item.skip_reason == ci_backfill.SKIP_ALREADY_PRODUCED
    assert db_session.scalars(select(CompanyIntelligenceJob)).first() is None


def test_a_company_with_a_job_in_flight_is_skipped(db_session: Session, enabled: None) -> None:
    seeded(db_session)
    company = ready_company(db_session)
    ci_jobs.enqueue(db_session, company=company, input_digest="something-else")

    run = ci_backfill.create_run(db_session, label="overlap", dry_run=False, batch_size=10)
    ci_backfill.advance(db_session, run=run, feature_enabled=True)

    item = ci_backfill.run_items(db_session, run_id=run.id)[0]
    assert item.outcome is IntelligenceBackfillOutcome.SKIPPED
    assert item.skip_reason == ci_backfill.SKIP_JOB_IN_FLIGHT
    assert len(db_session.scalars(select(CompanyIntelligenceJob)).all()) == 1


def test_pausing_and_resuming_keeps_the_cursor(db_session: Session, enabled: None) -> None:
    seeded(db_session)
    for index in range(4):
        ready_company(db_session, name=f"Company {index}")

    run = ci_backfill.create_run(db_session, label="pausable", dry_run=False, batch_size=2)
    ci_backfill.advance(db_session, run=run, feature_enabled=True)
    cursor = run.cursor_company_id

    ci_backfill.pause(db_session, run=run)
    assert run.status is IntelligenceBackfillStatus.PAUSED
    ci_backfill.resume(db_session, run=run)
    assert run.cursor_company_id == cursor

    ci_backfill.advance(db_session, run=run, feature_enabled=True)
    assert run.considered_count == 4


def test_a_cancelled_run_cannot_be_advanced_and_leaves_queued_work_alone(
    db_session: Session, enabled: None
) -> None:
    seeded(db_session)
    ready_company(db_session)
    # More companies than one batch, so the run is genuinely mid-flight rather
    # than already finished when it is cancelled.
    ready_company(db_session, name="Second Co")
    ready_company(db_session, name="Third Co")
    run = ci_backfill.create_run(db_session, label="cancelled", dry_run=False, batch_size=1)
    ci_backfill.advance(db_session, run=run, feature_enabled=True)
    queued = len(db_session.scalars(select(CompanyIntelligenceJob)).all())

    ci_backfill.cancel(db_session, run=run, reason="operator changed their mind")
    assert run.status is IntelligenceBackfillStatus.CANCELLED
    with pytest.raises(ci_backfill.BackfillError):
        ci_backfill.advance(db_session, run=run, feature_enabled=True)
    assert len(db_session.scalars(select(CompanyIntelligenceJob)).all()) == queued


def test_backfill_batch_size_is_validated(db_session: Session) -> None:
    with pytest.raises(ci_backfill.BackfillError):
        ci_backfill.create_run(db_session, label="bad", batch_size=0)
    with pytest.raises(ci_backfill.BackfillError):
        ci_backfill.create_run(db_session, label="bad", batch_size=10_000)


def test_every_skipped_item_carries_a_reason(db_session: Session, enabled: None) -> None:
    seeded(db_session)
    make_company(db_session, name="No Research Ltd")
    run = ci_backfill.create_run(db_session, label="reasons", dry_run=False, batch_size=10)
    ci_backfill.advance(db_session, run=run, feature_enabled=True)

    skipped = [
        item
        for item in db_session.scalars(select(CompanyIntelligenceBackfillItem)).all()
        if item.outcome is IntelligenceBackfillOutcome.SKIPPED
    ]
    assert skipped
    assert all(item.skip_reason for item in skipped)


def test_the_whole_loop_produces_intelligence_for_a_backfilled_company(
    db_session: Session, enabled: None
) -> None:
    seeded(db_session)
    company = ready_company(db_session)
    run = ci_backfill.create_run(db_session, label="end to end", dry_run=False, batch_size=10)
    ci_backfill.advance(db_session, run=run, feature_enabled=True)

    outcome = ci_runner.run_next(
        db_session,
        worker_id="worker-1",
        thinker_factory=factory(ScriptedThinker(INDUSTRY_ANSWER)),
    )
    assert outcome is not None and outcome.succeeded is True

    view = ci_read.get_company_intelligence(db_session, company_id=company.id)
    assert view is not None
    assert view.settled_values(IntelligenceDimension.INDUSTRY) == ("Manufacturing",)

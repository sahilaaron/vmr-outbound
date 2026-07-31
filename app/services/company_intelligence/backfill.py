"""Bounded, resumable backfill over Companies that already have Research (CI-001).

This is not a script. It is a durable run with per-company outcomes, a cursor, a
dry-run mode, and a hard ceiling — because the alternative, a loop in a
standalone file, has failed the same way every time it has been written: it runs
half way, somebody stops it, and nobody can say afterwards which companies it
reached, which it skipped, or why.

The shape:

* **Deterministic ordering.** ``(created_at, id)`` ascending. Not "whatever the
  planner returns": a resumed run must continue where it stopped, and a run whose
  order can change cannot have a cursor at all.
* **Bounded batches.** One :func:`advance` call considers at most ``batch_size``
  companies and returns. There is no unbounded synchronous loop anywhere in this
  module; a caller that wants the whole set calls it repeatedly and can stop
  between calls.
* **Idempotent and resumable.** One item row per (run, company), enforced by a
  unique constraint. Re-walking a company a run already recorded is a no-op, so
  a crash mid-batch costs at most one batch of duplicated *decisions* and zero
  duplicated jobs.
* **Dry run by default.** A preview walks the identical code path with the
  identical eligibility rules and records the identical per-company outcomes —
  it just does not enqueue. The report an operator reads before committing is
  therefore produced by the code that will actually run, not by a second
  implementation of it that can drift.
* **Truthful skips.** Every skip carries a reason code, and the reason codes are
  the same ones the job runner and the Admin screen use. A backfill that reports
  a company as handled when it was skipped is worse than one that fails loudly.

Enqueueing is all this does. It never produces inline, so it cannot spend a
hundred model calls inside one web request, and the worker's own bounds — one
active job per company, one version per input digest — still apply to everything
it queues.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.company_intelligence import (
    CompanyIntelligenceBackfillItem,
    CompanyIntelligenceBackfillRun,
    CompanyIntelligenceJob,
)
from app.models.enums import (
    IntelligenceBackfillOutcome,
    IntelligenceBackfillStatus,
)
from app.services.audit import record_audit_event
from app.services.company_intelligence import inputs as inputs_module
from app.services.company_intelligence import jobs as jobs_module
from app.services.company_intelligence import producer as producer_module
from app.services.company_intelligence.inputs import IntelligenceInputError
from app.services.company_intelligence.producer import POLICY_VERSION
from app.services.company_intelligence.runner import PRODUCER, PRODUCER_VERSION

BACKFILL_ACTOR = "operator"

DEFAULT_BATCH_SIZE = 25
MAX_BATCH_SIZE = 1000

#: Skip reasons this module can report. Two are its own; the rest are the input
#: assembler's, reused verbatim so one condition never acquires two names.
SKIP_ALREADY_PRODUCED = "already_current_for_input"
SKIP_JOB_IN_FLIGHT = "job_in_flight"
SKIP_FEATURE_DISABLED = "feature_disabled"


class BackfillError(ValueError):
    """A backfill operation that cannot be performed as asked."""


@dataclass(frozen=True)
class BatchReport:
    """What one bounded :func:`advance` call did."""

    run_id: uuid.UUID
    considered: int
    enqueued: int
    skipped: int
    failed: int
    exhausted: bool
    cursor_company_id: uuid.UUID | None
    skip_reasons: dict[str, int]

    @property
    def finished(self) -> bool:
        return self.exhausted


def create_run(
    session: Session,
    *,
    label: str,
    dry_run: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_companies: int | None = None,
    note: str | None = None,
    created_by: str | None = None,
) -> CompanyIntelligenceBackfillRun:
    """Open a backfill run. Nothing is walked until :func:`advance` is called."""

    if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
        raise BackfillError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    if max_companies is not None and max_companies < 1:
        raise BackfillError("max_companies must be at least 1 when set")

    run = CompanyIntelligenceBackfillRun(
        label=label.strip() or "company intelligence backfill",
        status=(
            IntelligenceBackfillStatus.PREVIEW if dry_run else IntelligenceBackfillStatus.RUNNING
        ),
        dry_run=dry_run,
        batch_size=batch_size,
        max_companies=max_companies,
        producer_version=PRODUCER_VERSION,
        policy_version=POLICY_VERSION,
        note=note,
        created_by=created_by,
    )
    session.add(run)
    session.flush()
    record_audit_event(
        session,
        actor=created_by or BACKFILL_ACTOR,
        action="company_intelligence.backfill_started",
        entity_type="company_intelligence_backfill_run",
        entity_id=str(run.id),
        new_state=run.status.value,
        reason=f"{'dry-run' if dry_run else 'live'} backfill opened",
        context={"batch_size": batch_size, "max_companies": max_companies},
    )
    return run


def advance(
    session: Session,
    *,
    run: CompanyIntelligenceBackfillRun,
    feature_enabled: bool,
    now: datetime | None = None,
    actor: str = BACKFILL_ACTOR,
) -> BatchReport:
    """Process at most ``run.batch_size`` more Companies. Bounded and resumable."""

    if run.status in (
        IntelligenceBackfillStatus.COMPLETED,
        IntelligenceBackfillStatus.CANCELLED,
    ):
        raise BackfillError(f"backfill run is {run.status.value} and cannot be advanced")

    moment = now or datetime.now(UTC)
    remaining_ceiling = (
        None if run.max_companies is None else max(run.max_companies - run.considered_count, 0)
    )
    if remaining_ceiling == 0:
        return _finish(session, run=run, moment=moment, actor=actor, exhausted=True)

    limit = run.batch_size if remaining_ceiling is None else min(run.batch_size, remaining_ceiling)
    companies = _next_companies(session, run=run, limit=limit)
    if not companies:
        return _finish(session, run=run, moment=moment, actor=actor, exhausted=True)

    counts = {"considered": 0, "enqueued": 0, "skipped": 0, "failed": 0}
    reasons: dict[str, int] = dict(run.skip_reasons or {})
    sequence = run.considered_count

    for company in companies:
        counts["considered"] += 1
        sequence += 1
        run.cursor_company_id = company.id

        outcome, reason, detail, job = _consider(
            session, run=run, company=company, feature_enabled=feature_enabled
        )
        if outcome is IntelligenceBackfillOutcome.ENQUEUED:
            counts["enqueued"] += 1
        elif outcome is IntelligenceBackfillOutcome.FAILED:
            counts["failed"] += 1
        elif outcome is IntelligenceBackfillOutcome.SKIPPED:
            counts["skipped"] += 1
            if reason:
                reasons[reason] = reasons.get(reason, 0) + 1

        session.add(
            CompanyIntelligenceBackfillItem(
                backfill_run_id=run.id,
                company_id=company.id,
                sequence=sequence,
                outcome=outcome,
                skip_reason=reason,
                detail=detail,
                job_id=job.id if job is not None else None,
            )
        )
        session.flush()

    run.considered_count += counts["considered"]
    run.enqueued_count += counts["enqueued"]
    run.skipped_count += counts["skipped"]
    run.failed_count += counts["failed"]
    run.skip_reasons = reasons
    session.flush()

    exhausted = len(companies) < limit or (
        run.max_companies is not None and run.considered_count >= run.max_companies
    )
    if exhausted:
        return _finish(
            session,
            run=run,
            moment=moment,
            actor=actor,
            exhausted=True,
            counts=counts,
            reasons=reasons,
        )

    return BatchReport(
        run_id=run.id,
        considered=counts["considered"],
        enqueued=counts["enqueued"],
        skipped=counts["skipped"],
        failed=counts["failed"],
        exhausted=False,
        cursor_company_id=run.cursor_company_id,
        skip_reasons=reasons,
    )


def _consider(
    session: Session,
    *,
    run: CompanyIntelligenceBackfillRun,
    company: Company,
    feature_enabled: bool,
) -> tuple[
    IntelligenceBackfillOutcome,
    str | None,
    str | None,
    CompanyIntelligenceJob | None,
]:
    """Decide one Company's outcome. The same rules in preview and in a live run."""

    try:
        source = inputs_module.assemble(
            session,
            company=company,
            producer=PRODUCER,
            producer_version=PRODUCER_VERSION,
            policy_version=POLICY_VERSION,
        )
    except IntelligenceInputError as exc:
        return IntelligenceBackfillOutcome.SKIPPED, exc.reason_code, exc.message[:500], None

    already = producer_module.existing_version(
        session, company_id=company.id, input_digest=source.digest
    )
    if already is not None:
        return (
            IntelligenceBackfillOutcome.SKIPPED,
            SKIP_ALREADY_PRODUCED,
            f"version {already.version_number} already covers this exact evidence",
            None,
        )

    active = jobs_module.active_job_for(session, company_id=company.id)
    if active is not None:
        return (
            IntelligenceBackfillOutcome.SKIPPED,
            SKIP_JOB_IN_FLIGHT,
            f"job {active.id} is already {active.status.value}",
            active,
        )

    if run.dry_run:
        return (
            IntelligenceBackfillOutcome.PREVIEWED,
            None,
            f"would enqueue: dossier v{source.dossier_version_number}, "
            f"{len(source.facts)} sourced fact(s)",
            None,
        )

    if not feature_enabled:
        # A live run with the switch off would queue work no worker will ever
        # execute. Saying so is more useful than a queue that silently fills.
        return (
            IntelligenceBackfillOutcome.SKIPPED,
            SKIP_FEATURE_DISABLED,
            "Company Intelligence is switched off, so nothing was queued",
            None,
        )

    job, created = jobs_module.enqueue(
        session,
        company=company,
        input_digest=source.digest,
        producer_version=PRODUCER_VERSION,
        policy_version=POLICY_VERSION,
        backfill_run_id=run.id,
        input_reference={
            "dossier_version": source.dossier_version_number,
            "sourced_facts": len(source.facts),
        },
        requested_by=run.created_by,
    )
    if not created:
        return (
            IntelligenceBackfillOutcome.SKIPPED,
            SKIP_JOB_IN_FLIGHT,
            f"an equivalent job ({job.id}) already existed",
            job,
        )
    return IntelligenceBackfillOutcome.ENQUEUED, None, None, job


def _next_companies(
    session: Session,
    *,
    run: CompanyIntelligenceBackfillRun,
    limit: int,
) -> list[Company]:
    """The next page of Companies, in the run's fixed order, skipping done ones.

    Ordering by ``(created_at, id)`` is what makes the cursor meaningful: the
    same run over the same data walks the same sequence, so "continue after this
    company" is a well-defined instruction rather than a hope.
    """

    statement = select(Company).order_by(Company.created_at.asc(), Company.id.asc())
    if run.cursor_company_id is not None:
        cursor = session.get(Company, run.cursor_company_id)
        if cursor is not None:
            statement = statement.where(
                tuple_(Company.created_at, Company.id) > (cursor.created_at, cursor.id)
            )
    # Belt and braces beside the cursor: a company already recorded for this run
    # is never reconsidered, even if the cursor was lost or the ordering key
    # changed underneath it.
    done = select(CompanyIntelligenceBackfillItem.company_id).where(
        CompanyIntelligenceBackfillItem.backfill_run_id == run.id
    )
    statement = statement.where(Company.id.not_in(done)).limit(limit)
    return list(session.scalars(statement).all())


def _finish(
    session: Session,
    *,
    run: CompanyIntelligenceBackfillRun,
    moment: datetime,
    actor: str,
    exhausted: bool,
    counts: dict[str, int] | None = None,
    reasons: dict[str, int] | None = None,
) -> BatchReport:
    run.status = IntelligenceBackfillStatus.COMPLETED
    run.finished_at = moment
    session.flush()
    record_audit_event(
        session,
        actor=actor,
        action="company_intelligence.backfill_completed",
        entity_type="company_intelligence_backfill_run",
        entity_id=str(run.id),
        new_state=run.status.value,
        reason=(
            f"{run.considered_count} considered, {run.enqueued_count} enqueued, "
            f"{run.skipped_count} skipped"
        ),
        context={"dry_run": run.dry_run, "skip_reasons": dict(run.skip_reasons or {})},
    )
    return BatchReport(
        run_id=run.id,
        considered=(counts or {}).get("considered", 0),
        enqueued=(counts or {}).get("enqueued", 0),
        skipped=(counts or {}).get("skipped", 0),
        failed=(counts or {}).get("failed", 0),
        exhausted=exhausted,
        cursor_company_id=run.cursor_company_id,
        skip_reasons=reasons if reasons is not None else dict(run.skip_reasons or {}),
    )


def pause(
    session: Session,
    *,
    run: CompanyIntelligenceBackfillRun,
    actor: str = BACKFILL_ACTOR,
) -> CompanyIntelligenceBackfillRun:
    """Stop advancing without losing the cursor. Resumable with :func:`resume`."""

    if run.status is not IntelligenceBackfillStatus.RUNNING:
        raise BackfillError(
            f"only a running backfill can be paused (this one is {run.status.value})"
        )
    run.status = IntelligenceBackfillStatus.PAUSED
    session.flush()
    record_audit_event(
        session,
        actor=actor,
        action="company_intelligence.backfill_paused",
        entity_type="company_intelligence_backfill_run",
        entity_id=str(run.id),
        new_state=run.status.value,
        reason="operator paused the backfill",
    )
    return run


def resume(
    session: Session,
    *,
    run: CompanyIntelligenceBackfillRun,
    actor: str = BACKFILL_ACTOR,
) -> CompanyIntelligenceBackfillRun:
    """Continue a paused run from its cursor."""

    if run.status is not IntelligenceBackfillStatus.PAUSED:
        raise BackfillError(
            f"only a paused backfill can be resumed (this one is {run.status.value})"
        )
    run.status = IntelligenceBackfillStatus.RUNNING
    session.flush()
    record_audit_event(
        session,
        actor=actor,
        action="company_intelligence.backfill_resumed",
        entity_type="company_intelligence_backfill_run",
        entity_id=str(run.id),
        new_state=run.status.value,
        reason="operator resumed the backfill",
    )
    return run


def cancel(
    session: Session,
    *,
    run: CompanyIntelligenceBackfillRun,
    reason: str,
    actor: str = BACKFILL_ACTOR,
    now: datetime | None = None,
) -> CompanyIntelligenceBackfillRun:
    """End a run early. Already-queued jobs are left alone, deliberately.

    Cancelling the plan is not the same as cancelling the work it already
    committed to: those jobs are idempotent, bounded and individually
    cancellable, and silently killing them would make "cancel" mean two things.
    """

    if run.status in (
        IntelligenceBackfillStatus.COMPLETED,
        IntelligenceBackfillStatus.CANCELLED,
    ):
        return run
    run.status = IntelligenceBackfillStatus.CANCELLED
    run.finished_at = now or datetime.now(UTC)
    run.note = reason[:1000]
    session.flush()
    record_audit_event(
        session,
        actor=actor,
        action="company_intelligence.backfill_cancelled",
        entity_type="company_intelligence_backfill_run",
        entity_id=str(run.id),
        new_state=run.status.value,
        reason=reason[:500],
    )
    return run


def eligible_company_count(session: Session) -> int:
    """How many Companies have a current dossier at all.

    An upper bound on what a backfill can do, not a promise: eligibility also
    requires sourced facts or populated sections, which is decided per company
    by the input assembler.
    """

    from app.models.company_dossier import CompanyDossierVersion

    return int(
        session.scalar(
            select(func.count(func.distinct(CompanyDossierVersion.company_id))).where(
                CompanyDossierVersion.is_current.is_(True)
            )
        )
        or 0
    )


def run_items(
    session: Session,
    *,
    run_id: uuid.UUID,
    limit: int = 200,
    offset: int = 0,
) -> list[CompanyIntelligenceBackfillItem]:
    """One page of a run's per-company outcomes, in the order they happened."""

    return list(
        session.scalars(
            select(CompanyIntelligenceBackfillItem)
            .where(CompanyIntelligenceBackfillItem.backfill_run_id == run_id)
            .order_by(CompanyIntelligenceBackfillItem.sequence)
            .offset(offset)
            .limit(limit)
        ).all()
    )


def list_runs(session: Session, *, limit: int = 50) -> list[CompanyIntelligenceBackfillRun]:
    """Recent backfill runs, newest first."""

    return list(
        session.scalars(
            select(CompanyIntelligenceBackfillRun)
            .order_by(CompanyIntelligenceBackfillRun.created_at.desc())
            .limit(limit)
        ).all()
    )

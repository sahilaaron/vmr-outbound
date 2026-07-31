"""PostgreSQL regressions for Campaign pause and worker concurrency.

These tests deliberately use independent database connections.  Thread coordination
uses barriers and events; wall-clock sleeps are not used to decide who won a race.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from app.db.session import engine
from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    AgentJobStatus,
    CampaignStatus,
    PipelineEventType,
    PipelineStageStatus,
)
from app.models.pipeline import CampaignContactAgentState
from app.models.verification_job import AgentJob
from app.services import campaign_contacts, campaigns, pipeline
from app.services.agents import controls, jobs, locking
from app.services.agents.adapters import AgentExecutionContext, AgentExecutionResult
from app.services.agents.orchestrator import (
    claim_next_campaign_job,
    execute_started_job,
    prepare_leased_job,
    reconcile_agent_control,
)
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

THREAD_TIMEOUT = 10.0


@dataclass(frozen=True)
class _Seeded:
    campaign_id: uuid.UUID
    membership_ids: tuple[uuid.UUID, ...]
    job_ids: tuple[uuid.UUID, ...]


def _seed(session: Session, *, contacts: int = 1) -> _Seeded:
    campaign = Campaign(
        name=f"Pause concurrency {uuid.uuid4()}",
        status=CampaignStatus.ACTIVE,
        execution_enabled=True,
    )
    company = Company(name=f"Concurrency {uuid.uuid4()}", domain="concurrency.example")
    session.add_all([campaign, company])
    session.flush()
    membership_ids: list[uuid.UUID] = []
    job_ids: list[uuid.UUID] = []
    for number in range(contacts):
        contact = Contact(
            first_name=f"Person{number}",
            last_name="Concurrency",
            company_name=company.name,
            company_domain=company.domain,
            natural_key=f"person{number}|concurrency|{uuid.uuid4()}",
        )
        session.add(contact)
        session.flush()
        enrolled = campaign_contacts.enrol_contact(
            session,
            campaign_id=campaign.id,
            contact_id=contact.id,
            source_type="test",
            enqueue=True,
            desired_stage=AgentIdentifier.IDENTITY,
        )
        assert enrolled.queued_job is not None
        membership_ids.append(enrolled.membership.id)
        job_ids.append(enrolled.queued_job.id)
    session.commit()
    return _Seeded(
        campaign_id=campaign.id,
        membership_ids=tuple(membership_ids),
        job_ids=tuple(job_ids),
    )


def _join(*threads: threading.Thread) -> None:
    for thread in threads:
        thread.join(THREAD_TIMEOUT)
        assert not thread.is_alive(), f"thread {thread.name} did not finish"


def _wait_for(
    predicate: Callable[[], bool],
    *,
    timeout: float = THREAD_TIMEOUT,
) -> None:
    deadline = time.monotonic() + timeout
    wake = threading.Event()
    while time.monotonic() < deadline:
        if predicate():
            return
        wake.wait(0.01)
    raise AssertionError("database condition was not reached before the timeout")


def _campaign_enabled(campaign_id: uuid.UUID, expected: bool) -> bool:
    with Session(bind=engine) as session:
        return (
            session.scalar(select(Campaign.execution_enabled).where(Campaign.id == campaign_id))
            is expected
        )


def test_previous_contact_then_job_vs_job_then_contact_cycle_is_a_real_deadlock(
    committed_session: Session,
) -> None:
    """Reproduce the exact inverse order that existed before the repair."""

    seeded = _seed(committed_session)
    rendezvous = threading.Barrier(2)
    errors: list[OperationalError] = []
    guard = threading.Lock()

    def contact_then_job() -> None:
        with Session(bind=engine) as session:
            session.execute(text("SET deadlock_timeout = '50ms'"))
            try:
                session.scalars(
                    select(CampaignContact)
                    .where(CampaignContact.id == seeded.membership_ids[0])
                    .with_for_update()
                ).one()
                rendezvous.wait()
                session.scalars(
                    select(AgentJob).where(AgentJob.id == seeded.job_ids[0]).with_for_update()
                ).one()
                session.commit()
            except OperationalError as exc:
                session.rollback()
                with guard:
                    errors.append(exc)

    def job_then_contact() -> None:
        with Session(bind=engine) as session:
            session.execute(text("SET deadlock_timeout = '50ms'"))
            try:
                session.scalars(
                    select(AgentJob).where(AgentJob.id == seeded.job_ids[0]).with_for_update()
                ).one()
                rendezvous.wait()
                session.scalars(
                    select(CampaignContact)
                    .where(CampaignContact.id == seeded.membership_ids[0])
                    .with_for_update()
                ).one()
                session.commit()
            except OperationalError as exc:
                session.rollback()
                with guard:
                    errors.append(exc)

    first = threading.Thread(target=contact_then_job, name="old-pause-order", daemon=True)
    second = threading.Thread(target=job_then_contact, name="old-worker-order", daemon=True)
    first.start()
    second.start()
    _join(first, second)

    assert len(errors) == 1
    assert campaigns.is_postgresql_deadlock(errors[0])


class _BlockingIdentity:
    agent_id = AgentIdentifier.IDENTITY

    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self._entered = entered
        self._release = release

    def execute(self, context: AgentExecutionContext) -> AgentExecutionResult:
        del context
        self._entered.set()
        assert self._release.wait(THREAD_TIMEOUT), "test never released the running Agent"
        return AgentExecutionResult(
            result={"identity": "committed"},
            output_reference={"identity": "committed"},
            outcome_committed=True,
        )


def test_pause_during_running_work_completes_without_deadlock_and_blocks_new_leases(
    committed_session: Session,
) -> None:
    seeded = _seed(committed_session, contacts=2)
    claimed = claim_next_campaign_job(committed_session, worker_id="worker-running")
    assert claimed is not None
    running_job_id = claimed.id
    committed_session.commit()
    locked = locking.lock_job_context(committed_session, running_job_id)
    assert locked is not None
    assert (
        prepare_leased_job(
            committed_session,
            job=locked.job,
            worker_id="worker-running",
        )
        is None
    )
    committed_session.commit()

    entered = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []

    def finish_running() -> None:
        with Session(bind=engine, expire_on_commit=False) as session:
            try:
                context = locking.lock_job_context(session, running_job_id)
                assert context is not None
                execute_started_job(
                    session,
                    job=context.job,
                    worker_id="worker-running",
                    adapters={AgentIdentifier.IDENTITY: _BlockingIdentity(entered, release)},
                )
                session.commit()
            except BaseException as exc:  # pragma: no cover - asserted below
                session.rollback()
                failures.append(exc)

    def pause() -> None:
        with Session(bind=engine, expire_on_commit=False) as session:
            try:
                campaigns.apply_campaign_execution(
                    session,
                    seeded.campaign_id,
                    enabled=False,
                    actor="pause-test",
                    batch_size=1,
                    sleep=lambda _seconds: None,
                    jitter=lambda low, _high: low,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

    worker = threading.Thread(target=finish_running, name="worker-completion", daemon=True)
    worker.start()
    assert entered.wait(THREAD_TIMEOUT)
    pauser = threading.Thread(target=pause, name="campaign-pause", daemon=True)
    pauser.start()
    _wait_for(lambda: _campaign_enabled(seeded.campaign_id, False))
    release.set()
    _join(worker, pauser)
    assert failures == []

    committed_session.expire_all()
    completed = committed_session.get(AgentJob, running_job_id)
    assert completed is not None and completed.status is AgentJobStatus.SUCCEEDED
    state = committed_session.scalars(
        select(CampaignContactAgentState).where(
            CampaignContactAgentState.campaign_contact_id == completed.campaign_contact_id,
            CampaignContactAgentState.agent_id == AgentIdentifier.IDENTITY,
        )
    ).one()
    assert state.status is PipelineStageStatus.COMPLETED

    other = committed_session.get(
        AgentJob, next(job for job in seeded.job_ids if job != running_job_id)
    )
    assert other is not None and other.status is AgentJobStatus.PAUSED
    assert other.error_class == "agent_disabled"
    assert claim_next_campaign_job(committed_session, worker_id="prohibited-worker") is None


def test_eight_workers_claim_distinct_jobs_under_the_shared_order(
    committed_session: Session,
) -> None:
    _seed(committed_session, contacts=16)
    rendezvous = threading.Barrier(8)
    claimed_ids: list[uuid.UUID] = []
    failures: list[BaseException] = []
    guard = threading.Lock()

    def claim(number: int) -> None:
        with Session(bind=engine) as session:
            try:
                rendezvous.wait()
                job = claim_next_campaign_job(session, worker_id=f"worker-{number}")
                assert job is not None
                session.commit()
                with guard:
                    claimed_ids.append(job.id)
            except BaseException as exc:  # pragma: no cover - asserted below
                session.rollback()
                with guard:
                    failures.append(exc)

    threads = [threading.Thread(target=claim, args=(number,), daemon=True) for number in range(8)]
    for thread in threads:
        thread.start()
    _join(*threads)
    assert failures == []
    assert len(claimed_ids) == 8
    assert len(set(claimed_ids)) == 8


def test_pause_preserves_a_leased_job_until_its_owner_reaches_the_gate(
    committed_session: Session,
) -> None:
    seeded = _seed(committed_session)
    leased = claim_next_campaign_job(committed_session, worker_id="lease-owner")
    assert leased is not None
    committed_session.commit()

    campaigns.apply_campaign_execution(
        committed_session,
        seeded.campaign_id,
        enabled=False,
        actor="pause-test",
        sleep=lambda _seconds: None,
        jitter=lambda low, _high: low,
    )
    committed_session.expire_all()
    preserved = committed_session.get(AgentJob, leased.id)
    assert preserved is not None
    assert preserved.status is AgentJobStatus.LEASED
    assert preserved.lease_owner == "lease-owner"
    assert claim_next_campaign_job(committed_session, worker_id="other-worker") is None

    context = locking.lock_job_context(committed_session, leased.id)
    assert context is not None
    rejected = prepare_leased_job(
        committed_session,
        job=context.job,
        worker_id="lease-owner",
    )
    assert rejected is not None
    committed_session.commit()
    assert context.job.status is AgentJobStatus.PAUSED
    assert context.job.error_class == "agent_disabled"


def test_two_simultaneous_pause_requests_are_idempotent(committed_session: Session) -> None:
    seeded = _seed(committed_session, contacts=4)
    rendezvous = threading.Barrier(2)
    failures: list[BaseException] = []

    def pause(actor: str) -> None:
        with Session(bind=engine) as session:
            try:
                rendezvous.wait()
                campaigns.apply_campaign_execution(
                    session,
                    seeded.campaign_id,
                    enabled=False,
                    actor=actor,
                    batch_size=2,
                    sleep=lambda _seconds: None,
                    jitter=lambda low, _high: low,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

    first = threading.Thread(target=pause, args=("pause-one",), daemon=True)
    second = threading.Thread(target=pause, args=("pause-two",), daemon=True)
    first.start()
    second.start()
    _join(first, second)
    assert failures == []
    committed_session.expire_all()
    assert committed_session.get(Campaign, seeded.campaign_id).execution_enabled is False  # type: ignore[union-attr]
    stored = committed_session.scalars(
        select(AgentJob).where(AgentJob.id.in_(seeded.job_ids))
    ).all()
    assert {job.status for job in stored} == {AgentJobStatus.PAUSED}


def test_pause_followed_by_resume_converges_to_the_newer_switch(
    committed_session: Session,
) -> None:
    seeded = _seed(committed_session, contacts=2)
    blocker_ready = threading.Event()
    release_blocker = threading.Event()
    failures: list[BaseException] = []

    def hold_first_contact() -> None:
        with Session(bind=engine) as session:
            locking.lock_campaign_contact(session, seeded.membership_ids[0])
            blocker_ready.set()
            assert release_blocker.wait(THREAD_TIMEOUT)
            session.commit()

    def toggle(enabled: bool, actor: str) -> None:
        with Session(bind=engine) as session:
            try:
                campaigns.apply_campaign_execution(
                    session,
                    seeded.campaign_id,
                    enabled=enabled,
                    actor=actor,
                    batch_size=1,
                    sleep=lambda _seconds: None,
                    jitter=lambda low, _high: low,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

    blocker = threading.Thread(target=hold_first_contact, daemon=True)
    blocker.start()
    assert blocker_ready.wait(THREAD_TIMEOUT)
    pauser = threading.Thread(target=toggle, args=(False, "pause"), daemon=True)
    pauser.start()
    _wait_for(lambda: _campaign_enabled(seeded.campaign_id, False))
    resumer = threading.Thread(target=toggle, args=(True, "resume"), daemon=True)
    resumer.start()
    _wait_for(lambda: _campaign_enabled(seeded.campaign_id, True))
    release_blocker.set()
    _join(blocker, pauser, resumer)
    assert failures == []

    committed_session.expire_all()
    campaign = committed_session.get(Campaign, seeded.campaign_id)
    assert campaign is not None and campaign.execution_enabled is True
    stored = committed_session.scalars(
        select(AgentJob).where(AgentJob.id.in_(seeded.job_ids))
    ).all()
    assert {job.status for job in stored} == {AgentJobStatus.PENDING}


def test_campaign_pause_and_global_control_reconciliation_share_one_order(
    committed_session: Session,
) -> None:
    seeded = _seed(committed_session, contacts=6)
    rendezvous = threading.Barrier(2)
    failures: list[BaseException] = []

    def campaign_pause() -> None:
        with Session(bind=engine) as session:
            try:
                rendezvous.wait()
                campaigns.apply_campaign_execution(
                    session,
                    seeded.campaign_id,
                    enabled=False,
                    actor="campaign-pause",
                    batch_size=2,
                    sleep=lambda _seconds: None,
                    jitter=lambda low, _high: low,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

    def global_pause() -> None:
        with Session(bind=engine) as session:
            try:
                rendezvous.wait()
                controls.set_global_control(
                    session,
                    agent_id=AgentIdentifier.IDENTITY,
                    status=AgentControlStatus.PAUSED,
                    reason="global maintenance",
                )
                reconcile_agent_control(
                    session,
                    agent_id=AgentIdentifier.IDENTITY,
                    actor="global-pause",
                )
                session.commit()
            except BaseException as exc:  # pragma: no cover - asserted below
                session.rollback()
                failures.append(exc)

    first = threading.Thread(target=campaign_pause, daemon=True)
    second = threading.Thread(target=global_pause, daemon=True)
    first.start()
    second.start()
    _join(first, second)
    assert failures == []
    committed_session.expire_all()
    assert {job.status for job in committed_session.scalars(select(AgentJob)).all()} == {
        AgentJobStatus.PAUSED
    }


def test_domain_pause_survives_campaign_pause_and_resume(committed_session: Session) -> None:
    seeded = _seed(committed_session, contacts=2)
    domain_job = committed_session.get(AgentJob, seeded.job_ids[0])
    domain_membership = committed_session.get(CampaignContact, seeded.membership_ids[0])
    assert domain_job is not None and domain_membership is not None
    jobs.mark_paused(
        committed_session,
        domain_job,
        reason="waiting for permanent evidence",
        reason_code="dependency_wait",
    )
    pipeline.transition_stage(
        committed_session,
        membership=domain_membership,
        agent_id=AgentIdentifier.IDENTITY,
        target=PipelineStageStatus.WAITING,
        event_type=PipelineEventType.STAGE_WAITING,
        actor="domain-test",
        job=domain_job,
        reason_code="dependency_wait",
        reason_detail="waiting for permanent evidence",
    )
    committed_session.commit()

    campaigns.apply_campaign_execution(
        committed_session,
        seeded.campaign_id,
        enabled=False,
        actor="pause",
        sleep=lambda _seconds: None,
        jitter=lambda low, _high: low,
    )
    campaigns.apply_campaign_execution(
        committed_session,
        seeded.campaign_id,
        enabled=True,
        actor="resume",
        sleep=lambda _seconds: None,
        jitter=lambda low, _high: low,
    )
    committed_session.expire_all()
    preserved = committed_session.get(AgentJob, domain_job.id)
    control_owned = committed_session.get(AgentJob, seeded.job_ids[1])
    assert preserved is not None and preserved.status is AgentJobStatus.PAUSED
    assert preserved.error_class == "dependency_wait"
    assert control_owned is not None and control_owned.status is AgentJobStatus.PENDING


class _SqlStateError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class _FailingCommitSession:
    def __init__(self, sqlstate: str) -> None:
        self.sqlstate = sqlstate
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1
        raise OperationalError("COMMIT", {}, _SqlStateError(self.sqlstate))

    def rollback(self) -> None:
        self.rollbacks += 1


def test_unrelated_operational_error_is_not_retried() -> None:
    session = _FailingCommitSession("08006")
    calls = 0

    def operation() -> None:
        nonlocal calls
        calls += 1

    with pytest.raises(campaigns.CampaignPersistenceError):
        campaigns._commit_with_deadlock_retry(
            session,  # type: ignore[arg-type]
            operation,
            attempts=3,
            sleep=lambda _seconds: None,
            jitter=lambda low, _high: low,
        )
    assert calls == 1
    assert session.commits == 1


def test_exhausted_deadlock_retries_raise_a_controlled_error() -> None:
    session = _FailingCommitSession("40P01")
    calls = 0
    backoffs: list[float] = []

    def operation() -> None:
        nonlocal calls
        calls += 1

    with pytest.raises(campaigns.CampaignConcurrencyError):
        campaigns._commit_with_deadlock_retry(
            session,  # type: ignore[arg-type]
            operation,
            attempts=3,
            sleep=backoffs.append,
            jitter=lambda low, _high: low,
        )
    assert calls == 3
    assert session.commits == 3
    assert len(backoffs) == 2

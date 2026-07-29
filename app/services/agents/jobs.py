"""PostgreSQL-backed durable queue shared by all Agents."""

from __future__ import annotations

import json
import random
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.enums import AgentIdentifier, AgentJobStatus
from app.models.verification_job import AgentJob
from app.services.audit import record_audit_event

CLAIMABLE_STATUSES = (AgentJobStatus.PENDING, AgentJobStatus.RETRY_SCHEDULED)
LEASED_STATUSES = (AgentJobStatus.LEASED, AgentJobStatus.IN_PROGRESS)
TERMINAL_STATUSES = (
    AgentJobStatus.SUCCEEDED,
    AgentJobStatus.FAILED,
    AgentJobStatus.CANCELLED,
)

_PUBLIC_STATUS = {
    AgentJobStatus.PENDING: "queued",
    AgentJobStatus.LEASED: "leased",
    AgentJobStatus.IN_PROGRESS: "running",
    AgentJobStatus.RETRY_SCHEDULED: "retrying",
    AgentJobStatus.FAILED: "failed",
    AgentJobStatus.SUCCEEDED: "completed",
    AgentJobStatus.PAUSED: "paused",
    AgentJobStatus.CANCELLED: "cancelled",
}


class AgentJobError(Exception):
    """Safe queue contract error."""


class AgentJobNotFound(AgentJobError):
    pass


class JobIdempotencyConflict(AgentJobError):
    pass


def public_status(job: AgentJob) -> str:
    return _PUBLIC_STATUS[job.status]


def _now() -> datetime:
    return datetime.now(UTC)


def _input(value: dict[str, Any] | None) -> dict[str, Any]:
    clean = value or {}
    if not isinstance(clean, dict):
        raise AgentJobError("job input_reference must be a JSON object")
    try:
        encoded = json.dumps(clean, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise AgentJobError("job input_reference must contain JSON values") from exc
    if len(encoded.encode("utf-8")) > 100_000:
        raise AgentJobError("job input_reference is too large (max 100000 bytes)")
    return clean


def _same_intent(
    job: AgentJob,
    *,
    agent_id: AgentIdentifier,
    email: str | None,
    policy_version: str | None,
    campaign_id: uuid.UUID | None,
    campaign_contact_id: uuid.UUID | None,
    contact_id: uuid.UUID | None,
    company_id: uuid.UUID | None,
    capture_id: uuid.UUID | None,
    entity_type: str | None,
    entity_id: uuid.UUID | None,
    task_kind: str,
    input_reference: dict[str, Any],
    parent_job_id: uuid.UUID | None,
) -> bool:
    return (
        job.agent_id is agent_id
        and job.email == email
        and job.policy_version == policy_version
        and job.campaign_id == campaign_id
        and job.campaign_contact_id == campaign_contact_id
        and job.contact_id == contact_id
        and job.company_id == company_id
        and job.capture_id == capture_id
        and job.entity_type == entity_type
        and job.entity_id == entity_id
        and job.task_kind == task_kind
        and (job.input_reference or {}) == input_reference
        and job.parent_job_id == parent_job_id
    )


def enqueue_job(
    session: Session,
    *,
    agent_id: AgentIdentifier,
    idempotency_key: str,
    task_kind: str,
    max_attempts: int,
    priority: int = 100,
    email: str | None = None,
    policy_version: str | None = None,
    campaign_id: uuid.UUID | None = None,
    campaign_contact_id: uuid.UUID | None = None,
    contact_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
    capture_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    input_reference: dict[str, Any] | None = None,
    parent_job_id: uuid.UUID | None = None,
    available_at: datetime | None = None,
    actor: str = "system",
) -> tuple[AgentJob, bool]:
    """Enqueue one intent idempotently; detect key reuse with different content."""

    clean_key = idempotency_key.strip()
    if not clean_key or len(clean_key) > 400:
        raise AgentJobError("idempotency_key must be 1 to 400 characters")
    clean_task = task_kind.strip()
    if not clean_task or len(clean_task) > 96:
        raise AgentJobError("task_kind must be 1 to 96 characters")
    if max_attempts < 1 or max_attempts > 100:
        raise AgentJobError("max_attempts must be between 1 and 100")
    if priority < -1_000_000 or priority > 1_000_000:
        raise AgentJobError("priority is outside the supported range")
    clean_input = _input(input_reference)

    existing = session.scalars(
        select(AgentJob).where(AgentJob.idempotency_key == clean_key)
    ).one_or_none()
    if existing is not None:
        if not _same_intent(
            existing,
            agent_id=agent_id,
            email=email,
            policy_version=policy_version,
            campaign_id=campaign_id,
            campaign_contact_id=campaign_contact_id,
            contact_id=contact_id,
            company_id=company_id,
            capture_id=capture_id,
            entity_type=entity_type,
            entity_id=entity_id,
            task_kind=clean_task,
            input_reference=clean_input,
            parent_job_id=parent_job_id,
        ):
            raise JobIdempotencyConflict(
                "job idempotency key was reused for a different execution intent"
            )
        return existing, False

    job = AgentJob(
        agent_id=agent_id,
        email=email,
        policy_version=policy_version,
        task_kind=clean_task,
        priority=priority,
        entity_type=entity_type,
        entity_id=entity_id,
        contact_id=contact_id,
        company_id=company_id,
        capture_id=capture_id,
        campaign_id=campaign_id,
        campaign_contact_id=campaign_contact_id,
        idempotency_key=clean_key,
        status=AgentJobStatus.PENDING,
        attempts=0,
        max_attempts=max_attempts,
        next_run_at=available_at or _now(),
        input_reference=clean_input,
        parent_job_id=parent_job_id,
    )
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError as exc:
        winner = session.scalars(
            select(AgentJob).where(AgentJob.idempotency_key == clean_key)
        ).one_or_none()
        if winner is None:  # pragma: no cover - defensive
            raise
        if not _same_intent(
            winner,
            agent_id=agent_id,
            email=email,
            policy_version=policy_version,
            campaign_id=campaign_id,
            campaign_contact_id=campaign_contact_id,
            contact_id=contact_id,
            company_id=company_id,
            capture_id=capture_id,
            entity_type=entity_type,
            entity_id=entity_id,
            task_kind=clean_task,
            input_reference=clean_input,
            parent_job_id=parent_job_id,
        ):
            raise JobIdempotencyConflict(
                "job idempotency key was reused for a different execution intent"
            ) from exc
        return winner, False
    record_audit_event(
        session,
        actor=actor,
        action="agent_job.enqueued",
        entity_type="agent_job",
        entity_id=str(job.id),
        new_state=public_status(job),
        reason=f"{agent_id.value} job queued",
        context={
            "campaign_id": str(campaign_id) if campaign_id else None,
            "campaign_contact_id": (str(campaign_contact_id) if campaign_contact_id else None),
            "task_kind": clean_task,
            "priority": priority,
        },
    )
    return job, True


def claim_next_job(
    session: Session,
    *,
    worker_id: str,
    lease_seconds: float,
    agent_ids: Iterable[AgentIdentifier] | None = None,
    campaign_contact_only: bool = False,
    recover_abandoned: bool = True,
    now: datetime | None = None,
) -> AgentJob | None:
    """Atomically lease the highest-priority due job using ``SKIP LOCKED``."""

    clean_worker = worker_id.strip()
    if not clean_worker or len(clean_worker) > 100:
        raise AgentJobError("worker_id must be 1 to 100 characters")
    if lease_seconds <= 0:
        raise AgentJobError("lease_seconds must be positive")
    now = now or _now()
    # Recovery is part of every claim pass, so an application restart needs no
    # separate scheduler. Exhausted abandoned work becomes FAILED; otherwise it
    # becomes due PENDING work while retaining a durable lease_expired marker.
    allowed = tuple(agent_ids or ())
    if recover_abandoned:
        recover_expired_leases(session, now=now, agent_ids=allowed or None)
    due = AgentJob.status.in_(CLAIMABLE_STATUSES) & (AgentJob.next_run_at <= now)
    stmt = select(AgentJob).where(due)
    if allowed:
        stmt = stmt.where(AgentJob.agent_id.in_(allowed))
    if campaign_contact_only:
        stmt = stmt.where(AgentJob.campaign_contact_id.is_not(None))
    stmt = (
        stmt.order_by(
            AgentJob.priority.desc(), AgentJob.next_run_at.asc(), AgentJob.created_at.asc()
        )
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = session.scalars(stmt).first()
    if job is None:
        return None
    reclaimed = job.error_class == "lease_expired"
    job.status = AgentJobStatus.LEASED
    job.attempts += 1
    job.lease_owner = clean_worker
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
    if not reclaimed:
        job.error = None
        job.error_class = None
        job.last_error = None
    session.flush()
    job.__dict__["_reclaimed"] = reclaimed
    return job


def claim_job(
    session: Session,
    *,
    job_id: uuid.UUID,
    worker_id: str,
    lease_seconds: float,
    now: datetime | None = None,
) -> AgentJob | None:
    """Lease one exact due job without consuming any neighbouring queue item.

    This is used by deliberate one-job execution paths such as the live
    verification smoke test.  It preserves the same lease and recovery semantics
    as :func:`claim_next_job` while guaranteeing that an unrelated queued job can
    never be selected instead.
    """

    clean_worker = worker_id.strip()
    if not clean_worker or len(clean_worker) > 100:
        raise AgentJobError("worker_id must be 1 to 100 characters")
    if lease_seconds <= 0:
        raise AgentJobError("lease_seconds must be positive")
    now = now or _now()
    job = session.scalars(
        select(AgentJob).where(AgentJob.id == job_id).with_for_update(skip_locked=True)
    ).one_or_none()
    if job is None:
        return None

    reclaimed = False
    if (
        job.status in LEASED_STATUSES
        and job.lease_expires_at is not None
        and job.lease_expires_at < now
    ):
        reclaimed = True
        job.lease_owner = None
        job.lease_expires_at = None
        job.last_error = "reclaimed after worker lease expired"
        job.error_class = "lease_expired"
        job.error = {
            "class": "lease_expired",
            "message": job.last_error,
            "retryable": job.attempts < job.max_attempts,
        }
        if job.attempts >= job.max_attempts:
            job.status = AgentJobStatus.FAILED
            job.finished_at = now
            session.flush()
            return None
        job.status = AgentJobStatus.PENDING
        job.next_run_at = now

    if job.status not in CLAIMABLE_STATUSES or job.next_run_at > now:
        session.flush()
        return None

    job.status = AgentJobStatus.LEASED
    job.attempts += 1
    job.lease_owner = clean_worker
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
    if not reclaimed:
        job.error = None
        job.error_class = None
        job.last_error = None
    session.flush()
    job.__dict__["_reclaimed"] = reclaimed
    return job


def lease_was_reclaimed(job: AgentJob) -> bool:
    """Return a durable-or-in-memory signal that this lease replaced an abandoned one."""

    return bool(job.__dict__.get("_reclaimed")) or job.error_class == "lease_expired"


def start_job(
    session: Session,
    job: AgentJob,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> AgentJob:
    if job.status is not AgentJobStatus.LEASED:
        raise AgentJobError("only a leased job can start")
    if job.lease_owner != worker_id:
        raise AgentJobError("job lease belongs to a different worker")
    now = now or _now()
    if job.lease_expires_at is not None and job.lease_expires_at <= now:
        raise AgentJobError("job lease expired before execution started")
    reclaimed = lease_was_reclaimed(job)
    job.status = AgentJobStatus.IN_PROGRESS
    job.started_at = now
    if not reclaimed:
        job.error = None
        job.error_class = None
        job.last_error = None
    session.flush()
    return job


def compute_backoff(attempts: int, *, base: float, cap: float) -> float:
    exp = min(base * (2 ** max(0, attempts - 1)), cap)
    jitter = random.uniform(0, exp * 0.25)  # noqa: S311 - queue jitter, not security
    return float(min(exp + jitter, cap * 1.25))


def schedule_retry(
    session: Session,
    job: AgentJob,
    *,
    error_class: str,
    reason: str,
    base_seconds: float,
    cap_seconds: float,
    error_detail: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> AgentJob:
    """Retry a transient failure, or terminate after the configured limit."""

    now = now or _now()
    job.error_class = error_class[:96]
    job.last_error = reason
    job.error = {
        "class": error_class,
        "message": reason,
        "retryable": job.attempts < job.max_attempts,
        "detail": error_detail or {},
    }
    job.lease_owner = None
    job.lease_expires_at = None
    if job.attempts >= job.max_attempts:
        job.status = AgentJobStatus.FAILED
        job.finished_at = now
        job.error["retryable"] = False
    else:
        delay = compute_backoff(job.attempts, base=base_seconds, cap=cap_seconds)
        job.status = AgentJobStatus.RETRY_SCHEDULED
        job.next_run_at = now + timedelta(seconds=delay)
    session.flush()
    return job


def mark_failed(
    session: Session,
    job: AgentJob,
    *,
    error_class: str,
    reason: str,
    retryable: bool = False,
    error_detail: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> AgentJob:
    if retryable and job.attempts < job.max_attempts:
        raise AgentJobError("use schedule_retry for a retryable failure")
    now = now or _now()
    job.status = AgentJobStatus.FAILED
    job.finished_at = now
    job.error_class = error_class[:96]
    job.last_error = reason
    job.error = {
        "class": error_class,
        "message": reason,
        "retryable": False,
        "detail": error_detail or {},
    }
    job.lease_owner = None
    job.lease_expires_at = None
    session.flush()
    return job


def mark_paused(
    session: Session,
    job: AgentJob,
    *,
    reason: str,
    reason_code: str = "operator_pause",
    error_detail: dict[str, Any] | None = None,
) -> AgentJob:
    if job.status in TERMINAL_STATUSES:
        raise AgentJobError("a terminal job cannot be paused")
    job.status = AgentJobStatus.PAUSED
    job.last_error = reason
    job.error_class = reason_code[:96]
    job.error = {
        "class": reason_code,
        "message": reason,
        "retryable": True,
        "detail": error_detail or {},
    }
    job.lease_owner = None
    job.lease_expires_at = None
    session.flush()
    return job


def resume_paused(
    session: Session,
    job: AgentJob,
    *,
    reason_codes: frozenset[str],
    now: datetime | None = None,
) -> AgentJob:
    """Make one specifically classified paused job claimable again.

    Callers must name the pause classifications they own. This prevents a
    dependency wake-up from erasing an operator, membership, suppression, or
    unrelated domain pause.
    """

    if job.status is not AgentJobStatus.PAUSED:
        return job
    if job.error_class not in reason_codes:
        return job
    job.status = AgentJobStatus.PENDING
    job.next_run_at = now or _now()
    job.error = None
    job.error_class = None
    job.last_error = None
    job.finished_at = None
    session.flush()
    return job


def cancel_job(
    session: Session,
    job: AgentJob,
    *,
    reason: str,
    reason_code: str,
    now: datetime | None = None,
) -> AgentJob:
    """Cancel one non-terminal job through the shared Agent lifecycle."""

    if job.status in TERMINAL_STATUSES:
        return job
    now = now or _now()
    job.status = AgentJobStatus.CANCELLED
    job.last_error = reason
    job.error_class = reason_code[:96]
    job.error = {
        "class": reason_code,
        "message": reason,
        "retryable": False,
    }
    job.finished_at = now
    job.lease_owner = None
    job.lease_expires_at = None
    session.flush()
    return job


def mark_completed(
    session: Session,
    job: AgentJob,
    *,
    result: dict[str, Any],
    outcome_committed: bool,
    now: datetime | None = None,
) -> AgentJob:
    """Complete only when the handler has staged a real domain outcome.

    The domain mutations and this status update must be committed by the same
    caller-owned transaction. A process exit without that commit completes
    neither.
    """

    if not outcome_committed:
        raise AgentJobError("a job cannot complete without a committed domain outcome")
    if job.status is not AgentJobStatus.IN_PROGRESS:
        raise AgentJobError("only a running job can complete")
    clean_result = _input(result)
    now = now or _now()
    job.status = AgentJobStatus.SUCCEEDED
    job.result = clean_result
    job.error = None
    job.error_class = None
    job.last_error = None
    job.finished_at = now
    job.lease_owner = None
    job.lease_expires_at = None
    session.flush()
    return job


def recover_expired_leases(
    session: Session,
    *,
    now: datetime | None = None,
    agent_ids: Iterable[AgentIdentifier] | None = None,
) -> list[AgentJob]:
    """Make abandoned work resumable, respecting exhausted attempt limits."""

    now = now or _now()
    statement = select(AgentJob).where(
        AgentJob.status.in_(LEASED_STATUSES),
        AgentJob.lease_expires_at.is_not(None),
        AgentJob.lease_expires_at < now,
    )
    allowed = tuple(agent_ids or ())
    if allowed:
        statement = statement.where(AgentJob.agent_id.in_(allowed))
    jobs = list(session.scalars(statement.with_for_update(skip_locked=True)).all())
    for job in jobs:
        job.lease_owner = None
        job.lease_expires_at = None
        job.last_error = "reclaimed after worker lease expired"
        job.error_class = "lease_expired"
        job.error = {
            "class": "lease_expired",
            "message": job.last_error,
            "retryable": job.attempts < job.max_attempts,
        }
        if job.attempts >= job.max_attempts:
            job.status = AgentJobStatus.FAILED
            job.finished_at = now
        else:
            job.status = AgentJobStatus.PENDING
            job.next_run_at = now
    if jobs:
        session.flush()
    return jobs


def _jobs_for_membership(
    session: Session,
    *,
    campaign_contact_id: uuid.UUID,
    statuses: tuple[AgentJobStatus, ...],
) -> list[AgentJob]:
    return list(
        session.scalars(
            select(AgentJob)
            .where(
                AgentJob.campaign_contact_id == campaign_contact_id,
                AgentJob.status.in_(statuses),
            )
            .with_for_update()
        ).all()
    )


def pause_jobs_for_membership(
    session: Session,
    *,
    campaign_contact_id: uuid.UUID,
    reason: str,
    actor: str,
) -> list[AgentJob]:
    jobs = _jobs_for_membership(
        session,
        campaign_contact_id=campaign_contact_id,
        statuses=CLAIMABLE_STATUSES + LEASED_STATUSES,
    )
    for job in jobs:
        job.status = AgentJobStatus.PAUSED
        job.last_error = reason
        job.error_class = "membership_paused"
        job.error = {
            "class": "membership_paused",
            "message": reason,
            "retryable": True,
        }
        job.lease_owner = None
        job.lease_expires_at = None
    if jobs:
        session.flush()
        record_audit_event(
            session,
            actor=actor,
            action="agent_job.membership_paused",
            entity_type="campaign_contact",
            entity_id=str(campaign_contact_id),
            new_state="paused",
            reason=reason,
            context={"job_ids": [str(job.id) for job in jobs]},
        )
    return jobs


def resume_jobs_for_membership(
    session: Session,
    *,
    campaign_contact_id: uuid.UUID,
    actor: str,
) -> list[AgentJob]:
    locked = _jobs_for_membership(
        session,
        campaign_contact_id=campaign_contact_id,
        statuses=(AgentJobStatus.PAUSED,),
    )
    jobs = [job for job in locked if job.error_class == "membership_paused"]
    now = _now()
    for job in jobs:
        job.status = AgentJobStatus.PENDING
        job.next_run_at = now
        job.last_error = None
        job.error_class = None
        job.error = None
    if jobs:
        session.flush()
        record_audit_event(
            session,
            actor=actor,
            action="agent_job.membership_resumed",
            entity_type="campaign_contact",
            entity_id=str(campaign_contact_id),
            new_state="queued",
            reason="Campaign Contact resumed",
            context={"job_ids": [str(job.id) for job in jobs]},
        )
    return jobs


def cancel_jobs_for_membership(
    session: Session,
    *,
    campaign_contact_id: uuid.UUID,
    reason: str,
    actor: str,
) -> list[AgentJob]:
    jobs = _jobs_for_membership(
        session,
        campaign_contact_id=campaign_contact_id,
        statuses=CLAIMABLE_STATUSES + LEASED_STATUSES + (AgentJobStatus.PAUSED,),
    )
    now = _now()
    for job in jobs:
        job.status = AgentJobStatus.CANCELLED
        job.last_error = reason
        job.finished_at = now
        job.lease_owner = None
        job.lease_expires_at = None
    if jobs:
        session.flush()
        record_audit_event(
            session,
            actor=actor,
            action="agent_job.membership_cancelled",
            entity_type="campaign_contact",
            entity_id=str(campaign_contact_id),
            new_state="cancelled",
            reason=reason,
            context={"job_ids": [str(job.id) for job in jobs]},
        )
    return jobs


def cancel_jobs_for_stage(
    session: Session,
    *,
    campaign_contact_id: uuid.UUID,
    agent_id: AgentIdentifier,
    reason: str,
    actor: str,
) -> list[AgentJob]:
    """Cancel non-terminal work for one deliberately skipped Agent stage."""

    stage_jobs = list(
        session.scalars(
            select(AgentJob)
            .where(
                AgentJob.campaign_contact_id == campaign_contact_id,
                AgentJob.agent_id == agent_id,
                AgentJob.status.in_(
                    CLAIMABLE_STATUSES + LEASED_STATUSES + (AgentJobStatus.PAUSED,)
                ),
            )
            .with_for_update()
        ).all()
    )
    now = _now()
    for job in stage_jobs:
        job.status = AgentJobStatus.CANCELLED
        job.last_error = reason
        job.error_class = "operator_skip"
        job.error = {
            "class": "operator_skip",
            "message": reason,
            "retryable": False,
        }
        job.finished_at = now
        job.lease_owner = None
        job.lease_expires_at = None
    if stage_jobs:
        session.flush()
        record_audit_event(
            session,
            actor=actor,
            action="agent_job.stage_cancelled",
            entity_type="campaign_contact",
            entity_id=str(campaign_contact_id),
            new_state="cancelled",
            reason=reason,
            context={
                "agent_id": agent_id.value,
                "job_ids": [str(job.id) for job in stage_jobs],
            },
        )
    return stage_jobs


def retry_failed_job(
    session: Session,
    *,
    job_id: uuid.UUID,
    actor: str = "operator",
    now: datetime | None = None,
) -> AgentJob:
    job = session.get(AgentJob, job_id)
    if job is None:
        raise AgentJobNotFound(f"job {job_id} does not exist")
    if job.status is not AgentJobStatus.FAILED:
        raise AgentJobError("only a failed job can be retried")
    if not bool((job.error or {}).get("retryable", False)):
        raise AgentJobError("the job has a terminal failure and cannot be retried")
    if job.attempts >= job.max_attempts:
        raise AgentJobError("job exhausted its retry limit")
    job.status = AgentJobStatus.PENDING
    job.next_run_at = now or _now()
    job.finished_at = None
    session.flush()
    record_audit_event(
        session,
        actor=actor,
        action="agent_job.operator_retry",
        entity_type="agent_job",
        entity_id=str(job.id),
        previous_state="failed",
        new_state="queued",
        reason="operator retried failed job",
    )
    return job

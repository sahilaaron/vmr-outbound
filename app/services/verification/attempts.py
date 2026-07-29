"""Recording and reading provider-facing verification attempts (MVP-01E).

The Phase 2 Agent Job knows how many attempts a job has made and how it ended.
This module records what the *provider* did on each one: which implementation
ran, whether a request was actually sent, whether the answer was reused, the
normalized outcome, and how a failure classifies.

Nothing here schedules, retries, or transitions a job. It writes history.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.enums import EmailVerificationResult, VerificationFailureClass
from app.models.verification_attempt import VerificationAttempt
from app.models.verification_job import AgentJob
from app.services.verification.provider import redact_secret

# The only failure class another attempt could plausibly fix. The Phase 2 queue
# decides whether a retry actually happens; this states whether one would help.
RETRYABLE_FAILURE_CLASSES: frozenset[VerificationFailureClass] = frozenset(
    {VerificationFailureClass.TRANSIENT_PROVIDER}
)


def is_retryable(failure_class: VerificationFailureClass) -> bool:
    """Whether *failure_class* is the kind of failure another attempt may fix."""

    return failure_class in RETRYABLE_FAILURE_CLASSES


def record_attempt(
    session: Session,
    job: AgentJob,
    *,
    started_at: datetime,
    finished_at: datetime,
    provider: str,
    provider_called: bool,
    failure_class: VerificationFailureClass,
    precise_status: str | None = None,
    verification_result: EmailVerificationResult | None = None,
    reused_evidence: bool = False,
    error_summary: str | None = None,
    verification_id: uuid.UUID | None = None,
    attempt_number: int | None = None,
    settings: Settings | None = None,
) -> VerificationAttempt:
    """Append one provider-facing attempt record for *job*.

    ``attempt_number`` defaults to the next free number. The Agent Job's counter
    is preferred, since the common queue increments it at claim time and it is
    what an operator sees, but it is not trusted blindly: the live smoke path
    invokes the domain directly, which can leave the counter behind the number of
    rows already written. Taking whichever is higher keeps history monotonic
    instead of colliding with the ``(job_id, attempt_number)`` constraint and
    failing a whole worker pass.

    ``error_summary`` is redacted before storage. Provider text is already
    redacted upstream, but this column is rendered to operators, so it does not
    rely on that alone.
    """

    settings = settings or get_settings()
    attempt = VerificationAttempt(
        job_id=job.id,
        attempt_number=(
            attempt_number if attempt_number is not None else _next_attempt_number(session, job)
        ),
        started_at=started_at,
        finished_at=finished_at,
        provider=provider,
        provider_called=provider_called,
        reused_evidence=reused_evidence,
        precise_status=precise_status,
        verification_result=verification_result,
        failure_class=failure_class,
        error_summary=(
            redact_secret(error_summary, settings.millionverifier_api_key)
            if error_summary
            else None
        ),
        verification_id=verification_id,
    )
    session.add(attempt)
    session.flush()
    return attempt


def _next_attempt_number(session: Session, job: AgentJob) -> int:
    """The next free attempt number for *job*: its counter, or past the last row."""

    highest = (
        session.scalar(
            select(func.max(VerificationAttempt.attempt_number)).where(
                VerificationAttempt.job_id == job.id
            )
        )
        or 0
    )
    return max(job.attempts, highest + 1, 1)


def attempts_for_job(session: Session, job_id: uuid.UUID) -> list[VerificationAttempt]:
    """Every recorded attempt for *job_id*, oldest first."""

    return list(
        session.scalars(
            select(VerificationAttempt)
            .where(VerificationAttempt.job_id == job_id)
            .order_by(
                VerificationAttempt.attempt_number.asc(),
                VerificationAttempt.started_at.asc(),
            )
        ).all()
    )


def latest_attempt(session: Session, job_id: uuid.UUID) -> VerificationAttempt | None:
    """The most recent recorded attempt for *job_id*, if any."""

    return session.scalars(
        select(VerificationAttempt)
        .where(VerificationAttempt.job_id == job_id)
        .order_by(
            VerificationAttempt.attempt_number.desc(),
            VerificationAttempt.started_at.desc(),
        )
        .limit(1)
    ).first()

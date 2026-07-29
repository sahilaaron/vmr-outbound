"""Per-attempt provider evidence for exact-address verification (MVP-01E).

Deliberately **not** a second job-attempt lifecycle. The Phase 2 Agent Job owns
execution state — attempt counts, leases, retry scheduling, terminal status — and
``pipeline_events`` already preserves when each attempt was queued, leased,
started, retried and how it ended. None of that is repeated here.

What Phase 2 cannot answer, because it is verification-specific, is what the
*provider* did on each try:

* which provider implementation ran, live or simulated;
* whether a request actually reached the provider at all, which is the only
  honest basis for reconciling a paid-call count;
* whether the answer came from reused evidence instead;
* the normalized address outcome that attempt observed;
* how a provider failure classifies, stored so a later policy change cannot
  relabel history;
* which authoritative evidence row the attempt produced or reused.

Rows are append-only and never rewritten once the attempt finishes. The
authoritative verdict about a mailbox stays in :class:`ExactEmailVerification`;
``verification_result`` here is what *this attempt observed*, kept so the row
stays readable after the evidence it referenced has been superseded, and never
read as evidence in its own right.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import EmailVerificationResult, VerificationFailureClass


class VerificationAttempt(Base):
    """One provider-facing attempt at one Agent Job's exact address."""

    __tablename__ = "verification_attempts"
    __table_args__ = (
        # One row per (job, attempt number). Re-recording the same attempt is a
        # bug, and the database says so rather than duplicating history.
        UniqueConstraint(
            "job_id", "attempt_number", name="uq_verification_attempts_job_id_attempt_number"
        ),
        Index("ix_verification_attempts_job_id", "job_id"),
        Index("ix_verification_attempts_started_at", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # The Phase 2 Agent Job this attempt belongs to. The physical table behind
    # AgentJob is still `verification_jobs`.
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "verification_jobs.id",
            name="fk_verification_attempts_job_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    # Correlates this row with the Agent Job's own attempt counter, which the
    # common queue increments at claim time. Stored only as the correlation key;
    # the authoritative count remains ``AgentJob.attempts``.
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # When the provider interaction began and ended — the duration of the call,
    # not the lifetime of the job.
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Which provider implementation ran, carrying the simulated-vs-live label so a
    # simulated attempt is never read as an external verification (VER-007).
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    # Whether the adapter invoked the provider at all. False for reused evidence
    # and for anything refused before invocation — the single most important
    # field for explaining a paid-call count.
    provider_called: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # True when this attempt answered from sufficiently fresh existing evidence.
    reused_evidence: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # The normalized precise status this attempt reached (an EmailPreciseStatus
    # value). Free-form String rather than a native enum so the precise-status set
    # can grow without a migration over history rows.
    precise_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # The address-level result this attempt observed, when it observed one. None
    # for every operational outcome — a provider error is not mailbox evidence.
    verification_result: Mapped[EmailVerificationResult | None] = mapped_column(
        Enum(EmailVerificationResult, name="email_verification_result"),
        nullable=True,
    )
    failure_class: Mapped[VerificationFailureClass] = mapped_column(
        Enum(VerificationFailureClass, name="verification_failure_class"),
        nullable=False,
        default=VerificationFailureClass.NONE,
    )
    # Operator-readable, redacted before storage. A provider credential must never
    # reach this column.
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The authoritative evidence row this attempt produced or reused, when any.
    # SET NULL rather than CASCADE: losing the evidence must not erase the fact
    # that an attempt happened.
    verification_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        # Named explicitly: the naming convention would generate an identifier
        # past PostgreSQL's 63-character limit for these two table names.
        ForeignKey(
            "exact_email_verifications.id",
            name="fk_verification_attempts_verification_id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"VerificationAttempt(job_id={self.job_id!r}, "
            f"attempt_number={self.attempt_number!r}, "
            f"failure_class={self.failure_class.value!r})"
        )

"""Durable audit rows for policy-bounded Email Agent candidate attempts."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class EmailCandidateAttemptStatus(enum.StrEnum):
    """Lifecycle of one allowed candidate inside one Email Agent execution."""

    PENDING = "pending"
    VERIFICATION_QUEUED = "verification_queued"
    WAITING = "waiting"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    RETRYABLE = "retryable"
    TERMINAL_NO_RESULT = "terminal_no_result"
    REFUSED = "refused"
    SIMULATED = "simulated"


_ATTEMPT_STATUS_VALUES = tuple(status.value for status in EmailCandidateAttemptStatus)
_ATTEMPT_STATUS_SQL = ", ".join(f"'{value}'" for value in _ATTEMPT_STATUS_VALUES)


class EmailCandidateAttempt(Base):
    """One exact candidate submitted to one child Verification Agent job.

    The existing :class:`EmailCandidate` remains the candidate record.  This row
    records the missing execution facts: which policy execution attempted it,
    its locked position, the one child job, and the authoritative evidence or
    refusal returned by that child.
    """

    __tablename__ = "email_candidate_attempts"
    __table_args__ = (
        UniqueConstraint(
            "email_job_id",
            "candidate_index",
            name="uq_email_candidate_attempts_job_index",
        ),
        UniqueConstraint(
            "email_job_id",
            "normalized_email",
            name="uq_email_candidate_attempts_job_email",
        ),
        UniqueConstraint(
            "verification_job_id",
            name="uq_email_candidate_attempts_verification_job",
        ),
        CheckConstraint(
            "candidate_index >= 0 AND candidate_index < 24",
            name="candidate_index_bounded",
        ),
        CheckConstraint(
            f"status IN ({_ATTEMPT_STATUS_SQL})",
            name="status_known",
        ),
        CheckConstraint(
            "employee_count_class IN ('more_than_50', '50_or_fewer', 'unknown')",
            name="employee_count_class_known",
        ),
        Index(
            "uq_email_candidate_attempts_one_accepted",
            "email_job_id",
            unique=True,
            postgresql_where=text("status = 'accepted'"),
        ),
        Index("ix_email_candidate_attempts_contact", "contact_id", "created_at"),
        Index("ix_email_candidate_attempts_email_job", "email_job_id", "candidate_index"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("verification_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("email_candidates.id", ondelete="RESTRICT"),
        nullable=False,
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="SET NULL"),
        nullable=True,
    )
    campaign_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "campaign_contacts.id",
            name="fk_email_attempts_campaign_contact",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    candidate_index: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_format: Mapped[str] = mapped_column(String(32), nullable=False)
    normalized_email: Mapped[str] = mapped_column(String(320), nullable=False)
    normalized_domain: Mapped[str] = mapped_column(String(255), nullable=False)

    policy_identifier: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(50), nullable=False)
    # Legacy provenance only. EV-001 no longer requires or reads size evidence
    # when choosing candidates; historical rows remain readable.
    employee_count_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    employee_evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "company_field_values.id",
            name="fk_email_attempts_employee_evidence",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    employee_evidence_reference: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    employee_evidence_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    employee_evidence_freshness: Mapped[str | None] = mapped_column(String(32), nullable=True)

    force_refresh: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    refresh_scope: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=EmailCandidateAttemptStatus.PENDING.value,
        server_default=EmailCandidateAttemptStatus.PENDING.value,
    )
    verification_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "verification_jobs.id",
            name="fk_email_attempts_verification_job",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    verification_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "exact_email_verifications.id",
            name="fk_email_attempts_verification_evidence",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    verification_decision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verification_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    refusal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    verification_queued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            "EmailCandidateAttempt("
            f"id={self.id!r}, email_job_id={self.email_job_id!r}, "
            f"candidate_index={self.candidate_index!r}, status={self.status!r})"
        )

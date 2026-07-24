"""PostgreSQL-backed exact-address verification job (VER-005 / OPS-001).

One job = one intent to obtain fresh evidence for one exact normalized address.
The queue lives in Postgres (no external broker) so background work is durable,
resumable, and observable with the rest of the system of record.

Two database invariants make duplicate paid calls structurally impossible:

* ``idempotency_key`` is unique. It is ``{policy_version}:{email}`` so a burst of
  concurrent enqueue attempts for the same address under the same policy collapse
  to a single row (``ON CONFLICT DO NOTHING``).
* ``uq_verification_jobs_active_email`` is a partial unique index over ``email``
  restricted to the *active* statuses, so an address can have at most one job in
  flight at a time regardless of policy version.

Recovery: a claimed job carries a ``lease_owner`` and ``lease_expires_at``. A
worker that dies mid-flight leaves the lease to expire; a later sweep reclaims the
job (its attempt was already counted, so a partial call cannot be double-charged
without evidence — the worker writes evidence before releasing the lease).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
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
from app.models.enums import VerificationJobStatus

# Statuses in which a job is considered "in flight" and holds the single active
# slot for its address. Kept here so the model, the partial index predicate, and
# the queue service agree on one definition.
ACTIVE_JOB_STATUSES: tuple[VerificationJobStatus, ...] = (
    VerificationJobStatus.PENDING,
    VerificationJobStatus.IN_PROGRESS,
    VerificationJobStatus.RETRY_SCHEDULED,
)
# The native enum stores member *names* (uppercase) as its labels, matching the
# repo's existing enums; the predicate compares against those, cast to the enum
# type so the expression is IMMUTABLE (required for a partial index predicate).
_ACTIVE_SQL = (
    "status IN ("
    "'PENDING'::verification_job_status,"
    "'IN_PROGRESS'::verification_job_status,"
    "'RETRY_SCHEDULED'::verification_job_status)"
)


class VerificationJob(Base):
    """A durable, idempotent unit of exact-address verification work."""

    __tablename__ = "verification_jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_verification_jobs_idempotency_key"),
        # At most one active job per exact address (prevents duplicate paid calls).
        Index(
            "uq_verification_jobs_active_email",
            "email",
            unique=True,
            postgresql_where=_ACTIVE_SQL,
        ),
        # The claim query orders claimable work by next_run_at; index it.
        Index("ix_verification_jobs_claimable", "status", "next_run_at"),
        Index("ix_verification_jobs_contact_id", "contact_id"),
        Index("ix_verification_jobs_email", "email"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # The exact normalized address to verify.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    # Optional link to the contact whose selected candidate this is.
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(400), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[VerificationJobStatus] = mapped_column(
        Enum(VerificationJobStatus, name="verification_job_status"),
        nullable=False,
        default=VerificationJobStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    # When the job becomes claimable (now for a fresh job, later for a retry).
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Lease held by the worker currently processing the job.
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The precise status of the job's terminal outcome (an EmailPreciseStatus
    # value). For a definite address result the evidence row is the source of
    # truth; for an operational failure that produces no address evidence
    # (insufficient credits, provider/config error) this is how the condition is
    # surfaced beside the email until an operator resolves it.
    outcome_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Set when the job produced address evidence, for traceability.
    verification_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("exact_email_verifications.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"VerificationJob(email={self.email!r}, status={self.status.value!r}, "
            f"attempts={self.attempts!r})"
        )

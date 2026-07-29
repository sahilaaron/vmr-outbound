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

Recovery: a claimed job carries a ``lease_owner`` and ``lease_expires_at``. The
production worker commits lease and Running checkpoints before it stages the
domain outcome. A process that dies therefore leaves recoverable work. External
providers remain responsible for honoring any provider-side idempotency key;
database rollback cannot undo a remote side effect.
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import AgentIdentifier, AgentJobStatus

# Statuses in which a job is considered "in flight" and holds the single active
# slot for its address. Kept here so the model, the partial index predicate, and
# the queue service agree on one definition.
ACTIVE_JOB_STATUSES: tuple[AgentJobStatus, ...] = (
    AgentJobStatus.PENDING,
    AgentJobStatus.LEASED,
    AgentJobStatus.IN_PROGRESS,
    AgentJobStatus.RETRY_SCHEDULED,
)
# The native enum stores member *names* (uppercase) as its labels, matching the
# repo's existing enums; the predicate compares against those, cast to the enum
# type so the expression is IMMUTABLE (required for a partial index predicate).
_ACTIVE_SQL = (
    "status IN ("
    "'PENDING'::verification_job_status,"
    "'LEASED'::verification_job_status,"
    "'IN_PROGRESS'::verification_job_status,"
    "'RETRY_SCHEDULED'::verification_job_status)"
)


class AgentJob(Base):
    """A durable, idempotent unit of work for any registered Agent.

    The physical table name is retained from the proven verification queue so
    existing jobs, usage-ledger foreign keys, and deployments migrate
    additively. ``VerificationJob`` remains an import alias below.
    """

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
        Index("ix_verification_jobs_claimable", "status", "priority", "next_run_at"),
        Index("ix_verification_jobs_agent_status", "agent_id", "status"),
        Index("ix_verification_jobs_campaign_contact_id", "campaign_contact_id"),
        Index("ix_verification_jobs_contact_id", "contact_id"),
        Index("ix_verification_jobs_email", "email"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[AgentIdentifier] = mapped_column(
        Enum(AgentIdentifier, name="agent_identifier"),
        nullable=False,
        default=AgentIdentifier.VERIFICATION,
        server_default=AgentIdentifier.VERIFICATION.name,
    )
    task_kind: Mapped[str] = mapped_column(
        String(96),
        nullable=False,
        default="verify_exact_email",
        server_default="verify_exact_email",
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default="100"
    )
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # The exact normalized address to verify.
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    # Optional link to the contact whose selected candidate this is.
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Optional campaign context, carried so each usage ledger entry can attribute
    # the request (and its cost) to a campaign / batch.
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="SET NULL"),
        nullable=True,
    )
    campaign_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaign_contacts.id", ondelete="SET NULL"),
        nullable=True,
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="SET NULL"),
        nullable=True,
    )
    capture_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("linkedin_profile_snapshots.id", ondelete="SET NULL"),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(400), nullable=False)
    policy_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[AgentJobStatus] = mapped_column(
        Enum(AgentJobStatus, name="verification_job_status"),
        nullable=False,
        default=AgentJobStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    # When the job becomes claimable (now for a fresh job, later for a retry).
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    input_reference: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    result: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(96), nullable=True)
    parent_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("verification_jobs.id", ondelete="SET NULL"),
        nullable=True,
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
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"AgentJob(agent={self.agent_id.value!r}, status={self.status.value!r}, "
            f"entity={self.entity_type!r}:{self.entity_id!r})"
        )


# Existing verification services and tests keep their import contract while
# operating on the common durable job model.
VerificationJob = AgentJob

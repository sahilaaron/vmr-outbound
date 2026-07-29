"""MVP-01E verification attempts: provider-facing evidence per Agent Job attempt.

Revision ID: b9d4e7a15c38
Revises: 8f0a3d6c2b91
Create Date: 2026-07-29

One narrow table, and deliberately nothing else.

The standalone Verification Agent was built before the Phase 2 backbone existed,
so it carried its own Agent metadata on the job row: an Agent type, a contract
version, an opaque requesting-job string, and a force-refresh flag. Phase 2 now
supplies all of that properly — ``agent_id``, the registry, a real
``parent_job_id`` and ``campaign_contact_id`` relationship, and a structured
``input_reference`` — so none of those columns are added here. The force-refresh
instruction lives in ``input_reference`` where the common Agent Job already
provides a durable, structured home for a job's input.

What Phase 2 genuinely does not represent is what the *provider* did on each
attempt. ``AgentJob.attempts`` counts tries and ``pipeline_events`` records when
each was queued, leased, started and how it ended, but neither can say which
provider implementation ran, whether a request actually reached the provider,
whether the answer was reused from cache instead, or how a provider failure
classifies. Those are the facts a paid-call reconciliation and a truthful
simulated-versus-live provenance claim depend on, so they are stored per attempt.

``failure_class`` is stored rather than derived so a later change to the retry
policy cannot reach back and relabel a historical failure as retryable. It is a
verification-domain classification, distinct from the Agent Job's
orchestration-visible ``error_class``: the domain says what the provider did, and
the Agent adapter translates that into the shared contract exactly once.

This is not a second verification truth store. The authoritative normalized
answer about a mailbox remains one row in ``exact_email_verifications``;
``verification_id`` points at it, and ``verification_result`` records only what
this attempt observed so the row stays readable after that evidence is
superseded.

No backfill. Jobs that ran before this table existed have no provider-attempt
history to reconstruct, and inventing one would be fabricating cost evidence.

Downgrade refuses while attempt rows exist, matching the Phase 2 backbone's own
convention of refusing before a destructive change rather than silently
discarding operator-visible evidence. An empty schema round-trips cleanly.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b9d4e7a15c38"
down_revision: str | Sequence[str] | None = "8f0a3d6c2b91"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


VERIFICATION_FAILURE_CLASS = "verification_failure_class"
VERIFICATION_FAILURE_CLASS_VALUES = (
    "NONE",
    "INVALID_INPUT",
    "POLICY_REFUSAL",
    "TRANSIENT_PROVIDER",
    "PERMANENT_PROVIDER",
    "INSUFFICIENT_CREDITS",
)


def upgrade() -> None:
    op.create_table(
        "verification_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("provider_called", sa.Boolean(), nullable=False),
        sa.Column("reused_evidence", sa.Boolean(), nullable=False),
        sa.Column("precise_status", sa.String(length=50), nullable=True),
        # ``email_verification_result`` already exists (Phase 2 email verification).
        # ``postgresql.ENUM(create_type=False)`` references it instead of trying
        # to create it a second time.
        sa.Column(
            "verification_result",
            postgresql.ENUM(
                "VALID",
                "INVALID",
                "CATCH_ALL",
                "UNKNOWN",
                "DISPOSABLE",
                name="email_verification_result",
                create_type=False,
            ),
            nullable=True,
        ),
        # Created implicitly by this table and dropped explicitly on downgrade —
        # the convention the existing verification migrations established.
        sa.Column(
            "failure_class",
            sa.Enum(*VERIFICATION_FAILURE_CLASS_VALUES, name=VERIFICATION_FAILURE_CLASS),
            nullable=False,
        ),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("verification_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_verification_attempts"),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["verification_jobs.id"],
            name="fk_verification_attempts_job_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["verification_id"],
            ["exact_email_verifications.id"],
            name="fk_verification_attempts_verification_id",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "job_id",
            "attempt_number",
            name="uq_verification_attempts_job_id_attempt_number",
        ),
    )
    op.create_index("ix_verification_attempts_job_id", "verification_attempts", ["job_id"])
    op.create_index("ix_verification_attempts_started_at", "verification_attempts", ["started_at"])


def downgrade() -> None:
    bind = op.get_bind()
    recorded = bind.execute(sa.text("SELECT count(*) FROM verification_attempts")).scalar_one()
    if recorded:
        raise RuntimeError(
            f"Refusing to downgrade: {recorded} verification attempt record(s) exist. "
            "These record which attempts reached the provider and what they cost, and "
            "nothing left behind can recompute them. Export or delete these rows "
            "deliberately first."
        )

    op.drop_index("ix_verification_attempts_started_at", table_name="verification_attempts")
    op.drop_index("ix_verification_attempts_job_id", table_name="verification_attempts")
    op.drop_table("verification_attempts")
    sa.Enum(name=VERIFICATION_FAILURE_CLASS).drop(bind, checkfirst=True)

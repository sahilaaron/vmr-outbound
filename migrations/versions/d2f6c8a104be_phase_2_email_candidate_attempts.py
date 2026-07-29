"""Phase 2 Email Agent candidate-attempt history.

Revision ID: d2f6c8a104be
Revises: b9d4e7a15c38
Create Date: 2026-07-29

The shared Agent Job remains the only queue and lifecycle record. This table
adds only Email-specific durable facts that the generic job cannot represent:
the locked candidate position and format, employee-count policy evidence, the
one requesting relationship to a Verification child, and the committed
Verification decision/evidence for that candidate.

Downgrade refuses while history exists. Candidate attempts are operator-visible
provenance and cannot be reconstructed from the surviving Contact email.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d2f6c8a104be"
down_revision: str | Sequence[str] | None = "b9d4e7a15c38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "email_candidate_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("campaign_contact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("candidate_index", sa.Integer(), nullable=False),
        sa.Column("candidate_format", sa.String(length=32), nullable=False),
        sa.Column("normalized_email", sa.String(length=320), nullable=False),
        sa.Column("normalized_domain", sa.String(length=255), nullable=False),
        sa.Column("policy_identifier", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=50), nullable=False),
        sa.Column("employee_count_class", sa.String(length=32), nullable=False),
        sa.Column("employee_evidence_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("employee_evidence_reference", sa.String(length=1024), nullable=True),
        sa.Column("employee_evidence_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("employee_evidence_freshness", sa.String(length=32), nullable=False),
        sa.Column(
            "force_refresh",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("refresh_scope", sa.String(length=128), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("verification_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("verification_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("verification_decision", sa.String(length=64), nullable=True),
        sa.Column("verification_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("refusal_reason", sa.Text(), nullable=True),
        sa.Column("verification_queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "candidate_index >= 0 AND candidate_index < 3",
            name="ck_email_candidate_attempts_candidate_index_bounded",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'verification_queued', 'waiting', 'accepted', "
            "'rejected', 'retryable', 'terminal_no_result', 'refused', 'simulated')",
            name="ck_email_candidate_attempts_status_known",
        ),
        sa.CheckConstraint(
            "employee_count_class IN ('more_than_50', '50_or_fewer', 'unknown')",
            name="ck_email_candidate_attempts_employee_count_class_known",
        ),
        sa.ForeignKeyConstraint(
            ["email_job_id"],
            ["verification_jobs.id"],
            name="fk_email_candidate_attempts_email_job_id_verification_jobs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["email_candidates.id"],
            name="fk_email_candidate_attempts_candidate_id_email_candidates",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name="fk_email_candidate_attempts_contact_id_contacts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_email_candidate_attempts_company_id_companies",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name="fk_email_candidate_attempts_campaign_id_campaigns",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_contact_id"],
            ["campaign_contacts.id"],
            name="fk_email_attempts_campaign_contact",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["employee_evidence_id"],
            ["company_field_values.id"],
            name="fk_email_attempts_employee_evidence",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["verification_job_id"],
            ["verification_jobs.id"],
            name="fk_email_attempts_verification_job",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["verification_id"],
            ["exact_email_verifications.id"],
            name="fk_email_attempts_verification_evidence",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_email_candidate_attempts"),
        sa.UniqueConstraint(
            "email_job_id",
            "candidate_index",
            name="uq_email_candidate_attempts_job_index",
        ),
        sa.UniqueConstraint(
            "email_job_id",
            "normalized_email",
            name="uq_email_candidate_attempts_job_email",
        ),
        sa.UniqueConstraint(
            "verification_job_id",
            name="uq_email_candidate_attempts_verification_job",
        ),
    )
    op.create_index(
        "ix_email_candidate_attempts_contact",
        "email_candidate_attempts",
        ["contact_id", "created_at"],
    )
    op.create_index(
        "ix_email_candidate_attempts_email_job",
        "email_candidate_attempts",
        ["email_job_id", "candidate_index"],
    )
    op.create_index(
        "uq_email_candidate_attempts_one_accepted",
        "email_candidate_attempts",
        ["email_job_id"],
        unique=True,
        postgresql_where=sa.text("status = 'accepted'"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    recorded = bind.execute(sa.text("SELECT count(*) FROM email_candidate_attempts")).scalar_one()
    if recorded:
        raise RuntimeError(
            f"Refusing to downgrade: {recorded} Email candidate attempt record(s) "
            "exist. Export or delete this irreconstructable policy and Verification "
            "history deliberately first."
        )

    op.drop_index(
        "uq_email_candidate_attempts_one_accepted",
        table_name="email_candidate_attempts",
    )
    op.drop_index(
        "ix_email_candidate_attempts_email_job",
        table_name="email_candidate_attempts",
    )
    op.drop_index(
        "ix_email_candidate_attempts_contact",
        table_name="email_candidate_attempts",
    )
    op.drop_table("email_candidate_attempts")

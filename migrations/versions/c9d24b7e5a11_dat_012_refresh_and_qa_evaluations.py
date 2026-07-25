"""DAT-012E/F snapshot reconciliation columns and QA evaluations

Revision ID: c9d24b7e5a11
Revises: f41c76a9d2e0
Create Date: 2026-07-24 16:00:00.000000

Adds the reconciliation bookkeeping on profile snapshots (reconciled_at,
review-only weak-match candidates, per-field refresh summary) and the
append-only ``contact_qa_evaluations`` table for the versioned employment QA
policy. No contact, import, or suppression table changes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c9d24b7e5a11"
down_revision: str | Sequence[str] | None = "f41c76a9d2e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Managed explicitly (create_type=False) so create_table never emits CREATE
# TYPE; created/dropped by hand around the table. Members stored by NAME.
_qa_outcome = postgresql.ENUM(
    "LIVE_CONTACT",
    "LEFT_COMPANY",
    "TITLE_CHANGED",
    "COMPANY_UNRESOLVED",
    "MULTIPLE_CURRENT_ROLES",
    "EXPERIENCE_MISSING",
    "EXPERIENCE_UNRECOGNIZED",
    "TENURE_REVIEW",
    "NON_FULL_TIME_REVIEW",
    "OPEN_TO_WORK_REVIEW",
    "LOW_CONNECTIONS_REVIEW",
    "INSUFFICIENT_EVIDENCE",
    "NEEDS_REVIEW",
    name="qa_outcome",
    create_type=False,
)


def upgrade() -> None:
    """Add reconciliation columns and the QA-evaluation table."""
    op.add_column(
        "linkedin_profile_snapshots",
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "linkedin_profile_snapshots",
        sa.Column("review_candidates", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "linkedin_profile_snapshots",
        sa.Column("refresh_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    _qa_outcome.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "contact_qa_evaluations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("policy_name", sa.String(length=128), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column(
            "evaluated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("contact_id", sa.UUID(), nullable=True),
        sa.Column("snapshot_id", sa.UUID(), nullable=True),
        sa.Column("contact_expectation", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("evidence_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("outcome", _qa_outcome, nullable=False),
        sa.Column("reason_codes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("signals", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("recommended_action", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["linkedin_profile_snapshots.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_contact_qa_evaluations_contact_id", "contact_qa_evaluations", ["contact_id"]
    )
    op.create_index(
        "ix_contact_qa_evaluations_snapshot_id", "contact_qa_evaluations", ["snapshot_id"]
    )


def downgrade() -> None:
    """Drop the QA table/enum and the reconciliation columns."""
    op.drop_index("ix_contact_qa_evaluations_snapshot_id", table_name="contact_qa_evaluations")
    op.drop_index("ix_contact_qa_evaluations_contact_id", table_name="contact_qa_evaluations")
    op.drop_table("contact_qa_evaluations")
    _qa_outcome.drop(op.get_bind(), checkfirst=True)
    op.drop_column("linkedin_profile_snapshots", "refresh_summary")
    op.drop_column("linkedin_profile_snapshots", "review_candidates")
    op.drop_column("linkedin_profile_snapshots", "reconciled_at")

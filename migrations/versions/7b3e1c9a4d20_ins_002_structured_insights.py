"""INS-002 typed Insight lineage and structured derivations.

Revision ID: 7b3e1c9a4d20
Revises: f2a91d7c4e60
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "7b3e1c9a4d20"
down_revision = "f2a91d7c4e60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical claims are deliberately not classified or backfilled.  Their
    # missing execution lineage remains visible as unavailable telemetry.
    op.add_column("insights", sa.Column("insight_type", sa.String(length=64), nullable=True))
    op.add_column("insights", sa.Column("structured_payload", postgresql.JSONB(), nullable=True))
    op.add_column(
        "insights",
        sa.Column("producer_job_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "insights",
        sa.Column("dossier_version_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("insights", sa.Column("derivation_version", sa.String(length=64), nullable=True))
    op.create_check_constraint(
        "insight_structured_pair",
        "insights",
        "(insight_type IS NULL AND structured_payload IS NULL) "
        "OR (insight_type IS NOT NULL AND structured_payload IS NOT NULL)",
    )
    op.create_check_constraint(
        "insight_structured_lineage",
        "insights",
        "insight_type IS NULL OR (producer_job_id IS NOT NULL "
        "AND dossier_version_id IS NOT NULL AND derivation_version IS NOT NULL "
        "AND btrim(insight_type) <> '' AND btrim(derivation_version) <> '')",
    )
    op.create_foreign_key(
        "fk_insights_producer_job_id_verification_jobs",
        "insights",
        "verification_jobs",
        ["producer_job_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_insights_dossier_version_id_company_dossier_versions",
        "insights",
        "company_dossier_versions",
        ["dossier_version_id"],
        ["id"],
        ondelete="NO ACTION",
    )
    op.create_index("ix_insights_producer_job_id", "insights", ["producer_job_id"], unique=False)
    op.create_index(
        "ix_insights_company_type_created",
        "insights",
        ["company_id", "insight_type", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_insights_company_type_created", table_name="insights")
    op.drop_index("ix_insights_producer_job_id", table_name="insights")
    op.drop_constraint(
        "fk_insights_dossier_version_id_company_dossier_versions",
        "insights",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_insights_producer_job_id_verification_jobs",
        "insights",
        type_="foreignkey",
    )
    op.drop_constraint(op.f("ck_insights_insight_structured_pair"), "insights", type_="check")
    op.drop_constraint(op.f("ck_insights_insight_structured_lineage"), "insights", type_="check")
    op.drop_column("insights", "derivation_version")
    op.drop_column("insights", "dossier_version_id")
    op.drop_column("insights", "producer_job_id")
    op.drop_column("insights", "structured_payload")
    op.drop_column("insights", "insight_type")

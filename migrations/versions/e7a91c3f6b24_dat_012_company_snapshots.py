"""DAT-012G LinkedIn company-page capture snapshots

Revision ID: e7a91c3f6b24
Revises: c9d24b7e5a11
Create Date: 2026-07-24 18:00:00.000000

Adds the immutable LinkedIn company-capture evidence table. Reuses the existing
``linkedin_snapshot_outcome`` enum. No canonical company table changes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e7a91c3f6b24"
down_revision: str | Sequence[str] | None = "c9d24b7e5a11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Already created by f41c76a9d2e0; referenced here without CREATE TYPE.
_snapshot_outcome = postgresql.ENUM(
    "STORED",
    "EXACT_MATCH_REFRESHED",
    "EXACT_MATCH_UNCHANGED",
    "UNMATCHED_STAGED",
    "AMBIGUOUS_REVIEW",
    "SUPPRESSED",
    name="linkedin_snapshot_outcome",
    create_type=False,
)


def upgrade() -> None:
    """Create the company snapshot evidence table."""
    op.create_table(
        "linkedin_company_snapshots",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("client_capture_id", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("normalized_company_url", sa.String(length=512), nullable=True),
        sa.Column("company_linkedin_id", sa.String(length=256), nullable=True),
        sa.Column("website_domain", sa.String(length=255), nullable=True),
        sa.Column("campaign_id", sa.UUID(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("extraction_status", sa.String(length=32), nullable=False),
        sa.Column("adapter_version", sa.String(length=128), nullable=True),
        sa.Column("missing_sections", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("page_warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("company_fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("hq_city", sa.String(length=255), nullable=True),
        sa.Column("hq_region", sa.String(length=255), nullable=True),
        sa.Column("hq_country", sa.String(length=255), nullable=True),
        sa.Column("outcome", _snapshot_outcome, nullable=False),
        sa.Column("matched_company_id", sa.UUID(), nullable=True),
        sa.Column("review_candidates", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["matched_company_id"], ["companies.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_capture_id", name="uq_li_company_snapshots_client_capture_id"),
    )
    op.create_index(
        "ix_li_company_snapshots_normalized_url",
        "linkedin_company_snapshots",
        ["normalized_company_url"],
    )
    op.create_index(
        "ix_li_company_snapshots_company_li_id",
        "linkedin_company_snapshots",
        ["company_linkedin_id"],
    )
    op.create_index(
        "ix_li_company_snapshots_matched_company_id",
        "linkedin_company_snapshots",
        ["matched_company_id"],
    )


def downgrade() -> None:
    """Drop the company snapshot table (the shared enum stays)."""
    op.drop_index(
        "ix_li_company_snapshots_matched_company_id", table_name="linkedin_company_snapshots"
    )
    op.drop_index("ix_li_company_snapshots_company_li_id", table_name="linkedin_company_snapshots")
    op.drop_index("ix_li_company_snapshots_normalized_url", table_name="linkedin_company_snapshots")
    op.drop_table("linkedin_company_snapshots")

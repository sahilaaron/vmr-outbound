"""DAT-012D LinkedIn profile snapshots and experience observations

Revision ID: f41c76a9d2e0
Revises: 3e86981e8306
Create Date: 2026-07-24 12:00:00.000000

Adds the immutable LinkedIn profile-capture evidence tables: one snapshot row
per accepted capture payload (verbatim body + normalized identity URL +
provenance + truthful ingest outcome) and one nested experience-observation row
per observed role. Nothing here touches contacts, suppressions, or imports.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f41c76a9d2e0"
down_revision: str | Sequence[str] | None = "3e86981e8306"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Managed explicitly (create_type=False) so create_table never emits CREATE
# TYPE; the type is created/dropped by hand around the table. SQLAlchemy stores
# enum members by NAME (upper-case). ``rejected`` payloads are never persisted,
# so the type has no such label.
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
    """Create the snapshot + experience-observation tables and their outcome enum."""
    _snapshot_outcome.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "linkedin_profile_snapshots",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("client_capture_id", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("normalized_profile_url", sa.String(length=512), nullable=True),
        sa.Column("public_identifier", sa.String(length=256), nullable=True),
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
        sa.Column("profile_fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("outcome", _snapshot_outcome, nullable=False),
        sa.Column("matched_contact_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["matched_contact_id"], ["contacts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_capture_id", name="uq_li_profile_snapshots_client_capture_id"),
    )
    op.create_index(
        "ix_li_profile_snapshots_normalized_url",
        "linkedin_profile_snapshots",
        ["normalized_profile_url"],
        unique=False,
    )
    op.create_index(
        "ix_li_profile_snapshots_public_identifier",
        "linkedin_profile_snapshots",
        ["public_identifier"],
        unique=False,
    )
    op.create_index(
        "ix_li_profile_snapshots_matched_contact_id",
        "linkedin_profile_snapshots",
        ["matched_contact_id"],
        unique=False,
    )

    op.create_table(
        "linkedin_profile_experience_observations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("snapshot_id", sa.UUID(), nullable=False),
        sa.Column("position_index", sa.Integer(), nullable=False),
        sa.Column("layout", sa.String(length=16), nullable=False),
        sa.Column("company_name", sa.Text(), nullable=True),
        sa.Column("company_linkedin_url", sa.String(length=512), nullable=True),
        sa.Column("company_linkedin_id", sa.String(length=256), nullable=True),
        sa.Column("job_title", sa.Text(), nullable=True),
        sa.Column("timeline_text", sa.Text(), nullable=True),
        sa.Column("duration_text", sa.Text(), nullable=True),
        sa.Column("start_year", sa.Integer(), nullable=True),
        sa.Column("start_month", sa.Integer(), nullable=True),
        sa.Column("end_year", sa.Integer(), nullable=True),
        sa.Column("end_month", sa.Integer(), nullable=True),
        sa.Column("dates_reliable", sa.Boolean(), nullable=False),
        sa.Column("employment_type", sa.String(length=64), nullable=True),
        sa.Column("role_location", sa.Text(), nullable=True),
        sa.Column("workplace_type", sa.String(length=64), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_lines", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["linkedin_profile_snapshots.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_id", "position_index", name="uq_li_profile_exp_snapshot_position"
        ),
    )
    op.create_index(
        "ix_li_profile_exp_snapshot_id",
        "linkedin_profile_experience_observations",
        ["snapshot_id"],
        unique=False,
    )
    op.create_index(
        "ix_li_profile_exp_company_li_id",
        "linkedin_profile_experience_observations",
        ["company_linkedin_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the observation and snapshot tables, then the outcome enum."""
    op.drop_index(
        "ix_li_profile_exp_company_li_id", table_name="linkedin_profile_experience_observations"
    )
    op.drop_index(
        "ix_li_profile_exp_snapshot_id", table_name="linkedin_profile_experience_observations"
    )
    op.drop_table("linkedin_profile_experience_observations")
    op.drop_index(
        "ix_li_profile_snapshots_matched_contact_id", table_name="linkedin_profile_snapshots"
    )
    op.drop_index(
        "ix_li_profile_snapshots_public_identifier", table_name="linkedin_profile_snapshots"
    )
    op.drop_index("ix_li_profile_snapshots_normalized_url", table_name="linkedin_profile_snapshots")
    op.drop_table("linkedin_profile_snapshots")
    _snapshot_outcome.drop(op.get_bind(), checkfirst=True)

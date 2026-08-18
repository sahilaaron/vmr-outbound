"""Campaign-scoped offering research read from a URL

Revision ID: c3e8b1d47a95
Revises: a7d3e5f19c22
Create Date: 2026-08-18 02:10:00.000000

One additive table, two enums, and one column on ``campaigns`` with a default.
Nothing existing is read or rewritten, and every Campaign that exists when this
runs takes ``offering_source = 'LIBRARY'`` — the behaviour it already has.

``campaign_offering_research`` is one Campaign's versioned attempt to understand
its offering from a page an operator pointed at. The row is both the run and the
answer, so a failure is a row rather than an absence.

Two partial unique indexes carry rules the service layer would otherwise only
promise:

* ``uq_campaign_offering_research_active_campaign`` — at most one run in flight
  per Campaign, so a double-click cannot spend two model calls on one question.
* ``uq_campaign_offering_research_current_campaign`` — at most one *current*
  version per Campaign.

And two check constraints make "a failed re-analysis keeps the last good answer"
a schema fact: only a ``READY`` row may be current, and a ``READY`` row must
carry its structured context.

Downgrade drops the table, the column and both enums. The research history is
lost, which is why the downgrade exists for a fresh environment rather than as a
routine path; no Campaign loses its Library offering, because none was ever
moved.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3e8b1d47a95"
down_revision: str | Sequence[str] | None = "a7d3e5f19c22"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCE = postgresql.ENUM(
    "LIBRARY",
    "URL_RESEARCH",
    name="campaign_offering_source",
    create_type=False,
)

_STATUS = postgresql.ENUM(
    "QUEUED",
    "READING",
    "ANALYZING",
    "CONNECTING",
    "READY",
    "FAILED",
    "CANCELLED",
    name="campaign_offering_research_status",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    _SOURCE.create(bind, checkfirst=True)
    _STATUS.create(bind, checkfirst=True)

    op.add_column(
        "campaigns",
        sa.Column(
            "offering_source",
            _SOURCE,
            nullable=False,
            server_default="LIBRARY",
        ),
    )

    op.create_table(
        "campaign_offering_research",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("source_host", sa.String(length=255), nullable=False),
        sa.Column("status", _STATUS, nullable=False, server_default="QUEUED"),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("offering_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("context_digest", sa.String(length=64), nullable=True),
        sa.Column("context_policy_version", sa.String(length=32), nullable=True),
        sa.Column("producer", sa.String(length=64), nullable=True),
        sa.Column("producer_version", sa.String(length=64), nullable=True),
        sa.Column("producer_model", sa.String(length=120), nullable=True),
        sa.Column("supporting_offering_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("failure_code", sa.String(length=96), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="2"),
        sa.Column(
            "next_run_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.String(length=100), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=400), nullable=False),
        sa.Column("requested_by", sa.String(length=255), nullable=True),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name="fk_campaign_offering_research_campaign",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["supporting_offering_id"],
            ["seller_offerings.id"],
            name="fk_campaign_offering_research_supporting_offering",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_campaign_offering_research"),
        sa.UniqueConstraint(
            "campaign_id",
            "version_number",
            name="uq_campaign_offering_research_campaign_version",
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_campaign_offering_research_idempotency_key"
        ),
        sa.CheckConstraint("version_number >= 1", name="version_number_positive"),
        sa.CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        sa.CheckConstraint("max_attempts >= 1 AND max_attempts <= 10", name="max_attempts_range"),
        sa.CheckConstraint("NOT is_current OR status = 'READY'", name="only_ready_is_current"),
        sa.CheckConstraint(
            "status <> 'READY' OR offering_context IS NOT NULL",
            name="ready_has_context",
        ),
    )
    op.create_index(
        "uq_campaign_offering_research_active_campaign",
        "campaign_offering_research",
        ["campaign_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('QUEUED'::campaign_offering_research_status,"
            "'READING'::campaign_offering_research_status,"
            "'ANALYZING'::campaign_offering_research_status,"
            "'CONNECTING'::campaign_offering_research_status)"
        ),
    )
    op.create_index(
        "uq_campaign_offering_research_current_campaign",
        "campaign_offering_research",
        ["campaign_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_index(
        "ix_campaign_offering_research_claimable",
        "campaign_offering_research",
        ["status", "next_run_at"],
    )
    op.create_index(
        "ix_campaign_offering_research_campaign_id",
        "campaign_offering_research",
        ["campaign_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_campaign_offering_research_campaign_id", table_name="campaign_offering_research"
    )
    op.drop_index(
        "ix_campaign_offering_research_claimable", table_name="campaign_offering_research"
    )
    op.drop_index(
        "uq_campaign_offering_research_current_campaign",
        table_name="campaign_offering_research",
    )
    op.drop_index(
        "uq_campaign_offering_research_active_campaign",
        table_name="campaign_offering_research",
    )
    op.drop_table("campaign_offering_research")
    op.drop_column("campaigns", "offering_source")
    _STATUS.drop(op.get_bind(), checkfirst=True)
    _SOURCE.drop(op.get_bind(), checkfirst=True)

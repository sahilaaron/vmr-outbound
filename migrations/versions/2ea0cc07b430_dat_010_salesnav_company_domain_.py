"""DAT-010 SalesNav company domain enrichment

Revision ID: 2ea0cc07b430
Revises: b2f7c1a904de
Create Date: 2026-07-24 00:45:00.690528

Adds ``salesnav_company_enrichments`` — one record per unique company per staged
Sales Navigator batch — holding the logo.dev lookup result (status, candidates,
query, time) and the operator's explicit domain decision (confirmed candidate,
manual override, or unresolved) with its actor/time. This is provenance/audit
metadata kept separately from the immutable Sales Navigator raw rows and from a
contact's provenance records; no secret (the logo.dev API key) is stored.

Three ENUM types back the truthful lookup/confirmation states. PostgreSQL keeps
a CREATE-d enum type after its table is dropped, so the downgrade drops the table
and then the three types, keeping the migration a clean round trip.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "2ea0cc07b430"
down_revision: str | Sequence[str] | None = "b2f7c1a904de"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOOKUP_STATUS = sa.Enum(
    "NOT_STARTED",
    "OK",
    "NO_MATCH",
    "API_UNAVAILABLE",
    "RATE_LIMITED",
    "MALFORMED",
    "ERROR",
    name="enrichment_lookup_status",
)
_CONFIRMATION_STATUS = sa.Enum(
    "UNCONFIRMED",
    "CONFIRMED",
    "UNRESOLVED",
    name="enrichment_confirmation_status",
)
_CONFIRMATION_SOURCE = sa.Enum(
    "CANDIDATE",
    "MANUAL",
    "UNRESOLVED",
    name="enrichment_confirmation_source",
)


def upgrade() -> None:
    op.create_table(
        "salesnav_company_enrichments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("batch_id", sa.UUID(), nullable=False),
        sa.Column("company_key", sa.String(length=512), nullable=False),
        sa.Column("company_name", sa.String(length=512), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("lookup_status", _LOOKUP_STATUS, nullable=False),
        sa.Column("candidates", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("lookup_query", sa.String(length=512), nullable=True),
        sa.Column("looked_up_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lookup_attempts", sa.Integer(), nullable=False),
        sa.Column("confirmation_status", _CONFIRMATION_STATUS, nullable=False),
        sa.Column("confirmed_domain", sa.String(length=255), nullable=True),
        sa.Column("confirmation_source", _CONFIRMATION_SOURCE, nullable=True),
        sa.Column("confirmed_by", sa.String(length=255), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
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
            ["batch_id"],
            ["import_batches.id"],
            name=op.f("fk_salesnav_company_enrichments_batch_id_import_batches"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_salesnav_company_enrichments")),
        sa.UniqueConstraint(
            "batch_id", "company_key", name="uq_salesnav_company_enrichments_batch_company"
        ),
    )
    op.create_index(
        "ix_salesnav_company_enrichments_batch_id",
        "salesnav_company_enrichments",
        ["batch_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_salesnav_company_enrichments_batch_id",
        table_name="salesnav_company_enrichments",
    )
    op.drop_table("salesnav_company_enrichments")
    # PostgreSQL retains a CREATE-d enum type after its table is dropped; remove
    # the three types so a later re-upgrade recreates them cleanly.
    bind = op.get_bind()
    _CONFIRMATION_SOURCE.drop(bind, checkfirst=True)
    _CONFIRMATION_STATUS.drop(bind, checkfirst=True)
    _LOOKUP_STATUS.drop(bind, checkfirst=True)

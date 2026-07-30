"""Model company-domain fallback state on the enrichment record.

Six columns recording the lookup that runs *behind* logo.dev, kept separate from
the provider's own columns rather than folded into them. The provider found
nothing and the model then found something are two different facts about one
company, and an operator confirming a provisional domain needs both — plus the
page the model says it read, which is the single most useful thing on the record
for deciding whether to trust the answer.

Folding them together would also have made ``lookup_attempts`` ambiguous: a
counter that sometimes counts provider calls and sometimes model calls is worse
than no counter.

Additive and reversible. Existing rows get NOT_STARTED and zero, which is
truthful — nothing has asked a model about them.

Revision ID: d3b7e2f19c45
Revises: c8f1a04b7e63
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d3b7e2f19c45"
down_revision = "c8f1a04b7e63"
branch_labels = None
depends_on = None

TABLE = "salesnav_company_enrichments"

# The enum type already exists — the provider's own `lookup_status` created it.
# `create_type=False` stops Alembic trying to CREATE TYPE a second time, which
# would fail the migration on a database that has ever run the DAT-010 revision.
_LOOKUP_STATUS = sa.Enum(
    "NOT_STARTED",
    "OK",
    "NO_MATCH",
    "API_UNAVAILABLE",
    "RATE_LIMITED",
    "MALFORMED",
    "ERROR",
    name="enrichment_lookup_status",
    create_type=False,
)


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column(
            "model_lookup_status",
            _LOOKUP_STATUS,
            nullable=False,
            server_default="NOT_STARTED",
        ),
    )
    op.add_column(TABLE, sa.Column("model_domain", sa.String(length=255), nullable=True))
    op.add_column(TABLE, sa.Column("model_source_url", sa.String(length=1024), nullable=True))
    op.add_column(TABLE, sa.Column("model_note", sa.Text(), nullable=True))
    op.add_column(TABLE, sa.Column("model_looked_up_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        TABLE,
        sa.Column("model_lookup_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    # The server defaults did their job filling existing rows; drop them so the
    # application layer stays the single place that decides a new row's values.
    op.alter_column(TABLE, "model_lookup_status", server_default=None)
    op.alter_column(TABLE, "model_lookup_attempts", server_default=None)


def downgrade() -> None:
    for column in (
        "model_lookup_attempts",
        "model_looked_up_at",
        "model_note",
        "model_source_url",
        "model_domain",
        "model_lookup_status",
    ):
        op.drop_column(TABLE, column)

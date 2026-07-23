"""workbench ambiguous outcome and mapping

Revision ID: a7c2f1d40e88
Revises: b84699f38ef5
Create Date: 2026-07-23 16:05:00.000000

Operator-workbench slice (DAT-004-compatible ambiguity representation):

* Adds ``ambiguous`` to the ``import_row_outcome`` ENUM. A row whose identity
  match is uncertain (several existing contacts share its natural key) is no
  longer accepted-with-note; it becomes an explicit, reviewable outcome that
  creates no contact and no campaign membership, so an uncertain match can
  never silently merge or silently enter outreach.
* Adds ``import_batches.ambiguous_rows`` to the batch summary counts.
* Adds ``import_batches.column_mapping`` (JSONB) recording the operator-
  confirmed source-column -> system-field mapping applied to the batch, so a
  batch's interpretation of its file is reproducible and reviewable.

Downgrade note: PostgreSQL cannot remove a value from an ENUM in place, so the
downgrade rebuilds the type. Rows carrying the ``ambiguous`` outcome are
re-labelled ``rejected`` first — the closest pre-existing semantics ("this row
did not produce a contact") — with the explanatory note retained. This is a
development-only, lossy-by-design path exercised by the migration round-trip
check; production never downgrades.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a7c2f1d40e88"
down_revision: str | Sequence[str] | None = "b84699f38ef5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_OUTCOMES = ("PENDING", "ACCEPTED", "REJECTED", "DUPLICATE", "SUPPRESSED")


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block on older
    # PostgreSQL; run it in an autocommit block for portability.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE import_row_outcome ADD VALUE IF NOT EXISTS 'AMBIGUOUS'")

    op.add_column(
        "import_batches",
        sa.Column("ambiguous_rows", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "import_batches",
        sa.Column("column_mapping", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("import_batches", "column_mapping")
    op.drop_column("import_batches", "ambiguous_rows")

    # Rebuild the ENUM without AMBIGUOUS (see module docstring for semantics).
    op.execute("UPDATE import_row_validations SET outcome = 'REJECTED' WHERE outcome = 'AMBIGUOUS'")
    op.execute("ALTER TYPE import_row_outcome RENAME TO import_row_outcome_old")
    sa.Enum(*_OLD_OUTCOMES, name="import_row_outcome").create(op.get_bind())
    op.execute(
        "ALTER TABLE import_row_validations "
        "ALTER COLUMN outcome TYPE import_row_outcome "
        "USING outcome::text::import_row_outcome"
    )
    op.execute("DROP TYPE import_row_outcome_old")

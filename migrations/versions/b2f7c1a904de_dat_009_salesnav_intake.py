"""DAT-009 Sales Navigator capture intake staging

Revision ID: b2f7c1a904de
Revises: 906334bc481c
Create Date: 2026-07-23 21:40:00.000000

Backend intake for the operator-driven Sales Navigator capture extension. The
records are staged onto the SAME import tables the CSV/XLSX importer uses; this
migration only adds what is needed to represent and idempotently key that source:

* Adds ``sales_navigator`` (stored as ``SALES_NAVIGATOR``) to the
  ``import_source_format`` ENUM so a staged capture batch records its true
  source instead of masquerading as a spreadsheet.
* Adds ``import_batches.client_batch_id`` — the extension-minted idempotency key
  — with a UNIQUE constraint (a duplicate submission is refused by the database,
  not only in application code) and a lookup index. Spreadsheet imports leave it
  NULL, and PostgreSQL treats NULLs as distinct, so CSV/XLSX batches are
  unaffected.
* Adds ``import_batches.source_metadata`` (JSONB) holding the verbatim batch-level
  provenance from the extension (schema version, source, capture timestamp,
  search URL, extraction metadata).

Downgrade note: PostgreSQL cannot remove a value from an ENUM in place, so the
downgrade rebuilds the type. Any batch still carrying ``SALES_NAVIGATOR`` is
re-labelled ``CSV`` first (closest surviving source label). This is a
development-only, lossy-by-design path exercised by the migration round-trip
check; production never downgrades.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b2f7c1a904de"
down_revision: str | Sequence[str] | None = "906334bc481c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_SOURCE_FORMATS = ("CSV", "XLSX")


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block on older
    # PostgreSQL; run it in an autocommit block for portability.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE import_source_format ADD VALUE IF NOT EXISTS 'SALES_NAVIGATOR'")

    op.add_column(
        "import_batches",
        sa.Column("client_batch_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "import_batches",
        sa.Column("source_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_unique_constraint(
        "uq_import_batches_client_batch_id", "import_batches", ["client_batch_id"]
    )
    op.create_index("ix_import_batches_client_batch_id", "import_batches", ["client_batch_id"])


def downgrade() -> None:
    op.drop_index("ix_import_batches_client_batch_id", table_name="import_batches")
    op.drop_constraint("uq_import_batches_client_batch_id", "import_batches", type_="unique")
    op.drop_column("import_batches", "source_metadata")
    op.drop_column("import_batches", "client_batch_id")

    # Rebuild the ENUM without SALES_NAVIGATOR (see module docstring for semantics).
    op.execute(
        "UPDATE import_batches SET source_format = 'CSV' WHERE source_format = 'SALES_NAVIGATOR'"
    )
    op.execute("ALTER TABLE import_batches ALTER COLUMN source_format DROP DEFAULT")
    op.execute("ALTER TYPE import_source_format RENAME TO import_source_format_old")
    sa.Enum(*_OLD_SOURCE_FORMATS, name="import_source_format").create(op.get_bind())
    op.execute(
        "ALTER TABLE import_batches "
        "ALTER COLUMN source_format TYPE import_source_format "
        "USING source_format::text::import_source_format"
    )
    op.execute("ALTER TABLE import_batches ALTER COLUMN source_format SET DEFAULT 'CSV'")
    op.execute("DROP TYPE import_source_format_old")

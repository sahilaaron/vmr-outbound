"""CI-002 geography relationships and cleaned specialty wording.

Revision ID: a8f3c92d4e17
Revises: c41a9d78e5b2
Create Date: 2026-08-01 06:20:00.000000

Three nullable columns on ``company_intelligence_classifications`` and two new
enum types. No table is created, no row is read or rewritten, and every existing
CI-001 classification stays exactly as it was — the new columns are NULL for all
of them, which is the truthful reading: those rows were produced before
relationships existed and never asserted one.

Why these are columns rather than encoded strings:

* ``geo_relationship`` is what every downstream reader will actually filter on.
  "Has a plant in Pune" and "sells into Pune" describe different companies to
  approach, and a system that cannot tell them apart is not worth querying.
  Packing it into ``unresolved_reason`` — a 96-character free-text field meaning
  "why this is not settled" — would have been exactly the lossy-string shortcut
  the CI-001 schema was built to avoid.
* ``presence_kind`` is derived deterministically from the relationship, and is
  stored anyway. A consumer asking "where is this company physically" should not
  have to re-implement the mapping, and the difference between a factory and a
  sales territory should survive in the row itself rather than in a reader's
  memory.
* ``normalized_value`` holds the deterministically cleaned form of a value, for
  the case where a promotional modifier was safely removed. Deliberately not
  ``term_label``: a term label names something in a controlled vocabulary, and a
  cleaned specialty belongs to no vocabulary. Reusing the column would make "this
  matched a canonical term" and "we removed the word *leading*" impossible to
  tell apart.

Two check constraints keep the pair honest: relationship and presence exist only
on GEOGRAPHY rows, and neither may exist without the other. Both are written so
they hold vacuously for every pre-existing row, so the upgrade cannot fail
against live data.

Downgrade drops the columns and both enum types, leaving the schema exactly as
CI-001 left it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a8f3c92d4e17"
down_revision: str | Sequence[str] | None = "c41a9d78e5b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# `create_type=False` and an explicit `.create()` below, matching the pattern the
# INS-001 migration established: `op.add_column` with a bare `sa.Enum` does not
# reliably emit CREATE TYPE, and a column referencing a type that does not exist
# yet fails at apply time rather than at review time.
_geo_relationship = postgresql.ENUM(
    "HEADQUARTERS",
    "OFFICE",
    "BRANCH",
    "FACILITY",
    "MANUFACTURING",
    "RESEARCH_AND_DEVELOPMENT",
    "WAREHOUSE",
    "DISTRIBUTION",
    "OPERATIONS",
    "COMMERCIAL_MARKET",
    "PLANNED_PRESENCE",
    "HISTORICAL_PRESENCE",
    "UNCLEAR",
    name="intelligence_geo_relationship",
    create_type=False,
)

_presence_kind = postgresql.ENUM(
    "PHYSICAL",
    "COMMERCIAL",
    "PROSPECTIVE",
    "FORMER",
    "UNKNOWN",
    name="intelligence_presence_kind",
    create_type=False,
)

_TABLE = "company_intelligence_classifications"


def upgrade() -> None:
    """Add geography relationship, presence and cleaned-value columns."""

    _geo_relationship.create(op.get_bind(), checkfirst=True)
    _presence_kind.create(op.get_bind(), checkfirst=True)

    op.add_column(_TABLE, sa.Column("normalized_value", sa.String(length=500), nullable=True))
    op.add_column(_TABLE, sa.Column("geo_relationship", _geo_relationship, nullable=True))
    op.add_column(_TABLE, sa.Column("presence_kind", _presence_kind, nullable=True))

    # Both hold vacuously for every existing row (all three columns are NULL),
    # so neither can fail against data written before this revision.
    op.create_check_constraint(
        "geo_fields_are_geography_only",
        _TABLE,
        "(geo_relationship IS NULL AND presence_kind IS NULL) OR dimension = 'GEOGRAPHY'",
    )
    op.create_check_constraint(
        "geo_relationship_and_presence_paired",
        _TABLE,
        "(geo_relationship IS NULL) = (presence_kind IS NULL)",
    )


def downgrade() -> None:
    """Remove the CI-002 columns, constraints and types."""

    op.drop_constraint(
        op.f("ck_company_intelligence_classifications_geo_relationship_and_presence_paired"),
        _TABLE,
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_company_intelligence_classifications_geo_fields_are_geography_only"),
        _TABLE,
        type_="check",
    )
    op.drop_column(_TABLE, "presence_kind")
    op.drop_column(_TABLE, "geo_relationship")
    op.drop_column(_TABLE, "normalized_value")

    # `drop_column` does not drop the type the column referenced. Leaving them
    # behind would make the next upgrade fail with "type already exists".
    op.execute(sa.text("DROP TYPE IF EXISTS intelligence_presence_kind"))
    op.execute(sa.text("DROP TYPE IF EXISTS intelligence_geo_relationship"))

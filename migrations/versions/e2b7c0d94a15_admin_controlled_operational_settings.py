"""Durable, administrator-controlled operational settings.

One table, created empty, and creating it empty is the point: with no rows every
operator control resolves to the deployment's existing ``FEATURES__*`` value, so
this revision changes no behaviour anywhere. The first row appears when an
administrator makes a decision on the Admin Configuration screen.

``key`` is the primary key rather than a surrogate id with a unique index,
because the table is a set of opinions — one per control — and not a log. The
service in ``app/services/operations/settings.py`` validates every key against
the control registry before writing, so a row can only exist for a switch that
is classified as operator-controlled; deployment and security settings have no
write path at all.

No enum type, no foreign key, no server-side default for ``enabled``: a row
exists precisely when somebody decided something, and a default would let one
appear without a decision behind it.

Reversible with no data loss beyond the recorded decisions themselves, which is
the only possible reading of dropping the table — after which every control falls
back to its environment value, which is where it was before this revision.

Revision ID: e2b7c0d94a15
Revises: c1f4a90b7d38
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2b7c0d94a15"
down_revision: str | Sequence[str] | None = "c1f4a90b7d38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "operational_settings"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.String(length=128), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.PrimaryKeyConstraint("key", name="pk_operational_settings"),
    )


def downgrade() -> None:
    op.drop_table(TABLE)

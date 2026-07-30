"""Per-campaign switches for provisional domains and employee-size ordering.

Revision ID: a3c91e5f7d02
Revises: d2f6c8a104be
Create Date: 2026-07-30

Two rules that were global and strict become per-campaign, because the right
answer genuinely differs by audience rather than by installation.

``allow_provisional_domains`` opens the downstream gates to a company domain that
one provider suggested and nothing independently corroborated. It defaults to
false: the stages it unlocks are the ones that spend money and send mail on the
strength of the domain being right, so the permissive reading has to be asked
for. What it deliberately does not touch is the pair of guards that keep a
provisional decision out of the approved-mapping store and stop a
provisional-backed Company counting as established evidence — those prevent a
guess from laundering itself into certainty, and they are independent of what any
campaign is allowed to do with a guess it knows is a guess.

``consult_employee_size`` decides whether the email-format order is chosen from
the Company's employee count. It defaults to true, preserving the existing
behaviour for every campaign that already exists. Turning it off is not a
loosening of a safety rule — it selects one format order for everyone, which is
why its default is the opposite way round from the switch above.

Both are ``NOT NULL`` with a server default, so no backfill is needed and an
existing campaign keeps behaving exactly as it did before this migration ran.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3c91e5f7d02"
down_revision: str | Sequence[str] | None = "d2f6c8a104be"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column(
            "allow_provisional_domains",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column(
        "campaigns",
        sa.Column(
            "consult_employee_size",
            sa.Boolean(),
            nullable=False,
            server_default="true",
        ),
    )


def downgrade() -> None:
    op.drop_column("campaigns", "consult_employee_size")
    op.drop_column("campaigns", "allow_provisional_domains")

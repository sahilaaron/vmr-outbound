"""Drop the employee-size switch: the format order is now fixed.

Revision ID: c8f1a04b7e63
Revises: b7e42d91c530
Create Date: 2026-07-30

``a3c91e5f7d02`` added ``campaigns.consult_employee_size`` so a campaign could turn
off employee-size-based ordering of the email formats. The email policy now uses one
fixed order of three formats for every Contact, so there is no ordering left for the
switch to influence, and a setting that changes nothing is worse than no setting: it
invites an operator to toggle it and conclude the system ignored them.

Removed rather than left in place precisely because it is this young. It was added
in the same unpushed branch, has no operational history behind it, and no stored
decision depends on it.

What is deliberately kept is the *classification*: ``employee_count_class`` and the
employee-evidence columns on ``email_candidate_attempts`` still record what was known
about a company when an attempt was made. That the fact no longer steers the plan is
not a reason to stop recording it.

The downgrade restores the column with its original default, so the revision is
reversible without inventing a value for existing rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8f1a04b7e63"
down_revision: str | Sequence[str] | None = "b7e42d91c530"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("campaigns", "consult_employee_size")


def downgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column(
            "consult_employee_size",
            sa.Boolean(),
            nullable=False,
            server_default="true",
        ),
    )

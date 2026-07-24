"""CMP-001 draft campaign settings

Revision ID: f4c533f48a92
Revises: 3e86981e8306
Create Date: 2026-07-24 12:25:24.633310

Adds the minimum launch-ready campaign settings a draft campaign must persist:
``offer`` (free text), structured ``audience_rules`` and ``exclusions`` (JSONB,
matching how other free-shaped data is already stored — see
``ImportBatch.source_metadata`` and ``AuditEvent.context``), ``min_score_threshold``
(the Initial Fit Score a contact must reach to enter research; defaults to the
launch absolute threshold of 85 from docs/GOAL.md), ``tone``, ``owner``,
``source``, and ``sending_reference``. All eight columns are nullable except
``min_score_threshold``, so existing campaign rows backfill safely with the
documented default and no historical data is altered or dropped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f4c533f48a92"
down_revision: str | Sequence[str] | None = "3e86981e8306"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("campaigns", sa.Column("offer", sa.Text(), nullable=True))
    op.add_column(
        "campaigns",
        sa.Column("audience_rules", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "campaigns",
        sa.Column("exclusions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "campaigns",
        sa.Column("min_score_threshold", sa.Integer(), server_default="85", nullable=False),
    )
    op.add_column("campaigns", sa.Column("tone", sa.String(length=100), nullable=True))
    op.add_column("campaigns", sa.Column("owner", sa.String(length=255), nullable=True))
    op.add_column("campaigns", sa.Column("source", sa.String(length=255), nullable=True))
    op.add_column("campaigns", sa.Column("sending_reference", sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("campaigns", "sending_reference")
    op.drop_column("campaigns", "source")
    op.drop_column("campaigns", "owner")
    op.drop_column("campaigns", "tone")
    op.drop_column("campaigns", "min_score_threshold")
    op.drop_column("campaigns", "exclusions")
    op.drop_column("campaigns", "audience_rules")
    op.drop_column("campaigns", "offer")

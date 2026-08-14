"""Campaign ownership and explicit per-user campaign assignment.

Two additive changes, and neither one guesses.

``campaigns.created_by_user_id``
    Nullable, ``ON DELETE SET NULL``, indexed. Every existing row is left NULL,
    which is the only truthful value available: campaigns created before this
    revision were written by a service whose audit actor is the constant string
    ``"operator"``, so the database contains no evidence of who made them.
    Backfilling them to the bootstrap administrator would record a fact nobody
    established, and it would be indistinguishable afterwards from a campaign
    that administrator really did create.

    Nothing is lost by leaving them NULL. An administrator reaches every
    campaign by role, so every historical campaign stays fully accessible after
    this migration; a normal user reaches an ownerless campaign only through an
    explicit assignment row, which is exactly the decision an operator should
    have to make deliberately.

``campaign_user_assignments``
    The many-to-many grant table. ``UNIQUE (campaign_id, user_id)`` makes the
    assign operation idempotent in the database rather than in a read-then-write
    the application would have to serialise. ``campaign_id`` and ``user_id``
    cascade on delete — deleting either end deletes the grant, because a grant to
    a deleted account or for a deleted campaign is not a thing anyone should be
    able to read back. ``assigned_by_user_id`` is ``SET NULL`` instead: deleting
    the administrator who granted access must not revoke somebody else's access.

Reversible with no data loss beyond the grants themselves, which is the only
possible interpretation of removing the table.

Revision ID: c1f4a90b7d38
Revises: b45732880eff
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1f4a90b7d38"
down_revision: str | Sequence[str] | None = "b45732880eff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CAMPAIGNS = "campaigns"
ASSIGNMENTS = "campaign_user_assignments"


def upgrade() -> None:
    op.add_column(
        CAMPAIGNS,
        sa.Column("created_by_user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_campaigns_created_by_user_id_users",
        CAMPAIGNS,
        "users",
        ["created_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_campaigns_created_by_user_id",
        CAMPAIGNS,
        ["created_by_user_id"],
    )

    op.create_table(
        ASSIGNMENTS,
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assigned_by_user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name="fk_campaign_user_assignments_campaign_id_campaigns",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_campaign_user_assignments_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_by_user_id"],
            ["users.id"],
            name="fk_campaign_user_assignments_assigned_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_campaign_user_assignments"),
        sa.UniqueConstraint(
            "campaign_id", "user_id", name="uq_campaign_user_assignments_campaign_user"
        ),
    )
    op.create_index("ix_campaign_user_assignments_campaign_id", ASSIGNMENTS, ["campaign_id"])
    op.create_index("ix_campaign_user_assignments_user_id", ASSIGNMENTS, ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_campaign_user_assignments_user_id", table_name=ASSIGNMENTS)
    op.drop_index("ix_campaign_user_assignments_campaign_id", table_name=ASSIGNMENTS)
    op.drop_table(ASSIGNMENTS)
    op.drop_index("ix_campaigns_created_by_user_id", table_name=CAMPAIGNS)
    op.drop_constraint("fk_campaigns_created_by_user_id_users", CAMPAIGNS, type_="foreignkey")
    op.drop_column(CAMPAIGNS, "created_by_user_id")

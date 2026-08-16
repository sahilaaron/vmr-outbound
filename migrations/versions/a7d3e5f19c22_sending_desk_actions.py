"""Sending desk: manual email actions and Today dismissals

Revision ID: a7d3e5f19c22
Revises: f4c9a2e70b18
Create Date: 2026-08-16 23:30:00.000000

Two additive tables and one enum. Nothing existing is read or rewritten.

``sequence_email_actions`` is the append-only record of what a person did about
one email of a seven-email package: Actioned (the manual sending-related step
was completed against this exact message version), Skipped (a follow-up was
deliberately removed from the manual cycle) or Undone (one earlier row is
reversed, by name). The first Actioned on position 1 is Day 0 for the person's
follow-up cadence. Nothing here sends anything.

``today_dismissals`` hides one Campaign's due card on Today for one user for one
local day. It changes no shared state.

Downgrade drops both tables and the enum; the history they hold is lost, which
is why the downgrade exists for a fresh environment and not as a routine path.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7d3e5f19c22"
down_revision: str | Sequence[str] | None = "f4c9a2e70b18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KIND = postgresql.ENUM(
    "ACTIONED", "SKIPPED", "UNDONE", name="email_action_kind", create_type=False
)


def upgrade() -> None:
    _KIND.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "sequence_email_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_contact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", _KIND, nullable=False),
        sa.Column("undoes_action_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["campaign_contact_id"],
            ["campaign_contacts.id"],
            name="fk_sequence_email_actions_campaign_contact_id_campaign_contacts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name="fk_sequence_email_actions_campaign_id_campaigns",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["email_sequence_messages.id"],
            name="fk_sequence_email_actions_message_id_email_sequence_messages",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["message_version_id"],
            ["email_sequence_message_versions.id"],
            name="fk_sequence_email_actions_message_version_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["undoes_action_id"],
            ["sequence_email_actions.id"],
            name="fk_sequence_email_actions_undoes_action_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sequence_email_actions"),
        sa.CheckConstraint("position >= 1 AND position <= 7", name="position_within_sequence"),
        sa.CheckConstraint("btrim(actor) <> ''", name="actor_not_blank"),
        sa.CheckConstraint(
            "(kind = 'UNDONE' AND undoes_action_id IS NOT NULL) "
            "OR (kind <> 'UNDONE' AND undoes_action_id IS NULL)",
            name="undo_names_its_target",
        ),
    )
    op.create_index(
        "ix_sequence_email_actions_membership_position",
        "sequence_email_actions",
        ["campaign_contact_id", "position", "occurred_at"],
    )
    op.create_index(
        "ix_sequence_email_actions_campaign_id", "sequence_email_actions", ["campaign_id"]
    )

    op.create_table(
        "today_dismissals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("local_day", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_today_dismissals_user_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name="fk_today_dismissals_campaign_id_campaigns",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_today_dismissals"),
        sa.UniqueConstraint(
            "user_id", "campaign_id", "local_day", name="uq_today_dismissals_user_campaign_day"
        ),
    )


def downgrade() -> None:
    op.drop_table("today_dismissals")
    op.drop_index("ix_sequence_email_actions_campaign_id", table_name="sequence_email_actions")
    op.drop_index(
        "ix_sequence_email_actions_membership_position", table_name="sequence_email_actions"
    )
    op.drop_table("sequence_email_actions")
    _KIND.drop(op.get_bind(), checkfirst=True)

"""DAT-006 suppression ledger audit trail and multi-reason

Revision ID: 3e86981e8306
Revises: 87cdc91ff558
Create Date: 2026-07-24 11:04:39.619813

Adds the append-only suppression lifecycle history, per-record ``created_by`` and
``is_active`` state, a new ``legal_compliance`` reason, and moves the ledger from
one-record-per-identity to one-record-per-(identity, reason) so an identity can
carry several suppression reasons at once.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "3e86981e8306"
down_revision: str | Sequence[str] | None = "87cdc91ff558"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Both enums are managed explicitly (create_type=False) so create_table never
# emits CREATE TYPE: suppression_reason already exists, and suppression_event_type
# is created/dropped by hand around the table. The postgresql dialect ENUM honours
# create_type=False during create_table (plain sa.Enum does not).
_suppression_reason = postgresql.ENUM(
    "OPT_OUT",
    "HARD_BOUNCE",
    "CUSTOMER",
    "COMPETITOR",
    "INTERNAL_EXCLUSION",
    "LEGAL_COMPLIANCE",
    "MANUAL",
    name="suppression_reason",
    create_type=False,
)
_event_type = postgresql.ENUM(
    "CREATED",
    "REACTIVATED",
    "DEACTIVATED",
    name="suppression_event_type",
    create_type=False,
)


def upgrade() -> None:
    """Add multi-reason state, lifecycle history, and the legal/compliance reason."""
    # New reason value. IF NOT EXISTS keeps a re-run (or a single-step downgrade
    # then re-upgrade) safe, since PostgreSQL cannot remove an enum value.
    # SQLAlchemy stores this enum by member NAME (upper-case), matching the
    # existing labels OPT_OUT, HARD_BOUNCE, … so the new value is 'LEGAL_COMPLIANCE'.
    op.execute("ALTER TYPE suppression_reason ADD VALUE IF NOT EXISTS 'LEGAL_COMPLIANCE'")

    _event_type.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "suppression_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("suppression_id", sa.UUID(), nullable=False),
        sa.Column("event_type", _event_type, nullable=False),
        sa.Column("reason", _suppression_reason, nullable=False),
        sa.Column("source", sa.String(length=255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(length=255), nullable=True),
        sa.Column("active_after", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["suppression_id"],
            ["suppressions.id"],
            name=op.f("fk_suppression_events_suppression_id_suppressions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_suppression_events")),
    )
    op.create_index(
        "ix_suppression_events_suppression_id",
        "suppression_events",
        ["suppression_id"],
        unique=False,
    )

    op.add_column("suppressions", sa.Column("created_by", sa.String(length=255), nullable=True))
    op.add_column(
        "suppressions", sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False)
    )
    op.add_column(
        "suppressions",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.drop_constraint(op.f("uq_suppressions_type_value"), "suppressions", type_="unique")
    op.create_index(
        "ix_suppressions_value_active", "suppressions", ["value", "is_active"], unique=False
    )
    op.create_unique_constraint(
        "uq_suppressions_type_value_reason", "suppressions", ["suppression_type", "value", "reason"]
    )


def downgrade() -> None:
    """Reverse DAT-006 structural changes.

    The ``legal_compliance`` enum value is intentionally left in place:
    PostgreSQL cannot drop an enum value, and ``downgrade base`` removes the whole
    ``suppression_reason`` type with its owning tables anyway. A single-step
    downgrade therefore leaves the (unused) value harmlessly present, and the
    ``IF NOT EXISTS`` guard on upgrade keeps a re-upgrade safe.
    """
    op.drop_constraint("uq_suppressions_type_value_reason", "suppressions", type_="unique")
    op.drop_index("ix_suppressions_value_active", table_name="suppressions")
    op.create_unique_constraint(
        op.f("uq_suppressions_type_value"), "suppressions", ["suppression_type", "value"]
    )
    op.drop_column("suppressions", "updated_at")
    op.drop_column("suppressions", "is_active")
    op.drop_column("suppressions", "created_by")
    op.drop_index("ix_suppression_events_suppression_id", table_name="suppression_events")
    op.drop_table("suppression_events")
    op.execute("DROP TYPE IF EXISTS suppression_event_type")

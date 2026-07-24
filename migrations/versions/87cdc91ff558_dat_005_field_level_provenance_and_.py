"""DAT-005 field-level provenance and freshness ledger

Revision ID: 87cdc91ff558
Revises: ad1e298fb49a
Create Date: 2026-07-24 10:59:26.497685

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "87cdc91ff558"
down_revision: str | Sequence[str] | None = "ad1e298fb49a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the append-only field-level provenance/freshness ledger (DAT-005)."""
    op.create_table(
        "contact_field_values",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("contact_id", sa.UUID(), nullable=False),
        sa.Column("field_name", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("import_batch_id", sa.UUID(), nullable=True),
        sa.Column("import_row_id", sa.UUID(), nullable=True),
        sa.Column("source_name", sa.String(length=512), nullable=True),
        sa.Column("source_reference", sa.String(length=1024), nullable=True),
        sa.Column("exported_by", sa.String(length=255), nullable=True),
        sa.Column("is_manual_override", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("is_current_winner", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("policy_version", sa.String(length=50), nullable=False),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name=op.f("fk_contact_field_values_contact_id_contacts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["import_batch_id"],
            ["import_batches.id"],
            name=op.f("fk_contact_field_values_import_batch_id_import_batches"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["import_row_id"],
            ["import_rows.id"],
            name=op.f("fk_contact_field_values_import_row_id_import_rows"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contact_field_values")),
    )
    op.create_index(
        "ix_contact_field_values_contact_field",
        "contact_field_values",
        ["contact_id", "field_name"],
        unique=False,
    )
    op.create_index(
        "uq_contact_field_values_winner",
        "contact_field_values",
        ["contact_id", "field_name"],
        unique=True,
        postgresql_where="is_current_winner",
    )


def downgrade() -> None:
    """Drop the field-level provenance/freshness ledger."""
    op.drop_index(
        "uq_contact_field_values_winner",
        table_name="contact_field_values",
        postgresql_where="is_current_winner",
    )
    op.drop_index("ix_contact_field_values_contact_field", table_name="contact_field_values")
    op.drop_table("contact_field_values")

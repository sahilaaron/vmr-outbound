"""dat-004 identity resolution and merge tombstone

Revision ID: 906334bc481c
Revises: a7c2f1d40e88
Create Date: 2026-07-23 17:59:15.053928

Operator identity-resolution slice (DAT-004):

* Adds ``identity_resolutions`` — the immutable audit history of operator
  decisions that resolve an ambiguous imported identity (assign to an existing
  contact, create a new contact, mark intentionally separate) or merge two
  confirmed duplicate contacts. Each row records the actor, action, reason,
  idempotency key, and before/after state snapshots. A partial unique index on
  ``import_row_id`` allows at most one active resolution per ambiguous row
  (idempotency), while merge-only records (no import row) do not collide on
  NULL. ``idempotency_key`` is globally unique so a retried submission is a
  no-op.
* Adds ``contacts.merged_into_id`` — a self-referential tombstone pointer. A
  contact confirmed as a duplicate is never deleted (its import history and
  provenance are preserved); it is folded into a surviving contact and points at
  it, so it drops out of dedup and the active list while staying auditable.

Downgrade drops the table, the contact column, and the ``identity_resolution_type``
ENUM (PostgreSQL keeps an ENUM type after its last table is dropped, which would
break a later re-upgrade), so the migration round-trip is clean.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "906334bc481c"
down_revision: str | Sequence[str] | None = "a7c2f1d40e88"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "identity_resolutions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "resolution_type",
            sa.Enum(
                "ASSIGN_EXISTING",
                "CREATE_NEW",
                "MARK_SEPARATE",
                "MERGE",
                name="identity_resolution_type",
            ),
            nullable=False,
        ),
        sa.Column("import_row_id", sa.UUID(), nullable=True),
        sa.Column("target_contact_id", sa.UUID(), nullable=True),
        sa.Column("merged_contact_id", sa.UUID(), nullable=True),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("previous_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("resulting_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["import_row_id"],
            ["import_rows.id"],
            name=op.f("fk_identity_resolutions_import_row_id_import_rows"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["merged_contact_id"],
            ["contacts.id"],
            name=op.f("fk_identity_resolutions_merged_contact_id_contacts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["target_contact_id"],
            ["contacts.id"],
            name=op.f("fk_identity_resolutions_target_contact_id_contacts"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_identity_resolutions")),
        sa.UniqueConstraint("idempotency_key", name="uq_identity_resolutions_idempotency_key"),
    )
    op.create_index(
        "ix_identity_resolutions_created_at", "identity_resolutions", ["created_at"], unique=False
    )
    op.create_index(
        "ix_identity_resolutions_merged_contact_id",
        "identity_resolutions",
        ["merged_contact_id"],
        unique=False,
    )
    op.create_index(
        "ix_identity_resolutions_target_contact_id",
        "identity_resolutions",
        ["target_contact_id"],
        unique=False,
    )
    op.create_index(
        "uq_identity_resolutions_import_row",
        "identity_resolutions",
        ["import_row_id"],
        unique=True,
        postgresql_where="import_row_id IS NOT NULL",
    )

    op.add_column("contacts", sa.Column("merged_into_id", sa.UUID(), nullable=True))
    op.create_index("ix_contacts_merged_into_id", "contacts", ["merged_into_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_contacts_merged_into_id_contacts"),
        "contacts",
        "contacts",
        ["merged_into_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_contacts_merged_into_id_contacts"), "contacts", type_="foreignkey")
    op.drop_index("ix_contacts_merged_into_id", table_name="contacts")
    op.drop_column("contacts", "merged_into_id")

    op.drop_index("uq_identity_resolutions_import_row", table_name="identity_resolutions")
    op.drop_index("ix_identity_resolutions_target_contact_id", table_name="identity_resolutions")
    op.drop_index("ix_identity_resolutions_merged_contact_id", table_name="identity_resolutions")
    op.drop_index("ix_identity_resolutions_created_at", table_name="identity_resolutions")
    op.drop_table("identity_resolutions")

    # PostgreSQL keeps an ENUM type after its last table is dropped; drop it so a
    # later re-upgrade can recreate it cleanly (migration round-trip check).
    sa.Enum(name="identity_resolution_type").drop(op.get_bind())

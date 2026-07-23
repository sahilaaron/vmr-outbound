"""phase 1 data and campaigns schema

Revision ID: c11379ba2041
Revises: d955c69a6052
Create Date: 2026-07-23 12:04:35.544820

Adds the Phase 1 (Data & Campaigns) data foundation: campaigns, contacts,
campaign membership, import batches, immutable raw import rows, per-row
validation results and errors, provenance records, and the suppression ledger
(DAT-001, DAT-002).

The downgrade drops the PostgreSQL ENUM types created by the new columns as well
as the tables, so the migration is fully reversible and an
upgrade -> downgrade -> upgrade round trip succeeds cleanly (this is exercised in
CI).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c11379ba2041"
down_revision: str | Sequence[str] | None = "d955c69a6052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Named ENUM types introduced by this revision. They are auto-created by the
# first table that uses each type on upgrade; PostgreSQL does not drop them
# automatically on drop_table, so they are dropped explicitly on downgrade.
ENUM_TYPES: tuple[str, ...] = (
    "campaign_status",
    "contact_workflow_state",
    "import_batch_status",
    "import_row_outcome",
    "dedup_match_type",
    "suppression_type",
    "suppression_reason",
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "campaigns",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("DRAFT", "ACTIVE", "ARCHIVED", name="campaign_status"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campaigns")),
        sa.UniqueConstraint("name", name=op.f("uq_campaigns_name")),
    )
    op.create_table(
        "contacts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("first_name", sa.String(length=255), nullable=False),
        sa.Column("last_name", sa.String(length=255), nullable=False),
        sa.Column("company_name", sa.String(length=512), nullable=False),
        sa.Column("company_domain", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("linkedin_url", sa.String(length=512), nullable=True),
        sa.Column("country", sa.String(length=128), nullable=True),
        sa.Column("industry", sa.String(length=255), nullable=True),
        sa.Column("company_size", sa.String(length=64), nullable=True),
        sa.Column("natural_key", sa.String(length=1024), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contacts")),
    )
    op.create_index("ix_contacts_company_domain", "contacts", ["company_domain"], unique=False)
    op.create_index("ix_contacts_natural_key", "contacts", ["natural_key"], unique=False)
    op.create_index(
        "uq_contacts_email",
        "contacts",
        ["email"],
        unique=True,
        postgresql_where="email IS NOT NULL",
    )
    op.create_table(
        "suppressions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "suppression_type",
            sa.Enum("EMAIL", "DOMAIN", name="suppression_type"),
            nullable=False,
        ),
        sa.Column("value", sa.String(length=320), nullable=False),
        sa.Column(
            "reason",
            sa.Enum(
                "OPT_OUT",
                "HARD_BOUNCE",
                "CUSTOMER",
                "COMPETITOR",
                "INTERNAL_EXCLUSION",
                "MANUAL",
                name="suppression_reason",
            ),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_suppressions")),
        sa.UniqueConstraint("suppression_type", "value", name="uq_suppressions_type_value"),
    )
    op.create_index("ix_suppressions_value", "suppressions", ["value"], unique=False)
    op.create_table(
        "import_batches",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("campaign_id", sa.UUID(), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "VALIDATING", "COMPLETED", "FAILED", name="import_batch_status"),
            nullable=False,
        ),
        sa.Column("source_name", sa.String(length=512), nullable=True),
        sa.Column("source_reference", sa.String(length=1024), nullable=True),
        sa.Column("exported_by", sa.String(length=255), nullable=True),
        sa.Column("exported_at", sa.Date(), nullable=True),
        sa.Column("total_rows", sa.Integer(), nullable=False),
        sa.Column("accepted_rows", sa.Integer(), nullable=False),
        sa.Column("rejected_rows", sa.Integer(), nullable=False),
        sa.Column("duplicate_rows", sa.Integer(), nullable=False),
        sa.Column("suppressed_rows", sa.Integer(), nullable=False),
        sa.Column("contacts_created", sa.Integer(), nullable=False),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name=op.f("fk_import_batches_campaign_id_campaigns"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_batches")),
    )
    op.create_index(
        "ix_import_batches_campaign_id", "import_batches", ["campaign_id"], unique=False
    )
    op.create_index(
        "ix_import_batches_content_hash", "import_batches", ["content_hash"], unique=False
    )
    op.create_table(
        "campaign_contacts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("campaign_id", sa.UUID(), nullable=False),
        sa.Column("contact_id", sa.UUID(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "IMPORTED",
                "AWAITING_VERIFICATION",
                "SUPPRESSED",
                "EXCLUDED",
                name="contact_workflow_state",
            ),
            nullable=False,
        ),
        sa.Column("source_batch_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name=op.f("fk_campaign_contacts_campaign_id_campaigns"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name=op.f("fk_campaign_contacts_contact_id_contacts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_batch_id"],
            ["import_batches.id"],
            name=op.f("fk_campaign_contacts_source_batch_id_import_batches"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campaign_contacts")),
        sa.UniqueConstraint(
            "campaign_id", "contact_id", name="uq_campaign_contacts_campaign_contact"
        ),
    )
    op.create_index(
        "ix_campaign_contacts_campaign_id", "campaign_contacts", ["campaign_id"], unique=False
    )
    op.create_index(
        "ix_campaign_contacts_contact_id", "campaign_contacts", ["contact_id"], unique=False
    )
    op.create_index("ix_campaign_contacts_state", "campaign_contacts", ["state"], unique=False)
    op.create_table(
        "import_rows",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("batch_id", sa.UUID(), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("raw_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["import_batches.id"],
            name=op.f("fk_import_rows_batch_id_import_batches"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_rows")),
        sa.UniqueConstraint("batch_id", "row_number", name="uq_import_rows_batch_row"),
    )
    op.create_index("ix_import_rows_batch_id", "import_rows", ["batch_id"], unique=False)
    op.create_table(
        "import_row_errors",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("import_row_id", sa.UUID(), nullable=False),
        sa.Column("column_name", sa.String(length=255), nullable=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["import_row_id"],
            ["import_rows.id"],
            name=op.f("fk_import_row_errors_import_row_id_import_rows"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_row_errors")),
    )
    op.create_index(
        "ix_import_row_errors_import_row_id", "import_row_errors", ["import_row_id"], unique=False
    )
    op.create_table(
        "import_row_validations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("import_row_id", sa.UUID(), nullable=False),
        sa.Column(
            "outcome",
            sa.Enum(
                "PENDING",
                "ACCEPTED",
                "REJECTED",
                "DUPLICATE",
                "SUPPRESSED",
                name="import_row_outcome",
            ),
            nullable=False,
        ),
        sa.Column("normalized_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("contact_id", sa.UUID(), nullable=True),
        sa.Column(
            "match_type",
            sa.Enum("EMAIL", "NATURAL_KEY", name="dedup_match_type"),
            nullable=True,
        ),
        sa.Column("suppression_id", sa.UUID(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name=op.f("fk_import_row_validations_contact_id_contacts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["import_row_id"],
            ["import_rows.id"],
            name=op.f("fk_import_row_validations_import_row_id_import_rows"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["suppression_id"],
            ["suppressions.id"],
            name=op.f("fk_import_row_validations_suppression_id_suppressions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_row_validations")),
        sa.UniqueConstraint("import_row_id", name="uq_import_row_validations_row"),
    )
    op.create_index(
        "ix_import_row_validations_contact_id",
        "import_row_validations",
        ["contact_id"],
        unique=False,
    )
    op.create_index(
        "ix_import_row_validations_outcome", "import_row_validations", ["outcome"], unique=False
    )
    op.create_table(
        "provenance_records",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("contact_id", sa.UUID(), nullable=False),
        sa.Column("import_batch_id", sa.UUID(), nullable=False),
        sa.Column("import_row_id", sa.UUID(), nullable=False),
        sa.Column("source_name", sa.String(length=512), nullable=True),
        sa.Column("source_reference", sa.String(length=1024), nullable=True),
        sa.Column("exported_by", sa.String(length=255), nullable=True),
        sa.Column("exported_at", sa.Date(), nullable=True),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name=op.f("fk_provenance_records_contact_id_contacts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["import_batch_id"],
            ["import_batches.id"],
            name=op.f("fk_provenance_records_import_batch_id_import_batches"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["import_row_id"],
            ["import_rows.id"],
            name=op.f("fk_provenance_records_import_row_id_import_rows"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provenance_records")),
    )
    op.create_index(
        "ix_provenance_records_contact_id", "provenance_records", ["contact_id"], unique=False
    )
    op.create_index(
        "ix_provenance_records_import_batch_id",
        "provenance_records",
        ["import_batch_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_provenance_records_import_batch_id", table_name="provenance_records")
    op.drop_index("ix_provenance_records_contact_id", table_name="provenance_records")
    op.drop_table("provenance_records")
    op.drop_index("ix_import_row_validations_outcome", table_name="import_row_validations")
    op.drop_index("ix_import_row_validations_contact_id", table_name="import_row_validations")
    op.drop_table("import_row_validations")
    op.drop_index("ix_import_row_errors_import_row_id", table_name="import_row_errors")
    op.drop_table("import_row_errors")
    op.drop_index("ix_import_rows_batch_id", table_name="import_rows")
    op.drop_table("import_rows")
    op.drop_index("ix_campaign_contacts_state", table_name="campaign_contacts")
    op.drop_index("ix_campaign_contacts_contact_id", table_name="campaign_contacts")
    op.drop_index("ix_campaign_contacts_campaign_id", table_name="campaign_contacts")
    op.drop_table("campaign_contacts")
    op.drop_index("ix_import_batches_content_hash", table_name="import_batches")
    op.drop_index("ix_import_batches_campaign_id", table_name="import_batches")
    op.drop_table("import_batches")
    op.drop_index("ix_suppressions_value", table_name="suppressions")
    op.drop_table("suppressions")
    op.drop_index("uq_contacts_email", table_name="contacts", postgresql_where="email IS NOT NULL")
    op.drop_index("ix_contacts_natural_key", table_name="contacts")
    op.drop_index("ix_contacts_company_domain", table_name="contacts")
    op.drop_table("contacts")
    op.drop_table("campaigns")

    # PostgreSQL does not drop ENUM types on drop_table; remove them explicitly so
    # a subsequent upgrade can recreate them (CI runs upgrade->downgrade->upgrade).
    bind = op.get_bind()
    for enum_name in ENUM_TYPES:
        sa.Enum(name=enum_name).drop(bind, checkfirst=True)

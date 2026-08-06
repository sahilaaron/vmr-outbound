"""Campaign-bound contact file import: imported-email truth model (IMP-001).

Adds the two tables that hold facts no existing table could hold truthfully, and
widens the two import tables that already exist rather than building a parallel
import subsystem beside them.

``imported_contact_emails`` is deliberately NOT
``exact_email_verifications``. That table means "a provider was asked about this
mailbox and answered"; an import asks nobody, so writing into it would
manufacture verification evidence from a spreadsheet cell. The new table records
the vendor's claims as the vendor's, next to the raw text they were read from,
and records what VMR itself did in two explicit outcomes that assert nothing
about deliverability.

Every new column on the two existing tables is nullable or has a server default,
so the migration is safe against live data and every pre-existing batch keeps
meaning exactly what it meant. The three new enum types are dropped on downgrade;
no value is added to an existing enum type, because ``ALTER TYPE ... ADD VALUE``
cannot be reversed and would have made this migration one-way.

Revision ID: c1f7a3e29b04
Revises: b6d4e07a1f38
Create Date: 2026-08-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c1f7a3e29b04"
down_revision = "b6d4e07a1f38"
branch_labels = None
depends_on = None


IMPORTED_EMAIL_SLOT = "imported_email_slot"
IMPORTED_EMAIL_STAGE_OUTCOME = "imported_email_stage_outcome"
IMPORTED_VERIFICATION_OUTCOME = "imported_verification_outcome"


def upgrade() -> None:
    slot = postgresql.ENUM(
        "PRIMARY",
        "SECONDARY",
        "TERTIARY",
        name=IMPORTED_EMAIL_SLOT,
        create_type=False,
    )
    stage_outcome = postgresql.ENUM(
        "IMPORTED_EMAIL_ACCEPTED",
        "IMPORTED_EMAIL_REJECTED",
        name=IMPORTED_EMAIL_STAGE_OUTCOME,
        create_type=False,
    )
    verification_outcome = postgresql.ENUM(
        "VERIFICATION_BYPASSED_IMPORTED_EMAIL",
        "VERIFICATION_NOT_PERFORMED",
        name=IMPORTED_VERIFICATION_OUTCOME,
        create_type=False,
    )
    bind = op.get_bind()
    slot.create(bind, checkfirst=True)
    stage_outcome.create(bind, checkfirst=True)
    verification_outcome.create(bind, checkfirst=True)

    # --- Imported-email evidence ------------------------------------------
    op.create_table(
        "imported_contact_emails",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("import_batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("import_row_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("slot", slot, nullable=False),
        sa.Column("raw_email", sa.String(length=512), nullable=False),
        sa.Column("normalized_email", sa.String(length=320), nullable=True),
        sa.Column("source_row_number", sa.Integer(), nullable=False),
        sa.Column("source_sheet_name", sa.String(length=255), nullable=True),
        sa.Column("source_file_checksum", sa.String(length=64), nullable=False),
        sa.Column("source_schema", sa.String(length=64), nullable=False),
        sa.Column("row_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("provider_source", sa.String(length=255), nullable=True),
        sa.Column("provider_status_raw", sa.String(length=128), nullable=True),
        sa.Column("provider_status_normalized", sa.String(length=128), nullable=True),
        sa.Column("provider_verification_source", sa.String(length=255), nullable=True),
        sa.Column("provider_catch_all_raw", sa.String(length=128), nullable=True),
        sa.Column("provider_catch_all_normalized", sa.String(length=128), nullable=True),
        sa.Column("provider_last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_last_verified_raw", sa.String(length=128), nullable=True),
        sa.Column("email_stage_outcome", stage_outcome, nullable=True),
        sa.Column("verification_stage_outcome", verification_outcome, nullable=True),
        sa.Column("rejection_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name="fk_imported_contact_emails_campaign_id_campaigns",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name="fk_imported_contact_emails_contact_id_contacts",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["import_batch_id"],
            ["import_batches.id"],
            name="fk_imported_contact_emails_import_batch_id_import_batches",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["import_row_id"],
            ["import_rows.id"],
            name="fk_imported_contact_emails_import_row_id_import_rows",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_imported_contact_emails"),
        sa.UniqueConstraint(
            "import_row_id",
            "slot",
            name="uq_imported_contact_emails_import_row_slot",
        ),
        sa.CheckConstraint(
            "(email_stage_outcome IS DISTINCT FROM 'IMPORTED_EMAIL_ACCEPTED')"
            " OR (verification_stage_outcome = 'VERIFICATION_BYPASSED_IMPORTED_EMAIL')",
            name="ck_imported_contact_emails_accepted_primary_records_bypass",
        ),
        sa.CheckConstraint(
            "(email_stage_outcome IS DISTINCT FROM 'IMPORTED_EMAIL_ACCEPTED')"
            " OR (normalized_email IS NOT NULL)",
            name="ck_imported_contact_emails_accepted_primary_normalized",
        ),
    )
    op.create_index(
        "ix_imported_contact_emails_contact_id",
        "imported_contact_emails",
        ["contact_id"],
    )
    op.create_index(
        "ix_imported_contact_emails_campaign_id",
        "imported_contact_emails",
        ["campaign_id"],
    )
    op.create_index(
        "ix_imported_contact_emails_batch_id",
        "imported_contact_emails",
        ["import_batch_id"],
    )
    op.create_index(
        "ix_imported_contact_emails_normalized_email",
        "imported_contact_emails",
        ["normalized_email"],
    )
    op.create_index(
        "ix_imported_contact_emails_campaign_contact_slot",
        "imported_contact_emails",
        ["campaign_id", "contact_id", "slot"],
    )
    # Partial: at most one ACCEPTED address per source-row-content per Campaign.
    # A refused row still leaves its evidence, so a corrected file can be
    # imported afterwards instead of being reported as already done.
    op.create_index(
        "uq_imported_contact_emails_accepted_row",
        "imported_contact_emails",
        ["campaign_id", "row_fingerprint", "slot"],
        unique=True,
        postgresql_where=sa.text("email_stage_outcome = 'IMPORTED_EMAIL_ACCEPTED'"),
    )

    # --- External source identifiers ---------------------------------------
    op.create_table(
        "import_source_identifiers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("system", sa.String(length=64), nullable=False),
        sa.Column("identifier_kind", sa.String(length=64), nullable=False),
        sa.Column("identifier_value", sa.String(length=256), nullable=False),
        sa.Column("first_seen_batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("recorded_by", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_import_source_identifiers_company_id_companies",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name="fk_import_source_identifiers_contact_id_contacts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["first_seen_batch_id"],
            ["import_batches.id"],
            name="fk_import_source_identifiers_first_seen_batch",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_import_source_identifiers"),
        sa.UniqueConstraint(
            "system",
            "identifier_kind",
            "identifier_value",
            name="uq_import_source_identifiers_system_kind_value",
        ),
        sa.CheckConstraint(
            "(contact_id IS NOT NULL) <> (company_id IS NOT NULL)",
            name="ck_import_source_identifiers_exactly_one_subject",
        ),
    )
    op.create_index(
        "ix_import_source_identifiers_contact_id",
        "import_source_identifiers",
        ["contact_id"],
    )
    op.create_index(
        "ix_import_source_identifiers_company_id",
        "import_source_identifiers",
        ["company_id"],
    )

    # --- Batch-level import lifecycle --------------------------------------
    op.add_column("import_batches", sa.Column("source_schema", sa.String(length=64), nullable=True))
    op.add_column("import_batches", sa.Column("selected_sheet_index", sa.Integer(), nullable=True))
    op.add_column(
        "import_batches", sa.Column("selected_sheet_name", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "import_batches", sa.Column("sanitized_filename", sa.String(length=512), nullable=True)
    )
    op.add_column(
        "import_batches",
        sa.Column("detected_headers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("import_batches", sa.Column("uploaded_by", sa.String(length=255), nullable=True))
    op.add_column(
        "import_batches", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "import_batches",
        sa.Column(
            "already_in_campaign_rows",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )

    # --- Per-row result detail ---------------------------------------------
    op.add_column(
        "import_row_validations", sa.Column("row_fingerprint", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "import_row_validations",
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "import_row_validations",
        sa.Column("campaign_contact_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "import_row_validations",
        sa.Column("imported_email_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "import_row_validations",
        sa.Column("contact_match_basis", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "import_row_validations",
        sa.Column("company_match_basis", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "import_row_validations",
        sa.Column("membership_action", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "import_row_validations",
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "import_row_validations", sa.Column("error_code", sa.String(length=64), nullable=True)
    )
    op.create_foreign_key(
        "fk_import_row_validations_company_id_companies",
        "import_row_validations",
        "companies",
        ["company_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_import_row_validations_campaign_contact",
        "import_row_validations",
        "campaign_contacts",
        ["campaign_contact_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_import_row_validations_imported_email",
        "import_row_validations",
        "imported_contact_emails",
        ["imported_email_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_import_row_validations_imported_email",
        "import_row_validations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_import_row_validations_campaign_contact",
        "import_row_validations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_import_row_validations_company_id_companies",
        "import_row_validations",
        type_="foreignkey",
    )
    for column in (
        "error_code",
        "warnings",
        "membership_action",
        "company_match_basis",
        "contact_match_basis",
        "imported_email_id",
        "campaign_contact_id",
        "company_id",
        "row_fingerprint",
    ):
        op.drop_column("import_row_validations", column)

    for column in (
        "already_in_campaign_rows",
        "confirmed_at",
        "uploaded_by",
        "detected_headers",
        "sanitized_filename",
        "selected_sheet_name",
        "selected_sheet_index",
        "source_schema",
    ):
        op.drop_column("import_batches", column)

    op.drop_index("ix_import_source_identifiers_company_id", table_name="import_source_identifiers")
    op.drop_index("ix_import_source_identifiers_contact_id", table_name="import_source_identifiers")
    op.drop_table("import_source_identifiers")

    for index in (
        "uq_imported_contact_emails_accepted_row",
        "ix_imported_contact_emails_campaign_contact_slot",
        "ix_imported_contact_emails_normalized_email",
        "ix_imported_contact_emails_batch_id",
        "ix_imported_contact_emails_campaign_id",
        "ix_imported_contact_emails_contact_id",
    ):
        op.drop_index(index, table_name="imported_contact_emails")
    op.drop_table("imported_contact_emails")

    bind = op.get_bind()
    for name in (
        IMPORTED_VERIFICATION_OUTCOME,
        IMPORTED_EMAIL_STAGE_OUTCOME,
        IMPORTED_EMAIL_SLOT,
    ):
        postgresql.ENUM(name=name, create_type=False).drop(bind, checkfirst=True)

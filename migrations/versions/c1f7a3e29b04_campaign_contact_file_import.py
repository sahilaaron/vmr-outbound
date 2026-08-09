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
            name="accepted_primary_records_bypass",
        ),
        sa.CheckConstraint(
            "(email_stage_outcome IS DISTINCT FROM 'IMPORTED_EMAIL_ACCEPTED')"
            " OR (normalized_email IS NOT NULL)",
            name="accepted_primary_normalized",
        ),
        # An accepted address belongs to somebody, and only the primary slot is
        # ever acted on. Both are what the model already says; neither was
        # enforceable, so a direct writer could create an accepted orphan or an
        # accepted alternate and no constraint would object.
        sa.CheckConstraint(
            "(email_stage_outcome IS DISTINCT FROM 'IMPORTED_EMAIL_ACCEPTED')"
            " OR (contact_id IS NOT NULL)",
            name="accepted_requires_contact",
        ),
        sa.CheckConstraint(
            "(email_stage_outcome IS DISTINCT FROM 'IMPORTED_EMAIL_ACCEPTED')"
            " OR (slot = 'PRIMARY')",
            name="accepted_is_primary",
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
    # Partial: at most one ACCEPTED primary address per person per Campaign.
    # The index below keys on row CONTENT, which answers "was this row imported
    # before"; this one keys on the person, which is what the Email stage asks.
    op.create_index(
        "uq_imported_contact_emails_accepted_campaign_contact",
        "imported_contact_emails",
        ["campaign_id", "contact_id"],
        unique=True,
        postgresql_where=sa.text(
            "email_stage_outcome = 'IMPORTED_EMAIL_ACCEPTED'"
            " AND slot = 'PRIMARY'"
            " AND contact_id IS NOT NULL"
        ),
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
            name="exactly_one_subject",
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


#: Per-row decision columns this migration adds and its downgrade drops. A row
#: is only holding something when one of these is set — ``warnings`` defaults to
#: an empty array, so an empty array is absence, not a decision.
_ROW_DECISION_PREDICATE = (
    "error_code IS NOT NULL"
    " OR warnings <> '[]'::jsonb"
    " OR membership_action IS NOT NULL"
    " OR company_match_basis IS NOT NULL"
    " OR contact_match_basis IS NOT NULL"
    " OR imported_email_id IS NOT NULL"
    " OR campaign_contact_id IS NOT NULL"
    " OR company_id IS NOT NULL"
    " OR row_fingerprint IS NOT NULL"
)

#: The same idea for the batch header. ``already_in_campaign_rows`` defaults to
#: zero, so zero is absence.
_BATCH_DETAIL_PREDICATE = (
    "source_schema IS NOT NULL"
    " OR selected_sheet_index IS NOT NULL"
    " OR selected_sheet_name IS NOT NULL"
    " OR sanitized_filename IS NOT NULL"
    " OR detected_headers IS NOT NULL"
    " OR uploaded_by IS NOT NULL"
    " OR confirmed_at IS NOT NULL"
    " OR already_in_campaign_rows <> 0"
)


def _unrecoverable_state(bind: sa.engine.Connection) -> list[str]:
    """Which facts this database holds that a downgrade would end.

    Deliberately narrow. The point of the guard is to protect decisions and
    provenance that exist nowhere else, not to make the migration unreversible
    for anyone who has ever run it. A database whose import tables predate this
    migration, and which therefore holds only the defaults the upgrade wrote
    into them, reverses without ceremony — which is also what keeps the
    round-trip test in ``tests/test_migrations.py`` meaningful.

    Every entry it returns is a count and a category name. No address, no
    filename, no identifier and no SQL appears in the result, because the string
    it builds is raised to whoever is running the migration and that may not be
    the person entitled to see the contents.
    """

    holdings: list[str] = []
    for table, label in (
        ("imported_contact_emails", "imported address record(s)"),
        ("import_source_identifiers", "imported source identifier(s)"),
    ):
        if bind.execute(sa.text("SELECT to_regclass(:name)"), {"name": f"public.{table}"}).scalar():
            count = bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()
            if count:
                holdings.append(f"{count} {label}")

    for table, predicate, label in (
        (
            "import_row_validations",
            _ROW_DECISION_PREDICATE,
            "per-row import decision(s) (contact and company match basis, membership "
            "action, refusal code, warnings, row fingerprint)",
        ),
        (
            "import_batches",
            _BATCH_DETAIL_PREDICATE,
            "file import batch record(s) with confirmation, uploader, worksheet or "
            "detected-header provenance",
        ),
    ):
        if not bind.execute(
            sa.text("SELECT to_regclass(:name)"), {"name": f"public.{table}"}
        ).scalar():
            continue
        count = bind.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE {predicate}")
        ).scalar_one()
        if count:
            holdings.append(f"{count} {label}")

    return holdings


def downgrade() -> None:
    """Reverse cleanly on an empty schema; refuse once there is data to lose.

    Four kinds of record disappear here and none of them can be re-derived. The
    imported address records are the only place the system states which address
    a Campaign was told to use *and* that no verification provider was asked
    about it — the pair of facts this whole feature exists to keep. The source
    identifiers are what make a re-import idempotent, so losing them means the
    next import of the same file silently duplicates people. The per-row
    decisions and the batch provenance record what an operator and this system
    concluded about rows that may have been imported long before this migration
    ran, on tables that already existed.

    That last point is why the guard looks at more than the two new tables. A
    downgrade that dropped only what it created would be safe; this one also
    removes nine columns from ``import_row_validations`` and eight from
    ``import_batches``, and those columns hold decisions about pre-existing
    batches too.

    The refusal is conditional on there being something to protect, following
    APP-003 (``c48b1f70a3d2``) and the seven other guarded migrations in this
    repository.
    """

    bind = op.get_bind()
    holdings = _unrecoverable_state(bind)
    if holdings:
        raise RuntimeError(
            "IMP-001 (c1f7a3e29b04) will not downgrade while the campaign contact file "
            "import holds records that exist nowhere else: "
            + "; ".join(holdings)
            + ". Reversing would destroy them silently and none of them can be "
            "re-derived — an imported address without its bypass is indistinguishable "
            "from a verified one, and a rebuilt match is a guess rather than the "
            "decision that was made. Restore from a backup taken before the upgrade "
            "instead."
        )

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
        "uq_imported_contact_emails_accepted_campaign_contact",
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

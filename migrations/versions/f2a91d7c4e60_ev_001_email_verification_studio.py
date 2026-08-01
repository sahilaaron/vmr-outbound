"""EV-001 provider configuration, policies, evidence and usage attribution.

Revision ID: f2a91d7c4e60
Revises: e8c4a91f2d60
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f2a91d7c4e60"
down_revision = "e8c4a91f2d60"
branch_labels = None
depends_on = None

_WATERFALL_ID = "e7000000-0000-4000-8000-000000000001"
_WATERFALL_ACTIVATION_ID = "e7000000-0000-4000-8000-000000000002"
_PATTERN_ID = "e7000000-0000-4000-8000-000000000003"
_PATTERN_ACTIVATION_ID = "e7000000-0000-4000-8000-000000000004"


def _append_only_trigger(table: str) -> None:
    function = f"prevent_{table}_mutation"
    op.execute(
        f"""
        CREATE FUNCTION {function}() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION '{table} is append-only';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER {table}_append_only
          BEFORE UPDATE OR DELETE ON {table}
          FOR EACH ROW EXECUTE FUNCTION {function}();
        """
    )


def upgrade() -> None:
    op.add_column(
        "usage_ledger_entries",
        sa.Column("campaign_contact_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "usage_ledger_entries",
        sa.Column(
            "origin", sa.String(length=32), nullable=False, server_default="customer_operation"
        ),
    )
    op.add_column("usage_ledger_entries", sa.Column("account_reference", sa.String(length=160)))
    op.create_foreign_key(
        "fk_usage_ledger_campaign_contact",
        "usage_ledger_entries",
        "campaign_contacts",
        ["campaign_contact_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "usage_origin_known",
        "usage_ledger_entries",
        "origin IN ('customer_operation', 'admin_operation', 'agent_studio')",
    )
    op.create_index(
        "ix_usage_ledger_entries_origin",
        "usage_ledger_entries",
        ["origin", "attempted_at"],
    )

    op.create_table(
        "provider_credential_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("label", sa.String(160), nullable=False),
        sa.Column("encrypted_secret", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(160), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_provider_credentials_provider_created",
        "provider_credential_versions",
        ["provider_id", "created_at"],
    )
    op.create_table(
        "provider_credential_activations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column(
            "credential_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("provider_credential_versions.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "previous_credential_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("provider_credential_versions.id", ondelete="RESTRICT"),
        ),
        sa.Column("activated_by", sa.String(160), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column(
            "activated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_provider_credential_activations_provider",
        "provider_credential_activations",
        ["provider_id", "activated_at"],
    )

    for table, activation in (
        ("verification_waterfall_policy_versions", "verification_waterfall_activations"),
        ("email_pattern_policy_versions", "email_pattern_policy_activations"),
    ):
        op.create_table(
            table,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("version_number", sa.Integer(), nullable=False, unique=True),
            sa.Column("schema_version", sa.String(64), nullable=False),
            sa.Column("name", sa.String(160), nullable=False),
            sa.Column("configuration", postgresql.JSONB(), nullable=False),
            sa.Column(
                "based_on_version_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey(f"{table}.id", ondelete="RESTRICT"),
            ),
            sa.Column("change_note", sa.Text()),
            sa.Column("created_by", sa.String(160), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_table(
            activation,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "policy_version_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey(f"{table}.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column(
                "previous_policy_version_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey(f"{table}.id", ondelete="RESTRICT"),
            ),
            sa.Column("activated_by", sa.String(160), nullable=False),
            sa.Column("reason", sa.Text()),
            sa.Column(
                "activated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )

    op.create_table(
        "learned_domain_email_formats",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("pattern_id", sa.String(64), nullable=False),
        sa.Column("human_format", sa.String(160), nullable=False),
        sa.Column("support_count", sa.Integer(), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("conflicts", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("provenance", postgresql.JSONB(), nullable=False),
        sa.Column(
            "source_verification_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("exact_email_verifications.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_learned_domain_formats_domain_observed",
        "learned_domain_email_formats",
        ["domain", "last_observed_at"],
    )
    op.create_table(
        "verification_provider_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "verification_attempt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("verification_attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("verification_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider_order", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("adapter_version", sa.String(64), nullable=False),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("provider_called", sa.Boolean(), nullable=False),
        sa.Column("precise_status", sa.String(50)),
        sa.Column("result", sa.String(50)),
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("conflict", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error_summary", sa.Text()),
        sa.Column(
            "verification_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("exact_email_verifications.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "usage_ledger_entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("usage_ledger_entries.id", ondelete="RESTRICT"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "verification_attempt_id", "provider_order", name="uq_verification_provider_step"
        ),
    )
    op.create_index(
        "ix_verification_provider_attempts_job",
        "verification_provider_attempts",
        ["job_id", "provider_order"],
    )
    op.create_table(
        "provider_test_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column(
            "credential_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("provider_credential_versions.id", ondelete="RESTRICT"),
        ),
        sa.Column("normalized_email", sa.String(320), nullable=False),
        sa.Column("live", sa.Boolean(), nullable=False),
        sa.Column("result", sa.String(50)),
        sa.Column("precise_status", sa.String(50)),
        sa.Column("error_summary", sa.Text()),
        sa.Column("response_summary", postgresql.JSONB()),
        sa.Column(
            "usage_ledger_entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("usage_ledger_entries.id", ondelete="RESTRICT"),
        ),
        sa.Column("actor", sa.String(160), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    # Legacy size evidence remains readable on historical candidate attempts but
    # is no longer required for new Email executions.
    op.alter_column("email_candidate_attempts", "employee_count_class", nullable=True)
    op.alter_column("email_candidate_attempts", "employee_evidence_freshness", nullable=True)
    op.drop_constraint(
        "ck_email_candidate_attempts_ck_email_candidate_attempts_257a",
        "email_candidate_attempts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_email_candidate_attempts_ck_email_candidate_attempts_257a",
        "email_candidate_attempts",
        "candidate_index >= 0 AND candidate_index < 24",
    )

    waterfall = {
        "schema_version": "verification-waterfall/v1",
        "providers": [
            {"id": "millionverifier", "enabled": True},
            {"id": "debounce", "enabled": True},
        ],
        "stop_on": ["valid", "invalid", "disposable", "role_based"],
        "continue_on": ["catch_all", "unknown", "transient_provider"],
    }
    patterns = {
        "schema_version": "email-pattern-policy/v1",
        "learned_formats_first": True,
        "max_candidates": 8,
        "stop_after_accepted": True,
        "patterns": [
            {"id": "firstname.lastname", "enabled": True, "example": "ada.lovelace"},
            {"id": "firstname", "enabled": True, "example": "ada"},
            {"id": "finitiallastname", "enabled": True, "example": "alovelace"},
        ],
    }
    op.execute(
        sa.text(
            "INSERT INTO verification_waterfall_policy_versions "
            "(id, version_number, schema_version, name, configuration, created_by) "
            "VALUES (:id, 1, 'verification-waterfall/v1', 'Initial provider waterfall', "
            "CAST(:configuration AS jsonb), 'migration:EV-001')"
        ).bindparams(id=_WATERFALL_ID, configuration=json.dumps(waterfall))
    )
    op.execute(
        sa.text(
            "INSERT INTO verification_waterfall_activations "
            "(id, policy_version_id, activated_by, reason) "
            "VALUES (:id, :policy_id, 'migration:EV-001', 'initial policy')"
        ).bindparams(id=_WATERFALL_ACTIVATION_ID, policy_id=_WATERFALL_ID)
    )
    op.execute(
        sa.text(
            "INSERT INTO email_pattern_policy_versions "
            "(id, version_number, schema_version, name, configuration, created_by) "
            "VALUES (:id, 1, 'email-pattern-policy/v1', 'Initial Email pattern policy', "
            "CAST(:configuration AS jsonb), 'migration:EV-001')"
        ).bindparams(id=_PATTERN_ID, configuration=json.dumps(patterns))
    )
    op.execute(
        sa.text(
            "INSERT INTO email_pattern_policy_activations "
            "(id, policy_version_id, activated_by, reason) "
            "VALUES (:id, :policy_id, 'migration:EV-001', 'initial policy')"
        ).bindparams(id=_PATTERN_ACTIVATION_ID, policy_id=_PATTERN_ID)
    )

    for table in (
        "provider_credential_versions",
        "provider_credential_activations",
        "verification_waterfall_policy_versions",
        "verification_waterfall_activations",
        "email_pattern_policy_versions",
        "email_pattern_policy_activations",
        "learned_domain_email_formats",
        "verification_provider_attempts",
        "provider_test_runs",
    ):
        _append_only_trigger(table)


def downgrade() -> None:
    for table in (
        "provider_test_runs",
        "verification_provider_attempts",
        "learned_domain_email_formats",
        "email_pattern_policy_activations",
        "email_pattern_policy_versions",
        "verification_waterfall_activations",
        "verification_waterfall_policy_versions",
        "provider_credential_activations",
        "provider_credential_versions",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS prevent_{table}_mutation()")

    op.drop_constraint(
        op.f("ck_email_candidate_attempts_candidate_index_bounded"),
        "email_candidate_attempts",
        type_="check",
    )
    op.create_check_constraint(
        "candidate_index_bounded",
        "email_candidate_attempts",
        "candidate_index >= 0 AND candidate_index < 3",
    )
    op.alter_column("email_candidate_attempts", "employee_evidence_freshness", nullable=False)
    op.alter_column("email_candidate_attempts", "employee_count_class", nullable=False)

    for table in (
        "provider_test_runs",
        "verification_provider_attempts",
        "learned_domain_email_formats",
        "email_pattern_policy_activations",
        "email_pattern_policy_versions",
        "verification_waterfall_activations",
        "verification_waterfall_policy_versions",
        "provider_credential_activations",
        "provider_credential_versions",
    ):
        op.drop_table(table)
    op.drop_index("ix_usage_ledger_entries_origin", table_name="usage_ledger_entries")
    op.drop_constraint(
        op.f("ck_usage_ledger_entries_usage_origin_known"),
        "usage_ledger_entries",
        type_="check",
    )
    op.drop_constraint(
        "fk_usage_ledger_campaign_contact", "usage_ledger_entries", type_="foreignkey"
    )
    op.drop_column("usage_ledger_entries", "account_reference")
    op.drop_column("usage_ledger_entries", "origin")
    op.drop_column("usage_ledger_entries", "campaign_contact_id")

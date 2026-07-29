"""Phase 2 Campaign, Collection, Agent job, and pipeline backbone.

Revision ID: 4c8e1b2d9a70
Revises: a4e2b91f7c38
Create Date: 2026-07-29

The existing ``verification_jobs`` and ``contact_labels`` physical tables are
extended in place. They are the proven queue and Collection registry,
respectively; retaining their names avoids copying data or breaking historical
foreign keys while canonical application models expose AgentJob and Collection.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "4c8e1b2d9a70"
down_revision: str | Sequence[str] | None = "a4e2b91f7c38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_ENUMS: dict[str, tuple[str, ...]] = {
    "agent_identifier": (
        "CAPTURE",
        "IDENTITY",
        "COMPANY",
        "RESEARCH",
        "EMAIL",
        "VERIFICATION",
        "INSIGHTS",
        "PERSONALIZATION",
        "SENDING",
    ),
    "agent_control_status": ("ENABLED", "PAUSED", "DISABLED"),
    "campaign_membership_status": ("ACTIVE", "PAUSED", "ARCHIVED"),
    "campaign_contact_eligibility": (
        "UNKNOWN",
        "ELIGIBLE",
        "REVIEW_REQUIRED",
        "BLOCKED",
    ),
    "pipeline_stage_status": (
        "WAITING",
        "RUNNING",
        "PAUSED",
        "RETRYING",
        "FAILED",
        "COMPLETED",
        "DISABLED",
        "SKIPPED",
        "BLOCKED",
    ),
    "pipeline_event_type": (
        "ENROLLED",
        "MEMBERSHIP_PAUSED",
        "MEMBERSHIP_RESUMED",
        "MEMBERSHIP_ARCHIVED",
        "STAGE_WAITING",
        "JOB_QUEUED",
        "JOB_LEASED",
        "JOB_STARTED",
        "STAGE_COMPLETED",
        "STAGE_SKIPPED",
        "RETRY_SCHEDULED",
        "FAILED_RETRYABLE",
        "FAILED_TERMINAL",
        "AGENT_PAUSED",
        "AGENT_DISABLED",
        "ELIGIBILITY_BLOCKED",
        "ELIGIBILITY_RESTORED",
        "JOB_CANCELLED",
    ),
    "capture_campaign_filing_status": ("PENDING", "APPLIED", "FAILED"),
}


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*NEW_ENUMS[name], name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for enum_name, labels in NEW_ENUMS.items():
        postgresql.ENUM(*labels, name=enum_name).create(bind, checkfirst=True)

    # PostgreSQL cannot use a newly added enum label until the ALTER TYPE
    # transaction commits. The explicit autocommit boundary makes LEASED
    # available to the follow-up partial-index migration even when ``upgrade
    # head`` applies both revisions in one command.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE verification_job_status ADD VALUE IF NOT EXISTS 'LEASED'")
        op.execute("ALTER TYPE verification_job_status ADD VALUE IF NOT EXISTS 'PAUSED'")
        op.execute("ALTER TYPE linkedin_snapshot_outcome ADD VALUE IF NOT EXISTS 'CONTACT_CREATED'")

    # A permanent Contact may exist before its name/company identity converges.
    # NULL is truthful unresolved data; no placeholder domain or person value is
    # introduced merely to satisfy the legacy import-era constraints.
    for column, column_type in (
        ("first_name", sa.String(255)),
        ("last_name", sa.String(255)),
        ("company_name", sa.String(512)),
        ("company_domain", sa.String(255)),
        ("natural_key", sa.String(1024)),
    ):
        op.alter_column(
            "contacts",
            column,
            existing_type=column_type,
            nullable=True,
        )

    op.add_column("contact_labels", sa.Column("description", sa.Text(), nullable=True))
    op.add_column(
        "contact_labels",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    for column in (
        sa.Column("sender_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("target_audience", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("messaging_direction", sa.Text(), nullable=True),
        sa.Column("primary_cta", sa.Text(), nullable=True),
        sa.Column("template_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cadence_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("sending_settings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "execution_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("settings_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_reason", sa.Text(), nullable=True),
    ):
        op.add_column("campaigns", column)

    for column in (
        sa.Column("source_capture_id", sa.UUID(), nullable=True),
        sa.Column("source_kind", sa.String(length=64), server_default="legacy", nullable=False),
        sa.Column("source_reference", sa.String(length=512), nullable=True),
        sa.Column("enrolled_by", sa.String(length=128), nullable=True),
        sa.Column(
            "enrolled_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "membership_status",
            _enum("campaign_membership_status"),
            server_default="ACTIVE",
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "eligibility_status",
            _enum("campaign_contact_eligibility"),
            server_default="UNKNOWN",
            nullable=False,
        ),
        sa.Column(
            "blocking_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("qualification_state", sa.String(length=64), nullable=True),
        sa.Column("review_state", sa.String(length=64), server_default="pending", nullable=False),
        sa.Column(
            "sending_state",
            sa.String(length=64),
            server_default="not_started",
            nullable=False,
        ),
        sa.Column(
            "provider_state",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "desired_stage",
            _enum("agent_identifier"),
            server_default="SENDING",
            nullable=False,
        ),
        sa.Column("current_stage", _enum("agent_identifier"), nullable=True),
        sa.Column("latest_completed_stage", _enum("agent_identifier"), nullable=True),
        sa.Column(
            "next_stage",
            _enum("agent_identifier"),
            server_default="IDENTITY",
            nullable=True,
        ),
        sa.Column(
            "pipeline_status",
            _enum("pipeline_stage_status"),
            server_default="WAITING",
            nullable=False,
        ),
        sa.Column(
            "processing_state",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    ):
        op.add_column("campaign_contacts", column)
    op.create_foreign_key(
        "fk_campaign_contacts_source_capture",
        "campaign_contacts",
        "linkedin_profile_snapshots",
        ["source_capture_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute("UPDATE campaign_contacts SET enrolled_at = created_at")
    op.execute(
        """
        UPDATE campaign_contacts
        SET eligibility_status = CASE
                WHEN state IN ('SUPPRESSED', 'EXCLUDED') THEN
                    'BLOCKED'::campaign_contact_eligibility
                ELSE 'UNKNOWN'::campaign_contact_eligibility
            END,
            pipeline_status = CASE
                WHEN state IN ('SUPPRESSED', 'EXCLUDED') THEN
                    'BLOCKED'::pipeline_stage_status
                ELSE 'DISABLED'::pipeline_stage_status
            END,
            current_stage = 'IDENTITY'::agent_identifier,
            latest_completed_stage = 'CAPTURE'::agent_identifier,
            next_stage = 'IDENTITY'::agent_identifier,
            blocking_reasons = CASE
                WHEN state IN ('SUPPRESSED', 'EXCLUDED') THEN
                    jsonb_build_array(jsonb_build_object(
                        'code', lower(state::text),
                        'detail', 'legacy Campaign Contact terminal state',
                        'terminal', true
                    ))
                ELSE '[]'::jsonb
            END
        """
    )
    op.create_index(
        "ix_campaign_contacts_membership_status",
        "campaign_contacts",
        ["campaign_id", "membership_status"],
    )
    op.create_index(
        "ix_campaign_contacts_pipeline_status",
        "campaign_contacts",
        ["campaign_id", "pipeline_status"],
    )
    op.create_index(
        "ix_campaign_contacts_current_stage",
        "campaign_contacts",
        ["campaign_id", "current_stage"],
    )
    op.create_index(
        "ix_campaign_contacts_eligibility",
        "campaign_contacts",
        ["campaign_id", "eligibility_status"],
    )

    op.create_table(
        "agent_controls",
        sa.Column("agent_id", _enum("agent_identifier"), nullable=False),
        sa.Column("status", _enum("agent_control_status"), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.String(length=128), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("agent_id", name="pk_agent_controls"),
    )
    op.create_table(
        "campaign_agent_overrides",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("campaign_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", _enum("agent_identifier"), nullable=False),
        sa.Column("status", _enum("agent_control_status"), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.String(length=128), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name="fk_campaign_agent_overrides_campaign_id_campaigns",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_campaign_agent_overrides"),
        sa.UniqueConstraint(
            "campaign_id",
            "agent_id",
            name="uq_campaign_agent_overrides_campaign_agent",
        ),
    )
    op.create_table(
        "campaign_collections",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("campaign_id", sa.UUID(), nullable=False),
        sa.Column("collection_id", sa.UUID(), nullable=False),
        sa.Column(
            "association_role",
            sa.String(length=32),
            server_default="audience",
            nullable=False,
        ),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name="fk_campaign_collections_campaign_id_campaigns",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["contact_labels.id"],
            name="fk_campaign_collections_collection_id_contact_labels",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_campaign_collections"),
        sa.UniqueConstraint(
            "campaign_id",
            "collection_id",
            name="uq_campaign_collections_campaign_collection",
        ),
    )
    op.create_index(
        "ix_campaign_collections_campaign_id",
        "campaign_collections",
        ["campaign_id"],
    )
    op.create_index(
        "ix_campaign_collections_collection_id",
        "campaign_collections",
        ["collection_id"],
    )

    # Generalize the proven verification queue in place.
    for column in (
        sa.Column(
            "agent_id",
            _enum("agent_identifier"),
            server_default="VERIFICATION",
            nullable=False,
        ),
        sa.Column(
            "task_kind",
            sa.String(length=96),
            server_default="verify_exact_email",
            nullable=False,
        ),
        sa.Column("priority", sa.Integer(), server_default="100", nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=True),
        sa.Column("entity_id", sa.UUID(), nullable=True),
        sa.Column("campaign_contact_id", sa.UUID(), nullable=True),
        sa.Column("company_id", sa.UUID(), nullable=True),
        sa.Column("capture_id", sa.UUID(), nullable=True),
        sa.Column(
            "input_reference",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_class", sa.String(length=96), nullable=True),
        sa.Column("parent_job_id", sa.UUID(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    ):
        op.add_column("verification_jobs", column)
    op.alter_column("verification_jobs", "email", existing_type=sa.String(320), nullable=True)
    op.alter_column(
        "verification_jobs",
        "policy_version",
        existing_type=sa.String(50),
        nullable=True,
    )
    op.alter_column("verification_jobs", "max_attempts", server_default="3")
    op.create_foreign_key(
        "fk_verification_jobs_campaign_contact_id_campaign_contacts",
        "verification_jobs",
        "campaign_contacts",
        ["campaign_contact_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_verification_jobs_company_id_companies",
        "verification_jobs",
        "companies",
        ["company_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_verification_jobs_capture_id_linkedin_profile_snapshots",
        "verification_jobs",
        "linkedin_profile_snapshots",
        ["capture_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_verification_jobs_parent_job_id_verification_jobs",
        "verification_jobs",
        "verification_jobs",
        ["parent_job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_index("ix_verification_jobs_claimable", table_name="verification_jobs")
    op.create_index(
        "ix_verification_jobs_claimable",
        "verification_jobs",
        ["status", "priority", "next_run_at"],
    )
    op.create_index(
        "ix_verification_jobs_agent_status",
        "verification_jobs",
        ["agent_id", "status"],
    )
    op.create_index(
        "ix_verification_jobs_campaign_contact_id",
        "verification_jobs",
        ["campaign_contact_id"],
    )

    op.create_table(
        "campaign_contact_sources",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("campaign_contact_id", sa.UUID(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=512), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_reference", sa.String(length=512), nullable=True),
        sa.Column("capture_id", sa.UUID(), nullable=True),
        sa.Column("import_batch_id", sa.UUID(), nullable=True),
        sa.Column("collection_id", sa.UUID(), nullable=True),
        sa.Column(
            "source_context",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("recorded_by", sa.String(length=128), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["campaign_contact_id"],
            ["campaign_contacts.id"],
            name="fk_campaign_contact_sources_membership",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["capture_id"],
            ["linkedin_profile_snapshots.id"],
            name="fk_campaign_contact_sources_capture",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["import_batch_id"],
            ["import_batches.id"],
            name="fk_campaign_contact_sources_import_batch_id_import_batches",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["contact_labels.id"],
            name="fk_campaign_contact_sources_collection_id_contact_labels",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_campaign_contact_sources"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_campaign_contact_sources_idempotency_key",
        ),
    )
    op.create_index(
        "ix_campaign_contact_sources_membership",
        "campaign_contact_sources",
        ["campaign_contact_id", "recorded_at"],
    )
    op.create_table(
        "campaign_contact_agent_states",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("campaign_contact_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", _enum("agent_identifier"), nullable=False),
        sa.Column(
            "status",
            _enum("pipeline_stage_status"),
            server_default="WAITING",
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("latest_job_id", sa.UUID(), nullable=True),
        sa.Column("reason_code", sa.String(length=96), nullable=True),
        sa.Column("reason_detail", sa.Text(), nullable=True),
        sa.Column("retryable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("waiting_on_agent", _enum("agent_identifier"), nullable=True),
        sa.Column("output_reference", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["campaign_contact_id"],
            ["campaign_contacts.id"],
            name="fk_campaign_contact_agent_states_membership",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["latest_job_id"],
            ["verification_jobs.id"],
            name="fk_campaign_contact_agent_states_job",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_campaign_contact_agent_states"),
        sa.UniqueConstraint(
            "campaign_contact_id",
            "agent_id",
            name="uq_campaign_contact_agent_states_membership_agent",
        ),
    )
    op.create_index(
        "ix_campaign_contact_agent_states_status",
        "campaign_contact_agent_states",
        ["agent_id", "status"],
    )
    op.create_table(
        "pipeline_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("campaign_contact_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", _enum("agent_identifier"), nullable=True),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("event_type", _enum("pipeline_event_type"), nullable=False),
        sa.Column("from_status", _enum("pipeline_stage_status"), nullable=True),
        sa.Column("to_status", _enum("pipeline_stage_status"), nullable=True),
        sa.Column("reason_code", sa.String(length=96), nullable=True),
        sa.Column("reason_detail", sa.Text(), nullable=True),
        sa.Column("retryable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["campaign_contact_id"],
            ["campaign_contacts.id"],
            name="fk_pipeline_events_campaign_contact_id_campaign_contacts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["verification_jobs.id"],
            name="fk_pipeline_events_job_id_verification_jobs",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_pipeline_events"),
    )
    op.create_index(
        "ix_pipeline_events_membership_time",
        "pipeline_events",
        ["campaign_contact_id", "occurred_at"],
    )
    op.create_index("ix_pipeline_events_job_id", "pipeline_events", ["job_id"])
    op.create_table(
        "capture_campaign_filings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("capture_id", sa.UUID(), nullable=False),
        sa.Column("submission_id", sa.UUID(), nullable=True),
        sa.Column("requested_campaign_id", sa.UUID(), nullable=False),
        sa.Column("campaign_id", sa.UUID(), nullable=True),
        sa.Column("campaign_contact_id", sa.UUID(), nullable=True),
        sa.Column(
            "status",
            _enum("capture_campaign_filing_status"),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=96), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
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
            ["capture_id"],
            ["linkedin_profile_snapshots.id"],
            name="fk_capture_campaign_filings_capture",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["contact_capture_submissions.id"],
            name="fk_capture_campaign_filings_submission",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name="fk_capture_campaign_filings_campaign_id_campaigns",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_contact_id"],
            ["campaign_contacts.id"],
            name="fk_capture_campaign_filings_membership",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_capture_campaign_filings"),
        sa.UniqueConstraint(
            "capture_id",
            name="uq_capture_campaign_filings_capture_id",
        ),
    )
    op.create_index(
        "ix_capture_campaign_filings_status",
        "capture_campaign_filings",
        ["status", "updated_at"],
    )

    # Existing memberships gain a conservative, explainable starting history.
    # Every row already points at a permanent Contact, so Capture is complete.
    # Campaign execution was introduced disabled-by-default, so Identity is
    # either disabled or retains the prior terminal policy block.
    op.execute(
        """
        INSERT INTO campaign_contact_agent_states (
            id, campaign_contact_id, agent_id, status, attempt_count,
            reason_code, reason_detail, retryable, output_reference,
            completed_at, updated_at
        )
        SELECT
            md5(cc.id::text || '-capture-state')::uuid,
            cc.id,
            'CAPTURE'::agent_identifier,
            'COMPLETED'::pipeline_stage_status,
            0,
            'legacy_permanent_contact',
            'Existing permanent Contact backfilled into the Phase 2 pipeline.',
            false,
            jsonb_build_object('contact_id', cc.contact_id::text),
            cc.enrolled_at,
            cc.enrolled_at
        FROM campaign_contacts AS cc
        """
    )
    op.execute(
        """
        INSERT INTO campaign_contact_agent_states (
            id, campaign_contact_id, agent_id, status, attempt_count,
            reason_code, reason_detail, retryable, updated_at
        )
        SELECT
            md5(cc.id::text || '-identity-state')::uuid,
            cc.id,
            'IDENTITY'::agent_identifier,
            cc.pipeline_status,
            0,
            CASE
                WHEN cc.pipeline_status = 'BLOCKED'::pipeline_stage_status
                    THEN lower(cc.state::text)
                ELSE 'campaign_execution'
            END,
            CASE
                WHEN cc.pipeline_status = 'BLOCKED'::pipeline_stage_status
                    THEN 'Legacy Campaign Contact terminal state.'
                ELSE 'Campaign execution is disabled until an operator enables it.'
            END,
            false,
            cc.enrolled_at
        FROM campaign_contacts AS cc
        """
    )
    op.execute(
        """
        INSERT INTO pipeline_events (
            id, campaign_contact_id, event_type, reason_code, reason_detail,
            retryable, detail, actor, occurred_at
        )
        SELECT
            md5(cc.id::text || '-enrolled-event')::uuid,
            cc.id,
            'ENROLLED'::pipeline_event_type,
            'legacy_backfill',
            'Existing Campaign membership entered the Phase 2 execution model.',
            false,
            jsonb_build_object('contact_id', cc.contact_id::text),
            'phase2-migration',
            cc.enrolled_at
        FROM campaign_contacts AS cc
        """
    )
    op.execute(
        """
        INSERT INTO pipeline_events (
            id, campaign_contact_id, agent_id, event_type, from_status,
            to_status, reason_code, reason_detail, retryable, detail, actor,
            occurred_at
        )
        SELECT
            md5(cc.id::text || '-capture-event')::uuid,
            cc.id,
            'CAPTURE'::agent_identifier,
            'STAGE_COMPLETED'::pipeline_event_type,
            'WAITING'::pipeline_stage_status,
            'COMPLETED'::pipeline_stage_status,
            'legacy_permanent_contact',
            'Existing permanent Contact backfilled as a completed Capture stage.',
            false,
            jsonb_build_object('contact_id', cc.contact_id::text),
            'phase2-migration',
            cc.enrolled_at + interval '1 microsecond'
        FROM campaign_contacts AS cc
        """
    )
    op.execute(
        """
        INSERT INTO pipeline_events (
            id, campaign_contact_id, agent_id, event_type, from_status,
            to_status, reason_code, reason_detail, retryable, detail, actor,
            occurred_at
        )
        SELECT
            md5(cc.id::text || '-identity-event')::uuid,
            cc.id,
            'IDENTITY'::agent_identifier,
            CASE
                WHEN cc.pipeline_status = 'BLOCKED'::pipeline_stage_status
                    THEN 'ELIGIBILITY_BLOCKED'::pipeline_event_type
                ELSE 'AGENT_DISABLED'::pipeline_event_type
            END,
            'WAITING'::pipeline_stage_status,
            cc.pipeline_status,
            CASE
                WHEN cc.pipeline_status = 'BLOCKED'::pipeline_stage_status
                    THEN lower(cc.state::text)
                ELSE 'campaign_execution'
            END,
            CASE
                WHEN cc.pipeline_status = 'BLOCKED'::pipeline_stage_status
                    THEN 'Legacy Campaign Contact terminal state.'
                ELSE 'Campaign execution is disabled until an operator enables it.'
            END,
            false,
            '{}'::jsonb,
            'phase2-migration',
            cc.enrolled_at + interval '2 microseconds'
        FROM campaign_contacts AS cc
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.execute(
        """
        DO $phase2_downgrade$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM contacts
                WHERE first_name IS NULL
                   OR last_name IS NULL
                   OR company_name IS NULL
                   OR company_domain IS NULL
                   OR natural_key IS NULL
            ) THEN
                RAISE EXCEPTION
                    'Phase 2 downgrade refused: unresolved Contacts do not fit the legacy schema';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM verification_jobs
                WHERE agent_id <> 'VERIFICATION'::agent_identifier
                   OR email IS NULL
                   OR policy_version IS NULL
                   OR status IN (
                       'LEASED'::verification_job_status,
                       'PAUSED'::verification_job_status
                   )
            ) THEN
                RAISE EXCEPTION
                    'Phase 2 downgrade refused: incompatible Agent jobs remain';
            END IF;
        END
        $phase2_downgrade$
        """
    )

    op.drop_index("ix_capture_campaign_filings_status", table_name="capture_campaign_filings")
    op.drop_table("capture_campaign_filings")
    op.drop_index("ix_pipeline_events_job_id", table_name="pipeline_events")
    op.drop_index("ix_pipeline_events_membership_time", table_name="pipeline_events")
    op.drop_table("pipeline_events")
    op.drop_index(
        "ix_campaign_contact_agent_states_status",
        table_name="campaign_contact_agent_states",
    )
    op.drop_table("campaign_contact_agent_states")
    op.drop_index(
        "ix_campaign_contact_sources_membership",
        table_name="campaign_contact_sources",
    )
    op.drop_table("campaign_contact_sources")

    op.drop_index("ix_verification_jobs_campaign_contact_id", table_name="verification_jobs")
    op.drop_index("ix_verification_jobs_agent_status", table_name="verification_jobs")
    op.drop_index("ix_verification_jobs_claimable", table_name="verification_jobs")
    op.create_index(
        "ix_verification_jobs_claimable",
        "verification_jobs",
        ["status", "next_run_at"],
    )
    for constraint in (
        "fk_verification_jobs_parent_job_id_verification_jobs",
        "fk_verification_jobs_capture_id_linkedin_profile_snapshots",
        "fk_verification_jobs_company_id_companies",
        "fk_verification_jobs_campaign_contact_id_campaign_contacts",
    ):
        op.drop_constraint(constraint, "verification_jobs", type_="foreignkey")
    for column in (
        "started_at",
        "parent_job_id",
        "error_class",
        "error",
        "result",
        "input_reference",
        "capture_id",
        "company_id",
        "campaign_contact_id",
        "entity_id",
        "entity_type",
        "priority",
        "task_kind",
        "agent_id",
    ):
        op.drop_column("verification_jobs", column)
    op.alter_column("verification_jobs", "max_attempts", server_default=None)
    op.alter_column(
        "verification_jobs",
        "policy_version",
        existing_type=sa.String(50),
        nullable=False,
    )
    op.alter_column("verification_jobs", "email", existing_type=sa.String(320), nullable=False)

    op.drop_index("ix_campaign_collections_collection_id", table_name="campaign_collections")
    op.drop_index("ix_campaign_collections_campaign_id", table_name="campaign_collections")
    op.drop_table("campaign_collections")
    op.drop_table("campaign_agent_overrides")
    op.drop_table("agent_controls")

    for index_name in (
        "ix_campaign_contacts_eligibility",
        "ix_campaign_contacts_current_stage",
        "ix_campaign_contacts_pipeline_status",
        "ix_campaign_contacts_membership_status",
    ):
        op.drop_index(index_name, table_name="campaign_contacts")
    op.drop_constraint(
        "fk_campaign_contacts_source_capture",
        "campaign_contacts",
        type_="foreignkey",
    )
    for column in (
        "processing_state",
        "pipeline_status",
        "next_stage",
        "latest_completed_stage",
        "current_stage",
        "desired_stage",
        "provider_state",
        "sending_state",
        "review_state",
        "qualification_state",
        "blocking_reasons",
        "eligibility_status",
        "archived_at",
        "membership_status",
        "enrolled_at",
        "enrolled_by",
        "source_reference",
        "source_kind",
        "source_capture_id",
    ):
        op.drop_column("campaign_contacts", column)

    for column in (
        "disabled_reason",
        "disabled_at",
        "enabled_at",
        "settings_version",
        "execution_enabled",
        "sending_settings",
        "cadence_config",
        "template_config",
        "primary_cta",
        "messaging_direction",
        "target_audience",
        "sender_context",
    ):
        op.drop_column("campaigns", column)
    op.drop_column("contact_labels", "updated_at")
    op.drop_column("contact_labels", "description")

    for column, column_type in (
        ("first_name", sa.String(255)),
        ("last_name", sa.String(255)),
        ("company_name", sa.String(512)),
        ("company_domain", sa.String(255)),
        ("natural_key", sa.String(1024)),
    ):
        op.alter_column(
            "contacts",
            column,
            existing_type=column_type,
            nullable=False,
        )

    for enum_name, labels in reversed(tuple(NEW_ENUMS.items())):
        postgresql.ENUM(*labels, name=enum_name).drop(bind, checkfirst=True)
    # LEASED/PAUSED and CONTACT_CREATED remain as unused labels on pre-existing
    # enums. PostgreSQL cannot safely remove individual values without rebuilding.

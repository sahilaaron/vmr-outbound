"""CI-001 Company Intelligence: versioned classifications over committed Research.

Revision ID: c41a9d78e5b2
Revises: d3b7e2f19c45
Create Date: 2026-07-31 21:10:00.000000

Eight new tables and one additive constraint on an existing one. Nothing here
alters, rewrites or reinterprets a single existing row.

* ``intelligence_taxonomies`` / ``_terms`` / ``_aliases`` — controlled, versioned
  vocabularies. A new edition is a new row, never an edit, so a classification
  stored under an old vocabulary keeps resolving after a newer one is activated.
* ``company_intelligence_versions`` — one immutable reading of one Company's
  committed Research, with the digest of its exact inputs. The unique constraint
  on ``(company_id, input_digest)`` is the idempotency guarantee: the same
  evidence under the same producer cannot make a second version, even under a
  race between two workers.
* ``company_intelligence_classifications`` — one row per classified value, so
  values are reviewable, queryable and individually evidenced rather than living
  in a JSON blob nobody can filter.
* ``company_intelligence_evidence_links`` — the lineage back into INS-001
  insights, insight evidence and dossier sections.
* ``company_intelligence_conflicts`` — disagreements kept as disagreements.
* ``company_intelligence_decisions`` — append-only operator judgements. Never an
  edit of a produced classification.
* ``company_intelligence_jobs`` / ``_backfill_runs`` / ``_backfill_items`` —
  durable, resumable, company-scoped production work, deliberately outside the
  Campaign Contact Agent queue (see docs/decisions/ADR-CI-001-pipeline-placement.md).

The one change to an existing table is
``uq_company_dossier_versions_id_company``: a redundant unique constraint on the
dossier version's ``(id, company_id)``. PostgreSQL requires a unique constraint
on exactly the referenced columns before a composite foreign key may point at
them, and this is what lets the database — rather than a service check — refuse
an intelligence version that reads another company's dossier. It constrains
nothing the primary key did not already guarantee, so it can never fail against
existing data, and the downgrade removes it cleanly.

Downgrade drops every table and every enum type this revision introduced, in
dependency order, and restores the schema exactly. No pre-existing type is
touched: all nine enum types below are new to this revision.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c41a9d78e5b2"
down_revision: str | Sequence[str] | None = "d3b7e2f19c45"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every enum type this revision creates. ``sa.Enum`` inside ``create_table``
#: creates the type implicitly, but ``drop_table`` does NOT drop it -- so a
#: downgrade that only dropped tables would leave nine orphaned types behind and
#: make the next upgrade fail with "type already exists". Naming them once here
#: keeps the round trip honest.
_ENUM_TYPES = (
    "intelligence_dimension",
    "intelligence_value_state",
    "intelligence_normalization",
    "intelligence_confidence_band",
    "intelligence_evidence_support",
    "intelligence_evidence_status",
    "intelligence_decision_action",
    "intelligence_job_status",
    "intelligence_backfill_status",
    "intelligence_backfill_outcome",
    "taxonomy_alias_source",
)


def upgrade() -> None:
    """Create the Company Intelligence schema. Purely additive."""
    # First, and deliberately: PostgreSQL refuses a composite foreign key whose
    # referenced columns have no unique constraint, and
    # `company_intelligence_versions` points at (id, company_id) on the dossier.
    # Autogenerate emits this last, which fails; ordering it here is the fix.
    op.create_unique_constraint(
        "uq_company_dossier_versions_id_company",
        "company_dossier_versions",
        ["id", "company_id"],
    )

    op.create_table(
        "company_intelligence_backfill_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PREVIEW",
                "RUNNING",
                "PAUSED",
                "COMPLETED",
                "CANCELLED",
                name="intelligence_backfill_status",
            ),
            server_default="PREVIEW",
            nullable=False,
        ),
        sa.Column("dry_run", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("batch_size", sa.Integer(), server_default="25", nullable=False),
        sa.Column("max_companies", sa.Integer(), nullable=True),
        sa.Column("cursor_company_id", sa.UUID(), nullable=True),
        sa.Column("considered_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("enqueued_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("skipped_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "skip_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("producer_version", sa.String(length=64), nullable=True),
        sa.Column("policy_version", sa.String(length=64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
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
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "batch_size >= 1 AND batch_size <= 1000",
            name=op.f("ck_company_intelligence_backfill_runs_batch_size_range"),
        ),
        sa.CheckConstraint(
            "max_companies IS NULL OR max_companies >= 1",
            name=op.f("ck_company_intelligence_backfill_runs_max_companies_positive"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_intelligence_backfill_runs")),
    )
    op.create_index(
        "ix_company_intelligence_backfill_runs_status",
        "company_intelligence_backfill_runs",
        ["status", "created_at"],
        unique=False,
    )
    op.create_table(
        "intelligence_taxonomies",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "dimension",
            sa.Enum(
                "INDUSTRY",
                "SUBINDUSTRY",
                "PRODUCT",
                "SERVICE",
                "SPECIALTY",
                "CAPABILITY",
                "GEOGRAPHY",
                "OPERATING_MARKET",
                "CUSTOMER_SEGMENT",
                "BUSINESS_MODEL",
                "COMPANY_TYPE",
                name="intelligence_dimension",
            ),
            nullable=False,
        ),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "btrim(version) <> ''", name=op.f("ck_intelligence_taxonomies_version_not_blank")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_intelligence_taxonomies")),
        sa.UniqueConstraint("dimension", "version", name="uq_intelligence_taxonomies_version"),
        sa.UniqueConstraint("id", "dimension", name="uq_intelligence_taxonomies_id_dimension"),
    )
    op.create_index(
        "uq_intelligence_taxonomies_active",
        "intelligence_taxonomies",
        ["dimension"],
        unique=True,
        postgresql_where="is_active",
    )
    op.create_table(
        "company_intelligence_jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column(
            "task_kind",
            sa.String(length=96),
            server_default="produce_company_intelligence",
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=400), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "LEASED",
                "IN_PROGRESS",
                "RETRY_SCHEDULED",
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
                name="intelligence_job_status",
            ),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("priority", sa.Integer(), server_default="100", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column(
            "next_run_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("producer_version", sa.String(length=64), nullable=True),
        sa.Column("policy_version", sa.String(length=64), nullable=True),
        sa.Column("expected_input_digest", sa.String(length=64), nullable=True),
        sa.Column("backfill_run_id", sa.UUID(), nullable=True),
        sa.Column(
            "input_reference",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_class", sa.String(length=96), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("lease_owner", sa.String(length=100), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_by", sa.String(length=255), nullable=True),
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
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "attempts >= 0", name=op.f("ck_company_intelligence_jobs_attempts_non_negative")
        ),
        sa.CheckConstraint(
            "max_attempts >= 1 AND max_attempts <= 100",
            name=op.f("ck_company_intelligence_jobs_max_attempts_range"),
        ),
        sa.ForeignKeyConstraint(
            ["backfill_run_id"],
            ["company_intelligence_backfill_runs.id"],
            name="fk_ci_jobs_backfill_run",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_company_intelligence_jobs_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_intelligence_jobs")),
        sa.UniqueConstraint("idempotency_key", name="uq_company_intelligence_jobs_idempotency_key"),
    )
    op.create_index(
        "ix_company_intelligence_jobs_backfill",
        "company_intelligence_jobs",
        ["backfill_run_id"],
        unique=False,
    )
    op.create_index(
        "ix_company_intelligence_jobs_claimable",
        "company_intelligence_jobs",
        ["status", "priority", "next_run_at"],
        unique=False,
    )
    op.create_index(
        "uq_company_intelligence_jobs_active_company",
        "company_intelligence_jobs",
        ["company_id"],
        unique=True,
        postgresql_where=(
            "status IN ("
            "'PENDING'::intelligence_job_status,"
            "'LEASED'::intelligence_job_status,"
            "'IN_PROGRESS'::intelligence_job_status,"
            "'RETRY_SCHEDULED'::intelligence_job_status)"
        ),
    )
    op.create_table(
        "intelligence_taxonomy_terms",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("taxonomy_id", sa.UUID(), nullable=False),
        sa.Column("code", sa.String(length=160), nullable=False),
        sa.Column("canonical_label", sa.String(length=255), nullable=False),
        sa.Column("normalized_label", sa.String(length=255), nullable=False),
        sa.Column("parent_id", sa.UUID(), nullable=True),
        sa.Column("depth", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "btrim(canonical_label) <> ''",
            name=op.f("ck_intelligence_taxonomy_terms_label_not_blank"),
        ),
        sa.CheckConstraint(
            "btrim(code) <> ''", name=op.f("ck_intelligence_taxonomy_terms_code_not_blank")
        ),
        sa.CheckConstraint(
            "depth >= 0", name=op.f("ck_intelligence_taxonomy_terms_depth_non_negative")
        ),
        sa.CheckConstraint(
            "parent_id <> id", name=op.f("ck_intelligence_taxonomy_terms_not_own_parent")
        ),
        sa.ForeignKeyConstraint(
            ["parent_id", "taxonomy_id"],
            ["intelligence_taxonomy_terms.id", "intelligence_taxonomy_terms.taxonomy_id"],
            name="fk_intelligence_taxonomy_terms_parent_same_taxonomy",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["taxonomy_id"],
            ["intelligence_taxonomies.id"],
            name="fk_taxonomy_terms_taxonomy",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_intelligence_taxonomy_terms")),
        sa.UniqueConstraint("id", "taxonomy_id", name="uq_intelligence_taxonomy_terms_id_taxonomy"),
        sa.UniqueConstraint("taxonomy_id", "code", name="uq_intelligence_taxonomy_terms_code"),
    )
    op.create_index(
        "ix_intelligence_taxonomy_terms_parent",
        "intelligence_taxonomy_terms",
        ["parent_id"],
        unique=False,
    )
    op.create_index(
        "ix_intelligence_taxonomy_terms_taxonomy",
        "intelligence_taxonomy_terms",
        ["taxonomy_id"],
        unique=False,
    )
    op.create_table(
        "company_intelligence_backfill_items",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("backfill_run_id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "outcome",
            sa.Enum(
                "PREVIEWED", "ENQUEUED", "SKIPPED", "FAILED", name="intelligence_backfill_outcome"
            ),
            nullable=False,
        ),
        sa.Column("skip_reason", sa.String(length=96), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome <> 'SKIPPED' OR skip_reason IS NOT NULL",
            name=op.f("ck_company_intelligence_backfill_items_skip_has_reason"),
        ),
        sa.ForeignKeyConstraint(
            ["backfill_run_id"],
            ["company_intelligence_backfill_runs.id"],
            name="fk_ci_backfill_items_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_company_intelligence_backfill_items_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["company_intelligence_jobs.id"],
            name="fk_ci_backfill_items_job",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_intelligence_backfill_items")),
        sa.UniqueConstraint(
            "backfill_run_id", "company_id", name="uq_company_intelligence_backfill_items_company"
        ),
    )
    op.create_index(
        "ix_company_intelligence_backfill_items_run",
        "company_intelligence_backfill_items",
        ["backfill_run_id", "sequence"],
        unique=False,
    )
    op.create_table(
        "intelligence_taxonomy_aliases",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("taxonomy_id", sa.UUID(), nullable=False),
        sa.Column("term_id", sa.UUID(), nullable=False),
        sa.Column("alias", sa.String(length=255), nullable=False),
        sa.Column("normalized_alias", sa.String(length=255), nullable=False),
        sa.Column(
            "source",
            sa.Enum("SEED", "OPERATOR", "MODEL_SUGGESTION", name="taxonomy_alias_source"),
            server_default="SEED",
            nullable=False,
        ),
        sa.Column("approved_by", sa.String(length=255), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "btrim(normalized_alias) <> ''",
            name=op.f("ck_intelligence_taxonomy_aliases_alias_not_blank"),
        ),
        sa.ForeignKeyConstraint(
            ["taxonomy_id"],
            ["intelligence_taxonomies.id"],
            name="fk_taxonomy_aliases_taxonomy",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["term_id", "taxonomy_id"],
            ["intelligence_taxonomy_terms.id", "intelligence_taxonomy_terms.taxonomy_id"],
            name="fk_intelligence_taxonomy_aliases_term_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_intelligence_taxonomy_aliases")),
        sa.UniqueConstraint(
            "taxonomy_id", "normalized_alias", name="uq_intelligence_taxonomy_aliases_normalized"
        ),
    )
    op.create_index(
        "ix_intelligence_taxonomy_aliases_term",
        "intelligence_taxonomy_aliases",
        ["term_id"],
        unique=False,
    )
    op.create_table(
        "company_intelligence_versions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("dossier_version_id", sa.UUID(), nullable=False),
        sa.Column("dossier_version_number", sa.Integer(), nullable=False),
        sa.Column(
            "sourced_fact_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("sourced_fact_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "taxonomy_versions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("producer", sa.String(length=255), nullable=False),
        sa.Column("producer_version", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("answer_digest", sa.String(length=64), nullable=True),
        sa.Column("classification_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("supported_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("unresolved_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("conflict_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "dimensions_addressed",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("is_current", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "btrim(producer) <> '' AND btrim(producer_version) <> ''",
            name=op.f("ck_company_intelligence_versions_producer_named"),
        ),
        sa.CheckConstraint(
            "version_number > 0",
            name=op.f("ck_company_intelligence_versions_version_number_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_company_intelligence_versions_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["dossier_version_id", "company_id"],
            ["company_dossier_versions.id", "company_dossier_versions.company_id"],
            name="fk_company_intelligence_versions_dossier_owner",
            ondelete="NO ACTION",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["company_intelligence_jobs.id"],
            name="fk_ci_versions_job",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_intelligence_versions")),
        sa.UniqueConstraint(
            "company_id", "input_digest", name="uq_company_intelligence_versions_input"
        ),
        sa.UniqueConstraint(
            "company_id", "version_number", name="uq_company_intelligence_versions_number"
        ),
        sa.UniqueConstraint("id", "company_id", name="uq_company_intelligence_versions_id_company"),
    )
    op.create_index(
        "ix_company_intelligence_versions_company",
        "company_intelligence_versions",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        "ix_company_intelligence_versions_dossier",
        "company_intelligence_versions",
        ["dossier_version_id"],
        unique=False,
    )
    op.create_index(
        "uq_company_intelligence_versions_current",
        "company_intelligence_versions",
        ["company_id"],
        unique=True,
        postgresql_where="is_current",
    )
    op.create_table(
        "company_intelligence_classifications",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("intelligence_version_id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column(
            "dimension",
            sa.Enum(
                "INDUSTRY",
                "SUBINDUSTRY",
                "PRODUCT",
                "SERVICE",
                "SPECIALTY",
                "CAPABILITY",
                "GEOGRAPHY",
                "OPERATING_MARKET",
                "CUSTOMER_SEGMENT",
                "BUSINESS_MODEL",
                "COMPANY_TYPE",
                name="intelligence_dimension",
            ),
            nullable=False,
        ),
        sa.Column("rank", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("model_value", sa.String(length=500), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("taxonomy_id", sa.UUID(), nullable=True),
        sa.Column("taxonomy_version", sa.String(length=64), nullable=True),
        sa.Column("term_id", sa.UUID(), nullable=True),
        sa.Column("term_code", sa.String(length=160), nullable=True),
        sa.Column("term_label", sa.String(length=255), nullable=True),
        sa.Column(
            "normalization",
            sa.Enum(
                "CANONICAL",
                "ALIAS",
                "UNMAPPED",
                "NOT_APPLICABLE",
                name="intelligence_normalization",
            ),
            server_default="UNMAPPED",
            nullable=False,
        ),
        sa.Column("parent_term_code", sa.String(length=160), nullable=True),
        sa.Column(
            "state",
            sa.Enum(
                "RESOLVED", "UNRESOLVED", "UNKNOWN", "CONFLICTED", name="intelligence_value_state"
            ),
            server_default="RESOLVED",
            nullable=False,
        ),
        sa.Column(
            "evidence_status",
            sa.Enum("SUPPORTED", "INSUFFICIENT", name="intelligence_evidence_status"),
            server_default="INSUFFICIENT",
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "confidence_band",
            sa.Enum("LOW", "MEDIUM", "HIGH", name="intelligence_confidence_band"),
            nullable=True,
        ),
        sa.Column("evidence_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("conflict_group", sa.Integer(), nullable=True),
        sa.Column("unresolved_reason", sa.String(length=96), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "btrim(model_value) <> ''",
            name=op.f("ck_company_intelligence_classifications_model_value_not_blank"),
        ),
        sa.CheckConstraint(
            "conflict_group IS NULL OR state = 'CONFLICTED'",
            name=op.f("ck_company_intelligence_classifications_conflict_group_state"),
        ),
        sa.CheckConstraint(
            "state <> 'RESOLVED' OR term_id IS NOT NULL OR normalization = 'NOT_APPLICABLE'",
            name=op.f("ck_company_intelligence_classifications_resolved_has_value"),
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name=op.f("ck_company_intelligence_classifications_confidence_range"),
        ),
        sa.CheckConstraint(
            "is_primary = false OR rank = 0",
            name=op.f("ck_company_intelligence_classifications_primary_is_rank_zero"),
        ),
        sa.CheckConstraint(
            "rank >= 0", name=op.f("ck_company_intelligence_classifications_rank_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["intelligence_version_id", "company_id"],
            ["company_intelligence_versions.id", "company_intelligence_versions.company_id"],
            name="fk_company_intelligence_classifications_version_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["taxonomy_id"],
            ["intelligence_taxonomies.id"],
            name="fk_ci_classifications_taxonomy",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["term_id"],
            ["intelligence_taxonomy_terms.id"],
            name="fk_ci_classifications_term",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_intelligence_classifications")),
        sa.UniqueConstraint(
            "id",
            "intelligence_version_id",
            name="uq_company_intelligence_classifications_id_version",
        ),
        sa.UniqueConstraint(
            "intelligence_version_id",
            "dimension",
            "rank",
            name="uq_company_intelligence_classifications_rank",
        ),
    )
    op.create_index(
        "ix_company_intelligence_classifications_company_dim",
        "company_intelligence_classifications",
        ["company_id", "dimension"],
        unique=False,
    )
    op.create_index(
        "ix_company_intelligence_classifications_term",
        "company_intelligence_classifications",
        ["term_id"],
        unique=False,
    )
    op.create_index(
        "ix_company_intelligence_classifications_version",
        "company_intelligence_classifications",
        ["intelligence_version_id"],
        unique=False,
    )
    op.create_table(
        "company_intelligence_conflicts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("intelligence_version_id", sa.UUID(), nullable=False),
        sa.Column(
            "dimension",
            sa.Enum(
                "INDUSTRY",
                "SUBINDUSTRY",
                "PRODUCT",
                "SERVICE",
                "SPECIALTY",
                "CAPABILITY",
                "GEOGRAPHY",
                "OPERATING_MARKET",
                "CUSTOMER_SEGMENT",
                "BUSINESS_MODEL",
                "COMPANY_TYPE",
                name="intelligence_dimension",
            ),
            nullable=False,
        ),
        sa.Column("conflict_group", sa.Integer(), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "conflict_group >= 0", name=op.f("ck_company_intelligence_conflicts_group_non_negative")
        ),
        sa.CheckConstraint(
            "member_count >= 2", name=op.f("ck_company_intelligence_conflicts_needs_two_members")
        ),
        sa.ForeignKeyConstraint(
            ["intelligence_version_id"],
            ["company_intelligence_versions.id"],
            name="fk_company_intelligence_conflicts_version",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_intelligence_conflicts")),
        sa.UniqueConstraint(
            "intelligence_version_id",
            "dimension",
            "conflict_group",
            name="uq_company_intelligence_conflicts_group",
        ),
    )
    op.create_index(
        "ix_company_intelligence_conflicts_version",
        "company_intelligence_conflicts",
        ["intelligence_version_id"],
        unique=False,
    )
    op.create_table(
        "company_intelligence_decisions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("intelligence_version_id", sa.UUID(), nullable=True),
        sa.Column("classification_id", sa.UUID(), nullable=True),
        sa.Column(
            "dimension",
            sa.Enum(
                "INDUSTRY",
                "SUBINDUSTRY",
                "PRODUCT",
                "SERVICE",
                "SPECIALTY",
                "CAPABILITY",
                "GEOGRAPHY",
                "OPERATING_MARKET",
                "CUSTOMER_SEGMENT",
                "BUSINESS_MODEL",
                "COMPANY_TYPE",
                name="intelligence_dimension",
            ),
            nullable=False,
        ),
        sa.Column("target_key", sa.String(length=320), nullable=False),
        sa.Column("target_label", sa.String(length=500), nullable=True),
        sa.Column(
            "action",
            sa.Enum(
                "CONFIRM",
                "CORRECT",
                "MARK_UNRESOLVED",
                "REJECT",
                name="intelligence_decision_action",
            ),
            nullable=False,
        ),
        sa.Column("corrected_term_id", sa.UUID(), nullable=True),
        sa.Column("corrected_term_code", sa.String(length=160), nullable=True),
        sa.Column("corrected_term_label", sa.String(length=255), nullable=True),
        sa.Column("corrected_value", sa.String(length=500), nullable=True),
        sa.Column("set_primary", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("is_current", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action <> 'CORRECT' OR corrected_term_id IS NOT NULL OR corrected_value IS NOT NULL",
            name=op.f("ck_company_intelligence_decisions_correction_has_value"),
        ),
        sa.CheckConstraint(
            "btrim(target_key) <> ''",
            name=op.f("ck_company_intelligence_decisions_target_key_not_blank"),
        ),
        sa.ForeignKeyConstraint(
            ["classification_id"],
            ["company_intelligence_classifications.id"],
            name="fk_ci_decisions_classification",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_company_intelligence_decisions_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["corrected_term_id"],
            ["intelligence_taxonomy_terms.id"],
            name="fk_ci_decisions_corrected_term",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["intelligence_version_id"],
            ["company_intelligence_versions.id"],
            name="fk_ci_decisions_version",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by_id"],
            ["company_intelligence_decisions.id"],
            name="fk_ci_decisions_superseded_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_intelligence_decisions")),
    )
    op.create_index(
        "ix_company_intelligence_decisions_classification",
        "company_intelligence_decisions",
        ["classification_id"],
        unique=False,
    )
    op.create_index(
        "ix_company_intelligence_decisions_company",
        "company_intelligence_decisions",
        ["company_id", "dimension"],
        unique=False,
    )
    op.create_index(
        "ix_company_intelligence_decisions_version",
        "company_intelligence_decisions",
        ["intelligence_version_id"],
        unique=False,
    )
    op.create_index(
        "uq_company_intelligence_decisions_current",
        "company_intelligence_decisions",
        ["company_id", "dimension", "target_key"],
        unique=True,
        postgresql_where="is_current",
    )
    op.create_table(
        "company_intelligence_evidence_links",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("classification_id", sa.UUID(), nullable=False),
        sa.Column("intelligence_version_id", sa.UUID(), nullable=False),
        sa.Column("insight_id", sa.UUID(), nullable=True),
        sa.Column("insight_evidence_id", sa.UUID(), nullable=True),
        sa.Column("dossier_section", sa.String(length=64), nullable=True),
        sa.Column("source_url", sa.String(length=1024), nullable=True),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column(
            "support",
            sa.Enum("SUPPORTS", "CONTRADICTS", name="intelligence_evidence_support"),
            server_default="SUPPORTS",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "insight_id IS NOT NULL OR source_url IS NOT NULL OR dossier_section IS NOT NULL",
            name=op.f("ck_company_intelligence_evidence_links_points_somewhere"),
        ),
        sa.ForeignKeyConstraint(
            ["classification_id", "intelligence_version_id"],
            [
                "company_intelligence_classifications.id",
                "company_intelligence_classifications.intelligence_version_id",
            ],
            name="fk_company_intelligence_evidence_links_classification_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["insight_evidence_id"],
            ["insight_evidence.id"],
            name="fk_ci_evidence_links_insight_evidence",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["insight_id"],
            ["insights.id"],
            name="fk_ci_evidence_links_insight",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_intelligence_evidence_links")),
        sa.UniqueConstraint(
            "classification_id",
            "insight_id",
            "source_url",
            name="uq_company_intelligence_evidence_links_source",
        ),
    )
    op.create_index(
        "ix_company_intelligence_evidence_links_classification",
        "company_intelligence_evidence_links",
        ["classification_id"],
        unique=False,
    )
    op.create_index(
        "ix_company_intelligence_evidence_links_insight",
        "company_intelligence_evidence_links",
        ["insight_id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the Company Intelligence schema, types included."""

    op.drop_index(
        "ix_company_intelligence_evidence_links_insight",
        table_name="company_intelligence_evidence_links",
    )
    op.drop_index(
        "ix_company_intelligence_evidence_links_classification",
        table_name="company_intelligence_evidence_links",
    )
    op.drop_table("company_intelligence_evidence_links")
    op.drop_index(
        "uq_company_intelligence_decisions_current",
        table_name="company_intelligence_decisions",
        postgresql_where="is_current",
    )
    op.drop_index(
        "ix_company_intelligence_decisions_version", table_name="company_intelligence_decisions"
    )
    op.drop_index(
        "ix_company_intelligence_decisions_company", table_name="company_intelligence_decisions"
    )
    op.drop_index(
        "ix_company_intelligence_decisions_classification",
        table_name="company_intelligence_decisions",
    )
    op.drop_table("company_intelligence_decisions")
    op.drop_index(
        "ix_company_intelligence_conflicts_version", table_name="company_intelligence_conflicts"
    )
    op.drop_table("company_intelligence_conflicts")
    op.drop_index(
        "ix_company_intelligence_classifications_version",
        table_name="company_intelligence_classifications",
    )
    op.drop_index(
        "ix_company_intelligence_classifications_term",
        table_name="company_intelligence_classifications",
    )
    op.drop_index(
        "ix_company_intelligence_classifications_company_dim",
        table_name="company_intelligence_classifications",
    )
    op.drop_table("company_intelligence_classifications")
    op.drop_index(
        "uq_company_intelligence_versions_current",
        table_name="company_intelligence_versions",
        postgresql_where="is_current",
    )
    op.drop_index(
        "ix_company_intelligence_versions_dossier", table_name="company_intelligence_versions"
    )
    op.drop_index(
        "ix_company_intelligence_versions_company", table_name="company_intelligence_versions"
    )
    op.drop_table("company_intelligence_versions")
    op.drop_index(
        "ix_intelligence_taxonomy_aliases_term", table_name="intelligence_taxonomy_aliases"
    )
    op.drop_table("intelligence_taxonomy_aliases")
    op.drop_index(
        "ix_company_intelligence_backfill_items_run",
        table_name="company_intelligence_backfill_items",
    )
    op.drop_table("company_intelligence_backfill_items")
    op.drop_index(
        "ix_intelligence_taxonomy_terms_taxonomy", table_name="intelligence_taxonomy_terms"
    )
    op.drop_index("ix_intelligence_taxonomy_terms_parent", table_name="intelligence_taxonomy_terms")
    op.drop_table("intelligence_taxonomy_terms")
    op.drop_index(
        "uq_company_intelligence_jobs_active_company",
        table_name="company_intelligence_jobs",
        postgresql_where=(
            "status IN ("
            "'PENDING'::intelligence_job_status,"
            "'LEASED'::intelligence_job_status,"
            "'IN_PROGRESS'::intelligence_job_status,"
            "'RETRY_SCHEDULED'::intelligence_job_status)"
        ),
    )
    op.drop_index("ix_company_intelligence_jobs_claimable", table_name="company_intelligence_jobs")
    op.drop_index("ix_company_intelligence_jobs_backfill", table_name="company_intelligence_jobs")
    op.drop_table("company_intelligence_jobs")
    op.drop_index(
        "uq_intelligence_taxonomies_active",
        table_name="intelligence_taxonomies",
        postgresql_where="is_active",
    )
    op.drop_table("intelligence_taxonomies")
    op.drop_index(
        "ix_company_intelligence_backfill_runs_status",
        table_name="company_intelligence_backfill_runs",
    )
    op.drop_table("company_intelligence_backfill_runs")
    # Last, mirroring the upgrade: the constraint can only go once every foreign
    # key that depends on it is gone.
    op.drop_constraint(
        "uq_company_dossier_versions_id_company",
        "company_dossier_versions",
        type_="unique",
    )

    for _type in _ENUM_TYPES:
        op.execute(sa.text(f"DROP TYPE IF EXISTS {_type}"))

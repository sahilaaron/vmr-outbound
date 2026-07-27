"""DAT-017A automatic company-domain resolution

Revision ID: d7a3f18c62b4
Revises: c48b1f70a3d2
Create Date: 2026-07-27 16:20:00.000000

Gives a captured employer a recorded, replayable domain decision instead of only
a confirmed-or-nothing flag.

Three changes:

1. Two new enum types — ``domain_resolution_state`` (confirmed / provisional /
   unresolved) and ``domain_resolution_kind`` (why a decision row exists).
2. Two existing enum types gain one label each. ``company_resolution_outcome``
   gains ``DOMAIN_PROVISIONAL``, the outcome that authorizes a promotion on
   provider-backed evidence while saying out loud that it is provisional.
   ``enrichment_confirmation_source`` gains ``AUTOMATIC_POLICY``, which records
   a confirmation the policy reached from evidence already on record.
3. ``company_domain_resolutions``: one append-only row per decision, at most one
   current row per capture, and a check constraint making "resolved, but to no
   domain" unrepresentable.

Nothing existing is rewritten. No column is dropped, no data is migrated, and
every capture that has no decision keeps behaving exactly as DAT-014 left it —
the feature switch is off by default, so this migration is inert until it is
deliberately turned on.

``ALTER TYPE ... ADD VALUE`` runs inside the migration transaction, which
PostgreSQL 12+ permits so long as the new label is not *used* in the same
transaction. It is not: no row here writes either new label.

Downgrade REFUSES once decisions exist. See :func:`downgrade`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d7a3f18c62b4"
down_revision: str | Sequence[str] | None = "c48b1f70a3d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# SQLAlchemy stores enum members by NAME (upper-case). Managed explicitly with
# create_type=False so create_table never emits a second CREATE TYPE.
_state = postgresql.ENUM(
    "CONFIRMED",
    "PROVISIONAL",
    "UNRESOLVED",
    name="domain_resolution_state",
    create_type=False,
)
_kind = postgresql.ENUM(
    "AUTOMATIC",
    "RECALCULATION",
    "OPERATOR_CORRECTION",
    name="domain_resolution_kind",
    create_type=False,
)

# The labels these two types carried BEFORE this migration. Used to rebuild them
# on downgrade, since PostgreSQL cannot remove a single enum label.
_OUTCOME_LABELS_BEFORE = (
    "PENDING_LOOKUP",
    "EXISTING_COMPANY_RESOLVED",
    "DOMAIN_CANDIDATE_CONFIRMED",
    "CANDIDATE_REVIEW_REQUIRED",
    "MULTIPLE_CANDIDATES_REVIEW_REQUIRED",
    "NO_CANDIDATE",
    "COMPANY_IDENTITY_AMBIGUOUS",
    "LOOKUP_UNAVAILABLE",
    "LEFT_UNRESOLVED",
)
_SOURCE_LABELS_BEFORE = ("CANDIDATE", "MANUAL", "UNRESOLVED", "PRIOR_MAPPING")


def upgrade() -> None:
    """Add the resolution decision table and the two new enum labels."""

    bind = op.get_bind()

    # --- 1. New enum types ----------------------------------------------------
    _state.create(bind, checkfirst=True)
    _kind.create(bind, checkfirst=True)

    # --- 2. New labels on existing types --------------------------------------
    # IF NOT EXISTS keeps a partially-applied environment recoverable rather than
    # failing on a label that is already there.
    op.execute("ALTER TYPE company_resolution_outcome ADD VALUE IF NOT EXISTS 'DOMAIN_PROVISIONAL'")
    op.execute(
        "ALTER TYPE enrichment_confirmation_source ADD VALUE IF NOT EXISTS 'AUTOMATIC_POLICY'"
    )

    # --- 3. company_domain_resolutions ----------------------------------------
    op.create_table(
        "company_domain_resolutions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("capture_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("enrichment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("resolved_company_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decision_number", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", _state, nullable=False),
        sa.Column("decision_kind", _kind, nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("company_name_original", sa.String(512), nullable=True),
        sa.Column("company_name_normalized", sa.String(512), nullable=True),
        sa.Column("candidates", postgresql.JSONB(), nullable=True),
        sa.Column("selected_domain", sa.String(255), nullable=True),
        sa.Column("selected_candidate", postgresql.JSONB(), nullable=True),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("provider_rank", sa.Integer(), nullable=True),
        sa.Column("reasons", postgresql.JSONB(), nullable=False),
        sa.Column("warnings", postgresql.JSONB(), nullable=True),
        sa.Column(
            "provider_call_made", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("correction_note", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.String(255), nullable=True),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # The capture this decision is about. CASCADE: a decision about a capture
        # that no longer exists explains nothing.
        sa.ForeignKeyConstraint(
            ["capture_id"],
            ["linkedin_profile_snapshots.id"],
            name=op.f("fk_company_domain_resolutions_capture_id_linkedin_profile_snapshots"),
            ondelete="CASCADE",
        ),
        # The DAT-010 candidate record. SET NULL, not CASCADE: losing the
        # candidate store must not delete the decision that explains a live
        # company link.
        sa.ForeignKeyConstraint(
            ["enrichment_id"],
            ["salesnav_company_enrichments.id"],
            name=op.f("fk_company_domain_resolutions_enrichment_id_salesnav_company_enrichments"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_company_id"],
            ["companies.id"],
            name=op.f("fk_company_domain_resolutions_resolved_company_id_companies"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_domain_resolutions")),
        sa.UniqueConstraint(
            "capture_id", "decision_number", name="uq_company_domain_resolutions_number"
        ),
        sa.CheckConstraint(
            "decision_number > 0",
            name="ck_company_domain_resolutions_decision_number_positive",
        ),
        # A state cannot contradict its domain. "Resolved, but to nothing" and
        # "unresolved, but here is a domain" are both unrepresentable rather than
        # merely discouraged.
        sa.CheckConstraint(
            "(state = 'UNRESOLVED' AND selected_domain IS NULL) OR "
            "(state <> 'UNRESOLVED' AND selected_domain IS NOT NULL)",
            name="ck_company_domain_resolutions_state_matches_domain",
        ),
    )
    op.create_index(
        "ix_company_domain_resolutions_capture", "company_domain_resolutions", ["capture_id"]
    )
    op.create_index("ix_company_domain_resolutions_state", "company_domain_resolutions", ["state"])
    op.create_index(
        "ix_company_domain_resolutions_company",
        "company_domain_resolutions",
        ["resolved_company_id"],
        postgresql_where=sa.text("resolved_company_id IS NOT NULL"),
    )
    # At most one live decision per capture — the database, not the service, is
    # what makes a retry idempotent.
    op.create_index(
        "uq_company_domain_resolutions_current",
        "company_domain_resolutions",
        ["capture_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )


def downgrade() -> None:
    """Reverse cleanly on an empty schema; refuse once there are decisions to lose.

    A resolution decision exists nowhere else. It is the only record of which
    evidence produced a company link, how certain that was, what the provider
    offered, and what an operator changed. Re-deriving one is not possible —
    today's evidence is not the evidence the decision was made on — so a
    downgrade that dropped them would be silent and unrecoverable.

    The refusal is conditional on there being something to protect, exactly as
    APP-003's is. A database that has never resolved a domain reverses without
    ceremony, which is what keeps the migration round-trip check meaningful.
    """

    bind = op.get_bind()

    decisions = bind.execute(
        sa.text("SELECT count(*) FROM company_domain_resolutions")
    ).scalar_one()
    if decisions:
        raise RuntimeError(
            f"DAT-017A (d7a3f18c62b4) will not downgrade while {decisions} company-domain "
            "resolution decision(s) exist. Each one records the evidence, certainty, "
            "candidates and operator corrections behind a live company link, and none of "
            "it can be re-derived — the evidence a decision was made on is not the "
            "evidence available now. Restore from a backup taken before the upgrade "
            "instead."
        )

    # A row already using either new label cannot survive the type rebuild below,
    # and losing it silently is exactly what this migration must not do.
    provisional = bind.execute(
        sa.text(
            "SELECT count(*) FROM contact_capture_promotions "
            "WHERE company_outcome = 'DOMAIN_PROVISIONAL'"
        )
    ).scalar_one()
    automatic = bind.execute(
        sa.text(
            "SELECT count(*) FROM salesnav_company_enrichments "
            "WHERE confirmation_source = 'AUTOMATIC_POLICY'"
        )
    ).scalar_one()
    if provisional or automatic:
        raise RuntimeError(
            "DAT-017A (d7a3f18c62b4) will not downgrade while records still use the "
            f"labels it added ({provisional} provisional promotion(s), {automatic} "
            "automatic confirmation(s)). Reversing would have to discard or misreport "
            "them. Resolve or correct those records first, or restore from a backup."
        )

    op.drop_index("uq_company_domain_resolutions_current", table_name="company_domain_resolutions")
    op.drop_index("ix_company_domain_resolutions_company", table_name="company_domain_resolutions")
    op.drop_index("ix_company_domain_resolutions_state", table_name="company_domain_resolutions")
    op.drop_index("ix_company_domain_resolutions_capture", table_name="company_domain_resolutions")
    op.drop_table("company_domain_resolutions")

    # PostgreSQL cannot remove one label from an enum type, so each type is
    # rebuilt without it: rename the old type aside, create the original, cast
    # the column across, drop the old. The casts above are proven safe by the
    # guard above — no row uses a label the rebuilt type lacks.
    _rebuild_enum(
        type_name="company_resolution_outcome",
        labels=_OUTCOME_LABELS_BEFORE,
        table="contact_capture_promotions",
        column="company_outcome",
        nullable=False,
    )
    _rebuild_enum(
        type_name="enrichment_confirmation_source",
        labels=_SOURCE_LABELS_BEFORE,
        table="salesnav_company_enrichments",
        column="confirmation_source",
        nullable=True,
    )

    _kind.drop(bind, checkfirst=True)
    _state.drop(bind, checkfirst=True)


def _rebuild_enum(
    *, type_name: str, labels: tuple[str, ...], table: str, column: str, nullable: bool
) -> None:
    """Recreate *type_name* with exactly *labels*, moving *table.column* across.

    A column default referencing the old type would block the cast, so any
    default is dropped and not restored: neither of these two columns carries a
    server default, and inventing one on the way down would change behaviour the
    downgrade is supposed to restore.
    """

    rendered = ", ".join(f"'{label}'" for label in labels)
    op.execute(f"ALTER TYPE {type_name} RENAME TO {type_name}_old")
    op.execute(f"CREATE TYPE {type_name} AS ENUM ({rendered})")
    op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT")
    op.execute(
        f"ALTER TABLE {table} ALTER COLUMN {column} "
        f"TYPE {type_name} USING {column}::text::{type_name}"
    )
    if not nullable:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL")
    op.execute(f"DROP TYPE {type_name}_old")

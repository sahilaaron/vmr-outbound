"""DAT-017 automatic company-domain resolution

Revision ID: 7c3a5d81be40
Revises: a5feeb1bb50a
Create Date: 2026-07-27 00:41:18.204517

Records what the versioned domain-resolution policy concluded for each company,
and why, so an automatic decision can be explained, audited and corrected.

All changes are additive to live data:

1. ``salesnav_company_enrichments`` gains the policy's conclusion — version,
   decision, ordered reason codes, the full evidence set it considered, the
   recommendation it would offer a reviewer, and when it ran. Every column is
   nullable, so the rows DAT-010 and DAT-014 already wrote stay valid and
   readable with a NULL decision meaning "the policy has not run on this one".
   The applied domain deliberately stays in ``confirmed_domain`` /
   ``confirmation_source``: an automatic decision and an operator decision are
   the same kind of fact, and every existing reader keeps working unchanged.

2. Two correction columns record an operator replacing an automatically chosen
   domain with a different one. That is the signal the policy is measured by, so
   it is stored as columns rather than left to be reconstructed from the audit
   trail — a correction rate should be a query, not an archaeology exercise.

3. Two indexes: one on the decision, which both the review queue and the metrics
   filter on; one on ``(company_key, confirmation_status)``, because
   prior-mapping reuse looks up confirmed domains by normalized company name on
   every single capture, and that is the one query which otherwise degrades as
   the CRM fills.

Two enums are widened. ``enrichment_confirmation_source`` gains
``AUTOMATIC_POLICY``; ``company_resolution_outcome`` gains
``DOMAIN_AUTO_CONFIRMED``. PostgreSQL cannot remove an enum value, so both
directions rebuild each type.

The downgrade is a genuine reversal rather than a formality: it refuses to run
while any row still depends on a value the older schema cannot express, instead
of silently rewriting an automatic decision into something an operator never
made.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "7c3a5d81be40"
down_revision: str | Sequence[str] | None = "a5feeb1bb50a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# SQLAlchemy stores enum members by NAME (upper-case).
_SOURCE_TYPE = "enrichment_confirmation_source"
_SOURCE_COLUMNS = (("salesnav_company_enrichments", "confirmation_source"),)
_SOURCES_BEFORE = ("CANDIDATE", "MANUAL", "UNRESOLVED", "PRIOR_MAPPING")
_SOURCES_AFTER = (*_SOURCES_BEFORE, "AUTOMATIC_POLICY")

_OUTCOME_TYPE = "company_resolution_outcome"
_OUTCOME_COLUMNS = (("contact_capture_promotions", "company_outcome"),)
_OUTCOMES_BEFORE = (
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
_OUTCOMES_AFTER = (
    "PENDING_LOOKUP",
    "EXISTING_COMPANY_RESOLVED",
    "DOMAIN_CANDIDATE_CONFIRMED",
    "DOMAIN_AUTO_CONFIRMED",
    "CANDIDATE_REVIEW_REQUIRED",
    "MULTIPLE_CANDIDATES_REVIEW_REQUIRED",
    "NO_CANDIDATE",
    "COMPANY_IDENTITY_AMBIGUOUS",
    "LOOKUP_UNAVAILABLE",
    "LEFT_UNRESOLVED",
)

# Managed explicitly so the ALTER TABLE never emits CREATE TYPE.
_decision = postgresql.ENUM(
    "AUTO_CONFIRMED",
    "PRIOR_MAPPING_REUSED",
    "REVIEW_REQUIRED",
    "NO_CREDIBLE_CANDIDATE",
    "CONFLICT",
    "PROVIDER_UNAVAILABLE",
    name="domain_resolution_decision",
    create_type=False,
)


def _rebuild_enum(
    type_name: str,
    columns: Sequence[tuple[str, str]],
    values: Sequence[str],
) -> None:
    """Recreate ``type_name`` with ``values`` and re-point its columns."""

    labels = ", ".join(f"'{value}'" for value in values)
    op.execute(f"ALTER TYPE {type_name} RENAME TO {type_name}_old")
    op.execute(f"CREATE TYPE {type_name} AS ENUM ({labels})")
    for table, column in columns:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} "
            f"TYPE {type_name} USING {column}::text::{type_name}"
        )
    op.execute(f"DROP TYPE {type_name}_old")


def upgrade() -> None:
    """Record the policy's conclusion alongside the domain it applied."""

    _rebuild_enum(_SOURCE_TYPE, _SOURCE_COLUMNS, _SOURCES_AFTER)
    # Neither enum column carries a server-side default — both defaults are
    # applied by SQLAlchemy in Python — so the USING cast is all the rebuild
    # needs. (A server default naming a value of the type being replaced would
    # have to be dropped and restored around it.)
    _rebuild_enum(_OUTCOME_TYPE, _OUTCOME_COLUMNS, _OUTCOMES_AFTER)

    _decision.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "salesnav_company_enrichments",
        sa.Column("resolution_policy_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "salesnav_company_enrichments",
        sa.Column("resolution_decision", _decision, nullable=True),
    )
    op.add_column(
        "salesnav_company_enrichments",
        sa.Column("resolution_reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "salesnav_company_enrichments",
        sa.Column("resolution_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "salesnav_company_enrichments",
        sa.Column("resolution_recommendation", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "salesnav_company_enrichments",
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "salesnav_company_enrichments",
        sa.Column("resolution_corrected_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "salesnav_company_enrichments",
        sa.Column("resolution_corrected_from", sa.String(length=255), nullable=True),
    )

    op.create_index(
        "ix_salesnav_company_enrichments_resolution",
        "salesnav_company_enrichments",
        ["resolution_decision"],
    )
    op.create_index(
        "ix_salesnav_company_enrichments_confirmed_key",
        "salesnav_company_enrichments",
        ["company_key", "confirmation_status"],
    )


def downgrade() -> None:
    """Remove the policy record, refusing to destroy a decision it explains."""

    bind = op.get_bind()

    # An AUTOMATIC_POLICY confirmation cannot be represented by the older enum.
    # Rewriting it to MANUAL would claim an operator typed a domain they never
    # saw, and dropping the row would delete an applied domain. Refuse instead,
    # and say exactly how to proceed.
    automatic = bind.execute(
        sa.text(
            "SELECT count(*) FROM salesnav_company_enrichments "
            "WHERE confirmation_source::text = 'AUTOMATIC_POLICY'"
        )
    ).scalar_one()
    if automatic:
        raise RuntimeError(
            f"Cannot downgrade: {automatic} company domain(s) were confirmed automatically "
            f"by the DAT-017 policy, and the earlier schema has no way to record that. "
            f"Re-confirm or clear them as an operator decision first "
            f"(confirmation_source='MANUAL' or 'UNRESOLVED'), then downgrade."
        )

    auto_outcomes = bind.execute(
        sa.text(
            "SELECT count(*) FROM contact_capture_promotions "
            "WHERE company_outcome::text = 'DOMAIN_AUTO_CONFIRMED'"
        )
    ).scalar_one()
    if auto_outcomes:
        raise RuntimeError(
            f"Cannot downgrade: {auto_outcomes} capture promotion(s) record an automatic "
            f"company resolution the earlier schema cannot express. Resolve those captures "
            f"through an operator confirmation first, then downgrade."
        )

    op.drop_index(
        "ix_salesnav_company_enrichments_confirmed_key",
        table_name="salesnav_company_enrichments",
    )
    op.drop_index(
        "ix_salesnav_company_enrichments_resolution",
        table_name="salesnav_company_enrichments",
    )

    for column in (
        "resolution_corrected_from",
        "resolution_corrected_at",
        "resolved_at",
        "resolution_recommendation",
        "resolution_evidence",
        "resolution_reasons",
        "resolution_decision",
        "resolution_policy_version",
    ):
        op.drop_column("salesnav_company_enrichments", column)

    _decision.drop(bind, checkfirst=True)

    _rebuild_enum(_OUTCOME_TYPE, _OUTCOME_COLUMNS, _OUTCOMES_BEFORE)
    _rebuild_enum(_SOURCE_TYPE, _SOURCE_COLUMNS, _SOURCES_BEFORE)

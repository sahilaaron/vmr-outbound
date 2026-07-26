"""DAT-014 capture promotion and capture-scoped domain enrichment

Revision ID: a5feeb1bb50a
Revises: 26f8ab7044f1
Create Date: 2026-07-26 11:56:31.708109

Bridges a DAT-013 contact capture to a canonical Contact through the existing
DAT-010 company-domain resolution path.

Two changes, both additive to live data:

1. The DAT-010 enrichment record becomes usable by either acquisition path. Its
   ``batch_id`` becomes nullable, a ``capture_id`` is added, and a check
   constraint enforces exactly one owner. Existing batch-owned rows are
   untouched and still satisfy the constraint. The record also gains the
   candidate provenance DAT-014 requires: the captured LinkedIn company hints,
   the normalized query, the provider and lookup version, and the rejected
   candidates.
2. A new ``contact_capture_promotions`` table records, per capture, the company
   resolution outcome and the contact promotion outcome SEPARATELY, plus the
   resolved company, the promoted contact, what carried over, and why a
   promotion did not happen. Its unique ``capture_id`` is what makes a retry
   idempotent at the database level.

``enrichment_confirmation_source`` gains ``PRIOR_MAPPING`` for a domain reused
from an earlier operator confirmation of the same company. PostgreSQL cannot
remove an enum value, so both directions rebuild the type.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a5feeb1bb50a"
down_revision: str | Sequence[str] | None = "26f8ab7044f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# SQLAlchemy stores enum members by NAME (upper-case).
_SOURCE_TYPE = "enrichment_confirmation_source"
_SOURCE_COLUMNS = (("salesnav_company_enrichments", "confirmation_source"),)
_SOURCES_BEFORE = ("CANDIDATE", "MANUAL", "UNRESOLVED")
_SOURCES_AFTER = (*_SOURCES_BEFORE, "PRIOR_MAPPING")

# Managed explicitly (create_type=False) so create_table never emits CREATE
# TYPE; each type is created and dropped by hand around the table.
_company_outcome = postgresql.ENUM(
    "PENDING_LOOKUP",
    "EXISTING_COMPANY_RESOLVED",
    "DOMAIN_CANDIDATE_CONFIRMED",
    "CANDIDATE_REVIEW_REQUIRED",
    "MULTIPLE_CANDIDATES_REVIEW_REQUIRED",
    "NO_CANDIDATE",
    "COMPANY_IDENTITY_AMBIGUOUS",
    "LOOKUP_UNAVAILABLE",
    "LEFT_UNRESOLVED",
    name="company_resolution_outcome",
    create_type=False,
)
_contact_outcome = postgresql.ENUM(
    "PENDING",
    "CONTACT_CREATED",
    "CONTACT_EXACT_MATCH_LINKED",
    "CONTACT_IDENTITY_AMBIGUOUS",
    "SUPPRESSED",
    "ALREADY_PROMOTED",
    "PROMOTION_BLOCKED",
    "PROMOTION_FAILED",
    name="contact_promotion_outcome",
    create_type=False,
)


def _rebuild_source_enum(values: Sequence[str]) -> None:
    """Recreate the confirmation-source type with ``values`` and re-point it."""

    labels = ", ".join(f"'{value}'" for value in values)
    op.execute(f"ALTER TYPE {_SOURCE_TYPE} RENAME TO {_SOURCE_TYPE}_old")
    op.execute(f"CREATE TYPE {_SOURCE_TYPE} AS ENUM ({labels})")
    for table, column in _SOURCE_COLUMNS:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} "
            f"TYPE {_SOURCE_TYPE} USING {column}::text::{_SOURCE_TYPE}"
        )
    op.execute(f"DROP TYPE {_SOURCE_TYPE}_old")


def upgrade() -> None:
    """Add the promotion table and make domain enrichment capture-aware."""

    _rebuild_source_enum(_SOURCES_AFTER)
    _company_outcome.create(op.get_bind(), checkfirst=True)
    _contact_outcome.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "contact_capture_promotions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("capture_id", sa.UUID(), nullable=False),
        sa.Column("enrichment_id", sa.UUID(), nullable=True),
        sa.Column("company_outcome", _company_outcome, nullable=False),
        sa.Column("contact_outcome", _contact_outcome, nullable=False),
        sa.Column("resolved_company_id", sa.UUID(), nullable=True),
        sa.Column("resolved_domain", sa.String(length=255), nullable=True),
        sa.Column("promoted_contact_id", sa.UUID(), nullable=True),
        sa.Column("labels_applied", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("notes_linked", sa.Integer(), nullable=False),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("promoted_by", sa.String(length=255), nullable=True),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
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
            name=op.f("fk_contact_capture_promotions_capture_id_linkedin_profile_snapshots"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["enrichment_id"],
            ["salesnav_company_enrichments.id"],
            name=op.f("fk_contact_capture_promotions_enrichment_id_salesnav_company_enrichments"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["promoted_contact_id"],
            ["contacts.id"],
            name=op.f("fk_contact_capture_promotions_promoted_contact_id_contacts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_company_id"],
            ["companies.id"],
            name=op.f("fk_contact_capture_promotions_resolved_company_id_companies"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contact_capture_promotions")),
        sa.UniqueConstraint("capture_id", name="uq_contact_capture_promotions_capture_id"),
    )
    op.create_index(
        "ix_contact_capture_promotions_company_id",
        "contact_capture_promotions",
        ["resolved_company_id"],
        unique=False,
    )
    op.create_index(
        "ix_contact_capture_promotions_contact_id",
        "contact_capture_promotions",
        ["promoted_contact_id"],
        unique=False,
    )

    op.add_column("salesnav_company_enrichments", sa.Column("capture_id", sa.UUID(), nullable=True))
    op.add_column(
        "salesnav_company_enrichments",
        sa.Column("company_linkedin_url", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "salesnav_company_enrichments",
        sa.Column("company_linkedin_id", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "salesnav_company_enrichments",
        sa.Column("location_hint", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "salesnav_company_enrichments",
        sa.Column("rejected_candidates", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "salesnav_company_enrichments",
        sa.Column("normalized_query", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "salesnav_company_enrichments", sa.Column("provider", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "salesnav_company_enrichments",
        sa.Column("lookup_version", sa.String(length=64), nullable=True),
    )
    op.alter_column(
        "salesnav_company_enrichments", "batch_id", existing_type=sa.UUID(), nullable=True
    )
    op.create_index(
        "uq_salesnav_company_enrichments_capture",
        "salesnav_company_enrichments",
        ["capture_id"],
        unique=True,
        postgresql_where="capture_id IS NOT NULL",
    )
    op.create_foreign_key(
        op.f("fk_salesnav_company_enrichments_capture_id_linkedin_profile_snapshots"),
        "salesnav_company_enrichments",
        "linkedin_profile_snapshots",
        ["capture_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_salesnav_company_enrichments_single_owner",
        "salesnav_company_enrichments",
        "(batch_id IS NULL) <> (capture_id IS NULL)",
    )


def downgrade() -> None:
    """Remove the promotion table and the capture-scoped enrichment support.

    Capture-owned enrichment rows are deleted first: the restored schema has no
    ``capture_id`` and requires ``batch_id``, so those rows would be both
    unrepresentable and unreachable. Batch-owned rows — every DAT-010 record —
    are untouched. Any surviving ``PRIOR_MAPPING`` decision is mapped back to
    ``MANUAL``, the closest truthful pre-DAT-014 meaning (a domain that did not
    come from clicking a provider candidate).
    """

    op.execute("DELETE FROM salesnav_company_enrichments WHERE capture_id IS NOT NULL")
    op.drop_constraint(
        "ck_salesnav_company_enrichments_single_owner",
        "salesnav_company_enrichments",
        type_="check",
    )
    op.drop_constraint(
        op.f("fk_salesnav_company_enrichments_capture_id_linkedin_profile_snapshots"),
        "salesnav_company_enrichments",
        type_="foreignkey",
    )
    op.drop_index(
        "uq_salesnav_company_enrichments_capture",
        table_name="salesnav_company_enrichments",
        postgresql_where="capture_id IS NOT NULL",
    )
    op.alter_column(
        "salesnav_company_enrichments", "batch_id", existing_type=sa.UUID(), nullable=False
    )
    op.drop_column("salesnav_company_enrichments", "lookup_version")
    op.drop_column("salesnav_company_enrichments", "provider")
    op.drop_column("salesnav_company_enrichments", "normalized_query")
    op.drop_column("salesnav_company_enrichments", "rejected_candidates")
    op.drop_column("salesnav_company_enrichments", "location_hint")
    op.drop_column("salesnav_company_enrichments", "company_linkedin_id")
    op.drop_column("salesnav_company_enrichments", "company_linkedin_url")
    op.drop_column("salesnav_company_enrichments", "capture_id")

    op.drop_index(
        "ix_contact_capture_promotions_contact_id", table_name="contact_capture_promotions"
    )
    op.drop_index(
        "ix_contact_capture_promotions_company_id", table_name="contact_capture_promotions"
    )
    op.drop_table("contact_capture_promotions")
    _contact_outcome.drop(op.get_bind(), checkfirst=True)
    _company_outcome.drop(op.get_bind(), checkfirst=True)

    op.execute(
        "UPDATE salesnav_company_enrichments SET confirmation_source = 'MANUAL' "
        "WHERE confirmation_source = 'PRIOR_MAPPING'"
    )
    _rebuild_source_enum(_SOURCES_BEFORE)

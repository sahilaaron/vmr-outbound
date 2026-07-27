"""INS-001 shared evidence and insight model.

Revision ID: e61f4c2b7a90
Revises: c48b1f70a3d2
Create Date: 2026-07-27 20:30:00.000000

Turns the DAT-001 insight stubs into a provider-neutral evidence boundary:
claims belong to exactly one permanent Company or Contact, claims declare
whether they are fact or interpretation and supported/conflicting/unknown, and
source observations retain retrieval, confidence, freshness, extraction and
raw-record lineage separately from the claim.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e61f4c2b7a90"
down_revision: str | Sequence[str] | None = "c48b1f70a3d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_insight_kind = postgresql.ENUM(
    "FACT",
    "INTERPRETATION",
    name="insight_kind",
    create_type=False,
)
_insight_state = postgresql.ENUM(
    "SUPPORTED",
    "CONFLICTING",
    "UNKNOWN",
    name="insight_state",
    create_type=False,
)


def upgrade() -> None:
    """Add the shared claim/evidence contract without rewriting legacy data."""

    # Existing rows must already identify one real subject. Guessing an owner
    # during migration would turn bad data into authoritative research.
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                      FROM insights
                     WHERE NOT (
                         (subject = 'COMPANY' AND company_id IS NOT NULL AND contact_id IS NULL)
                         OR
                         (subject = 'CONTACT' AND contact_id IS NOT NULL AND company_id IS NULL)
                     )
                ) THEN
                    RAISE EXCEPTION
                        'INS-001 cannot infer the owner of existing insight rows';
                END IF;
            END
            $$;
            """
        )
    )

    _insight_kind.create(op.get_bind(), checkfirst=True)
    _insight_state.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "insights",
        sa.Column("kind", _insight_kind, nullable=False, server_default="FACT"),
    )
    op.add_column(
        "insights",
        sa.Column("state", _insight_state, nullable=False, server_default="SUPPORTED"),
    )
    op.add_column(
        "insights",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("insights", sa.Column("created_by", sa.String(255), nullable=True))
    op.add_column("insights", sa.Column("idempotency_key", sa.String(255), nullable=True))
    op.add_column("insights", sa.Column("content_hash", sa.String(64), nullable=True))
    op.create_index("ix_insights_state", "insights", ["state"])
    op.create_check_constraint(
        "insight_exactly_one_subject",
        "insights",
        "(subject = 'COMPANY' AND company_id IS NOT NULL AND contact_id IS NULL) "
        "OR (subject = 'CONTACT' AND contact_id IS NOT NULL AND company_id IS NULL)",
    )
    op.create_check_constraint(
        "insight_claim_not_blank",
        "insights",
        "btrim(claim) <> ''",
    )
    op.create_check_constraint(
        "insight_version_positive",
        "insights",
        "version > 0",
    )
    op.create_unique_constraint(
        "uq_insights_company_idempotency",
        "insights",
        ["company_id", "idempotency_key"],
    )
    op.create_unique_constraint(
        "uq_insights_contact_idempotency",
        "insights",
        ["contact_id", "idempotency_key"],
    )
    op.alter_column("insights", "kind", server_default=None)
    op.alter_column("insights", "state", server_default=None)
    op.alter_column("insights", "version", server_default=None)

    op.add_column(
        "insight_evidence",
        sa.Column("source_title", sa.String(1024), nullable=True),
    )
    op.add_column(
        "insight_evidence",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("insight_evidence", sa.Column("evidence_summary", sa.Text(), nullable=True))
    op.add_column(
        "insight_evidence",
        sa.Column("extraction_method", sa.String(255), nullable=True),
    )
    op.add_column(
        "insight_evidence",
        sa.Column("freshness_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "insight_evidence",
        sa.Column("source_record_type", sa.String(100), nullable=True),
    )
    op.add_column(
        "insight_evidence",
        sa.Column("source_record_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "insight_evidence",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_check_constraint(
        "insight_evidence_confidence_range",
        "insight_evidence",
        "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
    )
    op.create_check_constraint(
        "insight_evidence_version_positive",
        "insight_evidence",
        "version > 0",
    )
    op.create_check_constraint(
        "insight_evidence_source_record_pair",
        "insight_evidence",
        "(source_record_type IS NULL AND source_record_id IS NULL) "
        "OR (source_record_type IS NOT NULL AND source_record_id IS NOT NULL)",
    )
    op.create_unique_constraint(
        "uq_insight_evidence_source_version",
        "insight_evidence",
        ["insight_id", "source_url", "version"],
    )
    op.alter_column("insight_evidence", "version", server_default=None)


def downgrade() -> None:
    """Remove INS-001 additions while preserving the original DAT-001 stubs."""

    op.drop_constraint(
        "uq_insight_evidence_source_version",
        "insight_evidence",
        type_="unique",
    )
    op.drop_constraint(
        "ck_insight_evidence_insight_evidence_source_record_pair",
        "insight_evidence",
        type_="check",
    )
    op.drop_constraint(
        "ck_insight_evidence_insight_evidence_version_positive",
        "insight_evidence",
        type_="check",
    )
    op.drop_constraint(
        "ck_insight_evidence_insight_evidence_confidence_range",
        "insight_evidence",
        type_="check",
    )
    for column in (
        "version",
        "source_record_id",
        "source_record_type",
        "freshness_at",
        "extraction_method",
        "evidence_summary",
        "published_at",
        "source_title",
    ):
        op.drop_column("insight_evidence", column)

    op.drop_constraint(
        "ck_insights_insight_version_positive",
        "insights",
        type_="check",
    )
    op.drop_constraint(
        "ck_insights_insight_claim_not_blank",
        "insights",
        type_="check",
    )
    op.drop_constraint(
        "ck_insights_insight_exactly_one_subject",
        "insights",
        type_="check",
    )
    op.drop_index("ix_insights_state", table_name="insights")
    op.drop_constraint("uq_insights_contact_idempotency", "insights", type_="unique")
    op.drop_constraint("uq_insights_company_idempotency", "insights", type_="unique")
    for column in (
        "content_hash",
        "idempotency_key",
        "created_by",
        "version",
        "state",
        "kind",
    ):
        op.drop_column("insights", column)

    _insight_state.drop(op.get_bind(), checkfirst=True)
    _insight_kind.drop(op.get_bind(), checkfirst=True)

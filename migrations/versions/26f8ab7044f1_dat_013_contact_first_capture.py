"""DAT-013 contact-first capture submissions, labels and notes

Revision ID: 26f8ab7044f1
Revises: e7a91c3f6b24
Create Date: 2026-07-26 10:18:11.878904

Turns the capture path contact-first. It adds the submission anchor the
extension posts to, the backend-owned label registry and its contact
assignments, append-only operator notes, the contact-first columns on the
existing capture-evidence table, and one new truthful outcome
(``duplicate_in_submission``).

Nothing here belongs to a campaign, and nothing here can make a contact
outreach-eligible. The existing ``campaign_id`` column on
``linkedin_profile_snapshots`` is deliberately left in place (nullable) so
legacy campaign-era captures remain readable.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "26f8ab7044f1"
down_revision: str | Sequence[str] | None = "e7a91c3f6b24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# SQLAlchemy stores enum members by NAME (upper-case). PostgreSQL cannot remove
# a value from an enum type, so both directions rebuild the type and re-point
# every column at it — which keeps the migration genuinely reversible.
_OUTCOME_TYPE = "linkedin_snapshot_outcome"
_OUTCOME_COLUMNS = (
    ("linkedin_profile_snapshots", "outcome"),
    ("linkedin_company_snapshots", "outcome"),
)
_OUTCOMES_BEFORE = (
    "STORED",
    "EXACT_MATCH_REFRESHED",
    "EXACT_MATCH_UNCHANGED",
    "UNMATCHED_STAGED",
    "AMBIGUOUS_REVIEW",
    "SUPPRESSED",
)
_OUTCOMES_AFTER = (*_OUTCOMES_BEFORE, "DUPLICATE_IN_SUBMISSION")


def _rebuild_outcome_enum(values: Sequence[str]) -> None:
    """Recreate the outcome type with ``values`` and re-point both columns."""

    labels = ", ".join(f"'{value}'" for value in values)
    op.execute(f"ALTER TYPE {_OUTCOME_TYPE} RENAME TO {_OUTCOME_TYPE}_old")
    op.execute(f"CREATE TYPE {_OUTCOME_TYPE} AS ENUM ({labels})")
    for table, column in _OUTCOME_COLUMNS:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} "
            f"TYPE {_OUTCOME_TYPE} USING {column}::text::{_OUTCOME_TYPE}"
        )
    op.execute(f"DROP TYPE {_OUTCOME_TYPE}_old")


def upgrade() -> None:
    """Add the contact-first tables, columns, and the duplicate outcome."""
    op.create_table(
        "contact_capture_submissions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("client_submission_id", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("capture_mode", sa.String(length=48), nullable=False),
        sa.Column("extension_version", sa.String(length=64), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("contact_count", sa.Integer(), nullable=False),
        sa.Column("requested_labels", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("operator_note", sa.Text(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contact_capture_submissions")),
        sa.UniqueConstraint(
            "client_submission_id", name="uq_contact_capture_submissions_client_submission_id"
        ),
    )
    op.create_index(
        "ix_contact_capture_submissions_received_at",
        "contact_capture_submissions",
        ["received_at"],
        unique=False,
    )
    op.create_table(
        "contact_labels",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("slug", sa.String(length=96), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contact_labels")),
        sa.UniqueConstraint("slug", name="uq_contact_labels_slug"),
    )
    op.create_table(
        "contact_capture_notes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("capture_id", sa.UUID(), nullable=False),
        sa.Column("submission_id", sa.UUID(), nullable=True),
        sa.Column("contact_id", sa.UUID(), nullable=True),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("note_text", sa.Text(), nullable=False),
        sa.Column("author", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["capture_id"],
            ["linkedin_profile_snapshots.id"],
            name=op.f("fk_contact_capture_notes_capture_id_linkedin_profile_snapshots"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name=op.f("fk_contact_capture_notes_contact_id_contacts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["contact_capture_submissions.id"],
            name=op.f("fk_contact_capture_notes_submission_id_contact_capture_submissions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contact_capture_notes")),
    )
    op.create_index(
        "ix_contact_capture_notes_capture_id",
        "contact_capture_notes",
        ["capture_id"],
        unique=False,
    )
    op.create_index(
        "ix_contact_capture_notes_contact_id",
        "contact_capture_notes",
        ["contact_id"],
        unique=False,
    )
    op.create_table(
        "contact_label_assignments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("contact_id", sa.UUID(), nullable=False),
        sa.Column("label_id", sa.UUID(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("capture_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["capture_id"],
            ["linkedin_profile_snapshots.id"],
            name=op.f("fk_contact_label_assignments_capture_id_linkedin_profile_snapshots"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name=op.f("fk_contact_label_assignments_contact_id_contacts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["label_id"],
            ["contact_labels.id"],
            name=op.f("fk_contact_label_assignments_label_id_contact_labels"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contact_label_assignments")),
        sa.UniqueConstraint(
            "contact_id", "label_id", name="uq_contact_label_assignments_contact_id"
        ),
    )
    op.create_index(
        "ix_contact_label_assignments_label_id",
        "contact_label_assignments",
        ["label_id"],
        unique=False,
    )

    op.add_column(
        "linkedin_profile_snapshots", sa.Column("submission_id", sa.UUID(), nullable=True)
    )
    op.add_column(
        "linkedin_profile_snapshots", sa.Column("capture_mode", sa.String(length=48), nullable=True)
    )
    op.add_column(
        "linkedin_profile_snapshots",
        sa.Column("source_surface", sa.String(length=48), nullable=True),
    )
    op.add_column(
        "linkedin_profile_snapshots",
        sa.Column("salesnav_lead_url", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "linkedin_profile_snapshots",
        sa.Column("operator_labels", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "linkedin_profile_snapshots", sa.Column("duplicate_of_id", sa.UUID(), nullable=True)
    )
    op.create_index(
        "ix_li_profile_snapshots_submission_id",
        "linkedin_profile_snapshots",
        ["submission_id"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_linkedin_profile_snapshots_duplicate_of_id_linkedin_profile_snapshots"),
        "linkedin_profile_snapshots",
        "linkedin_profile_snapshots",
        ["duplicate_of_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_linkedin_profile_snapshots_submission_id_contact_capture_submissions"),
        "linkedin_profile_snapshots",
        "contact_capture_submissions",
        ["submission_id"],
        ["id"],
        ondelete="SET NULL",
    )

    _rebuild_outcome_enum(_OUTCOMES_AFTER)


def downgrade() -> None:
    """Remove the contact-first tables, columns, and the duplicate outcome.

    Any capture recorded as ``duplicate_in_submission`` is mapped back to
    ``stored`` first: PostgreSQL cannot keep a value the restored type does not
    declare. The evidence rows themselves are untouched by that remap.
    """

    op.execute(
        "UPDATE linkedin_profile_snapshots SET outcome = 'STORED' "
        "WHERE outcome = 'DUPLICATE_IN_SUBMISSION'"
    )
    op.execute(
        "UPDATE linkedin_company_snapshots SET outcome = 'STORED' "
        "WHERE outcome = 'DUPLICATE_IN_SUBMISSION'"
    )
    _rebuild_outcome_enum(_OUTCOMES_BEFORE)

    op.drop_constraint(
        op.f("fk_linkedin_profile_snapshots_submission_id_contact_capture_submissions"),
        "linkedin_profile_snapshots",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_linkedin_profile_snapshots_duplicate_of_id_linkedin_profile_snapshots"),
        "linkedin_profile_snapshots",
        type_="foreignkey",
    )
    op.drop_index("ix_li_profile_snapshots_submission_id", table_name="linkedin_profile_snapshots")
    op.drop_column("linkedin_profile_snapshots", "duplicate_of_id")
    op.drop_column("linkedin_profile_snapshots", "operator_labels")
    op.drop_column("linkedin_profile_snapshots", "salesnav_lead_url")
    op.drop_column("linkedin_profile_snapshots", "source_surface")
    op.drop_column("linkedin_profile_snapshots", "capture_mode")
    op.drop_column("linkedin_profile_snapshots", "submission_id")
    op.drop_index("ix_contact_label_assignments_label_id", table_name="contact_label_assignments")
    op.drop_table("contact_label_assignments")
    op.drop_index("ix_contact_capture_notes_contact_id", table_name="contact_capture_notes")
    op.drop_index("ix_contact_capture_notes_capture_id", table_name="contact_capture_notes")
    op.drop_table("contact_capture_notes")
    op.drop_table("contact_labels")
    op.drop_index(
        "ix_contact_capture_submissions_received_at", table_name="contact_capture_submissions"
    )
    op.drop_table("contact_capture_submissions")

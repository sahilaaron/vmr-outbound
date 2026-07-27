"""APP-003 company workspace and dossier-ready data model

Revision ID: c48b1f70a3d2
Revises: a5feeb1bb50a
Create Date: 2026-07-27 11:40:00.000000

Gives the permanent Company an identity beyond a domain string, a real edge to
its Contacts, a provenance ledger for its canonical fields, and a place for
research to land without overwriting anything.

Four changes:

1. ``companies`` gains LinkedIn identity (URL + id), a ``research_state`` and a
   ``last_researched_at``. Every column is additive; the two firmographic
   columns already there are untouched.
2. ``contacts`` gains a nullable ``company_id`` foreign key. ``company_domain``
   is deliberately left NOT NULL and unchanged: it is dedup input, it is the
   captured evidence of what the source said, and legacy rows depend on it. The
   backfill below sets ``company_id`` only where the existing domain join is
   unambiguous.
3. ``company_field_values`` records every observation of every canonical company
   field, with exactly one current winner per (company, field) enforced by a
   partial unique index.
4. ``company_research_submissions`` and ``company_dossier_versions`` store raw
   research payloads and immutable structured readings of them, with at most one
   current version per company enforced by a partial unique index.

The ``research_state`` enum type is created here. It has existed in Python since
APP-002 but was only ever computed, never stored, so PostgreSQL has no such type
yet.

Downgrade REFUSES rather than running. See the note on :func:`downgrade`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c48b1f70a3d2"
down_revision: str | Sequence[str] | None = "a5feeb1bb50a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# SQLAlchemy stores enum members by NAME (upper-case). Managed explicitly with
# create_type=False so create_table never emits a second CREATE TYPE.
_research_state = postgresql.ENUM(
    "NOT_REQUESTED",
    "QUEUED",
    "RUNNING",
    "COMPLETED",
    "COMPLETED_WITH_WARNINGS",
    "FAILED",
    "STALE",
    name="research_state",
    create_type=False,
)
_company_field_source = postgresql.ENUM(
    "MANUAL",
    "LINKEDIN_COMPANY_SNAPSHOT",
    "CAPTURE_PROMOTION",
    "RESEARCH_DOSSIER",
    "IMPORT",
    name="company_field_source",
    create_type=False,
)


# The backfill, as one statement so it is atomic with the rest of the migration.
#
# A contact is linked ONLY when exactly one company carries its domain. The
# correlated COUNT is what enforces that: two companies sharing a domain is
# possible today (the unique index is partial and only covers non-NULL domains,
# and historical rows predate it), and picking one of them would be a guess
# dressed up as a migration.
#
# Left NULL on purpose, in every one of these cases:
#   * no company has that domain;
#   * more than one company has that domain;
#   * the contact's domain is blank or whitespace.
# An unlinked contact is visible and reviewable afterwards. A wrongly linked one
# is neither.
#
# ``company_id IS NULL`` makes the statement idempotent and non-destructive: a
# rerun cannot overwrite a link that already exists, whoever set it.
_BACKFILL = sa.text(
    """
    UPDATE contacts AS c
       SET company_id = (
               SELECT co.id
                 FROM companies AS co
                WHERE co.domain = c.company_domain
                LIMIT 1
           )
     WHERE c.company_id IS NULL
       AND c.company_domain IS NOT NULL
       AND btrim(c.company_domain) <> ''
       AND (
               SELECT count(*)
                 FROM companies AS co2
                WHERE co2.domain = c.company_domain
           ) = 1
    """
)


def upgrade() -> None:
    """Add company identity, the contact edge, provenance and dossier storage."""

    # --- 1. The research_state enum type -------------------------------------
    _research_state.create(op.get_bind(), checkfirst=True)
    _company_field_source.create(op.get_bind(), checkfirst=True)

    # --- 2. companies --------------------------------------------------------
    op.add_column("companies", sa.Column("linkedin_company_url", sa.String(512), nullable=True))
    op.add_column("companies", sa.Column("linkedin_company_id", sa.String(256), nullable=True))
    op.add_column(
        "companies",
        sa.Column(
            "research_state",
            _research_state,
            nullable=False,
            server_default="NOT_REQUESTED",
        ),
    )
    op.add_column(
        "companies",
        sa.Column("last_researched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_companies_linkedin_company_id",
        "companies",
        ["linkedin_company_id"],
        postgresql_where=sa.text("linkedin_company_id IS NOT NULL"),
    )
    op.create_index("ix_companies_research_state", "companies", ["research_state"])

    # --- 3. contacts.company_id + backfill -----------------------------------
    op.add_column(
        "contacts",
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_contacts_company_id_companies",
        "contacts",
        "companies",
        ["company_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_contacts_company_id",
        "contacts",
        ["company_id"],
        postgresql_where=sa.text("company_id IS NOT NULL"),
    )
    op.execute(_BACKFILL)

    # --- 4. company_field_values ---------------------------------------------
    op.create_table(
        "company_field_values",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("field_name", sa.String(64), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("source_kind", _company_field_source, nullable=False),
        sa.Column("source_reference", sa.String(1024), nullable=True),
        sa.Column("dossier_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "is_manual_override",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "is_current_winner",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("policy_version", sa.String(50), nullable=False),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_company_field_values_company_id_companies",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_company_field_values_company_field",
        "company_field_values",
        ["company_id", "field_name"],
    )
    op.create_index(
        "uq_company_field_values_winner",
        "company_field_values",
        ["company_id", "field_name"],
        unique=True,
        postgresql_where=sa.text("is_current_winner"),
    )
    op.create_index(
        "ix_company_field_values_dossier",
        "company_field_values",
        ["dossier_version_id"],
        postgresql_where=sa.text("dossier_version_id IS NOT NULL"),
    )

    # --- 5. company_research_submissions -------------------------------------
    op.create_table(
        "company_research_submissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("producer", sa.String(255), nullable=False),
        sa.Column("producer_version", sa.String(64), nullable=True),
        sa.Column("submitted_by", sa.String(255), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(128), nullable=False),
        sa.Column("request_context", postgresql.JSONB(), nullable=True),
        sa.Column(
            "submitted_at",
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
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_company_research_submissions_company_id_companies",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "company_id",
            "content_hash",
            name="uq_company_research_submissions_content",
        ),
    )
    op.create_index(
        "ix_company_research_submissions_company",
        "company_research_submissions",
        ["company_id"],
    )

    # --- 6. company_dossier_versions -----------------------------------------
    op.create_table(
        "company_dossier_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("submission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("interpreter", sa.String(255), nullable=False),
        sa.Column("interpreter_version", sa.String(64), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("overview", postgresql.JSONB(), nullable=True),
        sa.Column("products_services", postgresql.JSONB(), nullable=True),
        sa.Column("industries", postgresql.JSONB(), nullable=True),
        sa.Column("geography", postgresql.JSONB(), nullable=True),
        sa.Column("leadership", postgresql.JSONB(), nullable=True),
        sa.Column("activity_signals", postgresql.JSONB(), nullable=True),
        sa.Column("public_contacts", postgresql.JSONB(), nullable=True),
        sa.Column("sources", postgresql.JSONB(), nullable=True),
        sa.Column("unknowns", postgresql.JSONB(), nullable=True),
        sa.Column("warnings", postgresql.JSONB(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "is_current",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_company_dossier_versions_company_id_companies",
            ondelete="CASCADE",
        ),
        # RESTRICT: an interpretation cannot outlive the payload it interprets,
        # or it becomes an unfalsifiable claim.
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["company_research_submissions.id"],
            name="fk_company_dossier_versions_submission",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "company_id",
            "version_number",
            name="uq_company_dossier_versions_number",
        ),
        sa.CheckConstraint("version_number > 0", name="ck_company_dossier_version_positive"),
    )
    op.create_index(
        "ix_company_dossier_versions_company",
        "company_dossier_versions",
        ["company_id"],
    )
    op.create_index(
        "ix_company_dossier_versions_submission",
        "company_dossier_versions",
        ["submission_id"],
    )
    op.create_index(
        "uq_company_dossier_versions_current",
        "company_dossier_versions",
        ["company_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )

    # The dossier link on the provenance ledger can only be added once the
    # dossier table exists.
    op.create_foreign_key(
        "fk_company_field_values_dossier_version",
        "company_field_values",
        "company_dossier_versions",
        ["dossier_version_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Reverse cleanly on an empty schema; refuse once there is data to lose.

    Three kinds of record here exist nowhere else in the database: the resolved
    contact-to-company links, the company field provenance ledger, and every raw
    research submission with every interpretation of it. Dropping those would be
    silent and unrecoverable, and no automatic re-derivation is honest — a
    rebuilt link is a guess, not the decision somebody made.

    So the refusal is conditional on there being something to protect. A
    development database that has never held a company workspace reverses
    without ceremony, which is also what keeps the migration round-trip test
    meaningful. A database that has held one stops and says what to do instead.
    """

    bind = op.get_bind()
    populated: list[str] = []
    for table, label in (
        ("company_dossier_versions", "dossier version(s)"),
        ("company_research_submissions", "raw research submission(s)"),
        ("company_field_values", "company field observation(s)"),
    ):
        count = bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()
        if count:
            populated.append(f"{count} {label}")
    linked = bind.execute(
        sa.text("SELECT count(*) FROM contacts WHERE company_id IS NOT NULL")
    ).scalar_one()
    if linked:
        populated.append(f"{linked} contact-to-company link(s)")

    if populated:
        raise RuntimeError(
            "APP-003 (c48b1f70a3d2) will not downgrade while the company workspace holds "
            "data that exists nowhere else: " + ", ".join(populated) + ". Reversing would "
            "destroy it silently and it cannot be re-derived — a rebuilt link is a guess, "
            "not the decision that was made. Restore from a backup taken before the "
            "upgrade instead."
        )

    op.drop_constraint(
        "fk_company_field_values_dossier_version",
        "company_field_values",
        type_="foreignkey",
    )
    op.drop_table("company_dossier_versions")
    op.drop_table("company_research_submissions")
    op.drop_table("company_field_values")

    op.drop_index("ix_contacts_company_id", table_name="contacts")
    op.drop_constraint("fk_contacts_company_id_companies", "contacts", type_="foreignkey")
    op.drop_column("contacts", "company_id")

    op.drop_index("ix_companies_research_state", table_name="companies")
    op.drop_index("ix_companies_linkedin_company_id", table_name="companies")
    op.drop_column("companies", "last_researched_at")
    op.drop_column("companies", "research_state")
    op.drop_column("companies", "linkedin_company_id")
    op.drop_column("companies", "linkedin_company_url")

    _company_field_source.drop(bind, checkfirst=True)
    _research_state.drop(bind, checkfirst=True)

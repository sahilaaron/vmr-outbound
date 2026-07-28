"""DAT-019 LinkedIn identity links and the Sales Navigator member identifier.

Revision ID: c3f5a71d9b42
Revises: b8e5d34a91c7
Create Date: 2026-07-28

Adds the join between a person's two LinkedIn identifier forms, the columns that
carry the Sales Navigator member id and the provenance of a stored profile URL,
and a deterministic flag over the rows captured before the derivation stopped.

**Nothing existing is rewritten.** The back-fill only inserts new rows and sets
new columns. Every previously stored ``normalized_profile_url`` and
``contacts.linkedin_url`` is left exactly as it was, because those values are
acquisition evidence: a legacy ``/in/<lowercased-member-id>`` is flagged so it
stops acting as a canonical identity, and is then left alone for an operator to
resolve on real evidence. Flagging a row is not a claim that it was verified or
corrected.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3f5a71d9b42"
down_revision: str | Sequence[str] | None = "b8e5d34a91c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Rows captured before DAT-019 whose profile URL is really the member id from
# their own lead URL. Compared case-insensitively because ingest lowercased the
# slug; anchored on the lead URL so a genuine handle is never flagged.
_LEGACY_ALIAS_PREDICATE = """
    salesnav_lead_url IS NOT NULL
    AND normalized_profile_url IS NOT NULL
    AND lower(split_part(regexp_replace(normalized_profile_url, '^.*/in/', ''), '/', 1))
        = lower(split_part(split_part(
            regexp_replace(salesnav_lead_url, '^.*/sales/lead/', ''), '?', 1), ',', 1))
"""


def upgrade() -> None:
    op.add_column(
        "linkedin_profile_snapshots",
        sa.Column("salesnav_member_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "linkedin_profile_snapshots",
        sa.Column("profile_url_source", sa.String(length=32), nullable=True),
    )
    op.create_index(
        "ix_li_profile_snapshots_salesnav_member_id",
        "linkedin_profile_snapshots",
        ["salesnav_member_id"],
    )

    op.create_table(
        "linkedin_identity_links",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "contact_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("identifier_kind", sa.String(length=32), nullable=False),
        sa.Column("identifier_value", sa.String(length=512), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("decision_kind", sa.String(length=32), nullable=False),
        sa.Column(
            "suspected_alias",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "capture_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("linkedin_profile_snapshots.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_surface", sa.String(length=48), nullable=True),
        sa.Column("corroboration", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.String(length=128), nullable=False),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    # One live claim per identifier. Partial, so superseded history and rows
    # awaiting review coexist, and so a flagged legacy alias never occupies the
    # slot that belongs to a real handle.
    op.create_index(
        "uq_linkedin_identity_links_active_identifier",
        "linkedin_identity_links",
        ["identifier_kind", "identifier_value"],
        unique=True,
        postgresql_where=sa.text("state = 'active' AND suspected_alias = false"),
    )
    op.create_index(
        "ix_linkedin_identity_links_lookup",
        "linkedin_identity_links",
        ["identifier_kind", "identifier_value"],
    )
    op.create_index(
        "ix_linkedin_identity_links_contact_id",
        "linkedin_identity_links",
        ["contact_id"],
    )

    # --- provenance for rows that already exist ---------------------------------
    op.execute(
        f"""
        UPDATE linkedin_profile_snapshots
           SET profile_url_source = 'derived_from_sales_lead'
         WHERE {_LEGACY_ALIAS_PREDICATE}
        """
    )
    op.execute(
        """
        UPDATE linkedin_profile_snapshots
           SET profile_url_source = 'observed'
         WHERE normalized_profile_url IS NOT NULL
           AND profile_url_source IS NULL
        """
    )
    # The member id is recoverable from the lead URL that was always stored.
    # Original casing is taken from the lead URL, never from the lowercased slug.
    op.execute(
        """
        UPDATE linkedin_profile_snapshots
           SET salesnav_member_id = split_part(split_part(
                   regexp_replace(salesnav_lead_url, '^.*/sales/lead/', ''), '?', 1), ',', 1)
         WHERE salesnav_lead_url IS NOT NULL
           AND salesnav_lead_url LIKE '%/sales/lead/%'
        """
    )

    # --- back-fill the links, flagging what cannot be trusted --------------------
    #
    # One row per contact that already carries a LinkedIn URL. A contact whose
    # URL matches a snapshot flagged above is recorded as a suspected alias:
    # preserved, queryable, excluded from matching and from uniqueness, and
    # awaiting an operator rather than a guess. Contacts sharing a normalized URL
    # are all marked for review instead of one of them silently winning.
    op.execute(
        """
        INSERT INTO linkedin_identity_links (
            id, contact_id, identifier_kind, identifier_value, state,
            decision_kind, suspected_alias, decided_by, decided_at
        )
        SELECT
            gen_random_uuid(),
            c.id,
            'public_vanity_url',
            lower(c.linkedin_url),
            CASE WHEN dup.copies > 1 THEN 'needs_review' ELSE 'active' END,
            'migration_backfill',
            COALESCE(alias.flagged, false),
            'migration:dat-019',
            now()
        FROM contacts c
        JOIN (
            SELECT lower(linkedin_url) AS url, count(*) AS copies
              FROM contacts
             WHERE linkedin_url IS NOT NULL AND merged_into_id IS NULL
             GROUP BY lower(linkedin_url)
        ) dup ON dup.url = lower(c.linkedin_url)
        LEFT JOIN LATERAL (
            SELECT true AS flagged
              FROM linkedin_profile_snapshots s
             WHERE s.profile_url_source = 'derived_from_sales_lead'
               AND lower(s.normalized_profile_url) = lower(c.linkedin_url)
             LIMIT 1
        ) alias ON true
        WHERE c.linkedin_url IS NOT NULL
          AND c.merged_into_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_linkedin_identity_links_contact_id", "linkedin_identity_links")
    op.drop_index("ix_linkedin_identity_links_lookup", "linkedin_identity_links")
    op.drop_index("uq_linkedin_identity_links_active_identifier", "linkedin_identity_links")
    op.drop_table("linkedin_identity_links")
    op.drop_index("ix_li_profile_snapshots_salesnav_member_id", "linkedin_profile_snapshots")
    op.drop_column("linkedin_profile_snapshots", "profile_url_source")
    op.drop_column("linkedin_profile_snapshots", "salesnav_member_id")

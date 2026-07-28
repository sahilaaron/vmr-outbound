"""KB-001 seller-side knowledge base

Revision ID: b8e5d34a91c7
Revises: e61f4c2b7a90
Create Date: 2026-07-28 20:10:00.000000

Adds the seller side of the system: what *we* sell, what we may say about it,
and who we say it to. Everything here is entered by an operator, so there is no
provenance ledger, no confidence, no evidence rows, and no review state — the
entry is the authorization (KB-001).

Six tables and four join tables:

1. ``seller_profiles`` — the one profile of the selling organisation. It is
   deliberately NOT a row in ``companies``: that table holds externally
   researched prospect companies, and merging the two would have turned "is
   this us?" into a value someone has to remember to filter on. A partial
   unique index on ``is_current`` makes a second profile impossible in the
   database rather than merely discouraged.
2. ``seller_offerings`` — products, services, subscriptions, report categories
   and research engagements. A partial unique index on ``name`` covers ACTIVE
   rows only, so withdrawing an offering frees its name without renaming
   history.
3. ``seller_proof_points`` — first-party factual statements, stored once and
   referenced, not copied per offering.
4. ``seller_restricted_claims`` — statements generated copy must not make,
   either globally or for named offerings.
5. ``seller_personas`` — reusable buyer personas. Distinct from ``contacts``,
   which are real people with suppression state; nobody is ever contacted
   because of a persona.
6. ``seller_offering_proof_points``, ``seller_offering_restricted_claims``,
   ``seller_offering_personas`` — the offering associations.
7. ``campaign_offerings`` — which offerings a campaign concerns. Association
   only: it never writes email copy and never selects a call to action.

Three enum types are created: ``seller_record_state`` (shared by the four
record tables), ``seller_offering_type`` and ``seller_claim_scope``. As
everywhere in this repository, SQLAlchemy persists enum members by NAME, so the
labels are upper-case and the partial indexes compare against ``'ACTIVE'``.
The types are declared with ``create_type=False`` and created and dropped by
hand, because ``create_table`` would otherwise emit a second ``CREATE TYPE``
and because PostgreSQL does not drop an enum type when its last table is
dropped — which would break a re-upgrade after a downgrade.

The foreign keys on the join tables are named explicitly. The repository naming
convention would generate names longer than PostgreSQL's 63-byte identifier
limit for tables like ``seller_offering_restricted_claims``, and a silently
truncated name never matches the model again.

Nothing existing is altered. No column is added to ``campaigns``, ``contacts``,
``companies``, ``insights`` or any verification, suppression or scoring table:
the campaign-to-offering relationship lives entirely in its own join table, so
this migration cannot affect an existing campaign, import, or contact record.

Downgrade drops all ten tables and the three enum types, reversing cleanly on
an empty database. It refuses only when the knowledge base actually holds
operator-authored rows that exist nowhere else, because that content was typed
by a person and cannot be re-derived from anything the system stores.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b8e5d34a91c7"
down_revision: str | Sequence[str] | None = "e61f4c2b7a90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# SQLAlchemy stores enum members by NAME (upper-case). Managed explicitly with
# create_type=False so create_table never emits a second CREATE TYPE.
_seller_record_state = postgresql.ENUM(
    "ACTIVE",
    "ARCHIVED",
    name="seller_record_state",
    create_type=False,
)
_seller_offering_type = postgresql.ENUM(
    "PRODUCT",
    "SERVICE",
    "SOLUTION",
    "SUBSCRIPTION",
    "RESEARCH_REPORT",
    "RESEARCH_ENGAGEMENT",
    "OTHER",
    name="seller_offering_type",
    create_type=False,
)
_seller_claim_scope = postgresql.ENUM(
    "GLOBAL",
    "OFFERING",
    name="seller_claim_scope",
    create_type=False,
)

# Tables that hold operator-typed content, checked before a downgrade destroys
# them. The join tables are not listed: a link is re-creatable once the records
# it joins exist, so it is not independently unre-derivable.
_AUTHORED_TABLES = (
    "seller_profiles",
    "seller_offerings",
    "seller_proof_points",
    "seller_restricted_claims",
    "seller_personas",
)


def upgrade() -> None:
    """Create the seller knowledge base and the campaign-offering association."""

    bind = op.get_bind()
    _seller_record_state.create(bind, checkfirst=True)
    _seller_offering_type.create(bind, checkfirst=True)
    _seller_claim_scope.create(bind, checkfirst=True)

    op.create_table(
        "seller_profiles",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("is_current", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("short_description", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("positioning", sa.Text(), nullable=True),
        sa.Column("communication_guidance", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("industries_served", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("geographies_served", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("capabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("differentiators", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("updated_by", sa.String(length=120), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seller_profiles")),
    )
    # One profile, enforced by the database.
    op.create_index(
        "uq_seller_profiles_current",
        "seller_profiles",
        ["is_current"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )

    op.create_table(
        "seller_offerings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("offering_type", _seller_offering_type, nullable=False),
        sa.Column("short_description", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("problems_addressed", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("use_cases", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("differentiators", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("state", _seller_record_state, nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=120), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seller_offerings")),
    )
    op.create_index("ix_seller_offerings_state", "seller_offerings", ["state"])
    op.create_index(
        "uq_seller_offerings_active_name",
        "seller_offerings",
        ["name"],
        unique=True,
        postgresql_where=sa.text("state = 'ACTIVE'"),
    )

    op.create_table(
        "seller_proof_points",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("supporting_detail", sa.Text(), nullable=True),
        sa.Column("source_reference", sa.String(length=1024), nullable=True),
        sa.Column("state", _seller_record_state, nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=120), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seller_proof_points")),
    )
    op.create_index("ix_seller_proof_points_state", "seller_proof_points", ["state"])

    op.create_table(
        "seller_restricted_claims",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("examples", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("scope", _seller_claim_scope, nullable=False),
        sa.Column("state", _seller_record_state, nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=120), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seller_restricted_claims")),
    )
    op.create_index("ix_seller_restricted_claims_scope", "seller_restricted_claims", ["scope"])
    op.create_index("ix_seller_restricted_claims_state", "seller_restricted_claims", ["state"])

    op.create_table(
        "seller_personas",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("role_function", sa.String(length=255), nullable=True),
        sa.Column("seniority", sa.String(length=120), nullable=True),
        sa.Column("responsibilities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("challenges", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("use_cases", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("messaging_notes", sa.Text(), nullable=True),
        sa.Column("state", _seller_record_state, nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=120), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seller_personas")),
    )
    op.create_index("ix_seller_personas_state", "seller_personas", ["state"])
    op.create_index(
        "uq_seller_personas_active_name",
        "seller_personas",
        ["name"],
        unique=True,
        postgresql_where=sa.text("state = 'ACTIVE'"),
    )

    op.create_table(
        "seller_offering_proof_points",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("offering_id", sa.UUID(), nullable=False),
        sa.Column("proof_point_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["offering_id"],
            ["seller_offerings.id"],
            name="fk_seller_offering_proof_points_offering",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["proof_point_id"],
            ["seller_proof_points.id"],
            name="fk_seller_offering_proof_points_proof_point",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seller_offering_proof_points")),
        sa.UniqueConstraint(
            "offering_id",
            "proof_point_id",
            name="uq_seller_offering_proof_points_pair",
        ),
    )
    op.create_index(
        "ix_seller_offering_proof_points_offering_id",
        "seller_offering_proof_points",
        ["offering_id"],
    )
    op.create_index(
        "ix_seller_offering_proof_points_proof_point_id",
        "seller_offering_proof_points",
        ["proof_point_id"],
    )

    op.create_table(
        "seller_offering_restricted_claims",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("offering_id", sa.UUID(), nullable=False),
        sa.Column("restricted_claim_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["offering_id"],
            ["seller_offerings.id"],
            name="fk_seller_offering_restricted_claims_offering",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["restricted_claim_id"],
            ["seller_restricted_claims.id"],
            name="fk_seller_offering_restricted_claims_claim",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seller_offering_restricted_claims")),
        sa.UniqueConstraint(
            "offering_id",
            "restricted_claim_id",
            name="uq_seller_offering_restricted_claims_pair",
        ),
    )
    op.create_index(
        "ix_seller_offering_restricted_claims_offering_id",
        "seller_offering_restricted_claims",
        ["offering_id"],
    )
    op.create_index(
        "ix_seller_offering_restricted_claims_restricted_claim_id",
        "seller_offering_restricted_claims",
        ["restricted_claim_id"],
    )

    op.create_table(
        "seller_offering_personas",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("offering_id", sa.UUID(), nullable=False),
        sa.Column("persona_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["offering_id"],
            ["seller_offerings.id"],
            name="fk_seller_offering_personas_offering",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["persona_id"],
            ["seller_personas.id"],
            name="fk_seller_offering_personas_persona",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seller_offering_personas")),
        sa.UniqueConstraint(
            "offering_id",
            "persona_id",
            name="uq_seller_offering_personas_pair",
        ),
    )
    op.create_index(
        "ix_seller_offering_personas_offering_id",
        "seller_offering_personas",
        ["offering_id"],
    )
    op.create_index(
        "ix_seller_offering_personas_persona_id",
        "seller_offering_personas",
        ["persona_id"],
    )

    op.create_table(
        "campaign_offerings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("campaign_id", sa.UUID(), nullable=False),
        sa.Column("offering_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name="fk_campaign_offerings_campaign",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["offering_id"],
            ["seller_offerings.id"],
            name="fk_campaign_offerings_offering",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campaign_offerings")),
        sa.UniqueConstraint(
            "campaign_id",
            "offering_id",
            name="uq_campaign_offerings_campaign_offering",
        ),
    )
    op.create_index("ix_campaign_offerings_campaign_id", "campaign_offerings", ["campaign_id"])
    op.create_index("ix_campaign_offerings_offering_id", "campaign_offerings", ["offering_id"])


def downgrade() -> None:
    """Drop the knowledge base, refusing while it holds operator-typed content."""

    bind = op.get_bind()

    # A proof point or a positioning paragraph was typed by a person and exists
    # in no other table, no import file, and no external system. Dropping it
    # silently would destroy work that cannot be recomputed, so the downgrade
    # says so instead. Empty tables downgrade without complaint, which is what
    # the migration round-trip test exercises.
    populated: list[str] = []
    for table in _AUTHORED_TABLES:
        count = bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()
        if count:
            populated.append(f"{table} ({count})")
    if populated:
        raise RuntimeError(
            "KB-001 (b8e5d34a91c7) will not downgrade while the seller knowledge "
            f"base holds operator-entered content: {', '.join(populated)}. This "
            "content was typed by a person and is not derivable from any other "
            "table, import, or external system. Export or archive it first, or "
            "restore from a backup taken before the upgrade instead."
        )

    op.drop_index("ix_campaign_offerings_offering_id", table_name="campaign_offerings")
    op.drop_index("ix_campaign_offerings_campaign_id", table_name="campaign_offerings")
    op.drop_table("campaign_offerings")

    op.drop_index("ix_seller_offering_personas_persona_id", table_name="seller_offering_personas")
    op.drop_index("ix_seller_offering_personas_offering_id", table_name="seller_offering_personas")
    op.drop_table("seller_offering_personas")

    op.drop_index(
        "ix_seller_offering_restricted_claims_restricted_claim_id",
        table_name="seller_offering_restricted_claims",
    )
    op.drop_index(
        "ix_seller_offering_restricted_claims_offering_id",
        table_name="seller_offering_restricted_claims",
    )
    op.drop_table("seller_offering_restricted_claims")

    op.drop_index(
        "ix_seller_offering_proof_points_proof_point_id",
        table_name="seller_offering_proof_points",
    )
    op.drop_index(
        "ix_seller_offering_proof_points_offering_id",
        table_name="seller_offering_proof_points",
    )
    op.drop_table("seller_offering_proof_points")

    op.drop_index("uq_seller_personas_active_name", table_name="seller_personas")
    op.drop_index("ix_seller_personas_state", table_name="seller_personas")
    op.drop_table("seller_personas")

    op.drop_index("ix_seller_restricted_claims_state", table_name="seller_restricted_claims")
    op.drop_index("ix_seller_restricted_claims_scope", table_name="seller_restricted_claims")
    op.drop_table("seller_restricted_claims")

    op.drop_index("ix_seller_proof_points_state", table_name="seller_proof_points")
    op.drop_table("seller_proof_points")

    op.drop_index("uq_seller_offerings_active_name", table_name="seller_offerings")
    op.drop_index("ix_seller_offerings_state", table_name="seller_offerings")
    op.drop_table("seller_offerings")

    op.drop_index("uq_seller_profiles_current", table_name="seller_profiles")
    op.drop_table("seller_profiles")

    # PostgreSQL keeps an enum type after its last table is dropped; leaving
    # them behind would make a re-upgrade fail on CREATE TYPE.
    _seller_claim_scope.drop(bind, checkfirst=True)
    _seller_offering_type.drop(bind, checkfirst=True)
    _seller_record_state.drop(bind, checkfirst=True)

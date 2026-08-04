"""Add immutable Personalization policy history and draft provenance.

Revision ID: e8c4a91f2d60
Revises: d3b7e2f19c45

The migration is additive for existing production records.  Existing draft
versions retain NULL provenance, while a validated policy v1 and its activation
are inserted for new Personalization work.  PostgreSQL triggers enforce the
append-only contract even when writes bypass the application service.
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e8c4a91f2d60"
down_revision = "d3b7e2f19c45"
branch_labels = None
depends_on = None

_POLICY_ID = "a6100000-0000-4000-8000-000000000001"
_ACTIVATION_ID = "a6100000-0000-4000-8000-000000000002"


def _initial_policy() -> dict[str, object]:
    standards = (
        (
            "do_not_explain_company",
            "Do not tell a prospect obvious facts about their own organisation.",
            "Never explain the recipient's own company back to them.",
        ),
        (
            "context_must_improve_relevance",
            "Context earns a place only by making the seller's offer more relevant.",
            "Use context only when it creates a clear, useful connection to the offering.",
        ),
        (
            "prefer_curiosity",
            "Ask honestly instead of pretending to know an internal priority.",
            "Prefer a relevant question over an unsupported statement about priorities.",
        ),
        (
            "no_intelligence_display",
            "Research is not included merely to prove that research happened.",
            "Do not display intelligence for its own sake or summarize gathered facts.",
        ),
        (
            "admit_weak_evidence",
            "Weak evidence should lead to less personalization, not invention.",
            "When evidence is weak, step down the fallback ladder without apology or pretence.",
        ),
        (
            "explain_seller_offering",
            "The recipient must understand what the seller actually offers.",
            "State the seller's offering plainly enough that the recipient can evaluate relevance.",
        ),
        (
            "match_strategy_to_evidence",
            "The opening form must follow the evidence that is actually available.",
            "Use only a writing strategy whose evidence requirements were deterministically met.",
        ),
        (
            "minimum_personalization",
            "More personalization is not automatically better.",
            "Use the least personalization required to earn attention.",
        ),
    )
    strategies = (
        {
            "id": "relevant_question_first",
            "name": "Relevant question first",
            "enabled": True,
            "eligible_when": "Supported Contact, Company, combined, or sector context exists.",
            "evidence_required": ["contact", "company", "combined", "sector"],
            "opening_shape": "Open with one honest question grounded in selected context.",
            "introduction_placement": "Explain the seller after the relevance question.",
            "cta_shape": "Ask whether the subject is worth exploring.",
            "prohibited_behavior": ["leading question", "assumed priority", "research recital"],
            "fallback_destination": "earnest_offering_led",
        },
        {
            "id": "relevant_statement_then_question",
            "name": "Relevant statement followed by a question",
            "enabled": True,
            "eligible_when": "A current supported Company fact has offering relevance.",
            "evidence_required": ["company", "combined"],
            "opening_shape": "State one sourced fact briefly, then ask a question.",
            "introduction_placement": "Introduce the seller after the question.",
            "cta_shape": "Invite a reply to the relevance question.",
            "prohibited_behavior": ["company summary", "praise", "unsupported implication"],
            "fallback_destination": "relevant_question_first",
        },
        {
            "id": "role_led_relevance",
            "name": "Role-led relevance",
            "enabled": True,
            "eligible_when": "The recorded role creates a connection without guessing priorities.",
            "evidence_required": ["contact", "combined"],
            "opening_shape": "Open with a question about the recorded responsibility area.",
            "introduction_placement": "Introduce the offering after the role connection.",
            "cta_shape": "Ask whether the role includes the offered problem space.",
            "prohibited_behavior": ["claiming role ownership", "claiming a target or challenge"],
            "fallback_destination": "earnest_offering_led",
        },
        {
            "id": "company_context_relevance",
            "name": "Company-context relevance",
            "enabled": True,
            "eligible_when": "A supported current Company fact materially changes relevance.",
            "evidence_required": ["company", "combined"],
            "opening_shape": "Use one short Company reference; never describe the organisation.",
            "introduction_placement": "Introduce the offering after the relevance link.",
            "cta_shape": "Ask a narrow question about whether the connection matters.",
            "prohibited_behavior": ["fact stacking", "company explanation", "intelligence display"],
            "fallback_destination": "relevant_question_first",
        },
        {
            "id": "earnest_offering_led",
            "name": "Earnest offering-led introduction",
            "enabled": True,
            "eligible_when": "No meaningful prospect context clears the evidence threshold.",
            "evidence_required": [],
            "opening_shape": "Open plainly with what the seller helps with.",
            "introduction_placement": "Introduce the seller in the opening.",
            "cta_shape": "Ask whether the offering is relevant or who owns it.",
            "prohibited_behavior": [
                "fake personalization",
                "generic compliment",
                "invented familiarity",
            ],
            "fallback_destination": None,
        },
    )
    return {
        "schema_version": "personalization-policy/v1",
        "standards": [
            {
                "id": identifier,
                "description": description,
                "wording": wording,
                "strength": "required",
                "state": "enabled",
            }
            for identifier, description, wording in standards
        ],
        "temperament": {
            "company_context_usage": 2,
            "question_first_preference": 3,
            "commercial_directness": 2,
            "personalization_depth": 1,
            "evidence_confidence_tolerance": 1,
            "role_led_emphasis": 2,
            "seller_introduction_timing": 2,
            "assertive_tone": 1,
        },
        "strategies": list(strategies),
        "fallback_ladder": [
            "contact_and_company",
            "company_only",
            "contact_role_only",
            "sector_only",
            "offering_led",
        ],
        "examples": [],
        "evidence": {"maximum_age_days": 365},
    }


def upgrade() -> None:
    op.create_table(
        "personalization_policy_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("configuration", postgresql.JSONB(), nullable=False),
        sa.Column("validation_summary", postgresql.JSONB(), nullable=False),
        sa.Column("based_on_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("change_note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["based_on_version_id"],
            ["personalization_policy_versions.id"],
            name=op.f(
                "fk_personalization_policy_versions_based_on_version_id_"
                "personalization_policy_versions"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_personalization_policy_versions"),
        sa.UniqueConstraint("version_number", name="uq_personalization_policy_versions_number"),
    )
    op.create_index(
        "ix_personalization_policy_versions_created_at",
        "personalization_policy_versions",
        ["created_at"],
    )
    op.create_table(
        "personalization_policy_activations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("policy_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("previous_policy_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("activated_by", sa.String(length=128), nullable=False),
        sa.Column(
            "activated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["policy_version_id"],
            ["personalization_policy_versions.id"],
            name=op.f(
                "fk_personalization_policy_activations_policy_version_id_"
                "personalization_policy_versions"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["previous_policy_version_id"],
            ["personalization_policy_versions.id"],
            name=op.f(
                "fk_personalization_policy_activations_previous_policy_version_id_"
                "personalization_policy_versions"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_personalization_policy_activations"),
    )
    op.create_index(
        "ix_personalization_policy_activations_time",
        "personalization_policy_activations",
        ["activated_at"],
    )
    for name, column_type in (
        ("personalization_policy_version_id", postgresql.UUID(as_uuid=True)),
        ("personalization_strategy_id", sa.String(length=64)),
        ("personalization_decision", postgresql.JSONB()),
        ("producer", sa.String(length=128)),
        ("producer_version", sa.String(length=64)),
    ):
        op.add_column("draft_versions", sa.Column(name, column_type, nullable=True))
    op.create_foreign_key(
        op.f("fk_draft_versions_personalization_policy_version_id_personalization_policy_versions"),
        "draft_versions",
        "personalization_policy_versions",
        ["personalization_policy_version_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Embed only migration-owned constants so offline SQL is complete too. JSON
    # apostrophes are escaped as SQL string literals; no runtime input reaches
    # either statement.
    configuration = json.dumps(_initial_policy()).replace("'", "''")
    validation = json.dumps({"valid": True, "schema_version": "personalization-policy/v1"}).replace(
        "'", "''"
    )
    op.execute(
        f"""
        INSERT INTO personalization_policy_versions
            (id, version_number, schema_version, name, configuration,
             validation_summary, change_note, created_by)
        VALUES
            ('{_POLICY_ID}'::uuid, 1, 'personalization-policy/v1',
             'Initial outreach standard', '{configuration}'::jsonb,
             '{validation}'::jsonb, 'Seeded by Agent Studio migration',
             'system:migration')
        """
    )
    op.execute(
        f"""
        INSERT INTO personalization_policy_activations
            (id, policy_version_id, previous_policy_version_id, reason, activated_by)
        VALUES ('{_ACTIVATION_ID}'::uuid, '{_POLICY_ID}'::uuid, NULL,
                'Initial policy activation', 'system:migration')
        """
    )

    op.execute(
        """
        CREATE FUNCTION reject_agent_studio_history_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Agent Studio history is append-only';
        END;
        $$
        """
    )
    for table in (
        "personalization_policy_versions",
        "personalization_policy_activations",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_immutable
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_agent_studio_history_mutation()
            """
        )


def downgrade() -> None:
    for table in (
        "personalization_policy_activations",
        "personalization_policy_versions",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
    op.execute("DROP FUNCTION IF EXISTS reject_agent_studio_history_mutation()")
    op.drop_constraint(
        op.f("fk_draft_versions_personalization_policy_version_id_personalization_policy_versions"),
        "draft_versions",
        type_="foreignkey",
    )
    for column in (
        "producer_version",
        "producer",
        "personalization_decision",
        "personalization_strategy_id",
        "personalization_policy_version_id",
    ):
        op.drop_column("draft_versions", column)
    op.drop_index(
        "ix_personalization_policy_activations_time",
        table_name="personalization_policy_activations",
    )
    op.drop_table("personalization_policy_activations")
    op.drop_index(
        "ix_personalization_policy_versions_created_at",
        table_name="personalization_policy_versions",
    )
    op.drop_table("personalization_policy_versions")

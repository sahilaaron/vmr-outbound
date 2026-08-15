"""Make company-domain resolution acquisition-source-agnostic

Revision ID: f4c9a2e70b18
Revises: e2b7c0d94a15
Create Date: 2026-08-15 10:40:00.000000

Company-domain resolution could only ever be *about* a Chrome capture. The
decision ledger's ``capture_id`` was NOT NULL, and the candidate store it reads
was owned by a staged import batch or a capture and nothing else. A surface that
produces a permanent Contact directly — Google Sheets — therefore had no way to
enter the resolution process at all: its contacts stopped at
``company_domain_missing`` until somebody re-acquired the same person through
the extension.

This revision widens the *subject* of a decision instead of adding a second
pipeline beside the first:

1. ``company_domain_resolutions.capture_id`` becomes nullable and a nullable
   ``contact_id`` is added, with a check constraint requiring exactly one of the
   two. Decision numbering and the one-live-decision rule are re-expressed per
   subject.
2. ``salesnav_company_enrichments`` gains the same ``contact_id`` owner, and its
   ``single_owner`` check becomes "exactly one of batch, capture, contact".

Nothing existing is rewritten and no data is migrated. Every row already in
either table has a capture or a batch, satisfies the new constraints unchanged,
and keeps behaving exactly as it did — the widened columns are NULL on all of
them. The feature switches that decide whether resolution runs at all are
untouched, so this revision is inert until a contact-subject resolution is
actually performed.

Downgrade REFUSES once contact-subject rows exist. See :func:`downgrade`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f4c9a2e70b18"
down_revision: str | Sequence[str] | None = "e2b7c0d94a15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DECISIONS = "company_domain_resolutions"
_ENRICHMENTS = "salesnav_company_enrichments"

#: The enrichment owner rule before and after this revision. Written out in both
#: directions because PostgreSQL cannot edit a check constraint in place.
_OWNER_BEFORE = "(batch_id IS NULL) <> (capture_id IS NULL)"
_OWNER_AFTER = (
    "(batch_id IS NOT NULL)::int + (capture_id IS NOT NULL)::int "
    "+ (contact_id IS NOT NULL)::int = 1"
)


def upgrade() -> None:
    """Widen both tables from capture-bound to subject-bound."""

    # --- 1. The decision ledger ------------------------------------------------
    op.add_column(
        _DECISIONS,
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.alter_column(_DECISIONS, "capture_id", existing_type=postgresql.UUID(), nullable=True)
    op.create_foreign_key(
        op.f(f"fk_{_DECISIONS}_contact_id_contacts"),
        _DECISIONS,
        "contacts",
        ["contact_id"],
        ["id"],
        ondelete="CASCADE",
    )
    # Exactly one subject. Added after the column so the existing rows — every
    # one of which has a capture — satisfy it at creation time without a scan
    # that could fail.
    # Bare name: the metadata naming convention is ``ck_%(table_name)s_%(constraint_name)s``
    # and Alembic applies it here, so spelling the prefix would produce
    # ``ck_company_domain_resolutions_ck_company_domain_resol_<hash>``. See
    # migration ``b6d4e07a1f38``, which exists to repair exactly that mistake.
    op.create_check_constraint(
        "single_subject",
        _DECISIONS,
        "(capture_id IS NULL) <> (contact_id IS NULL)",
    )
    op.create_index(
        f"ix_{_DECISIONS}_contact",
        _DECISIONS,
        ["contact_id"],
        postgresql_where=sa.text("contact_id IS NOT NULL"),
    )
    op.create_unique_constraint(
        op.f(f"uq_{_DECISIONS}_contact_number"), _DECISIONS, ["contact_id", "decision_number"]
    )
    # The live-decision index becomes per subject. The capture one is recreated
    # with an explicit NOT NULL predicate: PostgreSQL already treats NULLs as
    # distinct, so this changes nothing operationally and states the intent
    # where a later reader will look for it.
    op.drop_index(f"uq_{_DECISIONS}_current", table_name=_DECISIONS)
    op.create_index(
        f"uq_{_DECISIONS}_current",
        _DECISIONS,
        ["capture_id"],
        unique=True,
        postgresql_where=sa.text("is_current AND capture_id IS NOT NULL"),
    )
    op.create_index(
        f"uq_{_DECISIONS}_contact_current",
        _DECISIONS,
        ["contact_id"],
        unique=True,
        postgresql_where=sa.text("is_current AND contact_id IS NOT NULL"),
    )

    # --- 2. The candidate store ------------------------------------------------
    op.add_column(
        _ENRICHMENTS,
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        op.f(f"fk_{_ENRICHMENTS}_contact_id_contacts"),
        _ENRICHMENTS,
        "contacts",
        ["contact_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        f"uq_{_ENRICHMENTS}_contact",
        _ENRICHMENTS,
        ["contact_id"],
        unique=True,
        postgresql_where=sa.text("contact_id IS NOT NULL"),
    )
    # ``op.f`` marks the name as already final. Without it the same naming
    # convention that shortens the create call would be applied to the drop too,
    # and this would try to drop a constraint that has never existed.
    op.drop_constraint(op.f(f"ck_{_ENRICHMENTS}_single_owner"), _ENRICHMENTS, type_="check")
    op.create_check_constraint("single_owner", _ENRICHMENTS, _OWNER_AFTER)


def downgrade() -> None:
    """Reverse cleanly while nothing uses the new subject; refuse once something does.

    A contact-subject decision is the only record of why a Contact acquired from
    a spreadsheet carries the company it carries, how certain that was, and what
    the provider offered. Narrowing ``capture_id`` back to NOT NULL would have to
    delete those rows, and they cannot be re-derived — the evidence a decision
    was made on is not the evidence available now. The same applies to a
    contact-owned candidate record, which holds the confirmation other surfaces
    now read back as an approved mapping.

    The refusal is conditional on there being something to protect, exactly as
    DAT-017A's is: a database that has never resolved a contact's company
    reverses without ceremony, which is what keeps the round-trip check
    meaningful.
    """

    bind = op.get_bind()

    decisions = bind.execute(
        sa.text(f"SELECT count(*) FROM {_DECISIONS} WHERE contact_id IS NOT NULL")
    ).scalar_one()
    records = bind.execute(
        sa.text(f"SELECT count(*) FROM {_ENRICHMENTS} WHERE contact_id IS NOT NULL")
    ).scalar_one()
    if decisions or records:
        raise RuntimeError(
            f"f4c9a2e70b18 will not downgrade while {decisions} contact-subject "
            f"company-domain decision(s) and {records} contact-owned candidate record(s) "
            "exist. Each one records the evidence, certainty, candidates and confirmation "
            "behind a live company link for a Contact that has no capture, and reversing "
            "would have to discard them. Restore from a backup taken before the upgrade "
            "instead."
        )

    # ``op.f`` marks the name as already final. Without it the same naming
    # convention that shortens the create call would be applied to the drop too,
    # and this would try to drop a constraint that has never existed.
    op.drop_constraint(op.f(f"ck_{_ENRICHMENTS}_single_owner"), _ENRICHMENTS, type_="check")
    op.create_check_constraint("single_owner", _ENRICHMENTS, _OWNER_BEFORE)
    op.drop_index(f"uq_{_ENRICHMENTS}_contact", table_name=_ENRICHMENTS)
    op.drop_constraint(
        op.f(f"fk_{_ENRICHMENTS}_contact_id_contacts"), _ENRICHMENTS, type_="foreignkey"
    )
    op.drop_column(_ENRICHMENTS, "contact_id")

    op.drop_index(f"uq_{_DECISIONS}_contact_current", table_name=_DECISIONS)
    op.drop_index(f"uq_{_DECISIONS}_current", table_name=_DECISIONS)
    op.create_index(
        f"uq_{_DECISIONS}_current",
        _DECISIONS,
        ["capture_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.drop_constraint(op.f(f"uq_{_DECISIONS}_contact_number"), _DECISIONS, type_="unique")
    op.drop_index(f"ix_{_DECISIONS}_contact", table_name=_DECISIONS)
    op.drop_constraint(op.f(f"ck_{_DECISIONS}_single_subject"), _DECISIONS, type_="check")
    op.drop_constraint(op.f(f"fk_{_DECISIONS}_contact_id_contacts"), _DECISIONS, type_="foreignkey")
    op.drop_column(_DECISIONS, "contact_id")
    op.alter_column(_DECISIONS, "capture_id", existing_type=postgresql.UUID(), nullable=False)

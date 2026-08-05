"""Align eight check-constraint names with the model metadata.

Revision ID: b6d4e07a1f38
Revises: a8f3c92d4e17
Create Date: 2026-08-05 15:40:00.000000

Eight check constraints across five tables carry a name in PostgreSQL that the
model metadata cannot produce. Nothing about what they *enforce* changes here —
this migration renames, and renames only. No constraint is dropped, recreated,
weakened, or added, and no row is read or rewritten.

## Why the names diverged

``app/db/base.py`` sets ``"ck": "ck_%(table_name)s_%(constraint_name)s"``, so a
constraint declared as ``name="status_known"`` on ``email_candidate_attempts``
becomes ``ck_email_candidate_attempts_status_known``. Two ways of writing a
constraint defeated that, both of them silent:

1. **The prefix was written out by hand as well.** A migration passing
   ``name="ck_email_candidate_attempts_status_known"`` gets the convention
   applied to that, producing
   ``ck_email_candidate_attempts_ck_email_candidate_attempts_status_known``.
   That is 69 bytes; PostgreSQL identifiers stop at 63, so SQLAlchemy truncated
   it and appended a hash of the full name —
   ``ck_email_candidate_attempts_ck_email_candidate_attempts_c875``. The model
   still resolves to the short, correct name, so the two never matched.
2. **The correct name was simply too long.** On
   ``company_intelligence_classifications`` the prefix alone is 40 bytes, and
   ``geo_relationship_and_presence_paired`` takes it to 76. The migration did
   nothing wrong; the name did not fit, and the same truncate-and-hash applied.

Neither shape is anybody's intent. ``..._geo_relationshi_daf6`` is what a length
limit produced, not what anyone would choose to see in an error message.

## Why this was invisible until now

Alembic did not compare check constraints by name until 1.19.0 added the
``checkconstraint_byname`` autogenerate plugin. Under 1.18.x, ``alembic check``
passed against exactly this schema. The drift is older than the tool that found
it, which is why the fix is one migration rather than a revert.

## Why it is safe

``ALTER TABLE ... RENAME CONSTRAINT`` rewrites a catalog entry. The expression,
the columns it covers, and every row it admits or refuses are untouched.

Each rename is matched on the constraint's *definition* rather than on the
truncated name, because those hash suffixes are an artefact of SQLAlchemy's
truncation and are not ours to depend on. Each is also a no-op when the target
name already exists, so this revision is safe to re-run and behaves identically
on a fresh database (which reaches it carrying the legacy names) and on one that
has already been migrated.
"""

from __future__ import annotations

from alembic import op

revision = "b6d4e07a1f38"
down_revision = "a8f3c92d4e17"
branch_labels = None
depends_on = None

#: ``(table, marker, aligned_name, legacy_name)``.
#:
#: ``marker`` is a fragment of ``pg_get_constraintdef`` that identifies exactly
#: one check constraint on that table — verified against the live catalog rather
#: than inferred from declaration order, which is not the order PostgreSQL
#: assigned the hashes in.
_RENAMES: tuple[tuple[str, str, str, str], ...] = (
    (
        "company_domain_resolutions",
        "decision_number > 0",
        "ck_company_domain_resolutions_decision_number_positive",
        "ck_company_domain_resolutions_ck_company_domain_resolut_ffdd",
    ),
    (
        "company_domain_resolutions",
        "selected_domain IS NULL",
        "ck_company_domain_resolutions_state_matches_domain",
        "ck_company_domain_resolutions_ck_company_domain_resolut_9d18",
    ),
    (
        "company_intelligence_classifications",
        "'GEOGRAPHY'",
        "ck_company_intelligence_classifications_geo_fields_geography",
        "ck_company_intelligence_classifications_geo_fields_are__1a64",
    ),
    (
        "company_intelligence_classifications",
        "(geo_relationship IS NULL) = (presence_kind IS NULL)",
        "ck_company_intelligence_classifications_geo_presence_paired",
        "ck_company_intelligence_classifications_geo_relationshi_daf6",
    ),
    (
        "contact_label_assignments",
        "(contact_id IS NOT NULL) OR (capture_id IS NOT NULL)",
        "ck_contact_label_assignments_anchor",
        "ck_contact_label_assignments_ck_contact_label_assignmen_8b26",
    ),
    (
        "email_candidate_attempts",
        "'verification_queued'",
        "ck_email_candidate_attempts_status_known",
        "ck_email_candidate_attempts_ck_email_candidate_attempts_479c",
    ),
    (
        "email_candidate_attempts",
        "'more_than_50'",
        "ck_email_candidate_attempts_employee_count_class_known",
        "ck_email_candidate_attempts_ck_email_candidate_attempts_c875",
    ),
    (
        "salesnav_company_enrichments",
        "(batch_id IS NULL) <> (capture_id IS NULL)",
        "ck_salesnav_company_enrichments_single_owner",
        "ck_salesnav_company_enrichments_ck_salesnav_company_enr_8ce6",
    ),
)


def _rename(*, table: str, marker: str, target: str) -> None:
    """Rename the one check constraint on ``table`` whose definition matches.

    Deliberately does nothing when the target name is already present, and
    deliberately raises when the marker matches no constraint or more than one.
    A rename that quietly matched nothing would leave the drift in place and
    report success, which is the failure this whole revision exists to end.
    """

    escaped_marker = marker.replace("'", "''")
    op.execute(
        f"""
        DO $$
        DECLARE
            existing text;
            matched int;
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint con
                JOIN pg_class rel ON rel.oid = con.conrelid
                JOIN pg_namespace ns ON ns.oid = rel.relnamespace
                WHERE ns.nspname = current_schema()
                  AND rel.relname = '{table}'
                  AND con.conname = '{target}'
            ) THEN
                RETURN;
            END IF;

            SELECT count(*), min(con.conname) INTO matched, existing
            FROM pg_constraint con
            JOIN pg_class rel ON rel.oid = con.conrelid
            JOIN pg_namespace ns ON ns.oid = rel.relnamespace
            WHERE ns.nspname = current_schema()
              AND rel.relname = '{table}'
              AND con.contype = 'c'
              AND pg_get_constraintdef(con.oid) LIKE '%{escaped_marker}%';

            IF matched <> 1 THEN
                RAISE EXCEPTION
                    'expected exactly one check constraint on % matching %, found %',
                    '{table}', '{escaped_marker}', matched;
            END IF;

            EXECUTE format(
                'ALTER TABLE %I RENAME CONSTRAINT %I TO %I', '{table}', existing, '{target}'
            );
        END
        $$;
        """
    )


def upgrade() -> None:
    """Give each constraint the name its model metadata already resolves to."""

    for table, marker, aligned, _legacy in _RENAMES:
        _rename(table=table, marker=marker, target=aligned)


def downgrade() -> None:
    """Restore the truncated names this revision replaced.

    The legacy names are spelled out because they cannot be derived: they are a
    hash of a string that no longer exists anywhere in the codebase. Restoring
    them exactly is what makes the downgrade/upgrade round trip land back on the
    schema the previous revision produced.
    """

    for table, marker, _aligned, legacy in _RENAMES:
        _rename(table=table, marker=marker, target=legacy)

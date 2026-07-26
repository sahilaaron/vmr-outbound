#!/usr/bin/env python3
"""DAT-014 Layer 4B database assertions, scoped to the sanctioned acceptance captures.

Read-only. Every statement is a SELECT; nothing here writes, deletes, resets or
"repairs" any row. No credential is typed or printed: the database is reached
through the application's own settings.

Why the scoping exists
----------------------
The Layer 4B acceptance runs against a local database that also accumulates
unrelated captures — notably those created while exercising the capture
extension against real LinkedIn pages. Those rows are legitimate data and are
deliberately left untouched, but they carry their own enrichment records,
lookup statuses and attempt counts.

An earlier version of this harness graded every capture-owned row in the
database. That produced false failures: check A failed on an unrelated capture
whose lookup was ``API_UNAVAILABLE``, check C failed because that capture was
never confirmed, and check C2 reported an aggregate attempt total that mixed
acceptance attempts with unrelated ones. None of those were DAT-014 defects —
they were scoping defects in this harness.

Graded checks therefore operate ONLY on the sanctioned synthetic acceptance
captures passed via ``--capture``. Everything else is reported in a sanitized
informational section: how many captures were excluded and how many provider
attempts they account for, and nothing else. No name, URL, company, payload or
any other personal data from an excluded row is read or printed.

Excluded rows are never a reason to fail, and are never presented as accepted.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from dataclasses import dataclass, field
from typing import Any

# The two committed fictitious identities used by the Layer 4B acceptance run.
DEFAULT_SANCTIONED_CAPTURES: tuple[str, ...] = (
    "1b9ea638-12d5-4066-b391-6faedb31d21a",  # Morgan Vale  @ Mozilla (live lookup, 2 attempts)
    "737dc59a-af6e-4474-803e-951d2ce8c1d9",  # Riley Chen   @ Mozilla (prior mapping, 0 attempts)
)
EXPECTED_DATABASE = "vmr_dat014"


@dataclass(frozen=True)
class Check:
    """One graded or informational assertion."""

    key: str
    title: str
    sql: str
    verdict_column: str | None = None

    @property
    def graded(self) -> bool:
        return self.verdict_column is not None


@dataclass
class Result:
    """The outcome of evaluating every check."""

    rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)
    empty: list[str] = field(default_factory=list)
    excluded: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.failed and not self.empty


# --- Checks -------------------------------------------------------------------
#
# Every capture-scoped or enrichment-scoped query filters on :captures. Contact
# scoped queries filter on the contacts those captures were promoted into, so an
# unrelated contact can neither pass nor fail an acceptance check.

_SCOPED_ENRICHMENTS = "capture_id = ANY(:captures)"
_SCOPED_PROMOTIONS = "capture_id = ANY(:captures)"
_PROMOTED_CONTACTS = (
    "SELECT promoted_contact_id FROM contact_capture_promotions "
    "WHERE capture_id = ANY(:captures) AND promoted_contact_id IS NOT NULL"
)


def build_checks() -> list[Check]:
    return [
        Check(
            "A",
            "the real logo.dev client ran and recorded itself truthfully",
            f"""
            SELECT provider, lookup_version, lookup_status::text AS lookup_status,
                   lookup_attempts, (looked_up_at IS NOT NULL) AS has_timestamp,
                   (batch_id IS NULL AND capture_id IS NOT NULL) AS capture_owned,
                   lookup_query, normalized_query,
                   CASE WHEN provider = 'logo.dev' AND lookup_status = 'OK'
                             AND lookup_attempts >= 1 AND looked_up_at IS NOT NULL
                        THEN 'PASS'
                        WHEN lookup_status = 'NOT_STARTED' AND lookup_attempts = 0
                        THEN 'PASS'
                        ELSE 'FAIL' END AS verdict
            FROM salesnav_company_enrichments
            WHERE {_SCOPED_ENRICHMENTS} ORDER BY created_at
            """,
            "verdict",
        ),
        Check(
            "B",
            "every candidate - live and rejected - keeps its rank and a null confidence",
            f"""
            SELECT bucket, c->>'domain' AS domain, c->>'name' AS provider_name,
                   c->>'rank' AS rank, (c ? 'confidence') AS confidence_key_present,
                   (c->'confidence' = 'null'::jsonb) AS confidence_is_null,
                   CASE WHEN (c->>'rank') IS NOT NULL AND (c ? 'confidence')
                             AND (c->'confidence' = 'null'::jsonb)
                        THEN 'PASS' ELSE 'FAIL' END AS verdict
            FROM (
              SELECT 'awaiting/confirmed' AS bucket, jsonb_array_elements(candidates) AS c
              FROM salesnav_company_enrichments WHERE {_SCOPED_ENRICHMENTS}
              UNION ALL
              SELECT 'rejected', jsonb_array_elements(rejected_candidates)
              FROM salesnav_company_enrichments WHERE {_SCOPED_ENRICHMENTS}
            ) t ORDER BY bucket, rank
            """,
            "verdict",
        ),
        Check(
            "C",
            "nothing was auto-confirmed: a human decided and is attributed",
            f"""
            SELECT confirmation_status::text AS confirmation_status,
                   confirmation_source::text AS confirmation_source,
                   confirmed_domain, (confirmed_by IS NOT NULL) AS has_actor,
                   (confirmed_at IS NOT NULL) AS has_time,
                   CASE WHEN confirmation_source IN ('CANDIDATE','MANUAL','PRIOR_MAPPING')
                             AND confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL
                        THEN 'PASS' ELSE 'FAIL' END AS verdict
            FROM salesnav_company_enrichments
            WHERE {_SCOPED_ENRICHMENTS} ORDER BY created_at
            """,
            "verdict",
        ),
        Check(
            "C2",
            "provider attempts inside the acceptance scope match what was authorised",
            f"""
            SELECT coalesce(sum(lookup_attempts), 0) AS scoped_provider_attempts,
                   :expected_attempts AS authorised_attempts,
                   count(*) FILTER (WHERE confirmation_source = 'PRIOR_MAPPING'
                                      AND lookup_attempts = 0) AS reused_without_lookup,
                   CASE WHEN coalesce(sum(lookup_attempts), 0) = :expected_attempts
                             AND count(*) FILTER (WHERE confirmation_source = 'PRIOR_MAPPING'
                                                    AND lookup_attempts > 0) = 0
                        THEN 'PASS' ELSE 'FAIL' END AS verdict
            FROM salesnav_company_enrichments WHERE {_SCOPED_ENRICHMENTS}
            """,
            "verdict",
        ),
        Check(
            "D",
            "a rejected candidate is preserved as a decision, with a reason",
            f"""
            SELECT r->>'domain' AS domain, r->>'rejection_reason' AS reason,
                   r->>'rejected_by' AS decided_by,
                   (r->>'rejected_at' IS NOT NULL) AS has_time,
                   CASE WHEN r->>'rejection_reason' IS NOT NULL
                             AND r->>'rejected_by' IS NOT NULL
                        THEN 'PASS' ELSE 'FAIL' END AS verdict
            FROM salesnav_company_enrichments e,
                 LATERAL jsonb_array_elements(e.rejected_candidates) AS r
            WHERE e.{_SCOPED_ENRICHMENTS}
            """,
            "verdict",
        ),
        Check(
            "E",
            "the Company was resolved by exact domain and not duplicated",
            f"""
            SELECT domain, count(*) AS company_rows,
                   CASE WHEN count(*) = 1 THEN 'PASS' ELSE 'FAIL' END AS verdict
            FROM companies
            WHERE domain IN (SELECT resolved_domain FROM contact_capture_promotions
                              WHERE {_SCOPED_PROMOTIONS} AND resolved_domain IS NOT NULL)
            GROUP BY domain
            """,
            "verdict",
        ),
        Check(
            "F",
            "the Contact was created or linked, on the resolved domain, with no invented email",
            f"""
            SELECT p.company_outcome::text AS company_outcome,
                   p.contact_outcome::text AS contact_outcome, p.resolved_domain,
                   ct.first_name || ' ' || ct.last_name AS contact,
                   ct.company_domain, ct.title, (ct.email IS NULL) AS email_left_null,
                   p.labels_applied::text AS labels_applied, p.notes_linked,
                   CASE WHEN p.contact_outcome IN ('CONTACT_CREATED',
                                                   'CONTACT_EXACT_MATCH_LINKED',
                                                   'ALREADY_PROMOTED')
                             AND ct.company_domain = p.resolved_domain
                             AND ct.email IS NULL
                        THEN 'PASS' ELSE 'FAIL' END AS verdict
            FROM contact_capture_promotions p
            JOIN contacts ct ON ct.id = p.promoted_contact_id
            WHERE p.{_SCOPED_PROMOTIONS}
            ORDER BY p.created_at
            """,
            "verdict",
        ),
        Check(
            "G",
            "promotion is idempotent at the database level",
            f"""
            SELECT count(*) AS promotions, count(DISTINCT capture_id) AS distinct_captures,
                   CASE WHEN count(*) = count(DISTINCT capture_id)
                        THEN 'PASS' ELSE 'FAIL' END AS verdict
            FROM contact_capture_promotions WHERE {_SCOPED_PROMOTIONS}
            """,
            "verdict",
        ),
        Check(
            "H",
            "labels carried over from the capture",
            f"""
            SELECT l.name, l.slug, count(*) AS assignments,
                   CASE WHEN count(*) > 0 THEN 'PASS' ELSE 'FAIL' END AS verdict
            FROM contact_label_assignments a JOIN contact_labels l ON l.id = a.label_id
            WHERE a.contact_id IN ({_PROMOTED_CONTACTS})
            GROUP BY l.name, l.slug ORDER BY l.name
            """,
            "verdict",
        ),
        Check(
            "I",
            "notes carried over, linked, none rewritten",
            """
            SELECT count(*) AS notes,
                   count(*) FILTER (WHERE contact_id IS NOT NULL) AS linked_to_contact,
                   CASE WHEN count(*) > 0
                             AND count(*) = count(*) FILTER (WHERE contact_id IS NOT NULL)
                        THEN 'PASS' ELSE 'FAIL' END AS verdict
            FROM contact_capture_notes WHERE capture_id = ANY(:captures)
            """,
            "verdict",
        ),
        Check(
            "J",
            "the DAT-013 capture rows (compare content_hash with the capture_state baseline)",
            """
            SELECT s.id::text AS capture_id,
                   (s.matched_contact_id IS NOT NULL) AS linked_to_contact,
                   (s.payload IS NOT NULL) AS payload_present, s.content_hash,
                   (SELECT count(*) FROM linkedin_profile_experience_observations o
                     WHERE o.snapshot_id = s.id) AS experiences
            FROM linkedin_profile_snapshots s
            WHERE s.id = ANY(:captures)
            ORDER BY s.ingested_at
            """,
        ),
        Check(
            "K",
            "provenance was appended under the DAT-005 policy",
            f"""
            SELECT field_name, source_name, count(*) AS observations,
                   count(*) FILTER (WHERE is_current_winner) AS current_winners,
                   count(*) FILTER (WHERE is_manual_override) AS manual_overrides
            FROM contact_field_values
            WHERE contact_id IN ({_PROMOTED_CONTACTS})
            GROUP BY field_name, source_name ORDER BY field_name
            """,
        ),
        Check(
            "L",
            "promotion created identity, not permission - every count must be 0",
            f"""
            WITH promoted AS ({_PROMOTED_CONTACTS})
            SELECT (SELECT count(*) FROM campaign_contacts
                     WHERE contact_id IN (SELECT * FROM promoted))        AS campaign_memberships,
                   (SELECT count(*) FROM email_candidates
                     WHERE contact_id IN (SELECT * FROM promoted))        AS email_candidates,
                   (SELECT count(*) FROM exact_email_verifications
                     WHERE contact_id IN (SELECT * FROM promoted))        AS verifications,
                   (SELECT count(*) FROM scores
                     WHERE contact_id IN (SELECT * FROM promoted))        AS scores,
                   (SELECT count(*) FROM draft_versions
                     WHERE contact_id IN (SELECT * FROM promoted))        AS drafts,
                   (SELECT count(*) FROM draft_approvals da
                     JOIN draft_versions dv ON dv.id = da.draft_version_id
                     WHERE dv.contact_id IN (SELECT * FROM promoted))     AS approvals,
                   CASE WHEN (SELECT count(*) FROM campaign_contacts
                               WHERE contact_id IN (SELECT * FROM promoted)) = 0
                             AND (SELECT count(*) FROM email_candidates
                                   WHERE contact_id IN (SELECT * FROM promoted)) = 0
                             AND (SELECT count(*) FROM exact_email_verifications
                                   WHERE contact_id IN (SELECT * FROM promoted)) = 0
                             AND (SELECT count(*) FROM scores
                                   WHERE contact_id IN (SELECT * FROM promoted)) = 0
                             AND (SELECT count(*) FROM draft_versions
                                   WHERE contact_id IN (SELECT * FROM promoted)) = 0
                             AND (SELECT count(*) FROM draft_approvals da
                                   JOIN draft_versions dv ON dv.id = da.draft_version_id
                                   WHERE dv.contact_id IN (SELECT * FROM promoted)) = 0
                        THEN 'PASS' ELSE 'FAIL' END AS verdict
            """,
            "verdict",
        ),
        Check(
            "M",
            "the suppression ledger was not touched",
            """
            SELECT (SELECT count(*) FROM suppressions) AS suppressions,
                   (SELECT count(*) FROM suppression_events) AS suppression_events
            """,
        ),
        Check(
            "N",
            "audit trail (whole database, informational)",
            "SELECT action, count(*) AS events FROM audit_events GROUP BY action ORDER BY action",
        ),
    ]


EXCLUDED_SQL = """
SELECT count(*)                              AS excluded_captures,
       coalesce(sum(lookup_attempts), 0)     AS excluded_provider_attempts
FROM salesnav_company_enrichments
WHERE capture_id IS NOT NULL AND NOT (capture_id = ANY(:captures))
"""


def evaluate(connection: Any, captures: list[str], expected_attempts: int) -> Result:
    """Run every check. Read-only; returns rows and verdicts, prints nothing."""

    import sqlalchemy as sa

    result = Result()
    params = {"captures": captures, "expected_attempts": expected_attempts}
    for check in build_checks():
        rows = [dict(r) for r in connection.execute(sa.text(check.sql), params).mappings().all()]
        result.rows[check.key] = rows
        if not check.graded:
            continue
        if not rows:
            result.empty.append(check.key)
        elif any(str(r.get(check.verdict_column)) == "FAIL" for r in rows):
            result.failed.append(check.key)

    excluded = connection.execute(sa.text(EXCLUDED_SQL), {"captures": captures}).mappings().first()
    result.excluded = dict(excluded) if excluded else {}
    return result


def _render_table(rows: list[dict[str, Any]], out: io.StringIO) -> None:
    if not rows:
        out.write("  (no rows)\n")
        return
    columns = list(rows[0].keys())
    cells = [[("" if r[c] is None else str(r[c])) for c in columns] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(columns)]
    out.write("  " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(columns)).rstrip() + "\n")
    out.write("  " + "-+-".join("-" * w for w in widths) + "\n")
    for row in cells:
        out.write("  " + " | ".join(v.ljust(widths[i]) for i, v in enumerate(row)).rstrip() + "\n")


def render(
    result: Result,
    *,
    database: str,
    captures: list[str],
    expected_attempts: int,
    attempts_note: str | None,
) -> str:
    out = io.StringIO()
    out.write("DAT-014 Layer 4B - live acceptance assertions\n")
    out.write(f"database: {database} (read-only; no credential printed)\n")
    out.write("acceptance scope: the sanctioned synthetic captures below only\n")
    for capture_id in captures:
        out.write(f"  - {capture_id}\n")
    out.write(f"authorised provider attempts in scope: {expected_attempts}\n")
    if attempts_note:
        out.write(f"attempts note: {attempts_note}\n")

    checks = {c.key: c for c in build_checks()}
    for key, rows in result.rows.items():
        out.write(f"\n=== {key}. {checks[key].title} ===\n")
        _render_table(rows, out)

    out.write("\n=== excluded from the acceptance scope (informational, sanitized) ===\n")
    out.write(
        "  Captures in this database that are NOT part of the DAT-014 acceptance -\n"
        "  chiefly those created while exercising the capture extension against real\n"
        "  LinkedIn pages. They are legitimate data, are deliberately left untouched,\n"
        "  and are neither graded nor accepted here. Only counts are reported: no\n"
        "  name, URL, company, payload or any other personal data is read or printed.\n"
    )
    _render_table([result.excluded] if result.excluded else [], out)
    out.write(
        "  These rows carry their own lookup statuses and attempt counts. Their\n"
        "  attempts are NOT added to the acceptance total in check C2, and their\n"
        "  statuses cannot fail checks A, C or any other graded check.\n"
        "  Separately tracked: the real-DOM top-card extraction defect these captures\n"
        "  demonstrate is DAT-016 (#167), which remains open. Nothing here accepts it.\n"
    )

    out.write("\n=== summary ===\n")
    graded = [c.key for c in build_checks() if c.graded]
    passed = [k for k in graded if k not in result.failed and k not in result.empty]
    out.write(f"  graded checks : {', '.join(graded)}\n")
    out.write(f"  passed        : {', '.join(passed) or 'none'}\n")
    out.write(f"  failed        : {', '.join(result.failed) or 'none'}\n")
    out.write(f"  no rows       : {', '.join(result.empty) or 'none'}\n")
    out.write(
        f"  ungraded (informational): {', '.join(c.key for c in build_checks() if not c.graded)}\n"
    )
    out.write(f"\n  OVERALL: {'PASS' if result.passed else 'FAIL'}\n")
    return out.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None, help="also write the report to this file")
    parser.add_argument(
        "--expect-attempts",
        type=int,
        default=2,
        help=(
            "provider attempts this acceptance was authorised to make, counted "
            "WITHIN the sanctioned capture scope only (check C2)"
        ),
    )
    parser.add_argument(
        "--attempts-note", default=None, help="why that many; printed verbatim in the report"
    )
    parser.add_argument(
        "--capture",
        action="append",
        default=None,
        help="sanctioned acceptance capture id; repeatable. Defaults to the two fixture captures.",
    )
    args = parser.parse_args(argv)

    sys.path.insert(0, os.getcwd())
    try:
        import sqlalchemy as sa
        from app.core.config import get_settings
        from sqlalchemy.engine import make_url
    except ModuleNotFoundError as exc:
        print(f"cannot import the application ({exc.name}).", file=sys.stderr)
        print(f"cwd is {os.getcwd()!r} - run this from the DAT-014 worktree root", file=sys.stderr)
        return 2

    settings = get_settings()
    database = make_url(settings.database_url).database
    if database != EXPECTED_DATABASE:
        print(
            f"refusing to run against {database!r} - expected {EXPECTED_DATABASE!r}",
            file=sys.stderr,
        )
        return 2

    captures = list(args.capture or DEFAULT_SANCTIONED_CAPTURES)
    engine = sa.create_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            result = evaluate(connection, captures, args.expect_attempts)
    finally:
        engine.dispose()

    report = render(
        result,
        database=database or "",
        captures=captures,
        expected_attempts=args.expect_attempts,
        attempts_note=args.attempts_note,
    )
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(report)
        print(f"written to {args.out}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

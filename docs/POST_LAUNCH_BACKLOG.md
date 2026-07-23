# Post-launch backlog (do not build before the first-campaign review)

This is a holding place for useful ideas surfaced during development that are
**out of scope** for the first 100-contact campaign. Recording an item here does
**not** authorize building it. Moving anything into launch scope requires an
explicit update to `GOAL.md` (see the Scope-Change Rule there and the Backlog
Admission Test in `GITHUB_BACKLOG.md`).

The canonical parked list lives in `GITHUB_BACKLOG.md` (P1/P2 cards and the
`FUT-*` parked backlog). This file only captures ideas that come up mid-build so
they are not lost or implemented opportunistically.

## Captured during Phase 0

- **psycopg 3.3.x compatibility.** We pinned `psycopg[binary]<3.3` because 3.3.x
  returned text columns as bytes and broke the SQLAlchemy dialect in local
  testing. Revisit the pin once a fixed 3.3.x / SQLAlchemy combination is
  available. (Engineering hygiene, not launch-blocking.)
- **Structured application logging.** Phase 0 ships without a logging framework.
  Add safe, secret-free structured logs when the first background jobs land
  (OPS-003), not before.
- **Health/readiness for external providers.** `/ready` currently checks only the
  database. Extend the system-health view to provider reachability when
  MillionVerifier/Saleshandy adapters exist (VER-006 / SHY-*).

## Captured during Phase 1 (Data & Campaigns, first slice)

- **Company entity and company-level dedup.** This slice normalizes company
  name/domain on the contact but has no `companies` table. Introduce one when
  company-contact saturation controls (CMP-004) or company insights (INS-*) need
  a shared company record. (DAT-004 full.)
- **Uncertain-match review queue.** Ambiguous natural-key matches are currently
  kept separate (a possible false duplicate, never a wrong merge) with an
  explanatory note. Add a human review/reconciliation queue when real import
  volume shows it is needed. (DAT-004.)
- **Immutability enforcement at the database.** `import_rows.raw_data` is treated
  as write-once by convention. Add a DB trigger/rule to hard-enforce immutability
  if a later requirement demands it.
- **Country and title canonicalization.** Normalization stays conservative (no
  synonym maps). Add curated country/title canonicalization only if scoring or
  targeting proves it necessary.

Add new items as `- **Title.** One or two sentences, and which real trigger
would justify building it.`

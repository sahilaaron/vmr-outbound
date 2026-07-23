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

Add new items as `- **Title.** One or two sentences, and which real trigger
would justify building it.`

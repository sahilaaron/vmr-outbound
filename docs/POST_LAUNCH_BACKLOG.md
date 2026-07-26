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

- **Operator UI for resolving profile-snapshot review candidates.** Weak-match
  and ambiguous snapshots (DAT-012E) are stored with review candidates but the
  workbench only displays them; add confirm/reject actions (reusing the
  DAT-004 identity-resolution flow) when real captures produce enough review
  volume to justify it.
- **QA-policy threshold configuration surface.** profile-employment-qa/1.0.0
  thresholds are code defaults recorded per evaluation; expose them as
  operator settings only if the pilot shows the defaults misfire.
- **Contact-side company linking from profile captures.** Experience
  observations carry company LinkedIn URLs/ids; linking them to the companies
  table (beyond DAT-012G's evidence matching) is deferred until scoring or
  research needs it.
- **About/skills/education capture on person profiles.** First release
  captures top card + experience only; add further sections when a concrete
  scoring or drafting need exists.

## Deferred by DAT-013 (contact-first acquisition)

Deliberately out of the contact-first refactor. Each is a real idea; none was
needed to make acquisition independently useful, and several would have weakened
the truthful-extraction standard if forced in.

| Idea | Why deferred |
| --- | --- |
| Creating a canonical Contact directly from a capture | A contact requires a company domain, which a LinkedIn page never shows. Inventing one would be fabricated evidence. Tracked as DAT-014, behind domain resolution. |
| Education observations | The main profile page's education block has no fixture coverage and no proven selector strategy. Adding it blind would ship a parser that fails silently. |
| Visible contact information (websites, public email/phone) | LinkedIn renders these behind the contact-info modal. Opening it is UI automation the safety rules exclude; there is no reliable already-rendered source. |
| Recent activity capture | Reliable activity capture needs navigation to the activity feed. The extension never navigates, so this stays deferred rather than half-implemented. |
| Individual Sales Navigator lead page as a capture surface | Investigated and NOT added: it would need a new host permission scope, and the lead page exposes no canonical `/in/` URL, so every capture would stay unmatched — the same outcome the results-row path already produces safely. |
| Label management UI (rename, merge, delete, colour) | The extension must not become a taxonomy manager. The backend owns the registry; management belongs in the workbench if real use proves the need. |
| Bulk label/note editing across saved contacts | No evidence of the need before the first real acquisition run. |

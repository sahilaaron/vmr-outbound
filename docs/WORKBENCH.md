# Operator Workbench (Phase 1 slice)

A local, server-rendered control surface for the data-and-campaigns foundation:
create campaigns, run staged CSV/XLSX imports with a deliberate
preview-then-confirm flow, and inspect every resulting record (batches, rows,
contacts, provenance, suppression state). Everything shown is read from the
local development database — the workbench renders no simulated data, and no
outreach capability exists anywhere in it.

## Scope

Functional areas: **Overview**, **Campaigns**, **Imports**, **Contacts**, and
the guarded **Local Tools** panel (local development only).

The navigation also lists the later-phase areas — Email Verification, Scoring,
Research, Drafts & Approval, Sequences, Activity, Settings — as visibly
disabled entries. Each leads to one clean "isn't available yet" state. There
are no fake tables, scores, drafts, sequences, verification results, or
simulated sending activity anywhere.

## Running it locally

```bash
# prerequisites: docs/DEVELOPMENT.md (Python 3.11+, local UTF-8 Postgres, migrations)
FEATURES__WORKBENCH=true FEATURES__CSV_IMPORT=true \
  uvicorn app.main:app --reload --port 8000
# open http://127.0.0.1:8000/
```

Or set the two switches in `.env` (`FEATURES__WORKBENCH=true`,
`FEATURES__CSV_IMPORT=true`). Both default **off**: without them the UI routes
do not exist at all (404), per the FND-007 disabled-until-verified rule.
`FEATURES__WORKBENCH` mounts the pages; `FEATURES__CSV_IMPORT` authorizes the
commit step of an import (the wizard's earlier steps work without it, but
confirm refuses).

There is **no live RDS deployment** of the workbench. It is a local operator
tool; the guarded reset controls additionally refuse any non-loopback database.

## Supported and unsupported formats

Supported: `.csv` (UTF-8, header row) and `.xlsx`.
Not supported (rejected visibly at upload): legacy `.xls`, Google Sheets links,
and every other format. Malformed or empty workbooks are rejected with an
actionable message; an unreadable file confirmed through the API path becomes a
visible FAILED batch, never a silent success.

Both formats run through **one** shared pipeline (parse → map → validate →
normalize → dedup → suppress → persist, `app/services/imports/`). The
format-specific code ends at `parsing.py`, which renders CSV and XLSX into the
same neutral rows; no business rule is duplicated per format. For XLSX the
workbook filename, sheet name, sheet index, and original per-sheet row number
are preserved on every stored raw row.

## The preview → confirm import flow

Imports are a deliberate two-step process; the old single-shot API route
(`POST /campaigns/{id}/imports`) still exists unchanged for programmatic CSV
use, but the workbench always stages first:

1. **Upload** — choose the target campaign, attach the file, optionally record
   provenance (source name/reference, exporter, export date).
2. **Sheets & mapping** — for a workbook, inspect every sheet (name, row count,
   columns found) and deliberately select the sheet(s) to import; then map
   source columns to system fields. Exact names and common aliases are
   suggested automatically; the operator always confirms. Required fields
   (`first_name`, `last_name`, `company_name`, `company_domain`) must be
   mapped; duplicate targets are rejected with specific messages.
3. **Preview & validate** — a true dry run over the shared pipeline: predicted
   accept/reject/duplicate/ambiguous/suppressed outcome per row, every
   validation problem listed. **Preview writes nothing** — no contacts,
   memberships, suppressions, batches, rows, or outcomes (proven by test).
4. **Confirm** — commits that exact interpretation through the same pipeline
   the API uses. The confirmed column mapping and mapper/parser versions are
   stored on the batch (`import_batches.column_mapping`), so a batch's
   interpretation of its file stays reproducible.

### Staged uploads (cleanup and expiry)

Between steps the file lives on local disk under `var/staged_uploads/`
(configurable via `STAGED_UPLOADS_DIR`) — never in the database. Each staged
upload expires **24 hours** after upload; expired entries are purged
opportunistically whenever the staging area is listed or read, so no background
job is required. Discarding a staged upload removes it immediately.

Repeated confirmation cannot duplicate an import, twice over: the staged record
remembers the batch it produced (re-submitting redirects to it), and the
importer independently reuses a completed batch with the same content hash —
which now covers content **plus** sheet selection **plus** mapping, so the same
bytes with a deliberately different interpretation are a new import while a
straight retry is not.

## Ambiguous rows (DAT-004-compatible representation)

A row whose identity match is uncertain — several existing contacts share its
natural key and no email disambiguates — is no longer accepted-with-a-note. It
gets the explicit `ambiguous` outcome: **no contact is created, no membership
is created, nothing merges silently**, the reason is recorded on the row, and
the batch page surfaces a review notice with an `ambiguous` filter. Resolution
is manual for now (fix the source data — typically adding an email — and
re-import). The full DAT-004 review queue/action remains open scope.

## Local Tools (safety)

Local-development controls: load a representative synthetic CSV fixture, load a
representative multi-sheet synthetic XLSX fixture, clear all local test data,
or reset to a known demo state. Guard rails:

* Available only when `APP_ENV=local`; otherwise the routes 404.
* Every action re-checks `ensure_local_database()`: it refuses unless the
  configured database host is a loopback address (127.0.0.1 / localhost / ::1).
  A live or remote database — including any RDS endpoint — can never be
  targeted, regardless of flags.
* Destructive actions require typing `RESET` into a confirmation field.
* Fixture data is synthetic only (`*.example.com` identities).
* Fixture loads and resets write audit events; the reset records its own audit
  event after clearing, so the wipe leaves a trace.

## Known limitations

* Ambiguous rows are reviewable but not yet actionable in the UI (no
  merge/assign action); resolution is via corrected re-import.
* The preview's row-by-row table shows the first 50 rows (all problems and all
  counts cover the full file; all rows are browsable on the batch after
  confirm).
* Campaigns carry only the authorized CMP-001 fields (name, description,
  status); offer/audience/threshold/tone/owner settings are open CMP-001 scope.
* Contact search is a simple case-insensitive substring match; no full-text
  index yet.
* No authentication — the workbench binds to localhost for a single local
  operator; access control is later-phase scope.
* Later-phase areas are navigation stubs by design.

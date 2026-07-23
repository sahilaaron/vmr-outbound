# Phase 1 — Data & Campaigns: evidence map (first slice)

This is the first build slice of Phase 1. It delivers the core data foundation
and the staged CSV import (DAT-001, DAT-002). It deliberately does **not** touch
email verification, scoring, research, drafting, or sending.

## What each card gets from this slice

| Card | Outcome in this slice | Evidence |
| --- | --- | --- |
| **DAT-001** Core RDS schema | Version-controlled schema for the Phase 1 data foundation: campaigns, contacts, campaign membership, import batches, immutable raw rows, per-row validation results and errors, provenance records, suppression ledger. All via a committed Alembic migration with constraints, indexes, timestamps, and FK relationships. | `app/models/`, `migrations/versions/c11379ba2041_*.py` |
| **DAT-002** Staged CSV import + row validation | Every upload creates a batch, preserves raw rows, validates required columns/values with actionable row-level errors, normalizes accepted rows, dedups conservatively, checks suppressions, and commits contacts + membership only after validation. Produces an import summary. Malformed rows are retained, never dropped. | `app/services/imports/`, `tests/test_imports.py`, `tests/fixtures/contacts_representative.csv` |
| DAT-003 Normalize company/contact data | Conservative normalization (trim/collapse, lowercase email + host, URL cleanup) with originals preserved on the immutable raw row. *Foundation only* — a dedicated companies entity and country-name canonicalization are not built here. | `app/services/imports/normalization.py` |
| DAT-004 Deduplicate / resolve contacts | Deterministic, explainable matching: exact normalized email, else exact natural key; ambiguous natural-key matches are kept separate. *Foundation only* — company-level dedup and a human review queue are deferred. | `app/services/imports/dedup.py` |
| DAT-005 Provenance & freshness | Each contact observation appends a provenance record (source, reference, exporter, export date, observation time); imports append, never overwrite. | `app/models/provenance.py` |
| DAT-006 Suppression ledger | Independent ledger of suppressed emails/domains, consulted on every row; a suppressed identity cannot become eligible, and when it matches an existing contact **every** non-terminal campaign membership for that contact (across all campaigns) is transitioned to `SUPPRESSED`. *Foundation only* — full eligibility gating across all outreach routes lands with verification/scoring/sending. | `app/models/suppression.py`, `app/services/suppressions.py`, `app/services/imports/importer.py` |
| CMP-001 Campaign creation | Minimum campaign shell needed to receive an import (name, description, status). Richer settings (offer, tone, audience rules, sending reference) are added when their phases need them. | `app/services/campaigns.py` |
| CMP-002 Contact workflow states | Explicit per-campaign membership states with a validated transition map; illegal transitions raise; transitions are audited. Only import-stage states are wired. | `app/models/enums.py`, `app/services/contact_state.py` |
| CMP-003 Link contacts to campaigns | Contacts join campaigns through a unique `(campaign, contact)` membership, so a contact can appear in several campaigns without a duplicate active-outreach record. | `app/models/campaign.py` |

## Import behaviour (exact)

1. **Feature gate.** The importer refuses to run unless `FEATURES__CSV_IMPORT`
   is on (default off). The API import route returns 404 while disabled.
2. **Raw capture (committed first).** A batch is created and every original row
   is written verbatim to `import_rows`, then committed — durable even if later
   processing fails.
3. **Structure gate.** Before any row is processed, the CSV structure is checked:
   a missing/unreadable header, a header missing any required column (which also
   catches a headerless file), or a header with no data rows is recorded as a
   batch-level `FAILED` with an actionable `error_detail`. Such a file never
   becomes a completed zero-row import; the raw evidence captured in step 2 is
   preserved.
4. **Processing (single transaction).** Each row is validated independently;
   rejected rows keep actionable `import_row_errors`; accepted rows are
   normalized, deduplicated, suppression-checked, and only then committed as
   contacts + memberships + provenance. On any failure the processing
   transaction rolls back (no partial contacts) and the batch is marked
   `FAILED`; the raw rows remain.
5. **Idempotency.** An identical file re-imported into the same campaign
   short-circuits to the prior summary; overlapping-but-different files reconcile
   through deduplication rather than creating duplicate contacts.
6. **Summary.** Per-row outcomes (`accepted` / `rejected` / `duplicate` /
   `suppressed`) are mutually exclusive and account for every row.

## Verification performed

- `ruff check .`, `ruff format --check .` — clean.
- `python -m mypy app` (strict) — no issues.
- `alembic upgrade head` + `alembic check` — applies and matches models.
- Migration round trip `upgrade -> downgrade -> upgrade` — clean, including
  explicit ENUM-type drops; now enforced as a CI step.
- `python -m pytest` — full suite passes against local PostgreSQL 16.

## Deliberately deferred (later slices / phases)

Company entity and company-level dedup, uncertain-match review queue
(DAT-004 full), representative **historical** import (DAT-007) and additional
file shapes (DAT-008), company-contact saturation controls (CMP-004), batch
stage actions and stage-count surfaces (CMP-005), and the dashboard build-out.
No verification, scoring, insights, drafting, or Saleshandy behaviour is
included; those feature switches remain off.

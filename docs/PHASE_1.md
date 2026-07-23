# Phase 1 — Data & Campaigns: evidence map (first slice)

This tracks the Phase 1 build slices. The first slices delivered the core data
foundation and the staged CSV import (DAT-002); a follow-up slice completed the
DAT-001 core schema (see "DAT-001 core-schema completion" below). None of this
touches email verification, scoring, research, drafting, or sending behaviour —
those remain later phases and their feature switches stay off.

## First-launch import boundary

Authorized spreadsheet import supporting **CSV and XLSX**. Legacy `.xls`, Google
Sheets direct import, and other spreadsheet formats are **out of scope** until
explicitly approved. XLSX *parsing* is not implemented in the DAT-001 slice; only
the schema is prepared for it (source format, MIME type, parser/mapper version,
and per-sheet row identity).

## What each card gets from this slice

| Card | Outcome in this slice | Evidence |
| --- | --- | --- |
| **DAT-001** Core RDS schema | Completed across two migrations: the data foundation (campaigns, contacts, membership, import batches, immutable raw rows, validation results/errors, provenance, suppression ledger) plus companies; three distinct email-evidence tables (exact-address verifications, domain-pattern observations, mail-domain observations); insights + evidence references; versioned scores, components, and score-evidence; immutable draft versions and exact-version approvals; external-provider events with duplicate protection; audit records. Import schema carries CSV/XLSX format metadata. Representation only — no later-phase behaviour. | `app/models/`, `migrations/versions/c11379ba2041_*.py`, `migrations/versions/b84699f38ef5_*.py`, `tests/test_schema_dat001.py` |
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

## DAT-001 core-schema completion (representation only)

A follow-up slice completes the DAT-001 schema. Added tables: `companies`;
`exact_email_verifications`, `domain_pattern_observations`,
`mail_domain_observations` (three structurally distinct email-evidence kinds so
exact-address, domain-pattern, and mail-domain/catch-all facts can never be
conflated); `insights` + `insight_evidence`; `scores`, `score_components`,
`score_evidence` (versioned, explainable, with rule version, component values,
total, reason, calculation time, and evidence links); `draft_versions`
(immutable) + `draft_approvals` (each approval references exactly one draft
version); `external_events` (provider, stable external id, event type, received
time, controlled payload, and a `(provider, external_event_id)` unique constraint
for duplicate protection). Audit records reuse the existing `audit_events` table.

The import schema now records `source_format` (`csv`/`xlsx`), `mime_type`,
`parser_version`, `mapper_version`, the file `content_hash`, and per-row
`sheet_name`/`sheet_index`, so it does not assume a flat CSV forever. `csv` stays
the default, and a flat CSV is represented as a single sheet (index 0), which
preserves the existing importer and per-batch row uniqueness.

This is **database representation only**. It creates the tables but implements
none of the later-phase behaviour: no MillionVerifier integration, no email
generation or verification, no score calculation, no research, no draft
generation, no approval workflow/UI, no Saleshandy or webhook processing, and no
XLSX parsing or broader file-format support. No live RDS deployment was performed;
migrations are proven on local PostgreSQL 16 only.

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

## Operator-workbench slice (this branch)

Adds the local operator workbench described in `docs/WORKBENCH.md`: a
server-rendered FastAPI + Jinja2 shell (no SPA, no Node build) with functional
Overview / Campaigns / Imports / Contacts areas, later-phase areas disabled
behind one clean unavailable state, and guarded local-only fixture/reset tools.

Pipeline changes in this slice:

* **XLSX parsing** (openpyxl) unified with CSV behind one shared pipeline
  (`app/services/imports/parsing.py`); workbook filename, sheet name/index and
  per-sheet row numbers preserved; malformed/empty workbooks fail visibly.
* **Two-step import**: staged upload → sheet selection → operator-confirmed
  column mapping (stored on the batch with mapper/parser versions) → true
  dry-run preview (writes nothing) → idempotent confirm. The DAT-002 API route
  is unchanged.
* **Ambiguous outcome** (`import_row_outcome = ambiguous`, DAT-004-compatible):
  an uncertain identity match creates no contact and no membership, records
  why, and is reviewable in the workbench. Migration `a7c2f1d40e88` (enum value
  + `import_batches.ambiguous_rows` + `import_batches.column_mapping`).

Evidence: `tests/test_parsing.py`, `tests/test_mapping.py`,
`tests/test_staging.py`, `tests/test_preview_and_xlsx_import.py`,
`tests/test_devtools.py`, `tests/test_workbench_web.py`.

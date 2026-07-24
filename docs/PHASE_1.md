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
| DAT-005 Provenance & freshness | Per-contact provenance records (source, reference, exporter, export date, observation time) **plus** a field-level value ledger: every observation of each tracked operational field (title, company name, company size, industry, country, LinkedIn URL) is appended with its source, observation/ingestion times, and confidence. A deterministic, **versioned** freshness policy (`freshness-v1`) picks exactly one current winner per field — newer evidence wins, older evidence never overwrites newer, manual operator overrides outrank imports and stay explicit, missing/equal timestamps resolve deterministically — and the winner is reproducible from stored evidence. `explain_field` answers "why is this the value currently being used?". | `app/models/provenance.py`, `app/models/contact_field_value.py`, `app/services/provenance/`, `app/services/imports/importer.py`, `tests/test_field_provenance.py` |
| DAT-006 Suppression ledger | Independent ledger of suppressed emails/domains, consulted on every advancing route. An identity may carry several reasons at once (opt-out, hard bounce, customer, competitor, internal, legal/compliance, manual); each record keeps its creator and active/inactive state. Unsuppressing sets a record inactive and appends a lifecycle event — history is never destroyed. `evaluate_suppression` returns a truthful blocked reason (e.g. `email opt_out`), distinct from *invalid*. Enforced at import (all campaign memberships transitioned to `SUPPRESSED`) and at the verification advance path (blocked before any candidate or paid call). Downstream gates (scoring, research, drafting, scheduling, sending) call the same primitive as they land. | `app/models/suppression.py`, `app/services/suppressions.py`, `app/services/imports/importer.py`, `app/services/verification/service.py`, `tests/test_suppression_ledger.py` |
| CMP-001 Campaign creation and settings | A draft campaign persists the minimum launch-ready settings for the pilot: name, description, offer, structured `audience_rules`/`exclusions` (JSON objects, not free text), `min_score_threshold` (defaults to the launch absolute threshold of 85), tone, owner, source, sending reference, lifecycle status, and timestamps. Creation validates required fields and rejects malformed/oversized values; partial updates change only the fields explicitly sent, `null` explicitly clears a nullable field, and status changes are checked against an explicit draft→active→archived transition map. No sequence generation, sending, scheduling, or provider integration. | `app/models/campaign.py`, `app/services/campaigns.py`, `app/api/routes.py`, `migrations/versions/f4c533f48a92_*.py`, `tests/test_campaigns.py`, `tests/test_api.py` |
| CMP-002 Contact workflow states | Explicit per-campaign membership states with a validated transition map; illegal transitions raise; transitions are audited. Only import-stage states are wired. | `app/models/enums.py`, `app/services/contact_state.py` |
| CMP-003 Campaign-contact membership and outreach history | A contact can join multiple campaigns without losing or blocking on earlier activity, and cannot acquire duplicate active outreach in one campaign — proven under real concurrent inserts, not just app checks. Adds `ensure_membership` (idempotent, suppression-checked) and `record_outreach_event` (idempotent, suppression/eligibility-gated) on top of the pre-existing membership table, reusing `external_events` as the outreach-history record instead of a new table. See "CMP-003 campaign-contact membership and outreach history" below for the exact rule and rollback notes. | `app/models/external_event.py`, `app/services/campaign_contacts.py`, `app/services/identity.py`, `migrations/versions/4b659a2054e4_*.py`, `tests/test_campaign_contacts.py` |

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

## CMP-001 campaign contract (draft settings)

`Campaign` (`app/models/campaign.py`) fields, all on the same table since the
Phase 1 schema — CMP-001 extended it, it did not create a parallel entity:

| Field | Type | Nullable | Default | Validation |
| --- | --- | --- | --- | --- |
| `name` | `String(255)`, unique | no | — | required, trimmed, 1–255 chars |
| `description` | `Text` | yes | `None` | trimmed, blank → `None`, ≤4000 chars |
| `offer` | `Text` | yes | `None` | trimmed, blank → `None`, ≤4000 chars |
| `audience_rules` | `JSONB` | yes | `None` | must be a JSON object, ≤20,000 bytes serialized |
| `exclusions` | `JSONB` | yes | `None` | same as `audience_rules`; campaign-scoped targeting exclusion — distinct from and never a substitute for the DAT-006 suppression ledger |
| `min_score_threshold` | `Integer` | **no** | `85` | 0–100 (service-layer only; see limitations below) |
| `tone` | `String(100)` | yes | `None` | trimmed, blank → `None`, ≤100 chars |
| `owner` | `String(255)` | yes | `None` | trimmed, blank → `None`, ≤255 chars |
| `source` | `String(255)` | yes | `None` | trimmed, blank → `None`, ≤255 chars |
| `sending_reference` | `String(255)` | yes | `None` | opaque reference; never resolved against a sending provider |
| `status` | enum `campaign_status` | no | `DRAFT` | see lifecycle below |
| `created_at` / `updated_at` | timestamptz | no | `now()` | `updated_at` bumps on every column update |

Lifecycle: `CampaignStatus` = `DRAFT` / `ACTIVE` / `ARCHIVED` (unchanged
enum). `ALLOWED_CAMPAIGN_TRANSITIONS` (`app/models/enums.py`) permits
`DRAFT → {ACTIVE, ARCHIVED}` and `ACTIVE → ARCHIVED`; `ARCHIVED` is terminal;
the same status requested again is always a no-op, never an illegal
transition. `create_campaign`/`update_campaign_settings`
(`app/services/campaigns.py`) are the only write paths; a partial update
changes only the fields explicitly passed (a private `UNSET` sentinel
distinguishes "omitted" from an explicit `None`), and a rejected update never
partially applies. JSON API: `POST /campaigns`, `GET /api/campaigns/{id}`,
`PATCH /campaigns/{id}` (the GET route lives under `/api/` specifically to
avoid shadowing the pre-existing HTML campaign-detail page at the bare
`/campaigns/{id}` path).

**Known limitation:** `min_score_threshold`'s 0–100 range is enforced only in
the service layer, not by a database `CHECK` constraint — Alembic autogenerate
in this project does not reliably detect `CheckConstraint` changes, and no
other model uses one (e.g. `Score.total` has the same conceptual range with no
DB check either). A write that bypasses the service layer could store an
out-of-range value; this is a deliberate, documented trade-off, not an
oversight.

## CMP-003 campaign-contact membership and outreach history

**The "duplicate active outreach" rule, precisely:** a contact has at most one
`CampaignContact` membership row per campaign, ever — enforced by the
database via the pre-existing unique index
`uq_campaign_contacts_campaign_contact` on `campaign_contacts(campaign_id,
contact_id)` (present since the Phase 1 schema). Because
`ALLOWED_CONTACT_TRANSITIONS` is a strict DAG with two terminal states
(`SUPPRESSED`, `EXCLUDED`) and no transition ever re-enters a non-terminal
state, that one row's current state is always the single, unambiguous answer
to "is this contact under active outreach in this campaign, and if not, why."
This is a schema-level impossibility, not an application-level check —
verified under two independent, really-committing database connections in
`tests/test_campaign_contacts.py::test_duplicate_membership_insert_fails_at_db_level_under_concurrency`.

Individual outreach **events** recorded against that one membership (a send
attempt, a bounce, a reply, a stop) are deduplicated separately, by the
pre-existing `uq_external_events_provider_event_id` unique index on
`external_events(provider, external_event_id)` — CMP-003 reuses
`ExternalEvent` (DAT-001) as the outreach-history record, adding three
nullable attribution columns (`contact_id` CASCADE, `campaign_id` CASCADE,
`campaign_contact_id` SET NULL) rather than inventing a parallel table. The
same concurrency proof exists for events
(`test_duplicate_outreach_event_insert_fails_at_db_level_under_concurrency`).
Sending identity/channel (e.g. a specific mailbox) is deliberately **not**
part of either key — no such concept exists anywhere in the repository yet.

**Historical-activity preservation:** joining a second campaign
(`ensure_membership`, `app/services/campaign_contacts.py`) never touches an
earlier campaign's membership row or history — different campaigns are
different rows by construction, and old/completed/suppressed history is never
a reason to refuse a new membership. If a membership row is ever removed by an
unrelated path, its history is not deleted with it: DAT-004's duplicate-contact
merge, when it coalesces two memberships that collide in the same campaign
(`app/services/identity.py::_apply_merge`), now re-parents any outreach events
onto the surviving membership *before* deleting the redundant row; the FK's
`ON DELETE SET NULL` is the defense-in-depth fallback for any other path
(proven directly in
`test_external_event_survives_membership_deletion_via_set_null`).
`contact_id`/`campaign_id` stay populated even if `campaign_contact_id` is
ever null, so history stays queryable by the durable keys.

**Suppression/verification interaction:** `ensure_membership` evaluates the
suppression ledger fresh (`evaluate_suppression`, DAT-006) on every call — a
brand-new membership for a currently-suppressed identity starts `SUPPRESSED`,
never `IMPORTED`. `record_outreach_event(..., is_outbound=True)` re-checks the
ledger fresh at record time (not merely the membership's stored state, which
can lag a suppression added after the membership was created) and refuses a
terminal membership; non-outbound events (e.g. recording the bounce that
*caused* a suppression) are always recorded so history stays complete. Neither
function replaces or weakens `evaluate_suppression`; both call the exact same
primitive the verification path already uses.

**Operator-visible failure behaviour:** `ensure_membership` and
`record_outreach_event` raise `CampaignContactNotFound` (unknown
campaign/contact) or `OutreachError` (blocked outbound attempt) with a
message that names the rule that blocked the request — never a database
error, stack trace, or internal identifier. Idempotent calls (a repeat
`ensure_membership`, a repeat `record_outreach_event` with the same
`external_event_id`) return the existing row instead of raising.

## Migration and rollback notes

Two CMP migrations, both additive and reversible, applied on top of the
Phase 1 schema in this order:

* `migrations/versions/f4c533f48a92_*.py` (CMP-001) — adds 8 nullable-except-one
  columns to `campaigns` (`min_score_threshold` is `NOT NULL DEFAULT 85`, so
  existing rows backfill safely). Downgrade drops exactly those 8 columns.
* `migrations/versions/4b659a2054e4_*.py` (CMP-003) — adds 3 nullable
  attribution columns to `external_events` plus their indexes and foreign
  keys. Downgrade drops exactly those 3 columns, their indexes, and their
  foreign keys.

Neither migration alters, drops, or backfills any pre-existing table's data,
and neither touches a historical migration file. Both were verified locally
with `alembic upgrade head`, `alembic check` (no drift before or after), and a
full `downgrade -1` → `upgrade head` round trip with the resulting schema
manually inspected via `psql \d`.

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
included; those feature switches remain off. CMP-001's operator workbench UI
for the new settings fields (offer/audience rules/tone/etc.) is not built —
those fields are reachable only through the JSON API today, deliberately, to
avoid post-launch campaign-builder scope. CMP-003 does not build any sending
orchestration, sequence generation, or provider integration — `sending_reference`
and outreach events are storage only.

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

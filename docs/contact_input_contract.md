# Authorized contact-input contract (FND-008)

Status: Agreed for Phase 0 (import implementation is Phase 1 / DAT-002).

This document defines the shape and provenance of an **authorized** contact batch
that the system will accept for the first campaign. It exists so that Phase 1 can
build CSV staging import against a fixed contract, and so acquisition stays
manual and compliant.

## Source and authorization

- Contacts arrive through one of two **authorized acquisition paths**, both
  feeding the same staged-import pipeline:
  1. An **authorized spreadsheet** — **CSV or XLSX** (both parse through one
     shared pipeline since the workbench slice) — exported by a human operator
     from an authorized source (e.g. a manual Sales Navigator export the
     operator is licensed to use, or another explicitly permitted list).
  2. An **operator-driven capture batch** from the Sales Navigator capture
     extension (`extensions/salesnav-capture/`): the operator browses and
     authenticates themselves, the extension reads only visible pages, the
     operator reviews the batch, and it is handed over as a JSON/CSV export or
     via a narrow intake endpoint. No unattended pagination, credential
     storage, or undocumented APIs.
- Legacy `.xls`, Google Sheets direct import, and other spreadsheet formats are
  out of scope until explicitly approved.
- The system does **not** perform unattended scraping, anti-bot evasion, CAPTCHA
  solving, or platform-limit bypasses (GOAL.md non-goals; CLAUDE.md). The
  acquisition method is deliberately kept outside and replaceable behind the
  import contract.
- Each batch is treated as one import with its own provenance (who exported it,
  from where, and when).

## Accepted columns

Encoding UTF-8, comma-separated, first row is a header. Column names are matched
case-insensitively and trimmed. Unknown columns are preserved as raw context but
ignored by validation.

### Required

| Column | Meaning | Validation (Phase 1) |
| --- | --- | --- |
| `first_name` | Contact given name | Non-empty after trim |
| `last_name` | Contact family name | Non-empty after trim |
| `company_name` | Employer / organization | Non-empty after trim |
| `company_domain` | Primary web domain of the company | Parseable hostname; used for email generation |

### Recommended (used by scoring/verification when present)

| Column | Meaning |
| --- | --- |
| `title` | Job title / role |
| `email` | Known email address, if the source already provides one |
| `linkedin_url` | Contact profile URL (provenance only; not scraped) |
| `country` | Contact or company country |
| `industry` | Company industry |
| `company_size` | Employee count or band |

### Provenance (attached per row at import; operator-supplied or defaulted)

| Field | Meaning |
| --- | --- |
| `source_name` | Where the batch came from (e.g. "Sales Navigator export – 2026-07") |
| `source_reference` | Optional URL or saved-search reference the operator used |
| `exported_by` | Operator who produced the export |
| `exported_at` | When the export was taken (date) |

If provenance fields are not columns in the CSV, they are captured once for the
whole batch at upload time. Every stored contact value retains its source and
observation time (AGENTS.md; DAT-005).

## Row-level validation behaviour (Phase 1 contract)

- Each row is validated independently; a batch can partially succeed.
- Errors are **actionable and row-level** (row number, column, reason), e.g.
  "row 42: company_domain 'acme' is not a valid hostname".
- Missing required fields, malformed domains, and malformed emails are reported,
  not silently dropped or guessed.
- No verification, scoring, research, or sending happens at import. Import only
  stages raw rows plus a normalized view for later phases.

## Explicitly out of scope for the contract

- Automated enrichment from third-party sources at import time.
- Accepting arbitrary/unknown file shapes. One representative CSV shape is
  supported first (DAT-007); additional shapes are added only when proven
  necessary (DAT-008, P1).

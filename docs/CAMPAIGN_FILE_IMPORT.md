# Campaign contact file import (IMP-001)

An operator opens a Campaign, uploads an Apollo-style contact export, sees
exactly what would happen, and confirms. The people in the file join that
Campaign's existing Agent pipeline with the address the file supplied.

The one thing to understand before anything else:

> **The imported address is carried, not discovered, and never verified.**
> No candidate address is generated. No domain pattern is applied. No
> verification provider is called — not MillionVerifier, not ZeroBounce, not any
> other. Whatever the export claims about an address stays labelled as the
> vendor's claim. Nothing in this path ever means "this mailbox exists".

---

## 1. Route map

All under the Customer application, bound to one Campaign throughout.

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/app/campaigns/{campaign_id}/imports` | Upload form, limits, and previous imports |
| `POST` | `/app/campaigns/{campaign_id}/imports` | Stages the upload. **Writes nothing to the database** |
| `GET` | `/app/campaigns/{campaign_id}/imports/staged/{staged_id}` | Preview. `?sheet=N` chooses a worksheet |
| `POST` | `/app/campaigns/{campaign_id}/imports/staged/{staged_id}/confirm` | **The first durable mutation** |
| `POST` | `/app/campaigns/{campaign_id}/imports/staged/{staged_id}/discard` | Throws the upload away |
| `GET` | `/app/campaigns/{campaign_id}/imports/{batch_id}` | The result, row by row |

The Campaign is in the URL of every step and is re-checked at each one. A staged
upload can only be confirmed into the Campaign it was uploaded for, and one
Campaign's batch cannot be opened from another (404, not a redirect — the page
would otherwise disclose another Campaign's contacts and their addresses).

## 2. Feature flag

| | |
| --- | --- |
| Flag | `FEATURES__CSV_IMPORT` |
| Default | **off** (`false`), per FND-007 |
| Production requirement | Must be **on** for the import to run at all |
| Customer UI when off | The page renders and explains the switch; the upload control is disabled and every service entry point (`inspect`, `preview`, `confirm`) raises `FeatureDisabledError` |

The existing flag is reused rather than replaced. It already means "authorized
spreadsheet import of contacts into a campaign", which is exactly this. A second
flag would have created two switches an operator has to reason about together.

Reaching the pipeline additionally needs the ordinary controls, none of which
this feature changes: the Campaign's `execution_enabled`, and the per-Agent
global/Campaign controls. With execution off, contacts are imported and enrolled
and simply wait — which is the safe default.

## 3. Supported schema

**Apollo Contact Export — schema version 1** (`apollo_contact_export_v1`).

Recognized **by header name, in any order**, with extra columns tolerated and
nothing to map by hand. Required headers:

`First Name`, `Last Name`, `Company Name`, `Email`

Everything else in the Apollo export is read when present: seniority,
departments, the person and company LinkedIn URLs, location, phones, the primary
/ secondary / tertiary address blocks with their source, status, verification
source and last-verified timestamp, the company block, and the Apollo Contact /
Account / Record ids. Unrecognized columns are carried verbatim in a bounded
per-row payload and are **never** guessed into a canonical field.

Formats: `.csv` (UTF-8, with or without BOM) and `.xlsx`. Multi-sheet workbooks
are reported and the operator chooses. `.xls` and Google Sheets direct import
remain out of scope.

## 4. Identity resolution

### Contact

Three exact signals, and they must **agree**:

1. Apollo Contact Id
2. Normalized primary email
3. Normalized LinkedIn profile URL

One signal identifies the person. Two signals naming two different permanent
Contacts is a contradiction, and the row is held for review rather than merged.
Nothing fuzzy participates — a matching name, employer or title never
contributes, because these files are full of people who share all three.

A matched Contact that already carries a **different** address is also held for
review: the imported address is never allowed to overwrite one something else
established.

### Company

`Company Name` is **not** evidence of company identity. The evidence hierarchy:

1. Apollo Account Id already recorded against a permanent Company
2. Canonical Website domain
3. Normalized Company LinkedIn URL
4. Agreement between the email domain and the Website domain
5. Otherwise: review-required

A public mailbox provider (gmail.com, outlook.com, …) can never establish a
company; such a row needs another reliable signal and carries a warning either
way.

The worked case from the specification — `AGILENT TECHNOLOGIES` next to
`twnoyes@llbean.com`, `https://llbean.com` and L.L.Bean's LinkedIn page —
resolves to L.L.Bean, keeps `AGILENT TECHNOLOGIES` as source evidence, and shows
a `supplied_company_name_conflict` warning. If a real Agilent is already on file
and the signals genuinely disagree, the row is held instead.

## 5. The imported-email truth model

`imported_contact_emails` holds one record per address slot per source row.

| Kept | Why |
| --- | --- |
| Normalized address, raw address | Normalization folds case; the raw value is what lets anyone check that |
| Import batch, source row number, sheet | Traceability to the exact cell |
| Source file checksum (SHA-256), source schema, row fingerprint | Traceability to the exact bytes, and idempotency |
| `provider_source`, `provider_status_raw` + `_normalized`, `provider_verification_source`, `provider_catch_all_raw` + `_normalized`, `provider_last_verified_raw` + `_at` | **The vendor's claims, labelled as the vendor's** |
| `email_stage_outcome`, `verification_stage_outcome` | **What VMR itself did** |

`Valid` / `valid` / `VALID` normalize to `valid`; `Catch-all` / `catch-all` /
`Catch All` normalize to `catch_all`. The raw wording is preserved beside it.

Secondary and tertiary addresses are retained with all their metadata and carry
**no** stage outcome. They are never promoted, even when the primary is
malformed — choosing between a person's addresses is an operator decision.

### Why not an existing table

`EmailCandidate` means "an address this system decided to try"; nothing decided
an imported one. `ExactEmailVerification` means "a provider was asked about this
mailbox and answered"; an import asks nobody, and writing into it would
manufacture verification evidence from a spreadsheet cell. No import ever writes
a row into either.

## 6. Email and Verification stages

For an imported row with an accepted primary address:

* **Email** completes as `imported_email_accepted`. The job result records
  `candidates_generated: 0`, `provider_call_created: false`,
  `address_derivation: operator_supplied_import_no_discovery` and
  `verification_id: null`.
* **Verification** completes as `verification_bypassed_imported_email`, with
  `decision: bypassed`, `verification_id: null`, `provider_called: false` and
  `source: campaign_file_import`. It is visible in stage history and in the
  stage's `output_reference`.

The imported branch sits before the candidate policy in
`app.services.email.agent.execute_step` and is entered only when the Campaign has
an accepted imported record for this Contact **and** it still matches the
Contact's current address. Suppression is re-checked inside the branch.

Nothing is weakened for anyone else. A Contact with no imported record for this
Campaign — every Sales Navigator, extension and manual acquisition — falls
straight through to the unchanged discovery path.

The imported branch is reached before the company-domain gates deliberately.
Those gates exist to stop the system *generating and verifying* addresses at an
unconfirmed domain — the step that spends money and touches a mail server. This
path does neither.

## 7. Pipeline position

Registry order is unchanged:

```
Capture → Identity → Company → Research → Email → Verification → Insights → Personalization → Sending
```

Research still runs (or is skipped by its own control) **before** Email. The
import skips no prerequisite: a Contact whose Research stage is blocked never
reaches Email. Sending has no adapter and is not skippable; it remains
unavailable.

## 8. Idempotency

| Repeat | Result |
| --- | --- |
| Same file, same worksheet, same Campaign | The existing batch is returned; nothing new is written |
| Same file, another Campaign | A second batch and a second membership; one Contact, one Company |
| Same row content, later batch, same Campaign | `skipped_duplicate` / `already_imported`; no duplicate evidence |
| Modified file with previously imported rows | Only genuinely new or changed rows are processed |
| Same person twice in one file | The first occurrence wins; the second is reported |

Keys: the batch content hash (bytes + worksheet + schema), the row fingerprint
(the canonical values the import persists as meaning), the enrolment idempotency
key, and `import_source_identifiers` for the vendor's own keys.

## 9. Partial success

Every row is committed inside its **own SAVEPOINT**. A database failure on one
row discards that row's Company, Contact, membership and evidence together — a
half-created identity is worse than none — and every other row stands. The
failure is recorded with a stable code and a message that names the exception
type at most: no stack trace, no driver text, no repetition of the row's address.

Batch counts: imported, matched existing, already in campaign, skipped
duplicate, review required, suppressed, failed.

## 10. File security

Filenames are sanitized (both separator conventions, traversal segments,
control characters) and stored beside the original. Uploads are size-limited
(`MAX_UPLOAD_BYTES`) before staging, and row- and column-capped
(`MAX_DATA_ROWS`, `MAX_COLUMNS`) before anything is stored. Workbooks are read
with `openpyxl` in read-only mode with cached values only, so **no formula is
ever evaluated**; a formula-shaped cell is stored as text, flagged with a
`formula_like_cells` warning, and neutralized with a leading apostrophe wherever
it is rendered or exported. Jinja autoescaping covers HTML and script. No office
application is ever invoked.

## 11. Authorization

The application is single-operator today: there are no user accounts, so an
import is scoped to a **Campaign**, not to a person. That boundary is real and
enforced — cross-Campaign staged confirmation and cross-Campaign batch reads are
both refused. The future ownership limitation is stated on the page rather than
implied by an absent login form.

## 12. Admin Workbench compatibility

Everything the later Admin Workbench needs is durable, not presentation-only:
the batch (schema, sheet, checksum, headers, counts, confirmation time), the
immutable raw row, the per-row outcome with its Contact/Company/membership/
imported-email references and match bases, the warnings, the error code, the
imported address with its provider claims, and both VMR stage outcomes.

## 13. Migration

`c1f7a3e29b04_campaign_contact_file_import` — creates
`imported_contact_emails` and `import_source_identifiers`, adds nullable /
defaulted columns to `import_batches` and `import_row_validations`, and creates
three new enum types. Reversible: the downgrade drops the columns, the tables and
the enum types. No value is added to an existing enum type, because
`ALTER TYPE … ADD VALUE` cannot be reversed.

## 14. Known limitations

* One vendor schema. A non-Apollo file that satisfies the same four required
  headers is accepted and labelled "Apollo-compatible" rather than refused.
* No operator-driven column mapping on this path. The generic mapped importer at
  `/imports` still exists for files this reader does not recognize.
* Review-required rows are recorded and visible on the batch page; resolving
  them is the existing DAT-004 review path and is not extended here.
* Delimiter detection is not attempted: a semicolon-delimited CSV will fail
  header recognition with the actionable missing-header message rather than
  being silently misread.
* No sending, no Gmail sync, no Sheets sync, no follow-up sequences — all
  deliberately out of scope.

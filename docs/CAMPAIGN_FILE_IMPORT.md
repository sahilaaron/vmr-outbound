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

### What "held" is, and what it is not

Holding is a **data-truth** mechanism. A row whose identity is ambiguous is
recorded with the exact reason it was held, and nothing is merged on a guess —
that is the whole point, and it is not negotiable.

It is not a queue anybody has to work off. A held row is an outcome the import
reached, kept with its reason so it can be resolved later by whoever wants to;
resolving it is optional and can be deferred indefinitely. The rest of the file
imports and enrols normally, and the contacts that did resolve carry on through
the pipeline without waiting for it.

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

These describe what the file did. They are a record of one import, not a
running total of arrears, and none of them is a number anybody is expected to
drive to zero.

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

## 12. Admin Workbench integration

Everything the Admin Workbench needs was already durable rather than
presentation-only: the batch (schema, sheet, checksum, headers, counts,
confirmation time), the immutable raw row, the per-row outcome with its
Contact/Company/membership/imported-email references and match bases, the
warnings, the error code, the imported address with its provider claims, and
both VMR stage outcomes. The Workbench reads that state; it stores nothing of
its own and re-implements no part of the importer.

`app/services/admin_workbench/import_lineage.py` is the read model. It calls the
same public helpers the customer screens call (`campaign_import.get_batch`,
`batch_rows`, `campaign_batches`, `retained_alternates`,
`imported_email_summary`), so the two surfaces cannot disagree about what a row
did. It never writes.

| Surface | What it adds |
|---|---|
| Campaign detail | A **File imports** panel: every batch, its schema, and its imported / already-here / held / refused counts. |
| Contact diagnosis | An **Origin — campaign-bound file import** card: batch, row, fingerprint, whether the Contact and the membership were created or reused, which signal matched, the resolved Company and its basis, the supplied Company name beside it, source identifiers, and any row warnings. |
| Contact diagnosis → Email | Address origin, the accepted imported address, and *candidate generation: bypassed*, with the vendor's claims behind a disclosure that labels them as the vendor's. |
| Contact diagnosis → Verification | *Bypassed — imported address*, provider called **no**, evidence **none**. |
| `/admin/imports/{batch_id}` | The batch and every row it produced, with per-row Contact, Company, supplied name, imported address and bypass state. Read-only. |
| Failures | A **File-import rows needing attention** table for refused and held rows. They have no Campaign Contact, no stage and no Agent Job, so the Phase 2 inbox cannot see them at all. |
| Contact / Company detail | A **Source identifiers** table, labelled as another system's keys rather than as identity. |

### Why the Verification stage needed anything at all

The Workbench's Verification projection validates its decision against
`VerificationDecision` and reports anything outside that vocabulary as
*undecided* rather than guessing. The import path commits `bypassed`, which is
deliberately **not** a verification decision — that vocabulary governs real
verification, where `accept` means a mailbox answered, and admitting `bypassed`
into it would let an import satisfy checks that exist to mean "a provider said
so". So the projection stays as it is and the lineage supplies the missing half.
Without it the page said "no committed decision" about a stage that had
committed one: safe, but silent.

For the same reason the Email and Verification stages no longer print the
registry's worker list for an imported Contact. That list is what *can* run a
stage, not what did; naming MillionVerifier and DeBounce beside an address no
provider ever saw is precisely the misreading this path exists to prevent. Both
stages say **none ran** instead, with the reason.

Tests: `tests/test_campaign_import_admin_workbench.py`.

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
* Review-required rows are recorded with their reason and visible on the batch
  page; resolving them is optional, deferrable, and goes through the existing
  DAT-004 path, which is not extended here.
* Delimiter detection is not attempted: a semicolon-delimited CSV will fail
  header recognition with the actionable missing-header message rather than
  being silently misread.
* No sending, no Gmail sync, no Sheets sync, no follow-up sequences — all
  deliberately out of scope.

---

## Appendix — corrections after an independent adversarial review

An independent hostile review of this branch reproduced twenty defects and
confirmed the truth model itself: no `EmailCandidate` is fabricated, no
`ExactEmailVerification` is written, no provider is constructed, and
`VerificationDecision` keeps its five provider-only members. Those properties
are now asserted in `tests/test_campaign_import_review_fixes.py` so they cannot
be lost quietly.

Three findings were blockers.

**A repeated header name imported the wrong column.** `detect_schema` recorded
the first column that claims a field, reported the second as a duplicate, and
the preview told the operator it was not applied — while both parsers built the
row as a dict keyed by header text, so the later column overwrote the earlier
one before the reading began. A column name is not a unique address into a
spreadsheet row. Rows now carry `cells` by position beside the durable
name-keyed payload, `SchemaDetection` records the winning column's position, and
every lookup goes through the position. The durable `raw_data` payload keeps the
first occurrence, because JSONB cannot hold two entries under one key and losing
one silently was the original failure.

**An email domain was never checked as a hostname.** `<jane@gmail.com>` — an
ordinary mail-client paste — produced the domain `gmail.com>`, which is not in
`PUBLIC_EMAIL_DOMAINS`, so a personal mailbox could found a Company on a string
that cannot resolve. `normalize_email` now unwraps the angle-addr form and sheds
trailing list punctuation, and `normalize_email_domain` canonicalizes to IDNA
and returns `None` for anything that is not a valid hostname. `is_valid_email`
consults it, so an accepted address always has a usable domain, and `bücher.de`
and `xn--bcher-kva.de` stop being two employers.

**The migration downgraded over data with no guard.** It dropped both new tables
and seventeen columns from the two pre-existing import tables, silently, against
a convention eight migrations in this repository already follow. It now refuses
while any of four categories is populated, reads the defaults the upgrade itself
wrote (`warnings = '[]'`, `already_in_campaign_rows = 0`) as absence so an empty
schema still reverses, and names categories and counts in its refusal without
quoting an address, a filename or any SQL.

The important findings shared one shape, and it is worth naming because it is
the failure this feature is most likely to repeat: **a new, careful surface was
written, and existing surfaces were left describing an address whose meaning had
just changed.** `BYPASS_STATEMENT` and `NO_DISCOVERY_STATEMENT` exist so that no
template can weaken them, and exactly one template used them. So the Verification
funnel counted a bypass under the word "passed"; the customer Contact page
rendered the vendor's claim as "pending", promising a check that will never run;
and the Admin contact card filed it under "Email addresses & verification",
sourced "canonical". Each is now answered at the read model rather than in a
template, so every consumer inherits the correction:
`StageFunnelStep.provider_passed` and `.bypassed_through` are separate numbers,
`StatusView.is_imported` is derived from the import's own evidence, and
`EmailStateRow.imported` marks the address on the Admin card.

Three more corrections to what an operator is told. The failures inbox filtered
on `error_code` rather than on outcome, so rows that had resolved to a Contact
appeared under a heading saying none was created — and its 200-row cap was
applied before the distinction, so a large re-import of already-present rows
could evict the genuinely refused ones from the page. A row with no validation
record was reported as `rejected`, asserting a refusal that never happened. And
the batch counters did not partition: `duplicate_rows` is the sum of three
dispositions and `already_in_campaign_rows` is one of them, so the two rendered
side by side could exceed the size of the file. `campaign_import.batch_counts`
now produces buckets that account for every row once, with an explicit residual.

**The fingerprint contract is now structural.** It claimed to cover everything
the import persists as meaning and covered a hand-written list of seventeen
attributes, with every provider claim outside it — so a re-export correcting
`Email Status` from `invalid` to `valid` hashed identical to the original and
imported nothing. It is now derived from `bounded_source_payload` minus the keys
that describe *where* a row sat, so the two cannot drift. A restated address for
somebody who already has an accepted one in the Campaign is stored as retained
evidence with `rejection_code = retained_person_already_has_an_accepted_address`:
the correction is on record, and swapping the address in use stays an operator's
decision rather than a consequence of uploading a newer file.

Two provenance corrections belong to the same principle as the truth model
itself. A batch was labelled "Apollo contact export" whenever four required
headers were present, which any hand-made CSV can satisfy — vendor provenance
manufactured from a column list. `is_apollo_export` already distinguished the
two and was consulted nowhere; a merely compatible file is now recorded as
`Apollo-compatible contact file`. And the duplicate-file note named the other
Campaign holding the same bytes; that the file was seen before is the useful
part, whose it was is not.

Two things are deliberately **not** fixed here. There is no CSRF protection
anywhere in this repository, so the new routes are consistent with their
neighbours and this branch does not introduce a repository-wide token system;
`POST /app/campaigns/{id}/imports` is nonetheless the first cross-site-forgeable
endpoint that accepts a file and writes Contacts campaign-wide, and that should
be centralized rather than patched here. And the upload's size check now
consults `Content-Length` before buffering, which is an improvement rather than
a guarantee — that header is client-supplied and absent from a chunked request,
so the reverse proxy's body limit remains the authoritative protection.

---

## Appendix B — operational warning: `c1f7a3e29b04` changed under a fixed revision id

**Read this before publishing the branch or migrating any database that is not
disposable.**

`c1f7a3e29b04` has been corrected twice in place while PR #242 is unmerged. The
revision id never changed, so its bytes did:

| Version | SHA-256 of the migration file |
| --- | --- |
| Original (`6d04e088`) | `36e4ba647c5702a8e73ec74b7ed15fb3c0eca3e12a3a467353bd442566d65a4e` |
| First repair (`16b16f98`) | `3644560fde83806357670b14c4ae24fb2e9cb13a463d1f00f1d4c341a0d9bb08` |
| Second repair (this head) | `a42c1e3ff6f641068bb227c26e26e830aad64e134edfe5398a6f351f9a6014b0` |

**Alembic does not repair this and does not warn about it.** A database that ran
an earlier byte-version has `alembic_version = 'c1f7a3e29b04'`, so the revision
is already applied and `alembic upgrade head` returns success and does nothing.
The schema silently lacks whatever the later versions added. An independent
review demonstrated exactly this: after upgrading with the original bytes,
`uq_imported_contact_emails_accepted_campaign_contact` was absent, the repaired
`upgrade head` exited 0 without creating it, and `alembic check` then exited 255
reporting the missing index.

Correcting the migration in place is nevertheless the right choice **while the
PR is unmerged**, and a follow-on revision was deliberately not added: it would
exist only to repair disposable databases that ran an unpublished draft, and it
would sit in the permanent history of every fresh installation forever. A fresh
database is unaffected — the table is created by this migration and is empty
when the constraints and index are created on it.

### What has to happen before publication

**1. Recreate every disposable database.** Development, UAT, CI, and any local
sandbox that ever ran `c1f7a3e29b04`: drop the database and migrate again from
the repaired chain. Do **not** stamp the revision and do **not** hand-create the
index or the check constraints — a stamped database is a database whose schema
nobody can reason about afterwards.

**2. Inventory whether any durable environment ever ran it.** This is a factual
question about the operator's estate that cannot be answered from a repository
and has not been answered here. The check is:

```sql
SELECT version_num FROM alembic_version;
```

Anything returning `c1f7a3e29b04` that is not disposable is in scope.

**3. If any durable environment ran the old bytes, STOP.** Do not downgrade it,
do not stamp it, and do not assume Alembic will re-run the revision — it will
not. Such an environment needs its own follow-on repair revision with a **new**
revision id and a preflight, because the corrected constraints cannot simply be
added to data that the old schema permitted. Concretely, the old schema allowed
two accepted primary addresses for one Campaign and Contact; creating
`uq_imported_contact_emails_accepted_campaign_contact` on a database holding
such a pair fails with a PostgreSQL `UniqueViolation`, and the same applies to
the accepted-orphan and accepted-alternate rows the new check constraints
forbid. The preflight has to find those rows and an operator has to decide how
each is reconciled. **That decision is not one to make silently or by default.**

**4. SEQ-001 reconciliation builds on the final chain.** Reconcile against a
database created from the reconciled migration chain, not from one that ran any
earlier byte-version of `c1f7a3e29b04`, and keep exactly one Alembic head.

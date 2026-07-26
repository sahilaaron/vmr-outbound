# LinkedIn capture — acceptance

Layer 3 (DAT-013) is the **current** contact-first acceptance. Layers 1 and 2
below are the DAT-012 campaign-era evidence, retained because the extraction
adapters, safety behaviour, and company-evidence path they cover are unchanged.
The DAT-012 profile *intake* path they exercise is now legacy.

Two layers of acceptance evidence for the DAT-012 epic:

1. **Sanitized backend acceptance (completed, reproducible)** — run against a
   live local backend with committed sanitized fixtures. No LinkedIn contact,
   no real profile data, no credentials. Reproduce with the commands below.
2. **Authenticated operator pass (manual, Sahil)** — the in-browser checks only
   a logged-in operator can perform. The extension is operator-controlled; this
   runbook never authorizes unattended LinkedIn automation.

Sensitive-data policy: fixtures use fictitious people/companies; the one live
operator pass uses the operator's own judgment about which profile to open, and
**no credentials, cookies, auth headers, browser storage, private messages,
contact-info panel data, or unnecessary raw personal information may be pasted
into evidence**. Redact names/URLs from anything committed.

## Layer 1 — sanitized backend acceptance (evidence)

Environment: local Postgres (fresh `vmr_accept` DB, `alembic upgrade head`),
app started with `FEATURES__SALESNAV_INTAKE=true`,
`FEATURES__LINKEDIN_PROFILE_INTAKE=true`, `FEATURES__LINKEDIN_PROFILE_REFRESH=true`,
`FEATURES__LINKEDIN_COMPANY_INTAKE=true`, `FEATURES__WORKBENCH=true`.
Seed: one contact ("Morgan Vale", stale title `Operations Manager`,
`linkedin_url` stored with mixed case + query string) and one same-name decoy
contact with no LinkedIn URL. Payloads: the committed
`extensions/salesnav-capture/docs/fixtures/profile.payload.example.json` /
`company.payload.example.json` with fresh `client_capture_id`s.

| # | Scenario | Result |
| --- | --- | --- |
| 1 | Existing SalesNav listing capture still works | `POST /api/intake/sales-navigator/stage` staged 2 records, returned `/imports/<batch>` workbench link; all 54 pre-existing extension tests and the DAT-009 backend suite unchanged and passing |
| 2 | Manually opened profile can be captured | `POST /api/intake/linkedin-profile/stage` → 201, snapshot stored verbatim with nested experience observations |
| 3 | Exact URL match refreshes the correct contact | Outcome `exact_match_refreshed`; seeded contact's title `Operations Manager` → `Director of Operations` under freshness-v1; same-name decoy untouched |
| 4 | Unchanged recapture is idempotent | Identical payload replay → HTTP 200, `already_received: true`, same `snapshot_id`, no second snapshot |
| 5 | Older evidence cannot replace newer | Back-dated capture (2019) with a different title → `exact_match_unchanged`; title kept its newer value |
| 6 | Suppression remains authoritative | With an active `email opt_out`: outcome `suppressed`; evidence linked; no field refreshed; suppression untouched |
| 7 | Weak matches never merge | New-URL capture matching two contacts by name/name+company → `unmatched_staged`, review candidates with `auto_merge: false`, zero contacts created/changed |
| 8 | QA policy produces versioned recommendation | `profile-employment-qa/1.0.0` evaluation stored with outcome, reason codes, signals+thresholds, evidence refs, explanation, recommended action |
| 9 | Operator-opened company page can be captured | `POST /api/intake/linkedin-company/stage` → 201 with deterministic HQ parse and `/company-profiles/<id>` record page (renders 200) |
| 10 | Recoverable failure preserves the reviewed draft | Extension keeps the reviewed draft + `client_capture_id` on every recoverable send failure and offers Retry (idempotent); proven by `test/profile-handoff.test.js` + service-worker send paths; in-browser confirmation is step L6 below |

Automated backstops at this branch head: backend `pytest` 463 passed (incl.
`test_linkedin_profile_intake.py`, `test_profile_refresh.py`, `test_qa_policy.py`,
`test_linkedin_company_intake.py`); extension `node --test` 109 passed; `alembic`
upgrade/check/downgrade round trips clean on a fresh database.

## Layer 2 — authenticated operator runbook (manual, ~15 min)

Prereqs: local backend running with the four feature switches above; extension
loaded unpacked; you are logged in to LinkedIn in the same Chrome profile.
Every step is an explicit operator action.

- **L1 (scenario 1).** Open a Sales Navigator people-search results page →
  panel shows *Sales Navigator Listings* → Capture visible records → review →
  Send → staged batch opens in the workbench Imports list.
- **L2 (scenario 2).** Open the MAIN profile page (`linkedin.com/in/…`) of one
  contact that already exists in your workbench → panel shows *LinkedIn Person
  Profile* → "Read this profile page" → verify name/headline/location/current
  role/experience count/connections/open-to-work and warnings match what you
  see on the page. Scroll the page and recapture if Experience shows missing.
- **L3 (scenarios 3+8).** Send → response shows `exact_match_refreshed` (or
  `exact_match_unchanged`) → "Open snapshot record" → check the refresh summary
  and, on the contact, the refreshed field(s) and the QA evaluation outcome.
- **L4 (scenario 4).** Press Send again without recapturing → "already
  received — idempotent", same snapshot id.
- **L5 (scenario 7).** Open a profile that is NOT in your data → Send →
  `unmatched_staged`; confirm any same-name candidates appear as review-only
  and no contact was created.
- **L6 (scenario 10).** Stop the backend, press Send → clear failure, draft
  and review stay intact → restart the backend → Retry → succeeds with the
  SAME capture id.
- **L7 (scenario 9).** Open the company's page yourself (the extension will
  not navigate there) → *LinkedIn Company Profile* mode → capture on the About
  page → verify firmographics → Send → open the company snapshot record.
- **L8 (challenge surface).** If LinkedIn ever shows a login/checkpoint during
  the pass, confirm the panel switches to *Challenge / Login Required* and no
  capture is possible until you resolve it yourself.

Record PASS/FAIL per step (redacting profile names/URLs) and file defects as
GitHub issues against epic #139 before closing #147.

---

# Layer 3 — contact-first capture acceptance (DAT-013)

The refactor moved acquisition from campaign-first ingestion to permanent-contact
acquisition, so the DAT-012 acceptance criteria no longer describe the shipped
path. This layer replaces them for the person surfaces.

Same sensitive-data policy as above: fixtures use fictitious people and
companies, and **no credentials, cookies, auth headers, browser storage, private
messages, contact-info panel data, or unnecessary raw personal information may be
pasted into evidence**.

## Layer 3A — sanitized backend acceptance (completed, reproducible)

Environment: local Postgres (fresh `vmr_accept` database, `alembic upgrade
head`), app started with `FEATURES__CONTACT_CAPTURE_INTAKE=true`,
`FEATURES__WORKBENCH=true`, `FEATURES__SUPPRESSIONS=true`, `APP_ENV=local`.
Seed: one contact ("Morgan Vale", stale title `Operations Manager`,
`linkedin_url` stored with mixed case, a trailing slash, and a tracking query
string) plus a same-name decoy with no LinkedIn URL. Payloads: the committed
`extensions/salesnav-capture/docs/fixtures/contact-capture.*.example.json`
with fresh client ids.

Reproduce:

```bash
python scripts/contact_capture_acceptance.py --base-url http://127.0.0.1:8000
```

The script refuses any non-loopback base URL and asserts every outcome, so it
fails loudly rather than reporting a pass it did not earn.

| # | Scenario | Result |
| --- | --- | --- |
| 1 | Manually opened profile saved without a campaign | HTTP 201 · outcome `exact_match_refreshed` · capture record link returned |
| 2 | Exact normalized URL refreshes one contact | `refreshed_exact_match` = 1; the seeded contact's stale title was replaced, the same-name decoy untouched |
| 3 | Identical retry is idempotent | HTTP 201 then 200 · `already_received: true` · same submission id · one snapshot, one note |
| 4 | Reused submission id with changed content | HTTP 409 `client_submission_id_conflict` |
| 5 | Older evidence cannot replace newer | Back-dated capture → `exact_match_unchanged`; the newer title stands |
| 6 | Sales Navigator rows saved without a campaign | HTTP 201 · 2 contacts · `staged_unmatched` = 2; the row with no `/in/` URL keeps a null identity |
| 7 | Same person twice in one submission | `duplicate_in_submission` = 1 · evidence preserved · reconciled once |
| 8 | Capture with no visible identity | HTTP 422 `validation_failed`, nothing stored |
| 9 | Submission carrying a campaign | HTTP 422 — the contract declares no campaign property |
| 10 | Legacy campaign-era payload posted to the new route | HTTP 422 `unsupported_contract` naming `/api/intake/linkedin-profile/stage` |
| 11 | Label registry is backend-owned and reusable | HTTP 200 · `Conference Lead`, `Healthcare`, `High Priority`, `Market Entry` |
| 12 | Save-vs-refresh lookup | HTTP 200 · `match: exact` · existence only, no contact field returned |
| 13 | Resulting capture and submission records open | Both operator pages render HTTP 200 |

Directly verified against the live database after the run:

| Check | Result |
| --- | --- |
| Matched contact's title | `Operations Manager` → `Director of Operations` |
| Same-name decoy | untouched (`Analyst`) |
| Labels applied to the matched contact | `Healthcare`, `Market Entry` (registry also learned the labels requested on unmatched captures) |
| Notes | append-only; every submission appended, none overwritten |
| Email candidates / campaign memberships created | 0 / 0 |
| Suppressed contact (active `email opt_out`) | outcome `suppressed`, title unchanged, `labels_applied` = 0, evidence still linked |
| Capture record page | renders capture mode, operator labels, append-only notes, reconciliation summary, person observations, experience observations |

Automated backstops at this branch head: backend `pytest` **506 passed**
(463 before; `tests/test_contact_capture_intake.py` adds 43); extension
`node --test` **186 passed** (109 before; 77 new); `ruff`, `ruff format --check`
and `mypy --strict` clean; `alembic` upgrade / check / downgrade round trips
clean on a fresh database.

## Layer 3B — authenticated operator runbook (manual, ~15 min, Sahil)

Prereqs: local backend running with the switches above; the extension loaded
unpacked; you are logged in to LinkedIn in the same Chrome profile. Every step
is an explicit operator action. **Not yet performed.**

- **C1.** Open a MAIN profile page (`linkedin.com/in/…`) of someone already in
  your data → panel shows *LinkedIn Person Profile* → *Read this profile page* →
  verify name / headline / location / current role / LinkedIn URL / About
  excerpt / experience count / connections / open-to-work / warnings against the
  page.
- **C2.** Confirm the primary action reads **Refresh Contact** (the backend
  recognised the exact URL). Save → the result reports `refreshed` with links to
  the contact and the capture record.
- **C3.** Press Save again without recapturing → "already saved — idempotent",
  same submission.
- **C4.** Open a profile that is NOT in your data → the action reads **Save
  Contact** → Save → `staged (new person)`; confirm any same-name candidates
  appear as review-only and no contact was created.
- **C5.** Add two labels and a note before saving → confirm both appear on the
  capture record, that the labels are offered again next time, and that a second
  capture of the same person appends a second note rather than replacing the
  first.
- **C6.** Open a Sales Navigator people-search results page → *Capture visible
  contacts* → exclude at least one row → confirm the Save button counts only the
  included rows → Save → counts match what you included.
- **C7.** Stop the backend, press Save → clear failure, draft and review stay
  intact → restart the backend → Retry → succeeds with the SAME submission id.
- **C8.** Open an unsupported page (a profile sub-route, or a Sales Navigator
  account search) → the panel explains which page to open and captures nothing.
- **C9.** If LinkedIn shows a login/checkpoint during the pass, confirm the panel
  switches to *Challenge / Login Required* and no capture is possible until you
  resolve it yourself.
- **C10.** Open the company page yourself → *LinkedIn Company Profile* → capture
  → confirm it saves company **evidence**, not a contact.
- **C11.** If you previously used the campaign-era extension: confirm the
  one-time "Workflow updated" notice appears, that *Download archived drafts*
  produces your old drafts, and that no campaign selector exists anywhere.

Record PASS/FAIL per step (redacting profile names/URLs) and file defects as
GitHub issues before closing the DAT-013 issues.

## Why the DAT-012 acceptance trial was paused

The DAT-011 / DAT-012 trial validated a **campaign-first** path: select a
campaign, stage a batch into its import workbench, then reconcile. The product
moved to permanent-contact acquisition, so that acceptance criterion can no
longer be satisfied honestly by the shipped code — the extension has no campaign
selector to exercise. The paused criteria are superseded by Layer 3 rather than
marked complete, and the legacy routes remain only so previously staged batches
and snapshots stay readable.

---

# Layer 4 — capture promotion acceptance (DAT-014)

The bridge from a staged capture to a canonical Contact, through the existing
DAT-010 logo.dev candidate flow. Policy and outcome vocabulary:
[`CAPTURE_PROMOTION.md`](./CAPTURE_PROMOTION.md).

## Layer 4A — sanitized live acceptance (completed, reproducible)

Environment: fresh `vmr_accept` database, `alembic upgrade head`, app started
with `APP_ENV=local`, `FEATURES__CONTACT_CAPTURE_INTAKE=true`,
`FEATURES__CONTACT_CAPTURE_PROMOTION=true`, `FEATURES__WORKBENCH=true`,
`FEATURES__SUPPRESSIONS=true`, `FEATURES__SALESNAV_DOMAIN_ENRICHMENT=true`, and
`LOGO_DEV_SEARCH_URL` pointed at the script's local stub.

**The provider is stubbed at the HTTP boundary.** The real logo.dev client, the
real enrichment service, the real workbench routes and the real database are all
exercised; only the provider itself is local, returning the documented Search
Brands response shape. No API key is used and no live logo.dev call is made.

Reproduce (backend running, as above):

```bash
python scripts/capture_promotion_acceptance.py --base-url http://127.0.0.1:8000
```

| # | Scenario | Result |
| --- | --- | --- |
| 1 | Unmatched capture is eligible for domain resolution | pending page lists it · `pending_lookup` · captured company shown |
| 2 | Provider candidates are stored, ranked, and left for review | `multiple_candidates_review_required` · 2 candidates · confidence shown as *not provided by this provider* |
| 3 | Promotion is refused while candidates await a decision | capture stays unpromoted; the reason is shown |
| 4 | A rejected candidate is preserved with its reason | moved to *Rejected candidates* with reason, actor and time |
| 5 | Operator confirmation resolves the company | `domain_candidate_confirmed` · source `candidate` · confirming operator recorded |
| 6 | Promotion creates the Contact and Company | `contact_created` · labels and notes carried over · capture linked |
| 7 | Retrying a promotion is idempotent | `already_promoted` · no second contact |
| 8 | A previously confirmed company is reused | `existing_company_resolved` · source `prior_mapping` · no provider call |
| 9 | The reused company promotes without a second lookup | `contact_created` against the same canonical Company |
| 10 | Promoted captures leave the pending queue | neither promoted person is listed as pending |

Database assertions taken directly after the run:

| Check | Result |
| --- | --- |
| Companies created | 1 (`Meridian Works` / `meridianworks.example`) — the second capture reused it |
| Contacts created | 2, both on the resolved domain, both with the captured title and profile URL, neither with an invented email |
| Enrichment records | 2, both capture-owned (`batch_id` null); one `ok` with source `candidate`, one `not_started` with source `prior_mapping` — the reuse cost no provider call |
| Rejected candidate | preserved with reason `different company, similar name` and the deciding actor |
| Label assignments | 4 (two labels × two contacts) |
| Notes | 2 of 2 linked to their promoted contact, text and timestamps unchanged |
| Provenance observations | `title`, `company_name`, `linkedin_url` appended to the DAT-005 ledger |
| Campaign memberships / email candidates | 0 / 0 |
| Captures | 2 of 2 linked to a contact, payloads intact |
| Promotion audit events | 2 |

Automated backstops at this branch head: backend `pytest` **555 passed**
(506 before; `tests/test_capture_promotion.py` adds 49); extension `node --test`
**186 passed** (unchanged — the extension is not part of DAT-014); `ruff`,
`ruff format --check` and `mypy --strict` clean; `alembic` upgrade / check /
downgrade round trips clean on a fresh database, with no orphaned enum types
after a downgrade to base.

## Layer 4B — live provider call (NOT PERFORMED)

A real logo.dev lookup needs `LOGO_DEV_API_KEY`, which the build session does
not have. Sahil's step, once a key is configured locally:

- **P1.** Start the backend with `FEATURES__SALESNAV_DOMAIN_ENRICHMENT=true`,
  `FEATURES__CONTACT_CAPTURE_PROMOTION=true` and a real `LOGO_DEV_API_KEY`
  (leave `LOGO_DEV_SEARCH_URL` at its default).
- **P2.** Open a pending capture and press *Run domain lookup*. Record the
  query, the returned candidate names and domains, and the lookup status.
  **Redact nothing about the provider; redact any real person's details.**
- **P3.** Confirm the correct candidate and check the recorded confirmation
  source, actor and time.
- **P4.** Promote, then open the resulting Contact and Company and confirm the
  labels, notes and provenance carried over.
- **P5.** Confirm no campaign membership, email candidate, verification, score
  or approval was created.
- **P6.** Repeat with a second person at the same company and confirm
  `existing_company_resolved` / `prior_mapping` with no second provider call.

Record PASS/FAIL per step. Everything above Layer 4B has been run; nothing in
Layer 4B has.

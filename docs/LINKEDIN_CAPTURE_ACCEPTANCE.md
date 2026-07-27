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

## Layer 4B — live provider call (PERFORMED 2026-07-26, PASS — complete)

Run against the real endpoint (`https://api.logo.dev/search`) at commit
`823c315`, on the dedicated `vmr_dat014` database, with the fictitious fixture
identities. **One** live provider call served the whole pass.

| Step | Requirement | Result |
| --- | --- | --- |
| P1 | Backend on the real endpoint with a real key | PASS — provider `logo.dev`, lookup version `logo.dev/search-brands/v1` |
| P2 | One lookup; candidates recorded truthfully | PASS — `ok`, 10 candidates, provider order preserved as `rank` |
| P2a | Confidence not invented | PASS — the provider returned **no** score/confidence field on any result; stored as explicit `null` |
| P2b | Nothing auto-confirmed | PASS — `multiple_candidates_review_required`, promote disabled, promotion refused |
| P3 | Operator confirmation recorded with source, actor, time | PASS — `domain_candidate_confirmed`, source `candidate`; two rejections preserved with reason, actor and time |
| P4 | Promotion creates Company and Contact; labels and notes carry over | PASS — one Company on the confirmed domain, Contact on that domain with no invented email, 2 labels and 1 append-only note carried |
| P4a | Capture immutable | PASS — only `matched_contact` changed |
| P5 | No permission granted | PASS — no campaign membership, email candidate, verification, score, draft or approval |
| P6 | Prior mapping reused with no second call | PASS — `existing_company_resolved` / `prior_mapping` at `not_started · 0 attempt(s)` before any operator action |

**Attempt history, recorded honestly.** Workbench attempt 1 returned
`api_unavailable`: the request was refused at the CDN edge (Cloudflare 1010,
`browser_signature_banned`, HTTP 403) because the client sent urllib's default
`Python-urllib/x.y` User-Agent. A publishable (`pk_`) key was configured at that
moment, but the request never reached provider authentication, so the key type
is **not** the proven cause. Diagnostic probes were made outside the application
and did not increment `lookup_attempts`. Workbench attempt 2 succeeded, running
commit `823c315`, which sends a truthful application `User-Agent`. Until that
commit, no live logo.dev lookup could succeed at all.

**Ambiguity was real.** The provider returned four different domains all named
"Mozilla". Auto-accepting the top-ranked name match would have chosen a domain
no operator sanctioned — the confirmation requirement is vindicated by live data,
not only by fixtures.

Sanitized evidence lives outside the repository in `layer4b/`
(`dat014_live_evidence.txt`, `dat014_live_evidence_db.txt`), because it is
operator run-evidence rather than source. Both shell verifications have now run
and **both passed**:

* `capture_state.py --compare` — byte-level capture immutability: only the
  canonical contact link changed.
* `run_assertions.py` — a thin shim over the committed, tested harness at
  [`scripts/layer4b_assertions.py`](../scripts/layer4b_assertions.py). Result at
  commit `44507fd`: checks **A, B, C, C2, D, E, F, G, H, I, L all PASS**, none
  failed, none empty, **OVERALL: PASS**. Check C2 recorded scoped provider
  attempts 2 against authorised attempts 2, with 1 record reused without a
  lookup. The sanitized informational section reported 1 excluded capture
  carrying 1 excluded provider attempt.

Nothing in Layer 4B is outstanding.

### The first aggregate assertion run failed on a harness defect, not a product defect

The harness originally graded **every** capture-owned row in the database. That
local database also holds captures created while exercising the extension
against real LinkedIn pages, so the first run reported:

* check A failed — an unrelated capture's lookup was `API_UNAVAILABLE`;
* check C failed — that same capture was never confirmed;
* check C2 reported 3 aggregate attempts (Morgan's 2 plus an unrelated 1).

Morgan's and Riley's own sanctioned fixture flows passed throughout. The three
failures were scoping defects in the harness: acceptance-scope questions were
being answered with database-wide data.

**No product data was changed to resolve this.** The unrelated captures were not
deleted, altered, reset or "repaired" — they are legitimate records and remain
exactly as they were. The harness was corrected instead: every graded check is
now scoped to the two sanctioned synthetic acceptance captures, and unrelated
rows appear only in a sanitized informational section reporting a count of
excluded captures and their aggregate attempts — no name, URL, company, payload
or other personal data. `tests/test_layer4b_assertions.py` holds the scoping in
place and, equally, proves it did not merely disable the checks: a sanctioned row
with a wrong status, a missing confirmation, invented candidate confidence, a
missing rank, an unreasoned rejection, an invented email or a wrong attempt count
still fails, and asking for the aggregate figure instead of the scoped one fails
too.

With the corrected scope, check C2 counts Morgan's 2 attempts plus Riley's 0 for
a total of 2, matching what was authorised — which is exactly what the rerun
reported. The unrelated capture and its single attempt appear only as counts in
the excluded section, where they cannot influence any verdict.

This layer accepts **DAT-014 provider resolution and contact promotion only**.
It says nothing about extension extraction correctness — see DAT-016 (#167),
which is open, and Layer 3B, whose step C1 fails.

---

# Layer 5 — authenticated top-card acceptance (DAT-016, C1)

**Performed 2026-07-27** against real, operator-opened `/in/` profiles in an
authenticated Chrome session, on branch `feat/dat-016-profile-selector-hardening`.

Six live profiles were inspected. Everything below is sanitized: profiles are
labelled A–F, field values are described by **shape** (word/digit pattern and
length) rather than content, and no name, handle, URL, employer, school, member
id or raw DOM is recorded. Live pages were read; nothing from them was committed.

## What C1 established before any extraction ran

`componentkey` attributes containing `topcard` **do exist** on the current
profile DOM — typically five per page, two of which hold the name heading. The
parser's Strategy A therefore selects the container on every live profile, and
the measured heading-climb (Strategy C), which the synthetic fixtures exercised
most heavily, is a fallback in practice.

That distinction is the reason C1 found a defect the automated suite could not:
Strategy A inherits whatever LinkedIn puts in its own card.

## Samples

| Sample | Structural characteristics | Blocks in card |
| --- | --- | --- |
| A | Complete card; two degree badges; very long headline (204 chars); followers **and** connections; contact-info row; mutuals | 19 |
| B | Complete; pronoun line + two degree badges; company · school row with both logo slots; followers + connections | 20 |
| C | Complete; pronoun + degree badges; company row with logo slot; followers only, no connection count node | 20 |
| D | **Self-view**: no degree badge, no pronoun line, no mutuals; contact-info row present | 17 |
| E | Complete; long headline (70 chars) with mixed punctuation and domain-like text | 20 |
| F | **Interrupted card**: promo line, interface controls and an ad-preferences panel (≈70 `option`/`legend`/`label` elements) sitting **between the name and the real top-card rows** | 124 → 50 |

## Field-by-field result

Verified against what was visibly rendered on each page.

| Field | A | B | C | D | E | F (before fix) | F (after fix) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Full name | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| Headline | PASS | PASS | PASS | PASS | PASS | **FAIL** — promo line returned | PASS |
| Location | PASS | PASS | PASS | PASS | PASS | **FAIL** — dropdown option returned | PASS |
| Current company | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| Current school (where shown) | n/a | PASS | PASS | n/a | n/a | n/a | PASS |
| LinkedIn profile URL | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| Connection count | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| Follower count (not a stored field) | not mistaken for connections | same | same | same | same | same | same |
| Section headings never became the name | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| Company/school row never became headline or location | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| Action labels never became a field | PASS | PASS | PASS | PASS | PASS | **at risk** | PASS |
| Missing count never became `0` | PASS | PASS | PASS | PASS | PASS | PASS | PASS |

Every sample resolved location through the **contact-info row** strategy; the
ordering fallback was never needed.

## Defect found

**D-1 — interrupted top card produced a confidently wrong headline and location.**

* Visible: headline was a job title at an employer; location was a city/region/country line.
* Extracted: headline was a promotional sentence (24 chars) from a block above the real card; location was an ad-preferences dropdown option, shaped like a place.
* Structural cause: Strategy A selected LinkedIn's own card, which on this profile also contained a promo block and a preferences form. The candidate window's lower bound was the **name heading**, which assumes the heading is immediately followed by the rest of the card. It was not.
* Classification: genuine parser defect — not a stale load, not an unsupported surface. Both fields were wrong, plausible, and unwarned. Blocker.

A second, latent gap was measured on samples A, B, C and E: action labels
rendered as `<div role="button">` or nested in a plain `<a>` were **not**
classified as controls. They were harmless on those profiles only because they
sat after the connection region, which the upper bound already excluded. On a
profile with no visible count region they would have become location candidates.

## Fixes

All three are tightenings. None can invent a value; the worst any of them can do
is leave a field null with a warning.

1. **Anchored candidate window.** Lower bound is now the last name-row anchor
   (degree badge, pronoun line, or the name) that still precedes the connection
   region, instead of the name heading.
2. **Non-profile subtrees are never collected.** Form controls, their labels and
   legends, popup/menu/listbox roles, and `aria-hidden` subtrees.
3. **Any `role="button"` is a control**, not only `<button>` and
   `<a role="button">`; `menuitem` and `tab` included.

Regression fixture: `profile-interrupted-topcard.html`, synthetic, authored from
the structural description above. Two of the four new tests fail against the
pre-fix parser; the other two are guards that the pre-fix parser also passed on
this fixture.

## Re-verification through the shipped extension

The fix was first validated by running the parser's decision path against the
live DOM. That is not the same as validating the extension, so all six samples
were then re-run **through the unpacked extension's side panel**, operator-pressed,
on `7268b63`. The results below are what the panel displayed.

| Sample | Name | Headline | Location | Connections | Status |
| --- | --- | --- | --- | --- | --- |
| A | correct | 204-char headline intact | correct | `500` (follower count not substituted) | partial (experience not loaded) |
| B | correct | correct | correct | `500` (not the 1,226 follower count) | partial |
| C | correct | correct | correct | **`—` + warning** | partial |
| D | correct | correct | correct | `500` | ok |
| E | correct | 70-char headline intact, name keeps its trailing period | correct | `500` (not the 1,731 follower count) | partial |
| F | correct | **correct — was the promo line** | **correct — was a dropdown option** | `500` | ok after scroll |

Three results are worth calling out because they are the ones that could only be
obtained live:

**Sample C is the null-behaviour proof.** The page showed a follower count and
**no** connection count. The panel reported connections as `—` with the sentence
*"connections was shown but could not be read"*. Not `0`, not the follower
count, and not silence: the `unparsed_value` code — "a region was there and I
could not pair it" — survived from the parser to a sentence an operator can act
on. A zero there would have been a blocker.

**Recapture stability was confirmed on two profiles.** A and F were each captured
twice, before and after scrolling. Name, headline, location, profile URL and
connections were byte-identical across both captures; only the lazy-loaded
Experience section changed, from `0` entries with *"Captured with gaps"* to the
full list with status `ok`. Absent-and-flagged, then correct — never wrong.

**The chained-experience layout was checked and is correct.** A profile with
three roles at one employer records all three with distinct date ranges and
marks exactly one `Current: yes`; the current-role field shows only that one.
The history is retained deliberately as provenance.

## Second defect found, NOT fixed here

**D-2 — the panel's MODE card reports a stale source URL after navigation.**

Observed on sample B: the panel displayed the *previous* profile's URL in its
MODE card while the review card below correctly showed the *current* profile's
data. The "N experience entries visible" badge is stale in the same way, and a
`Refresh` press corrects both.

The extracted data was never wrong — the content script reads the live tab. But
a capture tool whose provenance line disagrees with its payload is a trust
defect regardless, and an operator could reasonably believe they had captured a
different person.

This is **surface detection in the side panel, not top-card extraction**. It is
out of DAT-016 scope and is deliberately not fixed in this branch; it needs its
own issue and a `chrome.tabs.onUpdated` listener.

## Unrelated finding worth an issue

One sampled profile's **About** section ends with an instruction addressed to
language models, telling any model reading it to disregard its previous
instructions. It is captured verbatim into stored evidence, which is correct
parser behaviour — About text is evidence, not instruction.

It is recorded here because captured About text later flows into research and
drafting stages. Any component that puts this field in front of a model must
treat it as untrusted data. No change is made under DAT-016.

## Disclosed for review

The fixture contains one **real component identifier**,
`componentkey="com.linkedin.sdui.profile.card.topcard"`, because Strategy A only
fires when a `topcard` componentkey exists and a fixture that invents one would
not exercise the live path. It is a build-time component path shared by every
profile and carries no identity — no handle, member id, URN or token. Replaceable
on request, at the cost of the fixture no longer reproducing the live strategy.

## Limitations recorded, not fixed

* `raw_lines` remains verbatim page text and therefore carries the interruption's
  furniture on sample F. That is evidence noise, not a wrong field, and filtering
  it would defeat its purpose.
* No live profile with a `--` placeholder headline or a hidden connection count
  was found among the six. Those paths remain covered by fixtures only.
* Follower count is observed but not a stored contract field; C1 verified only
  that it is never mistaken for the connection count.

# Layer 6 — UI-011 live tab-following (#179)

Run against the unpacked extension on an authenticated session, on real
`/in/` profiles the operator opened. Profiles are labelled A/B/C; no name,
handle, URL, member id or page content is recorded here.

## How this layer was evidenced

Two passes with different reach, kept apart on purpose:

* **Pass 1 — instrumented browser session.** Could drive navigation and measure
  the live page directly, but could **not** see the side panel: the panel is
  browser UI, not page content. It also ran while the local backend was down
  (`127.0.0.1:8000` refused the connection), so it could make no claim about
  backend writes. It produced findings 1 and 2 below.
* **Pass 2 — operator at the machine.** Could see the panel, with the extension
  reloaded at the final revision and the backend running. It produced the
  results under "Operator-observed results".

Neither pass alone accepts this feature. Each result below names its pass, and
the closing section lists what neither pass covered. No claim here rests on
"the tests pass": the 24 deterministic tests cover the state that drives the
panel, which is a different claim from "the shipped panel showed the right
thing", and the two are not merged.

What pass 1 verified is the browser-side mechanics the panel depends on,
measured directly on live pages.

## Finding 1 — the history patch could never have worked (fixed)

The content script installed a wrapper over `history.pushState` and
`history.replaceState` to notice single-page navigation, with a comment
claiming this was "the only way to see them from in here".

Measured on a live profile-to-profile move, performed by clicking an in-app
profile link:

| Observation | Value |
|---|---|
| Document reloaded | no — a main-world probe installed before the move survived it |
| `pushState` calls made by the page | 1 |
| `replaceState` calls made by the page | 1 |
| `popstate` events fired | 0 |
| URL | changed from one `/in/` profile to another |

A content script runs in an isolated world. Assigning to `history.pushState`
from there rebinds that world's copy; the page calls its own. The page made
exactly the calls the wrapper was written to intercept, and the wrapper was in
no position to see any of them. No test caught this because the test harness
patches a fake history object in a single world, where the assignment does work.

The signal itself was never lost, by accident rather than by design: the
mutation observer calls the same href comparison on every burst, and an SPA
navigation always rewrites the document. The panel also learns about the move
independently from `chrome.tabs.onUpdated`, which fires with `changeInfo.url`
for history navigation and which `live-sync` already handles without requiring
`status === "complete"`.

Fixed by removing the ineffective wrapper and documenting both paths that do
work. `popstate` is kept: it is a real DOM event, it does reach the isolated
world, and it covers back/forward moves that mutate little.

## Finding 2 — the loop guard's stated premise was wrong (rationale corrected)

The reread guard was committed with the justification that a LinkedIn profile
"mutates continuously", so an ungated observer would re-parse forever. Measured
with a `MutationObserver` over the whole body subtree:

| Window | Mutation batches | Mutation records |
|---|---|---|
| ~11 s after arriving at a profile | 51 | 619 |
| ~11 s spanning two operator scrolls | 34 | 229 |
| 10 s idle, no interaction (profile 1) | 0 | 0 |
| 10 s idle, no interaction (profile 2) | 0 | 0 |

An idle profile is quiet. The guard is still correct, but for a different
reason than the one recorded: the failure mode is burst amplification, not an
endless loop. Ungated, a single scroll could cost a dozen full re-parses, and
because each completed read triggers a contact-existence lookup, a dozen
backend requests with it. The `minRereadMs` floor is what addresses that; the
`isComplete` gate is what stops work after the page is fully read.

The original commit message overstates this. It is corrected here and in the
code comment rather than by rewriting published history.

## Automatic backend behaviour, as read from the code

`live-sync` performs no submission. The one automatic outbound request is
`PROFILE_MATCH_STATE` → `lookupContact`, reached from `syncProfileSaveAction`
whenever a draft is present:

* `fetch(url, { signal })` with no `method` and no body — a GET.
* Route is `@router.get`, and its handler runs a read query and returns
  existence only: `match`, `contact_count`, and the normalized URL. It writes
  no row and records no audit event.
* It is skipped entirely while no draft exists, so the page-change and loading
  phases of a sync do not call it. One completed read, one lookup.

The profile URL travels in a query string to the loopback backend. That is
pre-existing behaviour, but UI-011 makes it automatic: the URL of every profile
the operator merely *looks at* now reaches the local backend without a click.
Operators should know that; it is not hidden by this change, and it is called
out here rather than left to be discovered.

## Operator-observed results (pass 2)

Observed by the operator at the machine, with the extension reloaded at the
final revision and the backend running. Roughly five to eight real profiles
were visited. Sanitized: no name, handle, URL or captured value is recorded.

| # | Scenario | Result |
|---|---|---|
| 1 | Moving across ~5–8 profiles with no `Refresh` press | **PASS** — the panel followed every move on its own |
| 2 | Source card and preview describe the same profile | **PASS** — stayed aligned throughout |
| 3 | Name, company, location and experience update on each move | **PASS** — fields tracked the profile actually open |
| 4 | No repeated reread, flicker or refresh loop during normal navigation | **PASS** — none visible |
| 5 | Explicit `Save Contact` creates the capture | **PASS** — succeeded once `CONTACT_CAPTURE_INTAKE` was enabled |
| 6 | No automatic save while merely browsing | **PASS** — none appeared in the operator flow |

Scenario 5 is worth noting operationally: the capture routes are behind
`CONTACT_CAPTURE_INTAKE`, and with the flag off the backend returns 404 rather
than failing loudly in the panel. That is the intended boundary, not a defect,
but an operator who has not enabled the flag will see saves quietly do nothing.

Scenario 4 is a *visual* result over normal browsing. It is consistent with the
bounds measured in pass 1, but it is not a counted measurement of rereads, and
it is not evidence about idle-time request frequency — see the limitations.

## Automatic contact lookup — accepted decision

`PROFILE_MATCH_STATE` → `lookupContact` is **approved as a read-only existence
check**. It exists so the panel can label its primary action `Save Contact` or
`Refresh Contact` before the operator commits.

The standing constraint, recorded so any future change is measured against it:

> Merely browsing a profile must never create persistence. The automatic lookup
> may read whether a contact exists; it must not create, modify, promote or
> capture one, and it must not cause any row, audit event or draft to be written
> on the backend. If a change would make browsing produce a write, it is not a
> refinement of this lookup — it is a different feature and needs its own
> decision.

Verified read-only by inspection: `fetch(url, { signal })` with no method and no
body; `@router.get` on the backend; the handler runs one read query and returns
`match`, `contact_count` and the normalized URL; no row written and no audit
event recorded. Operator observation (scenario 6) is consistent with this, but
the code is the binding evidence, not the observation.

The trade-off is stated plainly: the URL of every profile the operator merely
looks at now reaches the loopback backend in a query string, without a click.
That is accepted, not overlooked.

## Not verified — disclosed limitations, not passes

None of the following were tested. They are listed so nobody reads the table
above as covering them.

* **Rapid A → B → C stale-result race.** Not separately tested in either pass.
  Normal-speed navigation passed, which is not the same thing: the race only
  appears when a slow read for A or B returns after the operator has already
  reached C. The three stale guards (sequence, generation, page key) are covered
  by deterministic tests only.
* **Unsupported-page clearing.** Not tested. Whether stale profile data is
  cleared or visibly marked unavailable when the operator leaves `/in/` for an
  unsupported page has test coverage only.
* **Automatic lookup request frequency.** Not measured, idle or otherwise. No
  request count of any kind was observed live.
* **Database-level proof of zero writes before submission.** The database was
  not inspected before and after a browsing session. The operator saw no
  automatic save in the UI, and the code path is read-only by inspection, but
  neither is a row count.
* **Panel behaviour in pass 1.** The instrumented session could not see the
  panel at all; every display result above comes from pass 2 only.

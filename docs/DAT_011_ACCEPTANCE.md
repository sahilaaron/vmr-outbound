# DAT-011 — authenticated acquisition-path trial (#131)

Acceptance and evidence record for the real, operator-controlled acquisition
path. This is **not** a feature-development task: nothing here authorizes new
capability, and no defect found during the trial is fixed by broadening it.

Sensitive-data policy (unchanged from
[`LINKEDIN_CAPTURE_ACCEPTANCE.md`](./LINKEDIN_CAPTURE_ACCEPTANCE.md)): no
credentials, cookies, auth headers, browser storage, private messages, raw
private-page HTML or raw captured PII may appear in this file. Evidence is
counts, states, timestamps, sanitized identifiers and pass/fail observations.
Real people are referred to as P1…Pn and companies as C1…Cn.

---

## 1. Baseline

| Item | Value |
| --- | --- |
| `main` | `d99e274` (merge of PR #189, VM Prospector) |
| Working tree | clean; `origin/main` == local `main` at reconciliation time |
| Extension name | **VM Prospector** (`manifest.json`) |
| Extension version | **2.1.0** (set by DAT-018 `676e986`; unchanged by UI-012) |
| Contract | `linkedin-contact-capture/2.0.0` → `POST /api/intake/contact-captures` |
| Backend target | `http://127.0.0.1:8000`, local Postgres database `vmr_dev` |
| Extension suite at baseline | `npm test` → **338 passed, 0 failed** |

Feature flags this trial requires (all default `false`):

| Flag | Needed for |
| --- | --- |
| `FEATURES__CONTACT_CAPTURE_INTAKE` | the capture route at all — with it off the backend returns **404** and saves quietly do nothing (recorded in Layer 6) |
| `FEATURES__WORKBENCH` | opening the returned submission / capture records |
| `FEATURES__CONTACT_CAPTURE_PROMOTION` | the pending queue, domain decisions and promotion |
| `FEATURES__SALESNAV_DOMAIN_ENRICHMENT` | the DAT-010 candidate lookup |
| `FEATURES__SUPPRESSIONS` | suppression remaining authoritative |

Confirmed enabled by the operator before the trial; `/contact-captures/pending`
renders. Explicitly **not** enabled: `email_generation`, `millionverifier`,
`scoring`, `insights_research`, `drafting`, `saleshandy`. No RDS, no production,
no sending.

### Scoping, because the trial runs against a shared development database

`vmr_dev` already holds captures from earlier development and from the DAT-016
and UI-011 passes. Layer 4B recorded what happens when acceptance questions are
answered with database-wide data: three checks failed on unrelated rows, and the
fix was to scope the harness, not to touch the data.

The same rule applies here. **Every Phase 4 count is scoped to the identifiers
this trial creates** — the submission ids returned during the trial and the
capture ids reachable from them. Pre-existing rows are never counted, never
altered, and never "cleaned up". Where a database-wide figure is informative it
is reported separately and labelled as such, and it decides nothing.

### How this trial is operated, and what that costs the evidence

Two surfaces, two operators, recorded because it changes what the evidence
proves.

* **The side panel is operator-only.** Browser automation reads and clicks
  *page* content. A side panel is browser UI, not page content — a screenshot
  taken through the automation tools returns the tab viewport with no browser
  chrome in it. Layer 6 recorded the same limitation during UI-011 ("could
  **not** see the side panel"), and it was re-confirmed here rather than
  assumed. Every A, B, C and D step is therefore performed by Sahil.
* **The workbench is loopback and carries no LinkedIn session.** Phase E and the
  Phase 4 inspection run against `http://127.0.0.1:8000`. Those were driven
  through browser automation at Sahil's explicit instruction, with him present.

That second point is a real qualification on #131, which says *"the operator
controls page navigation, capture, review, submission, candidate selection,
preview, and confirm"*. For the workbench half, the decisions were Sahil's and
the clicks were not. It does not change what the product did, but a reader
deciding whether operator control is proven should know which half is which, so
each result below is marked **[operator]** or **[machine, supervised]**.

### Working constraint: the workbench renders real captured people

`/contact-captures/pending` lists 50 pre-existing captures with real names,
titles and companies. No screenshot of a workbench listing page may be committed
or pasted into evidence, and no captured value is reproduced in this document.
Where a figure is needed it is read as a count.

---

## 2. Issue reconciliation

### 2.1 Why #131 needs reconciling at all

#131 was written against the **campaign-first** model: select a campaign, stage a
batch into its import workbench, preview, then confirm. DAT-013 replaced that
with **contact-first** acquisition. `LINKEDIN_CAPTURE_ACCEPTANCE.md` already
records that the DAT-012 trial was paused for exactly this reason — "the
extension has no campaign selector to exercise".

Reconciliation rule applied throughout: a requirement is only marked obsolete
when the shipped code makes it **impossible to satisfy honestly**. A requirement
whose *mechanism* moved is marked renamed or moved, not dropped.

### 2.2 The one requirement that survives its own wording

#131 scenario 9 — *"Preview without committing and verify zero premature
contacts"* — reads as campaign-era wording, but the substance is still the
merged contract, under different names and at a different boundary:

* `app/services/captures/intake.py` `_OUTCOME_COUNTER` maps **no** snapshot
  outcome to the `created` counter. The capture route therefore reports
  `created: 0` for every submission — capture stores immutable evidence and
  never creates a Contact.
* `app/services/captures/promotion.py` is the only place a `Contact(` is
  constructed, and only after a company domain has been resolved by an explicit
  operator decision.
* `docs/CAPTURE_PROMOTION.md` states it directly: *"DAT-013 therefore reports
  `created: 0` and stores the person as permanent capture evidence. DAT-014
  resolves the domain … and only then creates the contact."*

So the non-committing step is **capture**, and the explicit commit is
**promotion** — not a preview/confirm pair inside one import. The requirement is
kept, restated against the real boundary.

One caveat found while verifying this, recorded as an observation rather than a
defect: `created` is a declared response counter that nothing can increment. The
side panel has a label for it, so a `created` outcome would render if it ever
appeared. Harmless today (the count is always 0 and the panel only renders
non-zero counts), but it is dead vocabulary in a contract that otherwise says
only true things. See D-OBS-1 below.

### 2.3 Reconciled trial matrix

Status key — **VALID** (unchanged), **RENAMED** (same requirement, new
name/shape), **MOVED** (now proven at a different surface), **PROVEN**
(already established by a merged task; re-proof not required), **OBSOLETE**
(cannot be satisfied honestly by the shipped code), **DEFERRED** (blocked on an
unmerged dependency).

| # | #131 requirement | Status | Reconciled form / reason |
| --- | --- | --- | --- |
| S1 | Capture a visible result page with more than one company | **VALID** | Unchanged. DAT-018 adds that rows with no company name are *skipped*, so "more than one company" must be read from the rows that survive eligibility. |
| S2 | Exclude at least one record before submission | **RENAMED** | UI-012 inverted the control: a ticked box means *included*. Same `TOGGLE_EXCLUDE` message. Step becomes "deselect at least one and confirm the Save count follows". |
| S3 | Retain one incomplete/uncertain record, verify truthful handling | **RENAMED + SPLIT** | DAT-018 created two distinct truthful behaviours that must not be conflated. **S3a retained-and-flagged**: missing location, uncertain identity, no stable link — kept, badged, submitted with the gap visible. **S3b skipped**: no company name — never enters the batch, reported as *N skipped — no company name*, nothing invented. |
| S4 | Submit to an active or draft campaign through the extension selector | **OBSOLETE** | Contact-first. No selector exists; `campaign-decoupling.test.js` forbids one; the contract declares no campaign property and a submission carrying one is rejected 422. **Replacement:** prove capture completes with no campaign and creates 0 campaign memberships. |
| S5 | Close/reopen the surface, restore the last staged result | **VALID** | Draft + `lastResult` restoration, preserved through UI-012. **Contradicted by the trial** — the draft half restores, the result half does not. See D-8; this reconciliation claim was wrong. **D-8 is fixed under UI-016 / #194 (§8); the live S5 re-run is still owed.** |
| S6 | Open the exact returned batch in the workbench | **RENAMED** | "Batch" is now a **submission**. The response returns `operator_workbench_url` and per-record `capture_url` / `contact_url`; the panel will only open loopback URLs under known prefixes (`handoff.isOpenableWorkbenchUrl`). |
| S7 | DAT-010 domain candidate lookup for unique companies | **MOVED** | No longer in the extension. `POST /contact-captures/{id}/company/lookup` in the workbench, over the DAT-010 provider client, gated by `SALESNAV_DOMAIN_ENRICHMENT` + `CONTACT_CAPTURE_PROMOTION`. |
| S8 | Confirm one candidate, override one, leave one unresolved | **MOVED, fully exercisable** | `POST …/company/confirm` accepts `decision=candidate` (confirm), `decision=manual` (typed domain override) and `decision=unresolved` (`LEFT_UNRESOLVED`); `POST …/company/reject` preserves a rejection with its reason. All three of #131's actions exist. |
| S9 | Preview non-committing; zero premature contacts | **RENAMED + MOVED** | See 2.2. Capture is the non-committing step (`created: 0`); promotion is the explicit commit. |
| S10 | Explicitly confirm and inspect outcomes | **RENAMED** | Outcome vocabulary is now `refreshed_exact_match`, `exact_match_unchanged`, `staged_unmatched`, `staged_ambiguous`, `duplicate_in_submission`, `suppressed` (capture) and the `CompanyResolutionOutcome` / `ContactPromotionOutcome` enums (promotion). |
| S11 | Retry/resubmit unchanged input → idempotent | **VALID** | Stable identity is `client_submission_id`, not `client_batch_id`. Reused id with changed content is a 409 by design. |
| S12 | One recoverable failure path without losing reviewed draft or stable identity | **VALID** | Preserved through UI-012; the panel keeps the draft and replays the same submission id on retry. |
| AC | "Domain confirmation propagates correctly to matching staged rows" | **RESHAPED** | Campaign-era batch language. The merged behaviour is per-capture confirmation plus **`prior_mapping` reuse**: a domain an operator already confirmed for the same normalized company (and, where both know it, the same LinkedIn company id) is reused without a second provider call. That reuse is what "propagates" now. |
| AC | Original raw capture data remains immutable | **VALID** | Phase 4 check. |
| AC | Unresolved companies remain rejected or held truthfully | **VALID** | `no_candidate`, `left_unresolved`, `company_identity_ambiguous`, `lookup_unavailable`. |
| AC | Idempotent retry produces no duplicate batch or contacts | **VALID** | |
| AC | Sanitized evidence sufficient for independent review | **VALID** | This document. |

### 2.4 Already proven by merged work — not re-proved here

| Area | Where | Consequence for this trial |
| --- | --- | --- |
| Contact-first backend contract, 13 scenarios incl. idempotency, 409 on changed content, campaign rejection | Layer 3A, reproducible via `scripts/contact_capture_acceptance.py` | The live trial confirms the *operator path*, not the contract arithmetic. |
| Domain candidates, confirmation, rejection, reuse, promotion, immutability | Layer 4A (stubbed provider) and Layer 4B (**live** logo.dev call, PASS) | S7/S8 in the live trial is a shipped-path confirmation, not first proof. |
| Profile top-card extraction against six live profiles | Layer 5 (DAT-016) | Person-field correctness is established; B-scenarios check the *panel presentation* of it. |
| Live tab-following, and that browsing performs no write | Layer 6 (UI-011) | The trial must still not assume a write-free browse — it is proven by code inspection, not by a row count. |

### 2.5 Deferred — do not claim

* **DAT-017 automatic domain resolution is NOT merged.** `2162692` exists only on
  `origin/feat/dat-017-automatic-domain-resolution`; `git merge-base
  --is-ancestor` against `origin/main` is false. This trial therefore records the
  **DAT-010 + DAT-014 operator-confirmation** state only. No confirmed /
  provisional / unresolved automatic workflow may be described as current.

### 2.6 Superseded acceptance wording found during reconciliation

Not defects in the product, but they will mislead the next reader:

* `LINKEDIN_CAPTURE_ACCEPTANCE.md` **Layer 3C S1 and S2** describe the pre-UI-012
  panel — *"a compact chip directly under **VMR Contact Capture** reading
  SalesNav Listing"*. UI-012 replaced the chip with the full-width detected-page
  strip and renamed the product. The *intent* of S1/S2 (surface switching is
  visible and correct) is carried into A0/B0/C0 below.
* `docs/CLAUDE.md` still names the product *"VMR Contact Capture"*.

Both are documentation drift from the UI-012 rename. Recorded as **D-OBS-2**;
not fixed here, because DAT-011 is an acceptance task.

---

## 3. Trial plan and results

Every step is an explicit operator action performed by Sahil in an
authenticated Chrome profile with the unpacked extension loaded from `d99e274`.
Nothing in this plan authorizes unattended traversal, pagination, background
navigation or anti-bot behaviour.

**Status: NOT YET PERFORMED.** Results are recorded per step as they are
returned, with counts and states only.

### A. Sales Navigator listings

| Step | What to do | What must be true | Result |
| --- | --- | --- | --- |
| A0 | Open the panel on a people-search results page | Header reads **VM Prospector**; detected-page strip reads *Sales Navigator · Search results*; no campaign selector anywhere | **PASS** [operator] — see A0 notes |
| A1 | Press *Capture visible contacts*; watch | Progress card appears with a live row count; scrolling is incremental, not jumpy | **PASS** [operator] — live count observed at 15 → 21 → 24; pass ended on its own; *Stop reading this page* offered throughout |
| A2 | Press *Stop reading this page* once, mid-pass | Pass stops promptly; view returns to top; rows already loaded remain reviewable; nothing submitted; feedback reads *Stopped* | **PASS** [operator] — stopped on request, view returned to the top, and only the rows read up to that point were merged into the existing batch |
| A3 | Press *Read this page again*; let it finish | Pass ends by itself; **no** page-2 advance, no new tab, no URL change | **PASS** [operator] — observed during A1: terminated on its own with no page advance, new tab or URL change |
| A4 | Review the list | ≥2 distinct companies among eligible rows; any no-company rows appear under *N skipped — no company name* and are absent from the list | **PASS — in full, 2026-07-28 re-run** [operator, machine-verified] — 12 distinct companies in a 12-row sample, and **S3b exercised for the first time**: 1 row skipped with `missing_company_name`, reported and absent from the list. See §6 |
| A5 | Deselect at least one row | *Review selected (N)* and the Save label follow the selection | **PASS** [operator] — one row deselected: tiles moved to 30 selected / 1 deselected, the badge to *30 need review*, and the primary action to *Capture 30 prospects*. **S2 satisfied** |
| A6 | Confirm a flagged row is retained (S3a) | A row with a warning is still selectable and still submitted with its gap visible | **PASS for the provenance case; fault case NOT EXERCISED** [operator] — 63 rows across 3 pages, none carrying a review fault. See §6 |
| A7 | Confirm nothing was invented for a skipped row (S3b) | No company borrowed from headline, school, location or an adjacent row | **PASS — 2026-07-28 re-run** [operator, code-verified] — a company name was **visible as plain text** on the skipped row and was still not taken. See §6 |
| A8 | Save the reviewed set | Submitted count == selected count; outcome counts render; `created` is 0 | **PASS** [operator] — *30 of 30 prospects saved*; sole outcome `staged_unmatched` = 30; **`created` absent, i.e. 0**. **S9 and S10 satisfied** |
| A9 | Close and reopen the panel | Last result and/or draft restored without recapturing or resaving | **PARTIAL** [operator] — the **draft** restores intact (selection and all); the **last result does not**. Reproduced deterministically after a fresh save. Cause found and demonstrated: **D-8**. **Fixed under UI-016 / #194 and verified in the harness (§8); this row stays PARTIAL until the live re-run is performed.** |
| A10 | Open the returned record via the panel's own link | The exact submission/capture record opens in the workbench | **PASS** [operator, machine-verified] — *Open captured contacts* opened `/contact-captures/submissions/01366e2e…`, the exact submission, with `client_submission_id 77ae7ae0…`, `contacts 30`, `created 0`. Reachable only straight after a save — see D-8 |

#### A0 observations (2026-07-27)

Shell, as displayed:

| Element | Observed | Verdict |
| --- | --- | --- |
| Header | **VM Prospector**, with the VMR mark | PASS |
| Detected-page strip | *Sales Navigator · Search results* | PASS |
| Strip badge | *47 found* | PASS (count is the batch, not the page) |
| Connection status | *Connected* | see A0-3 |
| Step rail | *STEP 1 OF 3*, first segment filled | PASS |
| Source URL line | present, beneath the strip | PASS |
| Primary action | *Review selected (47)* | PASS |
| Secondary action | *Read this page again* | indicates a batch was already present |
| Paging disclaimer | *"You control paging — VM Prospector never turns a page."* | PASS |
| **Campaign control anywhere in the panel** | **none** | **PASS — this is the evidence replacing #131 S4** |

Three things in that state need resolving before A1–A4 can be trusted:

* **A0-1 — every row is badged *Needs review*: 47 of 47.** The select-all line
  reads *47 need review*, and each visible row carries the amber badge. The
  badge means "this record has at least one warning". A flag that fires on
  100% of rows conveys nothing, so either the rows genuinely all carry a
  warning — plausible if, for example, DAT-018's observed-versus-derived profile
  URL leaves a code on most rows — or something is over-flagging. Not yet
  classified; diagnosis below.
* **A0-2 — *"4 rows currently visible on this page"* sits above a 47-row batch.**
  Literally true: the detect status reports rows currently rendered in the DOM,
  and Sales Navigator virtualizes the list, so after a completed pass returns to
  the top only a few rows remain mounted. Read next to *47 found* it invites the
  operator to think 43 rows were lost. Presentation, not data.
* **A0-3 — the batch's provenance is unknown to this record.** The panel restores
  a draft batch and a last result from `chrome.storage.local`, so a 47-row batch
  on arrival may be from a pass just run or from an earlier session. *Connected*
  rather than *Ready* points at a restored prior result, since that state is set
  when a submission succeeds. A1–A3 measure a read pass, so they need a pass
  this record watched from the start.

A0 passes on what it set out to check. A1 restarts from a cleared batch so the
pass under test is unambiguous.

#### A1 result — the read pass (2026-07-27)

Batch cleared first, so this pass is the one measured. Observed **[operator]**:

| Observation | Value |
| --- | --- |
| Progress card | *Reading this page*, with a bar and a live row count |
| Live count | **15 → 21 → 24** across the pass |
| Copy under the count | *"Stopping keeps every one of them."* |
| Stop control | *Stop reading this page*, present for the whole pass |
| Termination | stopped on its own |
| Final batch | **24 found**, *24 rows currently visible on this page* |
| Page advance / new tab / URL change | none |

**A0-2 does not reproduce here.** With the batch built by a pass just run, the
detect line (*24 rows currently visible*) agrees with the badge (*24 found*).
The earlier 4-versus-47 reading came from a restored batch whose rows were no
longer mounted in the virtualized list. Situational presentation, not a data
fault — recorded, not filed.

**A0-1 is explained, and it is a defect.** See D-2.

#### A4 result — the reviewed set (2026-07-27)

Observed on the step-2 screen **[operator]**, batch accumulated across two
operator-navigated pages:

| Element | Value |
| --- | --- |
| Step rail | *STEP 2 OF 3* |
| Eyebrow | *SAVE TO VMR* |
| Selected / deselected | **31** / **0** |
| Missing fields / uncertain id / selector fails | **0** / **0** / **0** |
| Pages | **2** |
| Distinct companies across rows | ≥2 — **S1 PASS** |
| Skipped block | **absent** — no row on either page lacked a company |
| Per-row assurance line | *"Will be saved and flagged for review. Nothing is guessed."* |
| Per-row links | *profile* and *lead* both present |
| Will be submitted | **31 prospects** |
| Labels & note card | present, with *"Labels classify permanent contacts — they are not campaigns."* |
| Primary action | *Capture 31 prospects* |

**Paging was operator-driven.** The source URL moved from `page=4` to `page=5`
between passes because the operator navigated there; the panel advanced nothing
by itself, which is what the *2 pages* tile records.

**S3b could not be exercised on this data.** Neither page contained a row
without a company, so the skipped-row path produced nothing to observe. Recorded
as not exercised rather than as a pass — DAT-018's Layer 3C S8 makes the same
point about needing to find or construct such a search.

#### A8 result — the save, and the zero-contacts guarantee (2026-07-27)

Observed on the outcome screen **[operator]**:

| Element | Value |
| --- | --- |
| Step rail | **DONE** |
| Headline | *30 of 30 prospects saved* |
| Body | *Saved to the VM Prospector workflow.* |
| *What happened* | a single line: **30** *staged as a new person*, badged *Needs review* |
| `created` / *captured as a new contact* | **absent — zero** |
| Actions offered | *Open captured contacts*, *Download JSON*, *Download CSV* |
| Per-record *Open contact* | **absent** |

**This is the reconciled S9, proven live.** Section 2.2 argued from the code
that capture reports `created: 0` and that only promotion constructs a Contact.
The panel now demonstrates it: thirty people submitted, thirty staged, nothing
created. Had a *captured as a new contact* line appeared, the merged contract
would have been contradicted on the spot.

The absence of a per-record *Open contact* button is the same fact from the
other side — the panel offers that link only when the response carries a
`contact_url`, and no contact exists to link to. Only the submission-level
*Open captured contacts* is offered.

**The *Needs review* badge here is correct**, and worth distinguishing from D-2.
`staged_unmatched` genuinely awaits a decision: identity is unresolved and the
capture cannot become a contact until a domain is confirmed. That is a real
review state, unlike the provenance and dedupe codes D-2 is about.

Submitted count (30) equals the selected count (30); the one deselected row was
not submitted.

#### A5 result — deselection (2026-07-27)

One row deselected on the review screen. Every count that describes the set
moved together **[operator]**:

| Element | Before | After |
| --- | --- | --- |
| `SELECTED` tile | 31 | **30** |
| `DESELECTED` tile | 0 | **1** |
| Summary badge | *31 need review* | *30 need review* |
| Primary action | *Capture 31 prospects* | *Capture 30 prospects* |

This is #131 S2, inverted by UI-012 from "exclude" to "deselect" as the
reconciliation predicted. The point of the step is that the number the operator
is about to commit to is the number they actually chose — and the button, the
tiles and the badge all agree.

#### A2 result — operator cancellation (2026-07-27)

Operator-confirmed **[operator]**: *Read this page again* → *Stop reading this
page* stops the pass, returns the scroller to the top, and adds only the rows
read up to that point into the existing batch.

That last part is the one worth stating precisely, because it is the behaviour
the DAT-018 route was built to guarantee and the one most easily lost: a
cancelled pass is **additive and deduplicated**, not destructive. The rows
already held were not discarded and the partial read was merged into them by
stable key. Nothing was submitted by the cancellation.

### B. Person profile

| Step | What to do | What must be true | Result |
| --- | --- | --- | --- |
| B0 | Open a `linkedin.com/in/…` main profile | Strip reads *LinkedIn · Person profile*; panel follows the tab without a Refresh press | **PASS** [operator] — the strip switched to *LinkedIn · Person profile* with the profile URL, unprompted |
| B1 | Review the extracted fields | Fields match the visible page; anything absent is shown as missing, never guessed | **PASS** [operator] — see *B1* below. Missing sections are named, and the panel states *"Empty fields stay empty — VM Prospector will not fill them in."* |
| B2 | Capture → confirm → Save | Outcome is `refreshed_exact_match` or `staged_unmatched`; `created` is 0 | **PASS** [operator, machine-verified] — `submitted 1 / staged_unmatched 1 / created 0`. Promotion then linked it to the **existing** contact rather than making a second one. See *B2* below |
| B3 | Save again without recapturing | *already saved — idempotent*, same submission | **PASS** [operator, machine-verified] — the control relabels itself *Refresh Contact*, the outcome reads **"1 already current — Unchanged"**, and the backend records `exact match unchanged`. Still one contact. See *B3* below |
| B4 | Open a Sales Navigator person page | Reports *unsupported* with the reason — the shipped detector supports only the main `/in/` profile | **PASS** [operator] — *"This Sales Navigator view isn't supported. Open a people-search results page (/sales/search/people)."* The **specific** reason, not the generic one. See *B4* below |

### C. Company profile

| Step | What to do | What must be true | Result |
| --- | --- | --- | --- |
| C0 | Open a `linkedin.com/company/…` page | Strip reads *LinkedIn · Company page* | **PASS** [operator] — strip reads *LinkedIn · Company page*, badge **Ready to capture** |
| C1 | Capture on the About page | Firmographics match; a website shown on the page is reported as *Shown on this page*; if absent, *Domain not confirmed* with nothing invented | **PASS** [operator] — firmographics match the visible page, `Founded — Not shown`, and the website is labelled **Shown on this page**. See *C1* below |
| C2 | Save | Saves company **evidence**, not a contact | **PASS** [operator, machine-verified] — *"Already saved (idempotent)"*, activity `Company page saved · Page evidence kept`, backend outcome `exact_match_unchanged`. No contact created; contact and pending counts unchanged. It also surfaced **D-9** |
| C3 | Confirm nothing overclaims | No company matching/diff presented as working — it is not implemented | **PASS** [operator] — the panel enumerates the only two domain states it can report and explicitly hands matching to the app. See *C3* below |

### D. Failure recovery

| Step | What to do | What must be true | Result |
| --- | --- | --- | --- |
| D1 | Stop the backend, press Save | Clear failure; reviewed draft intact; *Download as file instead* offered | **PASS** [operator, backend-down verified] — *"Connection lost … Nothing was saved, and what you reviewed is still here."*, `code: network_error`, *Try again* and *Back to review*. See *D1* below. **Note:** the offered fallback is *Back to review* plus retry, not a file download — see the note in D1 |
| D2 | Restart the backend, press *Try again* | Succeeds with the **same** submission id | **PASS** [operator, machine-verified] — retry succeeded, indicator returned to *Connected*, step **DONE**, outcome **"1 already current — Unchanged"** |
| D3 | Inspect | No duplicate submission, capture, contact or evidence row | **PASS** [machine, supervised] — the failed attempt left **nothing**; the retry wrote exactly one capture. Contacts 1, pending 78, both unchanged |

### E. Domain-resolution handoff (workbench, DAT-010 + DAT-014)

| Step | What to do | What must be true | Result |
| --- | --- | --- | --- |
| E1 | Open `/contact-captures/pending` | The captured person is listed as pending | **PASS** [machine, supervised] — trial captures present; before promotion the record reads *not promoted*, reason *"run the company-domain lookup first"*, with the promote control disabled and the candidates panel stating *"a domain is never invented for you"* |
| E2 | Run the company lookup | Candidates stored with provider order; confidence recorded as *not provided* rather than invented | **PASS** [operator] — live lookup returned **10 candidates**, reported as *awaiting your decision*; nothing auto-confirmed. See scope note. **Repeated in scope** [machine, supervised] on a trial capture: *Lookup finished: ok · 4 candidate(s) awaiting your decision* |
| E3 | Confirm one candidate | `domain_candidate_confirmed`, source `candidate`, actor and time recorded | **PASS** [operator] — two candidates rejected with the decisions preserved (*"the decision is kept with the candidates"*), one confirmed; outcome reached `domain_candidate_confirmed` and *Promote to contact* became available. See scope note. **Re-established in scope on the restored baseline** — see *E3 after the restart* below |
| E4 | Where practical: type a domain for a second capture | `decision=manual` recorded as an override | **PASS** [machine, supervised] **in scope** — on a trial capture: `confirmed domain utila.io (manual)`, distinct from `(candidate)`. See below |
| E5 | Where practical: leave a third unresolved | `left_unresolved`; nothing promoted | **PASS** [machine, supervised] **in scope** — `left_unresolved`, *"Recorded as deliberately unresolved. Nothing was promoted."*, promote control disabled, and a direct promote POST refused. See below |
| E6 | Promote the confirmed capture | `contact_created`; labels and notes carried; capture linked | **PASS** [machine, supervised] on `main@d99e274`, **twice**: once on a pre-existing capture and once **in scope** on a trial capture via the manual decision. See below |
| E7 | Promote again | `already_promoted`; no second contact | **PASS** [machine, supervised] — *"already promoted"*, same contact id, contact count unchanged. See below |

#### E2 in scope — the candidate record, in detail (2026-07-27)

Run on a trial capture **[machine, supervised]**. What the record held after the
lookup:

| Field | Value |
| --- | --- |
| `lookup` | `ok · 1 attempt(s) · logo.dev (logo.dev/search-brands/v1)` with a timestamp |
| Candidates returned | **4** |
| Rank ordering | preserved as the provider returned it (1…4) |
| **Confidence** | **"not provided by this provider"** on every candidate |
| `confirmed domain` | `—` |
| `resolved company` | `—` |
| `promoted contact` | `not promoted` |
| `why not promoted` | *"several domain candidates are waiting for your confirmation"* |

Three acceptance points land here at once. Confidence is **recorded as absent
rather than invented** — the provider returns no score, and the record says so
instead of manufacturing one. The provider's ordering is preserved as `rank`
without being treated as a decision. And promotion is **refused while candidates
wait**, with the refusal stating its own reason.

The candidate set was genuinely ambiguous: a `.com`, a `.net`, a `-dev.com` and
a different brand entirely. Auto-accepting rank 1 would have been right this
time and wrong on the `-dev` variant — which is the argument for confirmation,
made again on live data.

#### E2 / E3 scope note — exercised on a pre-existing capture

The operator ran the domain path on a capture from the **pre-existing 50**, not
on one of this trial's 30. Recorded honestly rather than quietly folded in:

* It is **outside the trial's scoped rows**, so it contributes no count to the
  Phase 4 figures, which stay measured against this trial's own identifiers.
* It is **stronger evidence for the shipped path than a trial row would have
  been**, because the company was genuinely ambiguous. The provider returned ten
  candidates and the operator rejected two before confirming one — which is the
  live-data version of the argument Layer 4B made for why auto-confirming a
  top-ranked name match is unsafe.
* Both rejections were preserved with their decision rather than discarded, and
  the capture only became promotable after an explicit confirmation.

**E2 is now doubly evidenced**: Layer 4B proved the provider path at the API
level, and this run proves it through the shipped workbench with a key
configured today. What is still owed to the trial's own scope is a promotion of
one of the trial's captures — E6/E7 below.

#### E3 after the restart — the earlier confirmation had committed

Resumed on the restored baseline, **before pressing anything**, per the standing
instruction not to re-confirm blind.

The capture page renders, which settles the restart itself: the missing-table
500 is gone and the environment is `main@d99e274` again. The record reads
`domain_candidate_confirmed` with `confirmed domain wisestamp.com (candidate)`,
and the pending list shows the same for that row.

**So the confirmation committed.** The 303 was doing what it appeared to do; only
the redirected render died with it. *Confirm* was therefore **not** pressed
again — the write already exists, and a second confirm would have been an
idempotency probe (S11's job) wearing E3's label.

E3 is recorded as **re-observed on the restored baseline**, not re-run. The
original run stays quarantined; this observation is what the acceptance rests on.

#### E3 completion — the remaining candidates decided

The confirmation alone does not open the gate: the record still read *"several
domain candidates are waiting for your confirmation"* with three undecided
candidates. Each was rejected in turn **[machine, supervised]**, and the
workbench behaved exactly as the earlier operator run did:

* Every rejection returned *"Rejected `<domain>`. The decision is kept with the
  candidates."*
* Each moved into a **Rejected candidates (kept as decisions)** table carrying
  domain, reason, actor (`workbench`) and timestamp. Nothing was deleted.
* The confirmed candidate was never touched.

This is the same preserve-the-decision behaviour the pre-contamination profile
capture showed, now reproduced in scope on the restored baseline.

#### Scope correction — which submission that capture belonged to

Recorded before the E6 result, because it changes what that result proves.

Reconciling every pending capture against its submission gives this census:

| Submission | Captures | Ingested |
| --- | --- | --- |
| `471bcf00…` | 47 | 2026-07-28 01:30 |
| `01366e2e…` | 30 | 2026-07-28 02:08 |
| three single earlier captures | 3 | 2026-07-25 / 07-27 |

**This trial's thirty are `01366e2e…`** — `submitted 30`, `staged_unmatched 30`,
`created 0`. The `50 → 80` figure recorded earlier is confirmed correct by this
census (3 + 47 = 50 before, + 30 = 80 after), so the Phase 4 delta stands.

But the capture used for E1–E3 and the first promotion belongs to **`471bcf00…`**,
which is part of that pre-existing 50. The E2/E3 scope note already said the
domain path was exercised on a pre-existing capture; **the first E6/E7 run
inherits that same limitation**, and an earlier draft of this section wrongly
called it "the trial's own capture". Corrected here rather than quietly fixed.

The gap is closed below: E4 and E6 were then run **in scope**, on captures from
`01366e2e…`.

#### E6 — promotion (first run, pre-existing capture)

Pressing *Promote to contact* returned **"contact created · company domain
candidate confirmed."**

| Check | Result |
| --- | --- |
| Contact created | exactly one, id recorded |
| Company resolved | canonical company created and linked, domain `wisestamp.com` |
| Capture linked | `matched_contact` now carries the contact id |
| Scoped contact count for that company | **0 → 1** |
| Pending queue | **80 → 79**, exactly −1 |
| Labels / notes | `0 note(s) linked · labels none` — nothing invented |

The capture's own status pill still reads `unmatched_staged`. That is correct,
not stale: the capture is immutable acquisition evidence describing what was true
at submission, and promotion is a **separate later event** rather than a
retroactive edit of the capture. The contact-first architecture is visible in the
data model here, not just in the prose.

**Promotion created identity and nothing else**, which is the claim the panel
makes and the one most worth checking:

| Contact field | Value |
| --- | --- |
| Research | *not requested* |
| Email | `—`, *unverified*, *"No address to verify yet."* |
| Suppression | *not suppressed* |
| Campaign membership | none |
| Scoring | *not assessed* |

Field provenance is recorded per field — source `linkedin-contact-capture`, the
observation timestamp, *"only observation of this field"*, policy `freshness-v1`
— so the contact carries its evidence rather than asserting values.

#### E7 — promoting again

Two findings, and the first is the stronger one.

**The UI removes the affordance.** Once promoted, the entire *Actions* panel is
gone from the capture page — no promote control, no confirm, no reject. The
operator cannot double-promote by clicking, because there is nothing to click.

**The service is idempotent underneath it** [machine, supervised]. Re-issuing the
promote POST directly returned **"already promoted · company domain candidate
confirmed."**, the same contact id, and the scoped contact count stayed at
**1**. The guarantee holds at the layer that matters, not only at the layer the
operator sees.

#### E4 — the manual override, in scope

Run on a capture from **`01366e2e…`**, this trial's own submission
**[machine, supervised]**.

The lookup returned **10 candidates**, and they make the case for confirmation
better than any argument could: alongside three plausible `utila.*` domains sat
a domain registrar, an unrelated software vendor, and a Romanian heavy-machinery
site. Rank 1 happened to be right. Auto-accepting rank order as truth would have
been wrong often enough to matter.

Typing the domain and pressing *Use this domain* produced:

| Field | Value |
| --- | --- |
| Banner | *"Confirmed utila.io. You can promote this capture now."* |
| `confirmed domain` | `utila.io` **(manual)** |
| Company resolution | `domain_candidate_confirmed` |
| Candidates | all 10 left undecided, and superseded by the explicit decision |

**The override is recorded as an override.** `(manual)` is stored and displayed
distinctly from `(candidate)`, which is the whole point of E4: the record
distinguishes a domain the operator typed from one the provider proposed and the
operator picked. Provenance survives the decision.

#### E6 in scope — promotion after a manual decision

The same trial capture then promoted normally: **"contact created"**, one
contact, one canonical company, `0 note(s) linked · labels none`.

| Measure | Before | After |
| --- | --- | --- |
| Pending queue | 79 | **78** |
| Scoped contacts for that company | 0 | **1** |

So E6 now holds on a capture from this trial's own submission, reached through
the manual path rather than the candidate path. The scope gap the E2/E3 note
opened is closed.

*(One wording nit, logged as D-OBS-4: the success flash reads "company domain
candidate confirmed" even when the decision was manual. The record itself is
correct — `(manual)` — so this is the message, not the data.)*

#### E5 — the deliberate non-decision, in scope

Run on a third capture from `01366e2e…` **[machine, supervised]**, with a typed
reason in the *"why leave it unresolved?"* field.

| Check | Result |
| --- | --- |
| Banner | *"Recorded as deliberately unresolved. Nothing was promoted."* |
| Company resolution | `left_unresolved` |
| `confirmed domain` | `— (unresolved)` |
| `why not promoted` | *"the operator left this company deliberately unresolved"* |
| *Promote to contact* | present but **disabled** |
| Pending queue | **unchanged** at 78 |
| Direct promote POST | **refused**, `err=the operator left this company deliberately unresolved` |

Two things worth separating. First, *unresolved is a decision, not an absence* —
it is stored with a source, an actor and a timestamp, and it changes the refusal
reason from "you haven't looked yet" to "you looked and declined". Second, the
refusal is **enforced in the service, not just greyed out in the page**: bypassing
the disabled control still fails, with the same truthful reason. A UI-only gate
would have passed the visual check and failed this one.

This also sharpens D-6. The `UNRESOLVED` branch sets `blocked_reason` explicitly
and its message is correct and current; the `CONFIRMED` branch is the one that
forgets. The bug is a missing line in one branch, not a systemic pattern.

### F. Backend truth (Phase 4)

Counts and sanitized identifiers only.

| Check | Expected | Result |
| --- | --- | --- |
| Raw capture payload after promotion | unchanged except the canonical contact link | **PASS** [machine, supervised] — re-read after promotion: `extraction_status` still `partial`, the status pill still `unmatched_staged`, every person observation identical. The only change is `matched_contact`, which now carries the contact id |
| Contacts created by capture alone | **0** | **PASS** [machine, supervised] — pending queue moved **50 → 80**, exactly +30. All thirty submitted captures are present and all thirty are *awaiting promotion*, i.e. none became a contact |
| Contacts created by promotion | 1 per promoted capture | **PASS** [machine, supervised] — one promotion, one contact; scoped count 0 → 1 and pending 80 → 79. A second promote created none |
| Campaign memberships | **0** | **PASS** [machine, supervised] — the promoted contact carries no campaign membership; the workbench states promotion "never adds a campaign membership" and the record agrees |
| Email candidates / verifications / scores / drafts | **0** | **PASS** [machine, supervised] — email `—` / *unverified* / *"No address to verify yet."*, research *not requested*, scoring *not assessed* |
| Repeated identical submission | one submission, one snapshot, one note | **PASS** [operator save, machine-verified] — the unchanged batch was resaved; the backend replayed it. Pending stayed **78**, the submission kept its `submission_id` and `client_submission_id`, and its counts stayed `submitted 30 / staged_unmatched 30 / created 0`. No second submission, no duplicate captures. See *S11* below |
| Fields with missing evidence | still null, with warnings | **PASS** [machine, supervised] — on a partial capture: `connection_count`, `open_to_work`, `about_text` all `—`, zero experience rows, `warnings 1`, and the table states *"No experience history was visible on the captured surface."* Absent evidence is reported as absent and explained, not filled in |

---

### Scoped delta after the save

Taken immediately after A8 **[machine, supervised]**:

| Figure | Before the trial | After the save | Delta |
| --- | --- | --- | --- |
| Captures awaiting promotion | 50 | **80** | **+30** |

The delta equals the submitted count exactly. Two things follow without needing
a database query: every submitted capture was persisted, and every one of them
is still *pending* — a capture leaves this queue only when it is promoted, so
thirty pending rows is thirty people who did **not** become contacts. That is
the same guarantee A8 showed from the panel side, now confirmed from the
backend's own queue.

The *Domain lookup unavailable* banner remains absent, so D-1 is still closed
and the candidate lookup is available for Phase E.

## 4. Defects

Filed as separate GitHub issues linked to #131. None fixed inside this task
unless a small unambiguous acceptance blocker is documented first.

| Ref | Summary | Severity | Blocker | Issue |
| --- | --- | --- | --- | --- |
| D-1 | `LOGO_DEV_API_KEY` was not configured, so the DAT-010 candidate lookup could not run | environment | **resolved before the trial** — no longer blocking | not filed (config, not a product defect) |
| D-5 | ~~Confirming a domain candidate 500s~~ — **withdrawn as a defect**; reclassified as an **environment-contamination incident**. The repository was switched to the DAT-017A branch while uvicorn ran with `--reload`; the hot-reloaded code queried a table the shared `vmr_dev` database has never had | **environment / trial integrity** | see the incident record | not a product defect; nothing to file against the product |
| D-4 | A Sales Navigator capture's derived profile URL is an opaque member id, so the same person captured from their profile page does not match it **by URL** | **downgraded** — see the correction below | no | still worth filing, but as a **narrow** identity risk, not fragmentation-by-default |
| D-3 | Reported: captured company not visible in the app for a Sales Navigator capture | unresolved | no | **not reproduced** — cause unknown, left open |
| D-OBS-3 | *Person observations* omits captured company and title; profile captures show the employer only via the experience table | presentation | no | not filed |
| D-2 | `derived_value` provenance is rendered as *Needs review*, flagging ~100% of Sales Navigator rows and destroying the signal | **blocker for S3a** | yes, for that step | **filed as #191** — tracked separately; not implemented inside DAT-011 |
| D-6 | After a candidate is confirmed, the capture page keeps showing a stale *"why not promoted"* reason that contradicts the confirmed state | operator truth | no — promotion is gated on the outcome, not the message | **filed as #192** (UI-014) — tracked separately; not implemented inside DAT-011 |
| D-9 | Promotion never sets `contact.company_id`, and nothing else ever does, so every promoted contact lacks the permanent company edge — the Companies list reads *Contacts 0* for companies that have contacts | **backend truth** | no | to be filed |
| D-10 | The Companies **list** reports `Identity: consistent` for a company whose **detail** page reports a conflict — `count_for_companies` omits the `CONTACT_LINK_UNRESOLVED` kind | operator truth | no | to be filed |
| D-8 | On reopening the panel the restored outcome view is overwritten by the first surface detection, so the last result and its *Open captured contacts* link are unreachable | **blocker for S5 (result half) and S6** | yes, for those steps | **filed as #194 (UI-016); fixed on `fix/ui-016-retained-result-restoration`** — harness-verified, live re-run owed. See §8 |
| D-7 | The operator's typed *"why leave it unresolved?"* reason is stored and audited but never displayed back on the capture page | evidence visibility | no — the reason is persisted, not lost | **filed as #193** — tracked separately; not implemented inside DAT-011 |
| D-OBS-6 | The same company is recorded as the vanity company slug from the company page and as the numeric company id from a person capture — the company-level twin of D-4 | latent | no | not filed — companies resolve by domain, so nothing matches on these URLs today |
| D-OBS-5 | A replayed submission still headlines *"30 of 30 prospects saved"*; the truthful *"already received"* wording sits in the body | presentation | no | not filed — fits #192 |
| D-OBS-4 | A manual domain decision flashes *"company domain candidate confirmed"*; the stored record correctly says `(manual)` | presentation | no | not filed — fits #192's area |
| D-OBS-1 | `created` is a declared capture counter no outcome can increment; the panel carries a label for it | observation | no | not yet filed |
| D-OBS-2 | Acceptance and CLAUDE docs still describe the pre-UI-012 panel and the old product name | documentation | no | not yet filed |

---

### D-1 — domain candidate lookup is unavailable in this environment

Observed on `/contact-captures/pending`: *"Domain lookup unavailable — Company-domain
enrichment is disabled or no provider key is configured."*

The banner covers two causes and does not say which. Isolated without firing a
provider call: the Overview page lists `salesnav_domain_enrichment` among the
enabled features, and `lookup_available` is
`_enrichment_enabled() and get_settings().has_logo_dev_key()`. The flag half is
true, so the missing half is the **key**.

Not a product defect — configuration. Its scope is narrow and worth stating
precisely, because it does not block Phase E as a whole:

* **S7 / E2 (provider candidate lookup)** — blocked. Cannot run without a key.
* **E4 (typed-domain override)**, **E5 (left unresolved)**, **E6/E7
  (promotion and its idempotency)** — unaffected. The page says so itself
  ("You can still open a capture and enter a domain by hand"), and
  `confirm_domain` accepts `decision=manual` and `decision=unresolved`
  independently of the provider.

Layer 4B already proved the live provider path end to end against the real
endpoint with a real key, including the ambiguity that motivates operator
confirmation. So E2 is a re-confirmation on the shipped path, not first proof —
which is why this was recorded as an environment blocker on one step rather than
on DAT-011.

**Resolved before the trial ran.** The operator set `LOGO_DEV_API_KEY` in `.env`
and restarted the application. Re-checked on `/contact-captures/pending`
**[machine, supervised]**: the *Domain lookup unavailable* banner is absent, so
`lookup_available` — `_enrichment_enabled() and has_logo_dev_key()` — is now
true. The key itself was never transmitted to or handled by the build session.
E2 is therefore in scope for the trial. Pending count at this moment: **50**,
all pre-existing, none created by this trial.

### D-2 — a provenance annotation is being shown as a warning

**Observed.** Every captured row carries the amber *Needs review* badge — 47 of
47 on the restored batch, 24 of 24 on the fresh pass — and the select-all line
reads *N need review*. Hovering the badge shows the raw string `derived_value`.

**Cause, confirmed in code.** `WARNINGS.DERIVED_VALUE` is DAT-018's provenance
annotation, documented in `constants.js` as *"a value the adapter computed from
another observed value rather than read off the page … so a derivation can never
be mistaken for an observation"*. On a Sales Navigator listing the public `/in/`
URL is usually not on the row, so `extraction.js` derives it from the lead URL
and annotates it. That is correct, intended DAT-018 behaviour and fires on
almost every row.

The panel then treats it as a problem. `recordTone` returns the warning tone for
*any* warning code, and `warnLabel` has no entry for `derived_value`, so the row
is badged *Needs review* and the tooltip falls through to the raw code.

**Why it matters, rather than being cosmetic.** Three separate harms:

1. A flag that fires on ~100% of rows carries no information. The operator
   cannot see which rows *actually* need attention — which is the exact
   guarantee #131 S3 asks to verify.
2. The review step's *N need review* count is inflated by the same amount, so
   the summary an operator saves against is wrong about its own set.
3. The tooltip shows a machine code, not a sentence — the one thing the warning
   presentation was built to avoid.

**Whose defect.** UI-012's. DAT-018 added the code; the UI-012 panel rebuilt the
warning presentation without an entry for it and without distinguishing
provenance from fault. It is not a DAT-018 regression and not a backend fault —
the payload is correct, and a derived URL is still recorded as derived.

**Acceptance impact.** Blocks **S3a** specifically: "retain at least one
incomplete or uncertain record and verify truthful handling" cannot be verified
while every row claims to need review. It does not block S1, S2, S5, S6, S11,
S12 or any backend check.

**Confirmed at the review step, and worse than first recorded.** The step-2
screen shows the panel contradicting itself in a single view:

| Element | Value |
| --- | --- |
| Summary badge | *31 need review* (no *ready* badge at all) |
| `MISSING FIELDS` tile | **0** |
| `UNCERTAIN ID` tile | **0** |
| `SELECTOR FAILS` tile | **0** |
| Per-row codes shown | `derived_value: linkedinProfileUrl`, `duplicate collapsed: stableKey` |

Its own tiles report zero missing fields, zero uncertain identities and zero
selector failures — that is, **no row has a data problem** — while the badge
above them says all 31 need review. An operator reading top-to-bottom is told
two incompatible things about the same set.

**A second code is mis-presented, not just one.** `duplicate_collapsed` is
dedupe bookkeeping: the same person seen twice across passes, merged by stable
key. It appears on these rows precisely *because* A2's cancelled re-read merged
into the existing batch — correct behaviour being reported as a fault. So the
defect is the general one: the panel treats **every** warning code as a problem,
and two of the codes it is most likely to see are routine provenance and routine
deduplication.

**Proposed minimal fix** — not applied, pending Sahil's decision: classify
warning codes into *fault* and *bookkeeping*, render bookkeeping as neutral
provenance markers with their source field, and let `recordTone` return the
warning tone only for genuine faults. `derived_value` and `duplicate_collapsed`
move to bookkeeping; the missing/selector/uncertain codes stay faults. A label
map entry per code and one predicate. No extraction, contract or payload change.

**Revised acceptance impact.** Still S3a only, but the reason is sharper: with
every row flagged, the trial cannot demonstrate truthful handling of a genuinely
incomplete record, because the panel gives an incomplete row and a perfect row
the same badge.

### D-3 (provisional) — a captured company can be invisible on the record page

**Reported by the operator:** a company shown in the extension for a Sales
Navigator capture does not appear in the app.

**Established so far, in code:** `_capture_profile_rows`
(`app/web/routes.py`) builds the *Person observations* block from exactly seven
fields — `full_name`, `headline`, `displayed_location`, `connection_count`,
`open_to_work`, `about_text`, `warnings`. **Neither the captured company nor the
captured title is among them.**

For a profile capture that is survivable: the employer still appears in the
*Experience observations* table. A record opened during this trial
**[machine, supervised]** showed 5 experience observations, so it was a profile
capture and its company was visible there.

The concern is the Sales Navigator shape, where a row is a listing rather than a
profile. The company is certainly *stored* — `/contact-captures/pending` renders
it, via `company_hints`, for all 50 pre-existing captures — so this is a display
question, not data loss. What is not yet established is which page the operator
was looking at and whether a salesnav capture carries an experience row at all.

**Outcome: not reproduced, cause unknown.** On re-checking, the operator reports
the company names are now visible in the app, with no change made to code,
configuration or data in between. That is not a fix and is not recorded as one.

Splitting what is known from what is not, because the two have different
standing:

* **Verified and still true:** the *Person observations* block omits the
  captured company and title. Independently checkable in
  `_capture_profile_rows`. Its practical severity is low — a profile capture's
  employer appears in the *Experience observations* table — so it is downgraded
  from a defect to a presentation gap, carried as **D-OBS-3**.
* **Not verified:** the original symptom. No reproduction, no identified
  surface, no cause. It is not closed and not counted as passing; it is recorded
  as an unexplained observation so that a recurrence is recognised as the second
  sighting rather than the first.

A symptom that disappears without a change is worth less trust, not more. If it
returns, the thing to capture is the exact URL and the capture's mode, which is
what would have settled it the first time.

### D-4 — the member-id question Layer 3C left open is now answered

`LINKEDIN_CAPTURE_ACCEPTANCE.md` Layer 3C closes with a "known item for review,
not a step": if a Sales Navigator member id is an opaque URN rather than a
vanity handle, the derived `/in/<member-id>` URL will not be string-equal to a
vanity URL already stored for the same person, and the backend matches profile
URLs exactly. It asked for one observation to settle whether a follow-up is
needed.

**The observation, taken from a trial capture** **[machine, supervised]**:

| Field | Shape observed |
| --- | --- |
| `capture_mode` | `salesnav_people_search` |
| `source_surface` | `salesnav_people_results` |
| `normalized_profile_url` | `/in/` + a **40-character opaque alphanumeric member id** |
| `salesnav_lead_url` | `/sales/lead/` + the same identifier in mixed case |
| `extraction_status` | `partial` |
| Reconciliation outcome | `unmatched_staged` |

It is an opaque id, not a vanity handle. So the follow-up Layer 3C anticipated
**is** needed, and the consequence is concrete: the same person captured from a
Sales Navigator listing and from their own profile page produces two different
`/in/` URLs, which the backend will not match to each other. Two captures, two
staged identities, eventually two contacts.

**This is a fragmentation risk, not a correctness failure**, and the distinction
matters. Nothing is invented and nothing is wrongly merged — the system is
behaving exactly as the "only an exact normalized LinkedIn profile URL may
auto-match" rule requires, and staging as `unmatched_staged` is the safe branch.
The cost is landing on the safe side of a decision the system cannot make.

**Out of scope for DAT-011.** It changes no result in this trial: idempotency
(S11) is about resubmitting the *same* capture, which still replays correctly.
It needs its own issue, because the fix is an identity question — whether the
member id should be resolved to a vanity handle at capture time, or carried as a
second matchable key — and that is a backend design decision, not an acceptance
step.

### D-6 — a confirmed capture still says candidates are waiting

**Observed** [machine, supervised] on `main@d99e274`. After confirming a
candidate, the capture record read:

| Field | Value |
| --- | --- |
| Company resolution | `domain_candidate_confirmed` |
| `confirmed domain` | `wisestamp.com (candidate)` |
| `why not promoted` | *"several domain candidates are waiting for your confirmation"* |

The last line contradicts the two above it. It tells the operator to do
something already done, and it survived every subsequent rejection — still
reading *"several"* when one candidate remained.

**Cause, by inspection.** In `promotion.resolve_company`, the branch that handles
an already-confirmed record sets `company_outcome` and `resolved_domain` and
returns — without clearing `promotion.blocked_reason`. The stale value from the
last unconfirmed evaluation is what the page then renders. The neighbouring
`PRIOR_MAPPING` branch **does** clear it explicitly, which is the strongest
argument that this is an oversight rather than a decision.

**Not a blocker.** Promotion is gated on `company_outcome in
_RESOLVED_COMPANY_OUTCOMES`, not on `blocked_reason`, and E6 promoted normally
with the stale message on screen. The message cleared itself once the promotion
row was rewritten.

**Why it still matters.** This document's own standard is that the system states
its reasons truthfully. A refusal reason that outlives its refusal is the same
class of fault as an invented confidence score — the operator is being told
something the system no longer believes. An operator who trusted it would go
looking for candidates that are not there.

### A9 — the draft survives a panel close, and the proof is arithmetic

**Observed** [operator]: the panel was closed and reopened with no other action.

It returned holding **31 rows, 30 selected, 1 excluded** — the operator's original
selection intact — while the same panel reported *"4 rows currently visible on
this page."*

That pair of numbers is the whole proof. The live page had four rows rendered; the
batch held thirty-one. A fresh read of the page could not have produced 31, so the
batch was restored from storage rather than reconstructed from the DOM. No
recapture, no resave.

**The batch identity was verified from the backend, not assumed.** This trial's
thirty (`01366e2e…`) were captured from **page 4** of the search; the pre-existing
forty-seven (`471bcf00…`) came from page 3. The restored 31/30 batch is the trial
draft, not a coincidental fresh capture of a similar page.

**What did not restore: the last result.** The panel opened on step 1 of 3 with
*Review selected*, not on the outcome view with its returned link. By inspection
`lastResult` lives in `chrome.storage.local` and is cleared only by *Clear batch*
— and the batch plainly survived, so the two should have restored together.
Rather than guess at the cause, the result path was retested under controlled
conditions (S11 below, which rewrites `lastResult`), and A9 re-run after it.

**Controlled re-run: the same thing happened.** Immediately after a successful
save — so `lastResult` was certainly written seconds earlier — closing and
reopening the panel again returned it to step 1 with the draft. Not a stale-state
artefact, not an eviction: **deterministic**. Diagnosed as D-8 below.

### S11 — resubmitting the unchanged batch

**Observed** [operator save, machine-verified]. The retained batch still carried
its original submission identity, so saving it again is precisely the
"unchanged input" case — the test and the recovery of `lastResult` in one action.

The panel reported:

> **30 of 30 prospects saved** — *"This submission had already been received — it
> was replayed, not duplicated."*
> **What happened:** 30 staged as a new person · Needs review

The backend agreed, checked independently:

| Measure | Before | After |
| --- | --- | --- |
| Pending queue | 78 | **78** |
| Captures in the trial submission | 30 | **30** |
| `submission_id` / `client_submission_id` | unchanged | **unchanged** |
| Outcome counts | `submitted 30 / staged_unmatched 30 / created 0` | **identical** |

**No second submission, no duplicated captures, and the extension said so
plainly.** This is the guarantee that makes the acquisition path safe to retry:
an operator who is unsure whether a save landed can press it again without
consequence, and the system tells them which of the two happened rather than
silently doing either.

*(Observation, D-OBS-5: the headline still reads "30 of 30 prospects saved" on a
replay — the truthful "already received" wording sits in the body beneath it.
For a multi-row submission the headline alone would read as a fresh save. Minor,
and the same family as D-6 / D-OBS-4: the data is right, one line of copy is
generic.)*

### B1 — the person profile reports what it could not read

**Observed** [operator]. Captured on a profile deliberately chosen because the
same person was already in this trial's Sales Navigator batch.

Before saving, the panel showed a preview headed *"Preview ready — review what
could not be read"*, and a panel titled **"Some details could not be read"**
listing `Current company`, `Location`, `Experience — 4 entries`, and
`About — Section missing`. Its guidance was to scroll so the missing sections
load and read the page again, followed by the sentence that matters most here:

> **"Empty fields stay empty — VM Prospector will not fill them in."**

The review step then showed `About — Not captured`, `Website — Not confirmed`,
and `Page evidence — Partial`.

**The parsing quality point worth recording.** This profile's *headline* is a
joke — "Protecting stuff". The extension did not use it as the title. It read
`Co-Founder & CEO` from the experience section instead. A capture tool that
treated the headline as the job title would have written nonsense into a
permanent record; this one went to the structured source.

**The "Needs review" badge is earned here**, unlike the Sales Navigator case in
D-2: the About section really was missing and the page evidence really was
partial. Same badge, opposite information value — which is precisely why D-2
matters.

### B2 — capturing someone the trial had already captured

This is the most informative single step in the trial, because it corrects a
defect this document had already filed.

**The two captures of the same person, side by side:**

| | Sales Navigator capture | Profile capture |
| --- | --- | --- |
| `capture_mode` | `salesnav_people_search` | `linkedin_profile` |
| `normalized_profile_url` | `/in/<opaque member id>` — **derived** | `/in/<public handle>` — **observed** |
| Ingest outcome | `staged_unmatched` | `staged_unmatched` |

At submission the panel reported **"1 staged as a new person"**, and the
submission recorded `submitted 1 / staged_unmatched 1 / created 0`. On the face
of it, D-4's fragmentation.

**Then promotion linked it to the existing contact** —
`contact exact match linked`, `matched_contact` = the same contact id the Sales
Navigator capture had produced. Scoped contact count for that company: still
**1**, not 2.

**Why, from the code.** Promotion resolves identity on **two keys in order**:

1. the exact normalized LinkedIn URL — which failed, exactly as D-4 predicted;
2. failing that, a deterministic **natural key** of first name + last name +
   **confirmed company domain** — which matched, because both captures had been
   resolved to `utila.io`.

The contact's `linkedin_url` then updated from the opaque id to the real handle
through the provenance and freshness policy — the observed value displacing the
derived one. And the panel, on returning to the profile, now reads **"Already
saved"** with *"Capturing again records what the page shows today. It never
overwrites what's already there."*

**This forces a correction to D-4** — recorded in the defect section rather than
left as filed.

### B3 — the second capture of the same page changes nothing

**Observed** [operator, machine-verified], and it completes the story B2 started.

The panel had already relabelled its primary control from *Save Contact* to
**Refresh Contact** — the affordance describes what the action will now do rather
than repeating the first-time wording. Pressing it returned:

> **Prospect saved** — *What happened:* **1 already current** · **Unchanged**

with *Open contact* now offered alongside *Open capture record*, because a
canonical contact exists to open.

The backend agrees. The contact's evidence list now carries **two** captures of
the same profile:

| Ingested | Outcome |
| --- | --- |
| 04:05 | `unmatched staged` |
| 04:15 | `exact match unchanged` |

Contact count for that company: still **1**.

**Three claims land together here.** The exact-URL match now succeeds at ingest,
because promotion had written the observed handle onto the contact — so the
system learned the person's real identity from the second capture and used it on
the third. Nothing was overwritten silently: field provenance shows
`linkedin_url`, `title` and `company_name` sourced from `linkedin-profile-capture`
at 04:15 with the stated reason *"most recent evidence"* under policy
`freshness-v1`. And both captures survive independently — **evidence accumulates
while identity does not multiply**, which is the contact-first architecture doing
exactly what it claims.

### C1 — firmographics, and the one field that matters most

**Observed** [operator] on a company page, first on the landing tab and then on
*About*; the panel followed the URL change and re-read without being asked.

| Field | Reported |
| --- | --- |
| Industry | Software Development |
| Company size | 51-200 employees |
| Displayed employees | *"95 associated members — LinkedIn members who've listed Utila as their current workplace on their profile."* |
| Headquarters | New York City, New York |
| Founded | **Not shown** |
| Company page | the LinkedIn URL |
| **Website** | `https://utila.io/` — **● Shown on this page** |

Each matches the visible page. `Founded` is **Not shown** rather than blank or
guessed.

**The "displayed employees" wording deserves its own note.** LinkedIn's member
count is not a headcount, and the panel refuses to let it read as one: it reports
the number *and* what the number actually counts. The easy version of this field
would have said "95 employees" and been quietly wrong on every company.

**The website line is the load-bearing one.** This trial spent Phase E
establishing that a domain is confirmed by an operator and never invented,
because LinkedIn does not reliably give you one. A company page that *does* show
a website is the exact place where a capture tool would be tempted to assert a
domain. This one reports it as an observation with its provenance stated —
*shown on this page* — and stops there. The value it read, `utila.io`, happens to
be the domain confirmed manually in E4, which is a pleasing cross-check but not
the point; the point is that the panel labelled it as *seen*, not as *true*.

### D1 — failing well

**Observed** [operator], with the backend confirmed stopped independently
(`Failed to fetch` against the workbench from a separate tab).

| Element | What it said |
| --- | --- |
| Connection indicator | **Not connected**, red |
| Step indicator | **FAILED** |
| Headline | **Connection lost** |
| Body | *"VM Prospector didn't answer. **Nothing was saved**, and what you reviewed is still here."* |
| Details | *"Could not reach the backend. Is it running on the configured loopback port?"* · `code: network_error` |
| Footnote | *"Retrying is safe — the same submission is replayed, never duplicated."* |
| Controls | **Try again** · **Back to review** |

Every part of the criterion lands. The failure is **specific** rather than
generic — it names the cause, gives a machine-readable code, and asks the one
diagnostic question that points at the actual fix. It makes the **negative claim
explicitly**: *nothing was saved*, which is the sentence an operator needs when
the alternative is wondering whether a half-write happened. The **draft
survives**, and *Back to review* is a route to it rather than a dead end.

**The footnote is the best line in the product.** At the exact moment an operator
is deciding whether pressing a button again is dangerous, the panel tells them it
is not, and says why — *the same submission is replayed, never duplicated*. And
this trial has independently **proved that claim true** in S11: an unchanged
resubmission produced no second submission, no duplicate captures, and no change
in any count. The reassurance is backed by demonstrated behaviour rather than
asserted.

**Two honest notes against the criterion as written.**

*Download as file instead* was **not** offered on this failure. The step's
acceptance text expects it. Retry plus a retained draft is arguably the better
answer for a transient network failure — a file export is a recovery route for
when the backend is not coming back — but the criterion says what it says, so
this is recorded as a difference rather than silently accepted. Whether the
export belongs on this path is a product question for the issue, not something
this trial should decide.

*The connection indicator corrected itself.* Before the save it still read
**Connected** while the backend was already down, which I had noted as a possible
overstatement. It flipped to **Not connected** the moment a save ran. That is
exactly what `shell.js` documents — the indicator reports "how the last save
ended", not live connectivity, because the panel does not poll. So the earlier
reading was stale-until-next-save by design, and the design corrected it at the
first real interaction. Recorded as resolved, not as a defect.

### D2 / D3 — recovering, and leaving no trace of the failure

**Environment stated as fact, not inferred.** The backend was restarted from
`d99e274` (`git rev-parse --short HEAD` → `d99e274`, `git status --porcelain`
empty) with **`uvicorn app.main:app --port 8000`** — no `--reload` — and
`GET /ready` returned `200 OK`. After the contamination incident, the commit
under test is recorded from the machine rather than assumed.

**D2.** Pressing *Try again* on the failed submission succeeded. The connection
indicator returned to **Connected**, the step indicator to **DONE**, and the
outcome read **"1 already current — Unchanged"** with *Open contact* offered.

**D3.** The contact's evidence list tells the whole story in three rows:

| Ingested | Outcome |
| --- | --- |
| 04:05 | `unmatched staged` |
| 04:15 | `exact match unchanged` |
| 04:51 | `exact match unchanged` (the retry) |

**The failed attempt appears nowhere.** It never reached the backend, so it wrote
nothing — no partial submission, no orphan capture, no half-linked contact. The
retry wrote exactly one capture. Contacts for the company **1**, pending queue
**78**: both unchanged.

That is the pair of properties recovery needs and rarely gets together: the
failure left no debris, and the retry was not punished for the failure having
happened. The panel promised *"retrying is safe — the same submission is
replayed, never duplicated"* at the moment of failure; the retry then behaved
exactly as promised, and the record shows it.

**Phase D is complete: D1–D3 all pass.**

### C2 — company evidence saves, idempotently, and creates no contact

**Observed** [operator, machine-verified]. The company had been captured once
before, so this save exercised the repeat path:

> **Already saved (idempotent)** — *Utila · saved to the VM Prospector workflow.*
> **Activity:** Company page saved · **Done** — Page evidence kept · **Done** —
> Domain `https://utila.io/` — Backend outcome **`exact_match_unchanged`**

Checked independently: contacts for that company **1 → 1**, pending queue
**78 → 78**. Company evidence, not a contact — as the control's own name promised.

Reporting the raw backend outcome (`exact_match_unchanged`) in the activity list
is a small thing worth noticing: the panel shows the vocabulary the backend
actually used rather than a paraphrase, so an operator reading the workbench and
an operator reading the panel are reading the same word.

**This step is also how D-9 surfaced** — the company list it sent me to check
said something that could not be true.

### C3 — the panel states the limits of what it can know

**Observed** [operator] at the confirm step, which is headed *"Is this the right
company?"* and ends with a titled block:

> **DOMAIN STATES THIS PANEL CAN REPORT**
> Shown on this page → **Confirmed**
> Not shown → **Missing**
>
> *"Whether the domain matches a company VM Prospector already knows is decided
> in the app, not here."*

**This is the requirement met exactly.** UI-012's brief said anything the backend
cannot support must be "omitted cleanly or presented only as a truthful
non-interactive state". Company matching and diffing are not implemented — so
rather than showing a disabled *Match* button or a hopeful "we'll link this up"
message, the panel enumerates its own two possible outputs and names the system
that owns the decision it cannot make.

Two more restraints in the same view: *"Open the company page yourself — the
panel never navigates there for you"*, which restates the no-automated-navigation
guarantee at the point where an operator might expect convenience; and the save
control named **"Save company evidence"** rather than "Save company", with the
footnote *"This saves company evidence, not a contact."*

*(Observation, D-OBS-6: this capture recorded the company as
`linkedin.com/company/utila-io` while the person capture recorded the same
company as `linkedin.com/company/91037437` — the vanity slug versus the numeric
id, the company-level twin of D-4. Latent rather than active: companies are
resolved by **domain**, so nothing matches on these URLs today. It would become
real the moment anything did.)*

### B4 — declining a page it could easily have parsed

**Observed** [operator] on a Sales Navigator **lead** page for the same person.

The strip read *"This page isn't supported"*, the body *"Nothing to capture here
— Open one of these and the panel switches by itself"*, followed by the three
supported surfaces with their URL shapes, and then the reason:

> *"This Sales Navigator view isn't supported. Open a people-search results page
> (/sales/search/people)."*

**That is the specific reason, not the fallback.** The detector distinguishes
"a Sales Navigator view I don't handle" from "not a LinkedIn page at all", and
the panel prints the one that actually applies. Guidance that names the right
next URL is the difference between a dead end and a redirect.

**The restraint is the finding.** That lead page was dense with exactly the data
this product wants — current role with dates, contact information, a timeline,
mutual connections. It would have been easy to parse and it is not in the
declared scope, so the extension took none of it. A capture tool that quietly
widened its own surface coverage would be a governance problem long before it
was a bug; this one stops at the boundary and says where the boundary is.

**Phase B is complete: B0–B4 all pass.**

### A10 / S6 — the panel's own link opens the exact record

**Observed** [operator], verified against identifiers already recorded here.

*Open captured contacts* opened `127.0.0.1:8000/contact-captures/submissions/01366e2e-8e5b-440b-81a8-e44353f2d49c`
— the submission this trial has been measuring throughout, not a list or a
search. The page carried `client_submission_id 77ae7ae0…`, `schema_version
linkedin-contact-capture/2.0.0`, `extension_version 2.1.0`, `contacts 30`,
`created 0`. Every one of those matches what was independently read from the
backend before the click.

So S6 holds under its renamed shape: the response's `operator_workbench_url` is a
loopback URL under a known prefix, and following it lands on the exact record.

**Two things worth keeping from this screen.** The panel, now sitting on a
workbench tab, reported *"This page isn't supported — Nothing to capture here"*
and then listed what it does support. It neither pretended to work nor went
blank; the unsupported state is a real, informative state. And **S6 is only
reachable immediately after a save** — see D-8 — so this passed on the second
save of the session rather than by reopening the panel.

### D-4, corrected — fragmentation is caught by a second key

**I filed D-4 as identity fragmentation. Tested live, it is narrower than that,
and the correction belongs on the record more than the original claim did.**

The URL half of D-4 is confirmed: a Sales Navigator capture stores the derived
opaque member id, a profile capture stores the real handle, and the two do not
match as URLs. What the original filing missed is that **URL matching is not the
only key**. Promotion falls back to a deterministic natural key — first name,
last name, confirmed company domain — and that key linked the two captures of the
same person into one contact. The observed handle then displaced the derived id
on the contact record.

**So the acquisition path does not silently duplicate people.** That is the
claim that matters for DAT-011, and it holds.

**What survives of D-4, stated precisely.** The natural key carries the whole
burden whenever the URL differs, so fragmentation returns wherever that key is
weak or unavailable:

* **The domain differs or is missing.** Two captures resolved to different
  domains — or one left deliberately unresolved (E5) — cannot share a natural
  key. The unresolved capture is never promoted, so it cannot merge.
* **The name does not normalize identically.** This trial's own batch contains
  people recorded as *"… MBA, CPA, ACA, ACMA, CGMA"* and *"…, PD (Finance), MBA,
  CMA"*, and others with initials for surnames. Credential suffixes, honorifics
  and abbreviated surnames all move the natural key.
* **The ingest report still says "new person."** The capture outcome is computed
  at submission on the URL alone, so the operator is told *"1 staged as a new
  person"* about someone the system will shortly recognise. Truthful about ingest,
  misleading about the person.

**Recommended reframing for the issue:** not *"Sales Navigator captures fragment
identity"* but *"the derived member-id URL is not a usable identity key, leaving
the natural key as the only defence — which fails on name variants and on
unresolved domains."* Narrower, and actionable: resolve the member id to the
public handle at capture time, or carry it as a second matchable key.

### D-9 — promotion never writes the permanent company edge

**Observed** [machine, supervised] on the Companies list, which showed both
companies created by this trial as:

| Company | Canonical domain | Contacts | Identity |
| --- | --- | --- | --- |
| Utila | utila.io | **0** | consistent |
| WiseStamp | wisestamp.com | **0** | consistent |

Each of those companies has exactly one promoted contact. The count is wrong and
the identity verdict is wrong with it.

**Cause, traced to a single missing assignment.** `capture_promotion` resolves
the company row, stores `company.id` on the promotion row and returns it in the
result detail — and then constructs the Contact with `company_name` and
`company_domain` only. `contact.company_id` is never set, on either the created
or the matched path.

**There is no later step that sets it.** `contacts.company_id` is written in
exactly one place in the entire system: the one-time backfill inside the APP-003
migration. No service, route or job assigns it. So every contact created after
that migration ran carries a NULL edge permanently.

**Why this is more than a wrong number.** APP-003 introduced `company_id`
precisely because `company_domain` is a mutable string, and its own model comment
says correcting a company's domain would otherwise "silently re-parent everyone
under it, with nothing recording that it had happened". Contacts produced by the
acquisition path are exactly the rows still exposed to that, because they are
linked by the transitional domain match alone.

**The design anticipated this state.** `companies/records.py` deliberately counts
"by the permanent edge only", and documents that domain-only contacts are "real
and they are reported — as a conflict". That is true on the company **detail**
page, which correctly showed *"1 conflict — 1 identity disagreement(s)"* and
*"Contacts 0 linked to this company record … 1 contact(s)"*. The mechanism works.
What is missing is anything that ever resolves the conflict, because promotion —
the only producer of these contacts — has the company id in hand and does not
write it.

**Severity.** Not a blocker for DAT-011's central claim: the contacts are
correct, identity resolution is correct, and nothing became outreach-eligible.
But it is Phase F territory — backend truth, wrong in the operator's main company
view — and it will affect every contact the acquisition path ever creates.

### D-10 — the company list contradicts the company page

**Observed** [machine, supervised] alongside D-9, and separable from it.

The company **detail** page reports the unresolved link as a conflict. The
company **list** shows `Identity: consistent` for the same company in the same
moment.

**Cause.** `conflicts.count_for_companies`, which feeds the list, computes three
conflict kinds — missing domain, linked contacts whose captured domain disagrees,
and snapshot-domain disagreement. It omits `CONTACT_LINK_UNRESOLVED`, the kind
that `conflicts.py` itself defines and the detail page reports.

**Why it matters independently of D-9.** The list is the surface an operator
scans to decide where to look. A conflict that appears only once you already
suspected something is a conflict the list has taught you to skip. Fixing D-9
would hide this by removing the affected rows; the omission would remain for any
other contact that arrives domain-linked — a spreadsheet import, for instance.

### D-8 — the restored outcome is painted, then immediately overwritten

**The most substantive defect this trial has found, and it is mine.** It was
introduced by UI-012, and the reconciliation in §1 asserted the opposite.

**Observed** [operator], twice, the second time seconds after a successful save:
reopening the panel shows the retained draft at step 1 of 3 rather than the
saved outcome and its *Open captured contacts* link.

**The result is not lost.** `sidepanel.js` reads it and renders it — a harness
reproduction confirms the outcome content is present in the DOM after init. The
panel then switches the body away from it.

**Cause, demonstrated rather than argued.** Two controllers initialise
independently:

1. `sidepanel.js` restores `lastResult`, calls `renderSaveResult`, which ends in
   `showView("outcome")`.
2. `sidepanel-profile.js` runs its first `paintMode()` and switches the body to
   the detected surface's default view.

Step 2 is supposed to be prevented by the sticky-view guard —
`if (changed || !panel.isSticky())` — whose stated purpose is that
"page re-detection repaints the strip underneath them but must not yank the body
away". But `changed` is computed as `mode !== currentMode` with `currentMode`
initialised to `null`, so on a **cold panel open it is always true**. The guard
protects a running panel and cannot protect a starting one: "no previous mode" is
indistinguishable from "the page changed".

Reproduced in the existing panel harness — stored `lastResult` plus a Sales
Navigator detection — with the result:

```
view after init:        listings-select
outcome text present:   true
```

The outcome was rendered and then replaced. Exactly what the operator saw.

**Impact.** S5's result half fails, and **S6 becomes unreachable after a
reopen**: the panel's own returned link is the sanctioned route to the exact
submission record, and it exists only on the outcome view. An operator who closes
the panel must save again to get the link back — safe, because S11 proves the
resave is idempotent, but it is a resave performed to recover a link, which is
not a workflow anyone would design.

**Not a data defect.** The submission, the captures and the workbench record are
all intact; `lastResult` is intact in storage. This is a view-arbitration bug at
startup.

**Scope.** Not fixed here — DAT-011 is acceptance, not development. It belongs
with the UI-012 panel work, and the fix is small: distinguish "first detection
after open" from "the page changed", or have the detection repaint refuse to
leave a sticky view regardless of `changed`.

**Resolved under UI-016 / #194** — the first of those two options, plus a second
cause this diagnosis had not reached. See §8.

### D-7 — the unresolved reason is asked for, kept, and never shown

**Observed** [machine, supervised]. The *Leave unresolved* action offers a
*"why leave it unresolved?"* field. A reason typed there does not appear anywhere
on the capture page afterwards.

**It is not lost.** `enrichment.confirm_record` assigns `record.note` and writes
an audit event carrying the decision, so the reason is persisted and traceable.

**It is not rendered.** The `note` row in `_capture_resolution.html` renders
`resolution.notes[-1].note_text` — the *capture's* operator notes, a different
collection. `record.note` is read by no template.

**Why it matters.** The unresolved decision is the one place in this workflow
where the system deliberately stops and the operator's justification is the only
evidence of why. Asking for that justification and then never showing it back
means a second operator sees a dead end with no explanation, and the first
operator cannot check what they wrote. The fix is a display line, not a schema
change — the data is already there.

### Environment-contamination incident — the code under test changed mid-trial

**This is the most consequential finding of the session, and it is about the
environment, not the product.** It is recorded as an incident, not as a defect:
nothing here is evidence about `d99e274`, and no acceptance step may be judged
from it in either direction.

**Cause, as established.** The repository working tree was switched from `main`
to the DAT-017A branch while uvicorn was running with `--reload`. WatchFiles
picked the change up and hot-reloaded the server onto DAT-017A code. The shared
`vmr_dev` database had never had migration `d7a3f18c62b4` applied, so the
reloaded page queried a `company_domain_resolutions` table that does not exist.
Code from one revision, schema from another — one process, two baselines.

The application console shows, between the E2 lookup and the E3 confirm:

```
WatchFiles detected changes in 'app\models\company_domain_resolution.py',
'migrations\versions\d7a3f18c62b4_dat_017a_company_domain_resolution.py',
'app\services\resolution\{service,store,policy,gates}.py',
'app\web\routes.py', 'app\services\captures\promotion.py',
'app\core\features.py', 'app\models\enums.py' … Reloading...
```

Section 2.5 of this document records that DAT-017A is *not merged* and that no
automatic resolution workflow may be described as current. From the reload
onward, the running application was no longer `d99e274`.

The 500 follows directly: `contact_capture_page` now calls
`resolution_service.capture_view`, which queries `company_domain_resolutions` —
a table created by the DAT-017A migration, which this database has never had
applied.

#### What still stands, and what does not

The WatchFiles line in the console is the boundary. Everything the console shows
*before* it was served by `main@d99e274` and is **retained in full** — it is not
re-run, not re-litigated, and not discarded. Everything after it is invalid for
DAT-011 until reproduced on `main@d99e274`.

| Evidence | Status |
| --- | --- |
| A0 – A3 — detection, bounded scroll, cancellation and resume, review screen | **Valid** (pre-reload) |
| A4 (S1), A5 (S2) — selection and the committed count matching the chosen count | **Valid** (pre-reload) |
| A8 (S9/S10) — submission outcomes, `created` = 0, contact-first evidence store | **Valid** (pre-reload) |
| Pending queue 50 → 80, scoped to this trial's identifiers | **Valid** (pre-reload) |
| E1 — capture reaches the domain-decision surface | **Valid** (pre-reload) |
| E2 — candidate lookup returns a ranked candidate set | **Valid** (pre-reload) |
| **The profile capture's full domain pass** — lookup, **confirmation**, **two candidate rejections**, `domain_candidate_confirmed`, and **promotion gating** opening as a result | **Valid** (pre-reload). This is the strongest evidence in the trial that the domain-decision and promotion-gate path works on the baseline, and it is explicitly retained. |
| D-4 — opaque Sales Navigator member-id fragmentation | **Valid** (pre-reload), and unrelated to the contamination — see below |
| E3 onward on the salesnav capture | **Invalid for DAT-011** until reproduced on `main@d99e274` |

The last uncontaminated checkpoint is therefore **E2 complete, with the profile
capture's domain pass and promotion gating already demonstrated**. The trial
resumes there. It is not restarted.

#### E3 partially succeeded, which the error message hides

The console is unambiguous about the order:

```
POST /contact-captures/<id>/company/confirm  ->  303 See Other
GET  /contact-captures/<id>?ok=Confirmed+wisestamp.com. ... ->  500
```

**The confirmation itself worked.** The domain was accepted and the redirect
carried the success message. What failed is the *page render afterwards*, in
DAT-017A's decision view. An operator seeing "Internal Server Error" would
reasonably conclude the confirmation failed; it did not.

That distinction matters for the record: E3's behaviour was not observed to be
broken. It simply cannot be *claimed* from this run, because the process that
served it was not the process under acceptance.

#### What this costs, and what it does not

It costs the second half of the trial, which must be re-run. It does not
invalidate the first half, and it does not implicate the merged product —
nothing here suggests a fault in `d99e274`.

It also demonstrates something worth keeping: an acceptance run against a
working tree that can change under it is not reproducible. A future trial should
run from a clean checkout of the exact commit, with the reloader off, so that
"the code under test" is a fact rather than an assumption.

#### Containment — DAT-017A stays out of this environment

DAT-017A review is a **separate task with a separate environment**. It is not
touched, staged, evaluated, or repaired here.

The contamination is also **not** an umbrella that invalidates other findings by
association. D-4 in particular was observed on the baseline, before the reload,
in a different subsystem (capture-time identity, not domain resolution). It
remains a legitimate standalone follow-up issue and is not withdrawn, downgraded,
or folded into this incident.

**The DAT-017A migration must not be applied to the DAT-011 database to repair
this incident.** Applying `d7a3f18c62b4` would make the error go away by
advancing the schema past the baseline under acceptance — the trial would then
be running unmerged code against an unmerged schema, and every result after it
would be worthless in exactly the way the results after the reload already are.
The repair is to restore the code, not to advance the database.

#### Preconditions for resuming

The trial resumes only once all four hold:

1. The working tree is restored to `main` at **`d99e274`** — the DAT-017A files
   out of the tree entirely, not merely unstaged.
2. uvicorn is **restarted** (a reload is not enough; the process must be the one
   started from the restored tree) and, preferably, started **without
   `--reload`** so the code under test cannot move again mid-run.
3. The `vmr_dev` schema is **unchanged** — still at the baseline revision, with
   no DAT-017A migration applied.
4. The capture page for the E3 record renders again, which confirms 1–3 from the
   operator's side.

#### Open question carried into the resumed run — did the Confirm commit?

The POST returned **303**, so the write plausibly committed and only the
redirected GET failed. That is a plausible reading, not an established fact: the
303 proves the handler returned without raising, not that the transaction was
committed, and the handler that ran was DAT-017A's, not the baseline's.

**First action on resume, before retrying E3:** open the E3 capture record and
read its current state.

* If the domain already shows as confirmed, the write committed. Record E3 as
  **re-observed**, note that the confirmation predates the restart, and do not
  re-confirm — a second confirm would test idempotency, which is S11's job, not
  E3's.
* If it does not, the write did not survive. Re-run E3 from a clean state.

Either way the *first* run of E3 stays quarantined. This check establishes what
state the record is in, not whether E3 passes.

### D-5 (withdrawn) — reclassified as environment contamination

**Withdrawn as an acceptance blocker and reclassified as an
environment-contamination incident** (recorded above). It is not a product
defect, no issue is filed against the product, and it blocks nothing about
`d99e274`. Kept here rather than deleted, because the reasoning that led to it
is part of the evidence trail and because a withdrawn defect is itself a result.

**Observed** [operator]: pressing *Confirm* on the rank-1 candidate of a trial
capture returned **Internal Server Error**.

*The three paragraphs that follow are the original, superseded reasoning, kept
verbatim so the error is legible. The capture shape is **not** the cause and
must not be cited as one.*

**Contrast that was believed to isolate it — it did not.** The same action succeeded earlier in this session
on a **profile** capture — confirmed, rejected two candidates, reached
`domain_candidate_confirmed`, and enabled promotion. The failing capture differs
in shape: `capture_mode = salesnav_people_search`, `extraction_status = partial`,
**0 experience observations**.

**Ruled out by inspection, so far:**

* The form is well-formed — it posts `decision=candidate` with the candidate's
  domain as a hidden field.
* `company_hints` is safe with zero experiences: `_current_role` falls back to
  `current_employment_hint` and then to `{}`, and the company name did render on
  the page, so hints resolve.
* The route catches `PromotionError` and redirects with a message, so a 500
  means an exception of a *different* type escaped — the failure is inside
  `enrichment.confirm_record` or `evaluate_company`, not in the guard clauses.

**Impact, as believed at the time.** That E3 was blocked, and therefore E6 and
E7. *Superseded:* the blocker is the contaminated environment, and it is removed
by restoring the baseline, not by fixing anything in the product.

**Diagnosed from the traceback.** `relation "company_domain_resolutions" does
not exist`, raised in `resolution/store.py` via `resolution_service.capture_view`
— DAT-017A code querying a table this database never had. Not a fault in the
merged baseline. See *Baseline drift* above.

The earlier reasoning was sound as far as it went and still wrong in its
conclusion: the shape difference between the salesnav and profile captures was
real but coincidental. The profile capture was confirmed *before* the reload;
the salesnav one *after*. Two variables moved at once and I attributed the
result to the visible one. Recorded because the trap is worth naming — the
contrast looked like a controlled comparison and was not.

## 5. Verdict

**Claude does not grade its own work.** What follows is a factual statement of
what was executed and observed, plus the status Claude claims. The acceptance
decision belongs to ChatGPT's independent review of the repository and to
Sahil's approval.

### Claimed status: the authenticated trial is complete, and the acquisition path performed as specified

Every phase ran to its end on `main@d99e274`, with the commit under test read
from the machine (`git rev-parse --short HEAD` → `d99e274`, working tree clean)
rather than assumed.

| Phase | Steps | Outcome |
| --- | --- | --- |
| A — Sales Navigator listings | A0–A5, A8 | pass |
| A — restore and open | A9 **partial** (draft yes, result no — D-8), A10 pass | one defect |
| B — person profile | B0–B4 | all pass |
| C — company page | C0–C3 | all pass |
| D — failure recovery | D1–D3 | all pass |
| E — domain resolution | E1–E7 | all pass, and in scope |
| F — backend truth | all seven checks | all pass |
| S-criteria | S1, S2, S6, S9, S10, S11, S12 pass; S5 partial | one defect |
| **Deferred by instruction** | A4–A7, S3a | held for a targeted rerun after #191 merges |

A4–A7 and S3a are deferred, not failed. D-2 makes the *Needs review* signal
unreadable on Sales Navigator rows, so running the selection-quality steps now
would measure the defect rather than the product.

### What the trial actually established

**The contact-first architecture behaves as documented, under observation rather
than only in prose.**

* A capture creates **no contact**: `created` = 0 across thirty submissions, and
  again on every later save.
* Promotion creates **identity only** — no campaign membership, no email, no
  verification, no score, no research. Checked field by field on the promoted
  contact.
* The capture is **immutable**: re-read after promotion, every field was
  unchanged except the reconciliation link. Promotion is a separate later event
  and the capture's own outcome is not rewritten to hide that.
* **Evidence accumulates; identity does not multiply.** The same person captured
  three times from two surfaces produced three retained capture rows and exactly
  one contact.

**The system refuses to invent.** Confidence absent from the provider is recorded
as *"not provided by this provider"*. `Founded` absent from a company page reads
*Not shown*. A profile's missing sections are named individually. A company
website is labelled *shown on this page* — an observation, never a domain
assertion. The panel states it outright: *"Empty fields stay empty — VM Prospector
will not fill them in."*

**Operator decisions are recorded as decisions.** `(candidate)`, `(manual)` and
`(unresolved)` are stored distinctly with actor and timestamp; rejected candidates
are kept rather than deleted; and the unresolved gate was verified **at the
service layer**, not merely greyed out in the page — a direct POST bypassing the
disabled control was refused with the same truthful reason.

**Failure and repetition are safe.** A save against a stopped backend failed
specifically (`code: network_error`), said *nothing was saved*, kept the reviewed
draft, and promised that retrying replays rather than duplicates. The retry then
did exactly that, and the failed attempt left no trace anywhere. Idempotency holds
at submission (S11), at promotion (E7), and at retry (D2/D3).

### What the trial cost, and what that is worth recording for

One incident and one correction, both about method:

* **Environment contamination.** Switching branches under a `--reload` server
  replaced the code under test mid-run. Half the trial had to be re-run. The
  lesson is now written into the resumption preconditions: a fixed commit, the
  reloader off, and the commit read from the machine.
* **A defect filed too broadly.** D-4 was filed as identity fragmentation.
  Tested live, promotion's natural key caught the case, so the finding was
  rewritten to the narrower and actionable claim. The original framing would have
  sent someone to fix a problem that does not exist in the form stated.

### Defects, and where they now live

| Ref | Summary | State |
| --- | --- | --- |
| D-2 | `derived_value` renders as *Needs review*, destroying the signal | **#191** |
| D-6 | Stale promotion refusal after confirmation | **#192** (with D-OBS-4) |
| D-7 | Unresolved reason stored, audited, never displayed | **#193** |
| D-8 | Restored outcome overwritten by the first detection — blocks S5's result half and S6 | **#194 (UI-016)** — fixed on `fix/ui-016-retained-result-restoration`; see §8 |
| D-9 | Promotion never writes `contact.company_id`; nothing else does either | to file |
| D-10 | Companies list reports *consistent* where the detail page reports a conflict | to file |
| D-4 | Derived member-id URL is not a usable identity key | to file, **rewritten and narrowed** |
| D-3 | Company briefly not visible in the app | not reproduced; left open |
| D-OBS-1…6 | Observations — copy and presentation | not filed individually |

**None of these blocks the promotion path.** D-8 blocks two acceptance steps
(S5's result half and S6 after a reopen). D-2 blocks one review step. D-9 and
D-10 are backend and operator truth in the company view. The rest are wording.

### What Claude is not saying

This document is not an acceptance. It does not close Issue #131, and nothing in
it was merged. The DAT-017A review remains a separate task against a separate
environment, and its migration was deliberately **not** applied to this database.
No fix for any defect above was implemented inside DAT-011.

**Owed next, for whoever picks this up:** file D-4, D-8, D-9 and D-10; then re-run
A4–A7 and S3a once #191 is merged, which is the only outstanding acceptance work
this trial identified.

---

## 6. A4–A7 and S3a — the deferred subset, re-run on the merged product

**Run date:** 2026-07-28, Asia/Kolkata (IST, UTC+05:30).

These five checks were the only DAT-011 work left outstanding. They were held
back deliberately: D-2 (#191) made the *Needs review* signal unreadable on Sales
Navigator rows, so running the selection-quality steps earlier would have
measured the defect instead of the product.

### 6.1 Environment under test

| Item | Value |
| --- | --- |
| Backend commit | **`e943fe6`** — "UI-014 + UI-015: make domain-decision feedback truthful (#203)" |
| Lineage | `aafb6df` INS-001 (#187) · `e05feac` UI-013 (#196) · `f11b9fd` DAT-017A (#190) |
| Working tree | clean (`git status --porcelain` empty) |
| Server | `uvicorn app.main:app --port 8000` — **no `--reload`**, verified from the console output |
| Readiness at the time of this historical run | `/ready` → `{"status":"ready","database":"ok"}`. This was the pre-hardening contract; current deployments must use authoritative `/readyz` and its nested `checks` body. |
| Database | local Postgres, environment `local`, dry-run on. No credentials recorded. |
| Extension | manifest **2.1.0**, reloaded from disk after the UI-013 merge |
| Feature flags enabled for the run | `contact_capture_intake`, `contact_capture_promotion` (both were off at preflight and had to be turned on), plus the pre-existing `csv_import`, `salesnav_intake`, `linkedin_profile_intake`, `linkedin_profile_refresh`, `linkedin_company_intake`, `workbench`, `salesnav_domain_enrichment` |
| Deliberately left OFF | `automatic_company_domain_resolution` (DAT-017A) — the S3a contract is written against DAT-014's explicit operator decision, and enabling the policy would have changed the behaviour under test. DAT-017A acceptance is a separate task. |

**Build fingerprint.** The manifest version was 2.1.0 before UI-013 and still is,
so it cannot prove the shipped build is current. The behavioural fingerprint used
instead: the listings filter label, which UI-013 renamed from *"Only prospects
with warnings"* to **"Only prospects needing review"**. The running panel showed
the new label, so the build under test contains #191.

**Baseline before any capture:** pending **78** · contacts **1044** · companies **2**.

### 6.2 Exact definitions used

Quoted from this document's own matrix, not restated from memory:

| Step | Definition |
| --- | --- |
| A4 | Review the list — ≥2 distinct companies among eligible rows; any no-company rows appear under *N skipped — no company name* and are absent from the list |
| A5 | Deselect at least one row — *Review selected (N)* and the Save label follow the selection |
| A6 | Confirm a flagged row is retained (S3a) — a row with a warning is still selectable and still submitted with its gap visible |
| A7 | Confirm nothing was invented for a skipped row (S3b) — no company borrowed from headline, school, location or an adjacent row |
| S3a | Retained-and-flagged: missing location, uncertain identity, no stable link — kept, badged, submitted with the gap visible |

### 6.3 Capture and selection

Three pages of one Sales Navigator people search, captured page by page by the
operator; the panel never turned a page.

| Page | Visible | Added | Skipped |
| --- | --- | --- | --- |
| 1 | 25 | 24 | **1** — `missing_company_name` |
| 2 | 14 | 14 | 0 |
| 3 | 25 | 25 | 0 |
| **Batch** | | **63** | 1 |

**A4 — PASS.** Twelve distinct companies in a twelve-row sample of the persisted
captures, against a threshold of two. The skipped row was reported in the panel
as *"1 visible row skipped: no Company Name on the page. Nothing was guessed and
nothing was submitted for them"*, named by row position, and absent from the
capturable list. 25 visible − 1 skipped = 24 added reconciles exactly.

**This is the first time S3b has been exercised.** The earlier run recorded that
no row on either page lacked a company, so the skipped block never appeared.

**A5 — PASS.** One row deselected: *Select all* went indeterminate, the primary
action moved from *Review selected (63)* to *Review selected (62)*, the review
tiles read **62 selected / 1 deselected**, and the Save label read **Capture 62
prospects**. The count follows the operator's decision, not the capture size.

### 6.4 A7 — the skipped row, tested against the tempting case

The skipped row **visibly displayed a company name as plain text** in its
subtitle. It was still not taken.

Cause, confirmed against the shipped selectors: every company selector targets a
LinkedIn company *entity* — `a[data-anonymize="company-name"]`,
`[data-anonymize="company-name"]`, `.artdeco-entity-lockup__subtitle a`. The
company on that row is not a LinkedIn Page (or was not linked in the person's
experience entry), so it renders as unlinked text with no entity. The three rows
above it rendered their companies in the heavier weight of a linked entity; this
one did not.

**A7 — PASS**, and on the case that actually matters. A blank row would have
proved only that nothing can be invented from nothing. Here a plausible company
string was on screen, one DOM level away from the field that wanted it, and the
extractor left it alone.

**Observation recorded, not filed as a defect:** an unlinked company is still a
real company, so rows like this are lost to acquisition entirely. Reading the
subtitle's company text when unlinked would be defensible and is arguably not
"inferring from the headline". Changing selectors is out of scope for an
acceptance run; see §7.

### 6.5 A6 and S3a's fault case — what could not be tested

The batch was swept for a genuinely flagged row using the panel's own
*"Only prospects needing review"* filter after each page:

| After page | Batch size | Rows with a review fault |
| --- | --- | --- |
| 1 | 24 | **0** |
| 2 | 38 | **0** |
| 3 | 63 | **0** |

Three independent counters agreed: the filter returned nothing, the review tiles
read `0 missing fields · 0 uncertain id · 0 selector fails`, and the review badge
read **62 ready** with no *need review* badge present at all.

**A6 — PASS for the provenance case.** A6's text is *"a row with a warning is
still selectable and still submitted with its gap visible"*. Every row in the
batch carries a `derived_value` warning; all stayed selectable, all were
submitted, and each shows its note on the row and again on the review card.

**S3a's fault case — NOT EXERCISED.** S3a names missing location, uncertain
identity and no stable link. None occurred in 63 rows across three pages. This is
recorded as untested rather than passed by association with A4, A5 and A7.

The sweep is the evidence. Sales Navigator renders a location on essentially
every row, so the case S3a describes is genuinely uncommon on this surface; it
will need either a search that happens to contain one or a deliberate fixture,
and a fixture would not be live authenticated evidence.

### 6.6 UI-013 (#191) confirmed in the live product

The fix this subset was waiting for, verified at five distinct surfaces:

| Surface | Before #191 | Observed now |
| --- | --- | --- |
| Row badge | *Needs review* on ~100% of rows | neutral **Derived** |
| Review tally | *N need review* counting every row | absent |
| Filter | matched every row | returns nothing |
| Review badge | *62 need review · 0 ready* | **62 ready** |
| Review card | *"Will be saved and flagged for review"* | *"Complete. The note above records where a value came from — nothing needs correcting."* |

All 12 sampled captures carry a derived profile URL and none an observed one,
which is the base rate that made the old behaviour so damaging.

### 6.7 S3a — the contact-first flow, proven independently

Run on the trial's own submission, workbench steps [machine, supervised].

| Element | Result |
| --- | --- |
| Submission | `e47a487a…` · client id `3288f17b…` · schema `linkedin-contact-capture/2.0.0` · extension 2.1.0 |
| Captures persisted | **62** |
| Outcome counts | `submitted 62 · staged_unmatched 62 · created 0 · duplicate_in_submission 0 · suppressed 0` |
| Pending queue | 78 → **140**, exactly **+62** |
| Label carry-over | submission carries `dat011-a4a7-rerun`; capture reads *"(requested; applied to a contact only once this capture is matched)"* |
| Note carry-over | `notes_recorded 62`; capture shows **Operator notes (1) — append-only** with timestamp and scope `submission` |
| Returned identifier / deep link | the panel's own link opened `/contact-captures/submissions/e47a487a-…` — the exact record, identifiers matching what had been read from the backend beforehand |
| Pre-decision state | `pending_lookup`, *"run the company-domain lookup first"*, **promote control disabled** |
| Lookup | `ok · 1 attempt · logo.dev`, 1 candidate, confidence **"not provided by this provider"** |
| Preview non-committing | after the lookup: `candidate_review_required`, promote **still disabled**, nothing created |
| Explicit confirmation | *"Confirmed the candidate investible.com. You can promote this capture now."* → `domain_candidate_confirmed`, promote **enabled**, still `not promoted` |
| Promotion | contacts **1044 → 1045** · companies **2 → 3** · pending **140 → 139** · scoped company contacts **0 → 1** |
| Label applied on promotion | promotion row reads `1 note(s) linked · labels dat011-a4a7-rerun`, and the capture's qualifier disappears — **requested → applied**, the loop closed |
| Retry | re-issued promote → *"already promoted"*, same contact id, **all four counts unchanged** |
| Immutability | capture re-read after promotion: `schema_version`, `capture_mode`, `extraction_status: partial` and the status pill all unchanged; only the reconciliation link differs |

**The negative case, on a second capture from the same submission.** *Leave
unresolved* with an operator reason produced `left_unresolved`, the banner
*"Recorded as deliberately unresolved. Nothing was promoted."*, the promote
control disabled, and `why not promoted: the operator left this company
deliberately unresolved`. A direct promote POST bypassing the disabled control
was **refused** with the same reason, and no count moved. The gate is enforced in
the service, not only in the page.

### 6.8 UI-014 (#192) and UI-015 (#193) confirmed

Both defects this trial raised are fixed in the running product:

* **#192 / D-6** — after confirmation `why not promoted` is **gone**, where it
  previously survived and told the operator to do something already done. The
  pre-confirmation wording is also count-aware: with one candidate it read
  *"1 domain candidate is waiting for your confirmation"*, not *"several"*.
* **#192 / D-OBS-4** — the success message now names the decision source:
  *"Confirmed **the candidate** investible.com."*
* **#193 / D-7** — the capture page now carries a **`why unresolved`** row
  displaying the operator's typed reason, and distinguishes it from the
  submission note (*"about the person, not the domain decision"*), which is
  exactly the confusion the original defect created.

### 6.9 Privacy and conduct

No real names, complete LinkedIn URLs, email addresses, raw profile text,
credentials, cookies, auth headers or browser storage appear in this record.
Identifiers are truncated; people are referred to by row position or not at all.

No unattended navigation, pagination, stealth behaviour, CAPTCHA handling or
anti-bot technique was used at any point. Every page was opened and paged by the
operator; the panel read only what was already on screen and states so in its own
copy. All workbench actions were performed against the local loopback backend.

### 6.10 Result

| Step | Result |
| --- | --- |
| A4 | **PASS** — in full, including S3b for the first time |
| A5 | **PASS** |
| A6 | **PASS** for the provenance case |
| A7 | **PASS** |
| S3a | **PASS** for the full contact-first flow; **fault case NOT EXERCISED** |

## 7. Findings from this run

| Ref | Summary | Severity | Blocker | State |
| --- | --- | --- | --- | --- |
| O-1 | The pages tile under-reports by one whenever page 1 is in the batch: Sales Navigator's page 1 carries no `page` query parameter, `pageNumberFromUrl` returns null, and the worker records only non-null page numbers. Page-1 rows also carry `sourcePageNumber: null` in their capture evidence. | provenance / cosmetic | no | to file — no existing issue found |
| O-2 | A row whose company is not a linked LinkedIn entity is skipped entirely, even when the company name is visible as plain text. Truthful and exactly as DAT-018 B specifies, but acquirable rows are lost. | coverage | no | to file as a follow-up to #185 — no existing issue found |

Neither changes any result above. The source search URL is stored on every
capture, so O-1 loses no evidence — page 1 is implicit rather than recorded.

**Carried forward from the earlier run, still unfiled:** D-8 (panel restore
overwritten by the first detection), D-9 (`contact.company_id` never written by
application code), D-10 (companies list contradicts the company detail page), and
D-4 as rewritten. D-9 was re-observed incidentally during this run: both existing
companies still show `Contacts 0` on the list while carrying promoted contacts.

---

## 8. D-8 resolved — UI-016 / #194 (2026-07-28)

Implemented on `fix/ui-016-retained-result-restoration`, branched from
`main@4483548` ("DAT-011: merge authenticated acceptance evidence (#206)").

**This section records what was verified by machine. The live S5 / A9 re-run has
not been performed and nothing here should be read as claiming it was.**

### The diagnosis held, and it was incomplete

D-8 as recorded above is correct: `paintMode()` computes `changed` as
`mode !== currentMode` with `currentMode` starting `null`, so on a cold panel
open it is unconditionally true and the sticky guard cannot protect a starting
panel. That is fixed.

Building the regression tests surfaced a **second cause the original diagnosis
had not reached**, and without it the fix would have worked exactly once:

`startLiveSync()` runs at the end of panel init and immediately calls
`preview()`, which sends `PROFILE_CAPTURE`. In the worker that message *removes*
`LAST_PROFILE_RESULT` — correct for the operator pressing *Read this page again*,
wrong for a preview that runs by itself on every open. So a restored result
would have been repainted correctly and then deleted from storage, and the
*next* reopen would have shown the draft again. The panel's automatic read now
carries `live: true`, and only an operator-initiated read discards the retained
result.

### What changed

| File | Change |
| --- | --- |
| `src/background/service-worker.js` | Every retained result is stored beside the capture context it came from — `{ kind, url }` under a versioned envelope. Reads tolerate a record written before this existed and return `context: null` rather than guessing. `PROFILE_CAPTURE` gained `live`. A new page merged into a results batch now discards the retained result, as a profile recapture already did. |
| `src/sidepanel/sidepanel.js` | Holds the context of the outcome on screen and answers `retainedStatus(surface, url)` → `match` / `other` / `unknown`. A save in flight and a failed save are explicitly context-free. |
| `src/sidepanel/sidepanel-profile.js` | The cold-open guard. A restored outcome is kept only when it can be *placed* on the detected page; genuine navigation still releases it. |
| `test/retained-result.test.js` | 18 new tests. |

`STICKY_VIEWS` is unchanged, detection is not suppressed, and no backend,
contract or schema change was made.

### Machine verification

Extension suite on an LF checkout of the branch: **370 tests, 370 pass**
(baseline `main@4483548` was 352; the 18 new tests account for the difference).

The new tests were run against the **pre-fix** source to prove they fail for the
stated reason: **12 of 18 fail**, including the cold-open restore, the first-paint
overwrite, the D-8 listings reproduction and the live-follower deletion. The six
that pass pre-fix are the negative cases — profile A's result must not appear on
profile B, a context-free result must not be restored, a newer draft must win —
which pass trivially when nothing restores at all, and which exist to stop the
fix over-reaching.

Coverage maps to #194's acceptance criteria as follows.

| Criterion | Test |
| --- | --- |
| A completed result survives close/reopen on the same context | *a completed result is still on screen after the panel is reopened* |
| Restoring creates no backend submission or snapshot | *restoring a result sends nothing to the backend* |
| A newer unsent draft is not overwritten | *a newer unsent draft is not covered by an older result*; *reading a profile again discards the result…*; *capturing more rows discards the result…* |
| Profile A's result never appears on profile B | *a result saved on profile A never appears on profile B*; *a listings result is not restored onto a person profile* |
| A genuine page change still updates the panel | *navigating from the restored result to another profile updates the panel*; *navigating to an unsupported page releases the restored result* |
| Returned links remain exact and usable | *the returned workbench link comes back with the restored result (S6)* |

### Two environment notes for whoever re-runs this

Running the extension suite against a **Windows working tree** produces two
failures that are line-ending artefacts, not defects:

- `capture-cancellation.test.js` — *"the cancellation path introduces no
  navigation or pagination"* slices the worker source at `indexOf("\n}\n")`,
  which never matches CRLF. Confirmed by converting the **unmodified** `main`
  worker to CRLF: the same test fails identically.
- `profile-topcard.test.js` — *"the company · school row is located by its logo
  slots"* fails on the CRLF tree both before and after this change.

Both pass on an LF checkout. Neither is touched by this branch.

### Still owed

- **The live S5 / A9 re-run has not been done.** Save a contact, close the panel,
  reopen it, and confirm the outcome and its *Open captured contacts* link come
  back on the same profile; then reopen a **second** time to confirm the live
  follower has not eaten the result; then open a different profile and confirm
  the first profile's result does not appear.
- A9, S5 and S6 stay at their current recorded states until that is done.

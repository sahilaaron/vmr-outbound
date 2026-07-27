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
| S5 | Close/reopen the surface, restore the last staged result | **VALID** | Draft + `lastResult` restoration, preserved through UI-012. |
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
| A4 | Review the list | ≥2 distinct companies among eligible rows; any no-company rows appear under *N skipped — no company name* and are absent from the list | **PASS on S1** [operator] — ≥2 distinct companies across the reviewed rows. **S3b not exercised**: no row on either page lacked a company, so no skipped block appeared |
| A5 | Deselect at least one row | *Review selected (N)* and the Save label follow the selection | **PASS** [operator] — one row deselected: tiles moved to 30 selected / 1 deselected, the badge to *30 need review*, and the primary action to *Capture 30 prospects*. **S2 satisfied** |
| A6 | Confirm a flagged row is retained (S3a) | A row with a warning is still selectable and still submitted with its gap visible | |
| A7 | Confirm nothing was invented for a skipped row (S3b) | No company borrowed from headline, school, location or an adjacent row | |
| A8 | Save the reviewed set | Submitted count == selected count; outcome counts render; `created` is 0 | **PASS** [operator] — *30 of 30 prospects saved*; sole outcome `staged_unmatched` = 30; **`created` absent, i.e. 0**. **S9 and S10 satisfied** |
| A9 | Close and reopen the panel | Last result and/or draft restored without recapturing or resaving | |
| A10 | Open the returned record via the panel's own link | The exact submission/capture record opens in the workbench | |

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
| B0 | Open a `linkedin.com/in/…` main profile | Strip reads *LinkedIn · Person profile*; panel follows the tab without a Refresh press | |
| B1 | Review the extracted fields | Fields match the visible page; anything absent is shown as missing, never guessed | |
| B2 | Capture → confirm → Save | Outcome is `refreshed_exact_match` or `staged_unmatched`; `created` is 0 | |
| B3 | Save again without recapturing | *already saved — idempotent*, same submission | |
| B4 | Open a Sales Navigator person page | Reports *unsupported* with the reason — the shipped detector supports only the main `/in/` profile | |

### C. Company profile

| Step | What to do | What must be true | Result |
| --- | --- | --- | --- |
| C0 | Open a `linkedin.com/company/…` page | Strip reads *LinkedIn · Company page* | |
| C1 | Capture on the About page | Firmographics match; a website shown on the page is reported as *Shown on this page*; if absent, *Domain not confirmed* with nothing invented | |
| C2 | Save | Saves company **evidence**, not a contact | |
| C3 | Confirm nothing overclaims | No company matching/diff presented as working — it is not implemented | |

### D. Failure recovery

| Step | What to do | What must be true | Result |
| --- | --- | --- | --- |
| D1 | Stop the backend, press Save | Clear failure; reviewed draft intact; *Download as file instead* offered | |
| D2 | Restart the backend, press *Try again* | Succeeds with the **same** submission id | |
| D3 | Inspect | No duplicate submission, capture, contact or evidence row | |

### E. Domain-resolution handoff (workbench, DAT-010 + DAT-014)

| Step | What to do | What must be true | Result |
| --- | --- | --- | --- |
| E1 | Open `/contact-captures/pending` | The captured person is listed as pending | **PASS** [machine, supervised] — trial captures present; before promotion the record reads *not promoted*, reason *"run the company-domain lookup first"*, with the promote control disabled and the candidates panel stating *"a domain is never invented for you"* |
| E2 | Run the company lookup | Candidates stored with provider order; confidence recorded as *not provided* rather than invented | **PASS** [operator] — live lookup returned **10 candidates**, reported as *awaiting your decision*; nothing auto-confirmed. See scope note. **Repeated in scope** [machine, supervised] on a trial capture: *Lookup finished: ok · 4 candidate(s) awaiting your decision* |
| E3 | Confirm one candidate | `domain_candidate_confirmed`, source `candidate`, actor and time recorded | **PASS** [operator] — two candidates rejected with the decisions preserved (*"the decision is kept with the candidates"*), one confirmed; outcome reached `domain_candidate_confirmed` and *Promote to contact* became available. See scope note |
| E4 | Where practical: type a domain for a second capture | `decision=manual` recorded as an override | |
| E5 | Where practical: leave a third unresolved | `left_unresolved`; nothing promoted | |
| E6 | Promote the confirmed capture | `contact_created`; labels and notes carried; capture linked | |
| E7 | Promote again | `already_promoted`; no second contact | |

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

### F. Backend truth (Phase 4)

Counts and sanitized identifiers only.

| Check | Expected | Result |
| --- | --- | --- |
| Raw capture payload after promotion | unchanged except the canonical contact link | |
| Contacts created by capture alone | **0** | **PASS** [machine, supervised] — pending queue moved **50 → 80**, exactly +30. All thirty submitted captures are present and all thirty are *awaiting promotion*, i.e. none became a contact |
| Contacts created by promotion | 1 per promoted capture | |
| Campaign memberships | **0** | |
| Email candidates / verifications / scores / drafts | **0** | |
| Repeated identical submission | one submission, one snapshot, one note | |
| Fields with missing evidence | still null, with warnings | |

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
| D-5 | Confirming a domain candidate on a **Sales Navigator** capture returns HTTP 500 | **acceptance blocker** | yes — blocks E3/E6/E7 in scope | to be filed, awaiting traceback |
| D-4 | A Sales Navigator capture's derived profile URL is an opaque member id, so the same person captured from their profile page will not match it | **identity fragmentation** | not for DAT-011 | to be filed — settles the open question Layer 3C left |
| D-3 | Reported: captured company not visible in the app for a Sales Navigator capture | unresolved | no | **not reproduced** — cause unknown, left open |
| D-OBS-3 | *Person observations* omits captured company and title; profile captures show the employer only via the experience table | presentation | no | not filed |
| D-2 | `derived_value` provenance is rendered as *Needs review*, flagging ~100% of Sales Navigator rows and destroying the signal | **blocker for S3a** | yes, for that step | to be filed |
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

### D-5 — confirming a candidate 500s on a Sales Navigator capture

**Observed** [operator]: pressing *Confirm* on the rank-1 candidate of a trial
capture returned **Internal Server Error**.

**Contrast that isolates it.** The same action succeeded earlier in this session
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

**Impact.** This blocks the in-scope E3, and therefore E6 and E7, because a
capture cannot be promoted without a confirmed domain. Sales Navigator is the
acquisition surface DAT-011 exists to accept, so a capture from it that cannot
be promoted is an acceptance blocker, not a cosmetic fault.

**Not yet diagnosed.** The traceback is on the operator's application console
and has not been read. No cause is asserted here until it has been.

## 5. Verdict

**Not yet determined — the authenticated trial has not been performed.**
Phase 1 reconciliation is complete; Phases 3–5 require the operator.

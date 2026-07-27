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
| A0 | Open the panel on a people-search results page | Header reads **VM Prospector**; detected-page strip reads *Sales Navigator · Search results*; no campaign selector anywhere | |
| A1 | Press *Capture visible contacts*; watch | Progress card appears with a live row count; scrolling is incremental, not jumpy | |
| A2 | Press *Stop reading this page* once, mid-pass | Pass stops promptly; view returns to top; rows already loaded remain reviewable; nothing submitted; feedback reads *Stopped* | |
| A3 | Press *Read this page again*; let it finish | Pass ends by itself; **no** page-2 advance, no new tab, no URL change | |
| A4 | Review the list | ≥2 distinct companies among eligible rows; any no-company rows appear under *N skipped — no company name* and are absent from the list | |
| A5 | Deselect at least one row | *Review selected (N)* and the Save label follow the selection | |
| A6 | Confirm a flagged row is retained (S3a) | A row with a warning is still selectable and still submitted with its gap visible | |
| A7 | Confirm nothing was invented for a skipped row (S3b) | No company borrowed from headline, school, location or an adjacent row | |
| A8 | Save the reviewed set | Submitted count == selected count; outcome counts render; `created` is 0 | |
| A9 | Close and reopen the panel | Last result and/or draft restored without recapturing or resaving | |
| A10 | Open the returned record via the panel's own link | The exact submission/capture record opens in the workbench | |

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
| E1 | Open `/contact-captures/pending` | The captured person is listed as pending | |
| E2 | Run the company lookup | Candidates stored with provider order; confidence recorded as *not provided* rather than invented | |
| E3 | Confirm one candidate | `domain_candidate_confirmed`, source `candidate`, actor and time recorded | |
| E4 | Where practical: type a domain for a second capture | `decision=manual` recorded as an override | |
| E5 | Where practical: leave a third unresolved | `left_unresolved`; nothing promoted | |
| E6 | Promote the confirmed capture | `contact_created`; labels and notes carried; capture linked | |
| E7 | Promote again | `already_promoted`; no second contact | |

### F. Backend truth (Phase 4)

Counts and sanitized identifiers only.

| Check | Expected | Result |
| --- | --- | --- |
| Raw capture payload after promotion | unchanged except the canonical contact link | |
| Contacts created by capture alone | **0** | |
| Contacts created by promotion | 1 per promoted capture | |
| Campaign memberships | **0** | |
| Email candidates / verifications / scores / drafts | **0** | |
| Repeated identical submission | one submission, one snapshot, one note | |
| Fields with missing evidence | still null, with warnings | |

---

## 4. Defects

Filed as separate GitHub issues linked to #131. None fixed inside this task
unless a small unambiguous acceptance blocker is documented first.

| Ref | Summary | Severity | Blocker | Issue |
| --- | --- | --- | --- | --- |
| D-OBS-1 | `created` is a declared capture counter no outcome can increment; the panel carries a label for it | observation | no | not yet filed |
| D-OBS-2 | Acceptance and CLAUDE docs still describe the pre-UI-012 panel and the old product name | documentation | no | not yet filed |

---

## 5. Verdict

**Not yet determined — the authenticated trial has not been performed.**
Phase 1 reconciliation is complete; Phases 3–5 require the operator.

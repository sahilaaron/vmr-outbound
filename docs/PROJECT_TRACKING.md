# Project Tracking

## Purpose

This document defines how development progress for **VMR Outbound Agent** is
translated into operational launch readiness.

The tracking system must answer one central question after every meaningful
build:

> **When can we go live, what prevents us from going live today, and who must
> act next?**

This is a management record, not a technical specification.

## Systems of Record

Use one owner for each type of information:

* **GitHub** is the engineering source of truth for code, issues, pull requests,
  technical decisions, tests, implementation evidence, and development history.
* **Google Sheets** is the management source of truth for operational
  readiness, delivery forecasts, blockers, decisions, ownership, and the
  current answer to “When can we go live?”
* `GOAL.md` defines the current authorized product scope and launch acceptance
  criteria.
* `AGENTS.md` and `CLAUDE.md` define permanent engineering and agent
  guardrails.

Never copy full technical specifications or implementation logs into Google
Sheets. Summarize their operational consequence and link to GitHub for evidence.

## Operating Ownership

The operating loop is deliberately split so the builder does not certify its
own work:

1. **Claude builds and prepares.** Claude works only within the authorized
   phase; creates the branch; commits and verifies changes (delivering the
   branch to the local repository when the session cannot push); and supplies
   a factual handoff and proposed tracker payload.
2. **Sahil bridges and decides.** When Claude cannot authenticate to GitHub,
   Sahil pushes the prepared local branch through CMD or GitHub Desktop. Sahil
   also resolves material product, operating, risk, cost, and scope choices and
   explicitly approves consequential actions such as merging.
3. **ChatGPT operates GitHub.** Once a branch is remote, ChatGPT creates or
   updates the PR; writes PR, issue, review, and closing content; manages issue
   and project state; and verifies the exact remote SHA after every push.
4. **ChatGPT verifies.** ChatGPT inspects the actual branch, diff, checks,
   migrations, tests, and phase exit conditions. Claude's handoff is a claim,
   not proof.
5. **ChatGPT records.** After issuing a formal verdict, ChatGPT updates the
   Roadmap and relevant phase tab when the verified evidence materially changes
   readiness, blockers, decisions, or forecast.

Claude does not require Google Sheets access. Its handoff must contain the exact
proposed management update, but ChatGPT owns the official entry. Sahil should
not have to copy development status between systems manually.

ChatGPT owns routine remote GitHub administration after the branch is available
there. Sahil's routine technical step is limited to pushing an unpushed local
branch when Claude lacks credentials. Claude owns product code and commits;
ChatGPT does not replace Claude as the implementation agent.

## Tracking Workbook

Google Sheet:

https://docs.google.com/spreadsheets/d/19_UV7IWEbjPhMT3Qyv_0XpaggsfgPCIUB8RnVYjBaeU/edit

The workbook contains ten development phases: Phase 0 plus Phases 1–9.

Create or maintain these tabs:

| Tab                           | Phase question                                                                        |
| ----------------------------- | ------------------------------------------------------------------------------------- |
| `00 — Foundation`             | Is the project base stable enough to begin product development?                       |
| `01 — Data & Campaigns`       | Can we reliably import and manage campaign contacts?                                  |
| `02 — Email Verification`     | Can we identify usable emails without creating unacceptable deliverability risk?      |
| `03 — Lead Scoring`           | Can we consistently decide which contacts deserve research?                           |
| `04 — Insights`               | Can we collect credible evidence for personalization at an acceptable operating cost? |
| `05 — Claude Bridge`          | Can Claude process bounded work reliably without unsafe authority?                    |
| `06 — Draft & Approval`       | Can emails be reviewed and approved accurately and quickly enough to operate?         |
| `07 — Saleshandy`             | Can approved outreach be scheduled and its outcomes recovered safely?                 |
| `08 — Dashboard & Operations` | Can the system be operated from desktop and phone, including during failures?         |
| `09 — Pilot & Launch`         | Are we ready to run or expand the first controlled campaign?                          |

Do not create duplicate tabs. If a tab already exists, update it.

## Required Phase Summary

Place the phase summary at the top of every phase tab.

| Field                           | Required content                                                 |
| ------------------------------- | ---------------------------------------------------------------- |
| Phase                           | Number and name                                                  |
| Phase objective                 | One plain-language operational outcome                           |
| Phase owner                     | Person responsible for moving the phase forward                  |
| Phase status                    | Not started, In progress, Blocked, Ready for review, or Complete |
| Go-live readiness               | No, Conditional, or Yes                                          |
| Earliest realistic go-live date | A date or date range supported by current evidence               |
| Forecast confidence             | Low, Medium, or High, with an optional percentage                |
| Critical blockers               | Count and concise list of launch-blocking conditions             |
| Decisions required              | Decisions Sahil or another named owner must make                 |
| Current build                   | Active branch, pull request, release, or build identifier        |
| Last verified build             | Most recent build for which readiness was actually checked       |
| Last updated                    | Date, time, and updating agent/person                            |
| Current answer                  | A two-to-four sentence answer to “When can we go live?”          |

Do not fill unknown values with optimistic guesses. Use `Unknown — requires
decision` or `Not estimated` and identify what is needed to produce an estimate.

## Operational Deliverables Table

Maintain one visible table below the phase summary.

| Column            | Meaning                                                                    |
| ----------------- | -------------------------------------------------------------------------- |
| Deliverable       | A management-level outcome, not a coding task                              |
| Launch reason     | Why the outcome matters to the first campaign                              |
| Owner             | Person or team accountable for the next action                             |
| Status            | Not started, In progress, Blocked, Ready for review, Complete, or Deferred |
| Planned date      | Original working target, retained for comparison                           |
| Current ETA       | Latest realistic completion date or range                                  |
| Dependency        | Outside condition or earlier deliverable required                          |
| Blocker           | Specific condition preventing progress or launch                           |
| Blocker class     | Product, Data, Vendor, IT, Compliance, Decision, Quality, or Capacity      |
| Launch impact     | Critical, High, Medium, or Low                                             |
| Decision required | Exact decision, approver, and required-by date                             |
| Evidence          | GitHub issue, pull request, test, demo, or runbook link                    |
| Latest update     | Concise operational change since the previous update                       |
| Verified at       | Date the evidence was last checked                                         |

One row may summarize several related GitHub issues when they produce one
operational outcome. Do not create one spreadsheet row per commit.

## Update Log

Below the deliverables table, maintain an append-only update log.

| Field                     | Meaning                                            |
| ------------------------- | -------------------------------------------------- |
| Date                      | When the update was recorded                       |
| Build or PR               | GitHub evidence for the build                      |
| What became usable        | New operational capability                         |
| What remains incomplete   | Important remaining work                           |
| New or resolved blockers  | Change in launch risk                              |
| Decision made or required | Decision and owner                                 |
| Forecast change           | Previous date, new date, and reason                |
| Go-live answer            | No, Conditional, or Yes with a concise explanation |
| Updated by                | Agent or person who made the update                |

Do not rewrite earlier log entries. Add a correction as a new entry when prior
information becomes wrong.

## Meaningful Update Triggers

Update the relevant phase tab when:

* A usable unit of work is completed and verified.
* An important pull request is opened, materially changed, or merged.
* A blocker appears, changes severity, or is resolved.
* A forecast date or confidence level materially changes.
* A decision from Sahil, IT, or a vendor becomes necessary.
* A phase exit condition is demonstrated.
* A dry run, test batch, or live pilot changes launch readiness.

Do not update the workbook after every commit, minor refactor, formatting
change, or internal implementation detail.

Finish and verify the meaningful unit of work first, then update tracking.

## Status Definitions

### Phase status

* **Not started:** No approved phase work is underway.
* **In progress:** Approved phase work is actively being built or verified.
* **Blocked:** No meaningful progress can continue without an external action,
  decision, access change, or dependency.
* **Ready for review:** The phase exit condition appears satisfied but has not
  received the required human review.
* **Complete:** The exit condition is verified, evidence is linked, remaining
  manual work is declared, and no unresolved phase blocker remains.

### Deliverable status

* **Not started:** No implementation or operating preparation has begun.
* **In progress:** Work is active and has a named owner.
* **Blocked:** A specific blocker prevents completion.
* **Ready for review:** Evidence exists and awaits review or approval.
* **Complete:** The outcome works and its evidence has been checked.
* **Deferred:** The outcome is outside the current authorized scope.

Do not use percentage-complete estimates. They create false precision. Report
what works, what does not work, and what remains instead.

## Go-Live Readiness

* **No:** At least one critical launch condition is missing or untested.
* **Conditional:** The system can launch only if named conditions are completed
  by specified owners before the proposed date.
* **Yes:** Current acceptance criteria are demonstrated, critical controls work,
  and all remaining tasks are explicitly non-blocking.

Code completion alone does not justify `Yes`.

Before marking a phase `Complete` or readiness `Yes`, confirm:

* The phase exit condition has been demonstrated.
* Relevant tests and checks pass.
* Known failures and recovery steps are documented.
* Remaining manual work is listed with owners.
* No unresolved critical blocker remains.
* Required human, IT, or vendor sign-offs are recorded.
* GitHub evidence is linked.
* The Google Sheet reflects the verified build, not an earlier build.

## Date and Confidence Rules

* Treat dates as forecasts until Sahil accepts them as commitments.
* Prefer a realistic range over a false single date.
* Preserve the original planned date and update the current ETA separately.
* Every delayed ETA must include the reason and changed dependency.
* Do not move a date merely to make the tracker appear current.
* Reduce confidence when work depends on an untested provider, unknown data
  quality, external approval, usage limit, or unresolved architecture decision.
* Raise confidence only after uncertainty is removed through evidence.

Use these confidence meanings:

* **Low:** Important unknowns can materially change the date.
* **Medium:** The approach is understood, but one or more dependencies remain.
* **High:** The remaining work is bounded, owned, and already demonstrated in a
  comparable path.

## Phase Operating Outcomes

Track these outcomes at minimum.

### 00 — Foundation

* Technical and hosting decisions recorded
* Repository and local setup reproducible
* Configuration and secrets handled safely
* Automated checks and migration tooling operational
* Authorized contact-input contract agreed
* Phase 1 can begin without unresolved foundational ambiguity

### 01 — Data & Campaigns

* A representative CSV imports with actionable errors
* Companies and contacts normalize and deduplicate safely
* Provenance and suppressions persist
* Campaign and contact states are visible
* Operators understand remaining manual import work

### 02 — Email Verification

* Email candidates generate and rank predictably
* Internal evidence is used without creating false verification
* MillionVerifier outcomes map safely
* Catch-all and unknown states remain visibly uncertain
* Verification usage, exception volume, and likely operating cost are known

### 03 — Lead Scoring

* Hard exclusions run before scoring
* Initial Fit Scores are explainable
* The 85/100 research threshold works
* Borderline and rejected samples receive human review
* The team understands expected research volume

### 04 — Insights

* Company and contact evidence carries sources
* Unsupported claims are rejected
* Insufficient evidence is handled honestly
* Research batches can pause and resume
* Evidence coverage, time per contact, and manual-review load are known

### 05 — Claude Bridge

* Claude retrieves only authorized work packets
* Structured results validate before storage
* Repeated submissions do not duplicate work
* Forbidden actions are unavailable
* Subscription limits and recovery steps are understood

### 06 — Draft & Approval

* Drafts use stored evidence
* Versions are immutable
* Editing invalidates approval
* Eligibility is checked before approval and scheduling
* Mobile review time and likely daily approval capacity are known

### 07 — Saleshandy

* Approved contacts schedule without duplication
* Delivery, reply, bounce, unsubscribe, and failure events return to RDS
* Suppressions update correctly
* Reconciliation and emergency-stop controls work
* IT confirms domain, mailbox, rotation, limit, and warm-up readiness

### 08 — Dashboard & Operations

* Essential workflows work on desktop and phone
* Failures and required actions are understandable
* Authentication, backups, health checks, and recovery steps work
* The owner can stop new scheduling immediately
* A non-developer can operate the first campaign from the dashboard

### 09 — Pilot & Launch

* Synthetic end-to-end dry run passes
* Security and privacy review is complete
* The authorized 100-contact batch passes preflight
* Pilot outcomes and operating effort are recorded
* Evidence supports stopping, fixing, or advancing to 250 contacts

## Update Procedure After a Build

After a meaningful build:

1. Claude supplies its structured handoff and proposed tracker payload.
2. If required, Sahil pushes the prepared local branch and reports the CMD or
   GitHub Desktop result.
3. ChatGPT verifies the remote head SHA and creates or updates the pull request.
4. ChatGPT verifies the actual GitHub build and relevant phase exit condition.
5. ChatGPT issues `PASS`, `PASS WITH CONDITIONS`, `FAIL`, or `BLOCKED`.
6. Claude prepares any requested code correction; Sahil pushes it only when
   Claude still lacks remote credentials; ChatGPT verifies the new SHA.
7. After a passing verdict and Sahil's explicit approval, ChatGPT merges and
   reconciles linked issues and project state.
8. ChatGPT identifies which operational deliverable changed.
9. ChatGPT updates only the relevant phase tab and the Roadmap if the overall
   launch answer changed.
10. ChatGPT updates the phase summary when status, blockers, ETA, confidence,
   decisions, or go-live readiness changed.
11. ChatGPT updates or adds the affected deliverable row and appends one
   update-log entry.
12. ChatGPT confirms that the build or PR link and verification date are
   correct.

If nothing operationally changed, do not manufacture a spreadsheet update.

## Access or Tool Failure

If the Google Sheet is temporarily unavailable:

* Do not discard or repeat completed development work.
* Do not claim the tracking update succeeded.
* Preserve Claude's exact proposed tracker payload in the handoff.
* ChatGPT records the pending official update when access returns.
* Do not mark the phase officially complete or begin the next phase until the
  tracker is reconciled.

If GitHub evidence is unavailable, do not mark the associated deliverable
complete.

## Scope Guardrails

* Do not use tracking work to expand `GOAL.md`.
* Do not add P1 or P2 deliverables to an active phase forecast unless Sahil
  explicitly moves them into scope.
* Do not treat future ideas as current launch blockers.
* Do not create dashboard polish work merely to improve tracker appearance.
* Do not allow tracker maintenance to consume time disproportionate to the
  build it summarizes.
* Do not hide delays, uncertainty, manual steps, or external dependencies.

The tracker exists to make launch decisions clearer—not to make progress look
better than it is.

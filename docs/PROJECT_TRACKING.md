# Project Tracking

## Purpose

This document defines the single management structure for the VMR Outbound Agent MVP.

The tracker must answer:

> **What prevents a captured Contact from becoming a verified, personalized and approved email ready for sending?**

The product outcome is:

> **A user can capture 2,000 Sales Navigator contacts in the morning and begin sending AI-personalized verified emails that afternoon.**

## Systems of record

- **GitHub** owns source code, issues, pull requests, technical decisions, tests and implementation evidence.
- **Google Sheets** owns management status, ownership, blockers, forecasts and readiness.
- `docs/GOAL.md` owns the authorized MVP outcome and acceptance criteria.
- `docs/ARCHITECTURE.md` owns the canonical data and Agent pipeline.
- `docs/AGENTS.md` and `docs/CLAUDE.md` own engineering guardrails.

The Sheet is not a second technical backlog. One management row may summarize several GitHub issues that produce one operational outcome.

## Canonical tracker structure

Replace the old phase-first roadmap with one Roadmap tab and these MVP stage tabs:

| Tab | Stage question |
| --- | --- |
| `Roadmap` | What is the current critical path to the MVP outcome? |
| `01 — Campaigns & Collections` | Can operators configure Campaigns and reusable Collections without making Campaigns own Contacts? |
| `02 — Capture & Identity` | Can 100–2,000 people be captured safely into permanent Contacts, with optional Campaign auto-add and persistent Labels? |
| `03 — Company Resolution` | Can Contacts converge on the correct reusable Company and domain? |
| `04 — Company Research` | Can the Research Agent produce reusable sourced Company facts? |
| `05 — Email & Verification` | Can the system find and verify an exact email using the locked policy? |
| `06 — Insights & Personalization` | Can verified Campaign Contacts receive evidence-backed AI insights and personalized copy? |
| `07 — Review & Sending` | Can exact message versions be approved and submitted safely? |
| `08 — Workbench & Agents` | Can every Agent and job be observed, paused, retried or disabled globally and per Campaign? |
| `09 — Pilot` | Can the complete pipeline process a controlled batch reliably? |
| `Future` | Which explicitly deferred ideas are being held outside the MVP? |

Do not create one tab per issue or legacy code prefix.

## Roadmap rows

The Roadmap uses one row per operational workstream:

1. Campaign and Collection model
2. Optional Campaign auto-add and persistent extension Labels
3. Contact and Company identity convergence
4. Company-domain reuse
5. Company research integration
6. Locked email discovery policy
7. Exact-address verification
8. AI company insights
9. Campaign Contact personalization
10. Review and approval
11. Sending integration
12. Workbench Jobs monitor and Agent controls
13. End-to-end dry run and pilot

## Required columns

| Column | Meaning |
| --- | --- |
| Workstream | Management-level outcome |
| MVP stage | Canonical stage above |
| GitHub evidence | Parent or implementation issue / PR |
| Owner | Person or agent responsible for the next action |
| Status | Not started, In progress, Blocked, Ready for review, Complete or Deferred |
| Dependency | Earlier outcome or external condition |
| Blocker | Specific condition preventing progress |
| Launch impact | Critical, High, Medium or Low |
| Current build | Active branch, PR or release |
| Latest verified result | What is actually usable now |
| Next action | One concrete next step |
| Decision required | Exact decision and owner, or None |
| Forecast | Realistic date or range, or Not estimated |
| Confidence | Low, Medium or High |
| Last updated | Timestamp and updater |

Do not use percentage-complete estimates.

## Locked product rules reflected in tracking

- Capture never requires a Campaign.
- An optional selected Campaign only auto-adds the resolved permanent Contact by upserting Campaign Contact membership.
- Labels in the extension are backend Collections.
- Campaign and Label selections persist until deselected.
- Campaign-specific scores, copy, approvals and send state belong to Campaign Contact.
- The frontend calls workers Agents.
- Every Agent needs global controls and Campaign-level overrides.
- The locked Agent order is Capture, Identity, Company, Research, Email, Verification, Insights, Personalization and Sending.
- The Workbench Jobs monitor is an MVP requirement, not a future dashboard enhancement.
- Email discovery attempts at most three formats and stops on the first verified result.

## Stage exit conditions

### 01 — Campaigns & Collections

- Campaign stores audience definition, seller context, messaging, CTA, guardrails, templates, sending configuration and Agent overrides.
- Collections are reusable backend records.
- Campaign Contact is the Campaign-specific membership and state boundary.
- Contacts remain permanent and campaign-independent.

### 02 — Capture & Identity

- Normal LinkedIn and Sales Navigator capture create or update permanent Contacts.
- Campaign selection is optional.
- A selected Campaign auto-adds resolved Contacts idempotently.
- Selected Labels apply as Collections.
- Campaign and Label selections persist until deselected.
- Repeated capture does not duplicate Contacts.

### 03 — Company Resolution

- Captures converge on reusable Companies.
- A resolved domain is reused across Contacts sharing the same Sales Navigator company identity.
- Ambiguous identity remains reviewable rather than fabricated.

### 04 — Company Research

- Research jobs are resumable and visible.
- Sourced facts retain provenance and freshness.
- Third-party text remains untrusted evidence.
- Research is reusable by Company.

### 05 — Email & Verification

- The employee-size pattern ordering is implemented as a versioned policy.
- No more than three candidates are attempted.
- Search stops after the first verified address.
- Verification outcomes remain exact-address evidence.
- Invalid, catch-all and unknown outcomes remain truthful.

### 06 — Insights & Personalization

- AI insights are stored separately from sourced facts.
- AI work runs after verification by default.
- Personalization uses Campaign context and stored evidence.
- Generated copy is Campaign Contact-specific and versioned.

### 07 — Review & Sending

- One exact message version can be reviewed and approved.
- Editing invalidates approval.
- Suppression and current eligibility are checked before sending.
- Approved Campaign Contacts can be submitted idempotently to the sending integration.
- Outcomes return to the application.

### 08 — Workbench & Agents

- Every Agent shows waiting, running, paused, retrying, failed and completed work.
- Queue depth, throughput and recent failures are visible.
- Operators can retry and inspect failures.
- Agents can be enabled, paused or disabled globally.
- Campaigns can override Agent defaults.
- New sending work has an emergency stop.

### 09 — Pilot

- A synthetic dry run completes without a real send.
- A controlled batch completes without duplicate Contacts, Companies, Campaign memberships, messages or sends.
- Failures are recoverable from the Workbench.
- Operating effort and bottlenecks are recorded before scale increases.

## Update procedure

After a meaningful build:

1. Verify the remote branch, diff, tests and migrations.
2. Update the relevant GitHub parent or implementation issue.
3. Record the operational consequence in the Roadmap and one stage tab.
4. State what became usable, what remains blocked and the next action.
5. Preserve earlier log entries; append corrections instead of rewriting history.

Also update the relevant tab when a stacked pull-request chain is assembled, or
merged in part, so that the remaining merge order and its owner stay visible.

Treat a stacked chain built by parallel threads as one unit of work. Do not log
each thread's branch separately. Record the merge order, which parent is
currently blocking its children, and who must act next. Intermediate restacks
and corrective pushes are engineering detail and belong in GitHub.

## Backlog rule

The single MVP epic owns the active end-to-end build. Keep a separate open issue only when it is:

- an active implementation slice;
- a narrow live defect;
- a required safety boundary;
- or a reviewable design task that directly advances the MVP.

All other suggestions belong in the single Future issue until operating evidence justifies activation.

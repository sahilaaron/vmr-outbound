# Project Tracking

## Purpose

This document defines the management structure for the current VMR Outbound Agent MVP.

The tracker must answer:

> **What prevents the assembled Contact-to-approved-draft product from being accepted in real operation?**

The current answer is operating proof, not another architecture build.

## Systems of record

- **GitHub** owns source code, issues, pull requests, technical decisions, tests and implementation evidence.
- **Google Sheets build tracker** owns management status, owner, blocker, next action and readiness consequence.
- [`CURRENT_MVP.md`](CURRENT_MVP.md) owns the current product reality.
- [`GOAL.md`](GOAL.md) owns the authorized MVP outcome and acceptance criteria.
- [`ARCHITECTURE.md`](ARCHITECTURE.md) owns the current data and Agent boundaries.
- `AGENTS.md` and `CLAUDE.md` own engineering guardrails.

The Sheet is not a second technical backlog. It summarizes the operational consequence of GitHub work.

## Current roadmap — 30 July 2026

| Workstream | Status | Evidence | Current reality | Next action |
| --- | --- | --- | --- | --- |
| Contact and Company foundation | Complete | #131 and merged foundation PRs | Permanent contact-first records, capture evidence, identity, domain and suppression exist | Use in live acceptance |
| Campaign pipeline | Complete | PR #232 | Durable Campaign Contacts, Agents, jobs, controls, Research, Email, Verification, Insights and Personalization are merged | Validate in operation |
| Customer v2 interface | Ready for merge | PR #233 / #217 | `/app`, Review and customer views are implemented; `/admin` remains the Workbench | Merge after CI and route checks |
| One-Contact live acceptance | Not started | #202 / #96 | Real website, MillionVerifier and Claude CLI path has not been proven together | Run one authorized Contact |
| Controlled 10–20 Contact batch | Not started | #202 / #96 | Concurrency, retries and usability need operating proof | Run only after the one-Contact pass |
| AI trust hardening | In progress | #181 | Core tool/evidence separation exists; adversarial matrix remains | Complete before send-capable pilot |
| Provider sending and outcomes | Post-MVP | #174 | No provider adapter; no sending side effect exists | Activate after MVP acceptance |
| Controlled 100-Contact send pilot | Post-MVP | #96 | Requires provider boundary and IT/mailbox readiness | Start with one provider submission |
| Fit scoring and Saved Audiences | Deferred | #161 / #163 | Explicit Campaign enrolment is the current path | Activate only from measured need |
| Cadence generation | Deferred | #213 / #214 | Current output is one immutable draft | Activate only from measured need |
| Extension Campaign auto-add | Deferred | #220 | Capture remains Campaign-independent; Workbench supports enrolment | Activate only if enrolment is a bottleneck |

## Tracker sheets

Use these management views:

1. **Roadmap** — current critical path and post-MVP boundary.
2. **MVP Acceptance** — one-Contact and 10–20 Contact proof checklist.
3. **Current Product** — implemented surfaces, Agent status and accepted limitations.
4. **Post-MVP** — work that must not appear as a current blocker.
5. **Issue Reconciliation** — GitHub issue/PR status map.

Do not return to the old phase-first plan. The current system is assembled vertically; management must track acceptance and operating consequence.

## Required columns

| Column | Meaning |
| --- | --- |
| Workstream | Management-level outcome |
| Status | Complete, Ready for merge, In progress, Not started, Blocked, Post-MVP or Deferred |
| GitHub evidence | Authoritative issue or PR |
| Owner | Person or agent responsible for the next action |
| Current reality | What is actually usable now |
| Blocker | Specific condition preventing the next state |
| Next action | One concrete step |
| Launch impact | Current MVP, acceptance, send-capable pilot or future |
| Confidence | Low, Medium or High |
| Last updated | Timestamp and updater |

Do not use percentage-complete estimates.

## Current MVP exit conditions

### Product merge

- PR #232 is merged.
- PR #233 is merged.
- `/`, `/app/review` and `/admin` render correctly after local update.

### One real Contact

- Identity and Company resolution complete truthfully.
- Research reads the real Company website and stores sources, gaps and a dossier version.
- Email and live Verification record the exact provider outcome.
- Real Claude CLI Insights and Personalization complete from persisted evidence.
- One exact immutable draft appears in Review.
- Human approve/discard is recorded in audit history.
- No sending side effect exists.

### Controlled 10–20 Contact batch

- Worker claims remain exclusive under the selected concurrency.
- Retries and failures remain readable and recoverable.
- Blocked and partial records remain truthful.
- Provider/model spend stays bounded to the selected batch.
- Customer UI and Workbench remain usable during execution.
- No duplicate Contact, Company, Campaign Contact or draft artifact is created.

### Verdict

Record one verdict:

- **Pass** — close #202 and plan post-MVP work.
- **Conditional pass** — accept the MVP with named follow-up defects that do not invalidate the workflow.
- **Blocked** — name the exact operating failure and smallest repair.

## Current product truth

- The Campaign pipeline is merged through PR #232.
- The worker-based Research adapter is authoritative.
- Research gathers evidence; Claude is used by Insights and Personalization.
- Email candidate order is `firstname.lastname`, `firstname`, `finitiallastname`.
- Live Verification requires the feature switch, credentials and Agent live authority.
- The v2 interface is the customer front door; the Workbench remains at `/admin`.
- Review decides one exact immutable `DraftVersion` and does not send.
- Sending, replies, sequences and analytics are not built.
- Scoring, Saved Audiences, extension Campaign auto-add and multi-email cadence are not current MVP blockers.

## Update procedure

After a meaningful delivery or acceptance event:

1. Verify the remote branch or PR, diff, tests and migrations.
2. Update the authoritative GitHub issue.
3. Update one Roadmap row and the relevant Acceptance or Product row.
4. State what became usable, what remains blocked and who acts next.
5. Preserve earlier evidence but mark superseded guidance clearly.
6. Reconcile `CURRENT_MVP.md` when the product boundary or actual capability changes.

## Backlog rule

Keep a separate open issue only when it is:

- current MVP operating acceptance;
- a narrow live defect;
- required safety hardening;
- an active post-MVP provider build;
- or a future item with an explicit activation gate.

A future capability must not be presented as incomplete MVP work merely because an old roadmap once included it.

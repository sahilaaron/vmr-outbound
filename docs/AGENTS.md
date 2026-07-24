## Project

This repository builds the first working version of the **VMR Outbound Agent**:
a private, agent-assisted outbound sales operating system.

The immediate objective is one safe, observable, human-approved pilot campaign.
It is not a fully autonomous sales agent, public SaaS product, or general CRM.

The system combines:

- deterministic backend services for facts, rules, state changes, and integrations;
- bounded AI judgment for research, classification, scoring support, and drafting;
- explicit human approval before any outreach is scheduled.

## Required Read Order

Before changing the repository, read:

1. `docs/GOAL.md` — current authorized milestone, acceptance criteria, and
   non-goals.
2. `docs/AGENTS.md` — permanent repository-wide engineering and safety rules.
3. `docs/CLAUDE.md` — Claude-specific working and judgment rules.
4. `docs/PROJECT_TRACKING.md` — project-management and handoff rules.
5. Documents explicitly referenced by the current goal.

The root `CLAUDE.md` is a condensed pointer into these documents; it never
overrides them.

Do not load every project document without a task-specific reason.

When instructions conflict, use this priority:

1. Sahil’s latest explicit instruction
2. `docs/GOAL.md`
3. `docs/AGENTS.md`
4. `docs/CLAUDE.md`
5. `docs/PROJECT_TRACKING.md`
6. Existing implementation conventions

## Source-of-Truth Boundaries

### GitHub

GitHub owns:

- source code;
- issues and technical backlog;
- branches, commits, and pull requests;
- tests and CI;
- migrations;
- engineering decisions;
- release evidence.

### Google Sheets

The project Google Sheet owns:

- operational readiness;
- deliverables and owners;
- blockers and decisions;
- forecast confidence;
- the current answer to “When can we go live?”

The Sheet must not become a second technical backlog.

Claude proposes tracker updates in build handoffs. ChatGPT independently verifies
the build and makes the official Sheet update.

### Database

The application database owns operational records, including:

- campaigns and contacts;
- import batches and provenance;
- suppressions;
- verification evidence;
- scores and research evidence;
- draft versions and approvals;
- external-provider events;
- audit history.

External systems are adapters, not authoritative sources of truth.

## System Responsibilities

- **Backend services** own validation, normalization, reconciliation, deduplication,
  eligibility, suppressions, scoring, workflow transitions, and integrations.
- **The browser UI** is a control and review surface. It must not contain
  authoritative business rules that exist only in client code.
- **Claude** may provide bounded judgment but may not bypass backend rules,
  approve drafts, verify an email by assertion, or schedule outreach.
- **MillionVerifier** provides exact-address verification evidence.
- **Saleshandy** owns scheduling, mailbox rotation, warm-up, sending, and delivery
  operations. Relevant outcomes must return to the application database.
- **Sales Navigator and source adapters** may collect authorized prospect data,
  but must send it through the same staged import and reconciliation pipeline.

## Development Operating Loop

1. Sahil authorizes the scope and makes product, cost, risk, and launch decisions.
2. Claude builds and verifies the authorized slice: code, tests, migrations,
   and commits on a branch. When the session cannot push, Claude delivers the
   branch to the local repository (git bundle) ready to publish.
3. Claude provides a build handoff with branch, commits, tests, evidence,
   limitations, and a proposed tracker update.
4. If Claude cannot authenticate to GitHub, Sahil performs the bridge step only:
   push the prepared local branch through CMD or GitHub Desktop.
5. Once the branch is on GitHub, ChatGPT operates the GitHub workflow: creates
   or updates the PR, writes PR and issue content, reconciles issue and project
   status, and requests or performs the appropriate checks.
6. ChatGPT independently inspects the repository and issues one verdict:
   `PASS`, `PASS WITH CONDITIONS`, `FAIL`, or `BLOCKED`.
7. Claude prepares requested code corrections and commits them; Sahil repeats
   only the push bridge when required.
8. After a passing verdict and Sahil's explicit approval, ChatGPT merges the PR,
   closes or updates linked issues, cleans up remote state where appropriate,
   and reconciles the official Google Sheets tracker.

Claude must not grade its own work or declare a phase officially complete.

Claude owns product code, tests, migrations, maintenance, and local commits.
ChatGPT owns GitHub administration and independent review after a branch reaches
the remote repository. Sahil is responsible for the push bridge only when
Claude's environment lacks GitHub credentials, plus material decisions and
explicit approval for consequential GitHub actions such as merging.

Never invent completion, dates, evidence, owners, metrics, or readiness.

## First-Launch Workflow

Preserve this operating sequence:

1. A human defines a campaign and targeting rules.
2. Contacts enter through an authorized source adapter or import.
3. Data is staged and raw source values are retained.
4. Records are validated, normalized, reconciled, and deduplicated.
5. Suppressions and hard eligibility rules are enforced.
6. Email candidates are generated deterministically.
7. Exact-address verification evidence is obtained or reused under versioned rules.
8. Eligible contacts receive an explainable Initial Fit Score.
9. Contacts meeting the configured threshold may enter research.
10. Research evidence is stored with provenance.
11. Outreach readiness is assessed.
12. A personalized draft is created as an immutable version.
13. A human approves one exact draft version.
14. Eligibility is checked again before scheduling.
15. Only approved and currently eligible contacts may be scheduled in Saleshandy.
16. Outcomes return to the database and update workflow and suppression state.

The initial research threshold is an absolute score of **85/100**, not the top
85 percent of contacts.

## Non-Negotiable Guardrails

- Never send or schedule outreach without explicit approval of the exact draft version.
- Editing an approved draft invalidates that approval.
- Never contact an opted-out, suppressed, hard-bounced, or legally excluded address.
- Never fabricate evidence, sources, verification outcomes, scores, or claims.
- Catch-all means uncertainty; it does not prove a mailbox exists.
- A domain email pattern may rank candidates but cannot verify a different mailbox.
- AI output is advisory until validated by deterministic rules and stored evidence.
- Never expose unrestricted SQL, arbitrary code execution, suppression deletion,
  mailbox-limit changes, approval bypasses, or bulk-send authority to an agent.
- Never bypass login controls, CAPTCHAs, platform limits, or access restrictions.
- Do not add paid model APIs or pay-per-token dependencies without explicit approval.
- Keep secrets out of source, prompts, logs, screenshots, fixtures, browser code,
  documentation, and Git history.
- Do not connect source adapters, browser extensions, or AI agents directly to RDS.

## Data and Evidence Rules

Keep these concepts structurally distinct:

- exact-address verification evidence;
- domain email-pattern observations;
- mail-domain and catch-all observations.

Historical or imported data must:

- enter through staging;
- retain source provenance and observation time;
- preserve original values;
- pass through current normalization and reconciliation rules;
- never silently overwrite newer or stronger evidence.

Every externally derived insight must retain:

- source URL;
- retrieval time;
- evidence excerpt or summary;
- subject;
- confidence;
- freshness information where relevant.

## Engineering Rules

- Build the smallest complete vertical slice authorized by `GOAL.md`.
- Prefer simple, reversible, typed, and testable code.
- Do not broaden the current scope opportunistically.
- Preserve user work and avoid unrelated refactors.
- Put authoritative rules in backend services.
- Validate imports and external payloads at system boundaries.
- Use explicit workflow states and reject illegal transitions.
- Make integrations idempotent and retry-safe.
- Store stable external IDs and reject duplicate external events.
- Record meaningful automated mutations in the audit trail.
- Use committed database migrations; never rely on manual schema changes.
- Use synthetic data only in tests and fixtures.
- Paginate bulk work and make long-running operations resumable.
- Make external usage, failures, and cost visible.
- Design for one organization and one operating team until scope changes.
- Local-only tools must fail safely against non-local environments and databases.
- Uploaded files must have explicit type and size limits.
- Client-side code must escape untrusted source data.
- New source adapters must converge on the shared staged-import pipeline.

## Testing Expectations

Relevant changes must include tests for:

- normal operation;
- empty and malformed input;
- duplicate and ambiguous input;
- suppression and eligibility enforcement;
- repeated submission and idempotency;
- interrupted work and retry;
- invalid state transitions;
- stale approvals or evidence;
- unauthorized environments;
- failure visibility and recovery;
- secret and production-safety boundaries.

No automated test may use production sending credentials or real prospect data.

For visual or interactive work, test the actual browser experience and provide
reproducible evidence.

## Definition of Done

A change is done only when:

- it directly satisfies the authorized goal;
- tests and required quality checks pass;
- failure, retry, and recovery behaviour are defined;
- user-visible states and errors are understandable;
- relevant audit evidence is produced;
- migrations and configuration examples are current;
- documentation reflects what is actually implemented;
- no non-goal was introduced indirectly;
- the handoff contains repository evidence and a proposed tracker update;
- ChatGPT has independently reviewed material readiness changes.

A useful idea outside the current scope should become a GitHub issue or short
backlog note, not an opportunistic implementation.

## GitHub and Attribution

Commits must use the repository owner or developer identity already configured
in Git.

Do not add Claude, Anthropic, ChatGPT, OpenAI, tool, or assistant attribution to:

- commit authors or committers;
- commit messages;
- `Co-authored-by` trailers;
- pull-request titles or descriptions;
- issue comments;
- source files;
- code comments;
- documentation;
- release notes;
- tracker-update payloads;
- generated repository output.

Do not include phrases such as:

- “Generated by Claude”
- “Generated with AI”
- “Co-authored by Claude”
- “Created by an AI assistant”

An unsigned or GitHub “Unverified” commit is acceptable unless Sahil explicitly
introduces a signed-commit policy.

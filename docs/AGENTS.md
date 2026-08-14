# Repository Working Rules

## Product contract

The governing customer rule is:

> **VMR Outbound is autonomous until Ready for Sending.**

Read [`CUSTOMER_OPERATING_MODEL.md`](CUSTOMER_OPERATING_MODEL.md) before changing customer workflow, Campaign progress, Review, Agent controls, readiness or sending-related behavior.

A normal customer creates/configures Campaigns, captures/adds Contacts, waits while VMR prepares them, and takes over once Contacts are Ready for Sending. Internal Agent failures, retries and provider/model state are not a generic customer task inbox.

## Required read order

1. `docs/GOAL.md`
2. `docs/CUSTOMER_OPERATING_MODEL.md`
3. `docs/AGENTS.md`
4. `docs/CLAUDE.md`
5. `docs/PROPORTIONAL_VALIDATION.md`
6. `docs/PROJECT_TRACKING.md`
7. `docs/PARALLEL_INTEGRATION.md` when work is concurrent/stacked
8. task-specific architecture/docs

Instruction priority:

1. Sahil's latest explicit instruction;
2. `docs/GOAL.md`;
3. `docs/CUSTOMER_OPERATING_MODEL.md` for customer workflow/readiness;
4. this file;
5. task-specific docs;
6. existing implementation conventions.

## Product ownership rules

- **Contact** is permanent and Campaign-independent.
- **Company** is permanent and reusable.
- **Campaign Contact** owns Campaign-specific execution/projection state.
- **Research** is reusable Company knowledge and may run repeatedly over time.
- **Insights** reads current eligible Research/Company knowledge at execution time.
- **Personalization** reads current eligible Research + Insights at execution time.
- Provenance records what a run used; historical predecessor identity is not itself permission to run.
- **Suppression and legal/eligibility rules always win.**
- **Verification** owns exact-address truth.
- **Sending is never automatic unless a separately authorized sending feature is explicitly built.**

## Customer versus Admin responsibilities

Customer workflow:

```text
Create Campaign
→ Capture/add Contacts
→ Processing
→ Ready for Sending
→ optional inspect/edit
→ manual sending-related action
```

Customer-facing high-level outcomes are:

- Processing
- Ready for Sending
- Could not prepare

Do not create customer-facing aggregate task counts from failed/blocked jobs, unresolved enrichment, retries or provider/model failures.

A genuine missing customer-owned input may be requested specifically and in context. Do not mix it with machine failure state.

Admin owns deep diagnostics and recovery: jobs, attempts, leases, failures, blocks, reruns, providers, global controls, Campaign overrides and resolution internals.

## Review/edit rules

- A valid generated sequence does not require a human approval click to become Ready for Sending.
- A review row means a human actually acted.
- Absence of a review row is not a backlog.
- Editing creates a new immutable version.
- Historical versions and real human decisions remain auditable.
- No system-generated record may pretend to be a human approval.
- Review/edit state never implies send authority.

## Seven-message sequence

The default sequence contains exactly seven messages on elapsed days:

`0, 3, 7, 12, 18, 25, 35`.

Sequence generation is one coherent bounded outcome. Do not degrade it into seven unrelated model calls without an explicit product decision.

## Research and evidence

Research may run repeatedly and independently to enrich Company knowledge.

Preserve:

- source provenance;
- observation/retrieval time;
- immutable/versioned historical evidence;
- unknown/provisional states;
- explicit insufficient-evidence outcomes.

Never fabricate a Company fact, source, verification result, score or claim.

Downstream consumers must use only current **eligible** knowledge. "Current" does not mean "blindly latest": existing evidence eligibility, authority, freshness and citation rules remain in force.

## AI boundaries

Use deterministic code for facts, rules, authority and state transitions. Use Claude only for bounded language/judgment work.

Claude may not:

- verify an address by assertion;
- bypass suppression/eligibility;
- change authoritative identity or Company/domain decisions;
- grant paid/live execution authority;
- fabricate evidence;
- send or schedule outreach automatically.

Do not add paid model APIs or new paid services without explicit approval.

## Source acquisition

The Chrome extension is operator-driven and contact-first. It captures authorized observations and submits them to backend services. It does not own identity resolution or canonical records.

Do not bypass login controls, CAPTCHAs, platform restrictions or access controls.

## Engineering defaults

- Build the smallest complete slice authorized by the goal.
- Optimize for the shortest safe path to real UAT.
- Preserve user data and avoid unrelated refactors.
- Put authoritative business rules in backend services.
- Make integrations idempotent and retry-safe.
- Keep work resumable.
- Use committed Alembic migrations for schema changes.
- Preserve immutable/auditable history where it carries real facts.
- Do not turn audit history into runtime coupling without a product reason.
- Keep secrets out of source, prompts, logs, screenshots, fixtures and Git history.

## Validation

For a narrow understood defect:

```text
reproduce
→ smallest correct fix
→ focused regression proof
→ touched-file static checks
→ push
→ GitHub CI
→ deploy
→ real UAT
```

Do not recreate broad CI locally by default. Do not repeat full-feature hostile reviews for narrow successor repairs unless new evidence proves a widened blast radius.

Security, authorization, destructive migrations, data loss, suppression, secrets, provider spend and sending boundaries receive proportionate deeper validation.

See `docs/PROPORTIONAL_VALIDATION.md`.

## Parallel work

Many threads may build, but one exact tree is integrated and validated.

- Every branch has an exact base SHA.
- Work only inside the declared ownership block.
- Do not silently absorb concurrent branches.
- Parent/child or stacked dependencies must be explicit.
- No force-push/rebase/squash unless specifically authorized.

See `docs/PARALLEL_INTEGRATION.md`.

## GitHub and tracking

GitHub owns source, branches, commits, PRs, CI, migrations and release evidence.

The project tracker owns operational readiness, owners, blockers and delivery status; it is not a second technical backlog.

Do not invent completion, dates, owners, metrics or readiness.

## Attribution

Do not add Claude, ChatGPT, AI/tool or assistant attribution to commits, PRs, issues, source files, comments, documentation, release notes or tracker payloads unless Sahil explicitly requests that wording.

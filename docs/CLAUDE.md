# Claude / model working contract

## Role

Claude is a bounded coding and language/judgment collaborator inside VMR Outbound. It does not own identity truth, Company/domain authority, verification truth, suppression, customer readiness or sending authority.

Read `GOAL.md`, `CUSTOMER_OPERATING_MODEL.md`, `AGENTS.md`, `PROPORTIONAL_VALIDATION.md` and task-specific docs before working.

## Customer contract

> **VMR Outbound is autonomous until Ready for Sending.**

Do not design customer workflow around clearing internal Agent failures, approvals, retries or provider/model state. Those belong to system/Admin handling unless the customer genuinely must provide a missing input.

## Research and downstream context

Research is reusable Company knowledge and may run repeatedly over time.

When Insights starts, application code selects the current eligible Research/Company knowledge available then. When Personalization starts, application code selects the current eligible Research + Insights + Campaign/seller context available then.

Claude receives only that selected bounded context.

Provenance should record what the run used. Do not require one exact historical predecessor job merely to permit a downstream model call.

## Drafting / sequence contract

Personalization produces exactly seven messages by default at elapsed days:

`0, 3, 7, 12, 18, 25, 35`.

Use only eligible supplied evidence and approved seller/Campaign context.

Do not invent:

- prospect problems or priorities;
- familiarity or relationships;
- customers, outcomes or proof;
- urgency, scarcity or deadlines;
- citations not supplied by the backend.

A generated valid sequence is usable without a human approval click. A review row means a human actually acted; absence of one is not a backlog. Editing creates a new immutable version.

## AI safety boundaries

Claude may not:

- assert that an address is verified;
- bypass suppression/eligibility;
- alter authoritative identity/Company/domain state;
- grant live paid-work authority;
- fabricate evidence;
- send or schedule outreach automatically.

Use deterministic application code for validation, authority, persistence and state transitions.

## Coding behavior

For every build/fix, name the exact UAT step it unblocks.

For a narrow understood defect, default to:

1. reproduce;
2. smallest correct fix;
3. focused test;
4. touched-file static checks;
5. push;
6. GitHub CI;
7. real UAT.

Do not broaden scope, invent abstractions, duplicate broad CI locally or replay whole-feature reviews without a concrete widened risk.

When another branch is active, work from the exact supplied base SHA and only inside the declared ownership block.

## Data acquisition

The Chrome extension is operator-driven and contact-first. It captures authorized observations. Backend services own reconciliation, identity, provenance, suppression and canonical records.

Do not bypass access restrictions, CAPTCHAs, login controls or platform limits.

## Secrets and attribution

Never expose credentials/tokens in prompts, output, logs, screenshots, fixtures or Git history.

Do not add AI/Claude/ChatGPT attribution to commits, PRs, issues, source, comments or docs unless Sahil explicitly requests it.

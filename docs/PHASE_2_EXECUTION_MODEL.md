# Phase 2 execution model

This document describes the durable Campaign/Agent execution model behind the customer contract in [`CUSTOMER_OPERATING_MODEL.md`](CUSTOMER_OPERATING_MODEL.md).

## Governing distinction

The Agent pipeline is an internal execution system, not a nine-step customer checklist.

Customer projection:

```text
Processing
→ Ready for Sending
```

or, if VMR cannot complete the package:

```text
Processing
→ Could not prepare
```

Detailed Agent/job state remains durable and inspectable in Admin.

## Domain ownership

| Entity | Ownership |
| --- | --- |
| Contact | permanent canonical person |
| Company | permanent reusable organization |
| Campaign | Campaign-specific setup, context and execution controls |
| Campaign Contact | one Contact in one Campaign with Campaign-specific execution/projection state |
| Agent Job | one durable resumable unit of work |
| Research evidence/dossier | reusable versioned Company knowledge |
| Email Sequence | Campaign-specific seven-message output |

Suppressions and hard eligibility rules take precedence over downstream work.

## Agent order

1. Capture
2. Identity
3. Company
4. Research
5. Email
6. Verification
7. Insights
8. Personalization
9. Sending

Sending is registered as an extension boundary but automatic sending is disabled.

## Agent responsibilities

### Capture

Preserves authorized intake observations and converges them on permanent Contact records through existing reconciliation rules.

### Identity

Owns permanent-person convergence and identity ambiguity.

### Company

Owns the Contact-to-Company association and usable Company/domain decision.

### Research

Research is a reusable Company knowledge function. It may run repeatedly and independently of one Campaign execution.

Each successful run may persist:

- sourced facts;
- raw/source records;
- structured Company knowledge;
- a newer versioned dossier;
- provenance and retrieval/freshness metadata.

A later run does not erase historical Research evidence.

### Email

Generates bounded candidate addresses according to the active policy.

### Verification

Owns exact-address verification truth. A pattern or model output can rank candidates but cannot verify a mailbox.

### Insights

At execution start, deterministic application code selects the **current eligible Research/Company knowledge available at that moment**.

Insights derives claims from that snapshot and records what evidence/dossier/fact versions it actually used.

It must not require the exact Research Agent Job that historically created those records merely to run.

### Personalization

At execution start, deterministic application code selects the **current eligible Research + Insights + Campaign/seller context available at that moment**.

Personalization records the versions/evidence actually used and produces the validated seven-message sequence.

### Sending

Automatic sending is unavailable. Gmail draft creation, where enabled, is a separate explicit action and still cannot send automatically.

## Lineage contract

Lineage is an audit/provenance property.

It answers:

> What did this run use?

It must not become a control-plane requirement that answers:

> Which historical predecessor job must exist before this Agent may run?

A later Research update does not invalidate an older Insights result. A later Insights rerun may consume newer eligible knowledge. The same current-state-at-execution principle applies to Personalization.

## Eligibility remains strict

Removing predecessor-job coupling does **not** relax evidence or safety rules.

Consumers must still respect:

- Research authority and provenance;
- evidence eligibility/freshness;
- Company/domain state;
- exact-address Verification policy;
- suppression and legal exclusions;
- Campaign/seller policy;
- citation allow-lists;
- live-work/spend controls.

"Current" means current **eligible** state, not blindly the latest row.

## Live-work controls

Research, Verification, Insights and Personalization may require Campaign/deployment authority for real external work or spend.

That authority is Campaign setup/configuration. Enabling it does not turn failed historical jobs into customer tasks.

Existing durable recovery mechanics may rerun or reconcile work. Routine recovery belongs in system/Admin operations rather than a generic normal-user task queue.

## Durable queue

Agent Jobs retain:

- stable identity;
- Agent/task kind;
- Campaign/Contact/Company references;
- queued/leased/running/retrying/failed/completed/paused/cancelled state;
- attempts and max attempts;
- lease owner/expiry;
- due time/priority;
- structured inputs/results/errors;
- lifecycle timestamps and events.

Claiming uses PostgreSQL `FOR UPDATE SKIP LOCKED` so parallel workers do not claim the same row. Expired leases remain recoverable.

The worker supports bounded concurrency and optional Agent scoping.

## Pipeline projection versus queue state

Queue status is operational detail. Customer state is a higher-level projection.

### Processing

The Contact has not yet reached the usable outbound package and still has a path forward.

### Ready for Sending

The Campaign Contact is eligible and unsuppressed and current policy has produced:

- usable Company/Research knowledge;
- an address accepted by Verification policy;
- completed Insights;
- completed Personalization;
- a complete valid seven-message sequence.

A human approval row is not required.

### Could not prepare

A terminal condition prevents production of the outbound package.

This is customer-visible status, not automatically a mandatory customer action.

## Customer versus Admin recovery

`/app` should communicate progress and outcome.

`/admin` owns deep execution inspection and recovery, including failures, blocks, attempts, leases, provider/model errors, controls and reruns.

A specific customer input may be requested only when the customer truly owns the missing decision/input. Machine failures must not be aggregated into "Needs you".

## Sequence contract

Personalization generates exactly seven logical messages by default on elapsed days:

`0, 3, 7, 12, 18, 25, 35`.

Generation success is not waiting for approval.

Human review/edit history remains auditable, but it does not gate customer readiness.

## Concurrency safety

Existing lock-order, lease and idempotency rules remain authoritative. Simplifying customer interaction or downstream knowledge selection does not authorize weakening queue safety, suppression, verification or durable history.

# Phase 2 execution model

This document describes the implemented Campaign, Campaign Contact, Agent, durable job queue and explainable pipeline model used by the current MVP.

It extends the permanent Contact, Company, Campaign, Collection/Label, verification, provenance, suppression, audit, evidence and dossier implementations. It does not create parallel person, company, audience or queue abstractions.

## Current delivery

- The execution backbone and domain model are merged.
- The complete draft-producing Campaign pipeline is merged through PR #232.
- The customer-facing v2 application and Review surface are delivered in PR #233.
- Sending remains registered but disabled because no production provider adapter exists.

See [`CURRENT_MVP.md`](CURRENT_MVP.md) for the current product boundary and acceptance plan.

## Domain ownership

| Entity | Lifetime and ownership |
| --- | --- |
| Contact | Permanent canonical person. Capture never requires a Campaign. |
| Company | Permanent reusable organization. Identity and research are shared across Contacts and Campaigns. |
| Campaign | Campaign-specific context and execution controls. |
| Collection | Reusable Contact grouping; the extension may display the word Label. |
| Campaign Contact | One permanent Contact participating in one Campaign, with Campaign-specific pipeline and draft state. |
| Agent Job | One durable, resumable and inspectable work unit. |
| DraftVersion | One immutable Campaign-specific generated draft. |
| DraftApproval | Human decision against one exact DraftVersion. |

The database enforces one active membership identity for a Contact in one Campaign. Archiving a Campaign Contact never deletes the permanent Contact. Suppression takes precedence over downstream Agent work.

## Agent registry

Operator-facing stages are called Agents.

| Order | Identifier | Current adapter | Default posture | Current MVP role |
| ---: | --- | --- | --- | --- |
| 0 | `capture` | Existing contact-first capture/promotion outcome | Enabled | Permanent intake invariant |
| 1 | `identity` | Authoritative identity convergence service | Enabled | Resolve the permanent person |
| 2 | `company` | Permanent Company/domain linking service | Enabled | Establish reusable Company context |
| 3 | `research` | Registered deterministic research workers | Controlled/live opt-in | Store source-backed Company evidence |
| 4 | `email` | Deterministic candidate generator | Controlled | Generate approved candidate formats |
| 5 | `verification` | Existing exact-address verification service | Controlled/live opt-in | Commit authoritative provider decision |
| 6 | `insights` | Claude CLI thinking seam, no tools | Controlled | Derive cited Insights from persisted evidence |
| 7 | `personalization` | Claude CLI thinking seam, no tools | Controlled | Write one immutable Campaign-specific draft |
| 8 | `sending` | No production adapter | Disabled | Post-MVP extension point |

An Agent cannot be enabled unless a production adapter exists. Sending therefore remains unavailable rather than simulated.

## Research authority

The worker-based RES-001 Research adapter is authoritative.

Research:

1. builds the registered research workers;
2. executes the deterministic research step;
3. preserves the raw submission;
4. writes one versioned dossier;
5. writes sourced INS-001 evidence records;
6. projects an operator-facing outcome from what was actually found.

Research does not call Claude and does not mutate canonical Company fields. Claude begins at Insights, after evidence has been persisted.

## Email and Verification relationship

The Email Agent uses one versioned candidate order:

1. `firstname.lastname`
2. `firstname`
3. `finitiallastname`

It attempts no more than three candidates, enqueues one child Verification Agent Job at a time and stops immediately after the first verified result.

The Email Agent does not call MillionVerifier or the Verification worker synchronously. It resumes from the committed Verification domain outcome.

Live Verification additionally requires:

- `FEATURES__MILLIONVERIFIER=true`;
- configured real provider credentials;
- effective Agent configuration containing `{"live": true}`.

Simulated evidence cannot complete the live Campaign stage.

## Insights and Personalization

Insights and Personalization share one provider-neutral thinking boundary whose current transport invokes the operator's local Claude CLI.

Both use `allowed_tools=()`.

### Insights

- consumes persisted sourced evidence;
- stores derived Insight records with evidence references;
- records unsupported/dropped claims and insufficient-evidence outcomes;
- does not browse or change authoritative facts.

### Personalization

- consumes approved seller context, Campaign context, verified address state and eligible Insights;
- re-checks suppression and policy boundaries;
- writes one immutable DraftVersion;
- cannot approve or send its own output.

## Control precedence

Effective Agent control is resolved in this order:

1. Campaign execution master switch;
2. Campaign Agent override;
3. stored global Agent control;
4. registry default.

Controls use versioned writes. A stale control update is refused rather than silently overwriting a newer decision.

Re-enabling work resumes only records paused by that control. It does not remove a suppression, identity block, domain block or other authoritative domain reason.

## Durable job queue

`AgentJob` extends the proven PostgreSQL verification queue in place. Every job stores:

- Agent and task kind;
- Campaign, Campaign Contact, Contact, Company, capture and related references where applicable;
- parent job where causality matters;
- priority and due time;
- queued, leased, running, retrying, failed, completed, paused or cancelled state;
- attempt and maximum-attempt counts;
- lease owner and expiry;
- stable idempotency identity;
- structured input, result and error data;
- lifecycle timestamps.

Claiming uses PostgreSQL `FOR UPDATE SKIP LOCKED`, preventing concurrent workers from claiming the same row. Expired leases are recoverable. A successful process exit without the final domain commit completes nothing.

The worker supports parallel thread pools and optional Agent scoping. Expensive Research/Claude work can be bounded separately from Email, Verification, Identity and Company work.

Examples:

```bat
run_vmr_worker.bat
run_vmr_worker.bat 8
run_vmr_worker.bat once
run_vmr_worker.bat 2 research insights personalization
run_vmr_worker.bat 6 email verification identity company
```

## Explainable pipeline state

Queue status is not pipeline status.

Each Campaign Contact retains:

- desired terminal stage;
- current, next and latest completed stage;
- current pipeline projection;
- one projection per attempted Agent;
- linked current and historical jobs;
- append-only pipeline events.

The projection distinguishes waiting, running, paused, retrying, failed, completed, disabled, skipped and blocked work. It retains attempts, latest job, dependency, retryability, reason, timestamps and output references.

The read model must answer:

- what ran;
- what is waiting;
- what failed and why;
- whether it can retry;
- what blocks progression;
- what output was committed;
- what should happen next.

## Workbench and customer interface

### `/admin`

The Workbench remains the low-level control room for:

- Agent and job monitoring;
- Campaign stage counts and record drill-down;
- global controls and Campaign overrides;
- retries and failure inspection;
- explicit Campaign enrolment;
- Knowledge Base editing;
- capture-domain decisions;
- suppression creation.

### `/app`

The customer application provides:

- Today and Campaign views;
- Campaign pipeline status;
- Contact and Company records;
- Company dossier and evidence;
- Agent state and logs;
- Knowledge Base views;
- Review of exact immutable drafts.

The two surfaces share services and models, not templates or stylesheets.

## Review contract

The Review service reads immutable DraftVersion rows and records one human approve/discard decision against the exact version.

- A superseded version cannot be approved as if current.
- Discard is a recorded invalidation, not deletion.
- Approval does not send.
- Sending has no adapter.

## Current vertical acceptance path

```text
real authorized Contact
→ Campaign Contact enrolment
→ Identity
→ Company
→ real Company website research
→ email candidates
→ live exact-address Verification
→ real Claude CLI Insights
→ real Claude CLI Personalization
→ immutable DraftVersion
→ `/app/review`
→ human approve or discard
```

No Research, Verification, Insights or Personalization success may be simulated for the live acceptance verdict.

After the one-Contact pass, run a controlled 10–20 Contact batch to prove concurrency, retries, blocked states, partial outcomes and operator usability.

## Deferred extension

The following are not part of the current execution proof:

- sending-provider submission and outcome ingestion;
- deterministic scoring and Saved Audience selection;
- extension Campaign auto-add;
- multi-email cadence generation;
- Redis, Celery or Kafka replacement of the PostgreSQL queue;
- arbitrary user-defined workflows.

These require measured operating evidence or a separate provider implementation issue.

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

- an `ENABLED` Verification control on an execution-enabled Campaign;
- effective Agent configuration containing `{"live": true}`;
- configured real provider credentials.

Simulated evidence cannot complete the live Campaign stage.

`FEATURES__MILLIONVERIFIER` is **not** required and is not a brake: it gates the
legacy `/verification` console routes and the smoke script only, and is never
read on the Agent path. Neither is `DRY_RUN`, which concerns sending rather than
provider spend.

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

## Concurrency and Campaign pause contract

`AgentJob` is mapped to the physical PostgreSQL table `verification_jobs`.
The name is historical: Identity, Company, Research, Email, Verification,
Insights, Personalization and Sending jobs all share this table. An
`agent_id = 'research'` row in `verification_jobs` is therefore expected.

### Authoritative lock order

Any transaction that may touch more than one execution object must acquire row
locks in this order:

1. the `campaigns` execution gate in shared mode, ordered by `campaigns.id`,
   when a transaction may create or recover a lease;
2. permanent `contacts`, ordered by `contacts.id`, when Contact domain state is
   written;
3. `campaign_contacts`, ordered by `campaign_contacts.id`;
4. `verification_jobs`, ordered by `campaign_contact_id NULLS LAST`, `agent_id`,
   `created_at`, then `id`;
5. pipeline stage rows and other state owned by the already-locked Campaign
   Contact.

A control projection begins at step 3 after the master-switch transaction has
committed. Prepare and completion begin at step 2 because they preserve an
existing lease rather than creating one. A queue-only transaction may lock
an unrelated job directly only if it never reaches backwards into a Contact,
Campaign Contact or pipeline row. A worker discovers immutable foreign-key
references without locking, then re-reads every mutable value after acquiring
the complete ordered lock context. Email child Verification work includes its
parent job in that context. If `SKIP LOCKED` cannot acquire every required row,
the worker skips the candidate instead of operating on a partial context.

This order is mandatory because the former production paths were inverse:

| Path | Before | After |
| --- | --- | --- |
| Campaign pause/resume and Agent-control reconciliation | Campaign Contact → Agent Job, with jobs locked inside a Contact loop | ordered Campaign Contacts → ordered Agent Jobs → stage projection |
| Worker prepare, completion and failure | Agent Job → Campaign Contact/pipeline; Email could also reach Contact and parent job later | Contact → Campaign Contact → ordered job and parent → stage/domain projection |
| Job claim and direct claim | Agent Job only, followed by later related-object work | shared Campaign gate → Contact → Campaign Contact → complete ordered job context; eligibility revalidated under the locks |
| Lease recovery, retry, stage skip and manual re-run | mixed direct job locks and later membership writes | shared Campaign gate for recovery, then ordered Campaign Contacts → ordered Agent Jobs → stage projection; retry/re-run starts at Campaign Contact |
| Capture promotion and membership reconciliation | Contact write → Campaign Contact → jobs | unchanged in direction, with deterministic Campaign Contact and job ordering |

The old Campaign Contact → Agent Job versus Agent Job → Campaign Contact cycle
could deadlock. Using the same direction is more important than which row type
comes first. Stable database `ORDER BY` clauses are also required whenever more
than one row is locked; Python collection order and PostgreSQL's unspecified
return order are not lock-order guarantees.

### Worker and Campaign-control transactions

Worker claim, prepare and completion remain separate durable transactions.
Claims read the Campaign master switch as a lease eligibility predicate, acquire
a shared row lock on the Campaign as an execution gate, then acquire the ordered
context with `FOR UPDATE SKIP LOCKED`. The Campaign switch and membership are
revalidated before leasing. Shared locks let eight or more workers claim in
parallel, but conflict with the switch update. The gate exists only for the
short claim transaction; prepare and completion acquire the Contact, membership
and job context without it, then verify lease ownership before writing domain or
pipeline state.

A Pause or Resume request first locks and commits the Campaign row and its
versioned master switch. That commit is the authoritative execution boundary:
after it, no worker may lease newly prohibited work. Projection then reconciles
Campaign Contacts in ordered batches of at most 100, with one transaction per
batch. A newer Pause or Resume version supersedes any remaining batches from an
older request, so short projection transactions do not let an earlier request
overwrite the latest master state.

Projection preserves durable history and worker ownership. Pending and
retry-scheduled work may be projected to a control-owned Pause. Leased or
Running work retains its lease and is allowed to finish safely, or it observes
the disabled control at its next prepare gate. Reconciliation does not project
Running work to Skipped, a terminal Failed stage to Disabled, or a dependency or
domain Pause to a control Pause. Resume restores only jobs whose recorded pause
reason belongs to the applicable control.

### Deadlock recovery boundary

The Campaign-control request boundary has a secondary, bounded retry defence.
Only PostgreSQL SQLSTATE `40P01` is classified as a deadlock. Each failed
transaction is rolled back before retry, at most three attempts are made, and a
brief jittered backoff separates attempts. Other `OperationalError` values are
not retried. Exhaustion becomes a controlled Campaign concurrency error that the
v2 route reports without exposing a database traceback. Retrying does not
replace the lock-order contract; a persistent collision remains visible.

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

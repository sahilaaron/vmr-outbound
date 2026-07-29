# Phase 2 application backbone

This is the implementation contract for Campaigns, Collections, Campaign
Contacts, Agents, the durable job queue, and explainable pipeline state. It
describes the backbone introduced by migrations `4c8e1b2d9a70` and
`8f0a3d6c2b91`.

The design extends the existing Contact, Company, Campaign, Label, verification
queue, provenance, suppression, and audit implementations. It does not create
parallel person, company, audience, or queue abstractions.

## Domain ownership

| Entity | Lifetime and ownership | Phase 2 storage |
| --- | --- | --- |
| Contact | Permanent person record. Capture never requires a Campaign. Missing observed identity or company fields remain `NULL`. | `contacts` |
| Company | Permanent reusable organisation. Identity and research are never copied into a Campaign. | `companies` and existing Company evidence tables |
| Campaign | Campaign-specific seller context, target audience, messaging direction, CTA, templates, cadence, sending settings, and execution controls. | Extended `campaigns` |
| Collection | Global reusable Contact grouping. The extension may display the word **Label**. | Existing `contact_labels`, exposed by the canonical `Collection` model |
| Collection membership | Contact-in-Collection fact, independent of Campaign participation. | Existing `contact_label_assignments`, exposed as `CollectionMembership` |
| Campaign–Collection association | Makes a global Collection available to a Campaign; it does not transfer ownership or enrol every member implicitly. | `campaign_collections` |
| Campaign Contact | One permanent Contact participating in one Campaign, with Campaign-specific lifecycle, eligibility, pipeline, review, personalization, and sending projections. | Extended `campaign_contacts` |

The database enforces one Campaign Contact per `(campaign_id, contact_id)`.
Every enrolment also records append-only acquisition provenance in
`campaign_contact_sources`; replaying the same source idempotency key changes
nothing.

Archiving a Campaign Contact cancels non-terminal jobs but never deletes the
Contact. Re-enrolment never silently reactivates an archived membership.
Suppression produces a terminal eligibility block and takes precedence over all
Agent work.

## Capture and filing

`linkedin-contact-capture/2.1.0` accepts `campaign_id` as an optional UUID or
`null`.

1. The backend stores the immutable capture.
2. It creates or refreshes the permanent Contact without inventing missing
   fields.
3. It applies selected Collections idempotently.
4. If a Campaign was selected, it records a durable
   `capture_campaign_filings` intent and upserts the Campaign Contact in a
   savepoint.
5. The Contact/capture transaction remains committable if Campaign filing
   fails. The response reports `applied`, `pending`, or `failed` with the reason.

An exact identity conflict remains reviewable instead of creating a third
person or merging existing people. The permanent conflicting Contacts already
exist and the capture evidence is retained until an operator decides.

The extension stores the selected Campaign separately from a draft. Selection
is preserved across browser sessions, `None` remains a valid choice, and failure
to load Campaigns never blocks contact capture.

## Agent registry and controls

Operator-facing stages are always called Agents.

| Order | Identifier | Phase 2 adapter | Registry default | Skippable |
| ---: | --- | --- | --- | --- |
| 0 | `capture` | Permanent Contact/capture outcome | Enabled | No |
| 1 | `identity` | Existing LinkedIn identity-link convergence | Enabled | No |
| 2 | `company` | Existing permanent Company / exact-domain linking | Enabled | No |
| 3 | `research` | Contract only | Disabled | Yes |
| 4 | `email` | Existing deterministic candidate generation | Disabled | No |
| 5 | `verification` | Existing live verification service; simulator refused | Disabled | No |
| 6 | `insights` | Contract only | Disabled | Yes |
| 7 | `personalization` | Contract only | Disabled | No |
| 8 | `sending` | Contract only | Disabled | No |

An unimplemented adapter cannot be enabled. Adding an Agent requires one
registry entry and an adapter implementing the shared input/output protocol; it
does not require a new worker script or queue.

Capture is the acquisition invariant, not an optional downstream action.
Accepted intake always preserves its permanent Contact and evidence, so Capture
cannot be paused or disabled. Campaign execution controls begin with Identity.

Effective control precedence is:

1. Campaign execution master switch (disabled wins);
2. Campaign Agent override;
3. stored global Agent control;
4. registry default.

Campaign overrides merge configuration over the global configuration. Control
changes reconcile affected queued jobs in the same transaction. Re-enabling
resumes only jobs paused by that control; a domain block keeps its own reason.
Verification additionally requires effective Agent config `{"live": true}` and
real MillionVerifier credentials. Missing live authority pauses the stage;
simulator evidence never completes a Campaign pipeline stage.

## Durable job queue

`AgentJob` extends the proven PostgreSQL verification queue in place. The
physical table remains `verification_jobs`, preserving existing jobs and
foreign keys. Existing verification callers retain the `VerificationJob`
compatibility import.

Every job stores:

- Agent and task kind;
- Campaign, Campaign Contact, Contact, Company, capture, and generic entity
  references where applicable;
- priority and due time;
- queued, leased, running, retrying, failed, completed, paused, or cancelled
  state;
- attempt and maximum-attempt counts;
- lease owner and expiry;
- a unique idempotency key;
- structured input, result, and error objects;
- parent job where causality is useful;
- lifecycle timestamps.

Claiming uses PostgreSQL `FOR UPDATE SKIP LOCKED`, so concurrent workers do not
claim the same row. Every claim pass recovers expired leases. Work below its
attempt limit becomes immediately claimable; exhausted abandoned work becomes a
terminal failure. The `lease_expired` marker is durable across process restarts.

The production worker uses three transaction checkpoints:

1. claim and commit the lease;
2. pass safety gates, project `Running`, and commit;
3. lock the job while the adapter runs, then atomically commit the real domain
   outcome, terminal job state, pipeline projection, and history.

A crash therefore leaves durable `leased` or `running` work for expiry
recovery. Queue completion is staged only after an adapter has produced a real
domain outcome. A worker process exiting successfully without the third commit
completes nothing. Adapters that call external providers must also use the
provider's idempotency facility where one exists; a database transaction cannot
undo an external side effect.

Run the common worker once:

```bash
python scripts/run_agent_worker.py --once
```

Run continuously, optionally restricted to registered adapters:

```bash
python scripts/run_agent_worker.py \
  --agent identity --agent company \
  --worker-id phase2-local
```

## Explainable pipeline state

Queue status is not pipeline status. Each Campaign Contact has:

- desired terminal stage;
- current and next stage;
- latest completed stage;
- fast current pipeline projection;
- one durable `campaign_contact_agent_states` projection per attempted Agent;
- append-only `pipeline_events`;
- linked current and historical Agent jobs.

The projection distinguishes `waiting`, `running`, `paused`, `retrying`,
`failed`, `completed`, `disabled`, `skipped`, and `blocked`. Stage rows retain
attempt count, latest job, dependency, retryability, reason, timestamps, and
output references.

Events preserve enrolment, queueing, leasing, starts, completion, retry,
terminal failure, control pause/disable, eligibility block/restoration,
operator membership actions, deliberate skip, and cancellation. The pipeline
API therefore answers what ran, what is waiting, what failed, why, whether it
can retry, what blocks it, and what should happen next without reconstructing
history from logs.

A deliberate skip requires an operator reason, cancels non-terminal work for
that stage, and writes a `stage_skipped` event. Only registry stages explicitly
marked skippable (Research and Insights in Phase 2) accept this action. Identity,
Company, Email, Verification, Personalization, and Sending remain
safety-critical. Neither a domain-blocked stage nor a terminal suppression block
can be bypassed by skipping.

## API surface

All new endpoints are under `/api`.

| Method and path | Contract |
| --- | --- |
| `GET /campaigns` | Active/draft Campaign selector; optional archived inclusion |
| `POST /campaigns` | Create validated Campaign operating context |
| `PATCH /campaigns/{id}` | Partial update with settings version and audit |
| `POST /campaigns/{id}/execution` | Enable/disable Campaign execution |
| `GET /campaigns/{id}/operating-state` | Context, audience counts, and effective Agent controls |
| `GET/POST /collections` | List or create global Collections |
| `PATCH /collections/{id}` | Rename/update a Collection |
| `PUT/DELETE /collections/{id}/contacts/{contact_id}` | Idempotent Contact membership |
| `PUT/DELETE /campaigns/{id}/collections/{collection_id}` | Campaign association only |
| `POST /campaigns/{id}/contacts/{contact_id}` | Idempotent Campaign Contact enrolment |
| `GET /campaigns/{id}/contacts` | Filtered, paginated Campaign audience |
| `GET /campaign-contacts/{id}` | Campaign-specific audience record |
| `POST /campaign-contacts/{id}/pause\|resume\|archive` | Safe lifecycle actions |
| `GET /campaign-contacts/{id}/pipeline` | Explainable stages, jobs, events, and next action |
| `POST /campaign-contacts/{id}/retry` | Retry a retryable or resolved blocked stage |
| `POST /campaign-contacts/{id}/stages/{agent}/skip` | Deliberate, reasoned skip |
| `GET /agents` | Registry metadata plus stored global controls |
| `PUT /agents/{agent}/control` | Global control |
| `PUT/DELETE /campaigns/{id}/agents/{agent}/override` | Campaign override |
| `GET /agent-jobs/{id}` | Durable job status/result/error |
| `POST /agent-jobs/{id}/retry` | Retry only a genuinely retryable failed job |

Legacy `/campaigns` import routes and the extension's minimal
`GET /api/campaigns` selector remain compatible.

## Vertical acceptance path

The Phase 2 proof uses real existing components:

```text
manual or capture enrolment
→ idempotent Campaign Contact
→ Identity Agent job
→ existing LinkedIn identity-link adapter
→ Company Agent job
→ exact permanent Company/domain adapter
→ committed Contact.company_id
→ completed durable pipeline projection and event history
→ GET /api/campaign-contacts/{id}/pipeline
```

No verification, research, personalization, or sending success is simulated.
Unimplemented registry entries remain disabled until their real adapters and
safety gates exist.

## Migration and compatibility

- There is one Alembic head.
- Existing Campaign Contacts receive Capture-complete and Identity-gated state
  rows plus append-only seed events; Campaign execution starts disabled.
- Existing verification jobs are backfilled as `verification` Agent jobs.
- Existing Labels and assignments are not copied or renamed physically.
- Contact name/company identity columns become nullable so acquisition can
  truthfully persist an unresolved permanent Contact.
- Downgrade refuses before destructive changes when unresolved Contacts or
  generic/leased/paused Agent jobs cannot fit the legacy schema.
- PostgreSQL enum additions remain as unused labels after downgrade because
  individual enum values cannot be removed safely.

## Deferred work

Phase 2 does not build the final Campaign authoring UI, a drag-and-drop workflow
editor, Redis/Celery/Kafka infrastructure, or fake Research/Insights/
Personalization/Sending adapters. Provider delivery and autonomous sending
remain disabled. Scaling the PostgreSQL queue beyond the single-application MVP
is an evidence-driven future decision, not a prerequisite.

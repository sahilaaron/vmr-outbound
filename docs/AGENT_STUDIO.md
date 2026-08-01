# Admin Agent Studio

Agent Studio is the global operator shell at `/admin/agents/studio`. It is part
of the local-only Admin Workbench and is never mounted below `/app`. It projects
the existing Agent registry, global controls, Campaign overrides and durable
Agent Jobs; it does not introduce a second execution-control system.

## Common Studio contract

`app.services.agent_studio.extensions` contains one typed presentation module
for every `AgentIdentifier` in the authoritative registry. A module declares:

- whether inspection, configuration, side-effect-free preview, live execution
  and reporting are currently supported;
- its dedicated Admin route;
- the exact configuration and preview boundary shown when a capability is
  unavailable.

`app.services.agent_studio.reader.load_studio` derives effective state through
`PhaseTwoWorkbenchReader`. Registry defaults, stored global controls, Campaign
execution and Campaign overrides retain their existing precedence. Control
writes remain in `app.services.agents.controls`; omitted configuration is
preserved wholesale, including the explicit `{"live": true}` authority.

Agent-specific configuration never belongs in the common card contract. The
common shell may show status, capabilities, recent runs, failures, attempt,
retry and lease facts already persisted by the queue. It must show an explicit
unavailable state for facts or capabilities the persistence model does not
contain.

## Personalization Policy Studio

Personalization owns a specialized policy schema. It is not a universal Agent
configuration model. A policy snapshot contains versioned writing standards,
eight bounded temperament values, the approved strategy library, the fixed
evidence fallback ladder and bounded preference examples.

Policy versions and activation records are append-only. The migration seeds a
validated v1 and activation. A saved edit creates a new inactive version;
activation appends a ledger entry. Activating a prior version is rollback and
does not mutate either version. Application ORM hooks and migrated PostgreSQL
triggers reject update/delete operations on policy history.

Execution authority is deliberately separate:

- Agent control and Campaign override decide whether execution may happen;
- Personalization policy decides how an already-authorized draft is written;
- suppression, Company/domain gates and evidence eligibility remain separate
  deterministic authorities;
- a generated `DraftVersion` records policy, strategy, decision and producer
  provenance but remains unapproved;
- only the existing human Review flow may approve an exact immutable draft;
- Personalization cannot send.

### Context and fallback

Context selection is deterministic and runs before model generation. Company
facts must be eligible INS-001 evidence, current within the active policy,
above its confidence threshold and explicitly relevant to Campaign or approved
seller Knowledge Base text. Descriptive facts that merely recite the Company
are rejected. Contact role and sector context require an explicit lexical
connection to Campaign or seller context and cannot establish an internal
priority.

The ordered fallback is Contact + Company, Company, Contact role, sector, then
earnest offering-led introduction. Level five is a successful policy decision.
The model receives only the selected context and its permitted evidence IDs;
invented citations are rejected.

### Preview boundary

Preview reads a persisted Campaign Contact and its Contact, Company, Campaign,
Research-derived Insights and seller Knowledge Base context. It invokes the
bounded thinking seam with no tools and returns a concise deterministic
decision summary. The generation service never adds, flushes, commits, queues,
approves, mutates a `DraftVersion`, or sends. The Admin preview route has no
commit path and never activates the previewed version.

## Research report boundary

The Research page is read-only. `ResearchReportReader` remains the stable typed
port and `DurableResearchReportReader` is its sole production implementation.
It projects the existing RES-001 tables; there is no second report service and
no report-specific persistence. The same reader backs both:

* `GET /admin/agents/studio/research?campaign_contact={campaign_contact_id}`;
* `GET /api/admin/agent-studio/research/jobs/{agent_job_id}/report`.

Both routes are part of the local-only Admin Workbench. They are absent from
`/app`, and the API returns the same frozen dataclass graph the HTML template
receives. Loading either route performs queries only: it cannot enqueue or
retry Research, change pipeline state, select a dossier, mutate an Insight, or
edit any worker/configuration.

### Artifact identity and persisted truth

The exact `AgentJob.result.submission_id` and `dossier_version` are the
authoritative links for a completed execution. This matters because raw
submissions are deduplicated by Company and content hash: a later job may
truthfully reuse a submission whose immutable `request_context.agent_job_id`
names the first job that submitted those bytes. Exact request context is used
only as a compatibility fallback when an older job has no result link. The
reader never substitutes the Company's current/latest dossier for the selected
job's dossier.

The durable report exposes, when present:

* Campaign, Campaign Contact, Contact, and the Company recorded by the job;
* the current capture-scoped domain decision, with a clearly labelled
  current-Company aggregate fallback when the execution capture has no current
  decision (the execution's own domain remains the historical value in its job
  result);
* public job status, attempts/max attempts, queue timestamps, next-run time,
  current lease owner/expiry, sanitized stored error and retryability;
* queue worker identity from job-linked `JOB_LEASED`/`JOB_STARTED` events and a
  bounded event timeline (never raw event detail);
* Research source-worker/version, successful pages and structured collection
  failures from the immutable raw submission;
* the exact submission reference and exact dossier version/status/sections;
* Research-produced Insights selected by the job-derived idempotency prefix,
  plus typed evidence IDs, safe source metadata and source-record references;
* related Research job generations. Queue retries stay on the same job and are
  represented by that job's persisted attempt count rather than invented as
  separate runs.

`complete`, `partial`, and `unavailable` are deterministic report states. A
successful job is complete only when its exact submission, exact dossier, and
worker payload persist. A non-terminal/failed job or a job missing one of those
artifacts is partial. A Campaign Contact with no Research job is unavailable.
General observability limitations remain listed even on an otherwise complete
report; a complete report does not imply that unpersisted telemetry exists.

### Sanitization and known limits

Only typed, bounded fields leave the service. Existing Workbench sanitizers
redact credential-shaped values and authorization material. A narrow display
adapter additionally removes local filesystem paths and strips URL user info,
query strings and fragments before a link reaches HTML or JSON. Raw job input,
raw result/error mappings, raw worker output, environment variables, shell
commands, console logs, authorization headers and private model reasoning are
never returned.

Current persistence cannot truthfully supply:

* a complete discovered/attempted URL ledger (only successful reads and
  structured collection failures persist);
* a dedicated attempt-by-attempt lease-expiry ledger (current lease state and
  append-only pipeline events persist, while terminal transitions clear the
  job's lease owner); the single job `started_at`/`finished_at` pair describes
  the latest persisted attempt interval, not a complete attempt timeline;
* a structured dropped/rejected-evidence ledger (dossier warnings persist, but
  they are not relabelled as rejected evidence);
* unbounded worker stdout/stderr or runtime-only collection detail.

No migration was needed. Adding logging tables merely to imitate those missing
signals would weaken the truth boundary; the report labels them unavailable.

### GLM reconnaissance outcome

The planned GLM 5.2 task on
`feat/agent-studio-research-report-read-model` exhausted its 3M-token free-tier
allowance after branch/isolation confirmation and repository/Admin API
exploration. It was still mapping Research job schema, persistence, Company
dossiers and sourced facts. It did not reach branch creation, completed
read-model design, implementation, tests, validation, documentation, commits,
push, bundle, SHA-256 or handoff; there is no partial implementation, patch or
migration either. Those findings were reconnaissance only. The durable reader
therefore lives directly on the Agent Studio integration branch; there is no
future GLM integration artifact to merge.

Research prompts, worker source, code and collection rules remain outside this
interface.

## Adding a future Agent page

1. Keep execution registration in `app.services.agents.registry`; Studio does
   not register Agents.
2. Add or update the Agent's `AgentStudioModule` capability declaration.
3. Define an Agent-owned typed read/configuration port. Do not reuse
   Personalization policy types unless the concepts genuinely belong there.
4. Mount the dedicated page only below `/admin/agents/studio` and provide a
   truthful unavailable state for unsupported functions.
5. Keep preview/test services read-only unless the operator invokes a separate,
   explicit production action. Never grant approval, suppression bypass,
   credentials, arbitrary tools, shell/Python execution or Sending authority.
6. Add service, routing, authorization, unavailable-state and side-effect tests.

Future Company Intelligence and Sending modules must remain separate from the
current Company and Research boundaries. Sending stays non-live until its own
authorized implementation exists.

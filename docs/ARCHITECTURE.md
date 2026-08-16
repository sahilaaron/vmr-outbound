# MVP Architecture

## Product outcome

The current architecture moves a permanent Contact through one observable, controllable pipeline until that Contact is Ready for Sending: a generated, validated seven-message sequence held as immutable versions.

```text
Capture
→ Identity
→ Company
→ Research
→ Email
→ Verification
→ Insights
→ Personalization
→ Ready for Sending
```

No stage waits for a human. Readiness is computed from the artifact — current, non-superseded message versions on a live sequence that generated and validated — not from anyone having read or approved it. Reading, inspecting and editing the messages are optional and change nothing about readiness.

Sending is a registered future stage but is not implemented in the current MVP. Sending is manual: nothing leaves the system without a person doing it.

See [`CURRENT_MVP.md`](CURRENT_MVP.md) for current delivery and acceptance status.

## Presentation architecture

The product has two server-rendered presentation layers over the same services and models.

### Customer application

- Route prefix: `/app`; `/` redirects here.
- Workflow-oriented views for Today, Campaigns, Emails, Contacts, Companies, Knowledge Base, Agents, Capture and Suppressions.
- Today is a compact operational overview — contacts processing, contacts ready for sending, contacts VMR could not prepare, and per-campaign progress — rather than a task inbox. Emails is the reading surface at `/app/review`.
- Own router, templates and `v2.css`.
- Unsupported features are displayed as unavailable rather than populated with invented data.

### Operator/admin Workbench

- Route: `/admin` and its areas (Overview, Campaigns, Contact diagnosis,
  Contacts, Companies, Agent/Stages, Failures, Review, Providers & Usage,
  Configuration, System, Advanced Diagnostics). See
  [ADMIN_WORKBENCH.md](ADMIN_WORKBENCH.md).
- Organised around the operator path Campaign -> Contacts -> Agent/Stage
  progress -> worker -> Agent Job -> attempt -> evidence and corrective
  action; read models in `app/services/admin_workbench`, templates in
  `app/web/templates/admin/`, its own `admin.css`.
- The retained legacy implementation routes (imports, identity review,
  verification console, captures, knowledge base, local tools, the legacy
  monitor and the original overview at `/admin/legacy/overview`) keep their
  own templates and `app.css`, and are catalogued under `/admin/diagnostics`.

The customer interface is not a Workbench reskin. The two surfaces share domain services, not presentation code.

A third surface is an intake and output client rather than an interface: the
Google Sheets add-on (`integrations/google-sheets/`, routes under
`/integrations/sheets`). It submits name-and-company rows into the same Contact,
Campaign and Agent path everything else uses, and reads back the verified address
and the seven-message sequence. It holds no state, adds no schema and makes no
decision of its own. See [GOOGLE_SHEETS_INTEGRATION.md](GOOGLE_SHEETS_INTEGRATION.md).

The Admin Workbench also contains Agent Studio at `/admin/agents/studio`. Its
common shell is a projection of the authoritative Agent registry, controls and
durable queue. Agent-specific modules own their own typed configuration and
preview contracts; Personalization Policy is intentionally not a universal
Agent schema. See [AGENT_STUDIO.md](AGENT_STUDIO.md).

The Capture module is an exact-job, read-only projection over the existing
extension snapshot, import validation, promotion, suppression, Campaign filing
and Campaign Contact source records. Because those authoritative paths are
synchronous, they record a terminal Capture Agent Job rather than introducing a
second Capture worker or queue. Its bounded versioned result pins historical
decision facts and references; immutable source tables retain source evidence.
Current Contact, merge, label, membership and suppression state is projected in
a separate labelled section and never repairs missing execution history.

The Company module is an exact-job read model over the existing Company Agent,
append-only domain-decision ledger, capture evidence, Campaign policy and child
Research job. Historical execution truth is pinned in the job; current capture,
Contact/Company aggregate and Campaign state are separate projections. Studio
does not invoke resolution or edit canonical records.

## Core entities

### Contact

Permanent canonical person. Capture never requires a Campaign.

### Company

Permanent canonical organization. Domain and research results are reusable across Contacts and Campaigns.

### Campaign

Campaign-specific operating context and execution controls.

### Campaign Contact

The Campaign membership and execution boundary. It owns:

- Campaign-specific pipeline state;
- blocking and exclusion reasons;
- generated draft references;
- exact-version human decision;
- future sending state and outcomes when a provider adapter exists.

### Collection

Reusable grouping of Contacts. The extension may display Collections as Labels.

### Agent Job

One resumable unit of work with stable identity, lease, attempts, structured inputs/results/errors and audit history.
Synchronous Capture paths use the same envelope as a terminal execution record;
that reporting use does not change pipeline order or create a Capture worker.

### Company dossier and evidence

Versioned research output. Raw submissions and sourced evidence remain separate from AI-derived Insights.

### DraftVersion and DraftApproval

`DraftVersion` is immutable. `DraftApproval` records a human approve/discard decision against one exact version, and exists only where a person actually decided; its absence is not a pending decision. Approval is not sending authority by itself.

## Agent pipeline

| Order | Agent | Authority |
| ---: | --- | --- |
| 0 | Capture | Preserve authorized intake and permanent Contact evidence |
| 1 | Identity | Converge repeated captures on the correct Contact |
| 2 | Company | Link the permanent Company and usable domain |
| 3 | Research | Gather and persist sourced Company evidence |
| 4 | Email | Generate approved candidates in deterministic order |
| 5 | Verification | Commit exact-address provider evidence |
| 6 | Insights | Derive cited interpretation from persisted evidence |
| 7 | Personalization | Generate the immutable Campaign-specific message versions |
| 8 | Sending | Disabled contract; post-MVP provider extension |

## Research boundary

Production Research uses bounded Claude CLI web research as its required primary
source. Registered deterministic workers remain outside the normal production
path for tests, diagnostics and future explicitly approved alternate modes.

It preserves:

- raw worker output;
- versioned dossier structure;
- source URLs and normalized evidence;
- partial/thin, insufficient-evidence and failure outcomes;
- operator-facing counts and gaps derived from the committed result.

Research does not silently promote gathered text into canonical Company truth.

## AI boundary

Insights and Personalization use one provider-neutral thinking seam whose current transport is the operator's local Claude CLI.

Both run with `allowed_tools=()`.

The model may:

- interpret persisted evidence;
- identify insufficient evidence;
- generate bounded Campaign-specific language.

The model may not:

- browse independently inside these stages;
- verify an email;
- change identity, Company or suppression state;
- alter Agent controls;
- approve its own draft;
- send.

## Email and Verification policy

The Email Agent uses the active immutable Email pattern policy. Its seeded
generic order begins with:

1. `firstname.lastname`
2. `firstname`
3. `finitiallastname`

The policy bounds candidate count, stops after the first accepted exact address,
and may put learned Company-domain formats first. Employee size does not select
or sequence formats. A pattern observation ranks candidates; only exact-address
Verification can accept one.

It enqueues one child Verification Agent Job at a time and stops immediately after the first verified result.

Verification is the authority for exact-address provider truth. Live completion requires an enabled Verification control on an execution-enabled Campaign, provider credentials and effective `{"live": true}` Agent configuration. Simulated evidence cannot complete a live Campaign stage. `FEATURES__MILLIONVERIFIER` is not part of that path — it gates the legacy console routes and the smoke script — and `DRY_RUN` concerns sending, not provider spend.

Verification traverses the active immutable provider waterfall inside one
existing Verification Agent attempt. Provider adapters normalize their own
responses into the shared decision contract; provider result strings never
drive pipeline state directly. Usage entries identify provider, operation,
origin (`customer_operation`, `admin_operation`, or `agent_studio`), and the
persisted Campaign/Contact context available for the call. Sending remains
disabled and unchanged.

## Control and execution model

Every Agent has a registry default, stored global control and optional Campaign override.

Precedence:

1. Campaign execution master switch;
2. Campaign Agent override;
3. stored global control;
4. registry default.

Control writes are versioned. Suppression, identity blocks and other domain reasons cannot be bypassed by toggling an Agent.

The PostgreSQL queue claims work with `FOR UPDATE SKIP LOCKED`, supports lease expiry recovery and can run parallel Agent-scoped worker pools.

## Explainable state

Campaign Contact pipeline state is a domain projection, not a restatement of queue status.

It records:

- current, next and latest completed stage;
- waiting, running, paused, retrying, failed, completed, disabled, skipped or blocked status;
- reason, dependency and retryability;
- linked jobs and attempts;
- output references;
- append-only pipeline events.

The application must be able to explain what happened and what should happen next without reconstructing truth from process logs.

These statuses are the durable state machine and stay exactly as they are. On customer surfaces they are diagnostics: a failed, blocked or retrying stage is the system's own work to resolve, is never presented as a customer task, and never carries a count that reads as arrears. Recovery — job retry, pause, resume, skip-stage, lease repair — lives in the Admin Workbench. The customer-facing status vocabulary is the three words in `docs/CUSTOMER_OPERATING_MODEL.md`: Processing, Ready for Sending, Could not prepare.

## Current write-path choices

To preserve one authoritative implementation per high-risk action:

- Campaign enrolment is explicit through the Workbench, including bulk enrolment.
- Knowledge Base editing remains on `/admin`.
- Capture-domain decisions remain on the existing admin flow.
- Suppression creation remains on the admin surface.
- The customer application reads these records and links to the authoritative action where needed.

## Emails contract

The customer surface at `/app/review`, reached in the navigation as **Emails**,
is a reading surface rather than a queue. It:

- reads current immutable message and draft versions;
- shows relevant Research, Verification and Insight evidence;
- requires nothing before a Contact counts as Ready for Sending;
- writes an edit as a new immutable version;
- records approve/discard with audit history when a person actually decides;
- refuses approval of a superseded version;
- performs no send.

## Trust and safety boundaries

- Operator instructions and approved seller Knowledge Base content are trusted configuration.
- LinkedIn, website and provider text are evidence, not instructions.
- Sourced evidence remains separate from derived model output.
- Suppression, identity, verification and human approval are deterministic authority.
- Missing evidence remains missing; unknown is not false and provisional is not confirmed.
- Historical jobs, evidence and draft versions remain readable after retries or regeneration.

### Durable Insights derivation boundary

The Insights Agent consumes only the exact committed Research execution pinned
to its job. Its one existing bounded model call receives the dossier plus a
catalog of opaque Research evidence handles and has no tools. The model may
propose claims and Employee Size candidates, but deterministic application code
validates every handle and owns numeric parsing, subject relevance, taxonomy,
freshness, duplicate/conflict decisions and persistence.

Structured Employee Size extends the shared append-only `Insight` record with a
typed JSON payload and exact producing-job/dossier lineage. No mutable
`Company.employee_size`, second evidence system, new Agent identifier, queue or
pipeline stage is introduced. The current typed projection is computed from
append-only derivations; conflicts and historical observations remain readable.
Company Intelligence remains a separate bounded system and Email/Verification
policy remains independent.

### Company identity and domain boundary

Identity owns person matching and merge/create/assign decisions. Company owns
the Contact-to-Company edge, employer identity choice, canonical domain and the
existing confirmed/provisional/unresolved gate. Research and Email consume that
choice; neither may become the authority that selects it.

Company executions reuse the permanent `Company` model, exact-domain adapter,
`SalesNavCompanyEnrichment` candidate store and append-only
`CompanyDomainResolution` ledger. The job result pins the decision ids and
effective Campaign policy needed to explain that execution later. A report may
also label unconfirmed stored candidates `provider_only`, but that display term
does not change the three-state authoritative domain contract. Company
Intelligence is a separate classification bounded context and is not part of
identity or domain resolution.

## Current MVP boundary

Included:

- authorized Contact capture and permanent records;
- identity and Company/domain convergence;
- Campaign Contacts and explicit enrolment;
- durable Agents, jobs, controls, retries and history;
- sourced Company research;
- ordered email discovery and live exact-address verification;
- evidence-backed Insights and Personalization;
- customer-facing v2 application;
- generated, validated message sequences that reach Ready for Sending without human action, with optional reading, editing and exact-version approve/discard.

Post-MVP:

- provider sending and outcome synchronization;
- scoring and Saved Audience criteria;
- extension Campaign auto-add;
- replies, provider-side sequencing and analytics;
- arbitrary workflow construction;
- multi-tenant SaaS.

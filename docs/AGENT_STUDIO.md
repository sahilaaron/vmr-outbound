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

The Research page is read-only. `ResearchReportReader` is the stable port for a
report containing subject, domain state, Agent Job, attempts, timing, workers,
collection reads/failures, raw submission, dossier, sourced facts, rejected
evidence, retries, final outcome and stored error. The current
`PersistedResearchReportReader` maps only existing RES-001 records and labels
missing attempt-level details unavailable. It never treats console logs as
observability and sanitizes local filesystem paths.

The independently developed Research report read-model branch can implement
the protocol or adapt its result into the dataclasses without changing the
route or template. Research prompts, worker source, code and collection rules
remain outside this interface.

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

# Admin Workbench

The Admin Workbench is the primary operator surface: the authoritative control
and diagnosis centre for the whole application. It replaces the fragmented,
screen-at-a-time Admin navigation with one product organised around the
operator's mental model:

    Campaign -> Contacts -> Agent/Stage progress -> underlying worker
    -> Agent Job -> attempt -> evidence, output, failure
    -> available corrective action

An Admin opens a Campaign and sees its Contacts the same broad way a Customer
does at `/app`, with deeper operational information behind every row. Nobody
has to begin from a UUID, an Agent registry entry, a raw Agent Job or an
implementation-specific report.

## Terminology

| Term | Meaning |
|---|---|
| **Agent/Stage** | The business-level pipeline step (Capture … Sending) visible to Customers and Admins. |
| **Worker** | An execution mechanism performing all or part of an Agent/Stage — e.g. Research has the deterministic `website` worker, the `claude_web` fallback worker, and dossier persistence. |
| **Agent Job** | A durable execution request (`AgentJob`). |
| **Attempt** | One worker claim and execution try of a Job. |

Stage state, Job state and attempt state are never flattened into one label:
the pages show the committed stage status, the Job's public and stored status,
and the attempt counters side by side.

## Running it

```bash
FEATURES__WORKBENCH=true FEATURES__AGENT_WORKBENCH=true \
  uvicorn app.main:app --reload --port 8000
# open http://127.0.0.1:8000/admin
```

`FEATURES__WORKBENCH` mounts the UI (hard-locked to `APP_ENV=local`) and makes
every Admin Workbench page readable. `FEATURES__AGENT_WORKBENCH` additionally
unlocks the corrective actions (retry / pause / resume / skip-stage /
retry-job), which share the authoritative `WorkbenchCommands` surface with the
legacy monitor. With it off the pages still render and every action refuses
with a visible reason.

## Information architecture

| Area | Path | What it answers |
|---|---|---|
| Overview | `/admin` | What is running, blocked, failed, awaiting review; queue and lease health; recent Research fallback use; prioritised attention items that all link to a diagnosis. |
| Campaigns | `/admin/campaigns`, `/admin/campaigns/{id}` | Campaign list with health triage; Campaign detail with the Agent/Stage funnel, execution warnings, recent failures and the full Contact table (filter by stage, pipeline status, needs-attention). |
| Contact diagnosis | `/admin/campaigns/{id}/contacts/{ccid}` | The heart of the redesign: the complete Agent/Stage timeline for one Campaign Contact — per stage the committed status, human-readable explanation, worker(s), latest Agent Job and attempts, committed outcomes (email / verification / research / insights / draft), Research deterministic-vs-fallback lineage, downstream eligibility, and the safe corrective action if one exists. Raw Job detail sits behind Technical details. |
| Contacts | `/admin/contacts`, `/admin/contacts/{id}` | Cross-Campaign lookup for the permanent Contact: memberships, suppressions, email addresses and verification state, promotion-linked capture history, recent Personalization output. |
| Companies | `/admin/companies`, `/admin/companies/{id}` | Canonical identity and domain state, linked Contacts, Campaign participation, dossier versions, Research executions with fallback lineage, unresolved conflicts, Company Intelligence links. |
| Agent/Stages | `/admin/stages`, `/admin/stages/{agent}` | Operator layer over the Agent registry: control state and provenance, queue and stage counts, workers, Campaign overrides, recent completions/failures, average duration where reliably measurable. |
| File imports | `/admin/imports/{batch_id}` | One campaign-bound contact file import (IMP-001): the batch and every row it produced — Contact and Company resolution with the evidence that decided each, the supplied Company name beside the resolved one, the imported address, and the Email/Verification bypasses. Read-only; importing and re-importing stay on the campaign import screens. Linked from the Campaign detail **File imports** panel. |
| Failures | `/admin/failures` | The exceptions inbox: committed stage failures, blocked Contacts, failed Jobs not represented by a stage row, and stale leases — normalized, categorised from committed fields only, each row leading to its diagnosis. Plus file-import rows that were refused or held: those never became a Campaign Contact, so they carry no stage and no Job and appear nowhere else. |
| Review | `/admin/review` | Read-only visibility into Personalization output and decisions. Approve/discard stays in the Customer queue at `/app/review`; the Workbench never decides. |
| Providers & Usage | `/admin/providers` | Claude CLI, MillionVerifier, Logo.dev, DeBounce: configured or not, feature switches, ledger-recorded usage windows, last use and last failure. Secrets never render. |
| Configuration | `/admin/configuration` | Read-only home for the effective configuration: dry-run, feature switches, global Agent controls with provenance, Campaign overrides, active immutable policy versions, Research fallback bounds. Each row links to its authoritative write surface. |
| System | `/admin/system` | Job counts, stale leases, alembic revision, application version, audit tail, raw Job search by UUID. |
| Advanced Diagnostics | `/admin/diagnostics` | The catalogue of the technical and legacy surfaces (below). |

## What actions exist, and what is read-only

Supported actions — all through `workbench_agents.WorkbenchCommands`, all
audited, all reporting the command surface's answer rather than the operator's
intention:

* pause / resume a Campaign Contact;
* retry a failed, retryable stage for a Campaign Contact;
* skip the current stage (reason required, skippable stages only);
* retry one failed Agent Job.

Everything else on the Admin Workbench is read-only. Deliberately unsupported
here (their authoritative surfaces remain where they were):

* suppression release — suppression is authoritative over every stage and is
  never released from the Workbench;
* draft approval or discard — Customer review queue only;
* Agent control and override writes — legacy monitor and Agent Studio;
* policy creation/activation, credential rotation — Agent Studio;
* campaign create/edit — legacy Campaigns workflow (linked in place);
* eligibility blocks — resolved by fixing the underlying condition, never
  released by hand.

## Sending

The Sending Agent is not implemented in this release. Every surface renders it
as `Unavailable — Sending Agent not implemented in this release`; nothing
simulates a sending control, queue or outcome. The Stage and Configuration
areas are where global emergency stop, mailbox assignment, throttling, queued
sends, and delivery/bounce monitoring will land once a Sending Agent exists.

## Research lineage (RES-002)

The Contact diagnosis and Company pages read the durable Research report
(`agent_studio/research_report.py`) and show, per execution: whether the
deterministic `website` worker ran and whether its result was usable (with the
recorded reason when not), whether the `claude_web` fallback was invoked, its
status, producer version and permitted tools, accepted and discarded claim
counts with discard reasons, source URLs, and the final dossier basis.
Executions that predate lineage recording say `lineage unavailable` — nothing
is inferred for them.

## Imported Contacts (IMP-001)

A Contact that arrived through a campaign-bound file import carries an **Origin**
card on its diagnosis page, and its Email and Verification stages explain what
they did *instead* of discovery and verification: candidate generation bypassed,
no provider called, no evidence written. The vendor's own claims about the
address are shown behind a disclosure that labels them as the vendor's — there is
no field anywhere on these surfaces called "verified".

The Verification projection is unchanged and still refuses to read `bypassed` as
a verification decision, because that vocabulary means "a provider answered".
The bypass is stated from the import records instead. Full rationale and the
surface-by-surface map: `docs/CAMPAIGN_FILE_IMPORT.md` §12.

A Contact acquired any other way has no import lineage, and that absence is
treated as a fact — "not imported" — never as missing data.

## Advanced Diagnostics and the legacy surface

Nothing an operator bookmarked disappears. The redesign moved exactly one
route: the original import-centric overview left `/admin` (now the Workbench
Overview) for `/admin/legacy/overview`. Everything else — the Agent Studio,
the legacy `/workbench` monitor, `/imports`, `/review` (identity),
`/verification`, `/contact-captures/*`, `/knowledge-base`,
`/admin/company-intelligence`, `/local-tools`, and the legacy `/campaigns`,
`/contacts`, `/companies` workspaces — is unchanged, reachable from the
Workbench rail ("Workflows") and catalogued under `/admin/diagnostics`.

## Architecture

* `app/services/admin_workbench/views.py` — frozen presentation DTOs. No view
  derives a state the services did not commit; uncertainty stays explicit.
* `app/services/admin_workbench/import_lineage.py` — the campaign file-import
  read model, built on the import services' own public helpers so the Admin and
  customer surfaces cannot disagree about what a row did. Read-only.
* `app/services/admin_workbench/reader.py` — one read-only reader assembling
  every page, reusing `PhaseTwoWorkbenchReader`, the drafts queue, the policy
  services and `DurableResearchReportReader`, plus grouped aggregation queries
  (campaign health, the failures inbox, provider usage windows). It never
  writes.
* `app/web/admin_workbench.py` — thin routes; shared shell context; mutations
  dispatch to `WorkbenchCommands` and commit/rollback exactly like the legacy
  monitor routes.
* `app/web/templates/admin/` + `app/web/static/admin.css` — the Admin design
  system (dark rail, dense light work surface, strict status vocabulary,
  `details`-based Technical sections). The customer `v2.css` and the legacy
  `app.css` are untouched and never loaded by these pages.
* No schema changes: the redesign reads existing state only.

## Known limitations

* Failure categories are derived from committed codes (`error_class`,
  `reason_code`); codes outside the known vocabulary land in "Other failure".
* The failures inbox lists the most recent 200 matching items per view.
* Provider cost columns render only where the usage ledger recorded cost.
* Average stage duration is measured over succeeded Jobs from the last 30
  days and shows its sample size; it is absent when nothing reliable exists.
* Campaign creation/settings, CRM annotation, imports, identity review and
  studio configuration still happen on their existing (legacy-styled) pages.

Tests: `tests/test_admin_workbench_web.py`.

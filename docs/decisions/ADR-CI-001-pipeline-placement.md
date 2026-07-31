# ADR CI-001: Company Intelligence is a derived artifact, not a pipeline stage

Status: Accepted for the CI-001 first release

Date: 2026-07-31

Branch: `feat/company-intelligence` (base `fd017ea`)

## Context

Company Intelligence turns committed Research evidence into structured,
versioned, evidence-linked understanding of a Company. The obvious place to put
it is the existing chain:

```
Company → Research → Company Intelligence → Insights → Personalization
```

The brief asked explicitly **not** to insert that stage automatically, and to
first inspect the pipeline transition contracts, the Agent registry, existing
migrations, job lineage, Campaign Contact state, retry semantics, current tests,
and the effect on already-enrolled Contacts. This ADR records what that
inspection found and what was decided as a result.

## What the inspection found

**1. The pipeline's unit of work is a Campaign Contact, not a Company.**

`AgentJob.campaign_contact_id` is what the worker claims on
(`orchestrator.claim_next_campaign_job` passes `campaign_contact_only=True`), and
`CampaignContactAgentState` is one row per `(campaign_contact_id, agent_id)`.
Classification is a property of a *Company*. Running it as a contact-scoped stage
would classify the same company once per enrolled contact — N model calls to
learn one thing, and N versions racing for the same `is_current` slot.

**2. Adding an `AgentIdentifier` value is not additive in practice.**

`app/services/campaigns.py::_reconcile_campaign_controls` iterates
`for agent_id in AgentIdentifier` and calls `reconcile_agent_control` for every
member. `registry.get_agent_spec` raises `ValueError` for an identifier that is
not in `AGENT_SPECS`. So a new enum value must be registered, and registering it
puts it in `PIPELINE_ORDER` — which is exactly what makes it a stage every
enrolled Contact waits behind. There is no "registered but not in the chain"
state.

**3. The enum change is not reversible.**

`agent_identifier` is a native PostgreSQL enum used by `verification_jobs`,
`campaign_contact_agent_states`, `pipeline_events` and `agent_controls`.
`ALTER TYPE ... ADD VALUE` cannot be undone; a downgrade would have to rebuild
the type and rewrite four columns' dependencies. The brief requires reversible
migrations and a downgrade/upgrade round trip, and this would be the one
migration in the repository that could not honestly claim it.

**4. Already-enrolled Contacts would be affected.**

`desired_stage` is immutable after enrolment and `enrol_contact` raises on a
mismatch. Inserting a stage between Research and Insights changes what
`agents_through(desired)` returns for existing memberships, so contacts already
resting past Research would acquire a new WAITING stage retroactively. Nothing in
the current migration set does anything like that to live pipeline state.

**5. Nothing downstream needs it to be a stage.**

Insights and Personalization read the *dossier*, not a classification. Making
Company Intelligence a stage would put it on the critical path of every Campaign
Contact for a benefit nothing currently consumes.

## Decision

**Company Intelligence is a company-scoped derived artifact attached to
Research, produced by its own durable job queue, and is not inserted into the
Campaign Contact pipeline in this release.**

Concretely:

* `company_intelligence_jobs` is its own table with its own status enum, its own
  lease/attempt/backoff semantics, and its own worker
  (`scripts/run_company_intelligence_worker.py`).
* Nothing in `app/services/agents/` changes. No `AgentIdentifier` value is added.
  `PIPELINE_ORDER`, `AGENT_SPECS`, `agents_through`, `desired_stage` and every
  existing stage projection are untouched.
* A `CompanyIntelligenceVersion` names the `CompanyDossierVersion` it read, with
  a composite foreign key so the database — not a service check — refuses a
  version that reads another company's dossier. That is the lineage link to
  Research.
* Contacts are unaffected. No Contact becomes outreach-eligible, no stage state
  is written, and an enrolled Contact's path through the pipeline is byte-for-byte
  what it was before this branch.

## Why not the alternatives

**Insert the stage anyway.** Rejected on points 2, 3 and 4 above: it requires an
irreversible enum change, it makes classification per-contact work, and it
retroactively alters the stage set of already-enrolled Contacts.

**Reuse an existing `AgentIdentifier` (e.g. `RESEARCH`) with a distinct
`task_kind`.** Tempting, because `claim_next_job(campaign_contact_only=True)`
would never claim a company-scoped job with a null `campaign_contact_id`. Rejected
because the Workbench reader groups jobs and queue counts by `agent_id`
(`reader.py` lines 364–380, 583–597, 1316), so Company Intelligence jobs would
silently appear inside the Research Agent's operator view and change counts on a
screen this branch is not allowed to touch.

**A standalone script over the database.** Rejected outright: the brief forbids
bypassing the durable job and audit model, and a script is precisely the thing
that cannot answer "which companies did it reach, which did it skip, and why"
after somebody stops it half way.

## Consequences

* Company Intelligence can be produced, re-produced, corrected and backfilled
  without any Campaign existing at all — which matches what it is.
* It is not automatically kept fresh by the pipeline. A Company whose research is
  re-run does not get re-classified until something enqueues a job (an operator
  from the Admin page, or a backfill run). This is a real limitation and is listed
  as such in `docs/COMPANY_INTELLIGENCE.md`.
* Two queues exist. They share nothing but PostgreSQL, and the second one is
  ~300 lines. The alternative was one queue that had learned two execution models.

## When to revisit

Formal stage insertion becomes worth reconsidering when **all** of these hold:

1. A downstream stage genuinely needs a classification to run — most likely
   Personalization or a scoring stage that filters on industry.
2. The freshness gap above starts costing operator time rather than being a
   convenience.
3. There is an appetite for a migration that rebuilds the `agent_identifier`
   enum, or the deployment has reached a point where a forward-only migration is
   acceptable.

If that day comes, the insertion is mechanical: add the identifier, register the
spec between `RESEARCH` and `INSIGHTS`, add an adapter that calls
`app.services.company_intelligence.runner.produce_for_company` for the contact's
company, and decide what an already-enrolled Contact should see. The data model
does not need to change — which is the point of having built it company-scoped.

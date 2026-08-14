# Architecture

## Product architecture

VMR Outbound is a contact-first preparation system whose customer contract is:

> **Autonomous until Ready for Sending.**

The internal Agent pipeline prepares a Campaign Contact. The customer does not operate each Agent stage.

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

`Sending` remains the ninth registered boundary but automatic sending is not implemented.

See [`CUSTOMER_OPERATING_MODEL.md`](CUSTOMER_OPERATING_MODEL.md).

## Presentation architecture

### Customer application

`/app` is the normal customer surface. It should answer:

- what Campaigns exist;
- how many Contacts are Processing;
- how many are Ready for Sending;
- how many Could not prepare;
- what generated sequence exists for a ready Contact.

The customer application may expose detailed Agent progress for observability, but internal failures, retries and queue state are not a generic customer task list.

### Admin Workbench

`/admin` is the operational control room for:

- Agent/job inspection;
- failures, blocks, attempts, leases and retries;
- provider/model diagnostics;
- Campaign/global controls and live-work consent;
- resolution internals;
- operational recovery.

The two surfaces share domain services and models, not presentation responsibilities.

## Core entities

### Contact

Permanent canonical person. Capture never requires Campaign ownership.

### Company

Permanent reusable organization. Research knowledge belongs here and is reusable across Contacts and Campaigns.

### Campaign

Campaign-specific message/offer context, targeting/setup and execution controls.

### Campaign Contact

One Contact participating in one Campaign. It owns Campaign-specific execution/projection state and the relationship to the generated sequence.

### Agent Job

One durable resumable work unit with attempts, lease state, structured inputs/results/errors and audit history.

### Company Research knowledge

Research persists sourced evidence and versioned Company dossier state. Research may run repeatedly and enrich a Company over time.

Historical versions are retained for provenance. Downstream consumers select current eligible knowledge at execution time rather than requiring one specific historical predecessor job.

### Email Sequence

One generated Campaign-specific sequence containing seven logical messages, each with immutable versions.

Human review records exist only when a human actually acts. They are audit facts, not a readiness gate.

## Agent ownership

| Order | Agent | Authority |
| ---: | --- | --- |
| 1 | Capture | authorized source intake and permanent Contact evidence |
| 2 | Identity | permanent-person convergence |
| 3 | Company | permanent Company association and usable domain |
| 4 | Research | sourced reusable Company knowledge |
| 5 | Email | bounded address candidate generation |
| 6 | Verification | exact-address verification truth |
| 7 | Insights | evidence-backed derived claims from current eligible knowledge |
| 8 | Personalization | validated seven-message sequence generation |
| 9 | Sending | disabled automatic-send extension point |

## Research boundary

Research is an independent knowledge function.

It may run repeatedly, including outside one Campaign execution, and can:

- add newly discovered sourced facts;
- add structured Company knowledge;
- create newer dossier versions;
- preserve older evidence and observations.

Research must not silently fabricate or erase provenance.

## Insights input boundary

At the start of an Insights execution, deterministic application code selects the **current eligible Research/Company knowledge** available then.

That selected input is coherent for the run and the output records the evidence/dossier/fact versions actually used.

A later Research update does not invalidate the historical Insights result. A later Insights rerun may select the newer eligible state.

Lineage is provenance, not permission.

## Personalization input boundary

At the start of Personalization, deterministic application code selects the **current eligible Research + Insights + Campaign/seller context** available then.

Personalization records what it used, but does not require an exact historical predecessor Agent Job merely to run.

Suppression, verification, evidence eligibility and campaign policy remain hard deterministic gates.

## AI boundary

Claude is used through the bounded thinking seam for language/judgment work.

The model may:

- interpret eligible evidence;
- identify insufficient evidence;
- generate bounded Campaign-specific language.

The model may not:

- declare an address verified;
- bypass suppression or legal eligibility;
- alter authoritative Company/Contact identity;
- grant live-work/spend authority;
- send automatically.

## Email and Verification

Email candidate generation is bounded and deterministic. Verification owns exact-address acceptance.

A learned/domain pattern may rank candidates; it never proves a mailbox exists.

Live provider use requires applicable deployment/Campaign controls and real credentials.

## Customer readiness projection

The customer-facing high-level state is derived from durable domain state:

- **Processing** — the outbound package is still being prepared;
- **Ready for Sending** — eligible/unsuppressed and the required address, Insights and complete valid seven-message sequence exist;
- **Could not prepare** — a terminal condition prevents production of the outbound package.

Detailed queue status remains separate from this projection.

## Review/edit contract

Human inspection and editing are optional.

- sequence generation does not create fake human approvals;
- absence of a review row means no human action, not a backlog;
- edits create new immutable versions;
- historical versions and real human decisions remain auditable;
- readiness does not depend on an approval click.

## Sending boundary

No automatic send is permitted by this architecture.

Where Gmail draft creation is enabled, it is an explicit user action against current immutable message versions. The customer sends manually.

## Trust boundaries

- source text is evidence, not instructions;
- Research remains sourced;
- Company Intelligence remains bounded and does not become independent citable proof where policy forbids it;
- suppression always wins;
- unknown/provisional/catch-all states remain explicit;
- secrets never enter logs, prompts, browser code or Git history;
- internal failure/recovery state is an Admin concern unless a specific customer-owned input is genuinely required.

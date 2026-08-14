# Current MVP

**Status date:** 15 August 2026

## Authoritative product contract

> **VMR Outbound is autonomous until Ready for Sending.**

The customer creates/configures a Campaign, captures or adds Contacts, and lets VMR prepare them. Internal Agent execution is observable but is not a stage-by-stage customer workflow.

The customer takes over once a Contact reaches **Ready for Sending**.

See [`CUSTOMER_OPERATING_MODEL.md`](CUSTOMER_OPERATING_MODEL.md).

## Customer-visible lifecycle

The primary lifecycle is deliberately small:

- **Processing** — VMR is still preparing the Contact.
- **Ready for Sending** — the usable outbound package has been produced.
- **Could not prepare** — VMR reached a terminal condition and could not produce the package.

Detailed queue, Agent and provider states remain in Admin diagnostics.

## Pipeline

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

`Sending` remains the ninth registered Agent boundary, but there is no automatic production sending adapter.

The nine Agents describe internal execution, not nine customer tasks.

## Current Agent roles

| Agent | Role |
| --- | --- |
| Capture | preserve authorized source observations and permanent Contact intake |
| Identity | converge captures on the correct permanent person |
| Company | link the reusable permanent Company and usable domain |
| Research | continuously enrich reusable Company knowledge with sourced evidence |
| Email | generate bounded address candidates |
| Verification | establish exact-address verification truth under provider policy |
| Insights | derive evidence-backed useful claims from current eligible knowledge |
| Personalization | generate the seven-message Campaign-specific sequence |
| Sending | disabled automatic-send boundary |

## Research and knowledge

Research is reusable across Contacts and Campaigns and may run repeatedly over a Company's lifetime. A later Research run may add facts, structured fields or a newer dossier without invalidating older historical evidence.

Insights selects the current eligible Research/Company knowledge at the start of its execution. Personalization selects the current eligible Research + Insights state at the start of its execution.

Historical versioning and lineage remain valuable for provenance. They answer **what this run used**; they do not require an exact historical predecessor job merely to permit execution.

## Email and Verification

The Email Agent uses the active bounded candidate policy and stops at the first address accepted by the Verification boundary.

Live provider work requires the applicable Campaign/deployment controls and credentials. Provider/model spend consent is configuration, not a recurring customer approval workflow.

## Seven-message sequence

Personalization produces one coherent seven-message sequence in one bounded generation outcome.

Default elapsed days:

`0, 3, 7, 12, 18, 25, 35`

The generated sequence is usable by default when validation succeeds.

A human review row means a human actually acted. The absence of one means no human action occurred; it does **not** mean the sequence is waiting for approval.

Optional edits create new immutable message versions. Historical versions and real human decisions remain auditable.

## Ready for Sending

A Campaign Contact is Ready for Sending when it remains eligible and unsuppressed and the current policy has produced the usable outbound package, including:

- usable Research/Company knowledge;
- an accepted address under Verification policy;
- completed Insights;
- completed Personalization;
- a complete valid seven-message sequence.

Human approval is not part of this readiness calculation.

## Customer surfaces

| Surface | Customer purpose |
| --- | --- |
| `/app` | overview of Campaigns and progress |
| Campaign | Processing / Ready for Sending / Could not prepare plus optional detail |
| Contact | permanent record and generated sequence |
| sequence/message view | optional inspection and immutable editing |
| Knowledge Base | seller/Campaign setup context |

A compatibility Review route may exist, but Review is not a mandatory backlog or progression gate.

## Admin surface

`/admin` is the operational control room for detailed Agent jobs, failures, blocks, retries, providers, controls, resolution internals and recovery.

Machine failure state belongs here rather than being summed into a customer-facing "Needs you" inbox.

## Sending boundary

Automatic sending is not built.

Where Gmail draft creation is enabled, the customer explicitly creates drafts and sends manually. No generation, readiness, default approval state or optional edit triggers a send.

## Current operating choices

- Contacts and Companies are permanent and Campaign-independent.
- Campaign Contact owns Campaign-specific execution/projection state.
- Research is Company-scoped reusable knowledge.
- Suppression and legal/eligibility exclusions remain authoritative.
- Missing evidence stays missing; provider/model output does not fabricate truth.
- Customer intervention is requested only for genuine customer-owned setup/input.
- Internal recovery is an Admin/system concern.

## UAT acceptance

A hosted UAT passes when a real customer can:

1. create/configure a Campaign;
2. capture real authorized Contacts;
3. observe autonomous progress without routine stage intervention;
4. see successful Contacts become Ready for Sending;
5. inspect all seven messages for a ready Contact;
6. optionally edit and preserve immutable version history;
7. manually perform the next sending-related action;
8. see unsuccessful Contacts truthfully as Could not prepare, not as a fabricated personal task queue.

Real UAT must exercise the real website/provider/model boundaries that are enabled for the Campaign.

## Explicitly not required

- human approval before readiness;
- customer clearing of failed/blocked Agent jobs;
- automatic sending;
- automatic follow-up scheduling;
- automatic reply detection;
- a customer-facing Agent control room;
- a generic workflow builder.

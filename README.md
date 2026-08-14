# VMR Outbound Agent

VMR Outbound Agent is a private, contact-first outbound preparation system built around permanent Contacts, reusable Companies, Campaign execution and a durable nine-Agent pipeline.

## Product rule

> **VMR Outbound is autonomous until Ready for Sending.**

A normal customer creates a Campaign, captures or adds Contacts, waits while VMR prepares them, and takes over only when a Contact is **Ready for Sending**.

The customer is not expected to operate the internal Agent pipeline. Failed jobs, retries, provider/model errors and other machine state belong to Admin diagnostics, not to a generic customer task inbox.

See [`docs/CUSTOMER_OPERATING_MODEL.md`](docs/CUSTOMER_OPERATING_MODEL.md).

## Customer flow

```text
Create Campaign
→ Capture / add Contacts
→ Processing
→ Ready for Sending
→ inspect/edit the seven emails if desired
→ perform sending-related actions manually
```

The primary customer-visible outcomes are:

- **Processing**
- **Ready for Sending**
- **Could not prepare**

Detailed Agent state remains observable but is not a list of customer obligations.

## Agent pipeline

1. Capture
2. Identity
3. Company
4. Research
5. Email
6. Verification
7. Insights
8. Personalization
9. Sending

Sending has no automatic production adapter. Gmail draft creation, where enabled, is an explicit customer action and still does not send.

## Ready for Sending

A Contact becomes Ready for Sending when the Campaign Contact remains eligible and unsuppressed and the system has produced the usable outbound package required by policy, including a usable verified address, completed Insights and a validated seven-message sequence.

The default sequence cadence is:

`0, 3, 7, 12, 18, 25, 35` days.

A human approval click is **not** required for readiness. Optional inspection and editing remain available; edits create new immutable versions.

## Research and knowledge

Research is reusable Company knowledge. It may run repeatedly and enrich the Company over time with sourced facts, structured fields and newer dossier versions.

Insights consumes the current eligible Research/Company knowledge available when it runs. Personalization consumes the current eligible Research and Insights state available when it runs. Lineage records what a run used; it is not a predecessor-job gate.

## Product surfaces

| Surface | Purpose |
| --- | --- |
| `/app` | customer workflow: Campaigns, Contacts, progress, Ready for Sending |
| Campaign / Contact sequence views | inspect/edit the seven generated messages |
| `/admin` | diagnostics, Agent controls, failures, retries, provider state and recovery |

A Review route may exist for compatibility, but Review is not a mandatory customer queue and must not control readiness.

## Delivery principle

The project optimizes for the **shortest safe path to real UAT**.

For a narrow fix:

```text
reproduce
→ smallest correct fix
→ focused proof
→ touched-file static checks
→ push
→ GitHub CI
→ merge
→ deploy
→ real UAT
```

Do not duplicate broad CI locally or restart whole-feature reviews for narrow successor repairs without a named risk that requires it.

See [`docs/PROPORTIONAL_VALIDATION.md`](docs/PROPORTIONAL_VALIDATION.md).

## Core objects

- **Contact** — permanent canonical person.
- **Company** — permanent reusable organization.
- **Campaign** — Campaign-specific context and execution controls.
- **Campaign Contact** — Campaign membership and pipeline state.
- **Agent Job** — durable resumable work unit.
- **Company dossier / evidence** — versioned sourced Research knowledge.
- **Email Sequence** — seven logical messages with immutable message versions.
- **Human review/edit records** — audit of real human actions; not a readiness prerequisite.

## Non-negotiable boundaries

- Contact-first ownership: Campaigns never own the permanent Contact.
- Suppressions and legal/eligibility exclusions always win.
- Verification truth comes from the verification boundary, never model assertion.
- Research evidence remains sourced; missing evidence stays missing.
- AI output is bounded and deterministically validated.
- No automatic send authority is implied by generation, readiness, review or editing.
- Secrets never enter source, prompts, logs, screenshots, fixtures or Git history.

## Governing documents

- [`docs/CUSTOMER_OPERATING_MODEL.md`](docs/CUSTOMER_OPERATING_MODEL.md) — authoritative customer workflow and Ready for Sending contract
- [`docs/GOAL.md`](docs/GOAL.md) — current product goal
- [`docs/CURRENT_MVP.md`](docs/CURRENT_MVP.md) — current product boundary and UAT target
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — data, Agent and knowledge architecture
- [`docs/PHASE_2_EXECUTION_MODEL.md`](docs/PHASE_2_EXECUTION_MODEL.md) — durable execution model
- [`docs/EMAIL_SEQUENCE.md`](docs/EMAIL_SEQUENCE.md) — seven-message sequence contract
- [`docs/PROPORTIONAL_VALIDATION.md`](docs/PROPORTIONAL_VALIDATION.md) — UAT-first delivery policy

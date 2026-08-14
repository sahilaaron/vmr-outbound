# Current Goal

Deliver and accept one cohesive Contact-to-Ready-for-Sending MVP.

## Defining outcome

> **An operator can capture an authorized LinkedIn or Sales Navigator prospect and enrol the permanent Contact into a Campaign, and VMR then runs sourced research, exact-address verification, evidence-backed Insights and Personalization on its own until that Contact is Ready for Sending — a generated, validated message sequence held as immutable versions, which the operator may read, edit and send by hand.**

The current MVP ends at Ready for Sending. Nothing in the pipeline waits for a human: the messages are usable the moment they are generated and validated, and reading, editing or recording a decision against a version is optional. Sending stays manual, and the MVP does not include sending-provider submission or outcome synchronization.

## Canonical operating flow

1. Capture a person into a permanent Contact without requiring a Campaign.
2. Resolve Contact identity, Company identity and Company domain honestly.
3. Enrol the permanent Contact into a Campaign explicitly and idempotently.
4. Run deterministic Company research and persist the raw submission, versioned dossier and sourced facts.
5. Generate email candidates in the fixed policy order.
6. Verify one exact address through the durable Verification Agent.
7. Generate derived Insights from persisted evidence through the bounded Claude CLI seam.
8. Generate the Campaign-specific messages through Personalization, each held as an immutable version.
9. Report the Contact as Ready for Sending once the generated messages exist and validate, with no human step in between.
10. Present the evidence and the generated messages for optional reading, editing and an optional recorded decision, and leave the sending itself to a person.

## Product surfaces

- `/` and `/app` — customer-facing application. Today is a compact operational overview: contacts processing, contacts ready for sending, contacts VMR could not prepare, and campaign progress.
- `/app/review` — reached in the customer navigation as **Emails**. A reading surface for the generated messages and their evidence, with optional editing and an optional exact-version decision. It is not a queue to clear.
- `/admin` — Workbench for low-level jobs, controls, retries and authoritative write paths.

The customer application and Workbench share services and models but keep separate presentation layers.

## Locked Agent order

1. Capture
2. Identity
3. Company
4. Research
5. Email
6. Verification
7. Insights
8. Personalization
9. Sending

Sending is registered for compatibility with the durable pipeline but has no production adapter and remains disabled.

## Agent boundaries

- Capture, identity, Company linking, suppression, verification, job state and approval are deterministic authority.
- Research gathers evidence through registered workers and does not use Claude.
- Insights and Personalization use the bounded thinking seam with `allowed_tools=()`.
- No model may verify an address, override suppression, change Agent controls, approve its own draft or send.
- Missing, provisional and insufficient-evidence states remain explicit.

## Email policy

The seeded generic pattern policy begins in this order and stops after the first
verified result:

1. `firstname.lastname`
2. `firstname`
3. `finitiallastname`

Agent Studio may activate a new immutable bounded ordering and may place learned
Company-domain formats first. Employee size does not select or sequence formats.
Each candidate still receives one child Verification job at a time.

The Email Agent enqueues one child Verification Agent Job at a time and resumes from the committed Verification outcome.

## Data ownership

- **Contact** and **Company** are permanent canonical records.
- **Campaign Contact** owns Campaign-specific pipeline state and draft output.
- **Company research** is reusable by permanent Company.
- **Sourced facts** remain separate from derived Insights.
- **DraftVersion** is immutable.
- **Approval** is a real human decision against one exact DraftVersion, recorded only when somebody actually acts. Readiness never waits on it, and it is never fabricated.
- **Suppression** remains authoritative over every downstream stage.

## Current operating choices

The MVP deliberately uses the shortest truthful operating path:

- Campaign enrolment is explicit and reversible through the Workbench, including bulk enrolment.
- Knowledge Base editing remains on `/admin`; the customer interface reads it.
- Capture-domain decisions and suppression creation retain one authoritative admin write path.
- Unsupported features are marked unavailable rather than represented with fake values or controls.

## MVP acceptance criteria

The product is accepted when one operator can:

- merge and run the customer application on top of the Campaign pipeline;
- process one authorized real Contact using real website research, live MillionVerifier and real Claude CLI calls;
- inspect the Company dossier, source evidence, Verification decision and derived Insights;
- see the Contact reach Ready for Sending with no human action, and read the generated messages in `/app/review`;
- optionally edit a message or record a decision against one exact version, and confirm the audit record;
- confirm that no sending side effect exists;
- process a controlled 10–20 Contact batch with understandable retries, failures, blocks and partial outcomes;
- operate `/app` and `/admin` without duplicate canonical records or hidden pipeline state.

Green CI alone does not meet this acceptance gate.

## Explicitly post-MVP

The current MVP does not require:

- SalesHandy or another sending-provider adapter;
- delivery, reply, bounce or opt-out synchronization;
- sending, replies, sequences or analytics backends;
- deterministic fit/confidence scoring;
- Saved Audience criteria and snapshots;
- extension Campaign auto-add;
- multi-email cadence generation;
- draft editing or auto-send;
- autonomous LinkedIn navigation;
- a general workflow builder;
- multi-tenant SaaS.

Post-MVP work must be activated from measured operating evidence rather than allowed to obscure whether the assembled draft-producing product is usable.

## Immediate sequence

1. Merge PR #233 after CI and local route checks.
2. Complete the one-Contact live acceptance.
3. Complete the controlled 10–20 Contact batch.
4. Record an explicit MVP verdict.
5. Only then begin provider sending under #174 and the controlled send-capable pilot under #96.

See [`CURRENT_MVP.md`](CURRENT_MVP.md) for the current implementation map and traceability.

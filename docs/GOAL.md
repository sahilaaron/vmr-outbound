# Current Goal

## Governing outcome

> **VMR Outbound is autonomous until Ready for Sending.**

The product goal is to let a normal customer create a Campaign, capture or add Contacts, and wait while VMR autonomously prepares each Contact until it either becomes **Ready for Sending** or the system truthfully reports that it **Could not prepare** the Contact.

The customer does not operate the internal Agent pipeline and does not clear a generic approval/retry/task queue.

See [`CUSTOMER_OPERATING_MODEL.md`](CUSTOMER_OPERATING_MODEL.md).

## Defining customer flow

```text
Create / configure Campaign
→ Capture or add Contacts
→ VMR processes autonomously
→ Ready for Sending
→ inspect/edit sequence if desired
→ customer performs sending-related actions manually
```

The normal customer responsibilities are limited to:

1. Campaign creation/configuration;
2. Contact capture/import/addition;
3. monitoring progress when useful;
4. optional inspection/editing once messages exist;
5. manual sending-related action after Ready for Sending.

A Campaign may require one explicit setup/consent choice for live paid work. That is Campaign configuration, not stage-by-stage operation.

## Ready for Sending

A Contact is Ready for Sending when the current Campaign Contact is eligible and unsuppressed and the current policy has produced the usable outbound package, including:

- usable Company/Research knowledge;
- a usable address accepted by Verification policy;
- completed Insights;
- completed Personalization;
- exactly seven valid sequence messages.

Default cadence:

`0, 3, 7, 12, 18, 25, 35` days.

Human approval is not required. A generated valid sequence is not a customer backlog merely because nobody reviewed it.

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

The order is execution structure, not a list of user tasks.

## Research model

Research is reusable Company knowledge and may run independently and repeatedly over time.

Each Research run may add sourced facts, structured Company knowledge and newer dossier versions. Historical evidence remains available.

Insights uses the current eligible Research/Company state available when Insights starts. Personalization uses the current eligible Research + Insights state available when Personalization starts. Provenance records what each run used; it must not require one exact historical predecessor execution in order to run.

## Human intervention

The system may request a specific user-owned input only when the customer genuinely must supply or change something, for example missing Campaign configuration or required live-work consent.

Internal failures are not customer work. Failed jobs, blocked jobs, retries, provider errors, model errors and recovery mechanics belong primarily to `/admin` diagnostics.

## Sending boundary

The current product does not automatically send outreach.

Gmail draft creation, where enabled, is explicit and manual. The customer sends manually. Generation, readiness, optional review and optional editing do not grant automatic send authority.

## Acceptance criteria

The product is accepted when a real hosted customer can:

- create/configure a Campaign;
- capture real authorized Contacts through the Chrome extension or supported import/add path;
- observe Contacts progressing without stage-by-stage intervention;
- see successful Contacts reach Ready for Sending;
- inspect the seven-message sequence for a ready Contact;
- optionally edit one message and see immutable version history preserved;
- perform the next sending-related action manually;
- see terminally unsuccessful Contacts as Could not prepare rather than as a manufactured personal task queue;
- use Admin diagnostics separately when operational debugging is necessary.

The real UAT must also prove website Research, live Verification and real Claude CLI work where those capabilities are enabled.

## Non-goals for this milestone

- automatic sending;
- automatic follow-up scheduling;
- automatic reply detection;
- a customer-facing Agent control room;
- a generic human approval queue;
- a generic workflow builder;
- multi-tenant SaaS;
- turning every internal failure into customer work.

## Delivery principle

Use the shortest safe path to real UAT. Narrow fixes get focused proof, touched-file checks, GitHub CI and live UAT. Add broader review/testing only for a named risk or widened blast radius.

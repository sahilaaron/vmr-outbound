# Next Delivery Model

Last updated: 2026-08-15

## Governing customer outcome

The current hosted Beta target is:

> **Create Campaign → Capture/add Contacts → VMR works autonomously → Ready for Sending → customer handles sending manually.**

The customer must not be asked to operate internal Agent stages, clear generic approval queues or repair routine machine failures.

See [`CUSTOMER_OPERATING_MODEL.md`](CUSTOMER_OPERATING_MODEL.md).

## Hosted Beta flow

```text
Authenticated VMR customer
→ create/configure Campaign
→ capture real prospects through VMR Contact Capture or supported import/add path
→ Contacts process through Agents automatically
→ successful Contacts become Ready for Sending
→ customer opens a Contact/sequence
→ sees exactly seven generated messages
→ optionally edits/copies/creates Gmail drafts
→ sends manually outside automatic VMR execution
```

Default sequence cadence:

`0, 3, 7, 12, 18, 25, 35` days.

## Customer responsibility

The customer owns:

- Campaign creation/setup;
- Contact capture/import/addition;
- any explicit Campaign-level consent required for paid/live work;
- optional inspection/editing of generated messages;
- manual sending-related action once ready.

The customer does not own routine Agent retry/recovery, provider/model failure handling or generic pipeline exception triage.

## Machine responsibility

VMR owns the path from captured Campaign Contact to Ready for Sending:

- identity and Company resolution under policy;
- reusable Company Research;
- email discovery;
- exact-address Verification;
- Insights;
- Personalization;
- seven-message sequence validation;
- safe retry/recovery behavior.

If VMR cannot produce the package, the customer-facing result is **Could not prepare**, with optional details. It is not automatically a personal task.

## Research model

Research continuously enriches reusable Company knowledge and may run repeatedly over time.

Insights consumes current eligible Research/Company knowledge when it starts. Personalization consumes current eligible Research + Insights when it starts. Historical versions are provenance, not predecessor-job gates.

## Ready for Sending

A Contact is ready when it remains eligible and unsuppressed and the usable outbound package exists under current policy, including:

- usable Research/Company knowledge;
- an accepted address under Verification policy;
- completed Insights;
- completed Personalization;
- complete validated seven-message sequence.

No human approval click is required.

## Review/edit

Generated messages may be inspected and edited, but this is optional.

A human review row means a person actually acted. Absence of a review row is not a backlog and does not keep a Contact from Ready for Sending.

Edits preserve immutable version history.

## Gmail draft integration

Gmail draft creation is an optional explicit action after messages exist. It is not automatic sending.

The Gmail adapter cannot send. The customer sends manually.

Gmail consent remains separate from VMR sign-in and Chrome extension authentication.

## Automatic sending

Automatic sending, automatic cadence execution, reply detection and automatic follow-up scheduling remain outside the current customer contract.

Do not introduce them as an incidental consequence of Ready for Sending.

## UAT priority

Prove the real hosted path with real Contacts:

1. Campaign setup;
2. extension/import capture;
3. autonomous pipeline progress;
4. Ready for Sending;
5. seven-message inspection/edit/copy or Gmail draft creation;
6. manual sending action.

UAT should also prove that failed/blocked machine state appears as status/diagnostics rather than being inflated into a customer-facing "Needs you" workload.

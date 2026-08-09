# Decision 0007 — Human-controlled Google delivery

Status: Approved product direction

Date: 2026-08-07; reconciled 2026-08-09

## Decision

The current delivery cycle does not build an autonomous Sending Agent.

VMR remains the authoritative system of record and the human remains responsible for sending.

The current cycle adds Google capabilities in two deliberately separate stages:

1. **Google sign-in / Workspace identity** authenticates an internal person to VMR.
2. **Gmail mailbox authorization** separately grants only the Gmail permissions required for operator-triggered draft management.

Google sign-in alone must never imply Gmail mailbox access.

## Current-cycle Gmail slice

The first Gmail capability is intentionally narrow:

```text
operator opens one exact current VMR sequence message/version
→ operator clicks Create Gmail Draft
→ VMR creates or updates one draft in that operator's authorized mailbox
→ VMR stores durable mailbox/draft/message-version lineage
→ operator continues manually in Gmail
```

VMR does not auto-send.

Repeated/retried draft creation must be idempotent and must not create duplicate drafts accidentally.

## Intended current-cycle flow

```text
Campaign Contact
→ Research
→ Company Intelligence
→ Insights
→ Personalization sequence
→ Beta 1 operator UI
→ internal VMR authentication
→ separate Gmail authorization
→ operator-triggered individual Gmail draft
→ human send from Gmail
```

## Deferred

The following are explicitly not required for this cycle:

- automatic cadence scheduling;
- automatic creation of later follow-up drafts;
- sent-message observation;
- reply/thread monitoring;
- automatic suppression/stop transitions driven by Gmail state;
- automatic sending.

These belong to a later human-send Gmail state machine after the first on-demand draft path is accepted.

## Google Sheets

Google Sheets synchronization is no longer a committed current-cycle gate.

The application is the primary operating surface. Sheets may be reconsidered later as a projection if real internal use demonstrates a reporting/collaboration need.

If built, Sheets must never become authoritative for evidence, sequence state, Gmail state, approvals or delivery decisions.

## Consequences

- VMR remains authoritative for evidence, policy, generation versions, sequence/message/version identity and provider lineage.
- Gmail is an external delivery destination, not a source-of-truth store.
- internal user/session ownership becomes a prerequisite for Gmail mailbox ownership;
- Gmail credentials/tokens remain server-side and encrypted;
- the Chrome extension authenticates only to VMR and never receives Google/Gmail tokens;
- no provider action may fabricate sent/delivered state;
- automatic Sending remains deferred.

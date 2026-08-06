# Decision 0007 — Human-controlled Google Workspace delivery

Status: Approved product direction

Date: 2026-08-07

## Decision

The next delivery phase will not build an autonomous Sending Agent.

VMR Outbound will instead generate a complete seven-message outreach sequence, synchronize those messages as Gmail drafts in the assigned internal user's connected Google Workspace mailbox, and synchronize the same sequence and status data to an accessible Google Sheet.

The user remains responsible for sending from Gmail.

## Intended flow

```text
Campaign Contact
→ Research
→ Company Intelligence
→ Insights
→ Personalization sequence
→ Gmail Draft Sync
→ Google Sheets Sync
→ human review and manual send
```

## Consequences

- VMR remains authoritative for evidence, policy, generation versions, review decisions and sync state.
- Gmail and Sheets are external projections, not the source of truth.
- One initial email plus six follow-ups must be generated and versioned as one coherent sequence.
- Internal user accounts, Google OAuth and mailbox ownership become required platform capabilities.
- Gmail synchronization creates or updates drafts only and must never send automatically.
- Automatic Sending remains deferred.
- The deployment target must evolve to an always-on multi-user server before internal rollout.

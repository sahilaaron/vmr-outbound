# Decision 0007 — Human-controlled Google Workspace delivery

Status: Approved product direction, refined 2026-08-08

Original date: 2026-08-07
Refinement date: 2026-08-08

## Decision

VMR Outbound will not build an autonomous Sending Agent for the current delivery path.

The product will generate a complete seven-message outreach sequence while keeping the human in control of delivery.

The 2026-08-08 refinement changes the rollout order. Gmail automation and Google Sheets are no longer prerequisites for the first usable operator experience.

## Current-cycle flow

```text
Campaign Contact
→ Research
→ Company Intelligence
→ Insights
→ seven-message Personalization sequence
→ approved by default
→ optional human inspection/basic edit
→ direct copy/paste from the VMR application
→ optional Campaign XLSX/CSV snapshot
→ VPS-hosted application over HTTPS
→ Sign in with Google
→ separate Gmail mailbox authorization
→ operator creates one selected VMR message as an individual Gmail draft on demand
→ human remains responsible for sending
```

## Permission boundary

Google identity and Gmail mailbox access are separate capabilities:

- Sign in with Google authenticates the internal user to VMR.
- Gmail authorization grants only the mailbox permissions required for the Gmail feature and binds that mailbox to the authenticated VMR user.

## Current-cycle Gmail scope

VMR may create/update a Gmail draft only when the operator explicitly selects a VMR sequence message and invokes the action.

The integration must retain exact Campaign Contact, sequence, message/version, mailbox and Gmail draft lineage; be retryable and idempotent; and never fabricate send state.

VMR must never send automatically.

## Next-cycle Gmail scope

After current-cycle acceptance, the product may automate the human-send workflow:

```text
VMR creates the current actionable draft when due
→ human sends manually
→ VMR observes sent message and thread
→ reply/stop/suppression eligibility is evaluated
→ next same-thread follow-up draft is created only when eligible
```

This later scope requires durable sent/reply/thread state and cadence scheduling.

## Google Sheets

Google Sheets synchronization is not required for the current cycle and is no longer an assumed mandatory stage.

The application UI plus optional Campaign export address the immediate operator handoff need. Revisit live Sheets synchronization only if internal use demonstrates a real collaboration/reporting requirement.

If ever built, Sheets remains an external projection and never the source of truth.

## Consequences

- VMR remains authoritative for evidence, policy, generation versions, edit history and delivery/sync lineage.
- One initial email plus six follow-ups are generated/versioned as one coherent sequence.
- Successful messages are approved by default; humans retain optional inspection and basic edit authority.
- The application UI is the primary operator surface.
- Campaign XLSX/CSV is a convenience snapshot only.
- Internal users and Google sign-in become part of the current-cycle deployment target.
- Gmail draft creation is individual and operator-triggered in the current cycle.
- Automatic cadence, sent/reply monitoring and same-thread follow-up automation move to the next cycle.
- Automatic Sending remains deferred.
- The deployment target is the always-on VPS-hosted application with HTTPS and managed web/worker services.

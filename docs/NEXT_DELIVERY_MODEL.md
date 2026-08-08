# VMR Outbound Agent — Next Delivery Model

Last updated: 2026-08-08

## Locked scope for the current delivery cycle

The current cycle is now fixed to the following outcome:

```text
1. Finish/cross-review/merge IMP-001 and Production Hardening
2. Reconcile and merge the seven-message Personalization sequence
3. Build the Beta 1 operator UI
   - seven messages visible together
   - approved by default
   - optional basic versioned edit
   - Copy subject / Copy body / Copy email
4. Add optional Campaign XLSX/CSV export
5. Publish/deploy the merged application to the VPS staging runtime
6. Run Claude CLI/background workers on the VPS service runtime
7. Put HTTPS + internal authentication in front of the application
8. Add Sign in with Google / Google Workspace identity
9. Add Gmail mailbox authorization for the connected user
10. Allow the operator to create one selected VMR message as an individual Gmail draft on demand
11. Run real end-to-end internal acceptance with the first operator
```

This is the target for the current cycle. Full Gmail cadence automation, sent/reply monitoring and automatic creation of later follow-up drafts are **not** required to declare this cycle complete.

## Product decision

For every Campaign Contact that passes Personalization, VMR produces one coherent seven-message sequence. Successful generated messages are considered approved by default because mandatory human review is not operationally realistic at current output quality. A human may still inspect and make a basic edit. Rich rewrite/regenerate controls can come later.

The **application itself is the primary Beta 1 operating surface**:

```text
Campaign
→ eligible Contacts
→ seven-message sequence
→ approved by default
→ optional inspection/basic edit
→ operator copies subject/body/full email directly from the VMR UI
→ operator uses the team's existing sending platform
```

The Campaign XLSX/CSV download is an additional convenience capability only. It is not the primary workflow and is not authoritative.

The application remains authoritative for evidence, generation, versions and sequence state.

## Google identity versus Gmail mailbox access

These are related but distinct capabilities and must remain separate in the implementation:

- **Sign in with Google / Google Workspace OAuth identity** authenticates the internal user to VMR.
- **Gmail mailbox authorization** grants only the Gmail permissions needed by the delivery feature and associates the connected mailbox with that authenticated VMR user.

Do not treat Google sign-in alone as Gmail authorization.

For this cycle, the Gmail slice is deliberately narrow:

```text
operator views one VMR sequence message
→ clicks Create Gmail Draft
→ VMR creates or updates that one selected message as a draft in the operator's authorized mailbox
→ VMR stores durable draft/mailbox/version lineage
→ operator continues working manually
```

No automatic send authority is introduced.

## Current engineering state

PR #241 is merged. IMP-001 Campaign Contact File Import and Production Hardening are in successor repair after independent adversarial review. A seven-message Personalization sequence implementation exists but must be reconciled again after final IMP-001 acceptance. The Ubuntu VPS staging foundation is prepared and reboot-validated; application deployment waits for stable application branches.

See `DELIVERY_RECONCILIATION_2026_08_08.md` for exact branch sequencing.

## Immediate delivery order

### 1. Finish and independently accept IMP-001 and Production Hardening

Complete both successor repairs, cross-review them independently, publish the accepted exact heads, obtain exact-head CI and merge.

Imported email truth remains non-negotiable: supplied by file is not discovered and is not provider-verified.

Production readiness must recover after transient dependency failure without process restart.

### 2. Reconcile and publish the seven-message sequence

After final IMP-001 merges, reconcile SEQ-001 against the final import migration/read-model behavior and the current merged application.

Produce one coherent versioned sequence per Campaign Contact:

1. Initial personalized email
2. Follow-up 1 — concise reminder
3. Follow-up 2 — new evidence or market angle
4. Follow-up 3 — role-specific relevance
5. Follow-up 4 — proof or value angle
6. Follow-up 5 — low-friction resource offer
7. Follow-up 6 — polite close-the-loop

Beta approval rule:

- successful generated messages are **approved by default**;
- human review is optional, not a required gate;
- a human can inspect all seven messages;
- a human can perform a basic edit;
- edits preserve auditable version history rather than silently overwriting generated text;
- richer rewrite/regenerate controls are deferred.

### 3. Build the Beta 1 operator UI

The application UI is the primary handoff surface.

For each Campaign Contact that has completed Personalization, the operator must be able to:

- see all seven generated messages together in a clear sequence view;
- see step number/purpose and recommended timing;
- see subject and body for each message;
- copy the subject directly;
- copy the body directly;
- copy subject + body/full email directly where useful;
- make a basic edit while preserving version history;
- see whether the current text is generated or edited;
- move between Contacts without opening seven separate review records;
- understand that the sequence is approved by default unless they choose to intervene.

Copy actions must be obvious and low-friction.

### 4. Add Campaign workbook export as an optional convenience

Each Campaign may provide a downloadable XLSX snapshot generated from current authoritative application data.

Preferred first shape: one row per Campaign Contact, with identity/company columns followed by seven message groups.

At minimum include Contact, Company, truthful email status/origin, sequence/version identifiers, all seven subjects/bodies, recommended timing and useful generated/edited version metadata.

Export requirements:

- Campaign-scoped;
- deterministic and repeatable;
- no duplicate rows for the same Campaign Contact;
- spreadsheet-formula safe;
- readable in Excel and equivalent software;
- download does not mutate sequence state;
- snapshot only, never a live synchronization target;
- no Gmail/Google dependency.

CSV may be secondary. XLSX is preferred for usability with long message bodies.

### 5. Deploy the merged Beta 1 application to the VPS

Publish/review the staging-foundation branch and deploy the final merged application.

Validate:

- staging PostgreSQL migrations;
- managed `vmr-web` and `vmr-worker` services;
- Claude CLI/background agent runtime under the dedicated VPS service account;
- logs and backups;
- `/healthz` and `/readyz`;
- restart/reboot recovery;
- smoke tests;
- Nginx/reverse-proxy behavior;
- HTTPS on the chosen application hostname.

The same VPS-hosted VMR application serves the operator UI; a second frontend is not required for this cycle.

### 6. Add internal authentication and Google sign-in

Introduce the minimum internal-user/account model required for the first operator.

Provide **Sign in with Google** / Google Workspace OAuth identity. Authorization remains application-owned: signing into Google does not by itself authorize Gmail mailbox access.

Restrict initial access to explicitly approved internal users/domain policy as appropriate.

### 7. Add the first Gmail slice — individual on-demand draft creation

Allow an authenticated operator with an authorized Gmail mailbox to select an individual VMR sequence message and create it as a Gmail draft.

Requirements:

- explicit user/mailbox ownership;
- least-privilege Gmail OAuth scope appropriate to draft creation/management;
- encrypted refresh-token/credential storage;
- durable Gmail draft ID;
- VMR Campaign Contact + sequence/message/version lineage;
- idempotency: repeated action does not create duplicate drafts accidentally;
- operator-visible success/failure state;
- safe retry behavior;
- deleting/recreating a draft must not fabricate send state;
- VMR must never auto-send.

For this cycle, automatic cadence scheduling, sent-message observation, replies and automatic same-thread follow-ups remain deferred.

### 8. Real end-to-end internal acceptance

The first operator should be able to:

1. reach the HTTPS-hosted VMR application;
2. sign in with Google;
3. open a Campaign;
4. find Contacts that passed Personalization;
5. view all seven messages;
6. optionally edit a message;
7. copy any message directly from the UI;
8. optionally download the Campaign workbook;
9. connect/authorize their Gmail mailbox;
10. click one selected message and create an individual Gmail draft;
11. open Gmail and see the correct draft with correct lineage/state retained in VMR.

Passing this workflow is the acceptance target for this cycle.

## Next cycle — Gmail delivery automation

After the current cycle is stable, build the full human-send Gmail state machine:

```text
VMR creates current actionable draft at cadence
→ human sends manually
→ VMR observes sent message and thread ID
→ reply/stop/suppression state is evaluated
→ next follow-up draft is created in the same Gmail thread only when eligible
```

This later slice requires durable sent/reply/thread state, cadence scheduling and explicit stop conditions.

## Google Sheets status

Google Sheets synchronization is not required for the current cycle and is not currently a committed delivery gate.

The application UI + optional XLSX export now cover the immediate operator handoff need. Revisit live Sheets synchronization only if internal use demonstrates a collaboration/reporting requirement that neither the application nor export serves well.

If built later, Sheets remains a projection only and never becomes authoritative for sequence state, Gmail state, evidence, approvals or delivery decisions.

## Deferred but recorded

- automatic sending;
- automatic Gmail cadence/follow-up creation;
- Gmail sent/reply/thread monitoring beyond what is required for later automation;
- rich AI rewrite/regenerate controls;
- Google Sheets live synchronization unless a concrete need survives internal use;
- broad provider sending infrastructure;
- Broadcast Campaign mode;
- broader CRM/workflow expansion.

## Non-negotiable product boundaries

- successful Personalization sequences are approved by default, while humans retain optional inspection/edit authority;
- edits preserve version history;
- the application UI is the primary operating surface;
- exports are convenience snapshots only;
- Google identity and Gmail mailbox authorization remain separate permission boundaries;
- Gmail draft creation is human-invoked in this cycle and must never auto-send;
- Company Intelligence remains company-scoped;
- Personalization cannot treat classifications as proof independent of Research evidence;
- imported email cannot be represented as provider-verified email;
- exported spreadsheet cells must be safe against formula execution while source evidence remains unchanged in the database;
- Gmail integrations must be durable, auditable, retryable and idempotent;
- no external provider action may fabricate verification, delivery or send status;
- new work must preserve immutable evidence and exact generation lineage.

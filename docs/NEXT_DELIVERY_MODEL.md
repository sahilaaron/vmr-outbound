# VMR Outbound Agent — Next Delivery Model

Last updated: 2026-08-08

## Product decision

The immediate beta does **not** wait for Gmail integration.

For every Campaign Contact that passes Personalization, VMR should produce one coherent seven-message sequence. Those messages are considered approved by default because mandatory human review is not operationally realistic at current quality. A human may still inspect and make a basic edit. Rich rewriting assistance can come later.

The **application itself is the primary Beta 1 delivery surface**:

```text
Capture / Import
→ Identity
→ Company
→ Research
→ Company Intelligence
→ Insights
→ Personalization
→ seven-message sequence
→ approved by default
→ optional human review/basic edit
→ operator views all seven messages in the application
→ operator copies subject/body directly from the UI into the existing sending platform
→ operator tracks sending manually for Beta 1
```

A Campaign XLSX/CSV download is an additional convenience capability, not the main workflow and not a Beta 1 source of truth.

The application remains authoritative for evidence, generation, versions and sequence state.

Beta 2 adds Gmail automation:

```text
approved sequence
→ VMR creates current actionable Gmail draft
→ human sends manually
→ VMR observes sent/reply state
→ next same-thread follow-up is created at the configured cadence only while still eligible
```

Google Sheets is not required for Beta 1 or Beta 2. Revisit it only if a live shared spreadsheet provides operational value beyond the application and optional export.

## Current engineering state

PR #241 is merged. IMP-001 Campaign Contact File Import and Production Hardening are in successor repair after independent adversarial review. A seven-message Personalization sequence implementation exists but must be reconciled again after final IMP-001 acceptance. The Ubuntu VPS staging foundation is prepared and reboot-validated; application deployment waits for stable application branches.

See `DELIVERY_RECONCILIATION_2026_08_08.md` for exact branch sequencing.

## Immediate delivery order

### 1. Finish and independently accept IMP-001

Complete the successor repair, perform final independent review, publish the accepted exact head to PR #242, obtain exact-head CI and merge.

Imported email truth remains non-negotiable: supplied by file is not discovered and is not provider-verified.

### 2. Reconcile the seven-message sequence and change the approval contract

After final IMP-001 merges, reconcile SEQ-001 against the final import migration/read-model behavior.

Produce one coherent versioned sequence per Campaign Contact:

1. Initial personalized email
2. Follow-up 1 — concise reminder
3. Follow-up 2 — new evidence or market angle
4. Follow-up 3 — role-specific relevance
5. Follow-up 4 — proof or value angle
6. Follow-up 5 — low-friction resource offer
7. Follow-up 6 — polite close-the-loop

New beta approval rule:

- successful generated messages are **approved by default**;
- human review is optional, not a required gate;
- a human can inspect all seven messages;
- a human can perform a basic edit;
- an edit must create/preserve auditable version history rather than silently overwrite generated text;
- richer rewrite/regenerate controls are deferred.

### 3. Build the Beta 1 sequence operating UI

The application UI is the primary handoff surface.

For each Campaign Contact that has completed Personalization, the operator must be able to:

- see all seven generated messages together in a clear sequence view;
- see step number/purpose and recommended timing;
- see the subject and body for each message;
- copy the subject directly;
- copy the body directly;
- preferably copy subject + body together where useful;
- make a basic edit while preserving version history;
- see whether the current text is generated or edited;
- move between Contacts without opening seven separate review records;
- understand that the sequence is approved by default unless they choose to intervene.

The copy action should be obvious and low-friction. Beta 1 assumes operators manually paste the messages into their existing sending platform.

### 4. Add Campaign workbook export as a convenience capability

Each Campaign may also provide a downloadable XLSX snapshot generated from current authoritative application data.

Preferred first shape: **one row per Campaign Contact**, with identity/company columns followed by seven message groups.

At minimum include:

- Contact name;
- Company;
- email address and truthful email-origin/verification status;
- sequence/version identifiers;
- Message 1 subject/body through Message 7 subject/body;
- recommended delay/timing for each follow-up where available;
- generated/edited indicator and useful version metadata.

Export requirements:

- Campaign-scoped;
- deterministic and repeatable;
- no duplicate rows for the same Campaign Contact;
- safe spreadsheet rendering / formula neutralization;
- readable in Excel and equivalent spreadsheet software;
- download must not mutate sequence state;
- workbook is a snapshot, not a live sync target;
- no Gmail, Google OAuth or Google Sheets dependency.

CSV may be a secondary format, but XLSX is preferred because seven long email bodies are easier to operate in a formatted workbook.

This export is an **add-on**. Beta 1 remains usable from the application UI without downloading anything.

### 5. Finish and independently accept Production Hardening

The production branch must pass the external-review readiness recovery attack before publication. One stalled dependency probe must never poison process readiness for life.

After final review, publish as a draft PR, obtain exact-head CI and merge.

### 6. Publish the VPS staging foundation and deploy Beta 1 to staging

Deploy the merged application only after application hardening contracts are stable. Validate web/worker services, staging PostgreSQL migrations, backups, logs, `/healthz`, `/readyz`, restart/reboot behavior and smoke checks.

### 7. Beta 1 internal launch

Operators should be able to:

- open a Campaign;
- see which Contacts completed Personalization;
- open a Contact's seven-message sequence;
- copy each message directly from the application into the existing sending platform;
- optionally make a basic edit;
- optionally download the Campaign workbook;
- manually track campaign sending progress for this first beta.

This is the immediate delivery target.

### 8. Beta 2 — internal users, Google Workspace and Gmail delivery state

After Beta 1 is stable, add internal application users, organization/workspace membership, Google OAuth and mailbox ownership.

Then build Gmail integration around the human-send contract:

1. VMR creates or updates only the current actionable draft.
2. A human sends manually from Gmail.
3. VMR observes the sent message and records message/thread IDs.
4. When cadence permits and no reply/stop/suppression applies, VMR creates the next follow-up in the same thread.
5. A reply, suppression or operator stop holds future steps.

### 9. Google Sheets decision checkpoint

Do not build Google Sheets synchronization merely because it appeared in the earlier roadmap.

After Beta 1 application use and Beta 2 Gmail use are tangible, decide whether a live Google Sheet solves a remaining collaboration/adoption problem. If not, defer it.

If ever built, Sheets remains a projection only and never becomes authoritative for sequence state, Gmail state, evidence, approvals or delivery decisions.

## Deferred but recorded

- automatic sending;
- rich AI rewrite/regenerate controls for already-generated sequence messages;
- Google Sheets live synchronization unless a concrete need survives Beta 1/2;
- broad provider sending infrastructure;
- Broadcast Campaign mode;
- broader CRM/workflow expansion.

## Non-negotiable product boundaries

- successful Personalization sequences are approved by default for Beta 1, but humans retain optional inspection/edit authority;
- edits preserve version history;
- the application UI is the primary Beta 1 operating surface;
- exports are convenience snapshots only;
- Company Intelligence remains company-scoped;
- Personalization cannot treat classifications as proof independent of Research evidence;
- imported email cannot be represented as provider-verified email;
- exported spreadsheet cells must be safe against formula execution while source evidence remains unchanged in the database;
- Gmail integration, when built, must be durable, auditable, retryable and idempotent;
- replies, suppression and explicit stop conditions must prevent later Gmail sequence steps;
- no external provider action may fabricate verification, delivery or send status;
- new work must preserve immutable evidence and exact generation lineage.

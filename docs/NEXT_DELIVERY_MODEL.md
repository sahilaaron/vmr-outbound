# VMR Outbound Agent — Next Delivery Model

Last updated: 2026-08-07

## Product decision

VMR Outbound will not prioritize an autonomous Sending Agent for the next delivery phase.

The near-term product should prepare a complete, human-controlled outreach package:

```text
Capture / Import
→ Identity
→ Company
→ Research
→ Company Intelligence
→ Insights
→ Personalization
→ seven-message email sequence
→ Gmail draft synchronization
→ Google Sheets synchronization
→ human review and manual sending from Gmail
```

The application remains the system of record for evidence, generation, versions, review decisions, sync status and audit. Gmail and Google Sheets are delivery destinations, not source-of-truth stores.

## Current merge candidate

PR #241 completes the Admin Workbench redesign and the Company Intelligence operating-model corrections:

- Research automatically queues one company-scoped Company Intelligence job;
- the standard Agent worker fleet consumes Company Intelligence work when Campaign work is idle;
- Company Intelligence remains company-scoped and is not duplicated per Contact;
- Personalization consumes only eligible current Company Intelligence as bounded, non-citable context;
- Research remains authoritative and Personalization Policy Studio remains the wording authority;
- exact Company Intelligence lineage is recorded on Personalization outputs;
- Sending remains unavailable.

## Immediate build order

### 1. Seven-message Personalization sequence

Produce one versioned sequence per Campaign Contact:

1. Initial personalized email
2. Follow-up 1 — concise reminder
3. Follow-up 2 — new evidence or market angle
4. Follow-up 3 — role-specific relevance
5. Follow-up 4 — proof or value angle
6. Follow-up 5 — low-friction resource offer
7. Follow-up 6 — polite close-the-loop

The sequence must be coherent as one unit. Follow-ups must know what earlier messages already said, avoid repetitive claims and CTAs, retain evidence lineage and remain governed by the active Personalization policy.

UI direction:

- `/app/review`: one compact Contact card with a sequence count and expandable message selector rather than seven full cards;
- `/app/contacts/{contact_id}?campaign={campaign_id}`: a tabular sequence view with step, purpose, recommended delay, subject, status and expandable body/lineage detail.

### 2. Campaign-bound Apollo file import

Restore XLSX/CSV import for Apollo-style datasets. Imported Contacts must be aligned to a selected Campaign and enter the existing Agent pipeline.

For imported primary email addresses:

- accept the supplied address with truthful import provenance;
- bypass candidate generation;
- bypass MillionVerifier/external verification through an explicit durable outcome;
- do not label the address VMR-verified or guaranteed deliverable;
- continue through Research, Company Intelligence, Insights and Personalization.

### 3. User accounts and Google Workspace connection

Introduce internal application users, organization/workspace membership and per-user Google Workspace mailbox connections.

Requirements include Google OAuth, mailbox ownership, encrypted token storage, reconnect/disconnect, Campaign/mailbox assignment and clear authorization boundaries.

### 4. Gmail Draft Sync

Create or update Gmail drafts only. Do not send automatically.

Each synchronized message must retain sequence position, VMR sequence/version lineage, Gmail draft ID, mailbox, sync status, retry state and failure reason. Duplicate draft creation must be prevented.

### 5. Google Sheets synchronization

Create or connect a Campaign Sheet with one row per sequence message, including Campaign, Contact, Company, mailbox, sequence/message version, subject, body, recommended send timing, Gmail draft ID/link, review status and sync status.

Synchronization must be idempotent: the same sequence version and message position updates the existing row rather than appending duplicates.

### 6. Always-on server deployment

Move from a single-user local Windows runtime toward an always-on Ubuntu VPS suitable for several internal users.

The deployment foundation should include managed web/worker services, PostgreSQL backups, HTTPS, health/readiness checks, logging, secure secrets, Claude CLI under a dedicated service account and future Google OAuth callback support.

## Deferred but recorded

- customer-facing Company Intelligence presentation/navigation improvements;
- proper Gmail-thread follow-up creation after prior messages are actually sent;
- automatic sending;
- provider outcome/reply synchronization;
- deterministic scoring and Saved Audiences;
- Broadcast Campaign mode.

## Non-negotiable product boundaries

- human control remains at review and send time;
- Company Intelligence remains company-scoped;
- Personalization cannot treat classifications as proof independent of Research evidence;
- Gmail/Sheets integrations must be durable, auditable, retryable and idempotent;
- no external provider action may fabricate verification, delivery or send status;
- new work must preserve immutable evidence and exact generation lineage.

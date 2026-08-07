# VMR Outbound Agent — Next Delivery Model

Last updated: 2026-08-08

## Product decision

VMR Outbound will not prioritize an autonomous Sending Agent for the next delivery phase.

The near-term product remains human-controlled:

```text
Capture / Import
→ Identity
→ Company
→ Research
→ Company Intelligence
→ Insights
→ Personalization
→ seven-message email sequence
→ current actionable Gmail draft
→ human manual send from Gmail
→ sent/reply observation
→ next same-thread follow-up only when still eligible
```

The application remains the system of record for evidence, generation, versions, review decisions, delivery state, sync state and audit. Gmail is the human-controlled send surface. Google Sheets is no longer assumed to be a mandatory delivery stage; it is a product decision checkpoint described below.

## Current merged baseline

PR #241 has merged. The merged product includes the Admin Workbench redesign, automatic Research → Company Intelligence handoff, shared-worker Company Intelligence dispatch and bounded Company Intelligence context for Personalization. Sending remains unavailable.

Two successor branches are currently under repair after independent adversarial reviews:

- IMP-001 Campaign Contact File Import;
- Production Hardening.

A seven-message Personalization sequence implementation also exists, but its current reconciliation includes an older IMP-001 head and therefore must be reconciled again after final IMP acceptance.

The Ubuntu VPS staging foundation is prepared and reboot-validated, but application deployment intentionally waits for the final application branches.

See `DELIVERY_RECONCILIATION_2026_08_08.md` for the current exact sequencing and branch gates.

## Immediate delivery order

### 1. Finish and independently accept IMP-001

Complete the successor repair, perform final independent review, publish the accepted exact head to PR #242, obtain exact-head CI and merge.

Imported email truth remains non-negotiable: supplied by file is not discovered and is not provider-verified.

### 2. Reconcile and publish the seven-message Personalization sequence

After final IMP-001 merges, rebuild the sequence reconciliation against the final import migration/read-model behavior.

Produce one coherent versioned sequence per Campaign Contact:

1. Initial personalized email
2. Follow-up 1 — concise reminder
3. Follow-up 2 — new evidence or market angle
4. Follow-up 3 — role-specific relevance
5. Follow-up 4 — proof or value angle
6. Follow-up 5 — low-friction resource offer
7. Follow-up 6 — polite close-the-loop

The sequence must preserve evidence lineage, immutable versions, review history and stop conditions.

### 3. Finish and independently accept Production Hardening

The production branch must pass the external-review readiness recovery attack before publication. One stalled dependency probe must never poison process readiness for life.

After final review, publish as a draft PR, obtain exact-head CI and merge.

### 4. Publish the VPS staging foundation and deploy the reconciled application

Keep infrastructure changes separated from application/domain code. Deploy the merged application to the Ubuntu staging VPS only after the application hardening contracts are stable.

Validate managed web/worker services, staging PostgreSQL migrations, backups, logs, `/healthz`, `/readyz`, restart/reboot behavior and smoke checks.

### 5. Internal users and Google Workspace connection

Introduce internal application users, organization/workspace membership and per-user Google Workspace mailbox connections.

Requirements include Google OAuth, mailbox ownership, encrypted token storage, reconnect/disconnect and Campaign/mailbox authorization boundaries.

### 6. Gmail delivery-state integration

Do not create all seven Gmail drafts at once by default.

The intended operating model is:

1. VMR creates or updates only the current actionable draft.
2. A human reviews/sends manually from Gmail.
3. VMR observes the sent message and preserves the Gmail message/thread identifiers.
4. If no reply/stop/suppression condition applies and cadence permits, VMR creates the next follow-up in the same thread.
5. A reply, suppression, operator stop or other terminal condition holds all future steps.

The implementation therefore needs durable Gmail draft IDs, message IDs, thread IDs, mailbox ownership, sync/retry state and explicit delivery-state transitions.

### 7. Internal multi-user staging pilot

Use the application as the primary operating surface with a small internal group. Validate whether users can understand Campaign progress, review sequences, see delivery state and resolve failures without needing a separate operational spreadsheet.

### 8. Google Sheets decision checkpoint

Do **not** build Google Sheets synchronization only because it appeared in the earlier roadmap.

The original reason for Sheets was adoption: internal users already understand spreadsheets and may not understand what the application does in the background.

After the application-native multi-user/Gmail workflow is tangible, decide between:

- **Application-native operations view:** preferred if the app gives users all required visibility with lower sync complexity;
- **Google Sheets projection:** build only if a familiar Sheet materially reduces adoption friction or supports an operational workflow the app does not yet serve well.

If built, Sheets remains read/projection infrastructure, never the source of truth. Sync must be idempotent and keyed to durable application entities/versions.

## Deferred but recorded

- automatic sending;
- broad provider sending infrastructure;
- customer-facing Company Intelligence presentation/navigation improvements;
- Broadcast Campaign mode;
- broader CRM/workflow expansion;
- Google Sheets synchronization unless the decision checkpoint demonstrates a real need.

## Non-negotiable product boundaries

- human control remains at review and send time;
- Company Intelligence remains company-scoped;
- Personalization cannot treat classifications as proof independent of Research evidence;
- imported email cannot be represented as provider-verified email;
- Gmail integration must be durable, auditable, retryable and idempotent;
- replies, suppression and explicit stop conditions must prevent later sequence steps;
- no external provider action may fabricate verification, delivery or send status;
- new work must preserve immutable evidence and exact generation lineage.

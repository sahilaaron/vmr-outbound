# VMR Outbound Agent — Delivery Reconciliation

Status: Current coordination record
Date: 2026-08-08
Authoritative engineering baseline: current remote `main`

This document records the current delivery state while reviewed successor branches remain unmerged. It does not promote branch-only behavior to merged-product truth.

## Locked current-cycle outcome

The current cycle is complete only when the first internal operator can perform this end-to-end flow:

```text
HTTPS-hosted VMR application on VPS
→ Sign in with Google
→ open Campaign
→ view Contacts that passed Personalization
→ view all seven generated emails for a Contact
→ messages approved by default
→ optionally make a basic versioned edit
→ copy subject/body/full email directly from the VMR UI
→ optionally download a Campaign XLSX/CSV snapshot
→ authorize the operator's Gmail mailbox
→ click one selected VMR message
→ create one individual Gmail draft on demand
→ open Gmail and see the correct draft
```

Automatic Gmail cadence, sent/reply monitoring and automatic creation of future follow-up drafts are **next-cycle work**, not current-cycle acceptance requirements.

Google Sheets is not required for this cycle.

## Current merged product

The merged application already provides the contact-first Campaign pipeline, v2 customer UI, Admin Workbench, Agent Studio, Company Intelligence, Research → Company Intelligence handoff, Insights and evidence-backed Personalization. Sending remains unavailable.

PR #241 is merged.

## Work currently in flight

### IMP-001 — Campaign Contact File Import

Branch: `feat/campaign-contact-file-import`

The first repair head `16b16f986f56f5c65064f9f5b9a320e6fdeb82da` failed its second independent adversarial review. The successor repair is focused on restatement evidence durability, spreadsheet-formula projection safety, blank-header width/provenance handling, strict malformed-CSV rejection, database truth constraints and stronger regression tests.

Status: **blocked until the successor repair passes final independent review**.

PR #242 must remain draft and must not merge the older remote bytes.

### Production Hardening

Branch: `feat/production-hardening`

The reviewed head `d99e323e3332d7f9c162a046e8a33ad60dbeab9f` failed independent external review because one stuck readiness worker can permanently poison `/readyz` for the lifetime of the process. The successor repair is focused on self-healing bounded readiness plus associated timeout, compatibility, documentation and test corrections.

Status: **blocked until the successor repair passes final independent review**.

### Seven-message Personalization sequence

The sequence implementation already exists as an unpublished/reconciled branch, but its current reconciliation includes the older IMP-001 bytes.

Status: **implemented but not publishable yet**.

After final IMP-001 is accepted and merged, SEQ-001 must be reconciled again against the final IMP migration/read-model behavior. That reconciliation must also change the operating contract from mandatory review to approved-by-default with optional basic edit.

### VPS staging foundation

The Ubuntu staging foundation has passed reboot validation. The corrected publication branch `chore/vps-staging-foundation-current-main` has a validated bundle/handoff. The server itself remains intentionally without the application deployment.

Status: **staging foundation ready; application deployment waiting on final application branches**.

## Sequential path from here to current-cycle delivery

1. Finish the two active successor repair passes: IMP-001 and Production Hardening.
2. Cross-review both repaired heads independently. No branch moves forward on builder self-certification alone.
3. Publish accepted exact heads, obtain exact-head CI and merge the repaired work.
4. Reconcile SEQ-001 against final merged IMP behavior and the current application.
5. Independently review the sequence reconciliation and merge it.
6. Build the Beta 1 operator experience:
   - all seven messages visible together;
   - approved by default;
   - optional basic edit with version history;
   - clear Copy subject, Copy body and Copy full email actions;
   - easy movement between Campaign Contacts.
7. Add Campaign XLSX/CSV export as a convenience snapshot only. It must be read-only, Campaign-scoped, deterministic and spreadsheet-formula safe.
8. Publish/review the corrected VPS staging-foundation branch.
9. Deploy the merged application to the VPS staging runtime. Validate PostgreSQL migrations, `vmr-web`, `vmr-worker`, Claude CLI/background-agent runtime, logs/backups, `/healthz`, `/readyz`, restart/reboot, smoke checks, Nginx and HTTPS.
10. Add the minimum internal-user/authentication model and **Sign in with Google**.
11. Add separate Gmail mailbox authorization for the authenticated operator. Google identity alone must not imply Gmail access.
12. Add the first Gmail slice: from a selected sequence message, the operator can create/update one Gmail draft on demand. Persist mailbox, draft ID and exact VMR sequence/message/version lineage; retries must not create duplicates; VMR never sends automatically.
13. Run real end-to-end internal acceptance with the first operator using the locked flow at the top of this document.

## Current-cycle UI contract

The VMR application is the primary operating surface. XLSX/CSV is only an additional convenience.

For each Campaign Contact that has passed Personalization, the user should be able to see all seven messages in one understandable sequence experience. Each message should expose obvious copy actions and basic edit capability without forcing seven independent approval steps.

The copy/paste path must remain usable even if Google/Gmail is temporarily unavailable.

## Google identity and Gmail boundary

Two permission boundaries exist:

- **Google sign-in** authenticates the person to VMR.
- **Gmail authorization** grants the Gmail API permissions needed for draft management and binds that mailbox to the VMR user.

Current-cycle Gmail scope is deliberately narrow: individual, operator-triggered draft creation/management. It does not include automatic cadence, inbox/reply monitoring or automatic follow-up creation.

## Next cycle — Gmail-assisted sequence automation

After current-cycle acceptance, build:

```text
VMR schedules current actionable draft
→ human sends manually
→ VMR observes sent message/thread
→ reply/stop/suppression eligibility is evaluated
→ next same-thread follow-up draft is created automatically when due
```

This requires durable sent/reply/thread state, cadence scheduling and explicit stop transitions.

## Google Sheets status

Google Sheets synchronization is **not a current-cycle prerequisite** and is **not currently a committed delivery gate**.

The original rationale was familiarity for internal users. The application-first UI, direct copy actions and optional Campaign workbook address that immediate need more cheaply.

Revisit live Sheets synchronization only if internal use demonstrates a collaboration/reporting problem that neither the application nor export solves.

If retained later, Sheets is a projection only. It must never become the source of truth for sequence state, Gmail state, evidence, approvals or delivery decisions.

## Documentation rule while branches are unmerged

Repository architecture/behavior docs should describe merged reality and clearly label branch-only work as in-flight. The Build Tracker may describe current blockers and next management actions. Branch handoffs/reviews remain acceptance evidence, not merged-product truth.

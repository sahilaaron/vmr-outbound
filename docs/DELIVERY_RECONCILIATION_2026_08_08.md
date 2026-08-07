# VMR Outbound Agent — Delivery Reconciliation

Status: Current coordination record
Date: 2026-08-08
Authoritative engineering baseline: current remote `main`

This document records the current delivery state while reviewed successor branches remain unmerged. It does not promote branch-only behavior to merged-product truth.

## Immediate product target — Beta 1

The first internal beta should **not wait for Gmail integration**.

A Campaign Contact that passes Personalization should have one generated seven-message sequence. Successful messages are approved by default. Human review is optional; operators can inspect all seven messages and make a basic auditable edit if required.

The **application UI is the primary Beta 1 operating surface**:

```text
Campaign
→ Contacts that reached Personalization
→ seven-message sequences
→ approved by default
→ optional inspection/basic edit
→ copy subject/body directly from the application
→ paste into the team's existing sending platform
→ operator tracks sending manually for Beta 1
```

A Campaign XLSX/CSV download is a convenience add-on only. It is not the primary workflow and is not authoritative.

No Google account, Gmail API or Google Sheets synchronization is required for Beta 1.

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

After final IMP-001 is accepted and merged, SEQ-001 must be reconciled again against the final IMP migration/read-model behavior. That reconciliation must also change the beta approval contract from mandatory human approval to approved-by-default with optional review/basic edit, and must provide the application-first copy/paste UI.

### VPS staging foundation

The Ubuntu staging foundation has passed reboot validation. The corrected publication branch `chore/vps-staging-foundation-current-main` has a validated bundle/handoff. The server itself remains intentionally without the application deployment.

Status: **staging foundation ready; application deployment waiting on final application branches**.

## Sequential path from here to Beta 1 delivery

1. Finish the two active successor repair passes: IMP-001 and Production Hardening.
2. Cross-review both repaired heads independently. No branch moves forward on builder self-certification alone.
3. Publish the accepted IMP-001 head to PR #242, obtain exact-head CI, then merge it.
4. Reconcile SEQ-001 against final IMP-001 and the current merged application.
5. During that sequence reconciliation, implement the Beta 1 operating contract:
   - seven messages generated for each Contact that passes Personalization;
   - successful messages approved by default;
   - optional human inspection;
   - basic edit with version history;
   - one clear seven-message Contact sequence view;
   - obvious copy-subject and copy-body controls in the application UI;
   - operator can move between Contacts without reviewing seven separate cards.
6. Add Campaign XLSX export as a convenience snapshot. It must be Campaign-scoped, formula-safe, deterministic and read-only; it is not required to use Beta 1.
7. Independently review the reconciled sequence/UI/export behavior; run combined migration/import/sequence/UI tests; publish and merge.
8. Publish the accepted Production Hardening branch as a draft PR, obtain exact-head CI, review and merge.
9. Publish/review the corrected VPS staging-foundation branch.
10. Deploy the merged Beta 1 application to the staging VPS. Validate migrations, managed web/worker services, backups, logs, `/healthz`, `/readyz`, restart/reboot behavior and smoke checks.
11. Run the first internal beta using the application as the primary operating surface. Operators copy/paste messages from VMR into their existing sending platform and manually track progress. XLSX export is available only as a convenience.

## Beta 2 — Gmail-assisted delivery

After Beta 1 is stable:

1. Add internal application users and Google Workspace authentication/mailbox ownership.
2. Build Gmail integration around the human-send contract:
   - VMR creates only the current actionable Gmail draft;
   - the human sends manually in Gmail;
   - VMR detects the sent message;
   - the next follow-up is created in the same Gmail thread only when cadence allows and the Contact is still eligible;
   - replies, suppression and stop conditions prevent future steps.
3. Run a multi-user staging pilot with real mailbox ownership and delivery-state visibility.

## Google Sheets status

Google Sheets synchronization is **not a Beta 1 prerequisite**, **not a Beta 2 prerequisite**, and **not currently a committed delivery gate**.

The original rationale was familiarity for internal users. Beta 1 now addresses that need more directly by making the application itself copy/paste-friendly and optionally providing a downloadable Campaign workbook.

Revisit live Sheets synchronization only if internal use demonstrates a collaboration/reporting need that neither the application nor the XLSX snapshot serves well.

If retained later, Sheets is a projection only. It must never become the source of truth for sequence state, Gmail state, evidence, approvals or delivery decisions.

## Non-negotiable delivery boundaries

Beta 1:

```text
VMR researches, generates and versions
→ sequence approved by default
→ operator may inspect/edit
→ operator copies from VMR UI (or optionally downloads workbook)
→ operator sends/tracks manually in existing platform
```

Beta 2:

```text
VMR creates current Gmail draft
→ human sends manually
→ VMR observes sent/reply state
→ next same-thread follow-up only while eligible
```

Automatic sending remains deferred.

## Documentation rule while branches are unmerged

Repository architecture/behavior docs should describe merged reality and clearly label branch-only work as in-flight. The Build Tracker may describe current blockers and next management actions. Branch handoffs/reviews remain acceptance evidence, not merged-product truth.

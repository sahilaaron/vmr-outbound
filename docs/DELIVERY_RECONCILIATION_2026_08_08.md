# VMR Outbound Agent — Delivery Reconciliation

Status: Current coordination record
Date: 2026-08-08
Authoritative engineering baseline: `main` at current remote head

This document records the current delivery state while several reviewed successor branches remain unmerged. It does not promote branch-only behavior to merged product truth.

## Current merged product

The merged application already provides the contact-first Campaign pipeline, v2 customer UI, Admin Workbench, Agent Studio, Company Intelligence, Research → Company Intelligence handoff, Insights and evidence-backed Personalization. Sending remains unavailable.

PR #241 is merged. Previous documentation wording that called PR #241 a current merge candidate is superseded.

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

After final IMP-001 is accepted and merged, SEQ-001 must be reconciled again against the final IMP migration/read-model behavior, including one deliberate same-origin policy for sequence and import POSTs.

### VPS staging foundation

The Ubuntu staging foundation has passed reboot validation. The corrected publication branch `chore/vps-staging-foundation-current-main` is based on the verified then-current `main` and has a validated bundle/handoff. The server itself remains intentionally without the application deployment.

Status: **staging foundation ready; application deployment waiting on final application branches**.

## Sequential path from here to internal delivery

The shortest safe delivery path is:

1. Finish the two active successor repair passes: IMP-001 and Production Hardening.
2. Cross-review both repaired heads independently. No branch moves forward on builder self-certification alone.
3. Publish the accepted IMP-001 head to PR #242, obtain exact-head CI, then merge it.
4. Reconcile SEQ-001 against the final merged IMP-001 history and semantics; run combined migration/import/sequence tests; independently review; publish and merge.
5. Publish the accepted Production Hardening branch as a draft PR; obtain exact-head CI; review and merge.
6. Publish/review the corrected VPS staging-foundation branch. Keep infrastructure changes separate from application/domain changes.
7. Deploy the reconciled merged application to the staging VPS. Run migrations against the staging database, enable managed web/worker services, validate `/healthz` and `/readyz`, backups, logs, restart/reboot behavior and smoke tests.
8. Add internal application users and Google Workspace authentication/mailbox ownership on the stable staging runtime.
9. Build Gmail integration around the human-send contract:
   - VMR creates only the current actionable Gmail draft;
   - the human sends manually in Gmail;
   - VMR detects the sent message;
   - the next follow-up is created in the same Gmail thread only when appropriate;
   - replies, suppression and stop conditions prevent future steps.
10. Run a small internal multi-user pilot on staging before broad rollout.
11. **Decision checkpoint: Google Sheets projection versus application-native internal operations view.** Do not build Sheets merely because it appeared in an earlier delivery list. Decide whether internal users actually need a Sheet once the multi-user application and Gmail workflow are tangible.
12. If the application provides the required visibility/accessibility, defer Sheets. If a familiar Sheet materially reduces adoption friction, build it as a read/projection sync only; the application database remains authoritative.
13. Complete controlled pilot readiness: mailbox/DNS/TLS, operational acceptance, audit/retry behavior, human-review/send procedures and measured pilot success criteria.

## Current Google Sheets status

Google Sheets synchronization is **not an engineering prerequisite for the next code merge** and is **not yet a committed delivery gate**.

Original rationale: give internal users a familiar operational surface without requiring them to understand the application or the background pipeline.

That rationale must now be compared with the increasingly complete customer-facing application. The product decision is deliberately open until the Gmail/internal-user workflow is reviewed.

If retained, Sheets is a projection only. It must never become the source of truth for sequence state, Gmail state, evidence, approvals or delivery decisions.

## Non-negotiable delivery boundary

```text
VMR researches, generates, versions and governs
→ Gmail receives the current actionable draft
→ human sends manually
→ VMR observes sent/reply state and advances or stops the sequence
```

Automatic sending remains deferred.

## Documentation rule while branches are unmerged

Repository architecture/behavior docs should describe merged reality and clearly label branch-only work as in-flight. The Build Tracker may describe current blockers and next management actions. Branch handoffs/reviews remain acceptance evidence, not merged-product truth.

# Current MVP / Hosted Beta acceptance

**Status date:** 14 August 2026  
**Merged main:** `c1bd054e45e09a22d3d8cf1e7aec629226f352e4`  
**Last independently verified live release:** `d9750b008919bf2bfe42a848b0b454eeedd66f1f`

For exact runtime facts and the merged-vs-live distinction, see [`CURRENT_PRODUCT_STATE.md`](CURRENT_PRODUCT_STATE.md).

## Outcome

VMR Outbound Agent is now in real Hosted Beta UAT.

The intended operator path is:

```text
Capture authorized LinkedIn / Sales Navigator prospect
→ preserve immutable evidence
→ resolve permanent Contact and Company
→ Research
→ Email candidate discovery
→ exact-address Verification
→ evidence-backed Insights
→ Campaign-specific Personalization / sequence
→ human review/edit/approval
→ Gmail draft creation
```

The product does **not** automatically send email.

Approval is not sending authority. Gmail draft creation is a separate mailbox handoff. Automatic sending, scheduler/polling loops, reply detection and autonomous follow-up execution are outside the current path.

## Product surfaces

| Route | Role |
| --- | --- |
| `/` and `/app` | Main operator product |
| `/app/review` | Exact-version review/edit/approval |
| `/app/admin/users` | Admin-created user directory; no public signup |
| `/admin` | Global Admin/Workbench operations |
| `/admin/agents/studio` | Global Agent inspection and Agent-specific Admin modules |
| `/gmail/*` | Separate Gmail mailbox authorization and draft creation |

## Current pipeline

```text
Capture → Identity → Company → Research → Email → Verification → Insights → Personalization → Review → Gmail draft
```

`Sending` remains a registered future boundary only; there is no production sending adapter or automatic sending authority.

## Real Hosted Beta UAT evidence

Campaign:

`PE&VC MENA 200-1000`

Campaign UUID:

`588b3e15-8c39-4d5f-962b-ff1b00d76412`

The original diagnosed cohort contained 50 recent VM Prospector captures.

Before the runtime repair:

- all 50 filing requests existed and targeted the correct Campaign;
- 0 promotion rows existed;
- all 50 promotions had never been attempted;
- all 50 filings remained `PENDING` with `attempts=0`;
- Campaign membership count was 0.

Root cause: Hosted Beta had capture promotion and automatic company-domain resolution unset, so both defaulted false.

After enabling the required staging runtime group and restarting web/worker, the normal worker backfill processed captures through supported application services. No manual SQL or hand-created Contact/CampaignContact rows were used.

For the original 50:

- 18 captures resolved provisionally and promoted;
- 32 remain `UNRESOLVED` awaiting operator confirmation;
- 18 distinct Campaign Contacts now exist;
- 32 filing requests remain `PENDING`;
- 0 filing failures occurred.

Repeat sightings are idempotent per `(Campaign, Contact)`, so applied filings can exceed distinct Campaign memberships.

## Current acceptance blocker

The 18 real Campaign Contacts are eligible/active but their pipeline is blocked at **Research** because Company Research is effectively disabled in the current live control state.

Observed live state after capture recovery:

- Company Research: off;
- Research Claude fallback: off;
- Company Intelligence: off;
- Insights Research: off;
- MillionVerifier operational enablement: off;
- Email Sequences: on;
- Gmail Drafts: on;
- `DRY_RUN=true`;
- automatic Sending: unavailable.

This is no longer a hosting/capture/Campaign-filing problem. The next operating proof is to make Research product-operable from the Admin UI, reclaim the paused Research jobs through supported services, and continue the same real Contacts downstream.

## Provider/runtime facts

Hosted capture promotion currently has:

- Logo.dev capability configured;
- automatic company-domain resolution enabled;
- Logo.dev/SalesNav domain enrichment enabled;
- model company-domain lookup switch enabled.

However the VPS does not currently have the `claude` executable on PATH. Model company-domain fallback attempts therefore return `API_UNAVAILABLE`.

During the real recovery run, 16 model fallback calls were attempted and returned no domain. Those records are no longer `NOT_STARTED`; if Claude CLI is installed later and those exact records should be retried, that requires an explicit forced re-lookup rather than an assumption that the ordinary pending worker will retry them.

## Authentication and users

The durable `users` table is hosted-access authority.

- No public signup.
- Admin creates accounts at `/app/admin/users`.
- Accounts may use any working business or personal email; they are not limited to the VMR domain.
- Google proves identity where used; the VMR account grants access.
- Admin-issued password setup links are single-use and time-bounded.

Current merged password minimum is 15 characters. A UAT repair reducing it to 8 is in development and is not yet merged/live product truth.

## VM Prospector authorization

PR #275 is merged on `main` and replaces ordinary hosted manual backend/shared-secret configuration with first-party VMR account linking:

- authorization-code + PKCE through `chrome.identity.launchWebAuthFlow`;
- short-lived access tokens;
- rotating refresh tokens;
- disabled/revoked account checks on every authorized request;
- hosted rejection of legacy reusable `vmrx1` credentials.

The extension authority remains exactly:

- `POST /api/intake/contact-captures`
- `GET /api/contact-labels`
- `GET /api/contacts/lookup`
- `GET /api/campaigns`

PR #275 is merged but is not called live until the VPS `/version` proves deployment of a containing SHA and real browser UAT passes.

## Review / sequence / Gmail invariants

- Seven messages exactly: days `0, 3, 7, 12, 18, 25, 35`.
- Human edits preserve immutable version lineage.
- A review row represents a real human action.
- Default approval is not human approval.
- Approval is not sending authority.
- Gmail mailbox authorization is separate from hosted identity and extension authorization.
- Gmail creates drafts only in the current path.

## Active UAT repair in development

Branch: `feat/uat-operator-controls`

This branch is not current product truth yet. It is implementing:

1. password minimum `8`;
2. Campaign creator ownership + multi-user assignment;
3. Admin global Campaign visibility with normal-user Campaign scoping;
4. server-side authorization across Campaign/review/membership actions;
5. durable Admin-operated product controls so ordinary operation no longer requires VPS `.env` edits;
6. supported recovery of jobs paused because an operational capability was disabled.

Testing on that branch found directly related authorization gaps in review approval/fallback rendering and CampaignContact-id routes; those are being repaired inside the same authorization scope. The clean full-suite rerun was still in progress at the time of this documentation reconciliation.

## Acceptance remaining

1. Finish/review/merge/deploy the operator-controls repair.
2. Enable Research from the Admin product surface and prove paused Research work is reclaimable without SQL.
3. Continue the existing real Campaign Contacts through Research.
4. Exercise real Email/Verification authority deliberately and record provider decisions/spend.
5. Exercise real Insights and Personalization with available model runtime.
6. Review/edit/approve exact immutable output.
7. Create Gmail drafts.
8. Verify no send side effect.
9. Record an explicit Hosted Beta `PASS`, `CONDITIONAL PASS` or `BLOCKED` verdict with named residual limitations.

Green CI remains implementation evidence, not a substitute for those real operating proofs.

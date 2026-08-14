# Current product and Hosted Beta state

**Status date:** 14 August 2026  
**Repository:** `sahilaaron/vmr-outbound`

This is the current-state reconciliation point for the product, Hosted Beta runtime and active UAT. Historical handoffs and review reports remain evidence of what was true at the time they were written; when they conflict with this document, this document and the current code/runtime win.

## 1. Current merged main

Current `main` after PR #275:

`c1bd054e45e09a22d3d8cf1e7aec629226f352e4`

PR #275 merged the account-linked VM Prospector authentication repair after CI #379 passed on exact branch head:

`0f08a55805070d6154f51d2d111679e2e64ceb67`

That merge includes:

- first-party extension account linking with authorization-code + PKCE;
- short-lived extension access tokens and rotating refresh tokens;
- account/revocation checks on every authorized extension request;
- hosted rejection of the old reusable `vmrx1` credential;
- the exact four-route extension authority contract;
- the Email/Verification Agent Studio Jinja rendering fix;
- deterministic CI test IDs for extension-auth hostile-input cases.

## 2. Last verified live Hosted Beta release

The last live VPS release independently verified during UAT is still:

`d9750b008919bf2bfe42a848b0b454eeedd66f1f`

Release directory observed on the VPS:

`/srv/vmr/releases/20260813T112854Z-d9750b008919`

Do not assume a newer merge is live until `/version` proves it.

The current known distinction is therefore:

- **Merged main:** `c1bd054e...`
- **Last verified live:** `d9750b008...`
- **Account-linked extension:** merged to main, not yet verified as deployed in the live Hosted Beta runtime.

## 3. Hosted Beta runtime facts

The live deployment is `APP_ENV=staging` and `DRY_RUN=true`.

The following capture-promotion capabilities were added to `/etc/vmr/vmr.env` during controlled UAT repair and validated through the application's real runtime validator before web/worker restart:

- `FEATURES__CONTACT_CAPTURE_PROMOTION=true`
- `FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION=true`
- `FEATURES__SALESNAV_DOMAIN_ENRICHMENT=true`
- `FEATURES__MODEL_COMPANY_DOMAIN_LOOKUP=true`

Provider capability state observed during that repair:

- Logo.dev credential: configured/present;
- model fallback setting: enabled;
- Claude CLI on VPS: unavailable (`claude` executable not on PATH), so model fallback attempts currently return `API_UNAVAILABLE`;
- Gmail drafts: enabled;
- Email Sequences: enabled;
- automatic Sending: not implemented and not authorized.

The runtime repair did not change application code or release SHA.

## 4. Real capture UAT result

Target campaign:

`PE&VC MENA 200-1000`

Campaign UUID:

`588b3e15-8c39-4d5f-962b-ff1b00d76412`

The diagnosed original cohort contained 50 recent extension captures. Before runtime repair:

- 50 filing requests were present and targeted the correct campaign;
- 50 promotions had never been attempted;
- 50 filings were `PENDING` with `attempts=0`;
- campaign membership count was 0.

Root cause was deployment configuration: capture promotion and automatic company-domain resolution were unset and therefore false.

After enabling the required runtime group and restarting only web/worker, the normal worker backfill processed the pending captures through supported application services. No SQL/manual row creation was used.

For the original 50 captures:

- 18 domain outcomes became provisional and promoted;
- 32 remained unresolved and require operator confirmation;
- 18 distinct Campaign Contacts now exist in the target campaign;
- 32 filing requests remain `PENDING`;
- 0 filing failures occurred.

The 32 unresolved captures are not failed or blocked. They carry current `UNRESOLVED` decisions and will not automatically retry without an operator confirmation or explicit forced re-resolution.

Across the wider filing-requested cohort observed during recovery:

- 72 filing-requested captures;
- 27 filings applied;
- 45 pending;
- 0 failed;
- the applied filings resolve to 18 distinct campaign memberships because repeated sightings are idempotent per Campaign/Contact.

## 5. Current pipeline state in Hosted Beta

The 18 real Campaign Contacts are currently eligible and active, but their pipeline is blocked at Research because Company Research is disabled in the live deployment's effective operational state.

Observed downstream state after capture recovery:

- Company Research: off;
- Research Claude fallback: off;
- Company Intelligence: off;
- Insights Research: off;
- MillionVerifier operational enablement: off;
- Email Sequences: on;
- Gmail Drafts: on;
- Sending: unchanged/not implemented.

The visible error `Company research is switched off for this deployment` is a real effective-state result, not a data/capture bug.

A separate UAT operator-controls branch has now completed implementation and local validation to move ordinary operational controls into the Admin product surface rather than requiring VPS `.env` editing. It is still unmerged and not live.

## 6. Authentication and users

Hosted access authority is the durable `users` table.

- There is no public signup.
- Admin creates accounts at `/app/admin/users`.
- Accounts may use any working business or personal email address; they are not restricted to `verifiedmarketresearch.com`.
- Google proves identity where used; the VMR user row grants access.
- Password setup uses an admin-issued one-time link.
- Current merged password minimum is still 15 characters; the completed operator-controls candidate reduces it to 8, but that behavior is not current until the branch is reconciled, reviewed, merged and deployed.

PR #275 changes extension authorization so VM Prospector is linked to the operator's VMR account rather than a manually pasted shared capture credential. The user-facing extension documentation on `main` reflects this newer model.

## 7. Extension authority

The hosted extension is intentionally narrow. An account-linked extension token may authorize only:

- `POST /api/intake/contact-captures`
- `GET /api/contact-labels`
- `GET /api/contacts/lookup`
- `GET /api/campaigns`

It does not gain Gmail, Sending, provider-spend, admin, user-management or generic API authority.

The legacy `vmrx1` shared credential remains only for local/development use after PR #275.

## 8. Gmail and Sending boundary

Gmail authorization is separate from platform sign-in and separate from extension authorization.

The implemented Gmail path creates Gmail **drafts only**. It does not send mail.

Current product invariants remain:

- human approval is not sending authority;
- no automatic sending is implemented;
- no scheduler/polling/reply-detection sending loop exists in the current path;
- Gmail draft creation is a handoff to the user's mailbox, after which the operator controls sending manually.

## 9. Operator-controls candidate — completed, not yet current product truth

Branch: `feat/uat-operator-controls`  
Original base: `d9750b008919bf2bfe42a848b0b454eeedd66f1f`  
Candidate head: `a84d9800809836a5ff757431f471bbcedf2e1303`

The candidate completed the requested UAT changes:

1. password minimum `15 → 8`, with max 256, Argon2id, rate limits, setup-link semantics and session invalidation unchanged;
2. Campaign creator ownership plus multi-user assignment;
3. Admin global Campaign visibility; USER visibility limited to Campaigns they created or were assigned;
4. server-side campaign authorization across campaign-id, membership-id, draft/sequence and form/query seams;
5. durable Admin-operated operational settings with an explicit classification of operator controls versus env/deployment-only settings;
6. effective-state model: deployment capability **AND** Admin operational setting **AND** Agent/Campaign control where applicable;
7. enabling Company Research from `/admin/configuration` resumes only jobs paused for `feature_disabled` through the supported `resume_paused` path;
8. `GET /app/agents` remains Admin-only as a global operational surface.

Historical Campaigns deliberately keep `created_by_user_id = NULL`; they remain Admin-visible and are not assigned guessed owners. Admin can explicitly assign users. Creator access does not depend on an assignment row.

Two additive migrations were produced:

- `c1f4a90b7d38` — campaign ownership + `campaign_user_assignments`;
- `e2b7c0d94a15` — `operational_settings`.

The branch found and repaired four directly related defects during validation:

- a USER could approve another Campaign's review draft;
- an empty review queue could render another Campaign's awaiting draft;
- seven `/api/campaign-contacts/{id}/…` routes were not campaign-scoped because they were keyed by membership id rather than campaign id;
- the first Admin Configuration write could be silently overwritten by a concurrent blank-version form submission.

Final local validation reported on `a84d980...`:

- `pytest tests/`: **3955 passed, 0 failed, 0 errors, 0 skipped**;
- Ruff clean;
- Ruff format clean;
- strict mypy clean;
- Alembic single head `e2b7c0d94a15`;
- `alembic check` no drift;
- migration upgrade → downgrade → upgrade round trip clean.

The supplied incremental bundle advertises exact head `a84d980...`, requires prerequisite `d9750b...`, and its SHA-256 was independently checked as `7efd8df9ea776e55c5bb29b6341525d2e6dd29620abf85aa2ddf9770b08dca9d`.

**Important integration state:** this candidate was built from `d9750b...`, while current `main` is now `c1bd054e...` after PR #275. It must be reconciled with current main by a normal merge (no rebase/squash/history rewrite), then receive focused authorization/configuration review and GitHub CI before merge. Do not describe it as merged/live before those gates complete.

## 10. UX / IA redesign status

A hostile UX/IA/UI audit is planned as the first redesign step.

The audit should use:

- the actual current Hosted Beta UI as the live experience;
- this document as current product/runtime context;
- the merged-but-not-yet-live extension-account-linking state as a known near-term delta;
- the completed-but-unmerged operator-controls candidate as known near-term behavior, not as current live behavior.

The audit should not waste time reporting already-known engineering defects as if newly discovered, but it may critique the product model and UX consequences of those areas.

## 11. Source-of-truth hierarchy

For current claims, use this order:

1. live application/database/runtime observations for what is deployed and happening now;
2. GitHub `main`, PRs, commits and CI for merged engineering truth;
3. current repository docs for contracts and operating procedures;
4. Build Tracker for management status;
5. historical handoffs/reviews only as historical evidence.

Never infer that a merged commit is live without a matching `/version` or deployment record.

# VMR Outbound Agent — Delivery Reconciliation

Status: Current coordination record
Date: 2026-08-09
Authoritative engineering baseline: `main` at `4dd09198940dc9eed8c1aa14de96a57e0d89ce28`

## Merged product truth

The following slices are merged and must be treated as product truth, not in-flight branch behavior:

- IMP-001 Campaign Contact File Import — PR #242;
- Production Hardening — PR #245;
- Chrome Extension pre-auth preparation — PR #248;
- seven-message Personalization sequence — PR #249;
- Beta 1 operator UI — PR #250.

The merged Beta path is:

```text
Campaign
→ Campaign Contact / Contact
→ exactly seven messages
→ approved by default unless a human acts
→ optional immutable edit
→ Copy Subject / Copy Body / Copy Full Email
```

CSV/XLSX export was deliberately deferred. Sending and Gmail remain unavailable.

## Current delivery gate

The immediate delivery gate is the VPS staging foundation.

An existing local foundation branch/workstream is being reconciled onto exact post-Beta main. That reconciliation must not deploy or expose the application yet.

Required deployment properties include:

- real `APP_ENV=staging` configuration;
- safe release-symlink/restart/rollback ordering;
- `uvicorn --no-proxy-headers` with explicit application trusted-proxy handling;
- deterministic shared writable upload/runtime paths;
- `/healthz`, `/readyz`, `/version` wiring;
- exact trusted host at deployment time;
- explicit trusted loopback proxy CIDRs;
- exact `RELEASE_ID`;
- 25 MiB upload payload with larger global request ceiling and nginx headroom;
- application-owned security headers;
- default-deny remote application access before app authentication exists;
- no public PostgreSQL exposure;
- no weakening of Production Hardening's non-loopback staging DB-host guard;
- reproducible dependency installation;
- deterministic worker `HOME`/`PATH` suitable for later managed Claude CLI use.

## Deployment-time values still required

These values are intentionally not invented in repository code:

1. staging DNS hostname;
2. temporary private-access mechanism before application authentication (for example Basic Auth or another explicitly selected boundary);
3. exact non-loopback local/private PostgreSQL host/address for the VPS topology.

## Security gate before real operator exposure

Issue #247 remains a launch blocker for remote operator acceptance: application write routes must sit behind an authenticated application boundary before the VPS operator UI is exposed.

The interim VPS/nginx configuration may keep the application private/default-denied, but that is not a substitute for the authenticated application model.

The next application slice after private staging is proven is therefore internal identity/session authentication, followed by separate Gmail mailbox authorization.

## Locked current-cycle definition of done

```text
private HTTPS VMR staging
→ authenticated internal operator
→ Campaign / Contact
→ seven-message Beta UI
→ optional edit/copy
→ separate Gmail authorization
→ operator chooses one exact current message/version
→ VMR creates or updates one Gmail draft
→ operator confirms the correct draft in Gmail
→ VMR retains exact durable lineage
```

Automatic sending, cadence, sent/reply/thread monitoring and later automatic follow-up drafts are next-cycle work.

Google Sheets remains deferred. Campaign spreadsheet export is also not a current launch requirement.

## Repository convergence rule

After each major milestone merge, run a repository convergence checkpoint before opening the next major implementation lane:

- confirm authoritative remote `main`;
- reconcile the Build Tracker;
- close or supersede stale PRs;
- reconcile delivery/decision documentation;
- inventory local worktrees and branches;
- update the normal local `main` without disturbing active isolated work;
- remove only branches/worktrees proven obsolete and clean;
- preserve handoffs/bundles/evidence where still useful;
- begin the next slice from one explicit baseline.

This checkpoint is coordination hygiene, not a new product gate.

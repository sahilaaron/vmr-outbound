# VMR Outbound Agent — Delivery Reconciliation

Status: Current coordination record
Date: 2026-08-09
Authoritative engineering baseline: `main` at `139f6e80d51b573d023bbd3eeb405c6aef268bfd`

## Merged product truth

The following slices are merged and must be treated as product truth, not in-flight branch behavior:

- IMP-001 Campaign Contact File Import — PR #242;
- Production Hardening — PR #245;
- Chrome Extension pre-auth preparation — PR #248;
- seven-message Personalization sequence — PR #249;
- Beta 1 operator UI — PR #250;
- post-Beta VPS staging foundation — PR #252.

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

The VPS foundation is merged. The immediate objective is no longer to build more infrastructure in isolation; it is to make the merged Beta safely usable as a real hosted application.

The next sequence is:

```text
prove VPS infrastructure on the real host
→ authenticated hosted `/app` + `/admin`
→ secure Chrome Extension remote capture
→ real contact moves through Agent stages
→ operator sees all seven messages
→ optional edit/copy
→ manual outreach
```

The initial VPS bring-up is an infrastructure smoke test only. It proves nginx, HTTPS, PostgreSQL, migrations, systemd services, health/readiness, release identity, logs, backup/rollback and reboot recovery. It is not operator UAT by itself.

## Security gate before real operator exposure

Issue #247 remains a launch blocker for remote operator acceptance: application write routes must sit behind an authenticated application boundary before the VPS operator UI is exposed.

The current Workbench safety rule makes `/app` and `/admin` local-only. That temporary localhost-era restriction must not simply be weakened. The successor rule should permit staging only when the authenticated internal-user/session boundary is valid, while preserving intentional local development behavior.

Anonymous remote application writes must be refused. Cookie-session writes require the appropriate CSRF boundary.

## Chrome Extension gate before real-contact acceptance

The merged extension is pre-auth preparation only. Real staging capture requires:

- a stable extension distribution/ID decision;
- VMR-specific authentication;
- HTTPS remote capture;
- Authorization-aware CORS and pinned extension origin;
- deliberate replacement of the current local-only capture restriction.

The extension authenticates only to VMR. It must never receive Google identity or Gmail mailbox tokens.

## Locked current-cycle definition of done

```text
private HTTPS VMR staging
→ authenticated internal operator
→ Chrome Extension captures a real prospect
→ Contact appears in the intended Campaign
→ Contact progresses through Agent stages
→ Research / Company Intelligence / Insights / Personalization complete
→ seven-message Beta UI
→ operator inspects each message
→ optional immutable edit
→ Copy Subject / Copy Body / Copy Full Email
→ operator performs outreach manually outside VMR
```

Passing this workflow with real contacts is the current-cycle definition of done.

**Gmail mailbox authorization and Gmail draft creation are postponed until after this hosted manual-copy Beta has been personally used and accepted.** Automatic sending, cadence, sent/reply/thread monitoring, Google Sheets and Campaign spreadsheet export are also not current launch requirements.

## Deployment-time values still required

These values are intentionally not invented in repository code:

1. staging DNS hostname;
2. temporary/private access mechanism during infrastructure bring-up;
3. exact non-loopback local/private PostgreSQL host/address for the VPS topology.

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

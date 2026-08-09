# VMR Outbound Agent — Next Delivery Model

Last updated: 2026-08-09

## Authoritative baseline

The current merged application baseline is `main` at `4dd09198940dc9eed8c1aa14de96a57e0d89ce28` (PR #250).

It includes:

- Campaign Contact File Import (IMP-001);
- Production Hardening;
- Chrome Extension pre-auth preparation;
- the final seven-message Personalization sequence;
- the Beta 1 operator UI.

Sending remains unavailable.

## Locked current-cycle outcome

The current cycle is complete only when the first internal operator can perform this flow:

```text
private HTTPS-hosted VMR application on the VPS
→ authenticated internal operator
→ open Campaign / Contact
→ view all seven generated messages
→ optionally edit or copy one exact message
→ authorize Gmail separately
→ create or update one selected exact VMR message/version as a Gmail draft
→ open Gmail and see the correct draft
```

Automatic sending, automatic Gmail cadence, sent/reply/thread monitoring and automatic creation of later follow-up drafts are next-cycle work.

Google Sheets is deferred unless real operator use demonstrates a concrete reporting or collaboration need.

Campaign CSV/XLSX export is also deferred from Beta 1; it is not a current launch gate.

## Current product contract

For every qualifying Campaign Contact, VMR produces exactly seven messages with cadence:

`0, 3, 7, 12, 18, 25, 35` days.

The ratified review rule is:

- absence of `EmailSequenceMessageReview` means approved by default;
- a review row exists only when a human acts;
- editing creates a new immutable N+1 message version;
- editing does not fabricate an approval/review row;
- generated, human-edited and regenerated origin remains auditable;
- approved does not mean sendable or sent.

The application is the primary operator surface. Beta 1 exposes all seven messages, exact subject/body text, copy controls, sequence state and basic immutable editing.

Imported identity/projection values retain formula-safety boundaries. Actual email subject/body text is displayed and copied exactly and is not spreadsheet-neutralized.

## Immediate delivery order

### 1. Reconcile and publish the VPS staging foundation

Reconcile the existing VPS foundation onto exact post-Beta `main`.

The deployment branch must preserve Production Hardening rather than bypass it. In particular:

- `APP_ENV=staging` must be real application configuration;
- trusted hosts and trusted proxies must be explicit;
- `/healthz`, `/readyz` and `/version` must reflect the deployed release;
- request-size configuration must retain multipart headroom;
- application-owned security headers must not be duplicated inconsistently in nginx;
- release switching, restart and rollback ordering must validate the new release rather than the previous symlink target;
- worker/runtime writable paths must be deterministic;
- PostgreSQL must not be publicly exposed;
- staging must not weaken the Production Hardening non-loopback database-host guard.

No live deployment occurs until the foundation candidate is reviewed and merged.

### 2. Deploy a private HTTPS staging instance

Deploy the accepted application and worker services to the VPS.

Before application authentication exists, nginx must default-deny the operator application rather than exposing unauthenticated write routes publicly. ACME and intentionally selected probes may remain reachable as required by infrastructure.

Validate migrations, release identity, health/readiness, restart/reboot recovery, logs, backups and browser operation of the merged Beta 1 UI.

### 3. Add the authenticated internal application boundary

Resolve the remote-write/authentication launch blocker before real operator exposure.

Introduce the minimum internal-user/session model required for the first operator and provide Sign in with Google / Google Workspace identity as appropriate.

Google identity authenticates a person to VMR. It does not grant Gmail mailbox access.

Anonymous remote application writes must be refused. Cookie-session writes require the appropriate CSRF boundary.

### 4. Add separate Gmail mailbox authorization

Gmail authorization is a distinct permission boundary from Google sign-in.

Requirements include:

- explicit VMR user ↔ mailbox ownership;
- least-privilege Gmail scope for draft management;
- encrypted durable credential/token storage;
- reconnect/disconnect behavior;
- no Gmail token in the Chrome extension;
- no automatic sending authority.

### 5. Add the first Gmail slice — one operator-triggered draft

From a selected current sequence message/version, the operator can create or update one Gmail draft on demand.

Persist durable lineage between:

- Campaign;
- Campaign Contact;
- logical sequence message;
- exact current message version;
- VMR user/mailbox;
- Gmail draft/provider identifier.

Retries must be idempotent and must not create duplicate drafts accidentally. A draft action must never fabricate sent/delivered state.

### 6. Run real end-to-end internal acceptance

The first operator must prove the actual browser flow on the private HTTPS staging instance:

1. authenticate to VMR;
2. open a Campaign and Contact;
3. view all seven messages;
4. verify copy controls with real clipboard behavior;
5. optionally perform a basic edit and observe the new current version;
6. authorize Gmail separately;
7. create one selected exact message/version as a Gmail draft;
8. open Gmail and confirm the correct draft;
9. confirm VMR retained exact lineage and truthful state.

Passing this flow is the current-cycle definition of done.

## Chrome Extension production integration

The merged extension work is pre-auth preparation only. Production remote capture still requires a stable extension distribution/ID decision, VMR bearer authentication, an HTTPS production capture endpoint, Authorization-aware CORS/origin pinning and deliberate relaxation of the current local-only capture gate.

The extension must authenticate only to VMR. It must never receive Google identity or Gmail mailbox tokens.

## Deferred work

- automatic sending;
- automatic Gmail cadence/follow-up creation;
- Gmail sent/reply/thread monitoring for automated follow-up logic;
- Campaign CSV/XLSX snapshot export unless operator use justifies it;
- Google Sheets live synchronization unless a concrete need survives internal use;
- broad provider sending infrastructure;
- Broadcast Campaign mode;
- broader CRM/workflow expansion.

## Non-negotiable boundaries

- contact-first records remain independent of Campaign ownership;
- Research remains authoritative evidence;
- Company Intelligence is bounded, company-scoped context and cannot become independent proof;
- imported email is carried input, not discovered/provider-verified truth;
- successful seven-message generation is approved by default, with optional human intervention;
- edits preserve immutable version history;
- approval is distinct from sendability/delivery state;
- the application is authoritative for sequence/message/version state;
- Google identity and Gmail authorization remain separate permission boundaries;
- Gmail draft creation is human-invoked in this cycle and never auto-sends;
- future provider integrations must be durable, auditable, retryable and idempotent;
- no external provider action may fabricate verification, delivery or send status.

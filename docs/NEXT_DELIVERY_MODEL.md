# VMR Outbound Agent — Next Delivery Model

Last updated: 2026-08-09

## Authoritative baseline

The current merged application baseline is `main` at `139f6e80d51b573d023bbd3eeb405c6aef268bfd` (PR #252).

It includes:

- Campaign Contact File Import (IMP-001);
- Production Hardening;
- Chrome Extension pre-auth preparation;
- the final seven-message Personalization sequence;
- the Beta 1 operator UI;
- the post-Beta VPS staging foundation.

Sending remains unavailable.

## Locked current-cycle outcome

The current cycle is complete only when the first internal operator can personally use the real hosted application with real contacts through this flow:

```text
Sales Navigator / source page
→ VMR Chrome Extension capture over HTTPS
→ authenticated VMR staging application
→ Campaign / Contact
→ contact progresses through Agent stages
→ Research
→ Company Intelligence
→ Insights
→ Personalization
→ exactly seven generated messages
→ Ready for Sending
→ operator optionally reads each message
→ optional immutable edit
→ Copy Subject / Copy Body / Copy Full Email
→ operator performs outreach manually outside VMR
```

This hosted manual-copy Beta is the current definition of done.

Everything up to Ready for Sending is VMR's own work and needs nobody. The
operator takes over there. [`CUSTOMER_OPERATING_MODEL.md`](CUSTOMER_OPERATING_MODEL.md)
is the authority on that boundary.

**Gmail draft integration is postponed until after the operator has personally used and accepted this exact workflow with real contacts.** Google Sheets and Campaign CSV/XLSX export are also not current launch gates.

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

### 1. Prove the merged VPS foundation on the real host

Deploy exact approved `main` to the VPS and prove infrastructure behavior:

- PostgreSQL topology and migrations;
- `/healthz`, `/readyz`, `/version`;
- nginx configuration and HTTPS;
- systemd web/worker services;
- logging;
- backup/restore path;
- release switching and rollback;
- reboot survival;
- default-deny application exposure until authenticated operator access exists.

This first deployment is an infrastructure smoke test only. It is not the operator UAT milestone.

### 2. Add authenticated hosted operator access

The merged Beta UI is already built, but `/app` and `/admin` are deliberately local-only today. Replace that temporary localhost-era restriction with a stronger hosted rule rather than simply weakening it.

Required behavior:

- local development remains intentionally easy;
- staging may expose `/app` and `/admin` only under a valid authenticated internal-user/session boundary;
- anonymous remote application writes are refused;
- cookie-session writes have appropriate CSRF protection;
- issue #247 or its successor is resolved before operator exposure;
- Google/Workspace sign-in may authenticate the operator, but it must not imply Gmail mailbox access.

The goal of this step is practical: the operator can open the private HTTPS staging URL, sign in, and use Campaigns, Contacts, Agent state and seven-message UI in a real browser.

### 3. Enable secure Chrome Extension remote capture

Move the merged extension from pre-auth/local preparation to production-style staging capture.

Required boundary:

- stable extension distribution/ID decision;
- VMR-specific bearer/session authentication;
- HTTPS capture endpoint;
- Authorization-aware CORS and pinned extension origin;
- deliberate removal/replacement of the current local-only remote-capture gate;
- no Google identity token or Gmail mailbox token in the extension.

The extension authenticates only to VMR.

### 4. Run real-contact end-to-end acceptance

The first operator must prove the actual browser workflow with real contacts:

1. authenticate to hosted VMR;
2. capture a real prospect with the Chrome Extension;
3. confirm the Contact lands in the intended Campaign;
4. wait while the Contact moves through the Agent stages, doing nothing to advance it;
5. confirm the Contact reaches Ready for Sending;
6. optionally inspect Research, Company Intelligence, Insights and Personalization outcomes as exposed by the product;
7. open the Contact and see all seven messages, usable as they stand;
8. verify exact subject/body copy behavior in the real browser;
9. optionally edit one message and confirm immutable N+1 versioning/current-version behavior;
10. manually copy the selected outreach content and use it outside VMR;
11. record any UAT defects found in real operation.

No approval step appears in this list, and none is missing from it. The seven
messages are usable when they are generated; an approve or discard recorded
along the way is a real human decision and changes nothing about readiness.

Passing this flow is the current-cycle definition of done.

### 5. Reassess Gmail only after hosted Beta acceptance

Gmail draft integration remains architecturally valid but is explicitly postponed.

Only after the hosted manual-copy workflow has been personally used with real contacts should the next delivery decision be made about:

- separate Gmail mailbox authorization;
- operator-triggered draft creation;
- durable Gmail draft lineage/idempotency;
- later thread/cadence/reply behavior.

No Gmail work should delay the hosted Beta milestone above.

## Chrome Extension production integration

The merged extension work is pre-auth preparation only. Production remote capture still requires a stable extension distribution/ID decision, VMR authentication, an HTTPS capture endpoint, Authorization-aware CORS/origin pinning and deliberate relaxation of the current local-only capture gate.

The extension must authenticate only to VMR. It must never receive Google identity or Gmail mailbox tokens.

## Deferred work

- Gmail mailbox authorization and Gmail draft synchronization until after hosted manual-copy Beta acceptance;
- automatic sending;
- automatic Gmail cadence/follow-up creation;
- Gmail sent/reply/thread monitoring;
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
- remote operator access must be authenticated before `/app` or `/admin` are exposed in staging;
- the Chrome extension authenticates to VMR only;
- Gmail remains a separate future permission boundary;
- no external provider action may fabricate verification, delivery or send status.

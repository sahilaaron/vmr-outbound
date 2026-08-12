# VMR Outbound documentation

## Current planning sources

- [Delivery reconciliation — 2026-08-09](DELIVERY_RECONCILIATION_2026_08_09.md) — current coordination record and repository-convergence checkpoint.
- [Next delivery model](NEXT_DELIVERY_MODEL.md) — current delivery order from merged VPS foundation through authenticated hosted Beta, Chrome Extension capture and real-contact manual-copy acceptance.
- [Decision 0010: Hosted manual-copy Beta before Gmail](decisions/0010-hosted-manual-copy-beta-before-gmail.md) — current priority: make the real hosted Campaign/Contact/seven-message workflow usable with real contacts before Gmail integration.
- [Decision 0007: Human-controlled Google delivery](decisions/0007-human-controlled-google-delivery.md) — retained architectural boundary: Google identity and Gmail mailbox authorization remain separate if/when Gmail work resumes.
- [Decision 0008: Extension distribution and origin pinning](decisions/0008-extension-distribution-and-origin-pinning.md) — stable extension identity/distribution remains a prerequisite for production origin pinning.
- [Decision 0009: Sequences as a bounded domain, not seven drafts](decisions/0009-seven-message-sequence-domain.md) — why legacy `DraftVersion` could not carry the seven-message sequence and the accepted approved-by-default review semantics.
- [The seven-message outreach sequence](EMAIL_SEQUENCE.md) — detailed sequence domain, versioning, cadence and future provider integration contract.
- [One-click Gmail draft creation](GMAIL_DRAFTS.md) — #267: the separate Gmail mailbox grant, encrypted token storage, draft lineage and idempotency. Built behind `FEATURES__GMAIL_DRAFTS`, **off by default**; it does not change the launch order below.

## Current merged baseline

`main` at `139f6e80d51b573d023bbd3eeb405c6aef268bfd` includes:

1. IMP-001 Campaign Contact File Import;
2. Production Hardening;
3. Chrome Extension pre-auth preparation;
4. final seven-message Personalization sequence;
5. Beta 1 operator UI;
6. post-Beta VPS staging foundation.

Sending remains unavailable.

## Approved next delivery order

1. Prove the merged VPS foundation on the actual host: nginx/systemd/PostgreSQL/migrations/HTTPS/health/readiness/backup/rollback/reboot.
2. Add authenticated hosted operator access so `/app` and `/admin` can be safely used in staging; resolve anonymous remote-write blocker #247.
3. Enable secure Chrome Extension HTTPS capture to VMR with VMR-only authentication and pinned production/staging origin.
4. Run real-contact end-to-end UAT: capture → Campaign/Contact → Agent stages → seven messages → inspect/edit/copy → manual outreach.
5. Only after that workflow is personally accepted, reconsider separate Gmail mailbox authorization and operator-triggered draft creation.

Automatic sending, automatic Gmail cadence, sent/reply/thread automation, Google Sheets synchronization and Campaign spreadsheet export are not current launch gates.

Gmail *draft* creation (#267) has now been built ahead of step 5, and step 5 is unchanged by it: the feature ships behind `FEATURES__GMAIL_DRAFTS`, which defaults to off, and while it is off the routes 404 and no control renders. It requires a Google Cloud client that does not exist yet (see [`GMAIL_DRAFTS.md`](GMAIL_DRAFTS.md) §8), so no deployment can enable it accidentally. Decision 0007 holds: Google identity and Gmail mailbox authorization are separate grants with separate clients.

# VMR Outbound documentation

## Current planning sources

- [Delivery reconciliation — 2026-08-09](DELIVERY_RECONCILIATION_2026_08_09.md) — authoritative post-Beta coordination record and repository-convergence checkpoint.
- [Next delivery model](NEXT_DELIVERY_MODEL.md) — current delivery order from post-Beta main through VPS, authentication, Gmail draft creation and real internal acceptance.
- [Decision 0007: Human-controlled Google delivery](decisions/0007-human-controlled-google-delivery.md) — Google identity and Gmail mailbox authorization are separate; the current Gmail slice is one operator-triggered draft, never automatic sending.
- [Decision 0008: Extension distribution and origin pinning](decisions/0008-extension-distribution-and-origin-pinning.md) — stable extension identity/distribution remains a prerequisite for production origin pinning.
- [Decision 0009: Sequences as a bounded domain, not seven drafts](decisions/0009-seven-message-sequence-domain.md) — why legacy `DraftVersion` could not carry the seven-message sequence and the accepted approved-by-default review semantics.
- [The seven-message outreach sequence](EMAIL_SEQUENCE.md) — detailed sequence domain, versioning, cadence and future provider integration contract.

## Current merged baseline

`main` at `4dd09198940dc9eed8c1aa14de96a57e0d89ce28` includes:

1. IMP-001 Campaign Contact File Import;
2. Production Hardening;
3. Chrome Extension pre-auth preparation;
4. final seven-message Personalization sequence;
5. Beta 1 operator UI.

Sending remains unavailable.

## Approved next delivery order

1. Reconcile, review and merge the VPS staging foundation onto post-Beta main.
2. Deploy a private HTTPS staging instance with default-deny application exposure until app authentication exists.
3. Add the authenticated internal user/session boundary and Google sign-in; resolve anonymous remote-write blocker #247.
4. Add separate Gmail mailbox authorization.
5. Add one operator-triggered Gmail draft action bound to an exact current VMR message/version.
6. Run real end-to-end internal acceptance.

Automatic sending, automatic Gmail cadence, sent/reply/thread automation, Google Sheets synchronization and Campaign spreadsheet export are not current launch gates.

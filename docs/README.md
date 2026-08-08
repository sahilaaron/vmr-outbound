# VMR Outbound documentation

## Current planning sources

- [Delivery reconciliation — 8 Aug 2026](DELIVERY_RECONCILIATION_2026_08_08.md) — current branch gates and locked current-cycle path to first internal acceptance.
- [Next delivery model](NEXT_DELIVERY_MODEL.md) — approved application-first Beta 1, VPS deployment, Google sign-in and individual Gmail-draft slice.
- [Decision 0007: Human-controlled Google Workspace delivery](decisions/0007-human-controlled-google-delivery.md) — product decision history for human-controlled Gmail delivery; refined by the current-cycle plan.

## Locked current-cycle delivery order

1. Finish and independently cross-review IMP-001 and Production Hardening.
2. Publish exact accepted heads, obtain exact-head CI and merge them.
3. Reconcile the seven-message Personalization sequence against final IMP/current main; review and merge.
4. Build the Beta 1 operator UI: seven messages together, approved by default, optional basic versioned edit, Copy subject/body/full email.
5. Add optional Campaign XLSX/CSV export as a convenience snapshot.
6. Publish/review the VPS staging foundation and deploy the merged application with the Claude CLI/background runtime, Nginx and HTTPS.
7. Add internal authentication and Sign in with Google / Google Workspace identity.
8. Add separate Gmail mailbox authorization.
9. Allow an operator to create one selected VMR sequence message as an individual Gmail draft on demand, with durable idempotent lineage and no auto-send.
10. Run real end-to-end internal acceptance with the first operator.

## Next cycle

After the current cycle is stable, add automated Gmail cadence, sent/reply/thread observation and automatic creation of the next same-thread follow-up draft while the Contact remains eligible.

Google Sheets remains deferred unless internal use demonstrates a collaboration/reporting need that the application UI and optional export do not solve.

Automatic sending remains deferred. The application remains authoritative.

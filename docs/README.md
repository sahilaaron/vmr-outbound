# VMR Outbound documentation

## Current planning sources

- [Delivery reconciliation — 8 Aug 2026](DELIVERY_RECONCILIATION_2026_08_08.md) — current branch gates and sequential path to internal delivery.
- [Next delivery model](NEXT_DELIVERY_MODEL.md) — approved human-controlled delivery architecture and current build order.
- [Decision 0007: Human-controlled Google Workspace delivery](decisions/0007-human-controlled-google-delivery.md) — approved decision to keep human sending in Gmail rather than build autonomous sending now. Its earlier assumption that Google Sheets is mandatory is subject to the current Sheets decision checkpoint.

## Current delivery order

1. Finish and independently accept IMP-001 Campaign Contact File Import.
2. Reconcile the seven-message Personalization sequence against final IMP-001, independently review, publish and merge.
3. Finish and independently accept Production Hardening.
4. Publish/review the VPS staging foundation and deploy the reconciled application to staging.
5. Add internal users and Google Workspace OAuth/mailbox ownership.
6. Build Gmail draft/sent/reply delivery state around manual human sending and same-thread follow-ups.
7. Run a small multi-user staging pilot using the application as the primary operating surface.
8. Decide whether Google Sheets adds enough adoption/operational value to justify a projection sync; defer it if the application already serves the need.
9. Complete controlled pilot readiness and measured operating acceptance.

Automatic sending remains deferred. The application remains authoritative; Gmail is the manual send surface; Google Sheets, if retained, is only a projection.

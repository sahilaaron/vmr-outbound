# VMR Outbound documentation

## Current planning sources

- [Delivery reconciliation — 8 Aug 2026](DELIVERY_RECONCILIATION_2026_08_08.md) — current branch gates and sequential path to Beta 1/Beta 2 delivery.
- [Next delivery model](NEXT_DELIVERY_MODEL.md) — approved application-first Beta 1 and Gmail-assisted Beta 2 architecture.
- [Decision 0007: Human-controlled Google Workspace delivery](decisions/0007-human-controlled-google-delivery.md) — earlier approved decision to keep human sending in Gmail rather than build autonomous sending. Its assumption that Gmail/Sheets are immediate prerequisites is superseded by the current Beta 1 plan.

## Current delivery order

1. Finish and independently accept IMP-001 Campaign Contact File Import.
2. Reconcile the seven-message Personalization sequence against final IMP-001 and change the approval model to approved-by-default with optional review/basic edit.
3. Build the application-first Beta 1 sequence UI with clear direct-copy controls for all seven messages.
4. Add Campaign XLSX/CSV export as an optional convenience snapshot.
5. Finish and independently accept Production Hardening.
6. Publish/review the VPS staging foundation and deploy Beta 1 to staging.
7. Run the internal Beta 1 by copying messages from VMR into the existing sending platform and manually tracking delivery.
8. Beta 2: add internal users, Google Workspace OAuth/mailbox ownership and Gmail current-draft/sent/reply/thread state.
9. Revisit Google Sheets only if a live synchronization need remains after application UI + export + Gmail are proven.
10. Complete controlled pilot readiness and measured operating acceptance.

Automatic sending remains deferred. The application remains authoritative. The application UI is the primary Beta 1 operating surface; XLSX/CSV export is an optional convenience.

# Workbench and Customer Surfaces

## Customer application

The normal customer product is `/app`.

Its governing rule is:

> **VMR Outbound is autonomous until Ready for Sending.**

The customer should primarily see:

- Campaigns;
- Contacts;
- Processing counts/status;
- Ready for Sending;
- Could not prepare;
- generated seven-message sequences;
- optional inspect/edit/copy/Gmail-draft actions.

The customer application must not be a low-level Agent control room.

Do not present failed/blocked jobs, retries, unresolved enrichment or provider/model errors as a generic customer task inbox.

## Admin Workbench

The Admin Workbench is the diagnostic/recovery surface.

It owns detailed visibility into:

- Agent jobs and attempts;
- failures, blocks and retries;
- queue/lease state;
- Campaign/global controls;
- provider/model diagnostics;
- resolution internals;
- operational recovery;
- audit/provenance views.

Admin may expose technical state that would overwhelm or confuse normal customers.

## Customer status projection

The customer-facing lifecycle is intentionally small:

- Processing
- Ready for Sending
- Could not prepare

Detailed Agent state may be expandable for transparency, but it is not a list of obligations.

## User-owned input

A customer-facing action is appropriate only when the customer genuinely owns the missing input, such as Campaign setup or explicit Campaign-level paid/live-work consent.

Those actions should be named specifically in context. They should never be mixed with machine failures into a generic "Needs you" count.

## Sequence handling

A complete valid seven-message sequence is usable without a human approval click.

The customer may inspect and edit messages. Review records exist only when a human actually acts and do not control Ready for Sending.

## Sending

Automatic sending is unavailable.

Where Gmail draft creation is enabled, it is an explicit customer action after messages exist. Sending remains manual.

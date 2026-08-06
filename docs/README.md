# VMR Outbound documentation

## Current planning sources

- [Next delivery model](NEXT_DELIVERY_MODEL.md) — approved build sequence after the Admin Workbench and Company Intelligence integration.
- [Decision 0007: Human-controlled Google Workspace delivery](decisions/0007-human-controlled-google-delivery.md) — Gmail drafts and Google Sheets replace an autonomous Sending Agent for the next phase.

## Approved next build order

1. Merge PR #241 after CI passes on its exact final head.
2. Build one initial personalized email plus six follow-ups as one versioned sequence.
3. Complete Campaign-bound Apollo XLSX/CSV import.
4. Introduce internal users and Google Workspace OAuth.
5. Add Gmail Draft Sync.
6. Add Google Sheets synchronization.
7. Establish an always-on Ubuntu deployment for multiple internal users.

Automatic sending remains deferred. Gmail is the human-controlled send surface for the next release.

# VMR Outbound documentation

## Current planning sources

- [Next delivery model](NEXT_DELIVERY_MODEL.md) — approved build sequence after the Admin Workbench and Company Intelligence integration.
- [Decision 0007: Human-controlled Google Workspace delivery](decisions/0007-human-controlled-google-delivery.md) — Gmail drafts and Google Sheets replace an autonomous Sending Agent for the next phase.
- [Decision 0008: Sequences as a bounded domain, not seven drafts](decisions/0008-seven-message-sequence-domain.md) — why `DraftVersion` could not carry a sequence, and what replaced it.
- [The seven-message outreach sequence](EMAIL_SEQUENCE.md) — the SEQ-001 domain, versioning, review, timing, rollout, and the future Gmail/Sheets design this build only prepares for.

## Approved next build order

1. Merge PR #241 after CI passes on its exact final head.
2. ~~Build one initial personalized email plus six follow-ups as one versioned sequence.~~ Built (SEQ-001); default off behind a deployment flag plus a per-Campaign opt-in.
3. Complete Campaign-bound Apollo XLSX/CSV import.
4. Introduce internal users and Google Workspace OAuth.
5. Add Gmail Draft Sync.
6. Add Google Sheets synchronization.
7. Establish an always-on Ubuntu deployment for multiple internal users.

Automatic sending remains deferred. Gmail is the human-controlled send surface for the next release.

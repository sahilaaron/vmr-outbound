# ADR 0004: Campaign Contact and PostgreSQL Agent backbone

Status: Accepted for Phase 2 implementation

Date: 2026-07-29

## Context

The repository already had permanent Contact and Company records, Campaign and
Campaign Contact membership, capture Labels, an exact-email verification queue,
identity convergence, Company resolution, suppression, provenance, and audit
history. The missing application backbone was a common way to enrol Contacts,
run current and future Agents, recover work, and explain per-Campaign progress.

The locked product rules require capture without a Campaign, reusable Contacts
and Companies, separate Collection and Campaign membership, and Campaign-
specific execution state on Campaign Contact.

## Decision

1. Extend `Campaign` and `CampaignContact`; do not introduce replacement
   abstractions.
2. Use `Collection` as the canonical backend name while retaining the proven
   `contact_labels` and `contact_label_assignments` tables.
3. Treat `CampaignContact` as the unique `(campaign_id, contact_id)`
   participation record and add append-only acquisition sources.
4. Generalize `verification_jobs` in place into the shared `AgentJob` queue.
5. Use a stable Agent registry with global controls, Campaign overrides,
   dependencies, classified results, and real adapters for existing components.
6. Keep durable pipeline projections separate from queue state and retain an
   append-only event history.
7. Make Contact identity/company fields nullable so every accepted capture can
   persist a truthful permanent person without placeholders.
8. Keep PostgreSQL as the queue for the MVP. Leases, `SKIP LOCKED`, idempotency,
   bounded retries, and restart recovery meet the current deployment needs.

## Consequences

- Capture can optionally file into a Campaign without coupling Contact creation
  to Campaign validity.
- Existing Label and verification data migrate additively.
- Lease and Running checkpoints survive worker restarts; domain outcomes and job
  completion share the final transaction.
- Operator-visible state is explainable from database records after a restart.
- Unimplemented Agents remain registered but disabled; they cannot report fake
  completion.
- A downgrade cannot restore the old Contact `NOT NULL` shape until unresolved
  permanent Contacts have been explicitly resolved or removed.

ADR 0002 remains the source for the contact-first ownership decision. Its
specific 2.0 assumptions that the capture contract rejects Campaign selection
and that unmatched capture cannot create a Contact are superseded by this ADR
and `linkedin-contact-capture/2.1.0`.

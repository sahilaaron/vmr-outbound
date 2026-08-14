# 0009 — A sequence is a bounded domain, not seven drafts

Date: 2026-08-06. Status: accepted.

## Context

Personalization produced one email per Campaign Contact and stored it as an immutable `DraftVersion`. The approved next build (`docs/NEXT_DELIVERY_MODEL.md`, Decision 0007) is one initial message plus six follow-ups, generated as one versioned sequence and later exposed through the operator UI and Gmail one message/version at a time.

The question was whether seven `DraftVersion` rows could carry that.

## Decision

**No. A new bounded domain, with `Draft`/`DraftVersion` left completely untouched.**

### 1. Why seven drafts fail

`draft_versions` is unique on `(contact_id, campaign_id, version_number)`, where `version_number` counts rewrites. `drafts._decide` refuses to record a decision on anything but the highest version number for a `(contact, campaign)` pair — the rule that stops an operator approving text the Agent has already replaced.

Store seven siblings there and six become permanently un-approvable. Relaxing the rule to make room for them would remove the protection for single drafts too. One column would also carry two unrelated meanings.

### 2. Four tables, split by what changes

`email_sequences` (one per generation, immutable) · `email_sequence_messages` (one per logical message, identity only, no text) · `email_sequence_message_versions` (one per immutable content version) · `email_sequence_message_reviews` (one decision per exact version when a human acts).

The split between message and version is the key decision. **Text belongs to a version; identity belongs to the message.** A later Gmail adapter must be able to bind one provider draft to the exact current version while the logical message identity survives edits and regeneration.

### 3. One model call, not seven

Coherence is the reason, cost is the confirmation. Later follow-ups must know what earlier messages already said so proof points and CTAs do not repeat. Seven independent calls would make coherence weaker and turn one atomic outcome into seven partial ones.

### 4. One Agent stage

Personalization stays one stage with one Agent Job. Seven follow-ups do not become seven stages because a sequence succeeds whole or fails whole.

### 5. Two switches, not one

A deployment flag and a per-Campaign opt-in prevent enabling the feature from silently changing every existing Campaign.

### 6. Historical drafts are not adapted

A pre-sequence draft remains one historical draft. The application may present the legacy path when no sequence exists, but it does not invent a one-message sequence around historical storage.

### 7. External provider references remain additive

Future Gmail provider linkage attaches to stable internal sequence/message/version identities. Provider-reference persistence remains an additive concern and must not redefine review or sequence identity.

### 8. Review semantics after final reconciliation

The accepted final product contract is Option C:

- generated messages are approved by default;
- absence of `EmailSequenceMessageReview` means approved by default;
- a review row exists only when a human acts;
- optional confirmation/discard records a real human decision;
- editing creates immutable N+1 content and no fake review row;
- approved is distinct from sendable/delivered state.

This reconciles the original sequence domain with the Beta operating model without rewriting historical draft storage.

`docs/CUSTOMER_OPERATING_MODEL.md` later stated the same position for the customer surface: a generated, validated sequence is Ready for Sending with no human action, and the surface it is read on is a reading surface rather than a queue. That document changed no rule in this ADR — it removed the customer-facing framing that contradicted them.

### 9. Corrections after adversarial review

Independent hostile review of the original implementation found several defects that changed domain rules:

- UI and generation availability must resolve through the same sequence-availability contract; existing sequences remain disclosable even when generation is disabled.
- `current_actionable_position` cannot skip a predecessor gap; a follow-up is not actionable merely because its own review state is acceptable.
- destructive migration downgrade must refuse while sequence data exists.
- absence assertions in tests must first prove that the prohibited state is reachable.

## Consequences

- Historical drafts, approvals and lineage remain untouched.
- Sequence identity and immutable content version identity are distinct and stable.
- Exactly seven messages and their version lineage can be exposed without overloading legacy draft semantics.
- Optional human review can coexist with approved-by-default generation.
- A future Gmail adapter can bind to exact current message/version identity without reopening the evidence or review model.
- Legacy and sequence read paths coexist until older Campaigns naturally move to the sequence domain.

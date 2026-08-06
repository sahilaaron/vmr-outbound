# 0008 — A sequence is a bounded domain, not seven drafts

Date: 2026-08-06. Status: accepted.

## Context

Personalization produced one email per Campaign Contact and stored it as an
immutable `DraftVersion`. The approved next build (docs/NEXT_DELIVERY_MODEL.md,
0007) is one initial message plus six follow-ups, generated as one versioned
sequence, reviewed by a human, and later synchronized into Gmail one draft at a
time.

The question was whether seven `DraftVersion` rows could carry that.

## Decision

**No. A new bounded domain, with `Draft`/`DraftVersion` left completely
untouched.**

### 1. Why seven drafts fail

`draft_versions` is unique on `(contact_id, campaign_id, version_number)`, where
`version_number` counts *rewrites*. `drafts._decide` refuses to record a
decision on anything but the highest version number for a `(contact, campaign)`
pair — the rule that stops an operator approving text the Agent has already
replaced.

Store seven siblings there and six become permanently un-approvable. Relaxing
the rule to make room for them would remove the protection for single drafts
too. And one column would carry two unrelated meanings, which is the kind of
overload that reads fine until somebody writes a query against it.

### 2. Four tables, split by what changes

`email_sequences` (one per generation, immutable) · `email_sequence_messages`
(one per logical message, identity only, no text) ·
`email_sequence_message_versions` (one per immutable content version) ·
`email_sequence_message_reviews` (one decision per exact version).

The split between the second and third is the decision that matters. **Text
belongs to a version; identity belongs to the message.** A later Gmail adapter
must be able to say "the follow-up after *this* sent message" and have that
survive an operator fixing a typo, a regeneration, and any change to position
numbering. It can, because `email_sequence_messages.id` is issued once and never
reissued — regeneration reuses the seven rows and writes only new content
versions.

### 3. One model call, not seven

Coherence is the reason, cost is the confirmation. Message 5 cannot avoid
reusing message 2's proof point unless it has read message 2, so per-message
calls cannot deliver non-repetition at any price — and they cost seven times as
much to fail at it. Seven calls also turn one atomic outcome into seven partial
ones, where a failure at position 6 leaves five messages that look finished.

A planning call before generation was considered and rejected: the plan is
already deterministic here, so it would spend money to produce something the
builder already knows.

### 4. One Agent stage

Personalization stays one stage with one Agent Job. Seven follow-ups did not
become seven stages, because a sequence succeeds whole or fails whole, and seven
stages would make "the Personalization stage completed" a statement about
nothing in particular.

### 5. Two switches, not one

A deployment flag *and* a per-Campaign opt-in. One flag would mean that enabling
the feature silently changed what every existing Campaign produced, on the next
run, with no decision recorded anywhere.

### 6. Historical drafts are not adapted

A pre-SEQ-001 draft is presented as what it is — one message — not as a
one-message sequence. An adapter would have to invent a sequence version, a
purpose and six absences. Every one of those is a small lie in a system whose
value is not telling them.

### 7. The external-reference table is documented, not built

`docs/EMAIL_SEQUENCE.md` §15 carries the full DDL for the deferred
`email_sequence_external_references` table. It is not created here because
nothing in this build can write to it, and a table with no writer is exactly the
speculative infrastructure `docs/AGENTS.md` forbids. It is purely additive, so
deferring it costs nothing.

The *generic* domain vocabulary it depends on — `SequenceDeliveryState` and
`SequenceStopReason`, kept strictly separate from review state — **is** built,
because that is what stops "approved" quietly coming to mean "ready to send"
once a delivery workflow arrives.

## Consequences

- Historical drafts, approvals and lineage are bit-for-bit unaffected.
- One additive, reversible migration; one Alembic head.
- Review gains a per-message decision model; the single-draft review path is
  unchanged and still reachable.
- A future Gmail adapter can attach to stable internal identities without
  reopening the review model, the evidence contracts or the migration topology.
- The cost: two review paths coexist until every Campaign has opted in. That is
  the price of not reinterpreting historical records, and it is the right price.

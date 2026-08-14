# Gmail draft integration

## Product role

Gmail integration is an optional **post-Ready-for-Sending manual action**.

A customer with a connected Gmail mailbox may explicitly create Gmail drafts from the current message versions in a ready sequence.

**This feature cannot send.** The customer sends manually in Gmail.

It does not make Review mandatory, does not change Ready for Sending, and does not create automatic cadence execution.

## Customer flow

```text
Contact becomes Ready for Sending
→ customer opens the seven-message sequence
→ optional inspect/edit
→ optional Create Gmail drafts
→ customer sends manually in Gmail
```

A human approval click is not required before draft creation merely because the sequence was generated automatically. The relevant authority is that the sequence/current message versions are valid and the Campaign Contact remains eligible under policy.

## Separate authentication boundaries

VMR sign-in, Chrome extension authentication and Gmail mailbox authorization are separate credentials/permission domains.

VMR sign-in never implies mailbox access. Extension authentication never receives Gmail tokens. Gmail consent is explicit and limited to the mailbox action.

## Scope

Use the narrow Gmail compose permission required for draft creation. Do not widen mailbox access without an explicit product/security decision.

The application adapter intentionally exposes no send operation.

## Token safety

Mailbox tokens are encrypted at rest with the configured Gmail token-encryption key.

Tokens must not appear in:

- logs;
- tracebacks;
- HTML/API responses;
- settings dumps;
- screenshots;
- fixtures;
- source or Git history.

Reconnect/revocation state must fail safely and visibly.

## Draft identity and idempotency

A Gmail draft record is tied to the exact immutable sequence message version, not merely to Contact + position.

Draft creation must remain idempotent across retries and reconnects. Ambiguous external outcomes are reconciled before another write is attempted.

Editing a message after a Gmail draft exists creates a new immutable VMR version. Historical Gmail draft lineage remains the record of what was actually drafted; it is not silently rewritten.

## Sequence safety

Draft creation requires the current sequence/message versions the customer saw and selected. If current versions changed underneath the action, refuse and require reload rather than silently drafting different text.

Suppression, address/Verification requirements and other authoritative safety conditions still apply.

## Threading

Follow-up drafts are standalone until a real send creates the message identity required for truthful reply threading. Do not fabricate thread relationships before a predecessor was actually sent.

## Not built

This integration does not provide:

- automatic sending;
- automatic follow-up scheduling;
- reply/bounce monitoring;
- mailbox polling;
- campaign automation;
- a generic synchronization engine.

Ready for Sending means VMR has prepared the outbound package. It does not mean Gmail drafts were created or messages were sent.

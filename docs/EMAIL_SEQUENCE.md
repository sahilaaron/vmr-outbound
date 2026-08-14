# Seven-message email sequence

## Product role

Personalization produces one coherent seven-message sequence for a Campaign Contact.

The sequence is part of the outbound package that makes a Contact **Ready for Sending**.

It is not a queue waiting for operator approval.

## Cadence

Default elapsed days:

`0, 3, 7, 12, 18, 25, 35`

The seven messages are:

1. initial outreach;
2. concise reminder;
3. new angle;
4. role relevance;
5. proof or outcome;
6. low-friction resource;
7. close the loop.

A Campaign may use a valid configured cadence override where the existing cadence contract permits it.

## Generation

All seven messages are generated as one bounded coherent outcome so later messages can avoid unnecessary repetition and preserve one strategy/context decision.

Generation must validate before the sequence is considered usable. Partial sequences are not Ready for Sending.

## Evidence

Messages may use only eligible context supplied by deterministic application code.

Research remains authoritative sourced Company knowledge. Company Intelligence remains bounded and non-citable where policy says it is non-citable.

Each message records what it actually used. Missing evidence stays missing; the generator must not invent facts, proof, urgency, relationships or citations.

## Current-state input

When Personalization starts, it receives the current eligible Research + Insights + Campaign/seller context available at that moment.

The resulting sequence records the evidence/versions it used for provenance.

Historical predecessor Agent Job identity is not itself a prerequisite to generate when valid current eligible context exists.

## Immutable message model

The sequence keeps logical message identity separate from immutable content versions.

Editing one message:

- writes a new current version for that logical message;
- preserves the version it replaced;
- does not rewrite the other six messages;
- preserves audit/provenance history.

## Human review semantics

Human review is optional.

- A missing review row means nobody acted.
- It does **not** mean the message is waiting for approval.
- A review row exists only when a human explicitly records a decision.
- The system must never fabricate a human approval.
- A valid generated sequence can be Ready for Sending without any review rows.

Optional editing remains available regardless of whether the customer previously reviewed the message.

## Customer presentation

The natural customer location for the sequence is the Campaign/Contact Ready-for-Sending experience.

The customer should be able to:

- see all seven messages;
- inspect subject/body and relevant evidence;
- copy content;
- optionally edit one message;
- proceed with manual sending-related actions.

A compatibility Review page may exist, but it is not a mandatory queue and must not carry "waiting for you" semantics merely because a generated message has no human review row.

## Ready for Sending relationship

A sequence contributes to Ready for Sending when:

- generation completed successfully;
- all seven logical positions exist;
- current versions are valid;
- the sequence has not been invalidated/stopped by an authoritative safety condition;
- the Campaign Contact remains eligible and unsuppressed;
- required address/Verification and upstream context conditions are satisfied.

Human approval is not part of this calculation.

## Sending

Sequence generation does not send anything.

Where Gmail draft creation is enabled, draft creation is a separate explicit user action against exact current message versions. The customer sends manually.

There is no automatic scheduler/follow-up sender in this contract.

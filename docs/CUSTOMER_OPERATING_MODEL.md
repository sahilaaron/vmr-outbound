# The customer operating model

> **VMR Outbound is autonomous until Ready for Sending.**

This document is the authority on what the customer does, what the system does,
and where the line between them sits. Where any other active document disagrees
with it, this one is right and the other one is stale.

---

## 1. What the customer does

Five things, and nothing else:

1. **Create and configure a Campaign.**
2. **Add contacts** — capture them through the Chrome extension, or import a file.
3. **Monitor progress**, if they want to. Optional.
4. **Wait** while VMR prepares each contact.
5. **Take over at Ready for Sending** — read, edit and use the seven generated
   emails, and do the sending-related work by hand.

Seller Knowledge Base entry and campaign configuration are legitimate setup and
remain part of the product. They are setup, done once and revisited when
something changes. They are not a recurring task inbox.

## 2. What the system does

Everything between step 2 and step 4. All nine Agents — Capture, Identity,
Company, Research, Email, Verification, Insights, Personalization, Sending —
including every retry, every backoff, every provider failure and every stage
that has to be skipped or held.

The customer is **not** an Agent operator. Specifically, none of the following
is a customer task, and none of them may be presented as one:

- failed Agent jobs;
- blocked Agent jobs;
- retryable machine failures;
- unresolved automated enrichment;
- domain, provider or model failures;
- internal pipeline stops;
- Research, Verification, Insights or Personalization failures;
- sequence messages nobody has reviewed.

All of these stay **visible** where visibility is useful. They are status and
diagnostics. They are not obligations, and they never carry a count that reads
as arrears.

## 3. Ready for Sending

A contact is **Ready for Sending** when the system has produced the usable
outbound package for that contact. Concretely, all of the following hold:

- the contact's campaign membership is valid — not excluded, not suppressed, not
  terminally blocked by the eligibility rules;
- Company resolution is sufficient under existing policy;
- eligible Research knowledge exists;
- a usable email address has been discovered;
- verification requirements have passed under existing policy;
- Insights has completed;
- Personalization has completed;
- the seven-message sequence has been generated and validated.

The projection that computes this lives in `app/services/customer_status.py`. It
tests the **artifact** — seven current, non-superseded message versions on a
live sequence whose generation completed, whose validation did not fail, and
which is not stopped — rather than trusting stage flags alone. That matters
because Personalization is skippable: a campaign with it switched off steps over
the stage and every contact would otherwise report the chain complete while no
message exists anywhere.

**No human action is part of this definition.** In particular, approval is not.

The canonical cadence remains seven messages on days **0, 3, 7, 12, 18, 25, 35**.

**Sending is manual.** There is no sending adapter and no automatic send. Once a
contact is Ready for Sending, the outbound work is the customer's, done outside
automatic execution.

## 4. The customer's status vocabulary

Three words. That is the whole vocabulary on the primary customer journey:

| Status | Means |
| --- | --- |
| **Processing** | VMR is still working on this person. Nothing is needed from the customer. |
| **Ready for Sending** | The usable outbound package exists. The customer takes over whenever they like. |
| **Could not prepare** | VMR stopped and will not produce messages for this person. The recorded reason is available. |

"Could not prepare" is a **status, not an obligation**. It is rendered without an
alarm tone and without a call to action, because it describes an outcome the
system reached rather than work the customer incurred.

The detailed Agent statuses — `waiting`, `running`, `retrying`, `paused`,
`failed`, `blocked`, `skipped`, `disabled`, `completed` — are untouched and
remain the durable state machine. They are used for **diagnostics only** on
customer surfaces. The nine-Agent pipeline stays visible as observability; its
presence does not imply the customer is expected to operate nine stages.

## 5. Inspecting and editing is optional

The customer **may** read the generated emails. They **may** edit them. Neither
is required, and neither is a prerequisite for anything.

- A generated, valid sequence is Ready for Sending the moment it is written.
- Reading it changes nothing. Not reading it changes nothing.
- Editing writes a new immutable version, as it always did.

## 6. There is no operator approval requirement

**A generated, valid sequence is not "waiting for approval".** No human action is
required merely because nobody clicked Approve.

This is not a new invariant — the sequence domain has always been
approved-by-default. `app/services/sequences/review.py` carries it: an
`EmailSequenceMessageReview` row exists **if and only if a human actually acted**,
and the absence of a row means approved, not awaiting. What changed is that the
customer UI now says the same thing the backend has always done.

What is preserved:

- the immutable edit and version history — nothing is deleted or rewritten;
- the ability to record a genuine human decision against one exact version, and
  to tell it apart from the default;
- the refusal to fabricate a system "approval": a review row may only be written
  by a real actor.

What is removed: any customer-facing framing in which review is a queue to clear,
a backlog, or a gate on readiness.

The legacy single-`DraftVersion` path (`app/services/drafts.py`) keeps its own
approve/discard semantics for records written before sequences existed. Those
drafts are readable, and reading them is optional. They do not gate readiness.

## 7. Reruns and recovery

Routine reruns are **operational recovery**, not a customer task. VMR retries what
it can on its own.

- The customer sees **which contacts stopped and why** — that is status they are
  entitled to.
- The customer-facing **rerun control** is shown to administrators only. The
  server route and its authorization are unchanged; this is a UI affordance
  decision.
- Deeper recovery — job retry, pause, resume, skip-stage, lease repair — lives in
  the Admin Workbench, where it always did.

## 8. Genuinely customer-owned conditions

Do not overcorrect. Some conditions really do require the customer, and those are
presented in context, in their own place, clearly separated from machine
outcomes:

- the campaign has no contacts yet;
- the campaign is paused;
- required campaign configuration is genuinely absent;
- live/paid work has not been enabled for a campaign that needs it.

The distinction that must never be collapsed:

> **USER INPUT REQUIRED** is not the same as **SYSTEM COULD NOT COMPLETE WORK.**

They render in different cards, under different headings, and they never share a
counter.

## 9. Admin is not simplified

This contract governs the **customer** operating model. Administrators keep full
visibility of blocked jobs, failed jobs, error classes, retries, providers, Agent
state, resolution internals and operational recovery. See
`docs/ADMIN_WORKBENCH.md`. Nothing here blinds an administrator.

## 10. What this replaced

The customer UI previously opened on an aggregate headed "N things want you" over
a card titled "Decisions only you can make", badged the navigation with the same
number, and printed "Needs you" against every campaign.

That total was the sum of five separate categories — undecided drafts, ambiguous
imports, unresolved capture domains, blocked contacts and failed stages. Four of
the five were machine outcomes, one real contact could contribute to several
categories at once, and one category was silently capped. The number was
therefore both wrong and, more importantly, about the wrong thing.

It has been removed rather than renamed. `app/web/v2/context.py` no longer has an
`AttentionCounts` type or an `attention_counts` function, and `nav_groups()`
takes no argument, so there is nothing a customer badge could be computed from.

---

*Tests holding this contract:* `tests/test_customer_operating_model.py`.

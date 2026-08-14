# The seven-message outreach sequence (SEQ-001)

Personalization used to produce one email. It now produces one *sequence* of
seven: an initial message and six follow-ups, generated together, versioned
together, and reviewed one message at a time.

This document is the reference for what that means, what it deliberately does
not mean, and what a later delivery workflow may assume about it.

**Nothing in this build sends anything.** There is no Sheets projection, no
mailbox polling, no scheduler and no sending adapter. Approving a message
records a decision about text; it creates no draft anywhere and grants no
authority to deliver anything.

**A generated, validated sequence is complete as generated.** Approval is not
part of that — §9 has always said so, and
[`CUSTOMER_OPERATING_MODEL.md`](CUSTOMER_OPERATING_MODEL.md) is the contract
that now says the same thing about the customer surface, which is reached as
**Emails** rather than as a queue.

**One piece of §15 now exists, and only one.** #267 added *draft-only* Gmail
integration: an operator connects a mailbox through a separate consent screen
and explicitly clicks Create Gmail drafts, and VMR writes one Gmail draft per
current message version. It cannot send — there is no send call in the adapter
and no route reaches one — and it is an explicit operator action, not something
approval triggers. Everything else in §15 remains designed for and unbuilt.
See [`GMAIL_DRAFTS.md`](GMAIL_DRAFTS.md).

---

## 1. Why a new domain model rather than seven drafts

The obvious implementation is seven `DraftVersion` rows. It does not work.

`draft_versions` is unique on `(contact_id, campaign_id, version_number)`, and
`version_number` counts *rewrites*, not steps. `app.services.drafts._decide`
refuses to record a decision on anything but the highest version number for a
`(contact, campaign)` pair — the rule that stops an operator approving text the
Agent has already replaced. Store seven siblings there and six of them become
permanently un-approvable, while one column silently carries two unrelated
meanings.

So SEQ-001 introduces four tables and leaves `Draft`/`DraftVersion` completely
untouched. No column was added to them, no constraint on them was relaxed, and
no historical row is read or rewritten by any code path in this feature.

## 2. The four tables

| Table | Holds | Immutable? |
|---|---|---|
| `email_sequences` | one row per **generation**: digest, producer, policy, strategy, context decision, lineage, aggregate status | content yes; four lifecycle fields mutate — see below |
| `email_sequence_messages` | one row per **logical message** (7): position, type, purpose, predecessor, delivery state. **No text.** | identity is permanent |
| `email_sequence_message_versions` | one row per **immutable content version** of one message | yes; an edit supersedes |
| `email_sequence_message_reviews` | one **decision per exact message version** | one row per version |

**"Immutable" is precise about content, not about every column.** Four fields on
`email_sequences` are written after the row is created: `superseded_at`,
`superseded_by_id`, `review_state` and `current_actionable_position`. Message
versions carry `superseded_at`, and a review row's `decision` is set to
`invalidated` by an edit. None of them is content — they are lifecycle and cache.
Nothing that was generated, decided or cited is ever rewritten in place.

`review_state` in particular is a **cache**, not the authority. See §9.

The split between the second and third table is the load-bearing idea. Text
belongs to a version; identity belongs to the message. A later delivery adapter
needs to say "the follow-up after *this* sent message", and it must be able to
say that without the statement being invalidated by an operator fixing a typo,
or by a regeneration, or by counting positions.

### Constraints that carry the safety properties

Three of these are database facts rather than application conventions, which
means no code path — including a direct write — can get around them.

- `uq_email_sequences_current_membership` — a *partial* unique index on
  `campaign_contact_id WHERE superseded_at IS NULL`. At most one live sequence
  per Campaign Contact; every superseded row stays for audit.
- `uq_email_sequence_message_versions_current` — the same one level down:
  exactly one live version per logical message.
- `ck_email_sequence_messages_initial_is_first_and_unchained` — position 1 is
  the initial message and has no predecessor; every other position is a
  follow-up and names one. Written as one expression because split in two it
  would admit a chain that starts twice, or one that never starts.
- `uq_email_sequence_messages_sequence_key_purpose` — two positions cannot claim
  the same purpose. A sequence whose messages share a purpose has not been
  planned as a sequence.
- `ck_email_sequences_stop_reason_paired_with_stop_state` — a stop that cannot
  say why is not a stop anybody can act on.

## 3. Generation

**One bounded model call produces all seven messages.** Three reasons, in order
of weight:

1. Coherence is unobtainable otherwise. Message 5 cannot avoid reusing message
   2's proof point unless it has read message 2.
2. Seven calls cost seven times as much for a worse result.
3. Seven calls turn one atomic outcome into seven partial ones, so a failure at
   position 6 leaves five messages that look finished.

A planning call before the generation call was considered and rejected: the plan
here is already deterministic — the purpose framework and the cadence are fixed
before anything is asked of the model — so a planning call would spend money to
produce something the builder already knows.

### Message purposes

| # | Purpose | What it is for |
|---|---|---|
| 1 | `initial_outreach` | strongest relevant context, clearest offering, one bounded CTA |
| 2 | `concise_reminder` | low-friction continuation, minimal repetition, asks for less |
| 3 | `new_angle` | a different evidence, market or company angle |
| 4 | `role_relevance` | the offering against the contact's recorded function, where supported |
| 5 | `proof_or_outcome` | approved proof only; no invented figure, customer or result |
| 6 | `low_friction_resource` | an example or extract; never a claim that an asset exists |
| 7 | `close_the_loop` | short and respectful; no guilt, scarcity, deadline or ultimatum |

These are *purposes, not templates*. The Campaign offering, the CTA, the active
Personalization policy, the evidence actually available and the selected
strategy all outrank them.

## 4. Evidence, and what "distributed" means

`decide_context` runs **exactly once** for the whole sequence, and every message
is written from that one decision. A later follow-up needing a fresh angle gets
a *different slice of the same eligible context* — never a relaxation of what
was eligible.

There is no code path by which follow-up 4 uses a Company Intelligence value
that follow-up 1 was refused, and none by which a later message cites evidence
the policy did not supply. The citation allow-list is the single-draft one,
reused verbatim and applied per message.

Each message version records what it actually used, kept distinct from what was
available:

- `context_used` — labels for the supplied context this message drew on
- `evidence_insight_ids` — the supplied ids this message cited
- `context_decision` — the full decision, including the omitted context and the
  reason each was omitted
- `intelligence_accepted_count` / `intelligence_excluded_count`

Nothing implies that all available context was used.

### Company Intelligence

Unchanged from ADR-0006 and applied identically at every position. Eligible
classifications reach the prompt as read-only orientation. They carry **no
citable evidence id**, so a message citing one is refused by the same allow-list
that refuses any unsupplied citation. Excluded values stay excluded at every
position, with their reasons recorded. Research remains authoritative wherever
Research and Company Intelligence conflict.

### Weak evidence

A thin sequence built on thin evidence is a **correct outcome**, not a defect.
When the fallback ladder reaches level 5 (`offering_led`), the sequence is still
seven messages: deliberately briefer, focused on the approved offering, with
lower-friction asks. Validation is written not to push the generator toward
padding — an eight-word phrase repeated across messages is caught, but a short
honest follow-up is not.

## 5. The input digest and cost control

`compute_input_digest` produces a SHA-256 over a canonical rendering of every
sequence-relevant input: the Campaign Contact, the contact's identity fields,
the Campaign offering and messaging direction and CTA, the policy version, the
selected strategy, the full context decision, the resolved cadence, the
Research/Insights/Company-Intelligence lineage, the sequence builder version,
the validation policy version, and the feature mode.

The digest is checked **before** the model is called. An unchanged input and a
retry after a committed sequence therefore both cost nothing.

### Replay contract, stated explicitly

Nothing intermediate is committed — no raw model output, no half-validated
representation. A persistence failure rolls the whole transaction back, leaves
no trace to replay from, and a subsequent retry calls the model again.

That is a deliberate trade. The alternative buys back one call's cost by
committing unvalidated producer output outside the sequence transaction, which
creates a row that is not yet known to be safe. Given that a sequence is written
once per contact and validation is the thing standing between a model and a
stranger's inbox, the cost is the cheaper side.

## 6. Persistence atomicity

All seven message versions are added and flushed **together**. There is no code
path that writes messages one at a time, so there is no arrangement of failures
that leaves six behind and reports a complete stage. The only per-row flush is
the first-ever creation of the seven logical message rows, where each row's id
is the next row's predecessor and the chain constraint is checked on insert.

A sequence is never persisted in a partial or failed state. Generation failure,
malformed output, a missing message, a duplicate position, an invalid purpose,
a per-message validation failure and a sequence-level validation failure all
raise before anything is written, and the Agent Job carries the bounded reason.

## 7. Validation

Two levels, three outcomes.

**Per-message**: subject present and within bounds, body present and within the
position's word ceiling, no unsupported citation, no invented priority, no
prohibited engagement claim, no invented urgency, no guilt or performed
familiarity, no leaked prompt or internal metadata, no raw JSON, no credential
or local path, no malformed Unicode, no HTML where plain text is expected, no
forward reference to a message not yet written, correct message type.

**Sequence-level**: exactly seven positions, 1–7, unique, correctly typed, valid
predecessor chain, a distinct purpose per position, coherent policy and
strategy, valid and increasing timing matching the resolved cadence, no repeated
subject line, bounded repetition of openings, subject structures, CTAs,
sentences and long phrases, and no late follow-up longer than the initial
message.

The three outcomes are **hard failure** (nothing persisted, nobody is offered
it), **warning** (persisted with the message and shown in review) and
**acceptable fallback** (thin because the evidence is thin, which is right).

## 8. Timing

Planned timing, recommended delay, planned elapsed day. Never a *schedule* —
nothing enqueues anything at a time and there is no scheduler to enqueue it
into.

Default ladder: day 0, 3, 7, 12, 18, 25, 35 — delays of 0, 3, 4, 5, 6, 7 and 10
days. It widens because a reminder three days after a first message is normal
and a sixth follow-up three days after the fifth is harassment.

A Campaign may override it through `cadence_config`:

```json
{"sequence": {"enabled": true, "elapsed_days": [0, 3, 7, 12, 18, 25, 35]}}
```

Overrides are validated and **refused rather than clamped** — a negative delay,
a follow-up before its predecessor, a first message not on day 0, a gap beyond
365 days or a span beyond 3650 days is an operator mistake worth showing, and
silently rewriting it into timing nobody chose would be worse.

## 9. Review

### Approved by default; a review row means a person

Generated messages are **approved**. Review is available and never required:
there is no queue to clear, nothing downstream waits on an operator, and a
sequence nobody has opened is as complete as one somebody confirmed.

The default is carried by the **absence** of a row. An
`email_sequence_message_reviews` row exists if and only if a human actually
acted — approving or discarding. Generation writes none. Editing writes none.
`review._record` is the only function in the codebase that constructs one, and
it refuses an empty or `system:`-prefixed actor.

The alternative considered and rejected was writing seven "system approved" rows
at generation. In the table and in the audit trail those are indistinguishable
from seven approvals a person made, so a reviews table containing decisions
nobody took cannot answer the one question it exists to answer. It would also
have made an operator's first real approval look like an amendment to an
existing decision rather than a decision, because `_record` decides whether to
write an audit event by comparing `decided_by`.

So the two are reported apart, everywhere:

| | Meaning | Where it comes from |
|---|---|---|
| `approved` | ready — default or human | no decision, or `APPROVED` |
| `human_approved` | a person confirmed this exact version | `APPROVED` |
| `unreviewed` | nobody has ruled on it; **not** a backlog | no decision |
| `discarded` | a person stopped the chain here | `DISCARDED` |

`MessageRow.decision_origin` is `"default"` or `"human"`, and every surface that
shows the word "approved" says which. `NEEDS_REVIEW`, `GENERATED`,
`PARTIALLY_REVIEWED` and `CONTAINS_EDITS` are no longer derivable; the members
remain so rows written before this change still load.

**Approved is not sendable.** Every message stays `not_ready` whatever the
review says, `current_actionable_position` authorises nothing, and no code path
reads either to decide whether to act.

### One decision, one exact version

Every decision names one exact immutable message version. There is no decision
recorded against "the sequence".

- An operator can confirm, discard or edit one message without touching the
  other six.
- Bulk approval is one operation that records approval for **every** exact
  message version it covered, sharing a `bulk_operation_id`. If the stored
  versions no longer match what the page was showing, nothing is approved.
- A superseded version cannot be approved.
- Every reader derives the state from `review.derive_state`, which takes only
  counts. The card chip and the counts beside it therefore cannot disagree —
  they used to, because the chip read the cached column while the counts
  aggregated live. `email_sequences.review_state` is a **filter and sort key**;
  the SQL pre-filter on it is a deliberate superset and the derived state
  narrows the result, so a drifted cache costs a row's worth of work and can
  never hide a sequence from the view it belongs in or place one in a view it
  does not. `SequenceCardRow.cache_is_stale` exposes any disagreement rather
  than silently repairing it.
- The sequence aggregate is **derived** from the seven exact message states.
  A complete, unstopped sequence with no discard is `approved`; a discard makes
  it `contains_discarded`; the terminal states are `blocked`, `failed` and
  `superseded`. `EmailSequence.review_state` caches that derivation and is never
  the authority.
- The Review filters are facts about the messages — *all sequences*, *you
  changed these*, *you reviewed these*, *contains a discard* — and the default
  is *all*. There is deliberately no "waiting for you" filter: under default
  approval it could never hold a row, and it used to be the page an operator
  landed on.
- A sequence can be expanded by id whichever filter is active. The filter
  narrows the list; it does not decide what can be read.

Six approvals and one discard is `contains_discarded`, not "partially approved"
— letting an approved count stand in for readiness it does not have is the
failure this model exists to prevent.

## 10. Editing

An edit writes a new immutable version of **that message only**, keeping the
text it replaced in `original_subject` / `original_body` and naming the version
it derived from in `source_version_id`. Three things follow and nothing else:

1. the edited message has a new current version;
2. any approval against the superseded version is marked `invalidated` — not
   deleted, because the approval did happen; it simply no longer applies to text
   nobody approved;
3. the aggregate is recomputed.

**No review row is written for the new version**, and that is the guarantee
rather than an omission. The new version carries no decision, which under
default approval means it is approved — so an edit neither silently unapproves a
sequence nor manufactures a record claiming somebody reviewed text written a
moment ago. `origin = human_edited` is the honest signal that a person changed
it. An `invalidated` decision can therefore only ever stand against a superseded
version, never against a current one.

The other six messages, their versions and their decisions are untouched, and
the *sequence* is not re-versioned. Editing one message must not re-version the
other six, because that would falsify six lineage records.

## 11. Feature flag and rollout

Two switches, both required:

1. deployment flag `FEATURES__EMAIL_SEQUENCES` (default off);
2. per-Campaign opt-in `cadence_config["sequence"]["enabled"] = true`.

With either off, the Personalization Agent writes exactly the single
`DraftVersion` it has always written, with the same audit action and the same
output keys, and no sequence row is created on any path.

**Off stops generation, not disclosure.** An existing sequence stays fully
visible on both the Emails surface and the Contact page when the deployment switch
is turned off or the Campaign opts out — with its messages, its lineage, its
edits and every recorded decision. Hiding seven approved messages and seven
human decisions is not the same as disabling a feature.

It is shown **read-only**: no new approval, discard or edit can be recorded, and
the refusal is enforced in the POST routes as well as in the template, because a
page left open across a configuration change will still post. The chosen
behaviour is deliberate rather than incidental — recording a fresh decision
against a configuration that no longer produces sequences would contradict the
notice the operator was just shown.

With the switch off and **no** sequence anywhere, the section is omitted
entirely rather than rendered as a permanent "switched off" banner on a page
about single drafts.

Requiring both is what stops enabling the feature from silently changing what
every existing Campaign produces.

**Rollout path.** Enable the deployment flag with no Campaign opted in — nothing
changes. Opt one pilot Campaign in and generate for a handful of contacts. Read
the sequences under Emails, note which validation warnings recur, adjust the
Personalization policy rather than the validator where the wording is the
problem. Widen Campaign by Campaign. Single-draft mode stays available
indefinitely; there is no step at which it is removed.

## 12. Historical compatibility

`SequenceAvailability` (in `app/web/v2/routes.py`) resolves exactly one state
per page, and each is rendered with its own wording by
`_sequence.html::unavailable`:

| State | When | What the page says |
|---|---|---|
| `feature_off` | deployment flag off, nothing generated | the Agent is writing single drafts; nothing is missing |
| `campaign_off` | flag on, this Campaign has not opted in | this Campaign is not set up to generate sequences, and will not until somebody opts it in |
| `pending` | opted in, nothing generated yet | this Campaign is opted in; the whole sequence appears at once when it finishes |
| `failed` | a generation was refused | nothing partial was kept, and nothing further appears on its own |
| `available` | a live sequence exists | the sequence |
| `available` + `read_only` | a sequence exists but a switch is now off | the sequence, plus a notice naming which switch and confirming decisions are unchanged |

Legacy single drafts are unaffected and render exactly as before, alongside.

Each of these is asserted by an HTTP test in
`tests/test_email_sequence_defects.py`. Three of them were unreachable dead code
in the first implementation, and a test asserted the *absence* of a string no
route could produce — it passed trivially and proved nothing.

Historical records fabricate nothing: no follow-ups, no sequence versions, no
Company Intelligence lineage, no delivery state, no review decision that never
happened.

**Compatibility strategy chosen:** *parallel models, no adapter.* A historical
draft is presented as what it is — one message — rather than as a one-message
sequence. An adapter that wrapped old drafts in sequence shape would have to
invent a sequence version, a purpose and six absences, and every one of those
would be a small lie in a system whose whole value is not telling them.

## 13. Migration

One additive, reversible revision: `0926b59b7912`, on top of `b6d4e07a1f38`.
Single Alembic head. Four `CREATE TABLE`s and ten new enum types; nothing
existing is altered, dropped or backfilled.

`downgrade` **refuses outright while any of the four tables holds a row.** Two
of them carry records that exist nowhere else and cannot be re-derived: the
generated message versions are the only copy of the copy, since regenerating
produces different text, and the review rows are the only record of what a human
decided. This follows the convention APP-003 (`c48b1f70a3d2`) established and
KB-001, CI-001 and DAT-017A also follow; the first implementation of this
migration omitted it.

The refusal is conditional, so an empty schema still reverses without ceremony —
which is what keeps the round-trip test meaningful. The error names how much
would be lost and of what kind, and deliberately no more: an operator needs the
scale of what they are about to destroy, not a sample of the content through an
error string.

On an empty schema it drops the four tables **and** the enum types they created —
dropping a table leaves its types behind, and without the explicit `DROP TYPE`
a downgrade followed by a re-upgrade would fail on "type already exists".
Proven by `alembic downgrade -1 && alembic upgrade head && alembic check`, and by
`tests/test_migrations.py`, which asserts both the refusal and the release path
after the data is deleted deliberately.

## 14. Read models and performance

No list page loads a message body. The Emails list issues a bounded number of
statements regardless of page size: the sequences, the position-1 subjects with
their excerpts **cut in SQL**, and the per-sequence decision tallies as one
grouped query. The card read model has no body field at all, so the bound is
structural rather than a convention a later change could drop.

One body is loaded exactly when one message is expanded. The Contact-page table
renders seven rows with subjects and no bodies; a row expansion fetches one.

**Admin diagnosis** issues a constant four statements regardless of how many
times a contact has been regenerated, by batching across the bounded history set
and grouping in Python. It previously issued three queries *per sequence
version* inside a loop, so six regenerations cost nineteen queries and every
further one added three. The ten-version history cap is unchanged.

**Lineage rendering is bounded** by `sequences/lineage.py`: depth, key count,
list length, individual string length and total size, with an explicit
`[truncated: …]` marker wherever something was removed. Stored lineage is
written by trusted code and is small in practice, but nothing enforced that, so
the page size was previously decided by whatever happened to be in the column.
Bounding does not sanitise — escaping is the template's job, Jinja already does
it, and a second implementation here would be a weaker one.

## 15. Future delivery model — designed for, mostly not implemented

**One part of this now exists: operator-initiated Gmail *draft* creation
(#267).** It reuses the identity split below exactly as designed — the exact
message version is the authority, `email_sequence_messages.id` stays stable, and
draft lineage lives in a table of its own rather than in columns on a core row.
See [`GMAIL_DRAFTS.md`](GMAIL_DRAFTS.md) for what was built.

**Everything else below still does not exist.** No Sheets API, no mailbox
polling, no Pub/Sub, no reply detection, no scheduler, no external sync worker,
no automatic draft creation and no sending. The domain is shaped so that the
approved workflow can be added later without replacing or weakening anything
above.

### The approved operating model

1. Personalization generates all seven messages in VMR.
2. The operator reads them in VMR, and edits or discards any they want to
   change. Neither is required — the seven are approved as generated.
3. VMR creates **only the currently actionable message** as a Gmail draft.
4. The operator sends that draft manually from Gmail.
5. A synchronization service detects that it was sent.
6. VMR stores the resulting sent-message identity and thread identity.
7. After the next message's planned delay, and only if no reply or stop
   condition exists, VMR creates the next follow-up as a **reply draft in the
   same thread**.
8. The operator sends it manually. The cycle repeats.
9. Google Sheets is updated automatically from VMR state.
10. VMR remains the source of truth throughout.

### One draft at a time

**This applies to the *delivery* adapter, not to #267.** The draft-only slice
creates all seven standalone drafts on one click, because with no sending there
is no thread to keep in order and no reply to hold a follow-up for; one click
that produced one draft would not be the one-click action the operator asked
for. When the delivery adapter is built, one-at-a-time returns with the
threading it exists to serve.

All seven messages exist and are reviewable in VMR; the *delivery* adapter
creates one external draft per sequence. This is not a simplification — it is
required.
The first sent message establishes the authoritative conversation; later
messages must be replies in that thread; messages must not go out of order;
follow-ups must be held when a reply arrives; planned timing must remain
enforceable; and an operator's Drafts folder must not accumulate seven unsent
drafts per contact.

### Draft-to-sent identity transition

A Gmail draft has a draft id and contains a message identity. When the operator
sends it, Gmail may delete the draft and expose a **different** sent message id.
A draft id is therefore never the permanent identity of a sent message. The
thread id must be captured; the RFC `Message-ID` lineage may be needed for
`In-Reply-To` and `References` on the next follow-up. Through all of it, VMR's
`email_sequence_messages.id` stays stable — which is exactly why that table
carries identity and no text.

### Review state versus delivery state

Kept strictly separate. `SequenceDeliveryState` exists as domain vocabulary:
`not_ready`, `waiting_for_predecessor`, `waiting_for_due_date`,
`eligible_for_draft`, `external_draft_created`, `waiting_for_manual_send`,
`sent_detected`, `held`, `stopped`, `sync_failed`.

**Every message in this build is `not_ready` and nothing advances it.** A
message can be approved text and nowhere near deliverable, and "approved" must
never quietly come to mean "ready to draft" or "sent".

Sequential eligibility — a later message stays ineligible until its predecessor
is confirmed sent, its delay has elapsed, its exact version is still approved,
the sequence is not stopped, the contact is not suppressed, the Campaign is not
paused or archived, the mailbox is connected and no reply or operator hold
exists — is expressible in this model without changing the review model later.

### The actionable-position rule, stated exactly

`current_actionable_position` is computed by walking the **predecessor chain**,
never by counting positions. The rule:

- a stopped sequence has no actionable message;
- the walk starts at the message with no predecessor;
- a message is *cleared* only when it is approved **and** its delivery state says
  it actually went out;
- the first message that is approved but not yet cleared is the actionable one;
- a discard, or an approval an edit withdrew, ends the walk and yields `None`.

Under default approval the walk reaches position 1 on a freshly generated
sequence, where it previously returned `None`. That is the intended consequence
and not an escalation: the value still authorises nothing.

**A discarded message is never stepped over.** Discarding the initial message
leaves the whole sequence with no actionable position until that message is
edited or regenerated and approved again. An earlier implementation skipped past
discarded messages in numeric order, so discarding the initial message promoted
follow-up 1 — which would have opened the conversation while its own copy said
"following up on my earlier note", referring to a message nobody ever sent.

Because no message in this build ever leaves `not_ready`, the only position this
can currently return is the head of the chain. That is the truthful answer:
nothing has been sent, so nothing after the first message can be next.

**The rule the future Gmail adapter inherits:** no follow-up is actionable
unless its required predecessor chain is approved *and* confirmed delivered.

### Stop conditions

`SequenceStopReason` covers `recipient_reply_detected`, `operator_hold`,
`operator_cancelled`, `mailbox_disconnected`, `contact_suppressed`,
`campaign_paused`, `campaign_archived`, `contact_removed_from_campaign` and
`synchronization_failure`. A sequence-level stop blocks the remainder. No
detector writes any of these in this build.

### Recommended external-reference design — deferred, not built

A generic, provider-agnostic table, added **when the adapter that writes to it
is built**. It is documented rather than created because a table with no writer
is precisely the speculative infrastructure `docs/AGENTS.md` forbids, and
because leaving it unbuilt costs nothing: it is purely additive.

```
email_sequence_external_references
  id                              uuid pk
  message_id                      uuid fk -> email_sequence_messages.id
  message_version_id              uuid fk -> email_sequence_message_versions.id  (nullable)
  provider                        varchar(32)     -- 'gmail', never assumed
  account_reference               varchar(255)    -- mailbox identity, never a credential
  external_draft_id               varchar(255)
  external_draft_message_id       varchar(255)
  external_sent_message_id        varchar(255)
  external_thread_id              varchar(255)
  rfc_message_id                  varchar(998)
  in_reply_to                     varchar(998)
  references_chain                jsonb
  external_draft_created_at       timestamptz
  detected_sent_at                timestamptz
  last_synchronized_at            timestamptz
  synchronization_status          varchar(32)
  synchronization_error_category  varchar(64)
  provider_metadata               jsonb           -- bounded
  unique (provider, message_id)
```

A separate table rather than columns on every message row: the overwhelming
majority of sequence messages will never have an external reference, and
speculative provider columns on a core row are how a domain model acquires a
vendor.

**No credential, OAuth token or Google secret belongs in this table or anywhere
else in the schema.**

### Google Sheets boundary

A synchronized operational **projection**, derivable entirely from VMR state.
It is not execution authority. Users never edit the Sheet to mark a message
sent, advance a follow-up, capture an identifier, stop a sequence or schedule
anything. VMR stays authoritative for content, versions, policy and evidence
lineage, review decisions, sequence progression, delivery eligibility,
synchronization state and stop conditions.

### Future user and mailbox association

No user accounts or Workspace OAuth in this task. (#267 later attached a mailbox
grant to the *hosted operator identity* rather than to a Campaign, because a
draft-only action is taken by a person on the page rather than executed by a
campaign. The Campaign-level association described below remains the right shape
for the delivery adapter, and nothing in #267 forecloses it.) The model does not assume one
global sender forever: sender context already lives on the Campaign, the
sequence records its Campaign, and the deferred external-reference table carries
`account_reference` per message. A future user/mailbox association attaches at
Campaign level (which mailbox this Campaign sends from), is inherited by the
sequence, and is recorded per message on the external reference at the moment a
draft is created. No sender identity is hardcoded into sequence persistence.

## 16. Known bounds carried deliberately

- **Form size** is checked from `Content-Length` before the body is read, which
  bounds the ordinary case. A chunked request declares no length, and Starlette
  buffers as it parses, so this is a bound rather than a guarantee; a complete
  fix is a body-size limit at the server or proxy layer, which is a deployment
  concern rather than a route one.
- **Same-origin** is checked from `Sec-Fetch-Site`, falling back to `Origin`. A
  request carrying neither is allowed, so scripted local tools keep working.
  This is a cross-site guard, not authentication — the workbench still has none,
  by design, and refuses to boot outside `APP_ENV=local`.
- **No performance index** was added for the Emails list. At pilot scale the
  filter and sort are a sub-2 ms sequential scan; adding a migration for it now
  would collide with the Campaign Import reconciliation for no present benefit.
  Revisit once superseded-row volume is real.

## 17. Explicit non-goals

Nothing in the fixes above added any of these. Not built, and not partially
built:

Google Sheets API calls · mailbox polling · Gmail push notifications or Pub/Sub ·
reply detection · scheduling · **automatic** draft creation · automatic sending ·
Gmail webhook handling · external sync workers · a second Personalization stage ·
per-follow-up Agent Jobs · any weakening of the Research, Company Intelligence,
Insights, Personalization Policy, evidence, review or audit contracts.

Two entries moved off this list in #267, and only these two: a Gmail OAuth grant
that is separate from sign-in, and `users.drafts.create` called from an explicit
operator click. Gmail *sending* endpoints remain unreachable by construction.

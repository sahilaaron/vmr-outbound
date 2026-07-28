# ADR 0003 — Seller-side knowledge base (KB-001)

Status: Proposed (awaiting Sahil's decision and ChatGPT's review)
Date: 2026-07-28
Baseline: `origin/main` @ `a0865ac`, Alembic head `e61f4c2b7a90`
Deciders: Sahil (owner); implementation prepared by the development agent.

## Context

Everything the system stores today is **prospect-side**: companies, contacts,
captures, snapshots, insights and evidence. All of it is repeated from an
outside source, which is why it carries source URLs, retrieval times,
confidence, freshness and provenance, and why `docs/INSIGHT_EVIDENCE.md` treats
it as untrusted text that is cited rather than obeyed.

The system has nowhere to record the other half: who **we** are, what we sell,
what we may factually say about it, and who we say it to. That knowledge exists
only in the operator's head. Drafting (DRF-*) cannot be built on top of nothing,
and its "prohibited claims" requirement in particular has no home.

This ADR introduces that half.

## Decision

Add a **Knowledge Base**: a seller-side context domain of six record types
(company profile, offerings, proof points, restricted claims, personas, plus the
campaign-to-offering association), with its own service package, its own
workbench area, and a deterministic readiness view.

### D1 — Seller knowledge is structurally separate from prospect data

New tables are prefixed `seller_` and the seller organisation is
`seller_profiles`, **not** a row in `companies`.

`companies` means *prospect company* everywhere in the codebase and in
`docs/COMPANY_WORKSPACE.md`. Putting ourselves in it would have made "is this us
or a prospect?" a column value that every query, every conflict rule and every
future researcher has to remember to filter on. One forgotten filter is a
research job pointed at ourselves, or our own positioning treated as evidence
about a lead.

### D2 — Operator entry is the authorization; there is no second approval

Seller records have no review state, no approval queue, no confidence and no
provenance ledger. An operator typed it; that is the authority for it.

The prospect side needs provenance because it is repeating what someone else
said and nobody here vouches for it. Nothing about that reasoning transfers to a
first-party statement. Adding a review step would have meant an operator
approving their own typing, which records nothing true and trains people to
click through.

Consequence: **the AI never writes to these tables.** No model generates,
enriches, rewrites, approves, scores or summarises seller knowledge. There is no
code path from a model to a seller record.

### D3 — Archive, never delete a record

Every record carries `SellerRecordState`, and no service offers a way to delete
one.

This is what makes the campaign association safe. A campaign that named an
offering must keep resolving to the same row afterwards; deletion would leave a
historical campaign pointing at nothing, and a snapshot copy would mean a
correction had to be hunted down across every campaign that ever used it.

Association rows are the exception and are genuinely deleted on unlink. A link
is a statement an operator can retract, and it is re-creatable from the two
records it joined; there is nothing unrecoverable in it. Every unlink is
audited, including the bulk removal that happens when an offering-scoped
restriction is widened to global.

### D4 — Proof points, claims and personas are shared rows, referenced

A proof point such as "we have covered this market since 2009" is one fact about
the company. It does not become a different fact because a second offering also
uses it, so it is stored once and linked.

The repository does snapshot elsewhere — captured evidence is frozen at capture
time — but that convention exists to preserve *what an external source said at a
moment*. There is no such moment here: an operator corrects a proof point
precisely because the corrected version is the one they want everywhere.

### D5 — The campaign association is organisational only

A campaign may have zero, one, or many offerings. There is no primary, no
ordering, no requirement, and no effect on content. The association table holds
nothing but the two ids, an author and a timestamp — it *cannot* express a
ranking.

Associating an offering never writes email copy and never selects a call to
action. The campaign operator defines purpose, copy, CTA, messaging direction
and sequence configuration directly. This is a product decision, not a
limitation: a system that silently chose a CTA from a picked offering would be
making the campaign's argument on the operator's behalf.

### D6 — Readiness is deterministic, explainable, and not a gate

`app/services/seller/readiness.py` is plain Python over stored columns. No
model, no score, no total, no percentage. Each item reports one of four states
with a reason string naming the fact that produced it.

Nothing in the application consults it before doing anything. A campaign runs
with an entirely empty knowledge base exactly as it did before this existed.

`INCOMPLETE` and `NOT_CONFIGURED` are kept distinct for the same reason a NULL
dossier section is not an empty one: "started and unfinished" and "never begun"
call for different actions.

### D7 — One retrieval boundary for future context assembly

`app/services/seller/context.py` exposes `assemble(session, campaign_id=...)`
returning frozen dataclasses. It is the single place a future drafting step
asks for seller context, so prompt-assembly code never learns this schema.

It also records the **trust-polarity** requirement: prospect evidence is
untrusted external text, seller context is first-party and restricted claims are
policy meant to be obeyed. A future prompt combines a trusted half and an
untrusted half and must not flatten them into one block of "context".

### D8 — A new `KB-` issue prefix, defined by this ADR

`APP-001…008` is a defined contact-first arc and all eight are claimed; `INS-`
and `AIC-` are prospect-side. The seller side is a genuinely new, permanent
product area that will need follow-on cards, so it gets its own prefix — the
same way `APP-*` was introduced by ADR 0002 without a `GITHUB_BACKLOG.md` entry.

### D9 — Behind a default-off switch

`FEATURES__SELLER_KNOWLEDGE_BASE`, off by default per FND-007. While off the
pages are 404, the nav entry is absent, and the campaign page renders exactly as
it did before. Stored data is untouched by the switch.

## Alternatives considered

**Extend `companies` with an `is_seller` flag.** Rejected under D1: it makes a
safety-relevant distinction into a filter everyone must remember.

**Store the knowledge base as one JSONB document.** Rejected: nothing could be
archived, referenced, counted or associated individually, and the
campaign-to-offering relationship would have had no key to point at.

**Model proof points through the existing `insights`/`insight_evidence`
tables.** Rejected under D2. Those tables exist to hold what an outside source
said, with the URL and retrieval time that make a claim checkable. A first-party
statement has no URL and needs none, and reusing the table would have implied
that seller assertions and researched claims are the same kind of thing —
exactly the confusion this ADR exists to prevent.

**Require exactly one primary offering per campaign.** Rejected under D5.

**Add messaging and CTA columns to `Campaign` so the readiness item could be
real.** Rejected as out of scope: those fields belong to CMP-* and DRF-*, and
adding them is not required to add the offering relationship cleanly. The
readiness item reports `NOT_APPLICABLE` with the reason instead.

## Consequences

- Ten new tables, three new enum types, one migration (`b8e5d34a91c7`).
- One new service package, one new workbench area, one new nav section.
- Drafting gains a defined place to read seller context from before it is built.
- Restricted claims are recorded but **enforce nothing today**, because nothing
  generates text yet. That is stated on the page so the record is not mistaken
  for a control.
- The campaign builder is otherwise unchanged.

## Open questions

- **Q1** — Should archiving an offering warn when live campaigns still name it?
  Today it does not; the campaigns are listed on the offering page instead.
- **Q2** — Should the profile keep history? One row is enough for the pilot; the
  partial unique index on `is_current` leaves room to add versions later without
  a rewrite.
- **Q3** — Does drafting need per-campaign restricted claims, distinct from
  global and offering-scoped? Deferred until DRF-* has a real requirement.

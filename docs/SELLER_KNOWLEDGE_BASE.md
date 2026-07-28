# Knowledge Base — seller-side context (KB-001)

What this area actually does, what it deliberately does not do, and what it
cannot do yet. Design rationale is in
[`docs/decisions/0003-seller-knowledge-base.md`](decisions/0003-seller-knowledge-base.md).

## The one distinction to keep

| | Seller knowledge (this area) | Prospect research (Companies, Contacts) |
|---|---|---|
| Where it comes from | An operator types it | Captured or researched from outside sources |
| Why it can be trusted | Because the operator asserts it | It cannot be — it is cited, never obeyed |
| What it carries | The statement | Source URL, retrieval time, confidence, freshness |
| Who may write it | Only an operator | The research pipeline |
| Approval | Entering it *is* the approval | Deterministic gates, review queue for anomalies |

Restricted claims are the sharpest case. A restricted claim is **policy** that a
future drafting step is meant to obey. A researched insight is a **report** of
what a web page said. If those two ever end up in one undifferentiated block of
"context", a prospect's website becomes able to issue instructions. Keeping the
tables, the services and the pages separate is what prevents that.

## Turning it on

```
FEATURES__WORKBENCH=true
FEATURES__SELLER_KNOWLEDGE_BASE=true
```

Off by default (FND-007). While off, every `/knowledge-base` route returns 404,
the nav entry is absent, and the campaign page renders exactly as it did before.
The switch hides the area; it never deletes anything.

The workbench itself only runs when `APP_ENV=local` — it has no authentication,
and `create_app()` refuses to start otherwise.

## Screens

| Route | What it is for |
|---|---|
| `/knowledge-base` | Overview and context readiness |
| `/knowledge-base/company` | The seller organisation profile |
| `/knowledge-base/offerings` | List and create offerings |
| `/knowledge-base/offerings/{id}` | Edit, archive, and associate proof points, restrictions and personas |
| `/knowledge-base/proof-points` | Reusable factual statements |
| `/knowledge-base/restricted-claims` | What generated copy must not say |
| `/knowledge-base/personas` | Reusable buyer personas |
| `/campaigns/{id}` | The offering association, added to the existing page |

Add `?archived=1` to any list to include withdrawn records.

There are no JSON API endpoints. The workbench forms are the write surface, as
they are for campaigns; the JSON API exists for the browser extension, and
nothing outside the app needs to write seller knowledge. Future in-process
readers use `app/services/seller/context.py`, not HTTP.

## The records

### Company profile

One row. `name` is required; everything else is optional. `industries_served`,
`geographies_served`, `capabilities` and `differentiators` are lists of short
labels, one per line in the form.

`NULL` and `[]` are different answers and readiness reports them differently:
`NULL` means nobody filled it in, `[]` means an operator considered it and said
nothing applies.

**Known limitation.** The form cannot currently produce `[]`. An empty textarea
means "not entered", and there is no way to say "I considered this and nothing
applies" without typing something. The distinction is real in the schema, in the
services and in readiness, and an API or a future control can set `[]` — but
through today's UI the three list fields are effectively "entered or not". A
checkbox per field would fix it; it was left out because nothing in the pilot
turns on it yet.

A second profile is impossible — a partial unique index on `is_current` enforces
it in the database.

### Offerings

Any commercial item: product, service, solution, subscription, research report,
research engagement, or other. Active names are unique; archiving frees the name.

### Proof points

Editable in place from the list, under **Edit** on each row — as are restrictions
and personas. Editing never touches associations, which is the point of storing
them by reference.

First-party statements — scale of coverage, years of experience, validated
statistics, approved case-study facts.

Stored **once** and referenced by every offering that uses them, so correcting
one corrects it everywhere. `source_reference` is an *internal* pointer (a
report name, a document, a person), not a web citation, and it is optional: the
operator's entry is the authority.

### Restricted claims

Two scopes. `global` applies whatever a campaign is selling. `offering` applies
only to the offerings it is linked to — and until it is linked to at least one,
it restricts nothing, which the page says out loud. Nothing forces a link: an
operator who has written the rule but not yet decided where it applies has done
something useful and should not lose it.

A `global` claim cannot be linked to an offering. Linking one would imply it had
been narrowed, and `context.assemble` would then return it twice. The service
refuses it, not just the picker.

Widening an offering-scoped claim to global drops its links, because links that
imply a narrowing must not outlive the narrowing. The audit event records which
offerings were dropped, since nothing else can reconstruct that afterwards.

**These enforce nothing today.** Nothing in the system generates text yet, so a
restriction is a record waiting for DRF-*. It is not a control, and the page
does not pretend otherwise.

### Personas

Reusable descriptions of roles we sell to. **Not** `Contact` records. A contact
is a real person with provenance and suppression state who can eventually be
written to; nobody is ever contacted because of a persona, and nothing in this
area creates, matches or modifies a contact.

## Archiving

`SellerRecordState` is `ACTIVE` or `ARCHIVED`. **No service can delete a
record** — not an offering, proof point, restriction or persona.

Association rows are the deliberate exception: unlinking a proof point from an
offering, or an offering from a campaign, really does delete that join row. A
link is a statement an operator can retract, and it is re-creatable from the two
records it joined, so there is nothing unrecoverable to preserve. The records
themselves are never touched by an unlink.

Archiving a record:

- removes it from readiness counts and from the pickers used to build new context;
- keeps every association it already has;
- **leaves every campaign that already names it intact and still resolving to it.**

That last point is asserted in SQL in `tests/test_migrations.py`, because it is
the schema that guarantees it, not a service convention.

An archived offering cannot be *newly* added to a campaign — adding one would be
recording a decision to sell something withdrawn.

## The campaign association

Optional, unordered, unlimited, and with no primary. `campaign_offerings` holds
the two ids, an author and a timestamp; it has no column that could express a
rank.

It records **what a campaign concerns**, for organisation, tracking, reporting
and later context retrieval. It never writes email copy, never selects a call to
action, and never changes the campaign record — `tests/test_seller_knowledge_web.py`
asserts the campaign's own fields are byte-identical after an association.

Zero offerings is a valid, permanent state for a campaign.

## Context readiness

Deterministic Python over stored columns. **No model is involved at any point.**
It is not a score, not an approval, and not a gate — nothing in the application
consults it before doing anything.

Four states:

| State | Meaning |
|---|---|
| `configured` | Everything this item asks for is present |
| `incomplete` | Started, and a named part is missing — the reason says which |
| `not_configured` | Never begun |
| `not_applicable` | The question cannot arise here, for a structural reason the reason states |

Each item carries a reason string naming the fact that produced it, so the
answer is always explainable without reading code.

**Known limitation — "Campaign messaging and CTA" is always `not_applicable`.**
The `Campaign` record holds a name, a description and a status; it has no
purpose, email-instruction, CTA, messaging-direction or sequence columns. Those
belong to CMP-* and DRF-* and adding them was not required to add the offering
relationship, so it was out of scope here. Reporting `not_configured` would show
the operator work they cannot do; omitting the row would hide a real gap in the
context a future assembler needs. `not_applicable` is the only honest answer
against today's schema, and this is the case the fourth state exists for.

## Reading seller context from code

```python
from app.services.seller import context

seller = context.assemble(session, campaign_id=campaign.id)  # or campaign_id=None
seller.profile  # SellerProfile | None
seller.offerings  # tuple[OfferingContext, ...]
seller.offerings[0].proof_points  # active, linked to that offering
seller.offerings[0].restricted_claims
seller.offerings[0].personas
seller.offerings[0].is_archived  # named by the campaign, since withdrawn
seller.global_restricted_claims  # apply whatever the campaign sells
```

Read-only and deterministic: it never writes, audits, calls a model, or costs
anything. Archived records are excluded except that an archived *offering* a
campaign named is still returned, flagged, so a consumer can notice it.

## Audit

Every write emits an `AuditEvent` with actor `operator`:

```
seller_profile.created / .updated
seller_offering.created / .updated / .archived / .restored
seller_offering.proof_point_linked / .proof_point_unlinked
seller_offering.restricted_claim_linked / .restricted_claim_unlinked
seller_offering.persona_linked / .persona_unlinked
seller_proof_point.created / .updated / .archived / .restored
seller_restricted_claim.created / .updated / .archived / .restored
seller_persona.created / .updated / .archived / .restored
campaign.offering_linked / campaign.offering_unlinked
```

Repeating an action that is already true (re-linking, re-archiving) succeeds and
writes **no** audit event, because nothing happened.

## Schema

Migration `b8e5d34a91c7`, on `e61f4c2b7a90`. Ten tables:

```
seller_profiles
seller_offerings                     seller_offering_proof_points
seller_proof_points                  seller_offering_restricted_claims
seller_restricted_claims             seller_offering_personas
seller_personas                      campaign_offerings
```

Three enum types: `seller_record_state`, `seller_offering_type`,
`seller_claim_scope`. `ContextReadinessState` is computed only and has no
PostgreSQL type.

No existing table is altered. No column is added to `campaigns`, `contacts`,
`companies`, `insights`, or any verification, suppression or scoring table.

The downgrade reverses cleanly on an empty database and **refuses** while the
knowledge base holds operator-typed content, because a person wrote it and
nothing in the system can recompute it.

## Deliberately not built

Embeddings, vector search, RAG, document ingestion, autonomous knowledge
extraction, a prompt-management system, AI-generated proof points, per-campaign
messaging fields, a second approval workflow, and any JSON API for seller
records. Ideas beyond this slice belong in `docs/POST_LAUNCH_BACKLOG.md`.

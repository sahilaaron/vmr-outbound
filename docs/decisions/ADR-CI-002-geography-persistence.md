# ADR CI-002: geography relationship as columns, and where the model's authority ends

Status: Accepted for the CI-002 release

Date: 2026-08-01

Branch: `feat/company-intelligence` (base `fd017ea`, on top of CI-001)

## Context

CI-001 shipped `geography` and `specialty` as generic classification rows: a
free-text `model_value`, no vocabulary, `normalization = not_applicable`. That
was honest and nearly useless. "EMEA", "our London office" and "presented at a
conference in Berlin" all became the same shapeless value, and a specialty of
"innovative customer-centric solutions" was stored with exactly the same standing
as "semiconductor failure analysis".

CI-002 was asked to harden both without redesigning the system. The brief was
explicit that a migration is permitted only where the current schema cannot
represent the truth, and equally explicit that structured concepts must not be
flattened into strings to dodge one.

## What the existing schema could already represent

Deliberately assessed before writing anything:

| CI-002 concept | Already representable? | How |
| --- | --- | --- |
| canonical country | **yes** | a taxonomy term at depth 0, `code` = ISO alpha-2 |
| canonical city | **yes** | a taxonomy term at depth 1, parented to its country |
| ISO alpha-2 | **yes** | it *is* the term code |
| ISO alpha-3 | **yes** | a seed alias of the country term — "DEU" genuinely is another way of saying Germany |
| city → country | **yes** | `parent_term_code`, already on the classification |
| taxonomy edition used | **yes** | `taxonomy_id` / `taxonomy_version` |
| evidence and conflict state | **yes** | unchanged from CI-001 |
| specialty hygiene verdict | **yes** | `unresolved_reason`, which is exactly what that column means |
| **relationship to the place** | **no** | — |
| **physical vs commercial presence** | **no** | — |
| **cleaned wording beside the original** | **no** | — |

Seven of ten needed nothing. Three did.

## Decision

**Add three nullable columns to `company_intelligence_classifications` and two
enum types. Add nothing else.**

* `geo_relationship` (`intelligence_geo_relationship`, nullable)
* `presence_kind` (`intelligence_presence_kind`, nullable)
* `normalized_value` (`varchar(500)`, nullable)

Migration `a8f3c92d4e17`, on `c41a9d78e5b2`. Additive, reversible, one head.

### Why relationship is a column

It is what every downstream reader will filter on. "Has a plant in Pune" and
"sells into Pune" describe different companies to approach; a targeting feature
that cannot tell them apart books the wrong meetings, and a scoring feature that
cannot tell them apart is scoring noise.

The available alternative was `unresolved_reason`, a 96-character free-text field
whose declared meaning is "why this value is not settled". Writing `headquarters`
into it would have made a settled value carry a not-settled field, made the
column mean two things, and made every consumer parse a string to recover an
enum. That is precisely the lossy-string shortcut the brief forbids.

### Why presence is a *second* column rather than a lookup

`presence_kind` is derived deterministically from `geo_relationship` — the
mapping lives in `geography.PRESENCE_FOR_RELATIONSHIP` and nothing else computes
it. Storing it anyway is a deliberate, small redundancy:

* a consumer asking "where is this company physically" should not have to
  re-implement a thirteen-way mapping, and every consumer that did would be a
  place for it to drift;
* the distinction between a factory and a sales territory should survive in the
  row itself, so a person reading the table — not the read model — can still see
  it;
* it makes `WHERE presence_kind = 'PHYSICAL'` an index-able question.

Two check constraints keep the pair honest: both columns are geography-only, and
neither may exist without the other.

### Why `normalized_value` is not `term_label`

`term_label` names something in a controlled vocabulary. A cleaned specialty
belongs to no vocabulary — specialty deliberately has no canonical list. Reusing
the column would have made "this matched a canonical term" and "we removed the
word *leading*" impossible to tell apart, which is the same class of mistake as
overwriting `model_value` with the canonical label: it destroys the difference an
operator needs in order to notice a mapping that is valid and wrong.

The column is generic rather than specialty-specific, because "the
deterministically cleaned form of what the producer wrote" is a concept the other
dimensions may want later.

### Why geography joins the taxonomy rather than getting its own tables

A geography reference is a controlled, versioned vocabulary with a two-level
hierarchy and aliases. `intelligence_taxonomies` / `_terms` / `_aliases` already
is that, exactly, including the edition semantics that let a classification
survive a vocabulary replacement. A parallel set of country/city tables would
have been the same shape with a different name and its own bugs.

The one adaptation: geography reads **either** depth, because "United Kingdom"
and "London" are both valid geography values. It is therefore absent from
`_DEPTH_FOR_DIMENSION`, where industry pins to 0 and subindustry to 1.

## Where the model's authority ends

Stated here because it is the load-bearing part of CI-002 and it is a boundary,
not an implementation detail.

**Deterministic code owns:** which places exist as candidates; canonical
identity and country inference; the relationship and presence enums; evidence
handle validity; whether cited evidence actually mentions the place; promotional
and broadness detection; duplicate detection; caps; ordering; unresolved states;
persistence; current-version selection.

**The model owns:** the relationship between a company and a candidate place,
and the wording of a specialty.

That split is chosen on capability, not on preference. No regex distinguishes
"headquartered in Pune" from "presented at a conference in Pune" — that needs
language understanding. Equally, no language model should be trusted to decide
whether a place it named exists in the evidence, because a fabricated location is
indistinguishable from a real one once it is a row in a table.

The model receives `allowed_tools=()` and a closed list of candidate handles. A
handle it did not receive is refused. A relationship outside the enum becomes
`unclear`. A relationship that contradicts the context a place was found in is
stored as a disagreement rather than resolved in the model's favour.

## Consequences

* **Every company re-classifies once.** The policy version moved 1 → 2 and the
  active vocabulary set gained geography, both of which are in the input digest.
  That is correct — the old version genuinely was produced under different rules —
  but it makes the first backfill after this upgrade a full one. Documented in
  `docs/COMPANY_INTELLIGENCE_BACKFILL.md`.
* **Operator decisions survive it**, because decisions are company-scoped
  (CI-001).
* **Existing rows are untouched.** All three columns are NULL for every CI-001
  classification, which is the truthful reading: those rows never asserted a
  relationship.
* **A place outside the edition produces nothing**, rather than a wrong answer.
  Coverage grows by publishing a new edition.
* **"Our Reading site" is missed.** Ambiguous surfaces need a capital *and* a
  preposition immediately in front, or their country named nearby. Missing a real
  place is recoverable; asserting a false one is not.

## Rejected alternatives

**Encode the relationship in `unresolved_reason` or in the rationale text.**
Rejected: it makes a structured concept a string, gives one column two meanings,
and pushes parsing onto every consumer.

**A separate `company_geographies` table.** Rejected: it would duplicate
versioning, evidence links, conflict groups, review lineage and current-version
selection, and would put geography outside the one read model everything else
goes through. The brief's "reuse existing structures where truthful" is the right
call here — a geography *is* a classification, with two extra properties.

**A closed specialty taxonomy.** Rejected on the merits, not on effort:
specialties are domain-specific and move fast, and a closed global list would
reject correct values while looking authoritative. Open vocabulary plus
deterministic hygiene, documented in
`docs/COMPANY_INTELLIGENCE_TAXONOMY.md`.

**A second model call for geography.** Rejected: it would double the per-company
cost for a question the same evidence already answers. One structured answer
covers every dimension, and the pre-call idempotency check is unchanged.

## When to revisit

* A consumer needs sub-country administrative regions (state, province). The
  hierarchy already supports a third depth; the data does not have one.
* The edition's coverage becomes the limiting factor — measurable as a rising
  count of companies with research and no geography rows.
* Specialty accumulates enough curated aliases that a canonical list becomes
  discoverable from the data rather than invented ahead of it.

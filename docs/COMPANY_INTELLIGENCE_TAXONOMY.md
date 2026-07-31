# Company Intelligence: taxonomy and vocabulary strategy (CI-001)

## The problem a vocabulary solves, and the one it creates

Without a controlled vocabulary, a classifier writes "Pharma & Healthcare" for
one company, "Pharmaceuticals" for the next and "pharma/healthcare" for a third.
Nothing can be filtered, counted or targeted, and nobody notices until an
audience comes back a third the size it should be.

With one, a value the vocabulary does not contain has to go *somewhere*. The
usual answer — force it to the nearest match — is worse than the disease: it is
wrong silently, it is wrong plausibly, and there is no record that a choice was
made. This design refuses that trade. An unmatched value is stored with the
producer's exact wording, marked `unmapped`, and put in front of a person.

## Editions, not lists

A vocabulary is a **released edition**: a row in `intelligence_taxonomies` with a
`version`, its terms, and its aliases. Editions are never edited. Publishing a
corrected industry list means creating a new edition and activating it; the old
one is marked retired and stays readable forever.

That is what makes "future vocabulary replacement without destroying historical
classifications" true rather than aspirational:

* a stored classification records the `taxonomy_id`, `taxonomy_version`,
  `term_id`, `term_code` and `term_label` it used;
* the term it points at cannot be deleted (`ON DELETE RESTRICT`);
* so a classification made under `2026.07` still resolves and still displays
  correctly after `2027.01` is active.

Exactly one edition per dimension is active at a time, enforced by a partial
unique index rather than by a service check — activating an edition is two row
updates and a half-applied activation must not be representable.

## Which dimensions have a vocabulary, and why the others do not

| Dimension | Vocabulary | Reasoning |
| --- | --- | --- |
| `industry` | Yes — supplied list, depth 0 | 16 categories, enumerable, stable |
| `subindustry` | Yes — supplied list, depth 1 | 245 children of the above |
| `business_model` | Yes — 12 terms | Small and genuinely closed |
| `company_type` | Yes — 14 terms | Small and genuinely closed |
| `customer_segment` | Yes — 10 terms | Coarse by nature |
| `operating_market` | Yes — 11 regions | Coarse regions only |
| `geography` | **No** | Specific countries, states and cities. A partial list would reject correct values while looking authoritative |
| `product` | **No** | Company-specific by definition |
| `service` | **No** | Company-specific by definition |
| `specialty` | **No** | Company-specific by definition |
| `capability` | **No** | Company-specific by definition |

A dimension with no vocabulary records `normalization = not_applicable`, which
says "free text is the intended representation here", and is different from
`unmapped`, which says "this dimension has a vocabulary and this value is not in
it". Keeping those apart is what lets an operator find the second and ignore the
first.

**Industry and subindustry share one edition.** They are two depths of a single
hierarchy, not two vocabularies. Two editions could disagree about which
categories exist; one cannot. `taxonomy.NORMALIZING_DIMENSION` declares the
mapping and `_DEPTH_FOR_DIMENSION` declares which depth each dimension reads, so a
subindustry can never resolve to a category or vice versa.

## The supplied industry taxonomy, used verbatim

`app/services/company_intelligence/data/industry_categories.json` is the
operator-supplied file, committed unchanged. Sixteen categories become depth-0
terms; their 245 entries become depth-1 terms. Nothing was renamed, merged,
dropped, reordered or invented.

**One adjustment, and it is the only one.** Every category ends with an entry
called `"Others"`. Sixteen terms normalizing to the identical string `others`
would make a lookup return whichever row the database happened to return first —
a silent, order-dependent misclassification, and the worst possible failure for a
component whose whole purpose is not guessing. Each is therefore stored as:

* canonical label `Others (Manufacturing)`, `Others (Retail)`, …
* code `manufacturing-others`, `retail-others`, …

The supplied word is preserved; only the ambiguity is removed. The bare word
`"others"` is deliberately **not** registered as an alias of any of them: it
genuinely is ambiguous, and an alias that guessed would be worse than an unmapped
value an operator can see. `test_repeated_others_entries_are_disambiguated_not_collapsed`
locks this in.

## Normalization: exact, and deliberately boring

`normalization.normalize_term` does five things and no more: Unicode NFKD with
combining marks dropped, case folding, `&` → `and`, every remaining
non-alphanumeric run collapsed to one space, trim.

It does **not** stem, strip plurals, remove stopwords, expand synonyms or compute
edit distance. "Coating" and "Coatings" stay different strings.

That is not laziness. A fuzzy matcher makes decisions nobody reviewed, in code
nobody reads, and its mistakes are invisible by construction — a value that
matched 87% looks exactly like a value that matched exactly. The way to make two
spellings mean one thing here is an **alias**: a row, with a source, an author and
a timestamp, visible in the vocabulary browser.

## Aliases, and why a model cannot create one

`intelligence_taxonomy_aliases` carries a `source`:

* `seed` — shipped with the edition, authoritative;
* `operator` — a human added it, authoritative immediately;
* `model_suggestion` — recorded, and **not used for matching** until a human
  approves it (`approved_at IS NOT NULL` is part of the resolution query).

A producer that could widen its own vocabulary could quietly redefine what every
classification means. Keeping model suggestions inert until approved is what
prevents that, and it costs nothing: the suggestion is still visible, so the
approval is one click rather than a re-discovery.

One alias means one term within an edition, enforced by
`UNIQUE (taxonomy_id, normalized_alias)`. `add_alias` refuses a second meaning
rather than picking one.

### Seeded industry aliases

Only one mechanical form: for a category containing `" & "`, each side on its own
("Pharma", "Healthcare"), and only when that fragment is not also a fragment of
another category. The ambiguous fragments — `Technology`, `Minerals`, `Metals`,
`Communication`, `Material`, `Power`, `Transportation`, `Engineering`,
`Semiconductor`, `Insurance`, `Financial Services`, `Defence`, `Beverages` — are
excluded by name in `seed._AMBIGUOUS_FRAGMENTS`, so the seed is deterministic
rather than dependent on which category is processed first.

Everything beyond that is a human decision made in the Admin vocabulary screen.

## Resolution order

1. Exact match on an active term's `normalized_label`, at the right depth.
2. Exact match on an **approved** alias of an active term, at the right depth.
3. Otherwise `unmapped`, with the caller's wording preserved.

A dimension that *should* normalize but has no active edition reports `unmapped`
rather than `not_applicable`, so a missing vocabulary shows up in review instead
of passing as intended free text.

## Publishing a new edition

```python
from app.services.company_intelligence import taxonomy

edition = taxonomy.create_taxonomy(
    session,
    dimension=IntelligenceDimension.INDUSTRY,
    version="2027.01",
    title="VMR industry taxonomy (2027 revision)",
    source="operator-supplied revision",
)
parent = taxonomy.add_term(
    session, taxonomy=edition, code="manufacturing", canonical_label="Manufacturing"
)
taxonomy.add_term(
    session,
    taxonomy=edition,
    code="manufacturing--pumps",
    canonical_label="Pumps, Valves & Fluid Handling",
    parent=parent,
)
taxonomy.add_alias(session, term=parent, alias="industrial manufacturing")
taxonomy.activate_taxonomy(session, taxonomy=edition)
```

Activation is audited (`company_intelligence.taxonomy_activated`). Existing
classifications are untouched; the next production run uses the new edition, and
its `input_digest` changes accordingly, which is what makes the re-classification
a new version rather than a silent reinterpretation of the old one.

## Seeding

`seed.seed_vocabularies(session)` publishes and activates the first-release
editions. Idempotent at the edition level: an edition that already exists is
reported as skipped and left exactly as it is — including any aliases an operator
has added, because this function has no business deciding those were wrong.

## What this does not claim

**The taxonomy is not complete, and this document does not claim it is.** It
covers the industries this business researches, at the granularity the supplied
file chose. Values outside it are recorded verbatim and surfaced as unmapped —
which is the mechanism by which the gaps become visible work rather than silent
misclassification.

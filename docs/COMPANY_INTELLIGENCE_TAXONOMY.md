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
| `geography` | **Yes (CI-002)** — versioned reference edition, two depths | 60 countries (depth 0) and 259 cities (depth 1) |
| `product` | **No** | Company-specific by definition |
| `service` | **No** | Company-specific by definition |
| `specialty` | **No** — open vocabulary with deterministic hygiene | Domain-specific and fast-moving; see the specialty section below |
| `capability` | **No** | Company-specific by definition |

A dimension with no vocabulary records `normalization = not_applicable`, which
says "free text is the intended representation here", and is different from
`unmapped`, which says "this dimension has a vocabulary and this value is not in
it". Keeping those apart is what lets an operator find the second and ignore the
first.

**Geography shares one edition across both depths**, for the same reason: a
country list and a city list that could disagree about which countries exist
would be two vocabularies pretending to be one. Unlike industry/subindustry,
geography reads *either* depth — "United Kingdom" and "London" are both valid
geography values — so it is deliberately absent from `_DEPTH_FOR_DIMENSION`.

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

---

# The geography reference edition (CI-002)

## What it is

`app/services/company_intelligence/data/geography_base.json`, edition `2026.08`:
**60 countries and 259 cities**, at least three cities per country and more
where three would obviously misrepresent it (the United States has ten, India
nine, the United Kingdom nine).

Coverage is deliberate across all nine declared regions: North America, Latin
America, Europe, Middle East and GCC, Africa, South Asia, East Asia, Southeast
Asia, Oceania. A test asserts that every declared region is actually
represented, so the claim cannot rot.

Each country carries its canonical name, ISO 3166-1 alpha-2 and alpha-3 codes,
common aliases and abbreviations. Each city carries its canonical name, its
country, and its alternate or former names — Bengaluru/Bangalore,
Mumbai/Bombay, Ho Chi Minh City/Saigon, Washington, D.C./Washington DC,
Munich/München, Zurich/Zürich.

## Selection criteria, stated honestly

Countries were chosen for **commercial relevance to this product's B2B outbound
work** — where the industries it researches concentrate buyers, suppliers,
manufacturing and R&D — with deliberate coverage across the nine regions.

**This is not a claim that these are the world's fifty most important
countries, and it is not a ranking.** It is the set this product needs today.
A country outside it produces no geography at all, which is a visible gap rather
than a wrong answer.

## Provenance and licence

* Country names and the ISO 3166-1 alpha-2 / alpha-3 code assignments are the
  **published public standard identifiers**. Short factual codes, not a
  copyrightable compilation, and only the ~60 entries this product needs are
  included.
* City names and their common alternate or former names are **widely published
  general geographic knowledge**.
* **No third-party geographic database is vendored.** Nothing here is copied
  from a licensed gazetteer.
* **No runtime network dependency.** There is no geocoding call, no external
  geography API, and none is planned for this release.
* **No fabricated facts.** No coordinates, populations, rankings or
  administrative hierarchies are asserted anywhere in the file. The only
  hierarchy is city → country, which is what the product needs and all it claims.

The file itself carries `source`, `license`, `criteria` and `update_procedure`
fields, so the provenance travels with the data rather than living only here.

## Updating it

Adding or correcting entries is a **new edition**: bump `edition` in the JSON,
add the entries, and `seed_vocabularies` publishes and activates it. Editions
are never edited in place, so a geography classified under `2026.08` keeps
resolving after a later edition is active — and because the active edition is
part of the input digest, the next production run for each company genuinely
re-classifies rather than silently reinterpreting.

## Ambiguity: the reason this is not just a dictionary

A dictionary lookup would turn "reading the manual" into a town in Berkshire and
"Michael Jordan" into a country. The file therefore marks **ambiguous surfaces**
per entry — surfaces that are ordinary English words, common given names or
product words: Bath, Cambridge, David, Jordan, Lima, Mobile, Nice, Orange,
Panama, Reading, Turkey, Victoria, Washington.

Note that this is per *surface*, not per entry: "Washington, D.C." is
unambiguous while its alias "Washington" is not.

An ambiguous surface becomes a candidate only when **both** hold:

1. it is **capitalised** in the original text — a weak signal, used as a
   necessary condition and never a sufficient one; and
2. either a preposition sits **immediately before it** (`in`, `at`, `near`,
   `from`, `into`, `throughout`, `across`, `outside`, `toward`, `towards`), or
   its own **country is named in the same text**.

The second condition applies to cities only. A country cannot vouch for itself —
without that rule, "Michael Jordan joined as operations director" unlocked the
country Jordan, because the co-occurrence check found "Jordan" in the text and
concluded Jordan's country had been named.

The window used to be six tokens and any location word. That let "operations",
four tokens away, make a country. A preposition directly in front of a
capitalised name is a narrower claim and a better one. The cost is that "our
Reading site" is missed — which is the right way round to be wrong.

---

# Specialty: an open vocabulary with rules (CI-002)

## Why there is no closed list

Specialties are domain-specific and move fast. "Antibody-drug conjugate
development" did not exist as a category a decade ago and "grid-scale battery
integration" barely did. A closed global taxonomy would be wrong within a year
and would reject correct values while looking authoritative — the exact failure
the geography edition avoids by being honest about its scope.

So specialty has **no canonical term list**. What it has instead:

* normalized text and deterministic duplicate detection;
* conservative singular/plural folding (a curated list of Latin plurals plus a
  not-a-plural ending guard, so "analysis" never becomes "analysi");
* promotional-phrase detection;
* broadness and minimum-specificity checks;
* an evidence requirement;
* deterministic ordering, count and length caps;
* operator-visible unresolved values;
* room for curated aliases later.

## Product / Service / Capability / Specialty

Four dimensions can describe the same sentence. The prompt states the boundary
and the producer enforces the consequence.

| Dimension | Is | Example |
| --- | --- | --- |
| Product | a thing sold or licensed | liquid cooling plate |
| Service | a deliverable performed for a customer | clinical trial recruitment |
| Capability | an ability, process or facility possessed | aseptic fill-finish capacity |
| Specialty | a domain concentration: subject matter + work type | EV battery thermal management |

The same evidence may genuinely support more than one. What it may not do is
put the identical wording in several — a specialty that repeats a product,
service or capability word for word is kept but marked
`dimension_boundary_unclear`, because the boundary really is unclear there and
guessing which side it falls on is not a deterministic layer's job.

## Accept, clean, unresolved, reject

**Accept** — specific, factual, non-promotional, evidence-supported. Stored
resolved.

**Clean** — a promotional modifier is removed and what remains is the *same*
factual specialty. "Leading cold-chain logistics provider" → "cold-chain
logistics". Cleaning only ever strips tokens from a curated list at the **edges**
of the phrase. It never reorders, substitutes or rephrases, and a promotional
word in the *middle* is never surgically removed — "cold chain innovative
packaging" stays unresolved, because removing the middle word would invent a
phrase nobody wrote. The original wording is always kept in `model_value`.

**Unresolved** — plausible but not settled. Too broad ("technology",
"consulting", "digital transformation leader"), promotional in a way that cannot
be safely stripped, longer than a specialty should be, or indistinguishable from
another dimension. Kept and shown with a reason. **This is the outcome the design
bends toward**: a suggestion an operator can judge is worth more than either a
false fact or a silent deletion.

**Reject** — only for: empty or malformed, wording that is *entirely*
promotional with nothing factual inside it ("trusted partner", "world-class
trusted partner"), and outcome claims ("driving growth", "improving efficiency",
"unlocking value"). Rejections are counted in the version's warnings, so even a
rejection is visible.

## Protected phrases

Some "promotional" words are load-bearing technical vocabulary. The rules are
phrase-aware precisely because of these:

| Phrase | Verdict | Why |
| --- | --- | --- |
| next-generation sequencing | accept | a laboratory technique |
| next-generation solutions | reject | a brochure |
| advanced driver assistance systems | accept | ADAS is the term of art |
| sustainable aviation fuel | accept | SAF is not "aviation fuel" |
| global navigation satellite systems | accept | GNSS is the term of art |
| world-class quality | unresolved | strip the boast and "quality" is a field |

The protected list is short by design — short enough to read in one sitting —
and is extended by a human, never by a model.

## What the model may not do

The producer cannot invent or install a canonical term or an alias for any
dimension. Model-proposed aliases are stored inert until an operator approves
them (CI-001), and specialty has no canonical list for a model to widen. The
only thing a model contributes here is wording, and every piece of wording it
contributes passes through the rules above before anything is stored.

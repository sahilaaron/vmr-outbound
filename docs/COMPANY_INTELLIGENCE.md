# Company Intelligence (CI-001)

Structured, versioned, evidence-linked understanding of a Company, derived only
from Research evidence that has already been committed.

It answers: what industry and subindustry is this company in, what does it make
and sell, where does it operate, who does it sell to, what distinguishes it, what
kind of organisation is it — and, just as importantly, **which of those answers
are strong, which are uncertain, which conflict, and which nobody could
establish.**

It is deliberately not a summary and not prose. A paragraph of AI description
cannot be filtered, cannot be corrected one value at a time, cannot cite the
sentence it came from, and cannot say which part of itself is a guess.

---

## The five rules everything else follows from

1. **Research evidence is immutable and Company Intelligence only reads it.**
   No path in this area writes a `CompanyResearchSubmission`, a
   `CompanyDossierVersion`, an `Insight` or an `InsightEvidence` row.
2. **No canonical Company field is ever written.** `companies.industry` and its
   neighbours have their own provenance model. A classification is evidence
   about a Company, never an overwrite of it.
3. **Every classification points at evidence or says there was none.** There is
   no third option and no silent drop.
4. **A model-produced classification is not verified.** It becomes
   operator-confirmed only when a person confirms it, and that confirmation is an
   auditable decision with an author and a time.
5. **Nothing here makes a Contact outreach-eligible.** No classification releases
   a suppression, satisfies verification, bypasses review, or reaches Sending.
   Company Intelligence cannot send.

---

## The domain model

Eight tables. The split between them is the design; see the module docstrings in
`app/models/company_intelligence.py` and `app/models/intelligence_taxonomy.py`
for the per-table reasoning.

### Vocabulary

| Table | What it is |
| --- | --- |
| `intelligence_taxonomies` | One **released edition** of a vocabulary for one dimension. Exactly one active per dimension, enforced by a partial unique index. |
| `intelligence_taxonomy_terms` | One canonical value inside an edition, optionally the child of another. |
| `intelligence_taxonomy_aliases` | Another way of saying a term, carrying its source and whether a human approved it. |

### Understanding

| Table | What it is |
| --- | --- |
| `company_intelligence_versions` | One immutable reading of one Company's committed evidence, naming the exact dossier version, sourced facts, taxonomy editions, producer and policy it used, plus a digest over all of them. |
| `company_intelligence_classifications` | One classified value. Rows, not a blob, because these are reviewed, filtered and evidenced one at a time. |
| `company_intelligence_evidence_links` | Why a classification exists: a pointer back into INS-001 insights, insight evidence, dossier sections and source URLs. |
| `company_intelligence_conflicts` | A disagreement kept as a disagreement. Competing values share a conflict group and none of them wins. |
| `company_intelligence_decisions` | Append-only operator judgements. Company-scoped authority, version-scoped lineage. |

### Work

| Table | What it is |
| --- | --- |
| `company_intelligence_jobs` | Durable, company-scoped production work. Its own queue — see `docs/decisions/ADR-CI-001-pipeline-placement.md`. |
| `company_intelligence_backfill_runs` / `_items` | A bounded, resumable pass with a cursor and a truthful per-company outcome. |

### The dimensions

A closed set of eleven: `industry`, `subindustry`, `product`, `service`,
`specialty`, `capability`, `geography`, `operating_market`, `customer_segment`,
`business_model`, `company_type`.

Primary and secondary industries are the same dimension at different ranks, not
two dimensions. They differ by rank, not by kind, and "promote this secondary
industry to primary" should be a rank change rather than a cross-dimension move.

### The value states

| State | Means |
| --- | --- |
| `resolved` | Supported by persisted evidence and (where a vocabulary exists) normalized. |
| `unresolved` | Proposed but not settled — no evidence, or no vocabulary match. Kept and visible, never dropped. |
| `unknown` | The producer addressed this dimension and the evidence said nothing. Different from a dimension with no row, which was never addressed. |
| `conflicted` | The evidence supports two answers that cannot both be true. |

### Geography and specialty (CI-002)

Two dimensions are hardened beyond the generic contract above.

**Geography** is no longer free text. Deterministic extraction finds the places
one company's evidence actually names, canonicalises them against a versioned
reference edition of 60 countries and 259 cities, and hands the model a short
list of candidate handles. The model assigns a **relationship** —
`headquarters`, `office`, `branch`, `facility`, `manufacturing`,
`research_and_development`, `warehouse`, `distribution`, `operations`,
`commercial_market`, `planned_presence`, `historical_presence`, `unclear` — and
deterministic code derives a **presence kind** from it: `physical`,
`commercial`, `prospective`, `former`, `unknown`.

The division of labour is the design. Code decides what places exist, so the
model cannot invent a location. The model decides the relationship, because no
regex distinguishes "headquartered in Pune" from "presented at a conference in
Pune". See `docs/COMPANY_INTELLIGENCE_EVIDENCE.md` for how each half is checked.

Only `physical` and `commercial` count as current presence. A planned plant and
a closed one are both real and neither is a place the company is today, so both
are stored, shown, and never settled.

**Specialty** passes through deterministic hygiene with four outcomes — accept,
clean, leave unresolved, reject — described in
`docs/COMPANY_INTELLIGENCE_TAXONOMY.md`. A specialty is *a concrete area of
domain expertise, technical focus, service practice or delivery competence that
is narrower than the broad industry and more specific than the general company
type.* "Semiconductor failure analysis" is one; "world-class customer-centric
solutions" is not.

---

## The producer contract

`app/services/company_intelligence/runner.py` → `produce_for_company`.

**Input.** `inputs.assemble` builds one frozen `IntelligenceInput` containing:
the Company's name, domain and domain-authority state; the current
`CompanyDossierVersion`'s populated sections; up to 120 `SUPPORTED` INS-001
claims with up to 3 evidence rows each, each given a short handle (`F1`, `F2`…);
the active taxonomy editions; and the producer and policy versions.

The producer receives that object and nothing else. It has no session, no
network and no tools (`allowed_tools=()`), so "does not browse" is structural
rather than a promise.

**Output.** One JSON object:

```json
{
  "classifications": [
    {"dimension": "industry", "value": "Manufacturing", "is_primary": true,
     "evidence": ["F1", "F4"], "confidence": 0.82, "rationale": "one sentence"}
  ],
  "conflicts": [
    {"dimension": "industry", "values": ["A", "B"], "statement": "what disagrees",
     "evidence": ["F2"]}
  ],
  "geography": [
    {"candidate": "G1", "relationship": "headquarters",
     "evidence": ["F2"], "confidence": 0.9, "rationale": "one sentence"}
  ],
  "unknown_dimensions": ["business_model"]
}
```

Places arrive only through `geography`, keyed by a candidate handle. A
`geography` entry in `classifications` is dropped with a warning: accepting one
would let the model name a location deterministic extraction never found.

**Validation policy** (`producer.POLICY_VERSION`, currently `2`), in order:

1. Not a JSON object, or `classifications` not a list → **malformed**. Nothing is
   persisted; the caller may retry. Partial parsing is never attempted.
2. Unknown dimension name → dropped and counted.
3. Cited handle that does not resolve to a fact this run actually showed →
   dropped and counted. A bare URL is accepted only if it was in the evidence.
4. No surviving citation → stored `unresolved` / `insufficient` with reason
   `no_evidence`. **Not dropped**: hiding the suggestion defeats review.
5. No vocabulary match on a dimension that has one → `unresolved` with reason
   `unmapped_value`, keeping the producer's exact wording.
6. Named in a conflict → `conflicted`, sharing a group. A conflict with fewer
   than two surviving members is dropped.
7. Ranks are dense and deterministic per dimension; `is_primary` applies only to
   industry, only at rank 0. Per-dimension caps drop the excess and say so.
8. **Specialty hygiene** (CI-002). Rejected only for empty, purely promotional
   or outcome-claim wording; cleaned when a modifier can be removed without
   changing the meaning; otherwise kept `unresolved` with a reason.
9. **Geography validation** (CI-002). An unknown candidate handle is dropped; an
   unknown relationship becomes `unclear`; cited evidence must be evidence that
   actually mentioned the place; a relationship that contradicts the context the
   place was found in is stored as a disagreement; a candidate the model ignored
   is stored `unclear` rather than lost. A settled city then infers its country —
   never the reverse.
10. **Geography vocabulary drift.** A place the vendored extraction base knows
    but the *active* vocabulary edition does not (an older published edition, or
    a base updated without re-seeding) is stored `unresolved` with reason
    `unmapped_value` — the same rule every other normalizing dimension follows —
    with its CI-002 relationship and presence intact, and a version warning
    naming the place and the remedy (re-seed the vocabulary). It is never stored
    `resolved` with no term behind it; the schema's `resolved_has_value`
    contract forbids that shape.

## Operating model: automatic handoff

The normal path requires no operator step and no dedicated process:

1. Research commits a new **usable** dossier (a dossier stored with
   insufficient evidence is recorded, not classified).
2. In the same transaction, `company_intelligence.handoff.enqueue_after_research`
   queues **one** idempotent, company-scoped job — keyed by `(company, input
   digest)`, marked `requested_by=research_handoff`. An already-answered digest
   queues nothing; an already-open job is reused; unchanged input never
   duplicates work. A new job appears only when the Research input or the
   producer policy/version changed.
3. The **shared Agent worker** (`run_agent_worker.py`) drains the Company
   Intelligence queue whenever the Campaign Agent queue is idle (skippable with
   `--skip-company-intelligence`, and skipped automatically when the worker is
   scoped with `--agent`). Claims count against `--max-jobs`: each is one model
   call.
4. The produced version is company-scoped and serves every Contact linked to
   the Company; nothing is duplicated per Campaign Contact.

`run_company_intelligence_worker.py` remains as an optional bounded
recovery/debug tool. Backfill remains for historical Companies, recovery,
policy-version migrations and deliberate reprocessing — it is not part of the
normal path. The Admin Workbench surfaces the handoff on the Contact diagnosis
(Research stage), the Company page (latest job + how it was queued) and the
Failures inbox (failed Company Intelligence jobs).

**Idempotency.** `input_digest` is SHA-256 over the dossier version, the exact
sourced facts (id + content hash), the taxonomy editions, and the producer and
policy versions. `UNIQUE (company_id, input_digest)` means the same question
cannot produce a second version even under a race between two workers. The runner
checks for an existing version **before** calling the model, so a repeat run
costs nothing.

**Persistence failures.** The worker executes production under a savepoint: if a
produced row violates a database contract (`IntegrityError` at flush), only the
produced rows roll back — the claim survives, the job fails durably with code
`persistence_integrity_error` (constraint named, statement and parameters never
recorded), and the continuous worker moves on to the next job. The failure is
not retryable: nothing was persisted, so a later re-enqueue after the defect is
fixed costs exactly one new model call. Operational errors (a lost connection)
still propagate — a durable outcome cannot be recorded on a broken connection.

**Versioning.** Any change to the dossier, the facts, the vocabulary, the
producer version or the policy version changes the digest and therefore produces
a new version. The previous version is superseded, never deleted.

**What is not stored.** The prompt and the raw answer text. Only a SHA-256 of the
answer is kept, so two runs can be compared without putting prompt framing or
configuration into the database.

---

## Operator review semantics

Four actions, all append-only, none of which edits a produced version:

| Action | Effect on the effective value |
| --- | --- |
| `confirm` | Stands, and a human is now responsible for it (`operator_confirmed`). |
| `correct` | Replaced by a canonical term or free text (`operator_corrected`). The original stays in the stored version. |
| `mark_unresolved` | Kept but explicitly unsettled (`operator_unresolved`). |
| `reject` | Removed from the effective set. The classification row is untouched. |

Plus **map alias**, which is a vocabulary action rather than a decision: it
teaches the active edition that a written value means a canonical term. It does
**not** retro-fit stored classifications — versions are immutable, so an alias
changes what the *next* run resolves, and the value in front of the operator is
fixed with a correction.

**Decisions are company-scoped with version-scoped lineage.** A decision names the
version and classification it was made against (so the reasoning stays
inspectable) but its authority is a statement about the Company. That is what
lets a confirmation survive the next production run instead of being discarded by
it. A decision concerning a value the newest version no longer proposes is still
honoured and reported as `operator_only`, counted in `stale_decision_count` —
because a person and the newest model run disagreeing is worth seeing, not
swallowing.

Superseding is two row updates and never a delete: change your mind and both
decisions remain, in order, with their authors and `superseded_by_id` linking
them.

---

## The read service

`app/services/company_intelligence/read.py` is the **only supported way in**.
Nothing outside the package should query the intelligence tables directly.

```python
from app.services.company_intelligence.read import get_company_intelligence

view = get_company_intelligence(session, company_id=company.id)
view.latest_model_version  # newest produced, reviewed or not
view.latest_reviewed_version  # newest an operator acted on, or None
view.current_version  # the selected understanding
view.classifications  # effective values, decisions applied
view.conflicts  # disagreements, unflattened
view.unresolved()  # unresolved + conflicted + unknown
view.settled_values(dimension)  # resolved AND backed — the targeting answer
view.primary_industry()  # None rather than a guess when conflicted
view.geographies(current_only=True)  # places the company is, now
view.headquarters()  # None when the evidence names two
view.countries()  # ISO alpha-2 codes with a current presence
```

Every geography view carries `geo_relationship`, `presence_kind`, `country_code`,
`country_name` and `city_name`, plus `is_physical_presence` and
`is_current_presence`. Every view carries `cleaned_value`, which is set when
deterministic hygiene removed a promotional modifier.

Every `ClassificationView` carries `source` (`model` / `operator_confirmed` /
`operator_corrected` / `operator_unresolved`), `normalization` (`canonical` /
`alias` / `unmapped` / `not_applicable`), `evidence_status`, `confidence_band`,
and the evidence itself.

`settled` is deliberately strict: resolved **and** either evidence-backed or
human-owned. A model-produced value with no evidence is never settled, however
confident the model said it was.

---

## Backfill

`app/services/company_intelligence/backfill.py`, driven from
`/admin/company-intelligence/backfill`. See
`docs/COMPANY_INTELLIGENCE_BACKFILL.md` for the operating guide.

Bounded batches, deterministic `(created_at, id)` ordering behind a cursor, one
item row per `(run, company)` so a resume never double-processes, a dry run that
walks the identical eligibility path, a hard `max_companies` ceiling, and a
truthful reason code on every skip. It enqueues; it never produces inline.

---

## Limitations, stated plainly

* **The taxonomy is not complete.** The industry vocabulary is the
  operator-supplied list used verbatim; the business-model, company-type,
  customer-segment and operating-market lists are short first-release
  vocabularies. Products, services, specialties and capabilities have **no**
  controlled vocabulary and record the producer's own wording. See
  `docs/COMPANY_INTELLIGENCE_TAXONOMY.md`.
* **The geography edition is not the world.** 60 countries and 259 cities,
  selected for commercial relevance to this product's B2B work. A place outside
  it is not extracted at all, so a company in a country this edition omits will
  show no geography rather than a wrong one. Adding coverage is a new edition.
* **Extraction is conservative about ambiguous names.** An ambiguous surface
  needs a capital letter *and* a preposition directly in front of it, or its
  country named nearby. "Our Reading site" is therefore missed. Missing a real
  place is recoverable; asserting a false one is not.
* **Specialty remains an open vocabulary.** There is no closed global list, so
  two companies can describe the same competence differently and nothing folds
  them together. Aliases are the extension point and are curated by hand.
* **Classifications are not verified** unless an operator has confirmed them
  under the review semantics above. Model confidence is an opinion.
* **Freshness is not automatic.** Re-running Research does not re-classify a
  Company until something enqueues a job. This follows from the placement
  decision (ADR-CI-001) and is the main thing to revisit.
* **Subindustries are not listed in the prompt.** All 245 would crowd out the
  evidence, so the producer writes its own wording and normalization either maps
  it or flags it.
* **The read model is correctness-first.** `get_many` loops rather than issuing
  one wide join. That is the right first version and the wrong hundredth.
* **No customer-app exposure.** The whole area is operator-only in this release.
* **One producer, one prompt.** There is no ensemble, no second opinion, and no
  cross-checking of one run against another.

---

## Future integration points

**Personalization** (its own branch, untouched here). It should read
`get_company_intelligence` and use `settled_values` — never the tables. The
useful constraint to carry over: a drafting stage should be allowed to mention a
classification only when it is `settled`, and should cite the same evidence the
classification cites, so a claim in an email traces to the page it came from.

**Saved Audiences and Campaign targeting.** `settled_values(dimension)` and
`primary_industry()` are the intended filters. Both refuse to guess:
`primary_industry()` returns `None` when the industry is conflicted rather than
picking the higher-scoring side, so an audience built on it cannot silently
include companies whose evidence disagreed with itself.

**Scoring.** `confidence_band` and `evidence_status` are the honest inputs; the
raw float is available but banding exists because 0.62 and 0.58 are not different
judgements.

**Reporting.** `unresolved()` and `stale_decision_count` are the two numbers that
say how much of the classified estate a human has actually looked at.

**Agent Studio.** No integration in this release, by instruction. If one is
wanted later, the seam is the read model, not the tables.

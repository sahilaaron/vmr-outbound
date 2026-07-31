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
  "unknown_dimensions": ["business_model"]
}
```

**Validation policy** (`producer.POLICY_VERSION`, currently `1`), in order:

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

**Idempotency.** `input_digest` is SHA-256 over the dossier version, the exact
sourced facts (id + content hash), the taxonomy editions, and the producer and
policy versions. `UNIQUE (company_id, input_digest)` means the same question
cannot produce a second version even under a race between two workers. The runner
checks for an existing version **before** calling the model, so a repeat run
costs nothing.

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
```

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
  vocabularies. Products, services, specialties, capabilities and specific
  geographies have **no** controlled vocabulary and record the producer's own
  wording. See `docs/COMPANY_INTELLIGENCE_TAXONOMY.md`.
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

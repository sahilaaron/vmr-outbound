# Company Intelligence: evidence lineage (CI-001)

The test this design has to pass: **pick any classification, six months later, and
answer every one of these questions from the database alone.**

| Question | Answered by |
| --- | --- |
| Which Research dossier was used? | `company_intelligence_versions.dossier_version_id` (+ `dossier_version_number`) |
| Which sourced facts were available? | `company_intelligence_versions.sourced_fact_ids` |
| Which sourced facts supported *this value*? | `company_intelligence_evidence_links.insight_id` |
| Which evidence references supported those facts? | `company_intelligence_evidence_links.insight_evidence_id`, `source_url`, `excerpt` |
| Which producer created it? | `producer`, `producer_version` |
| Which validation rules applied? | `policy_version` |
| Which taxonomy normalized it? | `classifications.taxonomy_id`, `taxonomy_version`, `term_id`, `term_code` |
| Was it operator-confirmed? | `company_intelligence_decisions` for `(company_id, dimension, target_key)` |
| Was it superseded? | `versions.is_current`, `versions.superseded_at` |
| What conflicting evidence existed? | `company_intelligence_conflicts` + the `conflict_group` on each member |
| Exactly which question was asked? | `versions.input_digest` |
| Which wording in the evidence produced this place? | `classifications.model_value` (the matched surface) |
| What is the company's relationship to it? | `classifications.geo_relationship` |
| Is that a site or a sales territory? | `classifications.presence_kind` |
| Was a promotional modifier removed, and from what? | `classifications.normalized_value` vs `model_value` |
| Was the answer the same as last time? | `versions.answer_digest` |

## The chain

```
InsightEvidence  ──┐
                   ├─→ Insight ──┐
CompanyResearch    │             │
  Submission ─→ CompanyDossier   │
                  Version ───────┤
                                 ▼
                  CompanyIntelligenceVersion
                    (dossier_version_id, sourced_fact_ids,
                     taxonomy_versions, producer, policy, input_digest)
                                 │
                                 ├─→ CompanyIntelligenceClassification
                                 │     (model_value, term_id, state,
                                 │      evidence_status, confidence, rank)
                                 │        │
                                 │        └─→ CompanyIntelligenceEvidenceLink
                                 │              (insight_id, insight_evidence_id,
                                 │               source_url, excerpt, support)
                                 │
                                 └─→ CompanyIntelligenceConflict
                                       (dimension, conflict_group, statement)

CompanyIntelligenceDecision ─────→ (company_id, dimension, target_key)
    lineage: intelligence_version_id, classification_id
```

## Design choices that keep the chain intact

**Only the final label surviving is the failure mode this avoids.** A
classification stores *both* the producer's exact wording (`model_value`) and
what it was normalized to (`term_id` / `term_code` / `term_label`). An operator
can see that "Kiln automation" was mapped to "Manufacturing" — which is how a
mapping that is technically valid and wrong gets noticed. Overwriting the wording
with the canonical label would have made those two cases indistinguishable.

**Evidence links point, they do not copy.** A link carries an `insight_id` and an
`insight_evidence_id`, not a duplicated claim. The excerpt is stored for display
only. Copying would create a second version of the truth that drifts.

**A link cannot point nowhere.** `CHECK (insight_id IS NOT NULL OR source_url IS
NOT NULL OR dossier_section IS NOT NULL)` — an "evidence" row naming none of those
is decoration, and decoration in an evidence table is worse than no row.

**Insight deletion is `SET NULL`, not `CASCADE`.** If a claim is ever removed, the
classification must still be able to say it once rested on something, rather than
silently appearing unsupported. The evidence *count* on the classification is
frozen at production time for the same reason.

**Ownership is a database constraint, not a service check.** Composite foreign
keys enforce that:

* a `CompanyIntelligenceVersion` reads a dossier belonging to the **same
  company** (`fk_company_intelligence_versions_dossier_owner` on
  `(dossier_version_id, company_id)`);
* a classification belongs to a version of the same company;
* an evidence link belongs to a classification of the same version.

A service check protects only the path that calls it. A migration, a fixture, a
future import or a direct write can all reach these tables without passing
through the service — and a cross-company classification is a claim attributed to
the wrong organisation, which is the kind of wrong that reads as fact.

**A dossier cannot be deleted while an interpretation of it survives.** The
composite key uses `ON DELETE NO ACTION`, which refuses to orphan a version but
defers to end-of-statement so `DELETE FROM companies` still cascades through both
tables cleanly.

**`sourced_fact_ids` records what was *available*, not what was used.** The links
record what was used. Keeping both is what makes "the producer never saw that
fact" distinguishable from "the producer saw it and did not use it" — two very
different explanations for a missing classification.

**The prompt and the raw answer are not stored.** `answer_digest` is a SHA-256, so
two runs can be compared without putting prompt framing, tool configuration or
seller context into the database. Lineage does not require a transcript; it
requires knowing which inputs produced which outputs, and the digests do that.

## Evidence and citation rules at production time

1. The producer may cite only handles it was shown (`F1`, `F2`…), or a URL that
   appears in the assembled input's `source_urls`.
2. An unresolvable citation is dropped and counted in the version's `warnings`.
   It is never softened into "probably meant this one".
3. A classification whose citations all dropped is stored `unresolved` with
   `evidence_status = insufficient` and `unresolved_reason = no_evidence`.
   **Stored, not discarded** — a dropped suggestion is invisible to review, and
   invisible work gets redone.
4. Only `SUPPORTED` insights are offered as evidence. An `UNKNOWN` insight is a
   gap the Insights Agent named and a `CONFLICTING` one is already disputed;
   neither is material a classifier should treat as fact.

## Conflicts

A conflict is a first-class row, not an absence of one. Members share a
`conflict_group` within `(version, dimension)`; the conflict row carries the
statement and the member count, with `CHECK (member_count >= 2)`.

Nothing resolves a conflict automatically. The producer is instructed not to
choose; the validator drops any "conflict" with fewer than two surviving members
(a conflict with one side is an assertion wearing the word); and
`read.primary_industry()` returns `None` rather than the higher-confidence side.
An operator resolves it with a correction, or leaves it — leaving it is a truthful
answer.

## Operator decisions in the chain

A decision carries both:

* **lineage** — `intelligence_version_id` and `classification_id`: what the person
  was looking at;
* **authority** — `company_id`, `dimension`, `target_key`, `target_label`: what
  they decided, independent of any version.

`target_key` is the term code for a canonical value and `text:<normalized>` for a
free-text one, so the two namespaces cannot collide. `target_label` stores what
the person actually saw, denormalized deliberately: a later vocabulary edition
rewording a term must not change the record of what somebody decided.

Superseding preserves the chain — `is_current = false`, `superseded_at`,
`superseded_by_id` — so a sequence of changes of mind reads in order, with
authors.

`intelligence_version_id` is `ON DELETE SET NULL`: a judgement outlives the
version that prompted it. Deleting the Company cascades both, which is correct —
there is nothing left to be accountable for.

## Audit trail

Every material action writes an `AuditEvent` alongside the domain row:

| Action | When |
| --- | --- |
| `company_intelligence.version_produced` | A version is stored |
| `company_intelligence.decision_recorded` | An operator confirms/corrects/rejects/unresolves |
| `company_intelligence.alias_mapped` | An operator teaches the vocabulary |
| `company_intelligence.taxonomy_activated` | A vocabulary edition becomes active |
| `company_intelligence.job_enqueued` / `_succeeded` / `_failed` / `_cancelled` | Queue transitions |
| `company_intelligence.backfill_started` / `_paused` / `_resumed` / `_cancelled` / `_completed` | Backfill lifecycle |

The audit trail is a second, independent record. The domain tables above are the
primary one — a lineage that only existed in audit events would be one truncation
away from gone.

---

# Geography lineage (CI-002)

Geography has a second lineage question the other dimensions do not: **which
words in which fact made this a place at all?**

## The chain, for one place

```
Insight.claim / InsightEvidence.excerpt
        │  exact phrase match against the active geography edition
        ▼
GeographyCandidate  (handle G1, canonical place, matched surface,
                     evidence handles, context flags)
        │  the model assigns a relationship, choosing only from these handles
        ▼
CompanyIntelligenceClassification
   dimension        = GEOGRAPHY
   model_value      = the wording that matched, verbatim
   term_id/term_code= the canonical place in the active edition
   parent_term_code = the ISO alpha-2 of its country, for a city
   geo_relationship = what the model said
   presence_kind    = derived deterministically from the relationship
   unresolved_reason= why it is not settled, when it is not
        │
        └─→ CompanyIntelligenceEvidenceLink  → the insights that named the place
```

## What is deliberately *not* scanned

**Source URLs and source titles.** A publisher's address lives there, and a paper
published in Heidelberg says nothing about the company. Only claim text and
evidence excerpts are read, which removes that whole class of false positive
structurally rather than by pattern-matching against it afterwards.

## Refusals are part of the record

Three tiers, and every refusal is written into the version's `warnings` so it is
reviewable rather than silent:

| Tier | Example | Outcome |
| --- | --- | --- |
| Ambiguous surface, unqualified | "reading the operating manual" | never a candidate, warning recorded |
| Hard-suppressing context | "published by a publisher in Munich", "presented at a conference in Berlin", "graduated from a university in Amsterdam", "governed by the laws of Ireland" | never a candidate, warning recorded |
| Soft context | "our customer Nordwerk is based in Munich", "acquired a Paris-based company", "plans a site in Bilbao" | candidate, carrying its flag |

A soft flag is then checked against the model's answer. A place found only in a
customer example that the model calls a *headquarters* does not become one: the
classification is stored `unresolved` with
`context_conflicts_with_relationship`, because a candidate and an answer
disagreeing is a disagreement, not a reason to prefer one side.

A second, unflagged sighting clears the doubt the first raised — "acquired a
Paris company" plus "our Paris office" is an office — so flags intersect across
sightings rather than accumulating.

## Country inference

A **settled city implies its country**, and the derived row says so in its
rationale ("derived from Pune, which the evidence places in India") and carries
`inferred_from`. A plant in Pune is a plant in India, and a reader filtering by
country should find it.

The reverse is **forbidden and not implemented**. "Operations in India" names no
city, and inventing one would be fabrication with a tidy shape. An *unclear* city
does not propagate either: an unclear country is noise.

## Physical versus commercial presence

| Relationship | Presence | Current? |
| --- | --- | --- |
| headquarters, office, branch, facility, manufacturing, research_and_development, warehouse, distribution, operations | `physical` | yes |
| commercial_market | `commercial` | yes |
| planned_presence | `prospective` | **no** |
| historical_presence | `former` | **no** |
| unclear | `unknown` | **no** |

`presence_kind` is derived, not asserted by the model, and it is stored rather
than computed at read time — so a consumer asking "where is this company
physically" never re-implements the mapping, and the difference between a factory
and a sales territory survives in the row itself.

## Specialty lineage

A cleaned specialty keeps both forms: `model_value` is what the producer wrote,
`normalized_value` is what deterministic hygiene left. Both are shown side by
side on the review screen, because the only way to notice a clean that went too
far is to see what it started from.

`unresolved_reason` carries the hygiene verdict — `promotional_language`,
`too_broad`, `dimension_boundary_unclear`, `specialty_too_long` — and rejected
values appear in the version's `warnings` with the reason and the original
wording. Nothing a producer proposed disappears without a trace.

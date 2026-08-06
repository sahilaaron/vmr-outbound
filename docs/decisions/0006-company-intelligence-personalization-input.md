# 0006 — Company Intelligence as a Personalization input, not an Insights source

Date: 2026-08-06. Status: accepted.

## Context

Company Intelligence is produced automatically after Research (0005 + the
Research handoff) but nothing downstream consumed it. Personalization drafts
from Insights, seller context, campaign context and the Policy Studio rules.
The question was where the structured intelligence layer belongs: in Insights
input, in Personalization input, or both.

## Decision

**Directly into Personalization context selection, and nowhere else.**

1. **Not into Insights.** The Insights Agent derives `Insight` rows
   (`state=SUPPORTED`) from the Research dossier with a research-job-pinned
   lineage. Feeding classifications in there would launder model-produced
   labels into the very evidence store Personalization treats as proof — the
   exact "silently downgrade classifications into facts" failure this
   integration must prevent. Insights stays dossier-only.
2. **One integration point: `personalization.generation.decide_context`.**
   A new module (`personalization/intelligence.py`) projects the *current*
   Company Intelligence version — via the existing effective read model, which
   already removes review-rejected values and applies operator decisions —
   into a typed, bounded `IntelligenceInputSnapshot`. `decide_context` attaches
   it to the `ContextDecision`, so the preview path and the pipeline adapter
   get identical behaviour through the one seam they already share.
3. **Structured context, never a candidate.** Accepted values render as a
   read-only prompt block explicitly marked *not proof*; they are not
   `ContextCandidate`s, contribute no citable evidence ids (the citation
   allow-list is unchanged, so an output citing one is refused), never change
   the fallback ladder, and are withheld entirely at fallback level 5 (the
   weak-evidence fallback keeps meaning what it meant) and when the policy
   temperament sets company-context usage to minimum (the Policy Studio stays
   authoritative).
4. **Eligibility is strict and recorded.** Accepted: resolved + supported +
   evidence-backed + normalized (never unmapped) + unconflicted + provenance
   present + no evidence link contradicting the Research evidence (Research
   stays authoritative). Everything else is carried as *excluded with its
   reason*. Review-rejected values are absent from the effective read model by
   construction. Operator assertions without stored evidence are excluded as
   such.
5. **Lineage rides the existing generation record.** The snapshot summary
   (status, exact version id/number, producer version, input digest, accepted
   and excluded values with reasons) is a new key inside the existing
   `personalization_decision` JSONB on `DraftVersion` and in the stage
   `output_reference` — no migration. Historical outputs lack the key and are
   reported as *lineage unavailable*, never relabelled.
6. **Company-scoped sharing.** The snapshot reads the one current version per
   Company; every Contact at that Company records the same version id.

## Consequences

* Copy generation gains normalized orientation (industry, geography with
  relationship/presence, segments) with full provenance, at zero model-spend
  and zero schema cost.
* Two withheld states exist (`withheld_weak_evidence_fallback`,
  `withheld_company_context_minimum`) so "available" is never conflated with
  "used" — the Workbench renders the distinction on every output.
* Insights and Company Intelligence remain independent readers of the same
  evidence; neither treats the other as source truth.

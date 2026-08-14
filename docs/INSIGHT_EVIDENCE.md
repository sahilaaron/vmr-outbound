# Insight and Evidence Contract

## Purpose

Insights are derived claims over eligible sourced knowledge. They are not canonical Company facts and they never replace Research provenance.

## Research authority

Research is reusable Company knowledge and may run repeatedly over time.

Research persists sourced facts, source metadata and versioned dossier state. Historical evidence remains readable after later Research runs.

## Insights input selection

When an Insights execution starts, deterministic application code selects the **current eligible Research/Company knowledge available at that moment**.

Selection must continue to respect existing rules for:

- authority;
- source provenance;
- freshness;
- confidence/eligibility;
- subject relevance;
- conflicts;
- citation suitability.

"Current" never means "blindly newest row".

## Lineage

The Insights result records the evidence/fact/dossier versions it actually used.

That lineage is provenance:

> what did this run use?

It is not a requirement that the current Insights Agent Job identify the one exact historical Research Job that created those records.

A later Research update does not invalidate an older Insight. A later Insights rerun may consume the newer eligible Research state.

## Evidence validation

Claude/model output may propose derived claims, but deterministic code owns:

- evidence-handle validation;
- source eligibility;
- freshness checks;
- numeric parsing/taxonomy where applicable;
- duplicate/conflict decisions;
- persistence.

Unsupported claims are dropped rather than promoted by model confidence alone.

## Personalization consumption

Personalization may consume current eligible Insights together with current eligible Research/Company and Campaign/seller context.

Each generated message records what evidence it used. A stale historical predecessor relationship must not block generation when valid current eligible context exists.

## Customer presentation

Insight generation is an internal preparation step toward Ready for Sending.

A failed or blocked Insights job is not automatically a customer task. Customer-facing state should remain Processing or Could not prepare as appropriate; detailed evidence/recovery belongs in Admin diagnostics.

# Admin Agent Studio

## Purpose

Agent Studio lives under `/admin/agents/studio` and is an **Admin diagnostic/configuration surface**.

It is not the customer operating model.

The customer contract is defined in [`CUSTOMER_OPERATING_MODEL.md`](CUSTOMER_OPERATING_MODEL.md): normal customers create/configure Campaigns, capture/add Contacts, wait while VMR processes them, and take over at Ready for Sending.

## Admin responsibilities

Agent Studio may expose:

- Agent registry/capabilities;
- global controls and Campaign overrides;
- live-work/spend configuration;
- durable jobs and attempts;
- failure/block/retry state;
- provider/model diagnostics;
- execution reports and provenance;
- side-effect-free previews where implemented.

These are operational controls and diagnostics, not customer tasks.

## Customer boundary

Do not surface Agent Studio as a normal customer requirement.

A customer may see high-level Agent progress for transparency, but should not need to understand jobs, leases, error classes, retries, exact execution lineage or provider internals to use VMR.

## Research Studio

Research reporting preserves historical executions, sourced facts and dossier versions for inspection.

Research itself is reusable Company knowledge and may run repeatedly over time.

Historical Research execution identity is valuable for audit, but downstream Insights must select current eligible Company/Research knowledge at execution time. The Studio must not teach a product rule that one exact historical predecessor job is required for every downstream run.

## Insights Studio

Insights reporting should make clear:

- what eligible Research/Company knowledge was selected when the run began;
- what evidence/versions were actually used;
- what claims were accepted/dropped;
- what structured outputs were produced.

Provenance explains the result; it does not grant permission to run.

## Personalization Studio

Personalization Policy controls how an already-authorized generation is written.

Execution authority remains separate from writing policy, suppression, Verification and Campaign live-work/spend consent.

Personalization consumes current eligible Research + Insights + Campaign/seller context at execution start and records what it used.

A valid generated seven-message sequence does not require a human approval click to become Ready for Sending.

Human review/edit records remain available as true audit history when a person actually acts.

## Verification Studio

Verification owns exact-address truth and provider/waterfall configuration.

A provider test or operational action may spend credits and therefore remains an Admin/configuration concern where appropriate. A pattern/model result never substitutes for exact-address verification evidence.

## Capture and Company Studio

Capture/Company modules remain diagnostic views over permanent Contact/Company and domain-resolution authority. They must preserve historical versus current truth and must not invent missing execution facts.

## Safety

Agent Studio must not:

- fabricate customer work;
- imply that internal failed/blocked jobs are a normal customer approval queue;
- bypass suppression/eligibility;
- turn model output into verification truth;
- send automatically;
- expose secrets or raw credential material.

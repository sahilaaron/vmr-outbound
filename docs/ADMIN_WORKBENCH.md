# Admin Workbench

## Role

`/admin` is the operational control room for VMR Outbound.

It is deliberately different from the normal customer application. The customer contract is autonomous preparation until Ready for Sending; Admin exists to explain and recover internal execution when needed.

## Admin capabilities

Admin may expose:

- Campaign execution controls;
- Agent registry/control state;
- Campaign Agent overrides and live-work/spend consent;
- durable Agent jobs;
- attempts, leases and queue state;
- blocked/failed/retrying work;
- provider/model usage and failures;
- identity/domain/resolution diagnostics;
- evidence/provenance reports;
- rerun/recovery controls;
- system/runtime diagnostics.

## Customer boundary

Do not project Admin failure/recovery state into the normal customer application as a generic task count.

A failed Research/Verification/Insights/Personalization job is an Admin/system concern unless the customer genuinely must supply a missing Campaign input.

The customer should normally see only Processing, Ready for Sending or Could not prepare, with optional details.

## Recovery

Rerun/retry actions are operational recovery tools.

They should remain available to Admin where safe and useful. They are not the default normal-user workflow.

## Provenance

Admin reports may preserve exact historical execution identity, job lineage, source versions and evidence used.

Historical identity is for audit and diagnosis. It does not imply that downstream Agents must require one exact historical predecessor execution in order to run against valid current eligible knowledge.

## Sending

Admin cannot infer or grant automatic send authority merely from sequence generation, readiness or review state. Automatic sending remains unavailable unless a separately authorized sending implementation is built.

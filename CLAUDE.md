# VMR Outbound Agent — working rules

## Read first

1. `docs/GOAL.md`
2. `docs/CUSTOMER_OPERATING_MODEL.md`
3. `docs/AGENTS.md`
4. `docs/CLAUDE.md`
5. `docs/PROPORTIONAL_VALIDATION.md`
6. `docs/PARALLEL_INTEGRATION.md` when work is concurrent or stacked

The governing customer rule is:

> **VMR Outbound is autonomous until Ready for Sending.**

A normal customer creates/configures Campaigns, captures/adds Contacts, waits while VMR prepares them, and takes over only when Contacts are Ready for Sending. Internal Agent failures, retries and provider/model state are Admin/system concerns unless a specific customer-owned input is genuinely required.

## Non-negotiables

- Contact-first: permanent Contacts and Companies are not owned by Campaigns.
- Research is reusable Company knowledge and may run repeatedly over time.
- Insights/Personalization use current eligible knowledge at execution time; lineage records what was used rather than acting as a historical predecessor gate.
- Suppression and legal/eligibility exclusions always win.
- Verification truth comes from the verification boundary, never a model assertion.
- Valid seven-message generation does not require a human approval click to become Ready for Sending.
- Absence of a review row is not a backlog.
- Editing creates a new immutable version and preserves history.
- No automatic send is implied by generation, readiness, review or editing.
- Secrets never enter source, prompts, logs, fixtures, screenshots or Git history.

## Seven-message sequence

Default elapsed days:

`0, 3, 7, 12, 18, 25, 35`.

The customer may inspect/edit it after generation. Inspection/editing are optional.

## Delivery behavior

Optimize for the shortest safe path to real UAT.

For a narrow understood defect:

```text
reproduce
→ smallest correct fix
→ focused regression proof
→ touched-file static checks
→ push
→ GitHub CI
→ deploy
→ real UAT
```

Do not duplicate broad CI locally or repeat whole-feature reviews for narrow successor repairs without a concrete widened risk.

## Division of labour

- Claude/Cowork builds code, tests, docs and branch commits.
- Sahil makes product, cost, risk and launch decisions and bridges local pushes only when needed.
- ChatGPT operates the remote GitHub workflow, independently verifies material changes, and merges only after Sahil's explicit approval.

Do not attribute commits, PRs, issues, source or documentation to Claude/ChatGPT/AI unless Sahil explicitly asks for that wording.

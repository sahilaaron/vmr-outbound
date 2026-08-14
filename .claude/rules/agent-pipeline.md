---
description: Research / Insights / personalization Agent-pipeline invariants
paths:
  - "app/services/agents/**"
  - "app/services/agent_studio/**"
  - "app/services/workbench_agents/**"
  - "app/services/research/**"
  - "app/services/insights/**"
  - "app/services/personalization/**"
  - "app/services/thinking/**"
  - "app/services/company_intelligence/**"
---

# Agent pipeline invariants

Authority: `docs/ARCHITECTURE.md`, `docs/AGENT_WORKBENCH.md`, `docs/RESEARCH_WORKERS.md`,
`docs/INSIGHT_EVIDENCE.md`. This file is the short form; those docs decide detail.

- **Research is Company knowledge, not a campaign-execution artefact.** It may run
  today, tomorrow, every day, or outside any campaign, and each run may enrich what
  is already known. Never bind a downstream Agent to one exact historical Research
  run, and never require a successor to reproduce it.
- **Insights reads the Company's current eligible Research state at execution time.**
  Do not reintroduce a requirement for the exact committed submission/dossier of a
  named prior execution — that is the defect fixed in `709aae1e`.
- **Lineage records what a run used, not whether it may run.** Deterministic-vs-fallback
  lineage is evidence for the operator; it is never an eligibility gate. Executions
  predating lineage recording report `lineage unavailable` — that is not a failure.
- **AI output is advisory until validated.** Deterministic rules live in the backend
  service; a model result never becomes a committed outcome without the service's own
  validation.
- Features default off; dry-run defaults on.
- Never fabricate evidence, verification outcomes, scores, or completion. Catch-all and
  unknown mailboxes stay uncertain — a domain pattern never verifies a mailbox.
- No paid model APIs or new paid services without Sahil's explicit approval.

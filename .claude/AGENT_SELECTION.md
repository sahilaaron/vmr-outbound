# VMR Outbound subagent selection

This repository carries a curated subset of a larger third-party subagent library.
Only the agents listed here load when Claude Code starts inside `vmr-outbound`.

## Why the subset exists

Claude Code loads `~/.claude/agents/` in **every** project and `.claude/agents/` only
in **this** one. The full library had been installed at the global path, so all 255
agents were described into every session in every project and Claude Code reported:

> Agent descriptions are over the 15.0k-token limit (~32.3k tokens)

The library itself was never the problem — its location was. The full library now
lives outside the global scan path and this repository projects only the relevant
agents.

## Where the full library lives

| | |
|---|---|
| Library root | `~/.claude-agent-library/agency-agents` (255 agents, 18 categories) |
| Timestamped snapshots | `~/.claude-agent-library/backups/` |
| Previously at | `~/.claude/agents/agency-agents` — globally active, now removed from that path |
| Upstream repository | **not recorded locally** — see "Provenance" below |

Nothing was deleted. The library is intact and can be used by any other project by
copying agents into that project's own `.claude/agents/`, or temporarily by placing
it back under `~/.claude/agents/`.

## Provenance and licensing

The library was installed as bare `.md` files with **no** `LICENSE`, `README`,
manifest, or git checkout, so the upstream repository and its license terms cannot be
determined from the installed files. If the upstream is identified later, record it
here and add any attribution or license notice the upstream requires.

## How a project copy differs from the library file

A project copy is the upstream file with **exactly one** change: the frontmatter
`description:` line is replaced by a short routing description (what it handles, when
to invoke it). Bodies — the actual system prompts — are byte-identical to the library
source, so a refresh is a clean re-copy rather than a merge.

Combined description footprint: **42 agents, 4,617 characters (~1.2k tokens)**, well
under Claude Code's 15k ceiling.

## Refreshing after the library is updated

```bash
python .claude/agents-refresh.py --check     # report drift, change nothing
python .claude/agents-refresh.py --apply     # rewrite .claude/agents/*.md
```

`.claude/agent-selection.json` is the source of truth: it records each agent's name,
category, source path within the library, short description, and why it was selected.
To add or drop an agent, edit that manifest and re-run with `--apply`.

If the library lives somewhere else, pass `--library /path/to/agency-agents`.

## What was selected, and why

Selection is grounded in the actual stack — FastAPI, SQLAlchemy 2.0, Alembic,
PostgreSQL, Jinja server-rendered operator UI, a Chrome capture extension, an
nginx/systemd VPS deployment, and the Research / Insights / personalization agent
services.

| Area | Agents |
|---|---|
| Backend & data (7) | backend-architect, software-architect, api-platform-engineer, database-optimizer, database-reliability-engineer, data-engineer, email-intelligence-engineer |
| Quality & review (11) | code-reviewer, minimal-change-engineer, codebase-onboarding-engineer, codebase-archaeologist, api-tester, test-automation-engineer, test-results-analyzer, evidence-collector, reality-checker, performance-benchmarker, accessibility-auditor |
| Security & privacy (7) | security-architect, appsec-engineer, senior-secops, secrets-credential-engineer, ai-generated-code-auditor, identity-access-engineer, privacy-engineer |
| Ops (4) | git-workflow-master, devops-automator, sre, incident-response-commander |
| Frontend (3) | frontend-developer, ux-architect, ui-designer |
| AI & product (7) | ai-engineer, prompt-engineer, multi-agent-systems-architect, rag-pipeline-engineer, technical-writer, workflow-architect, product-manager |
| Outbound domain (3) | outbound-strategist, email-strategist, legal-compliance-checker |

Deliberately excluded: GIS and cartography, game development, spatial computing and
visionOS, China platform marketing, unrelated compliance regimes (FedRAMP, Section
508 as a program), and framework specialists for stacks this repository does not use.
They remain available in the library.

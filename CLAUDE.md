# VMR Outbound Agent — working rules

Private, agent-assisted outbound sales system. Immediate objective: one safe,
human-approved 100-contact pilot campaign — not a platform.

## Read order (before changing the repository)

1. `docs/GOAL.md` — authorized milestone, acceptance criteria, non-goals.
2. `docs/AGENTS.md` — permanent engineering and safety rules.
3. `docs/CLAUDE.md` — AI judgment boundaries, MCP limits, research/draft contracts.
4. `docs/PROJECT_TRACKING.md` — management tracker and handoff rules.
5. `docs/SELLER_KNOWLEDGE_BASE.md` — the seller-side knowledge base (KB-001),
   when the work touches offerings, proof points, restricted claims, personas,
   campaign-to-offering associations, or context readiness.
6. `docs/PARALLEL_INTEGRATION.md` — integration authority, frozen base SHAs,
   ownership blocks, the gate sequence, and stacked-chain merge order, whenever
   another thread is building concurrently or a branch is stacked.
7. `docs/PROPORTIONAL_VALIDATION.md` — **UAT-first delivery authority** for
   builds, fixes, reviews, testing, CI, deployment, and successor repairs. Read
   it before choosing validation depth or adding another process step.

When instructions conflict: Sahil's latest explicit instruction > GOAL >
AGENTS > PROPORTIONAL_VALIDATION > CLAUDE > PROJECT_TRACKING >
PARALLEL_INTEGRATION > existing conventions.

## Operating model

- **Claude builds and maintains the product**: code, tests, migrations, and
  intentional commits on a branch, followed by a factual build handoff and
  proposed tracker payload. When a session cannot push, deliver the branch as a
  git bundle to the local repository.
- **Sahil decides and bridges**: scope, cost, risk, product, and launch choices.
  When Claude cannot authenticate to GitHub, Sahil pushes the prepared local
  branch through CMD or GitHub Desktop. He is not the routine PR, issue, review,
  merge, or tracker operator.
- **ChatGPT operates GitHub and independently reviews**: once the branch is on
  GitHub, ChatGPT opens or updates the PR, writes GitHub content, checks the
  actual diff and CI, records a PASS / PASS WITH CONDITIONS / FAIL / BLOCKED
  verdict, handles corrections and issue state, and merges only after Sahil's
  explicit approval. ChatGPT also owns the official Google Sheets tracker
  update and is accountable for keeping the path to UAT proportionate.

Claude never grades its own work, updates the Sheet, merges, closes issues, or
represents an unpushed local commit as present on GitHub.

## Non-negotiables (full list in docs/AGENTS.md)

- No send or schedule without human approval of the exact draft version; edits
  invalidate approval.
- Never contact suppressed, opted-out, hard-bounced, or invalid addresses.
- Never fabricate evidence, verification outcomes, scores, or completion.
- Catch-all/unknown stay uncertain; a domain pattern never verifies a mailbox.
- No unattended scraping, CAPTCHA/anti-bot evasion, or platform-terms bypass.
- Contacts are permanent and never require a campaign; acquiring one never makes
  it outreach-eligible. Only an exact normalized LinkedIn profile URL may
  auto-match an existing contact.
- No paid model APIs or new paid services without explicit approval.
- Secrets never in source, prompts, logs, fixtures, or Git history.
- No Claude/AI/tool attribution anywhere in commits, PRs, issues, or code.

## Engineering defaults

- **Every active build or fix should shorten the path to UAT.** UAT is the
  destination, not a final ceremony after optional cleanup.
- Smallest complete vertical slice authorized by `docs/GOAL.md`; no
  opportunistic scope, refactors, abstractions, or adjacent hardening.
- **Validation is proportional to semantic delta and real blast radius.** A
  narrow defect normally follows reproduce → smallest fix → failing test/file →
  touched-file static checks → push → CI → deploy → UAT. Do not duplicate broad
  CI locally, repeat a parent feature's full review, launch review-of-review
  loops, or wait on optional suites without a concrete escalation trigger.
- After one broad review of a substantial boundary, successor fixes get
  **delta-only review** unless new evidence proves the blast radius widened.
- When CI fails, inspect the failed job and exact failing tests first. Repair
  only candidate-caused failures; rerun proven infrastructure flakes instead of
  changing product code.
- Deterministic rules live in backend services; AI output is advisory until
  validated. Features default off; dry-run defaults on.
- Schema changes only via reversible Alembic migrations proven locally.
- The complete gate sequence in `docs/DEVELOPMENT.md` §6 and
  `docs/PARALLEL_INTEGRATION.md` applies to substantial feature/integration
  publication. It does **not** force a narrow successor fix to recreate CI
  locally; `docs/PROPORTIONAL_VALIDATION.md` decides the minimum local gate.
- Many threads may build; one thread integrates; one exact tree is validated.
  Work only inside the declared ownership block, against the exact frozen base
  SHA — never "the latest branch".
- Every handoff survives as a pushed branch or verified bundle when publication
  cannot happen directly. Do not create extra handoff ceremony when the branch
  can simply be pushed and CI can run.
- Out-of-scope ideas and non-blocking review findings go to the deferred backlog,
  not into the active UAT path.

## Claude Code configuration

- Subsystem rules live in `.claude/rules/*.md` and load only when matching files are
  touched. Put path-specific instructions there, not in this file.
- `.claude/agents/` carries a curated subset of a larger agent library that is kept
  outside `~/.claude/agents/` on purpose. See `.claude/AGENT_SELECTION.md`; never
  reinstall the full library at the global path.

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
   another thread is building concurrently or a branch is stacked. It is the
   single definition of the gate sequence; the documents above defer to it
   there.

When instructions conflict: Sahil's latest explicit instruction > GOAL >
AGENTS > CLAUDE > PROJECT_TRACKING > PARALLEL_INTEGRATION > existing
conventions.

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
  update.

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

- Smallest complete vertical slice authorized by `docs/GOAL.md`; no
  opportunistic scope, refactors, or abstractions.
- Deterministic rules live in backend services; AI output is advisory until
  validated. Features default off; dry-run defaults on.
- Schema changes only via reversible Alembic migrations proven locally.
- Checks before handoff: the gate sequence — commands in `docs/DEVELOPMENT.md`
  §6, rule in `docs/PARALLEL_INTEGRATION.md`. On a stacked or parallel-built
  branch, run them on the final assembled head. If they cannot be run, say
  `Integration incomplete; do not publish yet`.
- Many threads build; one thread integrates; one exact tree is validated. Work
  only inside the declared ownership block, against the exact frozen base SHA —
  never "the latest branch". Fixing one failing CI gate is not a correction.
- Every handoff survives as a pushed branch or a verified bundle, with base and
  head SHAs, bundle SHA-256, `git bundle verify` and `git merge-base` proof.
- Out-of-scope ideas go to `docs/POST_LAUNCH_BACKLOG.md`, not into code.

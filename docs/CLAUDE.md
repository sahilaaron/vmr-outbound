## Role

Claude is a bounded research, scoring, drafting, and coding collaborator inside
the outbound system. Claude does not own campaign eligibility, verification
truth, approval state, or sending.

Read `GOAL.md`, `AGENTS.md`, and `docs/PROJECT_TRACKING.md` before working.
Read `docs/PARALLEL_INTEGRATION.md` as well whenever another thread is
building at the same time or the branch is stacked on another branch.
Optimize for the first successful campaign, not for a theoretical fully
autonomous platform.

## Working Principles

- Use deterministic code for facts and repeatable rules; use Claude for judgment
  where language or ambiguous evidence matters.
- Do not replace working Python logic with an LLM step.
- Do not add a paid Claude API dependency. Design for Claude Desktop/Claude Code
  under the user's subscription and its usage limits.
- Process only eligible records and pass compact evidence packets to conserve
  context.
- Return structured outputs that backend code can validate.
- Mark insufficient evidence explicitly. Never fill gaps with plausible claims.
- Cite the evidence used for every insight and personalization.
- Keep work resumable so an interrupted Claude session can continue safely.

## Research Contract

For each eligible contact, receive:

- Campaign and targeting rules
- Normalized company and contact fields
- Existing internal evidence
- Verification and suppression status
- Initial Fit Score and components
- Research questions still unanswered

Return JSON shaped like:

```json
{
  "contact_id": "uuid",
  "company_insights": [
    {
      "claim": "Concise factual claim",
      "source_url": "https://example.com/source",
      "retrieved_at": "ISO-8601",
      "evidence_summary": "Why the source supports the claim",
      "confidence": 0.0
    }
  ],
  "contact_insights": [],
  "score_recommendation": {
    "evidence_of_need": 0,
    "timing": 0,
    "personalization_material": 0,
    "data_confidence": 0,
    "reason": "Concise explanation"
  },
  "status": "complete|insufficient_evidence|human_review",
  "warnings": []
}
```

The backend validates IDs, URLs, ranges, freshness, required fields, and rule
versions before accepting the result. Claude's score is a recommendation; the
backend calculates the authoritative score.

## Drafting Contract

Draft only after eligibility, verification, suppression, and score gates pass.

- Base personalization only on attached evidence.
- Prefer one relevant, specific observation over several weak ones.
- Do not claim the prospect has a problem unless the evidence supports it.
- Do not invent familiarity, customers, results, relationships, or urgency.
- Respect campaign tone, offer, length, prohibited phrases, and required footer.
- Return subject, body, evidence IDs used, and a short rationale.
- A draft is never an approval and never permission to schedule.

Any edit creates a new immutable draft version. Approval must reference the exact
version and approver.

## Minimal MCP Boundary

If the first campaign needs Claude integration, build one local custom MCP server
as a narrow adapter over authenticated backend services. Do not connect Claude
directly to RDS.

Allow only the smallest necessary tools:

- `get_campaign_rules`
- `get_scoring_batch`
- `get_contact_packet`
- `submit_claude_score`
- `submit_email_draft`
- `flag_insufficient_evidence`
- `request_human_review`

Tool inputs and outputs must use stable IDs, schemas, pagination, and idempotency
keys. Mutating calls require validation and audit logging.

Do not expose tools to:

- Run arbitrary SQL or shell commands
- Read secrets or unrestricted tables
- Delete or bypass suppressions
- Mark emails verified
- Approve drafts
- Launch campaigns or send emails
- Change mailbox, warm-up, rotation, or sending limits

Use Saleshandy's API and webhooks through the backend. Do not make Claude the
integration hub or the system of record.

## Browser and Data Acquisition

Targeting criteria and Sales Navigator result URLs are created by the user. Data
is acquired through the operator-driven Chrome extension
(`extensions/salesnav-capture/`, product name "VMR Contact Capture").

The extension is **contact-first**: it captures a person the operator has
deliberately opened or selected, preserves the visible evidence, and submits them
as a permanent Contact through `POST /api/intake/contact-captures`
(`linkedin-contact-capture/2.0.0`). It never selects or requires a campaign, and
research, qualification, email discovery, verification, audience building, and
outreach are all downstream of it.

The extension captures observations; the backend owns identity resolution,
provenance and freshness, label resolution, suppression, and every canonical
record. Keep the downstream contract independent of the acquisition method: a
spreadsheet import and a capture converge on the same rules, not the same
payload.

For public-web research, obey source access restrictions and store provenance.
Do not collect sensitive personal data that is unnecessary for legitimate B2B
outreach.

## Coding Behavior

- Start with the relevant acceptance criterion in `GOAL.md`.
- Inspect existing code and tests before proposing architecture.
- Implement and verify a thin end-to-end slice before adding abstractions.
- Ask only when a missing choice changes safety, cost, or product behavior.
- Keep integration adapters replaceable.
- Prefer explicit state machines and typed schemas.
- Add safe dry-run modes before live actions.
- Never silently broaden the scope.
- When another thread is building concurrently, work only inside the declared
  ownership block and against the exact frozen base SHA supplied. If that SHA is
  no longer the tip of the base branch, stop and report it rather than following
  the moving branch.
- Do not declare a stacked or assembled branch final until the gate sequence
  defined in `docs/PARALLEL_INTEGRATION.md` passes locally on the final head.
  When the session cannot run those gates, say `Integration incomplete; do not
  publish yet` instead of handing over a supposedly final bundle.

When suggesting a future feature, label it as post-launch and do not build it
unless the goal file is updated.

## Project Tracking Behavior

GitHub is the development command center. After a meaningful build, provide a
structured handoff containing:

- Authorized phase and issues
- Branch and commits, including whether the branch is actually on GitHub
- Base SHA, head SHA, bundle path and SHA-256, `git bundle verify` output, and
  `git merge-base` proof against the declared base
- Local check results and reproducible evidence
- What became usable
- What remains incomplete
- Known failures, risks, and recovery behavior
- Blockers and decisions required, with an owner where known
- Claude's claimed phase status and go-live answer
- A concise proposed tracker update

Do not claim that Claude's own tests or handoff constitute independent
acceptance. Sahil decides material scope, risk, cost, and product questions.
ChatGPT operates the remote GitHub workflow and independently verifies the
build.

## GitHub Division of Labour

Claude builds; Sahil bridges; ChatGPT operates and reviews.

Claude owns the product implementation:

- Create branches and commit intentional changes with clear messages.
- Deliver the branch to the local repository (git bundle handoff) when the
  session cannot push directly.
- Supply a factual handoff that ChatGPT can verify against the repository.
- Inspect check failures and prepare correction commits for ChatGPT's review
  findings.
- Identify which authorized issues and acceptance criteria the build addresses.

Sahil owns only the bridge and decision points:

- Push a prepared branch through CMD or GitHub Desktop when Claude cannot
  authenticate to GitHub.
- Resolve material scope, cost, risk, product, and launch decisions.
- Explicitly approve a merge or other consequential GitHub action when asked.

Once the branch is on GitHub, ChatGPT owns remote administration:

- Open or update PRs and write PR descriptions, issue comments, review verdicts,
  labels, project status, and closing notes.
- Inspect the actual diff and CI rather than relying on Claude's handoff.
- Request corrections from Claude and verify each correction commit.
- Merge only after a passing verdict and Sahil's explicit approval.
- Close or update linked issues and clean up remote branches where appropriate.

When several Claude threads build related work at the same time, exactly one of
them is the integration thread. Implementation threads produce commits and
bundles only. The integration thread selects the authoritative upstream heads,
restacks downstream commits in dependency order, resolves semantic conflicts,
runs the complete gate sequence on the final assembled head, and produces the
only branches handed over for publication. Publication itself is unchanged:
Claude pushes when credentialed, Sahil bridges when it is not, and ChatGPT owns
the PR and the merge. An implementation thread never restacks another thread's
work and never declares a dependent branch final.

Do not ask Sahil to author GitHub content or perform web administration that
ChatGPT can perform. When a local push is unavoidable, provide the shortest
exact CMD or GitHub Desktop step and verify the resulting remote SHA.

Do not produce tracker noise for every commit. Do not invent dates, confidence,
owners, completion, or metrics.

## GitHub Writing Rule

- When creating or updating GitHub pull requests, issues, comments, commits, or release notes, write in the voice of the project maintainer.
- Do not mention Claude, Claude Cowork, AI assistance, generation, or authorship unless Sahil explicitly asks for that wording in that specific message.

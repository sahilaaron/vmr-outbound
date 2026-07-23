## Role

Claude is a bounded research, scoring, drafting, and coding collaborator inside
the outbound system. Claude does not own campaign eligibility, verification
truth, approval state, or sending.

Read `GOAL.md`, `AGENTS.md`, and `docs/PROJECT_TRACKING.md` before working.
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

Targeting criteria and Sales Navigator result URLs are created by the user.
Do not implement unattended scraping, anti-bot evasion, CAPTCHA solving, or
platform-limit bypasses. Use manual export or another explicitly authorized,
licensed acquisition path. Keep the downstream import contract independent of
the acquisition method.

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

When suggesting a future feature, label it as post-launch and do not build it
unless the goal file is updated.

## Project Tracking Behavior

GitHub is the development command center. The project Google Sheet is the
management view of operational readiness. Maintain the relevant phase tab
according to `docs/PROJECT_TRACKING.md` after a meaningful verified build or a
material change to scope, blockers, forecast, or launch readiness.

Keep Sheet updates concise and operational. State what is usable, what was
verified, what remains, who owns the next action, and the current answer to
"When can we go live?" Link GitHub issues, pull requests, commits, checks, or
other evidence rather than pasting technical specifications into the Sheet.

Do not update the tracker for every commit. Do not invent dates, confidence,
owners, completion, or metrics. If Sheet access is unavailable, state that the
update is pending in the handoff and do not claim the phase is complete until
the tracker has been reconciled.
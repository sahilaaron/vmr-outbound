## Project

This repository builds the first working version of an agent-assisted outbound
sales operating system. It is a semi-automated workflow with deterministic
software, selective AI judgment, and explicit human approval before outreach.

The immediate objective is a safe, observable first campaignÃ¢â‚¬â€not a fully
autonomous sales agent or a general-purpose sales platform.

## Read Order

Before changing code, read:

1. `GOAL.md` for the current milestone, acceptance criteria, and non-goals.
2. This file for repository-wide engineering and safety rules.
3. `CLAUDE.md` when Claude is doing scoring, research, drafting, or MCP work.
4. `docs/PROJECT_TRACKING.md` before planning a phase, reporting build progress,
   or updating the management tracker.

If documents conflict, use this priority:

1. The user's latest explicit instruction
2. `GOAL.md`
3. `AGENTS.md`
4. `CLAUDE.md`
5. `docs/PROJECT_TRACKING.md`
6. Existing implementation conventions

## System Boundaries

Use these ownership rules:

- RDS is the system of record for campaigns, contacts, evidence, scores,
  verification results, approvals, sends, replies, and audit events.
- Python services own deterministic work: imports, normalization, deduplication,
  identity matching, email candidate generation, cache policy, state
  transitions, and integrations.
- MillionVerifier supplies external mailbox verification.
- Claude supplies bounded judgment: evidence classification, scoring support,
  personalization, and draft generation.
- Saleshandy owns campaign execution, mailbox rotation, warm-up features, and
  delivery operations. Sync relevant events back to RDS.
- The dashboard is a control and review surface. It must call backend services;
  it must not contain business rules that exist only in the browser.

External systems are adapters, not sources of truth.

## Project Records

Use GitHub as the development command center and engineering source of truth.
Issues, pull requests, commits, checks, and release evidence belong there.

Use the project Google Sheet as the management source of truth for operational
readiness, forecast, blockers, owners, decisions, and the current answer to
"When can we go live?" Follow `docs/PROJECT_TRACKING.md` for the required tabs,
fields, update triggers, and status definitions.

Update the relevant phase tab after a meaningful verified build, a material
scope or readiness change, or discovery/resolution of a launch blocker. Do not
turn the Sheet into a duplicate technical backlog or update it for every commit.
Link to GitHub evidence instead of copying technical specifications.

Never invent completion, dates, owners, metrics, or readiness. If the tracker
cannot be updated, record the pending update in the handoff and complete it
before declaring the phase finished.

## First-Launch Workflow

Preserve this sequence and its audit trail:

1. A human creates a campaign and defines targeting.
2. Contacts are imported from an authorized source.
3. Records are normalized, deduplicated, and checked against suppressions.
4. Email candidates are generated deterministically.
5. The internal database is checked for exact-address and domain-pattern
   evidence.
6. MillionVerifier is called when the exact-address cache is absent or stale.
7. Hard eligibility gates are applied.
8. An Initial Fit Score is calculated.
9. Contacts scoring at least 85/100 may enter insights research.
10. Company and contact evidence is collected with provenance.
11. An Outreach Readiness Score and personalized draft are produced.
12. A human reviews the exact draft version.
13. Only an approved version may be scheduled in Saleshandy.
14. Delivery, reply, bounce, unsubscribe, and campaign events return to RDS.

Do not interpret Ã¢â‚¬Å“top 85%Ã¢â‚¬Â as a percentile. The launch rule is a configurable
score threshold initially set to 85/100.

## Non-Negotiable Guardrails

- Never send or schedule an email without explicit approval of that exact draft
  version.
- Editing an approved draft invalidates its approval.
- Never contact an unsubscribed, suppressed, hard-bounced, or legally excluded
  address.
- Never fabricate an insight, source, verification result, score input, or
  personalization claim.
- Store source URL, retrieval time, evidence excerpt or summary, and confidence
  for externally derived insights.
- Treat a catch-all domain as uncertainty, not proof that a mailbox exists.
- A verified email pattern for one employee may rank candidates for the same
  domain; it does not prove that another employee's mailbox exists.
- Never expose unrestricted SQL, arbitrary code execution, suppression deletion,
  mailbox-limit changes, or bulk-send actions through an agent tool.
- Do not bypass access controls, CAPTCHAs, platform limits, or terms of use.
  Sales Navigator acquisition must be manual or use an explicitly authorized
  and compliant method. Keep acquisition replaceable behind an interface.
- Do not add paid model APIs or pay-per-token dependencies without explicit
  approval. The intended judgment surface is the user's Claude subscription.
- Keep secrets out of source, prompts, logs, screenshots, fixtures, and client
  code. Use environment variables or the chosen secret manager.

## Email Intelligence Rules

Separate these facts in the schema:

- `exact_email_verification`: evidence about one full email address
- `domain_pattern_observation`: evidence about a naming pattern at a domain
- `domain_mail_state`: MX/provider/catch-all observations about a domain

For the first campaign, use safe verification mode:

- Reuse a recent result only for the same normalized full email address.
- Use domain patterns to order generated candidates, never to auto-mark them
  valid.
- Verify each new unique candidate selected for outreach.
- Make TTLs configurable and store `checked_at`, provider, result, reason, and
  raw provider reference.
- Suggested initial TTLs: valid 15Ã¢â‚¬â€œ30 days, invalid 30 days, catch-all 7Ã¢â‚¬â€œ15
  days, unknown 1Ã¢â‚¬â€œ3 days. Confirm final values against provider guidance before
  production.
- A hard bounce creates a suppression event. An opt-out does not expire.

Historical data must enter through staging with source provenance, import
timestamps, normalization, deduplication, and confidence. It must never silently
overwrite newer live evidence.

## Scoring Rules

Apply hard gates before a numerical score. Initial gates include invalid or
suppressed email, wrong geography/company/industry/role, former employee,
existing customer, competitor, and campaign contact saturation.

Keep the score deterministic and explainable. Initial weighting:

- Company fit: 25
- Contact fit: 25
- Evidence of need: 20
- Timing: 15
- Personalization material: 10
- Data confidence: 5

Maintain two distinct scores:

- Initial Fit Score: computed before deep research
- Outreach Readiness Score: computed after evidence collection

Store component scores, rule version, evidence references, and a concise reason.
Claude may classify evidence or recommend component values; backend rules own
the final calculation and eligibility decision.

## Engineering Rules

- Build the smallest vertical slice that satisfies `GOAL.md`.
- Prefer boring, reversible, testable code over premature infrastructure.
- Preserve user changes and avoid unrelated refactors.
- Put business logic in services with typed inputs and outputs.
- Make integrations idempotent and retry-safe. Store external IDs and webhook
  event IDs.
- Use explicit workflow states; reject illegal transitions.
- Every automated mutation must record actor, timestamp, previous state, new
  state, and reason.
- Use database migrations. Never rely on manual production schema edits.
- Add fixtures with synthetic data only.
- Validate all imports and external payloads at the boundary.
- Paginate bulk work and persist resumable cursors.
- Rate-limit external calls and make cost/usage visible.
- Design for one organization and one operating team first. Do not add
  multi-tenancy unless `GOAL.md` changes.

## Minimum Tests

Every relevant change must cover:

- Unit tests for normalization, email generation, cache policy, scoring, and
  state transitions
- Integration tests for database persistence and adapter contracts
- Contract fixtures for MillionVerifier and Saleshandy payloads/webhooks
- An end-to-end dry run that cannot send real email
- Negative tests for suppression, stale approvals, duplicate webhooks, invalid
  imports, catch-all handling, and retries

No production send credential may be used in automated tests.

## Definition of Done

A change is done only when:

- It directly supports an acceptance criterion in `GOAL.md`.
- Tests pass and the relevant dry-run path works.
- Failure and retry behavior are defined.
- User-visible states and errors are understandable.
- Audit data is stored.
- Documentation and configuration examples are updated.
- The relevant project-tracking phase tab is current when the change materially
  affects deliverables, blockers, forecast, or launch readiness.
- No non-goal was introduced indirectly.

When a useful idea falls outside the launch scope, add it to a short backlog
note or issue; do not implement it opportunistically.
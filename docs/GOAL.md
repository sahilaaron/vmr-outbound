## Current Goal
Launch one safe, measurable outbound campaign through a cohesive semi-automated
system.

The first version must take an authorized contact batch from import to
Saleshandy scheduling while preserving data provenance, deterministic
eligibility, email-verification safety, explainable scoring, evidence-based
personalization, and human approval.

Success is a working vertical slice for a controlled pilotÃ¢â‚¬â€not feature
completeness or full autonomy.

## Launch User Journey

1. Create a campaign with targeting rules, scoring threshold, offer, tone, and
   sending configuration reference.
2. Import a CSV contact batch and see row-level validation errors.
3. Normalize and deduplicate contacts; match them to companies and existing
   historical records.
4. Apply suppressions and hard eligibility gates.
5. Generate ranked email candidates from the prospect name and company domain.
6. Check exact-address verification history and domain-pattern observations.
7. Call MillionVerifier for each selected new or stale exact address; safely
   represent valid, invalid, catch-all, and unknown results.
8. Calculate an explainable Initial Fit Score.
9. Move contacts scoring at least 85/100 into company/contact insights research.
10. Capture evidence with sources and compute an Outreach Readiness Score.
11. Generate a personalized email draft from approved evidence.
12. Review, edit, and approve the exact draft version from desktop or phone.
13. Schedule approved contacts in Saleshandy.
14. Sync delivery, bounce, unsubscribe, reply, and campaign status back to RDS.
15. View campaign progress, exceptions, and outcomes in the dashboard.

## Required MVP Surfaces

Build only these dashboard surfaces:

- Campaign list and campaign creation
- Campaign workspace with stage counts and exception alerts
- Contact table with import, filters, bulk stage action, and score columns
- Contact detail with verification, evidence, scores, and audit history
- Insights review queue
- Draft review and approval queue optimized for mobile
- Campaign execution and reply/outcome status
- Minimal system health view for integration failures and stale jobs

Rich visual customization is secondary to clear states and safe actions.

## Required Backend Capabilities

- RDS schema and migrations for campaigns, companies, contacts, identities,
  imports, suppressions, email candidates, verification evidence, domain-pattern
  observations, insights, scores, draft versions, approvals, external events,
  and audit events
- CSV staging import with provenance, validation, normalization, deduplication,
  and identity resolution
- A repeatable path for importing representative historical marketing data
- Deterministic email-format generation and candidate ranking
- Safe exact-address verification cache with configurable TTLs
- MillionVerifier adapter with rate limits, retries, idempotency, and usage logs
- Versioned hard gates and scoring rules
- Company and contact insight service using the user's Python scripts
- Minimal Claude MCP bridge for eligible batches, structured score
  recommendations, and draft submission
- Immutable draft versions and exact-version approval
- Saleshandy API adapter and webhook/event ingestion
- Resumable background jobs and visible failure states
- Dry-run mode that completes the workflow without scheduling real email

## Launch Acceptance Criteria

The MVP is launch-ready when:

- A synthetic end-to-end dry run succeeds without any real send.
- One authorized pilot batch of 100 contacts imports with actionable row errors.
- Duplicate people and companies do not create duplicate outreach records.
- Historical data ranks email candidates but cannot falsely mark a new mailbox
  valid.
- Recent exact-address verifications are reused; stale or absent results call
  MillionVerifier according to policy.
- Catch-all and unknown outcomes remain visibly uncertain and cannot silently
  become Ã¢â‚¬Å“valid.Ã¢â‚¬Â
- Suppressed, opted-out, hard-bounced, invalid, and ineligible contacts cannot
  reach scheduling.
- Every score shows components, rule version, evidence, and reason.
- Only contacts meeting the configured threshold enter insights research.
- Every personalization claim links to stored evidence.
- Claude output failing schema or evidence checks is rejected or routed to human
  review.
- Editing a draft invalidates its previous approval.
- Saleshandy receives only currently eligible contacts with an approved draft
  version.
- Duplicate webhook delivery does not duplicate state changes.
- Campaign stages, failures, approvals, and outcomes are visible on desktop and
  phone.
- Each material automated action has an audit record.
- The relevant Google Sheets phase tab reflects the latest verified build,
  links to GitHub evidence, and gives a current answer to: "When can we go
  live?"

After the 100-contact pilot is reviewed, scale deliberately to 250 and then 500
contacts. A target of 5,000 contacts per month is a later operating milestone,
not the first launch test.

Project tracking is delivery evidence, not application functionality. Follow
`docs/PROJECT_TRACKING.md` for the management tracker contract; do not build
Google Sheets integration into the product unless this goal is explicitly
changed.

## Build Order

1. Repository skeleton, configuration, database schema, audit model, and app
   shell
2. Campaign creation, CSV staging import, normalization, deduplication, and
   suppressions
3. Historical-data import and internal email intelligence
4. Email generation, cache policy, and MillionVerifier integration
5. Hard gates, Initial Fit Score, and contact-stage workflow
6. Insights services, evidence model, and Outreach Readiness Score
7. Minimal Claude MCP tools and structured result validation
8. Draft versioning, mobile review, and approval controls
9. Saleshandy scheduling adapter, webhooks, and outcome sync
10. End-to-end dry run, security review, 100-contact pilot, and launch review

Complete and verify each step before widening the next. A thin vertical path may
be wired early, but unfinished later phases must remain disabled. After each
meaningful verified build, update the relevant phase tab as defined in
`docs/PROJECT_TRACKING.md`.

## Explicitly Out of Scope

Do not build these for the first campaign:

- Fully autonomous prospect acquisition or unattended Sales Navigator scraping
- CAPTCHA solving, anti-bot evasion, or terms-of-use circumvention
- Economy verification that extrapolates one mailbox result to unverified people
- Automatic sending or scheduling without exact-draft human approval
- Automatic reply generation or autonomous reply sending
- Native iOS or Android apps; the responsive web dashboard/PWA is sufficient
- Multi-tenant SaaS, billing, white-labeling, roles beyond the small operating
  team, or a public plugin marketplace
- A general agent platform, arbitrary workflow builder, or unrestricted MCP
  server
- Paid LLM API integration
- Windows VPS deployment, 24/7 autonomous routines, or multi-agent
  orchestration
- Mailbox/domain purchasing, DNS setup, or warm-up infrastructure; IT and
  Saleshandy own these
- Advanced deliverability analytics, inbox placement testing, CRM replacement,
  calendar booking, or omnichannel outreach
- Supporting every historical file shape before one representative import path
  works

## Scope-Change Rule

A feature enters the launch scope only when it is necessary to satisfy an
acceptance criterion or mitigate a launch-blocking safety risk. Update this file
before implementing any newly approved scope.
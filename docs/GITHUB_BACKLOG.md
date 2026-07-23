# VMR Outbound Agent — GitHub Backlog

## How to Use This Backlog

Each workstream below should become a GitHub parent issue. Each table row should
become an issue card or sub-issue.

- `P0 — Launch`: required for the first controlled 100-contact campaign
- `P1 — Post-pilot`: consider only after reviewing real campaign use
- `P2 — Parked`: preserve the idea, but do not build it yet

Creating a `P1` or `P2` card does not authorize development. Moving one into the
launch scope requires an explicit update to `GOAL.md`.

## Recommended GitHub Project Setup

### Status

`Inbox` → `Ready` → `In progress` → `Review` → `Blocked` → `Done`

Use `Deferred` for intentionally parked cards.

### Fields

- Priority: `P0`, `P1`, `P2`
- Workstream: one of the epics below
- Release: `First campaign`, `Post-pilot`, `Later`
- Type: `Decision`, `Feature`, `Engineering`, `Safety`, `Test`, `Operations`
- Owner
- Dependency

### Labels

`priority:p0`, `priority:p1`, `priority:p2`

`type:decision`, `type:feature`, `type:engineering`, `type:safety`,
`type:test`, `type:operations`

`area:foundation`, `area:data`, `area:campaigns`, `area:email`,
`area:verification`, `area:scoring`, `area:insights`, `area:claude`,
`area:drafts`, `area:saleshandy`, `area:ui`, `area:operations`, `area:qa`

---

## EPIC 01 — Product Decisions and Foundation

| ID | Priority | Card | Done when |
| --- | --- | --- | --- |
| FND-001 | P0 | Decide the first-release technical stack | Frontend, backend, database access, background work, tests, and the reason for each choice are recorded in one short note. |
| FND-002 | P0 | Decide the first-release hosting model | The team knows where the private dashboard and backend run, how the phone reaches them, and what hosting work is deferred. |
| FND-003 | P0 | Create repository structure and local setup | Application, services, integrations, tests, migrations, scripts, and docs have clear homes; a new agent can start the project from written steps. |
| FND-004 | P0 | Add configuration and secret handling | Required settings are documented and secrets are excluded from source, logs, fixtures, and browser code. |
| FND-005 | P0 | Add automated checks and database migrations | Formatting, code checks, tests, and migration validation run before merge; schema changes are repeatable. |
| FND-006 | P0 | Add private single-operator access | The deployed dashboard is protected without building a full user-role system. |
| FND-007 | P0 | Add audit records, feature switches, and dry-run mode | Important actions are traceable, unfinished functions stay disabled, and the workflow can run without scheduling real email. |
| FND-008 | P0 | Define the authorized contact-input contract | Accepted columns and provenance fields are documented without depending on unattended Sales Navigator scraping. |

## EPIC 02 — Core Data and Historical Imports

| ID | Priority | Card | Done when |
| --- | --- | --- | --- |
| DAT-001 | P0 | Create the core RDS schema | Campaigns, companies, contacts, imports, suppressions, email evidence, insights, scores, drafts, approvals, external events, and audits are represented. |
| DAT-002 | P0 | Build staged CSV import and row validation | Every upload has a batch, raw rows, column mapping, processing state, and actionable row-level errors. |
| DAT-003 | P0 | Normalize company and contact data | Names, domains, URLs, countries, titles, and emails are consistent while original values remain available. |
| DAT-004 | P0 | Deduplicate companies and resolve contacts | Strong matches merge safely; uncertain matches enter review instead of being silently combined. |
| DAT-005 | P0 | Preserve provenance and evidence freshness | Every value retains source and observation time; old imports cannot silently replace newer evidence. |
| DAT-006 | P0 | Build and enforce the suppression ledger | Opt-outs, hard bounces, customers, competitors, and internal exclusions block every route into outreach. |
| DAT-007 | P0 | Import one representative historical dataset | One real file shape is staged, cleaned, reconciled, and reported as accepted, rejected, merged, ambiguous, or suppressed. |
| DAT-008 | P1 | Support additional historical file shapes | Add mappers only for formats proven necessary after the representative import succeeds. |

## EPIC 03 — Campaign and Contact Workflow

| ID | Priority | Card | Done when |
| --- | --- | --- | --- |
| CMP-001 | P0 | Build campaign creation and settings | A draft campaign stores its offer, audience rules, exclusions, score threshold, tone, owner, source, and sending reference. |
| CMP-002 | P0 | Define and enforce contact workflow states | Legal transitions from import through verification, research, approval, scheduling, and outcomes are explicit and audited. |
| CMP-003 | P0 | Link contacts to campaigns and outreach history | A contact can appear in multiple campaigns without losing earlier activity or creating duplicate active outreach. |
| CMP-004 | P0 | Add company-contact saturation controls | The campaign caps how many people at one company may be researched or contacted within a set period. |
| CMP-005 | P0 | Add controlled batch actions and stage counts | Only eligible selected contacts move forward; previews, results, blocked reasons, and campaign counts stay accurate. |

## EPIC 04 — Email Generation and Internal Intelligence

| ID | Priority | Card | Done when |
| --- | --- | --- | --- |
| EML-001 | P0 | Normalize names and domains for email generation | Punctuation, compound names, middle names, Unicode, subdomains, and invalid domains are handled predictably. |
| EML-002 | P0 | Build versioned email-pattern generation | Common patterns are generated deterministically without duplicates and each candidate records its rule and version. |
| EML-003 | P0 | Separate exact-email, domain-pattern, and mail-domain facts | The data model cannot mistake a company pattern or catch-all domain for proof that a specific mailbox exists. |
| EML-004 | P0 | Rank candidates using internal evidence | Fresh, strong domain-pattern evidence changes candidate order but never marks a new address valid. |
| EML-005 | P0 | Select the next verification candidate | The service chooses one candidate transparently and records why; ambiguous or impossible names enter review. |
| EML-006 | P0 | Expose internal email intelligence | The operator sees candidates, pattern observations, exact results, freshness, confidence, and selected-address reasoning. |
| EML-007 | P1 | Improve ranking from campaign outcomes | Valid results and bounces may adjust ordering only after enough real evidence exists. |

## EPIC 05 — MillionVerifier and Verification Safety

| ID | Priority | Card | Done when |
| --- | --- | --- | --- |
| VER-001 | P0 | Build the MillionVerifier adapter | One normalized address can be submitted and its response stored through a replaceable backend interface. |
| VER-002 | P0 | Map provider outcomes into internal states | Valid, invalid, catch-all, unknown, disposable, role-based, and provider-error results have explicit meanings. |
| VER-003 | P0 | Add exact-address caching and freshness rules | Only the same full address can reuse a recent result; time limits are configurable and policy-versioned. |
| VER-004 | P0 | Enforce conservative catch-all handling | Catch-all and unknown addresses remain uncertain and cannot silently become verified or scheduling-ready. |
| VER-005 | P0 | Add rate limits, retries, and idempotency | Temporary failures retry safely; repeated work avoids duplicate requests and charges where possible. |
| VER-006 | P0 | Track verification usage and exceptions | Calls, cache reuse, failures, credit use, stale results, contradictions, and safe next actions are visible. |
| VER-007 | P0 | Add provider contract tests and a live smoke test | Representative responses are tested offline and one deliberate live request confirms credentials and mapping. |
| VER-008 | P1 | Evaluate a separate catch-all policy | Only pilot evidence can justify any limited, explicitly approved treatment. |
| VER-009 | P2 | Experiment with domain-level cost reduction | Sampling or inference remains isolated from production until accuracy and bounce risk are proven. |

## EPIC 06 — Eligibility and Lead Scoring

| ID | Priority | Card | Done when |
| --- | --- | --- | --- |
| SCR-001 | P0 | Implement hard eligibility gates | Suppression, invalid email, former employee, wrong audience, customer, competitor, and saturation rules run before scoring. |
| SCR-002 | P0 | Build versioned, explainable score records | Components, total, rule version, evidence, and concise reason are stored without rewriting older scores. |
| SCR-003 | P0 | Implement the Initial Fit Score | Company and contact fit are calculated before deep research with deterministic, configurable rules. |
| SCR-004 | P0 | Enforce the 85/100 research threshold | Only contacts meeting the configured absolute threshold can enter insights research. |
| SCR-005 | P0 | Implement the Outreach Readiness Score | Evidence of need, timing, personalization material, and data confidence produce a separate post-research score. |
| SCR-006 | P0 | Explain scores and recalculate safely | The dashboard explains pass, fail, and review decisions; changed inputs produce a new versioned result. |
| SCR-007 | P0 | Add a pre-launch scoring review sample | The operator can inspect high, borderline, and rejected examples before any contact is scheduled. |
| SCR-008 | P1 | Calibrate weights and thresholds from outcomes | Real replies, bounces, false positives, and operator judgment inform changes after the pilot. |

## EPIC 07 — Company and Contact Insights

| ID | Priority | Card | Done when |
| --- | --- | --- | --- |
| INS-001 | P0 | Create the evidence and insight model | Claims, source URL, retrieval time, evidence summary, confidence, subject, and freshness are stored separately. |
| INS-002 | P0 | Wrap the company-insights Python script | Eligible company records can be processed through a typed, testable, resumable service. |
| INS-003 | P0 | Wrap the contact-insights Python script | Eligible contact records can be processed through a typed, testable, resumable service. |
| INS-004 | P0 | Build compact research packets | Research receives campaign rules, known facts, unanswered questions, and stable identifiers—nothing unnecessary. |
| INS-005 | P0 | Validate sources and prevent unsupported claims | Missing sources, malformed results, inaccessible evidence, and claims not supported by evidence are rejected or reviewed. |
| INS-006 | P0 | Reuse fresh evidence and handle insufficient evidence | Research avoids duplicate work and can finish honestly without producing fake personalization. |
| INS-007 | P0 | Build a resumable research and review queue | Batches pause, retry, and continue safely; the operator can review conflicts, confidence, and missing evidence. |
| INS-008 | P0 | Limit sensitive-data collection | Only information necessary for legitimate B2B outreach is retained. |

## EPIC 08 — Claude Subscription and Minimal MCP

| ID | Priority | Card | Done when |
| --- | --- | --- | --- |
| CLD-001 | P0 | Define the manual Claude operating runbook | It explains what the backend prepares, what Claude Desktop/Code does, where usage limits may interrupt work, and what remains human. |
| CLD-002 | P0 | Build the authenticated MCP bridge | Claude reaches narrow backend services without database credentials or unrestricted database access. |
| CLD-003 | P0 | Add read tools for rules, batches, and contact packets | Claude can retrieve only authorized, paginated, compact data using stable identifiers and resumable cursors. |
| CLD-004 | P0 | Add submission tools for score recommendations and drafts | Structured results are validated, evidence-linked, idempotent, audited, and stored as recommendations or draft versions. |
| CLD-005 | P0 | Add insufficient-evidence and human-review tools | Claude can report uncertainty without changing verification, eligibility, approval, or scheduling state. |
| CLD-006 | P0 | Prove forbidden actions are unavailable | Tests confirm Claude cannot run SQL, remove suppressions, mark emails valid, approve drafts, change mailbox limits, or schedule campaigns. |
| CLD-007 | P1 | Evaluate scheduled Claude routines | Consider only after manual runs are reliable and subscription/runtime limits are understood. |

## EPIC 09 — Drafting, Review, and Approval

| ID | Priority | Card | Done when |
| --- | --- | --- | --- |
| DRF-001 | P0 | Store versioned campaign drafting rules | Offer, tone, length, call to action, prohibited claims, footer, and sequence position are explicit. |
| DRF-002 | P0 | Build evidence-backed draft generation | Claude receives approved evidence and returns validated subject, body, evidence IDs, and rationale. |
| DRF-003 | P0 | Create immutable draft versions and editing | Every generation or human edit creates a new version without destroying history. |
| DRF-004 | P0 | Implement exact-version approval | Approval records approver, time, version, and rules; any edit invalidates earlier approval. |
| DRF-005 | P0 | Recheck eligibility before approval and scheduling | Suppression, verification freshness, score, evidence, and campaign state must still pass. |
| DRF-006 | P0 | Build mobile-first review and payload preview | The owner can inspect evidence, edit, approve, reject, request revision, and preview exactly what Saleshandy will receive. |
| DRF-007 | P1 | Consider guarded bulk approval | Real review volume must prove the need and determine sampling safeguards. |

## EPIC 10 — Saleshandy Execution and Outcome Sync

| ID | Priority | Card | Done when |
| --- | --- | --- | --- |
| SHY-001 | P0 | Configure Saleshandy and campaign mapping | Credentials remain private; internal campaigns have stable external references and test behavior. |
| SHY-002 | P0 | Build the approved-contact payload | Only currently eligible contacts with approved draft versions can form a Saleshandy payload. |
| SHY-003 | P0 | Schedule one contact and then a reviewed batch | Submissions have previews, result reports, external IDs, and duplicate protection. |
| SHY-004 | P0 | Receive and validate Saleshandy events | Authentic webhooks update scheduled, sent, bounced, unsubscribed, replied, paused, and failed states idempotently. |
| SHY-005 | P0 | Apply bounce and opt-out suppressions | Hard bounces and unsubscribes block future outreach according to policy across campaigns. |
| SHY-006 | P0 | Add reconciliation and retry controls | Missed events and recoverable failures can be compared and retried without duplicating state. |
| SHY-007 | P0 | Record IT mailbox-readiness sign-off | The launch checklist confirms domains, mailboxes, rotation, limits, and warm-up readiness outside the app. |
| SHY-008 | P1 | Add richer mailbox-health reporting | Build only if Saleshandy data and real operations reveal a decision the current view cannot support. |

## EPIC 11 — Dashboard and Mobile Control

| ID | Priority | Card | Done when |
| --- | --- | --- | --- |
| UI-001 | P0 | Integrate the approved Claude Design shell | The visual direction becomes maintainable components without moving business rules into the browser. |
| UI-002 | P0 | Build campaign list, creation, and workspace | Campaign settings, stage counts, exceptions, next actions, and execution state are clear. |
| UI-003 | P0 | Build contact import and reconciliation | Upload, mapping, validation, progress, row errors, and batch results are usable. |
| UI-004 | P0 | Build the contact table | Search, filters, pagination, essential columns, selection, scores, stage, verification, and allowed batch actions work. |
| UI-005 | P0 | Build the contact detail view | Identity, company, provenance, email intelligence, scores, evidence, drafts, and audit history are visible. |
| UI-006 | P0 | Build verification, insights, and draft queues | Each queue exposes status, exceptions, evidence, blocked reasons, and safe next actions. |
| UI-007 | P0 | Build execution, outcomes, and system-health views | Saleshandy state, replies, failures, stale work, and integration readiness are visible. |
| UI-008 | P0 | Build the mobile action centre | Approvals, failed jobs, blocked records, and other owner decisions require minimal taps. |
| UI-009 | P0 | Add complete interface states and accessibility | Loading, empty, error, blocked, keyboard, contrast, touch, and common phone-width behavior are covered. |
| UI-010 | P1 | Add saved views and dashboard customization | Only repeated real usage should determine which filters, columns, and widgets deserve persistence. |

## EPIC 12 — Reliability and Operations

| ID | Priority | Card | Done when |
| --- | --- | --- | --- |
| OPS-001 | P0 | Add resumable background work | Imports, verification, scoring, research, and synchronization persist progress and continue safely after interruption. |
| OPS-002 | P0 | Add concurrency, rate, and duplicate controls | Large batches and repeated clicks cannot flood providers or duplicate work. |
| OPS-003 | P0 | Add safe logs, health checks, and visible failures | Work can be traced by campaign, contact, batch, and job without exposing secrets; operators see clear recovery actions. |
| OPS-004 | P0 | Deploy the first private environment | Dashboard and backend use HTTPS, private access, managed settings, and a repeatable deployment. |
| OPS-005 | P0 | Add backups, retention rules, and recovery runbooks | Database restoration is tested; retention, deletion, deployment, rollback, credential rotation, and job recovery are documented. |
| OPS-006 | P0 | Add an emergency scheduling stop | The owner can immediately prevent new Saleshandy submissions without corrupting campaign data. |
| OPS-007 | P1 | Move to a semi-autonomous VPS runtime | Consider Windows VPS or another always-on host only after the manual workflow is stable. |

## EPIC 13 — Testing, Pilot, and Review

| ID | Priority | Card | Done when |
| --- | --- | --- | --- |
| QUA-001 | P0 | Create synthetic test fixtures | Tests cover duplicates, malformed data, catch-all domains, suppressions, conflicting evidence, and provider failures. |
| QUA-002 | P0 | Test deterministic rules and integrations | Normalization, generation, caching, gates, scoring, state changes, persistence, MillionVerifier, Saleshandy, and MCP contracts are covered. |
| QUA-003 | P0 | Test every critical safety failure | Suppression bypass, stale approval, draft edits, invalid email, duplicate events, repeated submissions, and failed providers remain blocked. |
| QUA-004 | P0 | Complete the synthetic end-to-end dry run | A batch travels from import through simulated scheduling and outcome sync without a real send. |
| QUA-005 | P0 | Complete security and privacy review | Authentication, secrets, input handling, webhooks, MCP access, logs, and personal-data handling are checked. |
| QUA-006 | P0 | Prepare and preflight the 100-contact pilot | Authorized contacts, rules, spend, mailboxes, suppressions, score sample, evidence sample, drafts, limits, and stop control are signed off. |
| QUA-007 | P0 | Launch the controlled 100-contact campaign | Only approved records are scheduled, failures are monitored, and the emergency stop remains available. |
| QUA-008 | P0 | Conduct the campaign review and scale decision | Accuracy, cost, bounces, replies, research quality, drafts, manual effort, and failures support a stop, fix, or 250-contact decision. |

---

## Parked Backlog — Do Not Build Before the First-Campaign Review

| ID | Priority | Card | Activation condition |
| --- | --- | --- | --- |
| FUT-001 | P1 | Scale to 250, 500, and eventually 5,000 contacts per month | Smaller steps prove verification safety, quality, deliverability, cost, and review capacity. |
| FUT-002 | P1 | Add message A/B testing | Baseline performance and sample-size rules exist. |
| FUT-003 | P1 | Add reply classification and reply drafting | Reply volume makes manual work costly and a separate approval policy exists. |
| FUT-004 | P1 | Add calendar or CRM integration | Real campaign handoffs reveal a named bottleneck and system owner. |
| FUT-005 | P1 | Add more verification providers | Cost, coverage, or reliability evidence shows MillionVerifier is insufficient. |
| FUT-006 | P1 | Add advanced deliverability and inbox-placement reporting | First campaigns reveal decisions that current Saleshandy information cannot support. |
| FUT-007 | P1 | Add an authorized automated contact source | A licensed or explicitly permitted source and its terms are confirmed. |
| FUT-008 | P1 | Add reusable campaign templates | Several completed campaigns reveal genuinely repeated settings. |
| FUT-009 | P2 | Add native mobile applications | The responsive web dashboard proves materially insufficient. |
| FUT-010 | P2 | Add multi-organization tenancy, billing, or white-labelling | A separate decision converts the internal tool into a commercial product. |
| FUT-011 | P2 | Add advanced user roles | More operators join and actual access boundaries are known. |
| FUT-012 | P2 | Add omnichannel outreach | Email is stable and a compliant channel-specific strategy exists. |
| FUT-013 | P2 | Add a general workflow builder | Repeated campaigns prove the fixed workflow cannot meet the need. |
| FUT-014 | P2 | Add unrestricted multi-agent orchestration | A specific measured need cannot be solved by the bounded workflow. |
| FUT-015 | P2 | Add verification inference or economy mode | Controlled evidence proves it will not materially increase bounce and reputation risk. |

## Backlog Admission Test

Before adding a new `P0` card, answer:

1. Which first-campaign acceptance criterion fails without it?
2. Can a safe manual step cover the need for the first 100 contacts?
3. Does it add a vendor, recurring cost, screen, data object, or operating burden?
4. What is the smallest version that proves the need?
5. Which existing card or goal document must change?

If question 1 has no clear answer, the card belongs in `P1` or `P2`.

# Current MVP

**Status date:** 2 August 2026
**Authoritative delivery:** PR #232 merged; PR #233 is the customer-interface merge gate.

## MVP outcome

The current VMR Outbound Agent MVP is complete as a product build when one operator can:

> capture an authorized LinkedIn or Sales Navigator prospect and enrol the permanent Contact into a Campaign, and leave VMR to run that Contact through sourced Company research, exact-address email verification, evidence-backed Insights and Personalization on its own until it is Ready for Sending — a generated, validated seven-message sequence held as immutable versions.

The MVP ends at Ready for Sending. Nothing in the pipeline waits for a person: the sequence is usable as soon as it is generated and validated, and reading it, editing it or recording a decision against one exact version is optional. It does **not** send email, and sending is manual.

SalesHandy/provider submission, delivery events, replies, bounces, opt-outs, provider-side sequencing and analytics are post-MVP work.

## Product surfaces

| Route | Role |
| --- | --- |
| `/` and `/app` | Customer-facing application. Today is a compact operational overview: contacts processing, contacts ready for sending, contacts VMR could not prepare, and campaign progress |
| `/app/review` | Reached in the customer navigation as **Emails**: a reading surface for the generated messages and their evidence, with optional editing and an optional exact-version approve/discard |
| `/admin` | Operator/admin Workbench for low-level controls, jobs, retries and authoritative write paths |
| `/admin/agents/studio` | Global Agent inspection plus Agent-specific Admin modules; never exposed in `/app` |

The customer interface and Workbench share the existing service and model layers. They use separate routers, templates and stylesheets.

## Current pipeline

```text
Capture
→ Identity
→ Company
→ Research
→ Email
→ Verification
→ Insights
→ Personalization
→ Ready for Sending
```

Readiness is a projection over the artifact — current, non-superseded message versions on a live sequence that generated and validated — rather than a record of anyone having looked at it. `app/services/customer_status.py` computes it.

`Sending` remains registered so the pipeline can extend without another domain redesign, but it has no production adapter and is disabled.

## Agent status

| Agent | Current implementation | Status |
| --- | --- | --- |
| Capture | Existing intake/promotion paths plus durable exact-execution reporting | Operational; live report acceptance pending |
| Identity | Shared Agent adapter over authoritative identity services | Operational |
| Company | Exact permanent-Company linking, canonical-domain gates and durable execution lineage | Operational; live acceptance pending |
| Research | Registered deterministic research workers | Operational; live acceptance pending |
| Email | Deterministic candidate policy | Operational |
| Verification | Durable exact-address verification using the existing MillionVerifier boundary | Operational; live authority required |
| Insights | One bounded no-tools thinking call plus deterministic evidence validation and Employee Size normalization | Operational; live acceptance pending |
| Personalization | Claude CLI through the bounded thinking seam, `allowed_tools=()` and immutable Personalization Policy versions | Operational; live acceptance pending |
| Sending | Contract only | Disabled; post-MVP |

## Research authority

Research gathers source-backed evidence. The worker-based RES-001 Research adapter is authoritative.

It writes:

- the raw worker submission;
- one versioned Company dossier;
- sourced INS-001 evidence records;
- an operator-facing outcome derived from what the workers actually found.

Research does not use a language model and does not silently rewrite canonical Company fields. Claude first enters the pipeline at Insights, after evidence has been persisted.

Capture reuses the existing immutable LinkedIn snapshots, staged import rows,
promotion decisions, suppression ledger and Campaign filing services. Future
extension, import and manual/API outcomes pin a bounded historical projection in
one terminal Capture Agent Job; no second Capture queue or workflow was added.
Admin Studio separates captured execution values and exact Contact/Campaign
Contact lineage from today's Contact, merge survivor, labels, memberships and
suppression state. Older telemetry that was never stored remains explicitly
partial or unavailable. Capture hands permanent-person work to Identity and
captured employer evidence to Company without absorbing either authority.

The Company Agent reuses the existing permanent Company, capture candidate and
append-only domain-decision systems. New jobs durably pin the historical
Company/domain, exact decision ids, effective provisional-domain policy and
Research handoff. Admin Studio shows that execution separately from current
capture and Company aggregate state. `confirmed`, `provisional`, `unresolved`
and report-only `provider_only` remain distinct; unresolved blocks Research,
while provisional may start Research and reaches later stages only when the
Campaign's existing setting permits it. Company Intelligence remains outside
this pipeline slice.

Insights pins the exact Research job, raw submission and dossier it consumed,
stores attributable claims through the shared evidence model, and derives an
append-only structured Employee Size fact. Supported Employee Size uses fixed
v1 bands from `1_10` through `10001_plus`; exact counts remain absent when only
a range or approximation is supported. Conflicted, stale, unresolved and
unavailable values are visible but ineligible downstream. Email candidate order
and Verification waterfall behavior do not use Employee Size.

## Email and verification policy

The Email Agent attempts no more than three candidates in one fixed versioned order:

1. `firstname.lastname`
2. `firstname`
3. `finitiallastname`

It enqueues one child Verification Agent Job at a time and stops immediately after the first verified result.

A live MillionVerifier result on the Agent path requires:

- an `ENABLED` Verification Agent control on an execution-enabled Campaign;
- effective Verification Agent configuration containing `{"live": true}`;
- a real, non-test provider credential — `MILLIONVERIFIER_API_KEY` or an active
  Agent Studio credential.

Simulated evidence cannot complete a live Campaign stage.

`FEATURES__MILLIONVERIFIER` is **not** in that list, and this page previously
said it was. The flag gates the legacy `/verification` console routes and the
smoke script; it is never read on the Agent path
(`app/services/agents/adapters.py` → `app/services/verification/waterfall.py` →
`provider.py`). Turning it off closes the console and leaves the Agent free to
spend.

`DRY_RUN` does not gate it either. `DRY_RUN` is about sending, and the overview
banner reading "no real email can be scheduled" is true about sending and says
nothing about verification credits.

## Current operating choices

- Contacts and Companies are permanent and Campaign-independent.
- Campaign Contact owns Campaign-specific execution state and draft output.
- Campaign enrolment is currently explicit and reversible through the Workbench, including bulk enrolment with refusal accounting.
- Knowledge Base editing remains on `/admin`; the customer interface reads it.
- Capture-domain decisions and suppression creation retain one authoritative admin write path.
- Missing capabilities are shown as unavailable rather than represented with invented data.
- A generated, validated sequence is Ready for Sending on its own. No approval is required to make it usable, and a decision row exists only where a person actually decided; the absence of one is not a queue.
- A decision is still recorded against one exact immutable version, and an edit still writes a new version.
- Approval does not send email. Nothing is sent automatically, and sending happens outside automatic execution.
- Failed, blocked and retrying Agent work is operational recovery, shown as status and diagnostics rather than as a customer task. The customer-facing rerun control is administrator-only; deeper recovery stays in the Admin Workbench.

## Not built

The current product does not provide:

- SalesHandy or another sending-provider adapter;
- delivery, reply, bounce or opt-out synchronization;
- sending, replies, provider-side sequencing or analytics backends;
- auto-send;
- deterministic fit/confidence scoring;
- Saved Audience criteria and snapshot generation;
- extension Campaign auto-add.

These are not hidden launch blockers for the current message-producing MVP. They remain explicit post-MVP work.

## Acceptance remaining

The implementation is assembled, but green CI is not operating acceptance.

1. Merge PR #233 after CI and local route checks.
2. Run one authorized real Contact end to end:
   - real website research;
   - real MillionVerifier decision;
   - real Claude CLI Insights and Personalization;
   - the Contact reaching Ready for Sending with no human action, and the generated messages visible in `/app/review`;
   - any optional edit, approve or discard recorded in audit history;
   - no sending side effect.
3. Run a controlled 10–20 Contact batch and verify:
   - worker claims and concurrency;
   - retries and failed/blocked state;
   - partial research outcomes;
   - provider/model spend boundaries;
   - usability of `/app` and `/admin` during execution.
4. Record an explicit pass, conditional pass or blocked verdict.

## Traceability

- Authoritative MVP epic: #202
- Campaign pipeline: PR #232
- Customer interface and Review: PR #233
- Shared Agent contract: #223
- Workbench controls: #221
- Research: #160 and #173
- Email Agent: #224
- Verification Agent: #225
- Insights: #212
- AI trust hardening: #181
- MVP acceptance and later pilot: #96
- Post-MVP sending: #174

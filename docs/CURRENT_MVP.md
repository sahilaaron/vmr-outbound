# Current MVP

**Status date:** 30 July 2026  
**Authoritative delivery:** PR #232 merged; PR #233 is the customer-interface merge gate.

## MVP outcome

The current VMR Outbound Agent MVP is complete as a product build when one operator can:

> capture an authorized LinkedIn or Sales Navigator prospect, enrol the permanent Contact into a Campaign, run the Contact through sourced Company research, exact-address email verification, evidence-backed Insights and Personalization, and approve or discard one exact immutable draft version.

The MVP ends at a trustworthy human-approved draft. It does **not** send email.

SalesHandy/provider submission, delivery events, replies, bounces, opt-outs, sequences and analytics are post-MVP work.

## Product surfaces

| Route | Role |
| --- | --- |
| `/` and `/app` | Customer-facing application and daily operating surface |
| `/app/review` | Exact-version draft review, approve/discard and evidence inspection |
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
→ Human review
```

`Sending` remains registered so the pipeline can extend without another domain redesign, but it has no production adapter and is disabled.

## Agent status

| Agent | Current implementation | Status |
| --- | --- | --- |
| Capture | Existing contact-first capture and promotion path | Operational |
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

A live MillionVerifier result requires:

- `FEATURES__MILLIONVERIFIER=true`;
- configured provider credentials;
- effective Verification Agent configuration containing `{"live": true}`.

Simulated evidence cannot complete a live Campaign stage.

## Current operating choices

- Contacts and Companies are permanent and Campaign-independent.
- Campaign Contact owns Campaign-specific execution state and draft output.
- Campaign enrolment is currently explicit and reversible through the Workbench, including bulk enrolment with refusal accounting.
- Knowledge Base editing remains on `/admin`; the customer interface reads it.
- Capture-domain decisions and suppression creation retain one authoritative admin write path.
- Missing capabilities are shown as unavailable rather than represented with invented data.
- A generated draft is not approved. Approval is recorded against one exact immutable `DraftVersion`.
- Approval does not send email.

## Not built

The current product does not provide:

- SalesHandy or another sending-provider adapter;
- delivery, reply, bounce or opt-out synchronization;
- sending, replies, sequences or analytics backends;
- auto-send;
- deterministic fit/confidence scoring;
- Saved Audience criteria and snapshot generation;
- extension Campaign auto-add;
- multi-email cadence generation;
- draft editing.

These are not hidden launch blockers for the current draft-producing MVP. They remain explicit post-MVP work.

## Acceptance remaining

The implementation is assembled, but green CI is not operating acceptance.

1. Merge PR #233 after CI and local route checks.
2. Run one authorized real Contact end to end:
   - real website research;
   - real MillionVerifier decision;
   - real Claude CLI Insights and Personalization;
   - exact draft visible in `/app/review`;
   - approve or discard recorded in audit history;
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

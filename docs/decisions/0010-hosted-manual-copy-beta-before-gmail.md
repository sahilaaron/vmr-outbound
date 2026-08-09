# Decision 0010 — Hosted manual-copy Beta before Gmail

Status: Accepted

Date: 2026-08-09

## Context

The merged application already contains the core operator workflow: Campaign/Contact navigation, Agent stages, the seven-message Personalization sequence, immutable editing and exact copy controls. The VPS staging foundation is also merged.

However, the hosted operator UI is still deliberately local-only and the Chrome Extension is only prepared for future authenticated remote capture. Gmail draft integration is architecturally planned but is not required to validate whether the actual product workflow is useful in real operation.

The immediate product question is therefore not whether VMR can create Gmail drafts. It is whether an internal operator can use the hosted application with real contacts from capture through generated outreach and manually act on those messages.

## Decision

The current delivery cycle prioritizes a **hosted manual-copy Beta** before Gmail integration.

The definition of done is:

```text
Sales Navigator / source page
→ Chrome Extension captures a real prospect over HTTPS
→ authenticated VMR staging application
→ Contact appears in the intended Campaign
→ Contact progresses through Agent stages
→ Research
→ Company Intelligence
→ Insights
→ Personalization
→ exactly seven messages
→ operator inspects each message
→ optional immutable edit
→ Copy Subject / Copy Body / Copy Full Email
→ operator manually performs outreach outside VMR
```

The operator must personally use and accept this workflow with real contacts before Gmail mailbox authorization or Gmail draft synchronization becomes a current delivery priority.

## Hosted UI consequence

The first VPS deployment may be an infrastructure-only smoke test, but that is not sufficient for operator acceptance.

The next application slice must make `/app` and `/admin` safely usable on staging through an authenticated internal-user/session boundary.

The existing localhost-era Workbench restriction must not simply be removed. Staging access is allowed only under the new authenticated remote-app boundary, including refusal of anonymous remote writes and appropriate CSRF protection for cookie-session writes.

## Chrome Extension consequence

The extension must move from pre-auth/local preparation to secure staging capture before the Beta can be accepted.

The extension authenticates only to VMR. It never receives Google identity credentials or Gmail mailbox tokens.

## Gmail consequence

Decision 0007 remains the architectural rule for future Google/Gmail work: Google identity and Gmail mailbox authorization are separate permission boundaries.

But Gmail is now sequenced **after** hosted manual-copy Beta acceptance.

The following are therefore deferred:

- Gmail mailbox authorization;
- operator-triggered Gmail draft creation;
- Gmail draft lineage/idempotency implementation;
- automatic cadence/follow-up creation;
- sent/reply/thread monitoring;
- automatic sending.

## Other deferred projections

Google Sheets synchronization and Campaign CSV/XLSX export remain deferred unless real operator use demonstrates a concrete need.

## Rationale

This sequencing gets the operator into the actual product sooner and validates the highest-value unknowns first: capture reliability, pipeline progression, generated-message usefulness, operator UI ergonomics, editing/version behavior and real-browser copy workflow.

Provider integrations should be added only after the underlying hosted workflow proves useful in real operation.

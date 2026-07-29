# MVP Architecture

## Product outcome

> **A user can capture 2,000 Sales Navigator contacts in the morning and begin sending AI-personalized verified emails that afternoon.**

The architecture exists to move permanent Contacts through one observable, controllable Contact-to-send pipeline.

## Operator-facing Agents

The frontend calls all backend workers **Agents**:

1. Capture Agent
2. Identity Agent
3. Company Agent
4. Research Agent
5. Email Agent
6. Verification Agent
7. Insights Agent
8. Personalization Agent
9. Sending Agent

The backend may implement these through queues, workers, services and scheduled jobs. The operator sees Agents, their states and their output.

## Core entities

### Contact

The permanent canonical person record. Capture never requires a Campaign.

### Company

The permanent canonical organization record. Domain and research results are shared across Contacts and Campaigns.

### Campaign

The operational context for outreach. It owns audience configuration, seller context, messaging, CTA, guardrails, templates, sending settings and Campaign-level Agent overrides.

### Campaign Contact

The many-to-many membership between Campaign and Contact. It owns Campaign-specific state:

- acceptance and exclusion reasons;
- fit and readiness decisions;
- personalized copy;
- immutable message versions;
- approval state;
- sending state and outcomes.

### Collection

A reusable grouping of Contacts. The Chrome extension presents Collections as Labels. Collections may also be referenced by Campaigns.

### Agent Job

One resumable unit of work with a stable identity, state, attempt history, error visibility and audit trail.

## Capture contract

```text
Operator capture
    ↓
Permanent Contact resolution
    ↓
Apply selected Labels / Collections
    ↓
Optional Campaign selected?
    ├─ No → capture completes without Campaign membership
    └─ Yes → create or update Campaign Contact membership
```

Campaign selection is an optional persistent shortcut. It does not alter Contact identity, ownership or canonical data.

## Pipeline

```text
Capture Agent
→ Identity Agent
→ Company Agent
→ Research Agent
→ Email Agent
→ Verification Agent
→ Insights Agent
→ Personalization Agent
→ Sending Agent
```

### Stage rules

- Identity work must converge repeated captures on the same Contact.
- Company resolution must reuse a resolved domain across Contacts sharing the same Sales Navigator company identity.
- Research output must retain provenance and remain separate from AI interpretation.
- Email discovery must stop after the first verified candidate and attempt no more than three formats.
- AI insight generation follows verification by default to avoid spending model work on Contacts without a verified address.
- Campaign-specific output is stored on Campaign Contact, not Contact.
- Sending remains blocked by suppression, eligibility and exact-version approval.

## Email policy

### More than 50 employees

1. `firstname.lastname`
2. `finitiallastname`
3. `lastnamefinitial`

### 50 or fewer employees

1. `firstname`
2. `firstname.lastname`
3. `finitiallastname`

The policy must be versioned and configurable behind a stable service boundary.

## Agent controls

Every Agent has a global default state and optional Campaign override.

Global states:

- enabled;
- paused;
- disabled.

Job states:

- waiting;
- running;
- paused;
- retrying;
- failed;
- completed;
- cancelled.

Campaign overrides may disable or pause an Agent for one Campaign without changing another Campaign.

## Workbench

The Workbench is the application control room. It must provide:

- Campaign stage counts;
- Agent health and current state;
- queue depth and throughput;
- recent failures and retry controls;
- global Agent controls;
- Campaign-level Agent overrides;
- record-level drill-down;
- emergency stop for new sending work.

## Idempotency and reuse

- Capture submissions use stable submission identities.
- Contact and Company resolution must be retry-safe.
- Campaign auto-add must upsert Campaign Contact membership.
- Company research is reusable by Company and version.
- Verification evidence is reusable under freshness policy.
- AI outputs are regenerable but versioned.
- Sending events use stable provider identifiers and duplicate-event rejection.

## Trust boundaries

- Operator instructions and approved seller Knowledge Base content are trusted configuration.
- LinkedIn text, website text and other third-party content are untrusted evidence.
- Sourced facts never become system instructions.
- AI output may not bypass deterministic eligibility, suppression or approval rules.

## MVP boundary

The MVP includes capture, optional Campaign auto-add, Collections, identity and Company resolution, research, email discovery, verification, insights, personalization, review, sending integration and Jobs monitoring.

Advanced analytics, autonomous replies, arbitrary workflow construction, omnichannel outreach and multi-tenant SaaS remain deferred.

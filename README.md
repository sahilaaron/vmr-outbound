# VMR Outbound Agent

VMR Outbound Agent is an internal, single-operator outbound operating system.

Its MVP has one defining outcome:

> **A user can capture 2,000 Sales Navigator contacts in the morning and begin sending AI-personalized verified emails that afternoon.**

Every active build must move a captured person closer to a sendable, campaign-specific email. Features that do not support that path are deferred.

## Canonical MVP workflow

```text
Capture permanent Contacts
→ optionally auto-add them to a Campaign through persistent extension selection
→ Contact and Company identity resolution
→ Company-domain resolution
→ Company research
→ Email discovery
→ Exact-address verification
→ AI company insights and outreach scoring
→ Campaign Contact acceptance
→ AI personalization inside campaign guardrails
→ Operator review
→ Email sending integration
```

The backend continues to use jobs and workers. The operator-facing application calls them **Agents**.

## Core product objects

- **Campaign** — owns audience criteria, seller context, messaging direction, CTA, guardrails, templates, sending configuration and per-campaign Agent controls.
- **Collection** — a reusable grouping applied to Contacts and Campaigns. The Chrome extension presents Collections as **Labels**.
- **Contact** — the permanent person record shared across campaigns.
- **Company** — the permanent organization record shared across contacts and campaigns.
- **Campaign Contact** — the campaign-specific membership record that owns fit, acceptance, generated copy, approval and send state.
- **Agent Job** — one resumable, inspectable unit of pipeline work.

A Contact may belong to many Collections and many Campaigns. Campaign-specific scores, messages, approvals and send outcomes must never be stored as permanent Contact facts.

## Capture model

Capture is always campaign-independent at the Contact level.

- Every successful capture creates or updates a permanent Contact.
- Campaign selection in the extension is optional.
- If a Campaign is selected, the system auto-adds the resolved Contact to that Campaign by creating or updating Campaign Contact membership.
- If no Campaign is selected, the Contact is still captured normally.
- Selecting a Campaign never changes identity resolution, Contact ownership or canonical data rules.

In other words, Campaign selection is a filing shortcut, not a prerequisite for capture.

## Campaign setup

A functional MVP Campaign supports:

- title and audience definition;
- employee-size, geography, industry, role and seniority criteria;
- seller Knowledge Base and company-profile context;
- messaging angle and CTA;
- AI guardrails;
- reusable and HTML-uploaded templates;
- operator preview, editing and personalization;
- email-account or sending-provider configuration;
- campaign-level Agent enablement and overrides;
- stage counts, exceptions and readiness state.

## Collections and extension Labels

Collections are first-class backend records. The extension calls them Labels because that is the clearest operator language.

The extension must:

1. search Campaigns and previously used Labels from the backend as the operator types;
2. allow multiple Labels;
3. persist the selected Campaign and Labels across every normal LinkedIn person capture and every Sales Navigator list capture;
4. keep them active until the operator deselects them;
5. attach selected Labels to every captured Contact without extra clicks;
6. when a Campaign is selected, auto-add each resolved Contact to that Campaign without changing the permanent Contact record;
7. continue to support normal capture when no Campaign is selected.

## Locked Agent order

1. **Capture Agent**
2. **Identity Agent**
3. **Company Agent**
4. **Research Agent**
5. **Email Agent**
6. **Verification Agent**
7. **Insights Agent**
8. **Personalization Agent**
9. **Sending Agent**

### Domain reuse

When one domain is resolved for a Sales Navigator company identity, every Contact with the same company identity must reuse that result and resolve toward the same permanent Company. Repeated captures must converge on existing Contacts and Companies rather than create duplicates.

### Email-discovery policy

Search at most three formats per Contact and stop after a verified address is found.

For companies with more than 50 employees:

1. `firstname.lastname`
2. `finitiallastname`
3. `lastnamefinitial`

For companies with 50 or fewer employees:

1. `firstname`
2. `firstname.lastname`
3. `finitiallastname`

Email discovery runs after company research and before AI insight generation. Paid AI work must not run for a Contact that has no verified email unless an operator explicitly overrides the Campaign.

## Workbench

The Workbench is the operating home of the application. It must show:

- Campaign progress by pipeline stage;
- queue depth and current throughput;
- running, paused, waiting, retrying, failed and completed Agent jobs;
- failure reasons and retry controls;
- global Agent on/off controls;
- Campaign-specific Agent overrides;
- drill-down from every stage count to the affected records;
- an immediate emergency stop for new sending work.

Global Agent settings define defaults. Campaign settings may disable or override an Agent without changing another Campaign.

## MVP boundary

The first usable model is complete when one operator can:

1. configure a Campaign;
2. capture between 100 and 2,000 Sales Navigator contacts, with optional persistent Campaign auto-add and persistent Labels;
3. see the captures converge into permanent Contacts and Companies;
4. run domain resolution, research, email discovery and verification automatically;
5. generate company insights, outreach scores and campaign-specific personalized email copy;
6. review ready contacts and messages;
7. hand approved records to an email sending service.

The MVP does not require advanced analytics, autonomous LinkedIn navigation, CRM replacement, omnichannel outreach, automatic replies, a general workflow builder or multi-tenant SaaS.

## Current foundation

The repository already contains substantial parts of the foundation:

- operator-controlled LinkedIn and Sales Navigator capture;
- immutable capture evidence;
- permanent Contact and Company records;
- LinkedIn identity resolution;
- company-domain lookup and decision history;
- provenance, notes, labels and suppression;
- seller-side Knowledge Base records;
- deterministic email candidate generation;
- MillionVerifier-backed exact-address verification;
- company evidence and insight models.

The immediate work is to connect these capabilities into the canonical Contact-to-send pipeline rather than continue building them as isolated features.

## Operating principles

- **One person, one Contact.** Campaign membership never duplicates the permanent person.
- **Capture never requires a Campaign.** Campaign selection only auto-adds a Contact after capture.
- **One company, reusable research.** Domain and research work should be reused across Contacts and Campaigns.
- **Campaign-specific output stays campaign-specific.** Scores, messages, approvals and send state belong to Campaign Contact.
- **Evidence before interpretation.** Raw captures and sourced research remain separate from AI-derived insights.
- **No fabricated certainty.** Missing and ambiguous values remain unresolved or reviewable.
- **Safe retries.** Jobs are resumable and idempotent.
- **Visible control.** Every Agent can be observed, paused and disabled.
- **Operator-controlled sending.** Suppression and approval rules remain authoritative.

## Technology

- Python 3.11+
- FastAPI
- SQLAlchemy 2
- PostgreSQL
- Alembic
- Pydantic Settings
- Pytest, Ruff and mypy
- Manifest V3 Chrome extension using plain JavaScript

## Quick start

### Windows

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env
alembic upgrade head
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

### macOS or Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
alembic upgrade head
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

See `docs/GOAL.md` for the authorized MVP, `docs/ARCHITECTURE.md` for the pipeline and data boundaries, and `docs/PROJECT_TRACKING.md` for tracker ownership and reporting rules.

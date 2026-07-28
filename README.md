# VMR Outbound Agent

VMR Outbound Agent is an internal, single-operator outbound workflow application.
It combines operator-controlled LinkedIn acquisition, permanent Contact and
Company records, sourced company intelligence, deterministic qualification, email
readiness, campaign context, and AI-assisted cadence generation.

The product is deliberately evidence-first: captured facts, identity claims,
company-domain decisions, research sources, qualification reasons, and generated
content remain distinguishable and inspectable.

## Current state

The acquisition foundation is operational on `main`:

- VM Prospector, a Chrome side-panel extension for operator-opened LinkedIn and
  Sales Navigator pages;
- campaign-independent capture intake with immutable snapshots;
- permanent Contact and Company records;
- company-domain lookup, decision history, and capture promotion;
- cross-surface LinkedIn identity resolution using both public vanity URLs and
  opaque Sales Navigator member IDs;
- provenance, labels, notes, suppression, audit events, and retry-safe workflows;
- seller-side Knowledge Base records for company profile, offerings, personas,
  proof points, restricted claims, and campaign-offering links;
- deterministic email candidate generation and MillionVerifier-backed exact
  address verification behind feature flags.

The authenticated operator path has been exercised end to end: Sales Navigator
and normal LinkedIn captures can converge on one canonical Contact, domain
resolution can create or reuse the Company, and retries remain idempotent.

Two focused acquisition corrections remain active before the intake workflow is
considered friction-complete:

- restore a visible, explicitly derived LinkedIn resolving alias for Sales
  Navigator rows without treating it as canonical identity;
- remove unnecessary Confirm and Promote clicks when one deterministic domain
  result is safe enough for the practical policy.

## MVP

The MVP is one complete intelligent vertical slice:

```text
Capture a real contact
→ resolve Contact and Company identity
→ gather sourced company facts
→ synthesize AI insights
→ calculate an explainable fit decision
→ select an approved audience
→ find and verify the contact email
→ configure campaign context and cadence
→ generate a personalized multi-email sequence
```

The MVP ends with a generated cadence that is ready for operator review and
external delivery. It does not require the application itself to send email.

### MVP product layers

**Authoritative data**

- immutable LinkedIn and Sales Navigator capture evidence;
- permanent Contact and Company records;
- identity links and company-domain decisions;
- sourced company facts with URLs, timestamps, warnings, and freshness;
- suppression and verification outcomes.

**Derived decisions**

- AI-generated company insights;
- deterministic Initial Fit scoring and eligibility;
- audience membership;
- email readiness;
- personalized cadence content.

Derived output may be regenerated, but it must not silently overwrite sourced
facts or raw capture evidence.

## v1 definition

v1 is complete when a single operator can reliably perform the following flow in
the application:

1. Capture a prospect from LinkedIn or Sales Navigator.
2. Resolve the prospect to one permanent Contact and the correct Company.
3. Inspect sourced company facts and their provenance.
4. Review AI-synthesized business insights separately from the facts.
5. See an explainable qualification result and audience decision.
6. Find and verify an email only when the required gates permit it.
7. Configure a campaign using seller Knowledge Base context.
8. Generate and inspect a personalized multi-email cadence.
9. Retry any completed step without duplicating Contacts, Companies, identity
   links, research records, verification work, or generated artifacts.

The v1 boundary ends before delivery-provider execution. No unattended LinkedIn
navigation, autonomous outreach, or automatic sending is enabled.

## Core operating principles

- **Operator-controlled acquisition.** The extension reads only pages the
  operator has already opened and saves only after an explicit action.
- **One person, one Contact.** Opaque Sales Navigator member IDs and observed
  public LinkedIn URLs remain separate identity claims that can resolve to the
  same Contact.
- **No fabricated certainty.** Missing or ambiguous values remain blank, warned,
  provisional, unresolved, or held for review.
- **Permanent records, reusable audiences.** Contacts are not owned or duplicated
  by campaigns.
- **Evidence before interpretation.** Sourced facts and AI-generated insights are
  stored and displayed as different layers.
- **Deterministic gates.** Suppression, qualification, email readiness, and
  campaign eligibility remain explainable and auditable.
- **Idempotent execution.** Stable submission and job identities make retries safe.
- **Features default off.** Provider calls and higher-level workflow stages are
  activated explicitly through configuration.

## Technology

- Python 3.11+
- FastAPI
- SQLAlchemy 2
- PostgreSQL
- Alembic
- Pydantic Settings
- Pytest, Ruff, and mypy
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

Create a UTF-8 PostgreSQL database named `vmr_dev` or configure another approved
local development target in `.env`. See `docs/DEVELOPMENT.md` for database setup,
feature flags, validation commands, and migration checks.

## Chrome extension

Load the unpacked extension from:

```text
extensions/salesnav-capture
```

In `chrome://extensions`:

1. Enable **Developer mode**.
2. Choose **Load unpacked**.
3. Select the directory above.
4. Set the extension target to the local backend:
   `http://127.0.0.1:8000`.

VM Prospector supports operator-opened:

- LinkedIn person profiles;
- LinkedIn company pages;
- Sales Navigator people-search result pages.

It does not paginate, navigate profiles automatically, store LinkedIn
credentials, or bypass access controls.

## Repository layout

```text
app/
  core/          settings and feature switches
  db/            SQLAlchemy base and session management
  models/        persistent domain models
  services/      capture, identity, research, verification, and workflow logic
  main.py        FastAPI application
extensions/
  salesnav-capture/  VM Prospector Chrome extension
migrations/      Alembic migrations
tests/           PostgreSQL-backed backend test suite
docs/            contracts, runbooks, ADRs, acceptance records, and engineering rules
.github/         CI configuration
```

## Validation

Before a change is accepted, the relevant suite should include:

```bash
pytest
ruff check .
ruff format --check .
mypy app
alembic heads
alembic check
```

Extension changes also run the complete Node test suite from
`extensions/salesnav-capture` on an LF checkout.

## Safety and data handling

- Secrets belong in local environment configuration, never source control.
- Schema changes use Alembic migrations and must retain one migration head.
- Raw captures and decision history are preserved rather than destructively
  rewritten.
- Suppression remains authoritative over qualification, drafting, and sending.
- No feature may claim a value was observed when it was derived or inferred.
- No email is scheduled or sent by the current v1 application boundary.

See `docs/AGENTS.md` for engineering constraints and `docs/CLAUDE.md` for the AI
implementation boundary.
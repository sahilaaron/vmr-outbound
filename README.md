# VMR Outbound Agent

A local-first, semi-autonomous outbound operating system for building one safe,
measurable 100-contact campaign.

The product combines deterministic workflow software, narrowly scoped AI
judgment, explicit evidence, and human approval. The only intended manual input
at the acquisition edge is an operator-authorized lead or batch; downstream
work should become increasingly automated without hiding uncertainty or
bypassing approval.

This is **not** an autonomous sending bot, a general agent platform, or an
unattended LinkedIn scraper.

## Current objective

Deliver a complete controlled path from an authorized contact batch to
Saleshandy scheduling while preserving:

- source provenance and field history;
- conservative identity resolution and deduplication;
- suppression and hard eligibility gates;
- exact-address email verification;
- explainable scoring and research evidence;
- immutable draft versions and exact-version approval;
- idempotent provider synchronization and audit history.

The launch target is one reviewed 100-contact pilot. Scaling to 250, 500, and
then higher monthly volume comes only after that pilot is proven.

See:

- `docs/GOAL.md` — launch journey, acceptance criteria, scope, and build order;
- `docs/AGENTS.md` — engineering, review, and safety rules;
- `docs/CLAUDE.md` — AI build boundary;
- `docs/PROJECT_TRACKING.md` — operational evidence and go-live tracking.

## Project status

### Proven on `main`

The repository currently contains:

- a FastAPI application with PostgreSQL, SQLAlchemy, Alembic, strict typing,
  audit events, feature switches, CI, and dry-run-safe defaults;
- a local server-rendered operator workbench for campaigns, staged imports,
  contacts, ambiguity review, provenance, suppression, and local tools;
- authorized CSV and XLSX staging with sheet selection, mapping, non-committing
  preview, explicit confirmation, row-level outcomes, and idempotent retry;
- conservative contact and company normalization, deduplication, identity
  resolution, immutable raw-row evidence, and merge auditing;
- field-level provenance with a versioned freshness policy and manual-override
  precedence;
- an append-only suppression ledger enforced at every implemented advancement
  path;
- an operator-driven Manifest V3 Sales Navigator capture extension that reads
  visible people-search results only after an explicit action;
- a local intake endpoint and workbench handoff for captured Sales Navigator
  batches;
- operator-confirmed company-domain enrichment through logo.dev behind a
  disabled-by-default feature switch;
- deterministic email candidate generation, exact-address verification cache,
  a retry-safe PostgreSQL queue, provider-neutral usage ledger, and truthful
  Pending / Successful / Failure / Warning states;
- a live MillionVerifier smoke path with explicit simulated-versus-live
  provenance. A real mailbox verification has been successfully exercised
  end to end.

### Built and under review

DAT-012 is staged as five stacked pull requests. It extends the operator-driven
Chrome extension from Sales Navigator result lists to manually opened LinkedIn
person and company pages, adds immutable capture snapshots, exact normalized
profile-URL refresh, versioned employment QA recommendations, and acceptance
runbooks.

This work is **not yet part of `main`**. The stack must be reviewed and merged in
order, and the authenticated operator acceptance pass must be recorded before
the epic is considered complete.

### In local development

CMP-001 campaign settings has been implemented and committed locally. CMP-003
campaign-contact membership and outreach-history integrity is the next job on
that branch. Neither should be treated as shipped until its branch is pushed,
reviewed, CI-verified, and merged.

### Not built yet

The launch path still requires major downstream capabilities, including:

- complete campaign lifecycle and contact-stage orchestration;
- historical marketing-data import;
- deterministic hard gates and Initial Fit Score;
- company/contact research with stored source evidence;
- Outreach Readiness Score;
- structured AI submission and validation boundaries;
- immutable personalized draft generation and mobile approval;
- Saleshandy scheduling, webhook ingestion, delivery/reply synchronization;
- end-to-end synthetic dry run, security review, and the 100-contact pilot.

There is currently **no production sending path enabled**.

## Safety model

- Feature switches default off.
- Dry-run remains the safe default wherever execution exists.
- LinkedIn capture is operator-controlled: no automatic navigation, pagination,
  CAPTCHA handling, anti-bot evasion, or background scraping.
- Browser capture stages observations; it does not directly mutate canonical
  records.
- Weak identity evidence is review-only and never silently merges contacts.
- Suppression, invalid verification, and ineligibility must block progression.
- Catch-all, unknown, simulated, cached, and live verification evidence remain
  visibly distinct.
- Secrets never belong in source, fixtures, logs, errors, audit payloads, or
  screenshots.
- Schema changes use reversible Alembic migrations.
- Material automated actions require durable audit evidence.
- No merge is considered complete until CI passes, ChatGPT independently
  reviews the PR, and Sahil explicitly approves the merge.

## Local development

The active development database is local PostgreSQL. Development RDS is not a
current prerequisite.

### Windows CMD

```cmd
py -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env
python scripts\dev_up.py
uvicorn app.main:app --reload --port 8000
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
python scripts/dev_up.py
uvicorn app.main:app --reload --port 8000
```

An optional loopback-only PostgreSQL 16 container is available through
`docker-compose.yml`. Full database, encoding, feature-switch, extension, and
smoke-test instructions are in `docs/DEVELOPMENT.md`.

Useful local checks:

```bash
python scripts/smoke.py
ruff check .
ruff format --check .
python -m mypy app
alembic upgrade head
alembic check
alembic downgrade -1
alembic upgrade head
python -m pytest
```

The Chrome extension has its own Node test suite:

```bash
cd extensions/salesnav-capture
node --test
```

## Repository layout

```text
app/
  api/          FastAPI routes
  core/         typed settings and feature switches
  db/           SQLAlchemy base and session management
  models/       PostgreSQL-backed domain models
  services/     imports, identity, provenance, suppression, verification, etc.
  templates/    server-rendered operator workbench
extensions/
  salesnav-capture/   operator-driven Chrome extension and contracts
migrations/           reversible Alembic revisions
tests/                PostgreSQL-backed Python test suite
scripts/              local bootstrap, smoke, and controlled operator commands
docs/                 goals, operating rules, phase docs, contracts, runbooks
.github/workflows/     CI
```

## Operating model

- Claude builds locally: code, tests, migrations, commits, correction commits,
  and factual handoffs.
- Sahil makes material product decisions and provides the local-to-GitHub push
  bridge when an AI session cannot authenticate.
- Once a branch is remote, ChatGPT owns PR creation, independent review, issue
  and project administration, and merge execution after Sahil's explicit
  approval.
- GitHub is the engineering source of truth. The Google Sheets tracker is the
  operational answer to: **When can we go live?**

## Go-live condition

The project is not launch-ready merely because individual phases compile or
pass tests. Go-live requires a complete vertical slice, a successful synthetic
dry run, an authorized 100-contact pilot, visible exception handling, exact
human approval before scheduling, provider synchronization, independent review,
and reconciled operational evidence.

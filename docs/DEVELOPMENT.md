# Development setup (FND-003 / FND-004 / FND-005)

Exact steps to start the project on a clean machine, run the checks, and apply
migrations. Phase 0 targets **local development only** — no production/RDS
credentials are used or stored here.

> Optional developer convenience: sections 2–4 (env file, database, migrations)
> can be run in one step with `python scripts/dev_up.py`, and
> `docker compose up -d db` provides a throwaway local UTF-8 Postgres that matches
> the default `DATABASE_URL`. `python scripts/smoke.py` checks a running instance.
> These scripts automate the manual steps documented below — they don't replace
> them; this file remains the reference.

## Prerequisites

- Python 3.11+
- PostgreSQL 16 (local). Any reachable Postgres works; the default URL assumes a
  local instance on port 5433, database `vmr_dev`, user `dev`.

> Encoding matters: the application database must be **UTF-8**. A cluster
> initialized in a `C`/`SQL_ASCII` locale will cause the driver to return text
> as bytes and break SQLAlchemy. Create the database with
> `ENCODING 'UTF8' TEMPLATE template0` (see below) or run `initdb -E UTF8`.

## 1. Clone and create a virtual environment

```bash
git clone https://github.com/sahilaaron/vmr-outbound.git
cd vmr-outbound
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -e ".[dev]"
```

## 2. Configure environment

```bash
cp .env.example .env
# Edit .env only if your local Postgres differs from the default URL.
```

The default `DATABASE_URL` is
`postgresql+psycopg://dev@127.0.0.1:5433/vmr_dev`. `DRY_RUN` defaults to `true`.

## 3. Create the local database (UTF-8)

If you do not already have a `vmr_dev` database:

```bash
# Example against an existing local Postgres superuser/role:
createdb -h 127.0.0.1 -p 5433 -U dev -E UTF8 -T template0 vmr_dev
# or in psql:
#   CREATE DATABASE vmr_dev ENCODING 'UTF8' TEMPLATE template0;
```

Or start a throwaway local Postgres that matches the default URL and let the
bootstrap script create the database (steps 3 and 4 together):

```bash
docker compose up -d db      # optional local Postgres on 127.0.0.1:5433 (UTF-8)
python scripts/dev_up.py     # create the DB if missing, then apply + verify migrations
```

## 4. Apply migrations

```bash
alembic upgrade head
```

Verify migrations match the models (no un-generated changes):

```bash
alembic check
```

To confirm reversibility during development:

```bash
alembic downgrade base && alembic upgrade head
```

## 5. Run the app

```bash
uvicorn app.main:app --reload --port 8000
# Liveness:  curl http://127.0.0.1:8000/health
# Readiness: curl http://127.0.0.1:8000/ready   (checks the database)
# Or:        python scripts/smoke.py    (health + readiness + which features are on)
```

To run the local operator workbench (server-rendered UI at `/`), enable its
switches (they default off):

```bash
FEATURES__WORKBENCH=true FEATURES__CSV_IMPORT=true FEATURES__SALESNAV_INTAKE=true \
  uvicorn app.main:app --reload --port 8000
```

`FEATURES__SALESNAV_INTAKE=true` also enables the local Sales Navigator capture
intake endpoint and campaign selector (DAT-009 / UI-010). On Windows, set these
in `.env` instead of inline and run `uvicorn app.main:app --reload --port 8000`.

See `docs/WORKBENCH.md` for the pages, the CSV/XLSX preview -> confirm import
flow, and the local-only reset safety rules.

To run the Phase 2 email-intelligence + verification path locally, also enable
its two switches (both default off):

```bash
FEATURES__WORKBENCH=true FEATURES__EMAIL_GENERATION=true \
  FEATURES__MILLIONVERIFIER=true uvicorn app.main:app --reload --port 8000
```

Without a real key, verification runs a deterministic, network-free simulator.
The exact API-key step and the single manual live smoke test are in
`docs/VERIFICATION_RUNBOOK.md`. A synthetic end-to-end demo is available with
`python scripts/phase2_verification_demo.py` (local only).

Run one durable Agent job with the shared worker:

```bash
python scripts/run_agent_worker.py --once
```

For a continuous local worker restricted to the Phase 2 vertical adapters:

```bash
python scripts/run_agent_worker.py --agent identity --agent company
```

See `docs/PHASE_2_EXECUTION_MODEL.md` for queue leases, controls, retry
semantics, and pipeline-state inspection.

## 6. Run the checks (same as CI)

```bash
ruff check .
ruff format --check .
python -m mypy app
alembic upgrade head
alembic check
alembic downgrade base && alembic upgrade head
python -m pytest
```

CI runs exactly these steps, in this order, against a Postgres 16 service — see
`.github/workflows/ci.yml`.

Two further checks are **local-only** — CI does not run them, and they are what
catch parallel threads breaking each other:

```bash
alembic heads                                              # exactly one head
cd extensions/salesnav-capture && npm install && npm test  # if extension code changed
```

Two migration heads mean two threads created sibling migrations; restack before
anything is published.

This file holds the runnable commands; `docs/PARALLEL_INTEGRATION.md` holds the
rule about when they count. Passing here is not the same as being final: on a
stacked or parallel-built branch the sequence must pass on the **final assembled
head**, not on a thread's own commits in isolation. If the environment cannot
run Postgres, Ruff, or mypy, report `Integration incomplete; do not publish yet`
rather than relying on CI to find the problems.

`.github/workflows/ci.yml`, the block above, and the gate section of
`docs/PARALLEL_INTEGRATION.md` change together, in one commit. A stale copy is
worse than no copy, because someone will run it and believe the result.

## 6a. Worktrees for parallel work

When more than one thread is building, give each its own worktree and keep one
persistent worktree for integration. The integration worktree must hold no
unrelated local edits.

```bash
git worktree add -b <branch> ../vmr-outbound-<domain> <base-sha>
git worktree add -b <integration-branch> ../vmr-outbound-integration <base-sha>
git worktree list
```

Omit `-b` and the worktree lands on a detached HEAD — fine for reading, wrong
for committing. When a worktree is finished, delete the directory and then run
`git worktree prune`; a stale registration blocks the next `worktree add` at the
same path.

```
vmr-outbound/                      operator clone
vmr-outbound-<domain-a>/           implementation worktree
vmr-outbound-<domain-b>/           implementation worktree
vmr-outbound-integration/          authoritative integration worktree
```

Before ending a session, the work must survive as a pushed branch or a verified
bundle:

```bash
git bundle create ../<artifact>.bundle <base-sha>..<branch>
git bundle verify ../<artifact>.bundle
git bundle list-heads ../<artifact>.bundle
```

The bundle's SHA-256, on Windows CMD:

```bat
certutil -hashfile ..\<artifact>.bundle SHA256
```

## 7. Run the contact-capture extension against the local backend (DAT-013)

Windows (CMD), from the repository root. The backend must be local; the intake
endpoint refuses any other environment.

```bat
:: 1. Backend dependencies and database (once)
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
psql -U postgres -c "CREATE DATABASE vmr_dev ENCODING 'UTF8' TEMPLATE template0;"
alembic upgrade head

:: 2. Run the checks
ruff check .
ruff format --check .
mypy app
python -m pytest

:: 3. Start the backend with the capture switches on
set APP_ENV=local
set FEATURES__CONTACT_CAPTURE_INTAKE=true
set FEATURES__WORKBENCH=true
set FEATURES__SUPPRESSIONS=true
set OPERATOR_BASE_URL=http://127.0.0.1:8000
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

:: 4. Extension tests (a second terminal)
cd extensions\salesnav-capture
npm install
npm test

:: 5. Sanitized live acceptance (a second terminal, backend running)
python scripts\contact_capture_acceptance.py --base-url http://127.0.0.1:8000
```

Load the extension:

1. Open `chrome://extensions` and turn on **Developer mode**.
2. **Load unpacked** → select `extensions\salesnav-capture`.
3. Pin it and click the icon to open the side panel.
4. In the panel's **Settings**, set *Local backend base URL* to
   `http://127.0.0.1:8000` and *Save target* to **Local VMR backend**, then
   *Save settings*. (Leave the target on **Mock receiver** and run
   `npm run mock-receiver` to exercise the flow with no backend at all.)
5. The first save prompts for loopback access — approve it. Nothing is
   transmitted before that prompt, and declining blocks the save with a Retry.

Capture and inspect:

- **A profile** — open `linkedin.com/in/<id>` yourself → *Read this profile
  page* → review → optionally add labels and a note → *Save Contact* (or
  *Refresh Contact*).
- **Sales Navigator contacts** — open a people-search results page yourself →
  *Capture visible contacts* → exclude any rows → *Save N included contacts*.
- **Inspect the result** — follow *Open contact* / *Open capture record* in the
  panel, or browse `http://127.0.0.1:8000/contact-captures/<capture_id>` and
  `http://127.0.0.1:8000/contact-captures/submissions/<submission_id>`.

## 8. Resolve and promote a capture (DAT-014)

A captured person becomes a canonical contact only after their company domain is
resolved. Add these to the backend environment from step 7:

```bat
set FEATURES__CONTACT_CAPTURE_PROMOTION=true
:: Only needed to call the provider. Without it you can still enter a domain by
:: hand or leave a capture pending — a domain is never invented.
set FEATURES__SALESNAV_DOMAIN_ENRICHMENT=true
set LOGO_DEV_API_KEY=your-local-key
```

Then, in the workbench:

1. Open `http://127.0.0.1:8000/contact-captures/pending`.
2. Open a capture to see the person, title, captured company, LinkedIn company
   hint, labels, note, identity warnings and company-resolution status.
3. *Run domain lookup* → review the ranked candidates (logo.dev returns no
   confidence score, and the page says so rather than inventing one).
4. *Confirm* the right candidate, *Reject* a wrong one with a reason, enter a
   domain by hand, or *Leave unresolved*.
5. *Promote to contact* → open the resulting Contact and Company from the card.

Sanitized acceptance with the provider stubbed on loopback (no API key needed):

```bat
set LOGO_DEV_SEARCH_URL=http://127.0.0.1:8788/search
set LOGO_DEV_API_KEY=local-stub-key-not-real
:: restart the backend so it picks the stub URL up, then:
python scripts\capture_promotion_acceptance.py --base-url http://127.0.0.1:8000
```

## Notes

- **Secrets**: never commit `.env` or any key. `.env.example` documents variable
  names only. Provider keys (MillionVerifier, Saleshandy) are added to a secret
  manager when their phase is built, not to source.
- **Feature switches**: every pipeline capability is off by default. Enable one
  locally with e.g. `FEATURES__CSV_IMPORT=true` in `.env`; unfinished features
  stay disabled until their phase is built.
- **Dry-run**: `DRY_RUN=true` is the default and the safe state. It must only be
  turned off deliberately, in an environment authorized to schedule real email.
- **Migrations own the schema**: tests create tables via SQLAlchemy for
  convenience, but the authoritative schema is the Alembic migration set.

# Development setup (FND-003 / FND-004 / FND-005)

Exact steps to start the project on a clean machine, run the checks, and apply
migrations. Phase 0 targets **local development only** — no production/RDS
credentials are used or stored here.

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
```

To run the local operator workbench (server-rendered UI at `/`), enable its
switches (they default off):

```bash
FEATURES__WORKBENCH=true FEATURES__CSV_IMPORT=true \
  uvicorn app.main:app --reload --port 8000
```

See `docs/WORKBENCH.md` for the pages, the CSV/XLSX preview -> confirm import
flow, and the local-only reset safety rules.

## 6. Run the checks (same as CI)

```bash
ruff check .
ruff format --check .
python -m mypy app
alembic upgrade head
alembic check
python -m pytest
```

CI runs exactly these steps against a Postgres 16 service — see
`.github/workflows/ci.yml`.

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

# Development setup (FND-003 / FND-004 / FND-005 / FND-009)

Exact steps to start the project on a clean machine, run the checks, and apply
migrations. Sections 1–6 cover the default **local** database mode; section 7
covers the deliberate **development-RDS** mode (FND-009). No RDS credential is
ever used or stored in this repository.

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

## 7. Using the development RDS database (FND-009)

The application supports two explicit database modes, selected by
`DATABASE_TARGET` in your local `.env`:

| Mode | Meaning | Rules (enforced fail-closed) |
| --- | --- | --- |
| `local` (default) | loopback development Postgres | host must be `127.0.0.1` / `localhost` / `::1` |
| `rds-dev` | the development RDS instance | `DATABASE_URL` must be supplied explicitly, point at a non-loopback host, and carry `sslmode=require`/`verify-ca`/`verify-full`; TLS is re-verified on every live connection |

Local-only operations — `scripts/dev_up.py`, the workbench reset/fixture
tools, the pytest suite, `alembic downgrade`, and database creation — refuse
any non-loopback host under every flag combination. The only supported way to
run migrations against RDS is the operator command below.

### Connect

Set, in your local `.env` only (never committed, never pasted into GitHub,
chat, logs, or screenshots):

```
DATABASE_TARGET=rds-dev
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@ENDPOINT:5432/DBNAME?sslmode=verify-full&sslrootcert=/path/to/rds-global-bundle.pem
```

`sslmode=verify-full` with the [AWS RDS global certificate bundle](https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem)
is preferred (verifies the server identity). `sslmode=require` is the accepted
minimum; `prefer` and weaker are refused. Connection details printed by any
tool are masked (`[masked-user]@[masked-host]`); errors print exception class
names only.

Pooling is conservative by default (pool 5 + overflow 5, pre-ping, 30-minute
recycle, 10 s connect timeout, 30 s statement / 5 s lock / 60 s
idle-in-transaction server-side timeouts). Override via the `DB_*` variables in
`.env.example` only with a reason.

### Migrate and check

```bash
python scripts/rds_migrate.py status    # read-only: server, encoding, TZ, TLS, schema head, drift
python scripts/rds_migrate.py upgrade   # apply alembic upgrade head (typed confirmation) + check
python scripts/rds_migrate.py prove     # readiness proof: capability checks + write/read/cleanup
                                        # via a temporary table — persists nothing
```

Calling `alembic` directly against a non-loopback host is refused
(`migrations/env.py` requires the one-shot token only `rds_migrate.py` sets),
and `alembic downgrade` against a non-loopback host is refused unconditionally
— recovering RDS uses backup/restore, not downgrade.

### Backup and restore

Run from your machine with the same `.env` values (never store dumps in the
repository; `PG*` variables keep the credential out of the command line and
shell history):

```bash
# Logical backup of the development database (custom format, compressed):
PGSSLMODE=verify-full PGSSLROOTCERT=/path/to/rds-global-bundle.pem \
  pg_dump -h ENDPOINT -p 5432 -U USER -d DBNAME -Fc -f vmr_dev_$(date +%Y%m%d).dump

# Restore into an (empty) database:
PGSSLMODE=verify-full PGSSLROOTCERT=/path/to/rds-global-bundle.pem \
  pg_restore -h ENDPOINT -p 5432 -U USER -d DBNAME --no-owner --clean --if-exists vmr_dev_YYYYMMDD.dump
```

`pg_dump`/`pg_restore` prompt for the password (or read `PGPASSWORD` from the
environment for one command — do not export it globally). RDS automated
snapshots remain the primary recovery mechanism and are managed in AWS by the
operator; nothing in this repository changes AWS-side settings.

### Rotate credentials

1. In the AWS console (operator action), set a new password for the database
   user — or create a new user, grant it the same privileges, and plan to drop
   the old one.
2. Update `DATABASE_URL` in your local `.env` with the new value.
3. Verify: `python scripts/rds_migrate.py status` (connects with the new
   credential; output stays masked).
4. If a separate user was created, drop the old user in AWS after the check
   passes. The old password stops working immediately either way.

Nothing else needs to change: the credential exists only in the local `.env`.

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

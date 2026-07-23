# Phase 0 — Foundation: evidence map

How each in-scope Phase 0 backlog card (EPIC 01, `GITHUB_BACKLOG.md`) is
satisfied, and what is deliberately deferred.

| Card | Outcome | Evidence in this repo |
| --- | --- | --- |
| **FND-001** Decide the first-release technical stack | Recorded with reasons | `docs/decisions/0001-tech-stack.md` |
| **FND-003** Repository structure and local setup | Clear homes for app/services/models/migrations/tests/docs; a new agent can start from written steps | `app/`, `migrations/`, `tests/`, `docs/DEVELOPMENT.md`, `README.md` |
| **FND-004** Configuration and secret handling | Typed settings from env; secrets excluded from source/logs/fixtures | `app/core/config.py`, `.env.example`, `.gitignore` |
| **FND-005** Automated checks and database migrations | ruff + mypy + pytest + migration validation run before merge; schema changes repeatable | `.github/workflows/ci.yml`, `pyproject.toml`, `alembic.ini`, `migrations/` |
| **FND-007** Audit records, feature switches, dry-run | `audit_events` model + recording service; all features default off; dry-run defaults on | `app/models/audit_event.py`, `app/services/audit.py`, `app/core/features.py`, `app/core/config.py` |
| **FND-008** Authorized contact-input contract | Accepted columns + provenance documented; no unattended scraping | `docs/contact_input_contract.md` |

## Verification performed

- `ruff check .` and `ruff format --check .` — clean.
- `python -m mypy app` (strict) — no issues.
- `alembic upgrade head` then `alembic check` — migration applies and matches the
  models (no un-generated drift); `alembic downgrade base` proves reversibility.
- `python -m pytest` — 12 tests pass (config defaults/overrides, feature switches,
  audit persistence + dry-run stamping, app `/health` and `/ready`).
- `uvicorn app.main:app` — the shell boots and serves `/health` and `/ready`.

All migration work was proven on a **local** PostgreSQL 16 instance. No RDS or
production credentials exist in the repository; production variable names are
documented in `.env.example` and `docs/decisions/0001-tech-stack.md`.

## Deferred within Phase 0 (decisions required)

- **FND-002** Hosting model — where the dashboard/backend run and how the phone
  reaches them. Requires Sahil + IT. Not needed to build Phases 1–6 locally;
  needed before OPS-004 and the pilot.
- **FND-006** Private single-operator access — dashboard protection. Tied to the
  hosting decision; local dev needs no auth.

These are recorded in `docs/decisions/0001-tech-stack.md` and surfaced in the
`00 — Foundation` tracker tab as decisions required.

## Explicitly out of scope (later phases)

Imports, normalization, deduplication, suppressions, email generation,
verification, scoring, insights, the Claude MCP bridge, drafting, Saleshandy,
the dashboard build-out, and deployment. Feature switches for these exist and are
off; no behaviour is implemented.

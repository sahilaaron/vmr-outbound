# VMR Outbound Agent

A semi-automated outbound sales operating system: deterministic software,
selective AI judgment, and explicit human approval before any outreach. The
immediate objective is one safe, measurable 100-contact campaign — not a fully
autonomous platform.

See `docs/GOAL.md` for the current milestone and acceptance criteria,
`docs/AGENTS.md` for engineering and safety rules, and `docs/CLAUDE.md` for the
AI collaboration boundary.

## Status

Phase 1 — Data & Campaigns (in progress). On top of the Phase 0 foundation
(typed configuration, audit model, feature switches all off, dry-run on by
default, Alembic migrations, CI), the repository now provides the core DAT-001
schema, staged CSV/XLSX import with the local operator workbench
(preview-then-confirm, ambiguous-row outcomes), the suppression ledger, and the
operator-driven Sales Navigator capture extension. Operator identity resolution
(DAT-004) is built and under review. No verification, scoring, research,
drafting, or sending behaviour exists; those feature switches remain off, and
no outreach capability is enabled.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# create a UTF-8 vmr_dev database (see docs/DEVELOPMENT.md)
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

Full instructions, including the database encoding requirement and the exact
check commands, are in `docs/DEVELOPMENT.md`.

## Layout

```
app/
  core/        # settings (pydantic-settings) and feature switches
  db/          # SQLAlchemy declarative base and session management
  models/      # ORM models (Phase 0: audit_events)
  services/    # typed service functions (Phase 0: audit recording)
  main.py      # FastAPI shell (/health, /ready)
migrations/    # Alembic environment and versioned migrations
tests/         # pytest suite (runs against PostgreSQL)
docs/          # GOAL, AGENTS, CLAUDE, PROJECT_TRACKING, decisions, contracts
.github/workflows/ci.yml
```

## Guardrails (summary)

- Dry-run defaults on; features default off. Nothing schedules real email in
  Phase 0.
- Secrets never live in source — `.env.example` documents variable names only.
- The database is the system of record; schema changes go through migrations.

See `docs/PHASE_0.md` for how each Phase 0 backlog card is satisfied.

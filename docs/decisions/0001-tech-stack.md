# ADR 0001 — First-release technical stack (FND-001)

Status: Accepted (Phase 0)
Date: 2026-07-23
Deciders: Sahil (owner); implemented by the development agent.

## Context

Phase 0 must record the first-release stack so that later phases build on stable,
agreed choices (GOAL.md build order step 1; FND-001). The guiding constraints are
in `GOAL.md`, `AGENTS.md`, and `CLAUDE.md`: build the smallest reliable vertical
slice, prefer boring/reversible/testable technology, keep deterministic logic in
code, avoid paid LLM APIs, and use PostgreSQL/RDS as the system of record.

## Decision

| Concern | Choice | Reason |
| --- | --- | --- |
| Language | Python 3.11+ | Matches the user's existing insight/research scripts (INS-002/003 will wrap them); one language across services keeps the small team productive. |
| Backend / API | FastAPI + Uvicorn | Typed request/response models via Pydantic, async-capable, minimal boilerplate for a small internal control surface. |
| ORM | SQLAlchemy 2.0 (typed, `Mapped[...]`) | Mature, explicit, testable; first-class PostgreSQL types (UUID, JSONB) needed for evidence/audit models. |
| Migrations | Alembic | Repeatable, reversible schema changes; no manual production DDL (AGENTS.md). |
| Database | PostgreSQL 16 (local dev → RDS Postgres later) | System of record per AGENTS.md; JSONB for evidence/context; the same engine locally and in RDS. |
| Config | pydantic-settings | Typed settings from environment; secrets never in source. |
| DB driver | psycopg 3 (`psycopg[binary]`), pinned `>=3.1,<3.3` | 3.3.x returns text as bytes under some encodings and breaks the SQLAlchemy dialect; 3.2.x is verified end-to-end. |
| Lint / format | Ruff | One fast tool for linting + formatting. |
| Types | mypy (strict) | Catch contract errors early; `AGENTS.md` asks for typed service inputs/outputs. |
| Tests | pytest (against real PostgreSQL) | Audit/evidence models use Postgres-specific types; testing on Postgres avoids false confidence from SQLite. |
| Background work | Deferred to when first needed (Phase 1+/OPS-001) | No queue/worker infrastructure until an import or verification job actually requires it; avoids premature infrastructure. |
| Frontend | Deferred to Phase 8 (UI epic); integrate the approved Claude Design shell then | Phase 0 excludes dashboard build-out; no framework is committed here beyond "responsive web / PWA" from GOAL.md. |

## Consequences

- The RDS/production database is referenced by variable name only; no credentials
  exist in the repository. Migrations are proven on local Postgres in Phase 0.
- Later phases add domain tables and services within this stack; integrations
  (MillionVerifier, Saleshandy, Claude MCP) are adapters behind interfaces.

## Deferred / decisions still required from Sahil

These Phase 0 backlog cards are **not** resolved by this ADR and need Sahil (and,
where noted, IT) input before or during the relevant later phase:

- **FND-002 — Hosting model.** Where the private dashboard/backend run, how the
  phone reaches them (VPN? Cloudflare Access? Tailscale?), and what hosting is
  deferred. Depends on IT. Not required to build Phases 1–6 locally; required
  before OPS-004 (first private environment) and the pilot.
- **FND-006 — Private single-operator access.** How the deployed dashboard is
  protected without a full role system. Tied to the hosting decision; deferred
  with it. Local development needs no auth.

Frontend framework selection is likewise deferred to the UI phase to avoid
committing before the dashboard is built.

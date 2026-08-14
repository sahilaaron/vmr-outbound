---
description: Alembic migration rules
paths:
  - "migrations/**"
  - "alembic.ini"
---

# Migrations

- Schema changes only via **reversible Alembic migrations proven locally** — a
  migration without a working `downgrade` is not finished.
- One migration per schema slice; do not fold unrelated schema changes together.
- Run the migration up **and** down against a local database before handing off. A
  migration that has only ever been run forward is unproven.
- On Windows, an Anaconda `alembic` on PATH may shadow the project virtualenv's.
  Invoke it through the project interpreter (`.venv/Scripts/python.exe -m alembic`)
  so the migration runs against the intended environment.

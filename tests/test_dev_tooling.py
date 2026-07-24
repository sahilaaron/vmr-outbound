"""Guards for the local developer tooling (bootstrap + smoke scripts).

Keeps the optional developer-experience scripts consistent, safe, and
syntactically valid. The database-backed tests use the same local Postgres the
suite already requires.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_dev_up() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "dev_up_script", REPO_ROOT / "scripts" / "dev_up.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _admin_engine():  # type: ignore[no-untyped-def]
    base = make_url(os.environ["DATABASE_URL"]).set(database="postgres")
    return create_engine(base, isolation_level="AUTOCOMMIT")


# --- static / consistency guards --------------------------------------------


def test_bootstrap_scripts_compile() -> None:
    import py_compile

    for script in ("scripts/dev_up.py", "scripts/smoke.py"):
        py_compile.compile(str(REPO_ROOT / script), doraise=True)


def test_compose_is_utf8_and_loopback_only() -> None:
    text_ = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "postgres:16" in text_
    assert "127.0.0.1:5433:5432" in text_  # loopback-bound, matches default URL
    assert "UTF8" in text_
    assert "healthcheck" in text_


def test_development_doc_references_the_tooling() -> None:
    text_ = (REPO_ROOT / "docs" / "DEVELOPMENT.md").read_text(encoding="utf-8")
    assert "scripts/dev_up.py" in text_
    assert "scripts/smoke.py" in text_
    assert "docker compose up -d db" in text_


def test_no_stale_fnd004_in_branch_tooling() -> None:
    # This branch is maintenance tooling with no backlog card; the files it adds
    # must not carry a stale FND-004 label.
    for rel in ("docker-compose.yml", "scripts/dev_up.py", "scripts/smoke.py"):
        assert "FND-004" not in (REPO_ROOT / rel).read_text(encoding="utf-8")


# --- safe identifier handling in the create-database path -------------------


def test_create_statement_quotes_identifier_safely() -> None:
    dev_up = _load_dev_up()
    # A name that would break out of a naive f-string is safely doubled/quoted.
    stmt = dev_up.build_create_database_statement('a"b; DROP DATABASE x')
    assert stmt == 'CREATE DATABASE "a""b; DROP DATABASE x" ENCODING \'UTF8\' TEMPLATE template0'
    assert dev_up.build_create_database_statement("vmr_dev").startswith('CREATE DATABASE "vmr_dev"')


def test_create_path_uses_composition_not_interpolation() -> None:
    src = (REPO_ROOT / "scripts" / "dev_up.py").read_text(encoding="utf-8")
    assert "sql.Identifier" in src  # psycopg safe composition is used
    assert 'f"CREATE DATABASE' not in src  # no interpolated create statement
    assert 'CREATE DATABASE \\"{' not in src  # the old unsafe pattern is gone


# --- runtime behaviour (uses the local Postgres) ----------------------------


def test_ensure_database_creates_missing_and_is_idempotent() -> None:
    dev_up = _load_dev_up()
    name = f"vmr_tool_{uuid.uuid4().hex[:12]}"
    url = (
        make_url(os.environ["DATABASE_URL"])
        .set(database=name)
        .render_as_string(hide_password=False)
    )
    admin = _admin_engine()
    try:
        dev_up.ensure_database(url)  # creates the missing database
        with admin.connect() as conn:
            found = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
            ).scalar()
        assert found == 1
        dev_up.ensure_database(url)  # idempotent: already exists, no error
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_unreachable_server_does_not_trigger_create() -> None:
    dev_up = _load_dev_up()
    # An unreachable server is a connection failure, NOT a missing database:
    # it must fail clearly and never attempt CREATE DATABASE.
    bad_url = "postgresql+psycopg://dev@127.0.0.1:1/vmr_should_not_be_created"
    with pytest.raises(SystemExit):
        dev_up.ensure_database(bad_url)

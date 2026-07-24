"""Guards for the local developer tooling (bootstrap + smoke scripts).

Keeps the optional developer-experience scripts consistent and syntactically
valid. They do not touch the database — the scripts themselves are exercised
manually against a live Postgres (see docs/DEVELOPMENT.md).
"""

from __future__ import annotations

import py_compile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_bootstrap_scripts_compile() -> None:
    for script in ("scripts/dev_up.py", "scripts/smoke.py"):
        py_compile.compile(str(REPO_ROOT / script), doraise=True)


def test_compose_is_utf8_and_loopback_only() -> None:
    text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "postgres:16" in text
    assert "127.0.0.1:5433:5432" in text  # loopback-bound, matches default URL
    assert "UTF8" in text
    assert "healthcheck" in text


def test_development_doc_references_the_tooling() -> None:
    # The tooling is documented in the single canonical dev-setup doc, not a
    # separate overlapping source of truth.
    text = (REPO_ROOT / "docs" / "DEVELOPMENT.md").read_text(encoding="utf-8")
    assert "scripts/dev_up.py" in text
    assert "scripts/smoke.py" in text
    assert "docker compose up -d db" in text

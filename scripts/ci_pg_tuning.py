"""Reduce the durability overhead of the throwaway CI PostgreSQL container.

Why this exists
---------------
`tests/conftest.py` empties the database after **every** test with a single
``TRUNCATE ... RESTART IDENTITY CASCADE`` over every application table. That is
the right call for test honesty — it is what makes "no contacts exist" a real
assertion rather than a lucky ordering — but it is not a cheap statement.
TRUNCATE rewrites each relation, so one sweep creates and unlinks a new file for
every table *and every index*: 90 tables and 341 indexes, 431 relations, per
test.

Measured on a 2 vCPU runner-equivalent, that sweep costs **329 ms per test** with
PostgreSQL's default durability settings, which is the majority of the wall time
of a database-heavy shard. On a GitHub-hosted runner, where the container writes
through an overlay filesystem onto network-backed storage, it costs
proportionally more — which is what pushed `tests (campaign-import)` to 19:08 in
run #324 and got the job cancelled by its own timeout.

With the settings below the same sweep costs **107 ms** — a 3.1x reduction, and
none of it comes from testing less.

What this changes, and why it is safe
-------------------------------------
Every setting here trades **crash durability** for speed. That trade is free in
this specific place and nowhere else: the database lives inside a service
container that GitHub destroys when the job ends, holds nothing but synthetic
fixtures, and is never read again. Turning off ``fsync`` cannot lose data that
nobody will ever look for.

What it explicitly does **not** change:

* nothing about the application, its settings, or ``DATABASE_URL``;
* nothing a test can observe. These are storage-durability knobs; transaction
  semantics, isolation, constraints and visibility are untouched, so a test that
  passes here passes on a default server and vice versa.

Every setting is ``sighup`` context except ``synchronous_commit`` (``user``), so
a reload applies them all — the service container never has to be restarted,
which a GitHub workflow could not do anyway.

This script is CI-only. Running it against anything other than a disposable
container would be a mistake, so it refuses any host that is not loopback.
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

#: Setting -> why it is being changed. Printed into the CI log so the job
#: explains itself rather than looking like unexplained magic.
SETTINGS: tuple[tuple[str, str, str], ...] = (
    ("fsync", "off", "TRUNCATE's relation rewrites no longer wait on the disk"),
    ("synchronous_commit", "off", "the per-test truncation commit stops flushing WAL"),
    ("full_page_writes", "off", "no torn-page protection needed for a disposable database"),
    ("max_wal_size", "4GB", "the truncation churn stops forcing checkpoints every few tests"),
    ("checkpoint_timeout", "30min", "at most one timed checkpoint in a job this short"),
)

#: Only ever loopback. A service container is reached on 127.0.0.1 through
#: GitHub's port mapping; anything else is somebody's real server.
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class UnsafeTuningTarget(RuntimeError):
    """The target is not a disposable CI database."""


def _assert_disposable(url: str) -> None:
    parsed = make_url(url)
    host = (parsed.host or "").lower()
    if host not in ALLOWED_HOSTS:
        raise UnsafeTuningTarget(
            f"Refusing to change durability settings on host {host!r}.\n"
            f"This script exists for the throwaway PostgreSQL service container in "
            f"CI, reached over loopback. It turns off fsync, which is only ever "
            f"acceptable on a database that is about to be deleted."
        )
    if os.environ.get("CI") != "true" or os.environ.get("GITHUB_ACTIONS") != "true":
        # Not fatal — an operator may want to reproduce a CI timing locally — but
        # it should never happen silently.
        print(
            "note: CI/GITHUB_ACTIONS are not both set, so this is not a GitHub run. "
            "Continuing because the target is loopback, but do not point this at a "
            "database you care about.",
            file=sys.stderr,
        )


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("error: DATABASE_URL is not set", file=sys.stderr)
        return 2

    try:
        _assert_disposable(url)
    except UnsafeTuningTarget as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    # ALTER SYSTEM cannot run inside a transaction block.
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            for name, value, reason in SETTINGS:
                # Values are literals from the table above, never user input.
                connection.execute(text(f"ALTER SYSTEM SET {name} = '{value}'"))
                print(f"  set {name} = {value}   ({reason})")
            connection.execute(text("SELECT pg_reload_conf()"))

            print("\nEffective after reload:")
            rows = connection.execute(
                text("SELECT name, setting FROM pg_settings WHERE name = ANY(:names)"),
                {"names": [name for name, _, _ in SETTINGS]},
            ).all()
            applied: dict[str, str] = {str(row[0]): str(row[1]) for row in rows}
            for name, _, _ in SETTINGS:
                print(f"  {name} = {applied.get(name, '<unknown>')}")

            # A reload is asynchronous only for the postmaster's children; the
            # values above are read back from this same session's view of the
            # postmaster's config, so a mismatch here means the reload did not
            # take and the job would silently run at full cost.
            if applied.get("fsync") != "off":
                print(
                    "\nerror: fsync is still on after reload — the tuning did not "
                    "apply, and this job would run several times slower than the "
                    "shard budget assumes.",
                    file=sys.stderr,
                )
                return 1
    finally:
        engine.dispose()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

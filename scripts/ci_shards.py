"""The single definition of how the pytest suite is split across CI jobs.

Why this file exists
--------------------
CI used to run one serial job whose final step was ``python -m pytest``. With
roughly three thousand tests that step alone dominated the run, so a red build
took over twenty minutes to say so. Splitting the suite across several GitHub
runners fixes the wall-clock problem but introduces a worse risk: a test that
belongs to no shard is a test that silently stops running, and nothing about a
green build would reveal it.

So the split is defined here, in one place, in terms the workflow cannot
contradict, and :func:`verify` proves against a real pytest collection that the
union of the shards is exactly the suite ``python -m pytest`` would run.

How the split works
-------------------
Every shard except one is a named list of path prefixes. The last shard,
:data:`CATCH_ALL_SHARD`, takes every test module that no earlier shard claimed.
Two consequences, both deliberate:

* a **new test file cannot be omitted** — if nobody assigns it, the catch-all
  runs it, so the failure mode of forgetting is "slightly slower shard", never
  "untested code";
* **no test can run twice**, because assignment is first-match-wins over an
  ordered list, which is a function, not a lookup that can hit two entries.

Shard membership is by file, never by test name, and never by anything computed
at runtime. The same commit always produces the same shards on every machine,
which is what makes a failure reproducible locally with the command the job
printed.

Groupings follow subject matter rather than balance alone, so a red job names
the area that broke: ``tests (campaign-import)`` is a lead, ``shard-3`` is not.
Within that constraint the groups are balanced against measured runtimes. (An
earlier version of this note pointed at ``handoff-ci-parallelization.md`` for
those measurements; no such file has ever existed in this repository, so the
pointer is removed rather than left to send the next reader looking.)

Usage
-----
``python scripts/ci_shards.py names``        every shard name, one per line
``python scripts/ci_shards.py paths NAME``   the test paths that shard runs
``python scripts/ci_shards.py plan``         human-readable file/shard table
``python scripts/ci_shards.py verify``       the full accounting proof
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: Test modules are discovered with this glob, which is pytest's own default
#: ``python_files`` pattern applied to the configured ``testpaths``. Keeping the
#: two in step is what lets a file-level argument list stand in for a full
#: collection.
TEST_FILE_GLOB = "test_*.py"


@dataclass(frozen=True)
class Shard:
    """One CI test job: a name, a reason for existing, and what it runs."""

    name: str
    description: str
    #: Path prefixes, relative to the repository root. A test module belongs to
    #: this shard when its path starts with any of them. Prefixes are matched
    #: against the full relative path, so ``tests/test_company_intelligence``
    #: claims ``tests/test_company_intelligence_jobs.py`` too — which is the
    #: intent, since a family of files is one subject.
    prefixes: tuple[str, ...] = ()
    #: The catch-all shard claims whatever is left. Exactly one shard sets this.
    catch_all: bool = False
    #: Modules this shard claims by exact path, overriding a prefix that would
    #: otherwise pull them into an earlier shard.
    exact: tuple[str, ...] = field(default=())


#: Ordered — first match wins. The catch-all must be last.
SHARDS: tuple[Shard, ...] = (
    Shard(
        name="company-intelligence",
        description=(
            "Company Intelligence: extraction, jobs, handoff, admin surface, and "
            "the CI-002 regression set."
        ),
        prefixes=(
            "tests/test_company_intelligence.py",
            "tests/test_company_intelligence_ci002",
            "tests/test_company_intelligence_handoff",
            "tests/test_company_intelligence_jobs",
            "tests/test_company_intelligence_specialty",
            "tests/test_company_intelligence_web",
            "tests/test_integration_company_intelligence",
        ),
    ),
    Shard(
        name="company-firmographics",
        description=(
            "Company firmographics and identity: geography, employee size, the "
            "company workspace, and automatic domain resolution."
        ),
        prefixes=(
            "tests/test_company_intelligence_geography",
            "tests/test_company_domain_resolution",
            "tests/test_company_web",
            "tests/test_company_workspace",
            "tests/test_employee_size_derivation",
            "tests/test_field_provenance",
            "tests/test_logodev_client",
            "tests/test_model_domain_lookup",
            "tests/test_salesnav_domain_enrichment",
        ),
    ),
    Shard(
        name="agents-research",
        description=(
            "Agent runtime, Agent Studio, research workers, the seller knowledge "
            "base, insight evidence, and personalization."
        ),
        prefixes=(
            "tests/test_agent",
            "tests/test_ev_001",
            "tests/test_insight_evidence",
            "tests/test_kb_",
            "tests/test_knowledge_agents",
            "tests/test_personalization",
            "tests/test_qa_policy",
            "tests/test_research",
            "tests/test_thinking_seam",
            "tests/test_workbench_agents",
        ),
        exact=("tests/test_seller_knowledge.py",),
    ),
    # Campaign import is split three ways. It was one shard until run #324, where
    # it passed 532 tests in 19:08 and was then killed by the job timeout — the
    # tests were fine, the shard was too big for a hosted runner's variance. The
    # three groups below are balanced on *both* measured duration and test count,
    # because on a hosted runner a large part of the cost is per-test fixture
    # work rather than per-assertion work, and balancing only one of the two
    # leaves the split hostage to which term dominates.
    Shard(
        name="campaign-import-review",
        description=(
            "The second-review pass over an import, plus the operator-facing "
            "import surfaces: the admin workbench view, the import web pages, "
            "idempotent re-import, and campaign basics."
        ),
        prefixes=(
            "tests/test_campaign_import_admin_workbench",
            "tests/test_campaign_import_idempotency",
            "tests/test_campaign_import_second_review",
            "tests/test_campaign_import_web",
            "tests/test_campaigns",
        ),
    ),
    Shard(
        name="campaign-import-pipeline",
        description=(
            "The review-fix pass, contact and company resolution during import, "
            "the import pipeline itself and its web surface, and the handoff "
            "from a finished import into an outreach sequence."
        ),
        prefixes=(
            "tests/test_campaign_import_pipeline",
            "tests/test_campaign_import_resolution",
            "tests/test_campaign_import_review_fixes",
            "tests/test_campaign_pipeline_web",
            "tests/test_import_to_sequence",
        ),
    ),
    Shard(
        name="campaign-import-parsing",
        description=(
            "The final-review pass, and everything that turns a file into rows: "
            "parsing, column mapping, validation, staging, spreadsheet preview "
            "and xlsx import, and the imported-email column."
        ),
        prefixes=(
            "tests/test_campaign_import_email",
            "tests/test_campaign_import_final_review",
            "tests/test_campaign_import_parsing",
            "tests/test_imports",
            "tests/test_mapping",
            "tests/test_parsing",
            "tests/test_preview_and_xlsx",
            "tests/test_staging",
            "tests/test_validation",
        ),
    ),
    Shard(
        name="web-ui-workbench",
        description=(
            "Server-rendered surfaces: the v2 customer and operator UI, the "
            "admin workbench, the contact CRM pages, and the JSON API."
        ),
        prefixes=(
            "tests/test_admin_workbench_web",
            "tests/test_api",
            "tests/test_crm",
            "tests/test_health",
            "tests/test_phase2",
            "tests/test_review_web",
            "tests/test_seller_knowledge_web",
            "tests/test_v2_",
            "tests/test_workbench_",
        ),
    ),
    Shard(
        name="email-verification-sequence",
        description=(
            "Email discovery, address verification and its provider seam, the "
            "seven-message outreach sequence, and the suppression ledger."
        ),
        prefixes=(
            "tests/test_email_",
            "tests/test_suppression",
            "tests/test_verification",
        ),
    ),
    Shard(
        name="migrations-runtime",
        description=(
            "Migration round trip, production hardening, worker concurrency, "
            "test-isolation guards, and configuration."
        ),
        prefixes=(
            "tests/test_audit_event",
            "tests/test_campaign_pause_concurrency",
            "tests/test_config",
            "tests/test_dev_tooling",
            "tests/test_devtools",
            "tests/test_features",
            "tests/test_migrations",
            "tests/test_production_hardening",
            "tests/test_schema_dat001",
            "tests/test_test_isolation",
            "tests/test_usage_ledger",
            "tests/test_worker_",
        ),
    ),
    Shard(
        name="capture-identity-core",
        description=(
            "Capture intake and promotion, identity resolution and its gates, "
            "LinkedIn and Sales Navigator intake — and every test module no "
            "earlier shard claims, so a newly added file always runs somewhere."
        ),
        catch_all=True,
    ),
)

CATCH_ALL_SHARD = next(shard for shard in SHARDS if shard.catch_all)


class ShardError(RuntimeError):
    """A shard definition or the accounting proof is wrong."""


def _validate_definition() -> None:
    """Fail fast on a definition that could not possibly be a clean partition."""

    names = [shard.name for shard in SHARDS]
    if len(set(names)) != len(names):
        raise ShardError(f"Duplicate shard names: {names}")

    catch_alls = [shard.name for shard in SHARDS if shard.catch_all]
    if len(catch_alls) != 1:
        raise ShardError(f"Exactly one catch-all shard is required, found {catch_alls}")
    if not SHARDS[-1].catch_all:
        raise ShardError("The catch-all shard must be last so it claims only leftovers")
    if CATCH_ALL_SHARD.prefixes or CATCH_ALL_SHARD.exact:
        raise ShardError("The catch-all shard must not also declare prefixes")


def test_files() -> list[str]:
    """Every test module in the suite, as repository-relative POSIX paths."""

    return sorted(
        path.relative_to(REPO_ROOT).as_posix() for path in TESTS_DIR.rglob(TEST_FILE_GLOB)
    )


def shard_for(path: str) -> str:
    """The one shard a test module belongs to.

    First match wins over an ordered list, so this is a total function onto the
    shard names: every path gets exactly one answer, and no path gets two.
    """

    for shard in SHARDS:
        if path in shard.exact:
            return shard.name
        if any(path.startswith(prefix) for prefix in shard.prefixes):
            return shard.name
    return CATCH_ALL_SHARD.name


def assignments() -> dict[str, list[str]]:
    """Shard name -> the test modules it runs."""

    _validate_definition()
    result: dict[str, list[str]] = {shard.name: [] for shard in SHARDS}
    for path in test_files():
        result[shard_for(path)].append(path)
    return result


def paths_for(name: str) -> list[str]:
    grouped = assignments()
    if name not in grouped:
        raise ShardError(f"Unknown shard {name!r}. Known shards: {', '.join(grouped)}")
    if not grouped[name]:
        raise ShardError(
            f"Shard {name!r} selects no test files. A shard that runs nothing is "
            f"almost always a stale prefix after a rename — fix the definition "
            f"rather than letting the job pass vacuously."
        )
    return grouped[name]


# ---------------------------------------------------------------------------
# The accounting proof
# ---------------------------------------------------------------------------


def _collect(args: Sequence[str]) -> set[str]:
    """Node IDs pytest would run for ``args`` (empty args = the whole suite).

    ``-o addopts=`` clears the project's ``-q`` so the output is one node ID per
    line rather than the per-file counts a doubled ``-q`` produces.
    """

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-o", "addopts=", "-q", "--collect-only", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ShardError(
            "pytest collection failed"
            + (f" for {' '.join(args)}" if args else " for the full suite")
            + f"\n--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
        )
    return {line.strip() for line in completed.stdout.splitlines() if "::" in line}


def _format_missing(label: str, node_ids: Iterable[str], limit: int = 20) -> str:
    listed = sorted(node_ids)
    shown = "\n".join(f"    {node}" for node in listed[:limit])
    more = f"\n    ... and {len(listed) - limit} more" if len(listed) > limit else ""
    return f"  {label} ({len(listed)}):\n{shown}{more}"


def verify(*, check_workflow: bool = True) -> int:
    """Prove the shards are exactly the suite, and say so in a readable report.

    Three independent claims are checked, because each can fail without the
    others noticing:

    1. every test module on disk is assigned to exactly one shard;
    2. the union of what the shards actually collect equals what a bare
       ``python -m pytest`` collects — no omission, no duplication;
    3. the workflow's matrix runs precisely the shards defined here, so the two
       cannot drift apart silently.
    """

    grouped = assignments()
    problems: list[str] = []

    print("Shard assignment (by file)")
    print("-" * 72)
    for name, files in grouped.items():
        print(f"  {name:30s} {len(files):4d} files")
    total_files = sum(len(files) for files in grouped.values())
    print(f"  {'TOTAL':30s} {total_files:4d} files")
    if total_files != len(test_files()):
        problems.append("Assigned file count does not equal the number of test modules on disk")

    print()
    print("Collecting the full suite ...")
    full = _collect(())
    print(f"  {len(full)} tests collected by `python -m pytest`")

    print()
    print("Collecting each shard ...")
    seen: dict[str, str] = {}
    duplicated: dict[str, list[str]] = {}
    per_shard_counts: dict[str, int] = {}
    for name, files in grouped.items():
        collected = _collect(files)
        per_shard_counts[name] = len(collected)
        print(f"  {name:30s} {len(collected):5d} tests")
        for node in collected:
            if node in seen:
                duplicated.setdefault(node, [seen[node]]).append(name)
            else:
                seen[node] = name

    union = set(seen)
    missing = full - union
    extra = union - full

    print()
    print("Accounting")
    print("-" * 72)
    print(f"  full suite            {len(full):5d}")
    print(f"  union of shards       {len(union):5d}")
    print(f"  sum of shard sizes    {sum(per_shard_counts.values()):5d}")
    print(f"  omitted by all shards {len(missing):5d}")
    print(f"  collected twice       {len(duplicated):5d}")
    print(f"  not in the full suite {len(extra):5d}")

    if missing:
        problems.append("Tests the shards would never run:\n" + _format_missing("omitted", missing))
    if extra:
        problems.append(
            "Shards collect tests the full suite does not:\n" + _format_missing("extra", extra)
        )
    if duplicated:
        detail = "\n".join(
            f"    {node} -> {', '.join(shards)}" for node, shards in sorted(duplicated.items())[:20]
        )
        problems.append(f"Tests collected by more than one shard ({len(duplicated)}):\n{detail}")
    if sum(per_shard_counts.values()) != len(union):
        problems.append("Shard sizes do not sum to the union, which means overlap")

    if check_workflow:
        problems.extend(_check_workflow_matrix())

    print()
    if problems:
        print("FAILED")
        for problem in problems:
            print(f"\n* {problem}")
        return 1

    print("PASSED — the union of the shards is exactly the suite `python -m pytest` runs.")
    return 0


def _check_workflow_matrix() -> list[str]:
    """The workflow must run every shard defined here, and only those."""

    try:
        import yaml
    except ImportError:  # pragma: no cover - PyYAML is a project dependency
        return ["PyYAML is unavailable, so the workflow matrix could not be checked"]

    if not WORKFLOW.exists():
        return [f"{WORKFLOW} does not exist"]

    # `on:` is parsed by PyYAML as the boolean True (YAML 1.1). Harmless here —
    # nothing below reads it — but worth knowing before adding checks.
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = document.get("jobs", {})
    matrix_shards: list[str] = []
    for job in jobs.values():
        strategy = (job or {}).get("strategy", {})
        matrix = (strategy or {}).get("matrix", {})
        if "shard" in matrix:
            matrix_shards.extend(matrix["shard"])

    defined = [shard.name for shard in SHARDS]
    if not matrix_shards:
        return ["No job in the workflow declares a `shard` matrix"]
    if sorted(matrix_shards) != sorted(defined):
        return [
            "The workflow matrix and the shard definition disagree.\n"
            f"    workflow: {sorted(matrix_shards)}\n"
            f"    defined : {sorted(defined)}"
        ]
    if len(set(matrix_shards)) != len(matrix_shards):
        return [f"The workflow matrix repeats a shard: {matrix_shards}"]
    return []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("names", help="print every shard name, one per line")
    sub.add_parser("plan", help="print the shard/file table")
    sub.add_parser("json", help="print the full assignment as JSON")
    sub.add_parser("verify", help="prove the shards equal the full suite")

    paths = sub.add_parser("paths", help="print the test paths for one shard")
    paths.add_argument("shard")

    args = parser.parse_args(argv)

    try:
        if args.command == "names":
            print("\n".join(shard.name for shard in SHARDS))
            return 0
        if args.command == "paths":
            print("\n".join(paths_for(args.shard)))
            return 0
        if args.command == "json":
            print(json.dumps(assignments(), indent=2))
            return 0
        if args.command == "plan":
            grouped = assignments()
            for shard in SHARDS:
                files = grouped[shard.name]
                print(f"{shard.name} ({len(files)} files)")
                print(f"  {shard.description}")
                for path in files:
                    print(f"    {path}")
                print()
            return 0
        if args.command == "verify":
            return verify()
    except ShardError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    raise AssertionError(f"unhandled command {args.command!r}")  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())

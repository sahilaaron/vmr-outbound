"""Guards for the pinned deployment dependency closure.

`vmr-deploy` installs `constraints.txt` as a *requirements* file and then
installs the project with `--no-deps`. That is the whole reason this file
matters: under `--no-deps`, pip never looks at `pyproject.toml`, so a runtime
dependency that is declared there and missing here is simply absent from the
release. The failure does not surface at install time. It surfaces as an
`ImportError` from the release's own import check, or — if the import happens to
be lazy — as a 500 on the first request that reaches it.

That is not hypothetical. `argon2-cffi` was declared in `pyproject.toml` for
hosted password hashing and never added here, so a clean deployment installed
the closure, installed the project `--no-deps`, and then could not
`import app.main` at all.

These are static guards. They read the two files and compare them; they do not
install anything.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONSTRAINTS = REPO_ROOT / "constraints.txt"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _normalize(name: str) -> str:
    """PEP 503 normalisation, so `argon2_cffi` and `argon2-cffi` compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(spec: str) -> str:
    """The bare distribution name from a requirement string.

    Drops extras and any version specifier: `psycopg[binary]>=3.1,<3.3` -> `psycopg`.
    """
    return _normalize(re.split(r"[\[<>=!~;\s]", spec.strip(), maxsplit=1)[0])


def _declared_runtime_dependencies() -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return {_requirement_name(dep) for dep in data["project"]["dependencies"]}


def _pinned_lines() -> list[str]:
    lines = []
    for raw in CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def _pinned_names() -> set[str]:
    return {_requirement_name(line) for line in _pinned_lines()}


def test_every_declared_runtime_dependency_is_pinned() -> None:
    """The check that `argon2-cffi` failed.

    `--no-deps` means pip will not resolve a missing entry for us, so anything
    `pyproject.toml` declares as a runtime dependency has to appear here by
    name or it is not installed in the release at all.
    """
    declared = _declared_runtime_dependencies()
    assert declared, "no runtime dependencies parsed — the reader is broken, not the project"

    missing = sorted(declared - _pinned_names())
    assert not missing, (
        "declared in pyproject.toml but absent from constraints.txt, so a "
        f"`--no-deps` release would not install them: {missing}"
    )


def test_the_dev_extra_is_not_pinned_into_the_runtime_closure() -> None:
    """A staging release must not carry pytest/ruff/mypy.

    The file says so in its own SCOPE section; this keeps that true.
    """
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    dev = {_requirement_name(dep) for dep in data["project"]["optional-dependencies"]["dev"]}
    assert dev, "no dev extra parsed — the reader is broken"

    leaked = sorted(dev & _pinned_names())
    assert not leaked, f"development-only packages leaked into the runtime closure: {leaked}"


def test_every_pin_is_exact() -> None:
    """A range here would defeat the point: two deploys of one SHA could differ."""
    loose = [line for line in _pinned_lines() if "==" not in line]
    assert not loose, f"constraints.txt must pin exact versions, found: {loose}"


def test_the_closure_stays_in_the_documented_sort_order() -> None:
    """The file documents `sort -f` as its regeneration recipe.

    Keeping the committed order equal to that recipe's output is what makes the
    diff of a regeneration reviewable instead of a reshuffle.
    """
    pinned = _pinned_lines()
    assert pinned, "no pins parsed — the reader is broken"
    assert pinned == sorted(pinned, key=str.lower), (
        "constraints.txt is no longer in case-insensitive sort order; "
        "regenerate it with the documented `sort -f` recipe"
    )


def test_the_deploy_readme_package_count_matches_the_file() -> None:
    """The count in the README is load-bearing documentation, not decoration.

    It is the only stated fact about this file's contents, and a reviewer uses
    it to notice an unexplained addition.
    """
    readme = (REPO_ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
    match = re.search(r"pinned runtime closure \((\d+)\s*\n?packages", readme)
    assert match is not None, "could not find the package count in deploy/README.md"
    assert int(match.group(1)) == len(_pinned_lines()), (
        "deploy/README.md states a different package count than constraints.txt holds"
    )


def test_every_env_example_value_survives_shell_sourcing() -> None:
    """`vmr-deploy` *sources* the env file to run Alembic against the release.

    systemd's parser and the shell's do not agree: systemd keeps the quotes in
    ``A=["x"]``, the shell strips them, and the value arrives as ``[x]`` and
    fails to parse. So the failure appears only under `vmr-deploy`, only at the
    migration step, and only after the database backup has been taken — while
    `vmr-web` started from the same file would have been fine.

    That is exactly what happened to ``AUTH__ALLOWED_OPERATOR_EMAILS``. The file
    states this rule at the top and then broke it, which is the kind of thing a
    reader trusts and a test does not.

    Commented lines are checked too: a placeholder is uncommented verbatim by
    whoever turns the feature on.
    """

    example = (REPO_ROOT / "deploy" / "vmr.env.example").read_text(encoding="utf-8")
    offenders: list[str] = []
    checked = 0
    for raw in example.splitlines():
        line = raw.strip()
        commented = line.startswith("#")
        if commented:
            line = line.lstrip("#").strip()
        if not line or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            continue
        checked += 1
        if not value:
            continue
        # Single-quoted is always safe: the shell hands the contents through
        # untouched, which is what the FORMAT RULE asks for.
        if value.startswith("'") and value.endswith("'") and len(value) > 1:
            continue
        # Only characters the shell actually *alters*. Brackets are not among
        # them -- `A=[]` and `A=[a@b.com]` both survive intact, and it is the
        # double quotes inside `["a@b.com"]` that get stripped.
        hazards = '"`$\\'
        if not commented:
            # An unquoted space is the other half of the systemd/shell
            # disagreement the FORMAT RULE describes. Prose is common inside
            # comments, so this stricter rule applies to live lines only.
            hazards += " "
        if any(character in value for character in hazards):
            offenders.append(line)

    assert checked, "no assignments parsed from vmr.env.example — the reader is broken"
    assert not offenders, (
        "these values contain shell metacharacters and are not single-quoted, so "
        "`vmr-deploy` would mangle them when it sources the file: " + "; ".join(offenders)
    )


def test_the_documented_install_command_matches_what_vmr_deploy_runs() -> None:
    """`--constraint` and `--requirement` mean different things here.

    As a *constraints* file, a line only caps a version something else already
    asked for. As a *requirements* file — which is what `vmr-deploy` passes —
    every line is installed unconditionally. Adding a package here therefore
    installs it, and the header must not claim otherwise.
    """
    header = CONSTRAINTS.read_text(encoding="utf-8")
    deploy_script = (REPO_ROOT / "deploy" / "sbin" / "vmr-deploy").read_text(encoding="utf-8")

    assert "--requirement" in deploy_script, "vmr-deploy no longer installs a requirements file"
    assert "--no-deps" in deploy_script, "vmr-deploy no longer installs the project --no-deps"
    assert "pip install --constraint constraints.txt" not in header, (
        "constraints.txt header describes `--constraint`, but vmr-deploy passes "
        "`--requirement`; the distinction changes what adding a line here does"
    )

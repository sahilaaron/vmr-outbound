#!/usr/bin/env python3
"""Re-project the VMR Outbound subagent subset from the full agent library.

The full third-party agent library is deliberately kept OUTSIDE Claude Code's
global scan path (`~/.claude/agents/`) so it is not loaded into every session in
every project. This repository carries only the curated subset that is relevant
to VMR Outbound, under `.claude/agents/`.

A project copy is the upstream file with exactly one change: the frontmatter
`description:` line is replaced by the short routing description recorded in
`.claude/agent-selection.json`. Bodies are byte-identical to the library source,
so a refresh is a clean re-copy rather than a merge.

Usage
-----
    python .claude/agents-refresh.py --check     # report drift, change nothing
    python .claude/agents-refresh.py --apply     # rewrite .claude/agents/*.md

    # override the library location if it is not at the default path
    python .claude/agents-refresh.py --apply --library /path/to/agency-agents

Exit codes: 0 = in sync (or applied), 1 = drift found (--check), 2 = error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

CLAUDE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(CLAUDE_DIR)
MANIFEST = os.path.join(CLAUDE_DIR, "agent-selection.json")
AGENTS_DIR = os.path.join(CLAUDE_DIR, "agents")

DESC_RE = re.compile(
    r"^description:[ \t]*(.*(?:\r?\n(?![A-Za-z_-]+:[ \t]|---)[ \t]*.*)*)\r?\n",
    re.M,
)


def project(source_text: str, description: str) -> bytes:
    """Return the upstream file with only its `description:` line replaced."""
    if not source_text.startswith("---"):
        raise ValueError("source has no frontmatter")
    end = source_text.find("\n---", 3)
    if end == -1:
        raise ValueError("source frontmatter is unterminated")

    fm, rest = source_text[3 : end + 1], source_text[end + 1 :]
    newline = "\r\n" if "\r\n" in source_text else "\n"

    match = DESC_RE.search(fm)
    if not match:
        raise ValueError("source has no description")
    if '"' in description or "\\" in description:
        raise ValueError("short description contains an unquotable character")

    fm = fm[: match.start()] + f'description: "{description}"' + newline + fm[match.end() :]
    return ("---" + fm + rest).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="report drift only")
    mode.add_argument("--apply", action="store_true", help="rewrite project copies")
    parser.add_argument("--library", help="path to the library's agency-agents directory")
    args = parser.parse_args()

    with open(MANIFEST, encoding="utf-8") as fh:
        manifest = json.load(fh)

    library = args.library or os.path.expanduser(manifest["library_root"])
    if not os.path.isdir(library):
        print(f"error: agent library not found at {library}", file=sys.stderr)
        print("       pass --library /path/to/agency-agents", file=sys.stderr)
        return 2

    os.makedirs(AGENTS_DIR, exist_ok=True)
    expected: set[str] = set()
    drift: list[str] = []

    for agent in manifest["agents"]:
        name = agent["name"]
        source_path = os.path.join(library, agent["source"].replace("/", os.sep))
        target_path = os.path.join(AGENTS_DIR, name + ".md")
        expected.add(name + ".md")

        if not os.path.isfile(source_path):
            drift.append(f"MISSING UPSTREAM  {name}  ({agent['source']})")
            continue

        with open(source_path, "rb") as fh:
            raw = fh.read()
        source_text = raw.decode("utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8")

        try:
            projected = project(source_text, agent["description"])
        except ValueError as exc:
            drift.append(f"UNPROJECTABLE     {name}: {exc}")
            continue

        current = None
        if os.path.isfile(target_path):
            with open(target_path, "rb") as fh:
                current = fh.read()

        if current == projected:
            continue

        drift.append(("NEW               " if current is None else "CHANGED           ") + name)
        if args.apply:
            with open(target_path, "wb") as fh:
                fh.write(projected)

    for stale in sorted(set(os.listdir(AGENTS_DIR)) - expected):
        if not stale.endswith(".md"):
            continue
        drift.append(f"NOT IN MANIFEST   {stale}")
        if args.apply:
            os.remove(os.path.join(AGENTS_DIR, stale))

    total = sum(len(a["description"]) for a in manifest["agents"])
    print(f"library:  {library}")
    selected = len(manifest["agents"])
    library_count = manifest["library_agent_count"]
    print(f"selected: {selected} of {library_count} library agents")
    print(f"combined description footprint: {total} chars (~{total // 4} tokens)")

    if not drift:
        print("in sync: project copies match the library + manifest")
        return 0

    print(("applied " if args.apply else "drift  ") + f"({len(drift)}):")
    for line in drift:
        print("  " + line)
    return 0 if args.apply else 1


if __name__ == "__main__":
    sys.exit(main())

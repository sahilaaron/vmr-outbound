# Parallel Work and Integration Authority

Several agent threads may build at the same time. Only one thread may assemble,
validate, and publish the result.

Running parallel threads is not the failure mode. Allowing each thread to act as
its own integration authority is. When three threads each rebase, each declare a
branch final, and each rely on CI to discover what broke, the result is branch
drift and a sequence of one-gate corrections.

This document defines how parallel work is scoped, frozen, handed off, and
integrated.

## Roles

**Implementation thread.** Builds one owned domain. Commits frequently. Exports
a durable artefact before the session ends. Never restacks another thread's work
and never declares a dependent branch final.

**Integration thread.** Exactly one per group of related work. Selects the
authoritative upstream heads, restacks or cherry-picks downstream commits,
resolves semantic conflicts, runs the complete gate sequence on the final
assembled head, and produces the only branches handed over for publication.

Many threads may build. One thread integrates. One exact tree is validated.

**Publication is not integration.** This document decides *which tree* is
correct. It does not change who acts on it: the division of labour in
`docs/AGENTS.md` and `docs/CLAUDE.md` still holds. Claude pushes when it holds
credentials and otherwise delivers a bundle for Sahil's push bridge; ChatGPT
opens, retargets, and merges pull requests, and merges only after Sahil's
explicit approval. Where this document says "parent merges before child", it
states the required order — it does not reassign the action.

## Classifying the Work Before Threads Start

Before launching parallel threads, write a dependency table and put it in the
task prompt of every thread in the group.

| Task | Base SHA | Depends on | Parallel development | Parallel finalization |
| ---- | -------- | ---------- | -------------------- | --------------------- |
|      |          |            | yes / no             | yes / no              |

Two categories:

**Siblings — safe to develop *and* finalize in parallel.** Tasks that share only
a stable public contract, touch disjoint files, introduce no migration ordering
dependency, and do not consume state the other newly introduced. Extension
parser hardening, an independent provider adapter, documentation, and unrelated
UI styling usually qualify.

**Dependents — safe to develop, not to finalize, in parallel.** Anything in a
chain. A downstream thread may scaffold against a pinned upstream contract and
build its own shell, but its final commit, its tests, and its integration must
wait for the upstream authoritative head.

If a task cannot be placed confidently in one category, treat it as a dependent.

## Ownership Blocks

Every parallel task prompt must state ownership explicitly:

```
Owns:
- <services and modules>
- <tests>
- <migration, or "no migration">
- <UI or API surface>

Must not modify:
- adjacent domain behaviour
- another thread's migration
- another thread's policy or rule version
- shared queue or workflow semantics unless explicitly assigned
```

A thread that needs to change something it does not own stops and asks. It does
not make the change and mention it in the handoff.

## Freezing an Authoritative SHA

A branch becomes a valid base for downstream finalization only when all of these
are true:

1. committed;
2. pushed, or delivered as a verified bundle;
3. the exact SHA is recorded;
4. its full gate sequence is green;
5. its contract has been reviewed;
6. it has been declared frozen for downstream integration.

Downstream threads are never told to "use the latest branch". They receive:

```
Base branch: <branch>
Required SHA: <full 40-character SHA>
Do not finalize against any other SHA.
```

A downstream thread that finds its recorded base SHA is no longer the branch tip
stops and reports it. It does not silently follow the moving branch.

## Machine-Verifiable Handoff

A prose report is never a substitute for a surviving Git commit. Before a
session ends, the thread must do one of the following — whichever its
credentials allow:

```bash
git push origin <branch>
```

or, when the session cannot authenticate to the remote, deliver a bundle for the
push bridge:

```bash
git bundle create <artifact>.bundle <base>..<branch>
git bundle verify <artifact>.bundle
git bundle list-heads <artifact>.bundle
```

Every thread handoff must carry:

- base SHA and head SHA;
- the commit list;
- the bundle path and its SHA-256;
- `git bundle verify` output;
- `git merge-base` proof against the declared base;
- `git diff --stat`;
- `git range-diff` when the branch was rebased;
- the changed-file list;
- the migration parent and the resulting migration head;
- the exact validation commands run and their results;
- gates that were **not** run, and why.

## The Complete Gate Sequence

This section defines when a branch counts as final. `docs/DEVELOPMENT.md` §6
carries the same commands as runnable setup instructions; nothing else in the
repository restates them.

Three things change together, in one commit: `.github/workflows/ci.yml`, the
block below, and `docs/DEVELOPMENT.md` §6. A stale copy is worse than no copy,
because someone will run it and believe the result.

**Step 0 — local-only pre-checks.** CI does not run these. They exist to catch
what parallel threads break in each other.

```bash
alembic heads                                              # exactly one head
cd extensions/salesnav-capture && npm install && npm test  # if extension code changed
cd -
```

Two migration heads means two threads created sibling migrations. Restack before
running anything else.

**Steps 1–7 — the CI sequence.** These are exactly the steps
`.github/workflows/ci.yml` runs, in the same order. A branch must not be
described as final until they pass locally on the final assembled head:

```bash
ruff check .
ruff format --check .
python -m mypy app
alembic upgrade head
alembic check
alembic downgrade base && alembic upgrade head    # reversibility round trip
python -m pytest
```

When `.github/workflows/ci.yml` changes, update this block in the same commit.

If the environment cannot run PostgreSQL, Ruff, or mypy, the thread must say:

> Integration incomplete; do not publish yet.

It must not hand over a supposedly final bundle and use CI as the formatter.
GitHub CI is confirmation, not the first complete test environment.

## No One-Gate Patches

When CI fails at an early gate, fixing that gate alone is not a correction. The
corrected head must pass every remaining gate locally before another push.

Where local reproduction is genuinely impossible:

1. create a temporary integration branch;
2. push the consolidated candidate once;
3. let CI expose everything reachable;
4. inspect the full diff against the entire toolchain;
5. produce one reviewed correction;
6. only then update the real stacked branch.

Sahil should not have to import and push a series of small corrective bundles.

## The Integration Worktree

Integration happens in a persistent worktree, not an ephemeral sandbox and not a
tree carrying unrelated local edits.

```
vmr-outbound/                      operator clone
vmr-outbound-<domain-a>/           implementation worktree
vmr-outbound-<domain-b>/           implementation worktree
vmr-outbound-integration/          authoritative integration worktree
```

The integration worktree assembles the stack in dependency order and is the only
place the final heads are produced.

## Stacked Pull Requests

Keep PRs stacked and merge in dependency order. For a backbone with three
dependent agents:

```
PR: backbone            base main
PR: <upstream agent>    base backbone
PR: <middle agent>      base <upstream agent>
PR: <downstream agent>  base <middle agent>
```

Parent merges before child. After each parent merges, the child PR is retargeted
or refreshed and its diff confirmed to contain only its owned changes. A child PR
whose diff has grown to include its parent's work has drifted and must be
restacked by the integration thread before review continues.

The PR operations themselves — creating, retargeting, refreshing, merging —
remain ChatGPT's, after Sahil's explicit approval. The integration thread
supplies the corrected branch and the merge order; it does not perform them.

## Stop Conditions

Stop and ask rather than proceed when:

- the recorded base SHA is no longer the tip of the base branch;
- two threads have produced conflicting migrations, or `alembic heads` reports
  more than one head;
- integration requires editing a file another thread owns;
- a downstream thread's tests only pass against an unfrozen upstream head;
- the environment cannot run the complete gate sequence.

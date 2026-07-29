# Engineering Agent Standard

This document defines the expected working method for coding agents operating
on VMR Outbound Agent.

## 1. Solve the observed workflow failure

An issue description and prior diagnosis are starting hypotheses, not guaranteed
complete explanations.

Before implementing:

1. Reproduce or trace the user-visible failure.
2. Verify the stated cause in the current code.
3. Inspect the complete lifecycle surrounding that cause.
4. Look for additional producers, consumers, cleanup paths, races, retries,
   startup effects, and persistence behavior that could recreate the same
   failure.
5. Implement against the full acceptance outcome rather than one named function.

When investigation reveals an additional causal defect, document it clearly and
include it in scope when it is necessary to make the requested outcome durable.

Do not silently expand into unrelated improvements.

## 2. Prefer invariants over patches

State the invariant the implementation should preserve.

Examples:

- persisted UI state must remain associated with the context that produced it;
- repeated execution must be idempotent;
- immutable capture evidence must not be destructively rewritten;
- ambiguous identity evidence must not cause an automatic merge;
- restored UI state must not create a backend mutation.

Prefer a model or boundary that expresses the invariant over timers, broad
guards, suppressed events, enlarged sticky-state lists, or special cases.

## 3. Test the real behavioral boundary

Use the closest practical production path:

- real services rather than duplicated logic;
- real panel and worker harnesses rather than isolated mock-only helpers;
- real database constraints when persistence behavior matters;
- complete lifecycle tests when startup, retries, navigation, restoration,
  concurrency, or cleanup are involved.

Test the operator-visible outcome, not only the implementation detail.

## 4. Prove regression tests detect the defect

For defect fixes, demonstrate that the new positive regression tests fail
against the pre-fix implementation for the expected reason.

Report separately:

- tests that fail before the fix;
- negative or guard tests that already pass before the fix;
- why each category is meaningful.

Do not weaken assertions merely to obtain a green suite.

## 5. Include adversarial and non-regression cases

Every defect fix should consider, where applicable:

- same context versus different context;
- first execution versus repeated execution;
- retry and idempotency;
- newer state versus older retained state;
- ambiguous or missing identifiers;
- legacy stored data;
- genuine navigation or state changes;
- no unintended backend mutation;
- failure and in-flight states;
- archive, deletion, and recovery behavior.

The fix must not satisfy the happy path by breaking adjacent valid behavior.

## 6. Preserve evidence and provenance

Do not destructively rewrite immutable acquisition evidence to make current state
look clean.

When a derived link, merge, identity resolution, or decision is introduced,
preserve:

- raw source values;
- source surface or acquisition path;
- decision reason;
- corroborating evidence;
- timestamp and actor;
- reversibility where required.

## 7. Separate automated verification from live acceptance

Report independently:

- unit and integration tests;
- full-suite results;
- migration and static-analysis checks;
- environment-specific artefacts;
- manual authenticated acceptance;
- checks that remain owed.

Never claim a manual scenario passed when it was not performed.

## 8. Investigate unexpected failures

Do not automatically label a failure as unrelated.

Reproduce it against unchanged `main` or an equivalent controlled baseline
before classifying it as:

- pre-existing;
- platform-specific;
- line-ending-related;
- environment-related;
- a genuine regression.

Document the reproduction.

## 9. Keep the implementation narrow

Do not begin adjacent issues without explicit authorization.

Avoid:

- opportunistic refactors;
- speculative abstractions;
- unrelated formatting changes;
- broad changes to global behavior;
- new infrastructure not needed by the acceptance criteria.

A newly discovered defect may be fixed in the same branch only when it is a
direct causal component of the requested outcome. Explain why.

## 10. Required handoff

Every implementation handoff must include:

- verified starting commit;
- branch and commits;
- root cause;
- any additional cause discovered;
- invariant used by the solution;
- files and boundaries changed;
- acceptance criteria mapped to tests;
- pre-fix regression proof;
- full test results;
- checks not run and why;
- live acceptance performed or still owed;
- deliberately deferred work;
- known limitations and migration behavior.

Plus the machine-verifiable evidence, which prose cannot replace:

- base SHA and head SHA;
- commit list and changed-file list;
- bundle path and SHA-256, with `git bundle verify` output;
- `git merge-base` proof against the declared base;
- `git diff --stat`, and `git range-diff` when the branch was rebased;
- migration parent and resulting migration head;
- exact validation commands and their results;
- gates that were not run, and why.

Before the session ends the work must survive as a pushed branch or a verified
bundle. A handoff describing commits that exist only in an ephemeral sandbox is
not a handoff.

## 11. Stop conditions

Stop and request guidance when:

- the issue’s requested behavior conflicts with current architecture;
- a destructive migration appears necessary;
- matching evidence is ambiguous;
- the implementation would weaken an established safety invariant;
- required credentials or authenticated acceptance are unavailable;
- the necessary change materially expands the product scope;
- the frozen base SHA supplied for the task is no longer the tip of the base
  branch;
- integration requires editing a file another thread owns;
- `alembic heads` reports more than one head after assembly;
- the environment cannot run the complete gate sequence.

## 12. Never convert assumptions into evidence

Planning language such as “assume this succeeds,” “proceed as though,” or
“expected to pass” must never be recorded as an observed test result.

Acceptance records must distinguish:

- directly observed operator evidence;
- automated test evidence;
- inferred or expected behavior;
- verification scheduled but not yet performed.

When an instruction appears to request recording an event that did not occur,
stop and ask whether the event was actually performed. Never fabricate an
acceptance observation merely to complete a workflow.

## 13. Integrate in one place, validate one tree

When several threads build related work at the same time, exactly one thread is
the integration authority. Implementation threads produce commits and bundles;
they do not restack each other's work and do not declare a dependent branch
final.

Work inside the declared ownership block and against the exact base SHA
supplied. Never follow "the latest branch".

A branch is final only when the gate sequence passes locally on the final
assembled head. `docs/PARALLEL_INTEGRATION.md` holds that sequence and is the
single definition of "final". Do not restate the list here — a copy is a copy
that will drift.

If the environment cannot run those gates, say `Integration incomplete; do not
publish yet`. CI is confirmation, not the first complete test environment.

When CI fails at one gate, fixing that gate alone is not a correction. Carry the
corrected head through every remaining gate before another push. A series of
one-gate patches is a defect in the working method, not progress.

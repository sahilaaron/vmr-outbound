# UAT-First Proportional Validation Standard

## Purpose

The project optimizes for the **shortest safe path to real UAT**.

A build, bug fix, review, test run, documentation task, or CI repair is useful only
if it moves the currently authorized product closer to a verified operator
outcome. Safety remains non-negotiable, but process that does not materially
reduce the risk of the changed behavior is itself a delivery defect.

The default question is:

> What is the smallest credible path from this exact change to UAT?

Do not ask what the maximum possible validation programme could be.

## UAT-first delivery mandate

Unless a concrete blocker prevents it, every active delivery should move through
this sequence:

1. identify the exact user-visible or operational outcome required for UAT;
2. reproduce the current blocker or define the acceptance criterion;
3. make the smallest complete change that removes that blocker;
4. run the narrowest local proof that can catch a defect in that change;
5. push promptly;
6. let authoritative GitHub CI provide broad repository regression coverage;
7. repair only concrete CI or review failures caused by the candidate;
8. merge once the required gate is green;
9. deploy promptly;
10. run the real UAT step that the change was meant to unblock.

**UAT is the destination, not an activity postponed until every possible review,
cleanup, refactor, or optional test has finished.**

When the candidate is safe enough for its risk tier and the remaining work is
non-blocking, record the remaining work as deferred and continue toward UAT.

## Core rule for narrow fixes

For a small, well-understood, low-blast-radius defect fix, the default path is:

1. reproduce the reported failure;
2. make the smallest correct fix;
3. run the directly affected test or file;
4. run touched-file lint/format/type checks where relevant;
5. **push immediately**;
6. let CI validate the broader repository;
7. deploy;
8. verify the original user-visible failure is gone.

Do not locally recreate a broad CI shard merely because CI will run it. Do not
wait on an optional full suite before pushing a narrow fix when focused proof is
green and authoritative CI is available.

A focused fix may legitimately move from diagnosis to pushed candidate in
minutes. Process must not dominate implementation time without a named risk that
justifies the delay.

## Time discipline

Time is a release constraint.

For a narrow deterministic fix with a known cause:

- diagnosis + implementation should normally be measured in minutes;
- local validation should normally be the failing test/file plus directly
  relevant static checks;
- if an optional local suite would take materially longer than the fix itself,
  prefer pushing and letting CI run it;
- do not hold a ready candidate for an optional test that CI already performs;
- do not spend an hour proving a one-line test-fixture repair before pushing it.

These are operating defaults, not artificial deadlines. A concrete security,
data-loss, spend, migration, or sending risk can justify more time. State that
risk explicitly when it does.

## Validation tiers

### Tier 1 — Narrow defect / presentation / local behavior

Examples:

- CSS or static asset failure;
- incorrect template output;
- typo or copy defect;
- isolated UI behavior;
- test-fixture adaptation after an intentional production guard;
- small deterministic bug with a clear reproduction and narrow code path.

Default gate:

- focused regression test/file;
- touched-file lint/format where relevant;
- push;
- CI;
- live verification of the reported behavior when applicable.

Independent review is not required by default.

### Tier 2 — Material application behavior

Examples:

- workflow-state changes;
- persistence or reconciliation behavior;
- campaign eligibility;
- data mutation with meaningful user impact;
- integration behavior with retries/idempotency consequences.

Default gate:

- focused regression proof;
- relevant integration/non-regression suites;
- standard repository quality gates that directly apply;
- push;
- CI;
- one targeted review when the changed behavior is materially difficult to
  reason about;
- live or controlled UAT when the behavior is operational.

Do not automatically require multiple review agents.

### Tier 3 — High-risk boundary

Examples:

- authentication or authorization architecture;
- secret/token handling;
- sending authority;
- suppressions or legal eligibility;
- destructive migrations;
- externally reachable trust-boundary changes;
- payment/cost authority;
- code execution or privileged infrastructure controls.

Default gate may include:

- adversarial tests targeted at the changed boundary;
- one independent review;
- broader regression suites;
- controlled deployment;
- explicit rollback proof;
- live acceptance before declaring completion.

Even here, successor repairs are reviewed by **delta**, not by restarting the
entire parent review. Do not repeatedly re-review already accepted architecture
when the successor changes one well-covered behavior unless new evidence warrants
it.

## Sensitive files do not automatically make every edit high-risk

Classify the change by the behavior it changes, not merely by the filename or
module it touches.

A one-line correction inside middleware may be narrow if:

- the existing trust decision is unchanged;
- the fix only publishes an already-validated value downstream;
- regression tests pin trusted and untrusted behavior;
- adjacent security invariants remain unchanged.

Conversely, a visually small diff can still be Tier 3 if it changes who is
trusted, what is authorized, or what side effect can occur.

Review the semantic delta.

## CI is the broad regression authority

GitHub CI exists to run broad repository gates on the target platform.

For Tier 1 and ordinary Tier 2 successor fixes:

- local work proves the changed behavior;
- CI proves broad non-regression;
- do not duplicate CI locally unless a concrete failure cannot be understood
  without reproducing its exact grouping;
- when CI fails, inspect the **failed job and exact failing tests first**;
- if the failure is deterministic and candidate-caused, make the narrow repair;
- if logs prove a known infrastructure flake, rerun the failed job rather than
  changing product code;
- do not launch unrelated investigations because one CI shard failed.

If CI prints a reproducible command and the failure is already understood from
its traceback, running the single failing test/file locally is normally enough
before the next push.

## Review policy

A substantial new security or product boundary may receive one broad independent
review.

After that:

- successor fixes receive **delta-only review** focused on the substantiated
  findings and directly affected regressions;
- do not launch a second whole-branch adversarial review merely because the first
  review found repairable issues;
- do not review the review;
- do not require a new specialist track for every follow-up test failure;
- if a review finds no concrete release blocker, proceed toward CI/merge/UAT.

A reviewer may record HIGH/MEDIUM/LOW follow-ups without blocking UAT. Only a
finding that invalidates the authorized UAT path, safety boundary, data
integrity, spend boundary, or deployability should block the current release.

## Escalation triggers

Escalate beyond the default tier only when there is concrete evidence of one or
more of the following:

- the root cause remains uncertain;
- the fix changes a security/trust decision rather than merely consuming one;
- the blast radius is materially broader than first believed;
- focused tests cannot represent the real failure;
- CI or relevant regression tests reveal unexplained failures;
- the change is difficult to reverse;
- a live failure could send mail, expose data, corrupt state, lose evidence,
  create uncontrolled provider spend, or bypass eligibility/authentication;
- reviewer or operator evidence identifies a reproducible candidate-caused
  blocker.

State the trigger explicitly. Do not escalate on vague discomfort.

## De-escalation rule

When investigation proves a reported incident is narrower than initially feared,
reduce the process immediately.

Examples:

- once a hosted-auth defect is proven to be only wrong-scheme static URLs, the
  successor CSS/static fix is not another hosted-auth security programme;
- once a CI failure is proven to be a test fixture that now violates an
  intentional production guard, repair that fixture and push — do not re-audit
  the production guard;
- once a review blocker is fixed and the delta is small, re-review the delta,
  not the entire branch.

## Local full-suite rule

A full local suite is not automatically required for a narrow successor fix.

Prefer:

- failing test/file locally;
- directly neighboring tests where the changed contract crosses a boundary;
- authoritative CI for repository-wide coverage.

Run a local full suite only when:

- CI does not provide equivalent coverage;
- the change is Tier 3 and local full-suite evidence materially reduces risk;
- a failure appears only in combined local execution and the combination itself
  is relevant;
- or Sahil explicitly requests it.

Never hide an unrun suite. State it plainly, then proceed when the required gate
is satisfied.

## Deployment and UAT rule

Once the required tier-specific evidence and CI are green, move to deployment
without inventing new gates.

Deployment is not the end of delivery. The next action should be the smallest
real UAT proof of the capability just unblocked.

Examples:

- refresh the actual hosted page after a static fix;
- perform the real sign-in after an auth fix;
- import one real contact after an enrolment fix;
- create one real Gmail draft after Gmail configuration;
- run one controlled provider verification when the provider boundary is the
  acceptance target.

Do not postpone an easy live check behind speculative additional analysis.

## Stop-the-process rule

Before adding another review, local suite, specialist, document pass, or
investigation, ask:

> What specific failure could this step catch that the already planned focused
> proof + CI + UAT would not catch?

If there is no concrete answer, do not add the step.

If an optional process step is delaying UAT and has no named risk-reduction
purpose, stop it and proceed with the required gate.

## Responsibilities

### Builder

- implement the smallest complete repair;
- prove the changed behavior locally;
- push promptly when the focused proof is green;
- do not self-expand the task into adjacent cleanup or research;
- stop and report before touching a second scope when a tightly bound repair
  cannot be completed within its authorized files/behavior.

### Coordinator

The coordinator is accountable for **time-to-UAT as well as safety**.

In particular:

- keep prompts tightly bounded;
- do not ask builders to duplicate CI locally;
- do not reflexively request independent review for every fix;
- do not turn a tiny successor patch into a repeat of the parent feature's full
  review;
- do not wait on optional test runs after the required local proof is already
  satisfied;
- inspect only the failing CI job before widening investigation;
- prefer delta review after a broad review;
- keep deferred findings out of the active release unless they block UAT;
- move a green merged build into deployment/UAT promptly.

When choosing a heavier process than this document's default, record the concrete
reason.

### Reviewer

- attack the changed behavior and named release risks;
- distinguish release blockers from deferred hardening;
- do not widen a delta re-review into a fresh product audit without new evidence;
- return PASS once the authorized UAT path is safe, even if non-blocking follow-up
  work remains.

## Worked example: one-line CI fixture repair

Observed failure:

- a new production readiness guard intentionally refuses enrolment into an unsafe
  running sequence campaign;
- an old display-safety fixture creates exactly that unsafe state;
- 12 unrelated display tests fail before reaching their assertions.

Correct path:

1. confirm the failures share that fixture;
2. change the fixture so execution is off during import while preserving sequence
   configuration and every display assertion;
3. run that test file;
4. run touched-file lint/format;
5. push;
6. let CI rerun the full shard;
7. do not locally wait 20–30 minutes for the same broad shard before pushing.

The production guard is not re-reviewed because the CI failure did not challenge
its correctness.

## Worked example: static assets behind HTTPS

Observed failure:

- authenticated page loads;
- CSS/JS do not;
- generated static URLs use `http://` on an HTTPS page;
- direct HTTPS static requests return 200.

If diagnosis shows the application already made the correct trusted-proxy scheme
decision and the fix only makes that already-trusted scheme visible to URL
generation, handle the successor patch as:

1. focused code fix;
2. trusted/untrusted scheme regression tests;
3. directly relevant auth/static/web tests;
4. push;
5. CI;
6. merge and deploy;
7. refresh the real page and continue UAT.

Do not repeat the full hosted-auth adversarial review unless the patch changes
who or what is trusted.

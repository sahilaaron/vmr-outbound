# Proportional Validation Standard

## Purpose

Verification must be proportional to the change being made.

The project values safety, but safety work that is disconnected from the actual
blast radius is itself a delivery defect. A narrow production bug must not be
turned into a multi-agent audit programme merely because it happens to sit near
security-sensitive code.

The default question is:

> What is the smallest validation path that gives credible evidence this exact
> change is safe to ship?

Do not ask what the maximum possible validation process could be.

## Core rule

For a small, well-understood, low-blast-radius defect fix, the default path is:

1. reproduce the reported failure;
2. make the smallest correct fix;
3. add or run focused regression tests that fail before the fix and pass after;
4. run directly relevant non-regression checks;
5. let CI validate the repository on the target platform;
6. deploy;
7. verify the original user-visible failure is gone.

Do not add independent adversarial review, repeated full-suite runs, architecture
re-review, mutation campaigns, or additional handoff cycles unless the change
itself creates a concrete reason for them.

A focused fix may legitimately move from diagnosis to live verification in
minutes. Process must not dominate implementation time without a specific risk
justification.

## Validation tiers

### Tier 1 — Narrow defect / presentation / local behavior

Examples:

- CSS or static asset failure;
- incorrect template output;
- typo or copy defect;
- isolated UI behavior;
- small deterministic bug with a clear reproduction and narrow code path.

Default gate:

- focused regression test;
- directly adjacent tests;
- lint/format checks where changed files require them;
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
- standard repository quality gates;
- CI;
- one targeted review when the change is materially difficult to reason about;
- live or controlled acceptance when the behavior is operational.

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

- adversarial tests;
- independent review;
- broader regression suites;
- controlled deployment;
- explicit rollback proof;
- live acceptance before declaring completion.

Even here, review depth must target the changed boundary. Do not repeatedly
re-review already accepted architecture when a successor patch changes one
well-covered line unless new evidence warrants it.

## A sensitive file does not automatically make every edit high-risk

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

## Escalation triggers

Escalate beyond the default tier only when there is concrete evidence of one or
more of the following:

- the root cause remains uncertain;
- the fix changes a security/trust decision rather than merely consuming one;
- the blast radius is materially broader than first believed;
- focused tests cannot represent the real failure;
- CI or relevant regression tests reveal unexplained failures;
- the change is difficult to reverse;
- a live failure could send mail, expose data, corrupt state, lose evidence, or
  bypass eligibility/authentication;
- reviewer or operator evidence identifies a reproducible candidate-caused
  blocker.

State the trigger explicitly. Do not escalate on vague discomfort.

## De-escalation rule

When investigation proves a reported incident is narrower than initially feared,
reduce the process accordingly.

For example, if live UAT proves authentication succeeds and the remaining defect
is only wrong-scheme static URLs, treat the remaining work as the narrow defect
it actually is. Do not carry forward the full authentication review process into
the CSS/static successor fix unless that successor changes the auth boundary.

## Review-loop limit

Do not create review loops for their own sake.

For a Tier 1 or ordinary Tier 2 fix:

- one implementation pass;
- one focused review at most when justified;
- CI;
- deploy and verify.

If a review finds no concrete blocker, proceed. Do not launch another review to
review the review.

If the same agent built the change, its self-review is useful evidence but not
an independent review. That does not imply an independent review is mandatory;
it is required only when the change's tier or concrete risk calls for one.

## Full-suite rule

A full local suite is not automatically required for every narrow successor fix.

Prefer:

- focused tests locally;
- relevant neighboring suites locally;
- authoritative CI for the complete repository when CI provides that coverage.

Do not spend an hour waiting for a platform-specific local full suite when the
relevant focused gates are green and the authoritative Linux CI is about to run,
unless the change has a specific risk that only the full local suite can expose.

Never hide an unrun or incomplete suite. State it plainly.

## Deployment rule

Once the required tier-specific evidence is green, move to deployment without
inventing new gates.

For a production defect whose acceptance criterion is directly observable, the
post-deploy check is part of the proof. Examples:

- CSS loads normally;
- the page renders correctly;
- login/logout behaves correctly;
- the expected API status is returned;
- a previously failing workflow succeeds.

Do not postpone an easy live check behind speculative additional analysis.

## Responsibilities

The coordinator is responsible for keeping the process proportional.

In particular:

- do not reflexively request independent review for every fix;
- do not turn a tiny successor patch into a repeat of the parent feature's full
  security review;
- do not wait on optional test runs after the required gate is already satisfied;
- do not ask the operator to perform process steps that do not materially reduce
  risk;
- prefer the shortest safe path to a verified live outcome.

When choosing a heavier process than this document's default, record the concrete
reason.

## Worked example: static assets behind HTTPS

Observed failure:

- authenticated page loads;
- CSS/JS do not;
- generated static URLs use `http://` on an HTTPS page;
- direct HTTPS static requests return 200.

If diagnosis shows the application already made the correct trusted-proxy
scheme decision and the fix only makes that already-trusted scheme visible to
URL generation, the successor patch should normally be handled as:

1. focused code fix;
2. trusted/untrusted scheme regression tests;
3. relevant auth/static/web tests;
4. CI;
5. merge and deploy;
6. refresh the real page and confirm styling;
7. finish the remaining login/logout UAT.

Do not repeat the full hosted-auth adversarial review unless the patch changes
who or what is trusted.

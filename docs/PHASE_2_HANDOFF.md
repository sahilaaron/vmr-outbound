# Phase 2 build handoff — Issue #137 (email intelligence + MillionVerifier)

> **Extension (provider-neutral usage/cost ledger).** A follow-up commit adds a
> provider-neutral `usage_ledger_entries` table: every MillionVerifier request (and
> every cache hit that avoids one) records provider, operation, campaign, a soft
> job/request reference, attempted time, result, cache status, retry number, units,
> estimated cost, provider-reported cost (when available), currency, and whether the
> charge is confirmed/uncertain/none. An interrupted job records an *uncertain*
> charge. A compact "MillionVerifier usage & cost" card on `/verification` shows
> calls, cache savings, failures, estimated spend, remaining credits, and projected
> cost to finish the active batch. The ledger is provider-neutral so future
> research/AI/enrichment/Saleshandy usage reuses the same table with no schema
> replacement; the full multi-provider finance dashboard is intentionally out of
> scope. Migration `ad1e298fb49a` (reversible). New tests: `tests/test_usage_ledger.py`.
> Totals after this extension: **361 tests pass**. See `docs/PHASE_2.md` →
> "Provider-neutral usage/cost ledger".

**Not an acceptance.** This is a factual build handoff. ChatGPT independently
inspects the remote PR and issue evidence and issues the verdict; Sahil approves
merges. Claude does not grade its own work or declare Phase 2 complete.

## Branch & commit

* Branch: `feat/issue-137-email-verification`
* Commit SHA: `9d32f32119e14066aa5df277ad6dd3b07e4cd5f4`
* Base: `main` @ `7be42cd3d2310426b6bd54cd75954d9b1eba222a`
* Committer identity: `Sahil Aaron <sahilaaron19o@gmail.com>` (repo-configured; no
  AI/tool attribution). Unsigned (no signed-commit policy in effect).
* Not pushed — this environment has no GitHub credentials. Standard bundle
  handoff prepared: `vmr-outbound-issue-137.bundle` (delivered separately).

### Apply the bundle and push (bridge step)

```bash
# from your local clone of vmr-outbound, on main and up to date
git fetch origin && git checkout main && git pull
git bundle verify /path/to/vmr-outbound-issue-137.bundle
git fetch /path/to/vmr-outbound-issue-137.bundle \
  feat/issue-137-email-verification:feat/issue-137-email-verification
git push -u origin feat/issue-137-email-verification
```

## Files & migrations changed (54 files, +4827 / −22)

New backend: `app/services/email/{normalization,patterns,candidates}.py`,
`app/services/verification/{provider,policy,mapping-in-policy,status,queue,service,usage,console}.py`,
models `app/models/{email_candidate,verification_job,verification_usage}.py`.
Modified: `app/models/{enums,email_evidence}.py`, `app/core/config.py`,
`app/db/base.py`, `app/web/routes.py`, templates
(`base, contacts, contact_detail, _macros, verification`), `app/web/static/app.css`.
Migration: `migrations/versions/93c46f2a8df9_*.py` (one reversible revision).
Docs: `README.md`, `.env.example`, `docs/{DEVELOPMENT,PHASE_2,VERIFICATION_RUNBOOK}.md`.
Demo: `scripts/phase2_verification_demo.py`. Tests: 9 new files (74 tests).
Evidence: `docs/phase2_evidence/*.png` (12 synthetic-data screenshots).

## Exact commands run & results

| Command | Result |
| --- | --- |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 134 files already formatted |
| `python -m mypy app` (strict) | Success: no issues in 65 source files |
| `alembic upgrade head` | OK (applied `93c46f2a8df9`) |
| `alembic check` | No new upgrade operations detected |
| `alembic downgrade base && alembic upgrade head && alembic check` | Clean round trip; no new operations |
| `python -m pytest` (local Postgres, UTF-8) | **354 passed** (280 prior + 74 new) |

Database: local PostgreSQL 16, UTF-8, on `127.0.0.1:5433` per the default
`DATABASE_URL`. Development RDS was **not** used or reintroduced.

## Migration upgrade/downgrade evidence

`93c46f2a8df9` adds `email_candidates`, `verification_jobs` (with a partial
unique index enforcing ≤1 active job per address), `verification_usage_events`,
five evidence signal columns on `exact_email_verifications`, and the `DISPOSABLE`
value on the `email_verification_result` enum. Downgrade drops the new tables,
columns, and the three new enum types; the additive `DISPOSABLE` value is
intentionally not removed (PostgreSQL cannot drop one enum value; the whole type
is dropped on a downgrade-to-base). The full `downgrade base → upgrade head`
round trip is clean and `alembic check` reports no drift.

## Browser evidence (synthetic data only)

`docs/phase2_evidence/`: contacts list with all four icons; verification console
(paid vs billed calls, cache reuse, exceptions, credit balance, usage log);
contact-detail panels for each state — successful (valid), failure (invalid),
warning (catch-all / unknown / disposable / role-based / insufficient-credits),
pending (retry-scheduled / unverified); and a mobile-viewport detail. Captured
with Chromium/Playwright against a running uvicorn on local Postgres.

## Child-issue → implementation & tests

See `docs/PHASE_2.md` for the full table. Summary:

| Issue | Where | Tests |
| --- | --- | --- |
| #23 EML-001 normalize names/domains | `services/email/normalization.py` | `test_email_normalization.py` |
| #24 EML-002 versioned generation | `services/email/patterns.py`, `models/email_candidate.py` | `test_email_normalization.py`, `test_email_candidates.py` |
| #25 EML-003 separate fact tables | `models/email_evidence.py`, `models/email_candidate.py` | `test_verification_status.py`, `test_verification_service.py` |
| #26/#28 EML-006 expose intelligence | `services/verification/console.py`, templates | `test_verification_web.py` |
| #30 EML-004 rank via evidence | `services/email/candidates.py` | `test_email_normalization.py` |
| #27 VER-002 map outcomes | `services/verification/policy.py` | `test_verification_policy.py` |
| #29 VER-003 cache + freshness | `policy.py`, `service.py` | `test_verification_policy.py`, `test_verification_service.py` |
| #32 VER-001 adapter | `services/verification/provider.py` | `test_verification_provider.py`, `test_verification_secrets.py` |
| #33 VER-004 conservative catch-all | `status.py`, `policy.py` | `test_verification_status.py` |
| #34 VER-005 rate/retry/idempotency | `models/verification_job.py`, `queue.py`, `service.py` | `test_verification_queue.py`, `test_verification_service.py` |
| #35 VER-006 usage/exceptions | `models/verification_usage.py`, `usage.py`, `console.py` | `test_verification_service.py`, `test_verification_web.py` |

Scenario proofs (tests): all four visual states + accessibility
(`test_verification_status.py`, `test_verification_web.py`); imported-email and
generated-candidate paths (`test_email_candidates.py`); cache reuse, stale
evidence, provider failure + retry, insufficient credits, interrupted-job
recovery (`test_verification_service.py`, `test_verification_queue.py`);
duplicate concurrent requests → max one paid call
(`test_verification_service.py::test_duplicate_enqueue_makes_max_one_paid_call`
plus the DB-level `test_active_email_partial_unique_blocks_duplicate_job`);
secret redaction (`test_verification_secrets.py`). No default test makes a live
network call.

## Known limitations & manual actions

* **Live smoke test is the only manual acceptance item** (VER-007). All documented
  provider outcomes are proven offline; one deliberate live request with a real
  key confirms credentials + mapping end to end. Steps in
  `docs/VERIFICATION_RUNBOOK.md`.
* The local operator "run pending jobs" processes the queue synchronously on
  demand (correct for a single-operator local tool). A standalone always-on
  worker process is not built (OPS-007 is post-pilot); the queue is designed for
  one (leases, idempotency, recovery) so it drops in later without schema change.
* Conflict detection compares the two most recent fresh results for an address; a
  richer multi-provider reconciliation is out of scope for Phase 2.
* Feature switches remain **off** by default; nothing here schedules or sends
  email, and dry-run stays on.

## Exact API-key configuration instruction (for Sahil)

Add this line to your **local, git-ignored** `.env` and restart the app — do not
put the key anywhere else (source, fixtures, screenshots, logs, commits, tracker):

```
MILLIONVERIFIER_API_KEY=your-real-key-here
```

Without it, the whole pipeline still runs on a deterministic, network-free
simulator. The documented test keys also route to the simulator.

## Proposed Phase 2 tracker update — "When can we go live?"

For ChatGPT to enter on the `02 — Email Verification` tab (Claude proposes; ChatGPT
verifies and owns the official Sheet):

* **Phase status:** Ready for review.
* **Go-live readiness:** Conditional.
* **Current answer:** "The Phase 2 email-verification path is code-complete and
  verified offline against local Postgres: candidate generation, safe mapping,
  policy-versioned caching, an idempotent recoverable queue, and truthful
  four-state status. Catch-all/unknown/provider-error/insufficient-credit states
  are demonstrably never shown as valid, and duplicate concurrent work cannot
  cause a duplicate paid call. Remaining before this phase is 'Yes': (1) ChatGPT's
  independent PR/CI review and verdict; (2) the single manual MillionVerifier live
  smoke test with a real key to confirm credentials and real-world mapping and to
  measure real per-call cost/credit burn. No calendar date is committed until the
  live smoke test and expected pilot verification volume/cost are confirmed by
  Sahil."
* **Forecast confidence:** Medium — approach is proven in code and tests; the
  untested dependency is the live provider (credentials, real-world outcome
  distribution, and per-call cost).
* **Critical blockers:** none code-side; gating items are the ChatGPT review and
  the live smoke test (owner: Sahil for the key + run).
* **Current build:** branch `feat/issue-137-email-verification` @ `9d32f32`
  (pending push + PR).

## Proposed PR

**Title:** `Phase 2: email-pattern intelligence and MillionVerifier verification (#137)`

**Body:**

> Delivers the complete Phase 2 email-intelligence path behind
> `FEATURES__EMAIL_GENERATION` and `FEATURES__MILLIONVERIFIER` (both default off):
> deterministic versioned name/domain normalization and ranked, transparent
> candidate generation; a replaceable, fully offline-testable MillionVerifier
> Single API adapter with a network-free simulator; a policy-versioned
> exact-address cache; a Postgres-backed idempotent, retry-safe, recoverable
> verification queue; and a compact, accessible four-state status icon
> (Pending / Successful / Failure / Warning) beside every prospect email that
> preserves the precise underlying states.
>
> Provider errors, insufficient credits, timeouts, unknown results, and catch-all
> addresses stay visibly uncertain and never look like an invalid mailbox or a
> verified address. Exact-address evidence, domain-pattern observations, and
> mail-domain observations stay structurally distinct; a pattern never verifies a
> different mailbox. Duplicate concurrent requests cannot cause a duplicate paid
> call (unique idempotency key + partial unique active-email index).
>
> One reversible migration; local Postgres only (no dev RDS). 74 new tests
> (354 total pass); ruff + ruff format + strict mypy clean; migration
> upgrade/downgrade round trip clean. Browser evidence and a synthetic demo
> included. The single MillionVerifier live smoke test with a real key is the only
> manual acceptance item.
>
> Coordinating issue: #137.
> Implements (pending independent verification before any issue is closed):
> #23, #24, #25, #26, #28, #30, #27, #29, #32, #33, #34, #35.
> Explicitly out of scope: #31 (EML-007), #132 (VER-010).
>
> Closing language for child issues is intentionally deferred: ChatGPT confirms
> each child issue's complete outcome against the remote build before closing.

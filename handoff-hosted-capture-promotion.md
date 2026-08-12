# Build handoff — hosted capture promotion boundary

## Branch and commit

| | |
| --- | --- |
| Repository | `sahilaaron/vmr-outbound` |
| Branch | `feat/hosted-capture-promotion` |
| Base SHA | `8720b7f4cbda8e9b193551355998ecfc363987be` (`main`) |
| Head SHA | `b90f8e448b01fef803283ba328acc87c6d546916` |
| On GitHub | **Yes** — pushed; `git ls-remote origin refs/heads/feat/hosted-capture-promotion` returns the head SHA above |
| `git merge-base HEAD origin/main` | `8720b7f4cbda8e9b193551355998ecfc363987be` — exactly the declared base |
| Commits | 1 — `b90f8e44 Let staging promote captures, and only when it really can` |
| Bundle | Not produced. The branch is on the remote, which is the durable artefact `docs/PARALLEL_INTEGRATION.md` asks for. |
| Migration | **None.** `alembic heads` reports one head, `0926b59b7912`, unchanged from base. |

No PR was opened — that is ChatGPT's operation.

## Files changed

```
 .env.example                           |   3 +
 app/core/features.py                   |   7 +
 app/core/runtime.py                    | 107 ++++-
 deploy/vmr.env.example                 |  47 ++-
 docs/CAPTURE_PROMOTION.md              |  27 +-
 docs/STAGING_RUNBOOK.md                |  16 +-
 tests/test_capture_promotion.py        |  10 +-
 tests/test_hosted_capture_promotion.py | 752 +++++++++++++++++++++++++++++++++
 8 files changed, 948 insertions(+), 21 deletions(-)
```

Exactly one file carries behaviour: `app/core/runtime.py`. Everything else is
the new test file, feature/doc comments, and the environment examples.

## The exact validation logic introduced

In `app/core/runtime.py`:

1. `contact_capture_promotion` was **removed from `_LOCAL_ONLY_FEATURES`** and
   replaced by an explicit conditional policy, not by a deletion. The three
   remaining local-only intakes (`salesnav_intake`, `linkedin_profile_intake`,
   `linkedin_company_intake`) are unchanged and still refused hosted.

2. New module constants:

   ```python
   _STAGING_PROMOTION_ENVIRONMENTS = frozenset({"staging"})

   _PROMOTION_REQUIRED_FEATURES = (
       ("automatic_company_domain_resolution", "FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION"),
       ("salesnav_domain_enrichment", "FEATURES__SALESNAV_DOMAIN_ENRICHMENT"),
   )
   ```

3. New function `_hosted_promotion_issues(settings, *, environment)`, called
   from `validate_runtime_settings` **only inside the existing
   `if environment in _PRODUCTION_LIKE:` block**, so local, development, test
   and ci never reach it:

   | Condition | Result |
   | --- | --- |
   | `features.contact_capture_promotion` is false | `[]` — allowed, unchanged |
   | environment not in `{"staging"}` (i.e. production) | one issue naming `FEATURES__CONTACT_CAPTURE_PROMOTION`, "authorised for staging only"; returns immediately |
   | staging, `FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION` false | one issue naming that variable |
   | staging, `FEATURES__SALESNAV_DOMAIN_ENRICHMENT` false | one issue naming that variable |
   | staging, `settings.has_logo_dev_key()` false | one issue naming `LOGO_DEV_API_KEY` |
   | staging, all four present | `[]` — allowed |

   Issues accumulate into the existing `RuntimeConfigurationError`, so one
   restart reports every missing value. The production refusal deliberately
   short-circuits: listing prerequisites under a refusal no prerequisite can
   lift reads as a checklist.

4. Secrets: the checks read `settings.has_logo_dev_key()`, which returns a
   boolean. No message contains a key, and `logo_dev_api_key` keeps its existing
   `repr=False, exclude=True`. Proven by test.

### Enforcement reach

`validate_runtime_settings` is called by `app/main.py:48` (`create_app`) **and**
`app/db/session.py:31` (`create_db_engine`). The agent worker
(`scripts/run_agent_worker.py`) imports `app.db.session`, so the worker process
— the one that actually runs `resolve_pending` — is refused by the same rule. A
half-configured staging box cannot start the API or the worker.

### What was NOT changed

- No change to the promotion service, the resolution service, the pending
  worker, the campaign-filing service, or the pipeline. **No second promotion
  path exists.**
- No change to extension bearer auth, hosted Google auth, Gmail, or Sending.
- No change to the contact-first model, DAT-014 semantics, or DAT-017A states.
- No migration, no schema change, no new dependency, no new paid service.

## Focused test counts

New file `tests/test_hosted_capture_promotion.py` — **21 test functions, 24
collected cases** (one is parametrized over the four development environments).
All 24 pass.

Coverage against the twelve required scenarios:

| # | Required scenario | Test |
| --- | --- | --- |
| 1 | staging + promotion disabled → allowed | `test_staging_with_promotion_off_starts_exactly_as_it_did` |
| 2 | staging + promotion on, domain-resolution flag missing → refused | `test_staging_refuses_promotion_without_automatic_domain_resolution` |
| 3 | staging + promotion on, enrichment flag missing → refused | `test_staging_refuses_promotion_without_the_provider_switch` |
| 4 | staging + promotion on, provider key absent → refused | `test_staging_refuses_promotion_without_a_provider_key` |
| 5 | staging + all prerequisites → allowed | `test_staging_with_every_prerequisite_starts`, `test_a_complete_staging_promotion_configuration_builds_the_application` |
| 6 | production + same config → refused | `test_production_refuses_the_same_beta_promotion_configuration`, `test_production_refuses_promotion_even_with_nothing_else_configured` |
| 7 | local behaviour unchanged | `test_local_behaviour_is_unchanged` (local/development/test/ci) |
| 8 | pending capture created before enablement can later promote | `test_a_capture_accepted_while_promotion_was_unavailable_promotes_later` |
| 9 | valid explicit campaign filing → exactly one CampaignContact | `test_an_explicit_campaign_request_becomes_exactly_one_campaign_contact` |
| 10 | retry remains idempotent | `test_a_second_pass_creates_no_second_contact_and_no_second_membership` |
| 11 | no Contact when domain resolution stays unresolved | `test_an_unresolved_domain_creates_no_contact_at_all` |
| 12 | secrets never in diagnostics/settings output | `test_no_secret_ever_appears_in_a_refusal_or_a_settings_dump` |

Additional tests beyond the required twelve:

- `test_staging_reports_every_missing_prerequisite_at_once`
- `test_the_other_intakes_did_not_move_with_promotion`
- `test_the_pending_worker_does_nothing_while_a_prerequisite_is_missing` —
  fail-closed at the point of use, and the capture is left *untouched* (no
  recorded non-decision), which is what keeps it recoverable
- `test_a_fresh_hosted_capture_promotes_and_files_inside_the_request`
- `test_no_campaign_membership_is_invented_for_a_capture_that_requested_none`
- `test_enrollment_completes_capture_and_queues_identity_and_nothing_further`
- `test_a_paused_campaign_still_enrolls_but_holds_every_stage_after_capture`

## Pending capture recovery — what was actually verified

The 44 captures and 24 pending filings are recoverable **without editing any
database row**, and the tests reproduce the state rather than assuming it:

1. A capture is staged with promotion unavailable. Asserted: 0 Contacts, 0
   Companies, 0 CampaignContacts, `matched_contact_id` null, filing `PENDING`
   with the correct `requested_campaign_id` and null `campaign_contact_id`, and
   — critically — **zero `CompanyDomainResolution` rows for that capture**. A
   recorded decision is what would make it unrecoverable, because
   `pending_capture_ids` skips anything already decided.
2. The four prerequisites are supplied. Nothing else is touched.
3. `pending.resolve_pending()` — the existing worker pass, reached in production
   through `scripts/run_agent_worker.py::_resolve_pending_captures` — considers
   the capture, resolves the domain, and promotes it.
4. Result: 1 Contact, 1 Company, 1 CampaignContact; filing `APPLIED` with
   `campaign_contact_id` set and `applied_at` populated.

Campaign filing verified: correct campaign UUID (a second decoy campaign exists
and is not used), `source_capture_id` matches the capture, exactly one
membership, filing leaves `PENDING`, and a repeat `promote()` returns
`ALREADY_PROMOTED` with identical counts and the same membership id.

Pipeline handoff verified: `latest_completed_stage is CAPTURE`, `next_stage is
IDENTITY`, the Capture stage row is `COMPLETED`, the Identity stage row exists
and is not completed, and **no stage row exists for Company, Research, Email,
Verification, Insights, Personalization or Sending** — nothing is jumped or
fabricated. With `campaign.execution_enabled=False` the membership still exists
and Capture is still complete, while Identity is neither queued nor auto-skipped
and `effective_control(...)` reports `DISABLED` from the campaign execution
source — the Pause/Resume gate is intact.

## Validation commands run, and their results

| Gate | Command | Result |
| --- | --- | --- |
| 0 | `alembic heads` | `0926b59b7912 (head)` — exactly one |
| 1 | `ruff check .` | **All checks passed!** |
| 2 | `ruff format --check .` | **505 files already formatted** |
| 3 | `python -m mypy app` | **Success: no issues found in 253 source files** |
| 4 | `alembic upgrade head` | OK |
| 5 | `alembic check` | **No new upgrade operations detected** |
| 6 | `alembic downgrade base && alembic upgrade head` | OK — run against a scratch `vmr_revcheck` database so the operator's local `vmr_dev` was not destroyed; ends at `0926b59b7912 (head)` with `alembic check` clean |
| 7 | `python -m pytest` | **NOT run to completion locally** — see below |
| — | extension `npm test` | Not run; no extension code changed |

### Gate 7 — stated plainly

The complete local suite was **not** run to completion. It executes at roughly
12 tests/minute on this Windows machine (~2,900 tests, ≈4 hours), and the task
brief directed proportional validation — focused tests plus relevant regression
tests plus lint/type checks plus CI. What was run instead:

| Set | Suites | Result |
| --- | --- | --- |
| Focused | `tests/test_hosted_capture_promotion.py` | **24 passed / 24** |
| A — boundary | `test_production_hardening`, `test_extension_capture_auth`, `test_hosted_auth`, `test_hosted_auth_raw_asgi`, `test_hosted_auth_templates`, `test_capture_promotion` | **428 passed / 430, 2 failed** |
| B — behaviour | `test_resolution_backfill`, `test_resolution_auto_promotion`, `test_intake_auto_resolution`, `test_contact_capture_intake`, `test_resolution_gates`, `test_resolution_web`, `test_company_domain_resolution`, `test_worker_backfill_boundary`, `test_model_domain_lookup`, `test_promotion_identity_links`, `test_capture_resolution_feedback`, `test_phase2_orchestration` | **275 passed / 275** |

The two failures in set A are:

```
FAILED tests/test_production_hardening.py::test_readyz_uses_the_real_disposable_postgres
FAILED tests/test_production_hardening.py::test_cancelled_readiness_caller_does_not_leave_database_work_running
```

Both are the documented Windows-only readiness failures — `assert 503 == 200`
and `TimeoutError: readiness probe exceeded its wall-clock budget` from
`app/core/health.py`, caused by psycopg's async path on Windows'
`ProactorEventLoop`. **This was verified rather than assumed:** both were re-run
from a clean `git worktree` at the untouched base commit
`8720b7f4cbda8e9b193551355998ecfc363987be` and fail identically there. Neither
test is in a file this branch modifies, and the diff touches nothing in the
readiness or database path.

One test (`test_readyz_recovers_after_a_midstream_tcp_freeze`) was deselected: it
hangs indefinitely on this machine rather than failing, and would stall any
unattended run.

**GitHub CI is the confirming full-suite run on Linux**, and per the local-test
notes it is the authority for this repository.

Do not treat this handoff as independent acceptance.

## Staging environment values needed later

`LOGO_DEV_API_KEY` is **already installed** (see the installation record below).
Three lines remain, and they must be added only *after* this branch is merged
and deployed — the currently deployed release refuses the first one outright:

```
FEATURES__CONTACT_CAPTURE_PROMOTION=true
FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION=true
FEATURES__SALESNAV_DOMAIN_ENRICHMENT=true
```

All four values must be present together, or the application refuses to start.

Already present on the staging box and unchanged: `APP_ENV=staging`,
`AUTH__*`, `EXTENSION_AUTH__*`, `FEATURES__CONTACT_CAPTURE_INTAKE=true`,
`FEATURES__WORKBENCH=true`, `DRY_RUN=true`.

`FEATURES__MODEL_COMPANY_DOMAIN_LOOKUP` is **not** a prerequisite and should
stay unset for this step.

### New operator secret requirement

**One: `LOGO_DEV_API_KEY`** — and it is **now installed on staging**, copied
from the operator's local `.env` rather than recreated. It is never logged,
never returned by any endpoint, and is excluded from `repr(settings)` and
`settings.model_dump()` — verified live on the box, not only in a unit test.

No other credential changes. Gmail, Google identity, Saleshandy and the
extension credential are untouched.

## Staging secret installation — record (2026-08-12)

`LOGO_DEV_API_KEY` and `MILLIONVERIFIER_API_KEY` were copied from the operator's
local `.env` into `/etc/vmr/vmr.env`. Values were never printed to chat,
terminal output, shell history, logs, Git or any artifact.

| | |
| --- | --- |
| Name mapping | Read from the `Settings` model, not assumed: `logo_dev_api_key ← LOGO_DEV_API_KEY`, `millionverifier_api_key ← MILLIONVERIFIER_API_KEY` (no pydantic aliases, so field name uppercased) |
| Backup | `/etc/vmr/vmr.env.bak.providersecrets.20260812T101809Z`, taken with `cp -a` |
| Permissions | before `640 root:vmr` → after `640 root:vmr`; the installer aborts and restores from backup if they differ |
| File | 109 → 113 lines; prior assignments for both keys stripped before append, so a re-run replaces rather than shadows |
| Transport | remote script generated locally with the values base64-embedded, fed to `ssh … "base64 -d \| bash -s"` on **stdin**; the remote writes them with `printf`, a bash builtin, so there is no `/proc/<pid>/cmdline` to read them from |
| Round-trip proof | HMAC-SHA256 under a random per-run salt, computed on both ends and compared; salt discarded. Both matched. |
| Pre-restart validation | The deployed release's own parser, run via `systemd-run` with the units' `User=vmr`, `WorkingDirectory=/srv/vmr/app` and `EnvironmentFile=/etc/vmr/vmr.env`, so **systemd** parsed the env file exactly as it does at boot |

Validation result, before any restart:

```
app_env: staging            debug: False            dry_run: True
has_logo_dev_key: True      has_millionverifier_key: True
features_enabled: ['agent_workbench', 'contact_capture_intake', 'csv_import',
                   'seller_knowledge_base', 'workbench']
hosted_auth_validation: PASS
runtime_validation: PASS
secrets_absent_from_settings_dump: True
```

After restarting `vmr-web` and `vmr-worker`: both `active`/`enabled`,
`NRestarts: 0`, "Application startup complete", worker polling normally. Health
over loopback with the canonical Host header — `/healthz` 200, `/readyz` 200
(`configuration: ok`, `database: ok`), `/version`
`8720b7f4cbda8e9b193551355998ecfc363987be`.

**Two things this deliberately did NOT do.**

1. **The three `FEATURES__` promotion switches were not installed.** The
   deployed release is `8720b7f4cbda…` — this branch's base, pre-merge — whose
   code still refuses `FEATURES__CONTACT_CAPTURE_PROMOTION=true` in staging
   outright. Writing those lines now would make the next restart fail. They go
   in *after* the merge and deploy, per step 2 below.
2. **Installing the keys enabled nothing.** `FEATURES__MILLIONVERIFIER` remains
   unset and `DRY_RUN=true`, so no verification credit can be spent. Worth
   stating plainly all the same: a real MillionVerifier key now sits on staging,
   so the moment `FEATURES__MILLIONVERIFIER=true` is set there, live billable
   calls become possible. That is a cost decision and it is Sahil's.

## Deploy / UAT steps

1. **Merge and release.** ChatGPT opens the PR against `main`, verifies the diff
   and CI, records a verdict; merge only after Sahil's explicit approval.
2. **Add the three feature switches — after the release, not before.** Order
   matters: the *currently deployed* release (`8720b7f4cbda…`) refuses
   `FEATURES__CONTACT_CAPTURE_PROMOTION=true` outright, so writing those lines
   into `/etc/vmr/vmr.env` before the new release is deployed would make the
   next restart fail. Deploy the release first, then add the three `FEATURES__`
   lines. `LOGO_DEV_API_KEY` is already in place.
3. **Restart and prove the boundary.** Restart `vmr-web` and `vmr-worker`. The
   refusal is worth exercising once: temporarily comment out
   `FEATURES__SALESNAV_DOMAIN_ENRICHMENT` and confirm startup fails naming it,
   then restore it. Health check needs the canonical Host header —
   `curl -H "Host: srv1885453.hstgr.cloud" http://127.0.0.1:8000/readyz`; a bare
   `127.0.0.1` request returns 400 from `TrustedHostMiddleware`, which is the
   guard working, not a fault.
4. **Let the worker drain the backlog.** No manual step and no SQL. The worker's
   backfill pass picks up the pending captures 50 at a time
   (`--resolve-limit`). Watch the worker log for the backfill line.
5. **UAT the recovery**, against the real data:
   - `contact_capture_promotions` rows appear with `contact_outcome` set;
   - `contacts` and `companies` counts rise from 0;
   - the 24 filings for campaign `588b3e15-8c39-4d5f-962b-ff1b00d76412` move
     `PENDING → APPLIED` with `campaign_contact_id` populated;
   - `campaign_contacts` for that campaign equals the number of filings that
     applied — no duplicates;
   - each membership shows `latest_completed_stage = capture` and
     `next_stage = identity`;
   - captures whose company stayed unresolved created **no** Contact and kept a
     `PENDING` filing — that is correct, not a failure, and they are the
     operator's to settle in `/contact-captures/pending`.
6. **UAT a fresh capture.** Capture one new person through the extension with
   the campaign selected; it should promote and file inside the intake request.
7. **Do not enable Sending.** `DRY_RUN=true` stays, and the Sending Agent has no
   adapter.

## What remains incomplete / known limitations

- **A production promotion path does not exist and is refused.** That is
  deliberate and is a separate design.
- **Captures the policy cannot resolve stay pending.** Ambiguous, conflicting
  and empty provider results produce `UNRESOLVED`, no Contact, and an operator
  decision in the workbench. The number of the 44 that land here is not
  predictable from the repository and will only be known after the run.
- **`_urllib_transport` is what the tests substitute.** No live logo.dev call is
  made by any automated test, and no real key exists in the repository.
- **Gate 7 was not completed locally** — stated above.

## Proposed tracker update (for ChatGPT to verify and apply)

- Item: hosted capture promotion boundary (staging Beta).
- Status: build complete on `feat/hosted-capture-promotion`, pushed, awaiting
  independent review and CI.
- Blocker resolved in code: `contact_capture_promotion` is no longer refused in
  staging; it is permitted behind a fail-closed four-part prerequisite boundary.
- Blocker still open, owner Sahil: `LOGO_DEV_API_KEY` is not provisioned on
  staging. Nothing promotes until it is, and startup will refuse the
  half-configured state rather than promote silently.
- Go-live answer unchanged: this does not enable Sending and does not by itself
  make any Contact outreach-eligible.

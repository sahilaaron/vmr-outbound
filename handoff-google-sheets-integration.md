# Handoff — Google Sheets add-on MVP

Branch `feat/google-sheets-integration`, base `3b261d4` (origin/main).
One commit. Not pushed at the time of writing; delivered as a git bundle.

Canonical documentation: [`docs/GOOGLE_SHEETS_INTEGRATION.md`](docs/GOOGLE_SHEETS_INTEGRATION.md).
Install steps: [`integrations/google-sheets/README.md`](integrations/google-sheets/README.md).

---

## 1. Scope note

This is a **new surface**, and `docs/GOAL.md` does not authorize one. It was
built on an explicit instruction, and it is recorded here as a scope change
rather than folded in silently. Nothing in `GOAL.md` was edited.

It does not widen the MVP's promise. The add-on ends exactly where the product
already ends — a verified address and a reviewed seven-message sequence — and
adds no sending, no scheduling and no mailbox access.

## 2. Architecture

The spreadsheet is a thin client and nothing else:

```
Sheet rows -> POST /integrations/sheets/batches
           -> Contact + Campaign membership (campaign_contacts.enrol_contact)
           -> the existing durable Agent pipeline, run by the existing worker
           -> POST /integrations/sheets/results -> address + seven messages
```

No research, domain policy, email discovery, verification, insight or message
generation exists in Apps Script. VMR Outbound stays authoritative for every
record; deleting the spreadsheet deletes nothing.

## 3. Migrations — none, and that is proved

`alembic heads` → single head `e2b7c0d94a15`. `alembic upgrade head` then
`alembic check` → *No new upgrade operations detected*.

Two tables could have been widened to hold this, and both refuse a capture-less
row by database constraint, so neither was:

* `company_domain_resolutions.capture_id` is `NOT NULL` and every index is keyed
  per capture;
* `salesnav_company_enrichments` enforces `(batch_id IS NULL) <> (capture_id IS NULL)`.

A spreadsheet row is not a capture, and making it look like one to reuse a table
would have put false provenance into the evidence trail. Instead:

* **row provenance** rides in `campaign_contact_sources` — `source_type`
  `"google_sheets"`, `source_reference` the client row id, `source_context` the
  install, spreadsheet, tab and generation;
* **the credential** is minted by Google per execution and never stored;
* **the batch** is derived, not persisted — results are read by a bounded list of
  submission identifiers the add-on already holds.

## 4. The four judgement calls

**Only `CONFIRMED` company domains are accepted; `PROVISIONAL` is refused.** On
the capture path a provisional domain is safe because it is recorded and the
gates read it. Here there is no such record, so an accepted provisional domain
would produce a Company indistinguishable from an established one — domain
laundering. The row says the company could not be identified instead, which is a
smaller product and a true one.

**`desired_stage` is adopted, not chosen.** `enrol_contact` refuses to re-aim an
existing membership, correctly. So the sheet joins a membership on the terms it
already has, and a new one takes `campaign_contacts.DEFAULT_DESIRED_STAGE` — the
same default every other enrolment path takes. The no-send guarantee comes from
the Sending Agent having no production adapter, not from a stage number.

**The feature flag is an operator control, not an environment-only switch.** The
classification conformance test requires every `FeatureFlags` key to be declared
one way or the other, and a control the routes ignored would be worse than none —
so the routes read it through `operations.settings.enabled`. It is unavailable
until `SHEETS__ALLOWED_AUDIENCES` is set (a security boundary, no write path from
any screen) and `email_sequences` is on (without it no row can reach Ready).

**The routes are not under `/api`.** That prefix is administrator-only by policy
and this surface is for ordinary accounts on their own campaigns. They are
classified in `app/core/auth/policy.py` as anonymous-to-the-middleware, with the
guard as a *router-level* dependency — the same shape `require_admin` uses — and
a test asserts it is on the router rather than on individual handlers.

## 5. Defect found in shared code, recorded and not fixed

`app/services/resolution/service._existing_company_matches` pre-filters candidate
Companies with `LOWER(name) LIKE '%<first six characters of the folded name>%'`,
and folding removes spaces. `"Kiln Systems"` folds to `"kilnsystems"`, whose first
six characters are `"kilnsy"` — which does not appear in `"kiln systems"`. **A
multi-word company never matches its own permanent row.**

On the capture path the consequence is a logo.dev lookup that was not needed; the
failure direction is safe, which is why it has gone unnoticed. On this path it
would be the difference between working and not, so
`app/services/integrations/sheets/companies.py` builds the same evidence without
the prefilter, locally, and says why in its docstring. Fixing the shared helper is
a change to the capture path and belongs with the next change to that module.
Recorded in `docs/POST_LAUNCH_BACKLOG.md`.

## 6. Validation

| Gate | Result |
| --- | --- |
| `ruff check .` | clean |
| `ruff format --check .` | 577 files already formatted |
| `mypy app` | clean, 292 files |
| `alembic heads` / `upgrade` / `check` | single head, no drift |
| `python scripts/ci_shards.py verify` | PASSED — 4122 tests, 0 omitted, 0 doubled |
| pytest, every shard | **4122 passed, 0 failed** |
| `node --test` (add-on) | **25 passed** |

New backend tests: `tests/test_google_sheets_integration.py` (51), covering all
eighteen listed backend cases plus the credential rules, the feature gate, the
administrator switch, suppression, provisional refusal and sanitisation.

New add-on tests: `integrations/google-sheets/test/` (25), covering the ten listed
sheet-side cases. They exist to answer one question — *can a result ever land on
the wrong row?* — under sorting, insertion, deletion, a deleted key column, a
partly refreshed sheet and a retried submission.

Two existing conformance tests were updated with reasons rather than worked
around: the anonymous-path set and the CSRF router-exemption set in
`tests/test_hosted_auth_templates.py`.

## 7. What remains manual

* Pushing the branch, opening the PR, review, merge, tracker update.
* Google Cloud: binding the Apps Script project to the deployment's Cloud project.
* Copying the `aud` the sidebar displays into `SHEETS__ALLOWED_AUDIENCES`.
* Running `scripts/run_agent_worker.py`. **Without it every row stays Pending
  forever** — the add-on submits and the worker does the work.
* Turning the control on in `/admin`.

## 8. Deliberately deferred

Per-account revoke for this surface, per-account rate limiting across requests, a
batch table and `GET /batches/{id}`, Marketplace publication and admin install,
dossier/Insights columns, editing a message in the sheet, and any scheduling or
sending. Each with its reason in §13 of the canonical doc and in
`docs/POST_LAUNCH_BACKLOG.md`.

## 9. First UAT step this unblocks

One operator, one sheet, a handful of real rows: submit, wait for the worker,
refresh, and read a verified address and seven messages out of the spreadsheet —
without opening the app.

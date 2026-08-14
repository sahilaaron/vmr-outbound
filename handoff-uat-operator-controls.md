# UAT operator controls + campaign ownership repair — handoff

Branch `feat/uat-operator-controls`, based on `origin/main` at
`d9750b0` (PR #274). Nothing merged, nothing deployed, no VPS or runtime file
touched. The separate `fix/uat-extension-account-linking` branch was not
inspected, not merged and not modified.

Three requirements from real Hosted Beta UAT, in commit order.

---

## 1. Password minimum is eight characters

**Commit** `db861a0`.

`MIN_PASSWORD_CHARS` 15 → 8 in `app/core/auth/passwords.py`. Eight is the NIST SP
800-63B minimum for a user-chosen memorised secret. Fifteen was pushing operators
onboarded through a one-time setup link toward passwords they wrote down, which
is the failure a length rule exists to prevent.

Unchanged, deliberately and asserted: the 256-character maximum, Argon2id at the
OWASP configuration, sign-in rate limiting, one-time setup-link behaviour, no
public signup, and every password/session invalidation rule.

The blocklist **did** change and had to. At fifteen, most of what an online
attacker tries first was refused by length before the list was consulted; at
eight it is not. Thirty-two entries covering the eight-to-fourteen character band
were added (`12345678`, `qwerty123`, `passw0rd`, `letmein1`, …). The module
comment already anticipated this: the short entries were kept "in case the
minimum is ever lowered".

Copy is data-driven — the setup form renders `PASSWORD_RULES.minimum_characters`
— so no template needed changing. `docs/HOSTED_AUTH.md` updated.

**Tests.** `tests/test_user_accounts.py`: a dedicated boundary test asserting
8 accepted / 7 refused / 256 accepted / 257 refused, so a drift to seven or nine
fails; blocklist cases at the new length; the setup-form error copy now reads
"at least 8 characters".

---

## 2. Campaign ownership and per-user assignment

**Commits** `db861a0` (data model), `4429618` (authorization), `bbcffe2` (review
scoping + tests).

### Migrations

Both additive; single head `e2b7c0d94a15` after requirement 3.

`c1f4a90b7d38` — campaign ownership and assignment:

* `campaigns.created_by_user_id` — nullable, `ON DELETE SET NULL`, indexed.
* `campaign_user_assignments` — `id`, `campaign_id` (CASCADE), `user_id`
  (CASCADE), `assigned_by_user_id` (SET NULL), `created_at`, with
  `UNIQUE (campaign_id, user_id)` making assign idempotent in the database.

`alembic upgrade → check → downgrade → upgrade` clean; `alembic check` reports no
drift from the models.

### Exact authorization rules

`app/services/campaign_access.py` is the only module that decides.

* **ADMIN** — every campaign, read and write, and the only role that may assign
  or unassign.
* **USER** — campaigns they created (`created_by_user_id`) plus campaigns
  explicitly assigned to them. Nothing else, for reading, editing, enrolling,
  importing, executing, reviewing or any API call naming the campaign.
* **Multiple assignment** — any number of users per campaign, any number of
  campaigns per user.
* **Unassign** — a delete; takes effect on the next request, no re-sign-in,
  because access is computed per request and never copied into the cookie.
* **No accounts** — where `AUTH__ENABLED` is off (local, the whole test suite),
  `CampaignActor.enforced` is `False` and every check passes, exactly as
  `require_admin` next door behaves.

### Existing-campaign migration semantics

Historical rows keep `created_by_user_id = NULL` and **are not backfilled**. The
database records `actor = "operator"` — a constant, not an identity — so any
backfill would be a guess indistinguishable afterwards from a fact.

Consequence, stated rather than hidden: **administrators reach every historical
campaign**; a normal user reaches one only after an explicit assignment.

### Where it is enforced (all server-side)

* **Router-level dependency** `require_campaign_path_access` on the v2 product,
  the legacy web routes, the Admin Workbench and both API routers — so any route
  with a `{campaign_id}` path parameter is scoped the moment it is registered.
  Administrators pass without a query; a non-UUID path parameter is left to the
  handler; everything else is refused before the handler body runs.
* **Query/form parameters**, checked in the handler that reads them:
  `/app/review?campaign=`, `/app/contacts/{id}?campaign=`,
  `POST /contacts/add-to-campaign`, `POST /api/intake/contact-captures`.
* **Id-keyed review writes** — approve/discard of a draft, all three sequence
  message writes, sequence approve, and sequence Gmail drafts.
* **Lists** — `list_campaigns` and `campaigns_for_offering` take a *required*
  `actor`; the review queue, sequence queue, `first_awaiting` and the contact
  page's memberships take a `campaign_ids` restriction.

Refusals are one shape from one handler:
`{"error": "campaign_access_denied", "status": 403}`. 403 rather than 404 for the
reason `AdminRequiredError` already records.

### Two defects found while testing, both real, both fixed

1. **Review decisions were unscoped.** A USER with a valid session and CSRF token
   could POST `/app/review/{draft_id}/approve` for another team's draft and get a
   303 with a `DraftApproval` row written. Approval is the human authorisation
   the pipeline waits for — a signature on work somebody was never shown.
   Reproduced before fixing.
2. **The review page's fallback draft was unscoped.** The queue filtered
   correctly, but an empty queue fell through to `first_awaiting()` with no
   restriction and rendered another team's draft subject, body and evidence.

### Admin UI

`/app/campaigns/{id}` gains a "Who can use this campaign" panel for
administrators: creator, assignees with who granted each and when, an unassign
button per assignee, and an assign control whose options come from the `users`
table (never a typed address). The edit page states the same facts read-only; the
new-campaign page says who will own it.

Writes are `POST /app/admin/campaigns/{id}/assign` and `/unassign` on a router
carrying `require_admin`, under a prefix the middleware already withholds, and
the service refuses a non-administrator actor again.

### The extension

`fix/uat-extension-account-linking` is **not** merged here. A capture credential
carries no user, so `actor_from_request` resolves it to an unidentified actor and
`GET /api/campaigns` keeps today's behaviour rather than returning nothing and
breaking the shipped extension. Filing a capture already calls
`may_access_campaign`, which fails closed as soon as a user *is* resolvable.

When that branch lands it has one thing to do: write `operator_user_id` and
`operator_role` into the request scope. Every rule then applies with no further
change — `GET /api/campaigns` returns only that user's campaigns and unauthorized
filing fails closed. The seam is one `if` in `app/api/phase2.py`, commented in
place.

### One deliberate reclassification

`GET /app/agents` moved to the administrator surface, joining the control POST
that was already there. The monitor names every campaign carrying an Agent
override and lists jobs across all of them, and it is not scoped to one person's
campaigns. Per-campaign Agent work is untouched under `/app/campaigns/{id}/...`.
`tests/test_route_authorization.py` records the decision and its conformance test
fails if any route's classification changes without one.

---

## 3. Administrator-operable product configuration

**Commit** `1bd9e76` (+ a concurrency fix after review).

### The finding

Agent controls enabled, Research jobs paused with `feature_disabled`, because
`FEATURES__COMPANY_RESEARCH` was false in `/etc/vmr/vmr.env`. The only fix was
SSH, an edit and a restart — a deployment procedure standing in for an operating
decision.

### Migration

`e2b7c0d94a15` — `operational_settings` (`key` PK, `enabled`, `reason`,
`updated_by`, `updated_at`, `version`). Created **empty**, so with no rows every
control resolves to its existing `FEATURES__*` value and no deployment changes
behaviour. Round-trip verified; single head.

### The effective-control contract

In force when all three hold, and the screen shows all three separately:

1. **deployment capability** (credential configured, environment permits,
   prerequisite control on) — not overridable, evaluated on every read;
2. **the administrator's durable setting** — the row;
3. **the Agent/Campaign control** where one applies — unchanged.

The environment is the **default**, not a ceiling. With no row, the env value is
used; with a row, the row wins. Anything else would have satisfied the words
"operator control" while leaving the UAT finding where it was.

### Operational vs deployment classification

Full table in `docs/ADMIN_CONFIGURATION.md`. Summary:

* **17 operator product controls** — company_research, research_claude_fallback,
  company_intelligence, insights_research, automatic_company_domain_resolution,
  salesnav_domain_enrichment, model_company_domain_lookup,
  contact_capture_promotion, millionverifier, email_generation, drafting,
  email_sequences, gmail_drafts, csv_import, suppressions, seller_knowledge_base,
  agent_workbench.
* **7 deployment/security settings, env only, no write path** — workbench,
  salesnav_intake, linkedin_profile_intake, linkedin_profile_refresh,
  linkedin_company_intake, contact_capture_intake, claude_mcp_bridge. Each shown
  read-only with its reason. Two reasons recur: startup validation that a runtime
  switch would walk past, and mount-time decisions a row cannot change.
* **4 declared but not consulted** — normalization, deduplication, scoring,
  saleshandy. Listed under their own heading rather than offered as switches that
  do nothing.

A test asserts the three sets are disjoint and together cover every
`FeatureFlags` field, so a flag added later cannot be left unclassified.

Secrets are never displayed — only whether a credential is configured.
`set_control` refuses any key outside the product-control registry, so a
hand-crafted POST naming a deployment setting is refused server-side, not merely
absent from the form.

### DeBounce

There is no `debounce` feature flag and no credential path for it in the
codebase; it is registered in the provider registry only. It therefore appears on
`/admin/providers` as "no credential path is configured yet" and has no control
on the configuration screen. Creating one would be a switch that cannot do
anything — deferred, noted below.

### MillionVerifier and the simulator

The capability requires the credential **except** in local/development/test/ci,
where routing to the deterministic simulator with no key is documented, tested
behaviour. A hosted deployment reporting verification as on while a simulator
quietly answers is the case worth refusing; local development is not.

### Research recovery semantics

A feature refusal is a **pause**, not a failure or a skip: `AgentBlocked
("feature_disabled")` → `jobs.mark_paused` → stage `BLOCKED`. The job, its
attempt count and its stage all survive.

`orchestrator.reclaim_feature_paused_jobs` is the way back and reuses the
supported mechanism — `jobs.resume_paused` with an explicit set of pause
classifications the caller owns, exactly as `reconcile_agent_control` does when
an Agent control is re-enabled. **Only `feature_disabled` is resumed**; operator,
membership, suppression, agent-control and campaign-execution pauses are
untouched.

A resumed job returns to `PENDING` with `next_run_at` now and its errors cleared;
its stage moves `BLOCKED → WAITING` with an `ELIGIBILITY_RESTORED` event. Nothing
is skipped and nothing is terminally consumed — every gate that refused it gets
to refuse it again if it still applies. The Admin screen reports how many jobs
went back into the queue.

### Read-site conversion

Every behavioural and display read of a product control now goes through the
effective layer (`operational.enabled` / `operational.effective_flags`). Startup,
router mounting, runtime validation and the intake-endpoint flags still read the
environment directly, by design.

Two behaviour changes fell out of the capability gate and are called out:

* `salesnav_domain_enrichment` refusals on the enrichment routes now come from a
  shared `operational.refusal()` message, so a missing credential and an
  administrator's decision produce different, specific sentences instead of one
  generic "not enabled".
* `LookupBlocker.setting` strings used to tell an operator to set an environment
  variable. They now name the control and point at Admin → Configuration —
  except `LOGO_DEV_API_KEY`, which really is environment-only.

### A defect found in review, fixed

A blank `expected_version` from a form rendered before any row existed was being
sent as "no opinion", so a second administrator's write could silently overwrite
a first. It is now submitted as version `0`, which the update path reports as the
conflict it is, matching the Agent control forms. The create-race path reports a
conflict too rather than overwriting.

---

## Tests and results

Two new files, both written to fail when the rule they name is removed and both
verified by mutation:

* `tests/test_campaign_authorization.py` — **16 tests**. Admin sees all;
  creator sees own; assignee sees assigned; unrelated user cannot list, read,
  edit or mutate (403 body asserted, not just the status, so a CSRF or
  `admin_required` refusal cannot pass as authorization); multiple assignment;
  unassign revokes on the next request; ownerless historical campaign is
  administrator-only; direct URL and API fail closed; creation records the owner;
  review approve/discard and sequence approve refused and then permitted once
  assigned; nothing refused where authentication is off.
* `tests/test_admin_operational_configuration.py` — **13 tests**. Empty table is
  behaviour-neutral; a row beats the environment (Company research enabled with
  no VPS edit); capability is a ceiling a hand-inserted row cannot lift;
  deployment settings have no write path and the classification covers every
  flag; stale-version refusal and no-op writes; audit events; ADMIN can change /
  USER is refused on both verbs; no sending authority added; secrets never
  render; **the end-to-end recovery** — Research paused with `feature_disabled`,
  control turned on, job back to `PENDING` as the same row with the stage no
  longer blocked and nothing skipped or cancelled; reclamation touches only
  feature pauses.

Existing suites adjusted where a fixture, not a rule, was the cause:
`tests/test_route_authorization.py` (the `/app/agents` reclassification, recorded
with its reasoning), `tests/test_gmail_draft_integration.py` and
`tests/gmail_factory.py` (fixture campaigns now carry an owner, and the
multi-operator tests assign the campaign first so they assert the Gmail
boundary rather than the campaign one), `tests/test_user_accounts.py`,
`tests/test_email_sequence.py`, `tests/test_import_to_sequence.py`,
`tests/test_model_domain_lookup.py`, `tests/test_verification_web.py`,
`tests/test_capture_page_agreement.py`.

Gates on the branch head: `ruff check` clean, `ruff format --check` clean,
`mypy app` strict clean (279 files), `alembic heads` single, `alembic check`
clean, migration round trip clean. Focused suites green. **GitHub CI is the broad
regression authority after push.**

---

## What remains manual

* Push, PR and merge — this session cannot push.
* The Google Sheet phase tab update.
* On deploy: `alembic upgrade head` (two additive revisions). No environment
  change is required; `/etc/vmr/vmr.env` is untouched and every control keeps its
  current value until an administrator changes one.
* Assigning historical campaigns to the operators who should have them. There is
  no safe automatic answer; the Admin panel is the intended route.

## Deliberately deferred

* **Extension account linking.** Left entirely to
  `fix/uat-extension-account-linking`. The seam is written and commented.
* **Scoping the Agent monitor** rather than withholding it. That means rewriting
  the reader the administrator surfaces share; withholding was one line.
* **A DeBounce operational control.** No credential path exists, so a switch
  would be inert.
* **`contact_capture_promotion` / `automatic_company_domain_resolution` on the
  capture page** still read the environment. Converting them makes the whole
  promotion surface disappear on a deployment with no logo.dev key, because the
  capability chain requires one. That is a UX decision, not a mechanical one.
* **`app/api/routes.py` `csv_import_batch`** still reads the environment: the
  route has no database session and adding one to an intake API route was out of
  scope for this slice.
* Sending, in any form.

## Remaining risks

1. **The reclassification of `GET /app/agents`.** A normal operator loses a
   screen they could previously read. If that is wrong, reverting is one entry in
   `_ADMIN_PATH_PREFIXES` plus the recorded test classification.
2. **Historical campaigns are invisible to normal users until assigned.** This is
   the intended semantic, and on a deployment with many existing campaigns it is
   a visible change on the first sign-in after deploy. Assign before announcing.
3. **One extra query per feature read.** `operational.enabled` runs a small
   `SELECT` on `operational_settings` per call. It is a tiny indexed table, but
   hot paths that read several flags now issue several reads; if that shows up,
   `effective_flags` once per request is the fix and the call sites already
   exist.
4. **The capability gate can turn something off that was on.** A deployment with
   `FEATURES__X=true` and a missing credential used to run a fallback; it now
   reports the control as unavailable. Two such cases were found and handled
   (MillionVerifier's simulator, the logo.dev message). A third may exist on a
   deployment configured differently from staging — the screen will say which.
5. **Two administrators, one screen.** Optimistic concurrency is now correct on
   both paths, but it produces a "reload and try again" that an operator has to
   read.

**UAT OPERATOR CONTROLS READY FOR REVIEW**

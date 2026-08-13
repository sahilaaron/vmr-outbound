# Build handoff — Hosted Beta operator readiness repair

A focused repair, not a redesign. It removes the concrete blockers stopping a
normal USER operator from running the Hosted Beta workflow without a developer.

## Branch and commits

Branch `fix/operator-readiness-beta`, cut from the exact main SHA supplied and
not from an older feature branch.

```
base  a10c1de1c535f26bdc2825a3a9ffc127557b7ecb   (main, CI #361 green on this SHA)
head  aa5701d7094faea1a7a918f94ac38167757a2938   (verified on the remote)
```

`git ls-remote origin refs/heads/fix/operator-readiness-beta` returned
`aa5701d7094faea1a7a918f94ac38167757a2938`, matching the local head. Every gate
and every suite recorded below was run at that commit; the only commit after it
is the one that writes these two SHAs into this file, which touches no code.

`git merge-base HEAD a10c1de1` returns `a10c1de1c535f26bdc2825a3a9ffc127557b7ecb`,
so the branch descends from that commit and nothing was rebased onto it.
`origin/main` is still `a10c1de1` and was not modified. The branch is 10 ahead,
0 behind, and the working tree is clean apart from files that were already
untracked when the session began.

| Commit | What it does |
| --- | --- |
| `fa433f47` | Put argon2 in the closure the deployment actually installs |
| `ee9ff8fe` | Give the route policy a second question: which operators, not just whether |
| `bb0aeb30` | Let an operator turn on the seven-message workflow, and refuse to start it broken |
| `deb70d83` | Put the copy controls on the surface people review from |
| `684dec3a` | Correct the deployment facts that would have failed or misled the staging deploy |
| `7f3f044a` | Sort the import block the new readiness import landed in |
| `ba4b09e2` | Close the newline bypass, and put ambiguous-import triage back where it belongs |
| `cd3d4de8` | Keep the unreadable value instead of refusing, and follow through on two invariants |
| `83a77591` | Let the authentication suites reach the behaviour they were written to test |

No migration is added. This repair needed no schema change, which was the
preferred outcome.

## P0-1 — the deployment dependency closure

**Confirmed, reproduced, fixed.**

`vmr-deploy` installs `constraints.txt` as a *requirements* file and then
installs the project `--no-deps`. Under `--no-deps` pip never reads
`pyproject.toml`, so `argon2-cffi` — declared there since #270 for hosted
password hashing and never added to the closure — was simply absent from the
release. Reproduced against that exact pattern in a clean virtualenv:

```
pip install --requirement constraints.txt
pip install --no-deps --editable .
VMR_TEST_MODE=1 python -c "import app.main"
  -> ModuleNotFoundError: No module named 'argon2'   (app/core/auth/passwords.py:56)
```

With `argon2-cffi==25.1.0` and `argon2-cffi-bindings==25.1.0` pinned, the same
three steps import cleanly. `pip check` reports no broken requirements, and the
Argon2id hash/verify round trip runs through the application's own module.

**One platform fact worth a reviewer's attention.** `argon2-cffi-bindings`
ships `cp39-abi3` wheels tagged `manylinux_2_28`, where every other binary wheel
in this closure is content with `manylinux_2_17`. The closure therefore now
needs glibc ≥ 2.28. Staging is Ubuntu 24.04 (glibc 2.39), so this is satisfied
with room to spare, and it is recorded in the PLATFORM note in `constraints.txt`
so a move to an older base image is a deliberate decision rather than a
surprise. The alternative — pinning the bindings back to the last
`manylinux_2_17` release, 21.2.0 from 2021 — was rejected as a worse trade on a
password-hashing primitive.

**Gmail was checked for the same class of omission and has none.** Its runtime
imports are `httpx` and `cryptography`, both already pinned. No other declared
runtime dependency is missing.

Two documentation facts were corrected in passing, one of which was already
wrong before this branch: the header described the install as
`pip install --constraint constraints.txt .`, which is not what `vmr-deploy`
runs. The distinction matters precisely because packages were being added — as a
*requirements* file every line is installed unconditionally, so adding a line
installs a package rather than merely capping one. The package count in
`deploy/README.md` moves 64 → 66.

**Deviation to declare.** No Linux host was available in this environment (no
WSL distribution, no Docker), so the reproduction ran on Windows with the single
`uvloop` line filtered out — `uvloop` does not build on Windows and is unrelated
to `import app.main`. Linux wheel availability for both new pins was verified
separately with a platform-targeted `pip download`. The honest statement is:
the dependency *closure* is proven, the *Linux* install is proven only at the
wheel-resolution level, and CI plus the deploy script's own import check remain
the confirmation.

## P0-2 — USER vs ADMIN authorization

**The reported problem was real and larger than reported.**

Counted from the live router table: **272 route/method pairs, of which 253 were
reachable by any signed-in account regardless of role.** `require_admin` existed
and was declared on exactly one router — the account directory at
`app/web/v2/admin_users.py:58` — and nowhere else. The session middleware wrote
`operator_role` into the request scope on every request and then never read it.

Among what a normal USER could reach and do:

- `POST /admin/agents/studio/verification/credentials/{provider_id}` — rotate
  the MillionVerifier credential
- `POST /verification/bulk` — enqueue up to 500 contacts against a paid provider
- `POST /admin/agents/studio/verification/test` with `mode=live` — one paid call
  per POST, booked to the usage ledger
- `PUT /api/agents/{id}/control` and three other paths — halt or resume every
  campaign's pipeline, including the sending agent
- `POST /knowledge-base/restricted-claims/{id}/state` — deactivate a KB-001
  compliance guard
- `GET /admin/configuration`, `/admin/providers`, `/admin/system`,
  `/admin/diagnostics` — settings, provider spend and job internals
- `GET /docs`, `/redoc`, `/openapi.json` — the map for all of the above

This is not theoretical on staging: `MILLIONVERIFIER_API_KEY` is already
installed in `/etc/vmr/vmr.env`.

### The matrix, and where it lives

The classification is written out in `app/core/auth/policy.py` beside the
anonymous one, as an explicit, reviewable set — not derived, on purpose, so
changing it is a recorded decision. After the change, computed against the live
router table:

| | count |
| --- | --- |
| ADMIN | 168 |
| USER | 90 |
| PUBLIC (anonymous) | 13 |

The USER set is exactly the operator product: `/app/**` (less `/app/admin/**`
and the global agent-control POST), the capture workflow, contacts, companies,
the knowledge base except restricted-claim writes, `GET /verification`, and the
operator's own Gmail connection.

### Why the middleware rather than router dependencies

Enforcement sits in the same middleware as the anonymity check, before routing.
That placement is what makes an alternate spelling, an unmounted path under an
administrator prefix, and a route nobody remembered to decorate all refuse
identically — `/app/../admin`, `//admin` and `/static/../admin` are normalised
first and all refuse. It is also the only thing that *could* express the
boundary: the administrator surface spans three routers, one of which
(`app/web/routes.py`) also serves ~107 ordinary operator routes, so no
per-router dependency covers it without splitting a 4,700-line module first.

Prefix matching is safe in this direction and exact matching is not required.
An anonymous prefix would grant access to routes that do not exist yet — the
opposite of default-deny — whereas an admin prefix withholds it, so a router
mounted under `/admin` next month is administrator-only the moment it is
mounted. The residual risk runs the other way: a brand-new *top-level* surface
would default to USER. That is what the classification conformance test is for.

### The decisions you made, as implemented

**`/api/**` is ADMIN for session callers.** Verified first: the server-rendered
`/app` product makes **zero** calls to `/api` — no `fetch`, no htmx, no XHR, no
`axios`. The only `/api` references in any template are six read-only links to
`/api/admin/agent-studio/*/report` on Agent Studio pages.

**The extension bearer contract is unchanged and was not widened.** It is
resolved before the role check and carries no role, so the four contract routes
still answer. On your specific instruction about
`POST /api/intake/linkedin-company/stage` — I traced how it is actually
authenticated rather than adding it to the contract. It is **not authenticated
at all, and never reaches a hosted backend**: the extension refuses client-side
at `service-worker.js:1255-1264` with `company_capture_local_only`, and posts
with `credentials: "omit"` and no `Authorization` header. Company evidence
capture is a local-development path, where this middleware is inert. So it is
covered by the `/api` prefix, no carve-out was needed, and extension authority
was not broadened.

**Reads stay USER, dangerous writes become ADMIN**, for the three surfaces the
product links to: `/verification` (page USER, `bulk`/`run`/`recover` ADMIN),
`/contacts/{id}` (record USER, `/verify` ADMIN), and the knowledge base (all
sections USER except restricted-claim writes).

### Two UI fixes that were part of the same gap

The "Operator Workbench" link was rendered to **every** user, outside the
`is_admin` block, and pointed at `/`, which redirects back to `/app` — so it was
simultaneously a dead link and an advertisement. And the agent-control form is
now withheld from a non-administrator with a sentence saying who to ask, rather
than shown as a button that answers 403.

### Gmail ownership — checked, already correct, unchanged

The mailbox is keyed to the durable `User.id` resolved from the session, the
service layer takes `user_id` as a keyword-only argument with no `by_email` or
`by_subject` variant and no admin override, and
`test_an_administrator_does_not_inherit_another_users_mailbox` already pins it.
No change was made, and `/gmail/*` is deliberately **not** admin-gated, because
a USER connecting their own mailbox is core product.

## MillionVerifier safety — what is actually true

Investigated as its own question and verified directly rather than taken from
the docs. **No live provider call was made and no provider flag was enabled.**

1. **What `FEATURES__MILLIONVERIFIER` gates.** The legacy `/verification`
   console routes, the admin display, and the smoke script. Every read site is
   in `app/web/routes.py`, `app/services/admin_workbench/reader.py`,
   `live_smoke.py` and `scripts/verify_live_smoke.py`.
2. **What makes the Verification Agent spend.** An `ENABLED` Verification
   control on an execution-enabled campaign, whose effective config carries
   `{"live": true}` (`adapters.py:804`), with a real non-test key — reaching
   `waterfall.py:101` → `provider.py:494` → `urlopen` at `provider.py:296`.
   The feature flag is **not** on that path; `grep` finds no read of it anywhere
   under `app/services/agents/` or `app/services/verification/` except the smoke
   script.
3. **DRY_RUN does not affect it.** `grep -rn "dry_run" app/services/verification/
   app/services/agents/` returns nothing. `DRY_RUN` concerns sending. The
   overview banner reading "no real email can be scheduled" is true about
   sending and says nothing about verification credits.
4. **Minimal live-UAT config**, derived from code: a real key
   (`MILLIONVERIFIER_API_KEY` or an active Agent Studio credential), campaign
   execution on, Verification control `ENABLED`, effective config `{"live":
   true}`, a queued job with a valid non-suppressed candidate and no fresh
   cached evidence, and a running agent worker. `FEATURES__MILLIONVERIFIER` is
   **not** required.
5. **Legacy bulk routes are independently exposed** — `/verification/bulk`,
   `/verification/run`, `/verification/recover` and
   `/contacts/{id}/verify`, plus the Agent Studio live test and credential
   rotation. All were reachable by a normal USER; all are now ADMIN.

Four documents claimed the feature flag was required for live verification,
which implied it was also a brake. Corrected in `CURRENT_MVP.md`,
`PHASE_2_EXECUTION_MODEL.md`, `ARCHITECTURE.md` and `README.md`, and stated
where an operator sets the key.

## P0-3 — the seven-message opt-in control

`cadence_config["sequence"]["enabled"]` had **no writer anywhere in the
product** — it could only be set by editing JSON by hand. The campaign settings
form (`/app/campaigns/{id}/edit`) now carries the switch.

The *create* form deliberately does not. A campaign is opted in immediately
after creation with one click on the settings form, the two forms would need the
same three-way flag/absent/unchanged handling duplicated, and the smaller slice
is the one worth shipping into a boundary change. Say the word and it is a
five-line addition.

Three things it had to get right, all easy to miss:

- **Unrelated keys survive.** The writer copies the existing object and sets one
  key, because the column belongs to the campaign and the cadence module claims
  only one of them.
- **The flag is a real `bool`.** `campaign_opted_in` tests `is True`, so an HTML
  checkbox arriving as the string `"on"` would have written a value that reads
  back as *not* opted in — a control that appears to work and does nothing.
- **Absent means unchanged when the control was never offered.** The switch is
  rendered only when `FEATURES__EMAIL_SEQUENCES` is on. Reading the checkbox
  unconditionally would have silently opted a campaign *out* every time somebody
  renamed it in an environment without the control.

The write goes through `update_campaign`, so it gets the settings-version bump
and the audit event that function owns; nothing writes the column directly from
a template or handler. The fixed cadence — days 0, 3, 7, 12, 18, 25, 35 — is
displayed and **not** editable here, and is unchanged in code. Campaigns whose
`cadence_config` is NULL or malformed keep working, because the accessor already
treats both as absent.

**On campaign `588b3e15-8c39-4d5f-962b-ff1b00d76412` (PE&VC MENA 200-1000):**
answered from code, not from the row. The edit form operates on any campaign by
id and is backward-compatible with a NULL `cadence_config`, so it will be
updatable through this UI once `FEATURES__EMAIL_SEQUENCES` is on in staging. I
did **not** query the staging database to inspect that specific row — that would
have meant touching the VPS, which is out of scope here. Worth confirming during
UAT.

## P0-4 — safe activation

**The trap is real, and worse than reported: it does not need a Resume at all.**

A disabled *skippable* agent is not held, it is stepped over permanently.
`schedule_next` moves the stage to `SKIPPED` with
`reason_code="control_disabled_autoskip"`, and `SKIPPED` is absorbing —
`app/services/pipeline.py:89` gives it `frozenset()` outgoing transitions,
`schedule_next` returns early for it, and the re-run path does not list it as a
stopped stage. Enabling the agent afterwards recovers nothing. The same skip
also fires on ordinary worker progress with execution already on, one contact at
a time, with no operator action whatsoever.

The three skippable agents are Research, Insights and Personalization — exactly
what the seven-message workflow depends on. Skip Personalization and the
campaign produces **no messages at all**, silently, while reporting every
contact complete.

The auto-skip itself is deliberate and worth keeping; what was missing was any
check that the operator had finished configuring *before* the walk starts — and
switching execution on starts it for every contact at once.

**The mechanism.** `app/services/agents/readiness.py` distinguishes two
failures, and only one is worth refusing over:

- **Blocking** — a skippable agent that is `DISABLED`. Resuming burns it
  irreversibly. This refuses the resume.
- **Holding** — a non-skippable agent that is disabled, or a skippable one that
  is merely `PAUSED`. Work waits and resumes later; nothing is lost. This is
  reported, not refused, because a deliberately partial pipeline is legitimate.

The refusal lives in `set_campaign_execution`, the one function both the UI and
the JSON API reach, and runs before `execution_enabled` is written, so a refusal
leaves nothing half-applied. It fires only on a state *change*, so re-affirming
execution on a running campaign does not start failing, and only for campaigns
opted in to sequences, so nothing else changes behaviour. The message names the
agents and says what would be lost; the campaign page says the same thing before
the button is pressed.

One subtlety worth flagging for review: controls are re-resolved in this module
rather than read from `effective_control`, because that function reports
everything as disabled with `source="campaign_execution"` while execution is off
— the exact state the check runs in — and would have refused every resume. Only
that one layer is skipped; registry default, global control, campaign override
and unimplemented-adapter handling are resolved identically, and the two must
not drift.

**Sending is untouched and stays disabled.** It has no adapter, so it can never
be enabled, and refusing on it would be a refusal with no operator remedy — it
is excluded from the check rather than made a requirement. Nothing here enables
an agent on the operator's behalf, and no terminal historical job is mutated.

**What the preflight does not cover — residual risk, stated deliberately.** It
guards the operator action the brief named: the off→on transition of campaign
execution. It does **not** guard the second trigger I found, which is ordinary
worker progress on an already-running campaign. So if a sequence campaign is
already running, an agent is disabled afterwards (or contacts are enrolled
afterwards), those contacts can still reach the terminal skip with no operator
action. Closing that would mean either refusing the disable itself or changing
the auto-skip, both of which are larger behaviour changes than this repair
authorises. The preflight covers the mass-loss case — one click burning every
contact in the campaign at once — and the residual case is one contact at a
time. Worth a follow-up decision.

## P0-5 — review copy controls

Copy existed on the contact page and not on `/app/review`, the primary review
surface.

`/app/review` renders **one** message body at a time. That is a documented,
deliberate design pinned by three tests, and the contact page is the existing
all-seven surface. Per your decision, the design is kept: the selector lists all
seven, each is one click away, and **each of the seven now carries Copy Subject
/ Copy Body / Copy Full Email as it is opened**. The alternative — expanding all
seven bodies here — would have reversed that decision and broken those tests.

The controls are the contact page's markup driven by the same delegated handler
in `static/sequence.js`, which is **unchanged**: it is keyed entirely on data
attributes with no page-specific coupling, so reusing it avoids a second copy
mechanism. Ids are keyed on the version id, so two messages on one page can
never target each other's text. Subject and body stay separate nodes, so the
boundary is preserved and a copy takes the intended message only. They read the
un-neutralized display nodes deliberately — neutralization exists for imported
spreadsheet values and would corrupt a body opening with `-` or `+`.

Copying is not a decision: `type="button"`, the handler calls `preventDefault`,
and the controls sit outside `v2-mail-foot` and outside the edit `<details>`, so
no approve/discard/edit state is touched and the immutable edit lineage is
unaffected. Approval is still not sending authority, and Gmail draft creation
remains a separate explicit action — still deliberately absent from this page,
because it drafts all seven and this page shows one.

## P0-6 — deployment contradictions

Only the ones that would fail the deploy or teach the wrong boundary. The
general documentation backlog is untouched, deliberately.

**The one that would have failed the deploy.**
`AUTH__ALLOWED_OPERATOR_EMAILS` shipped as `["REPLACE_WITH_OPERATOR_EMAIL"]`,
unquoted, and fails two ways — both reproduced here:

- left as shipped, the value is not an email address and the validator refuses
  it;
- replaced with a real address but still unquoted, the shell strips the inner
  double quotes when `vmr-deploy` sources the file, so it arrives as
  `[ops@example.com]` and does not parse.

Both surface at `alembic upgrade head` — **after** the database backup, before
the symlink moves — and neither is caught by the release import check, which
runs under `VMR_TEST_MODE` and never builds real settings from the env file.
systemd's parser keeps the quotes, so `vmr-web` would have started from the same
file, which is what makes it confusing. The file states this rule at the top and
then broke it. Now empty and single-quoted, with the same fix applied to the two
commented `EXTENSION_AUTH__` placeholders somebody will uncomment verbatim.

**`LOGO_DEV_API_KEY` was listed as deliberately unset**, contradicting its own
block in the same file, the staging runbook, and the code. With capture
promotion on outside local development the application **refuses to start**
without it; with promotion off every hosted capture stays pending forever while
every Capture job reports success. So either the deploy fails its health gate or
the capture UAT proves nothing. Removed from that list, with the consequence
stated.

**The stale auth claims.** The nginx snippet still said the application
authenticates none of the surface it fronts, and `HOSTED_AUTH.md` still named
the allow-list as what decides who may sign in. The snippet ships `deny all`
either way, so no directive was wrong — but read together they teach that
removing an address from `AUTH__ALLOWED_OPERATOR_EMAILS` revokes access. It does
not: that list is a startup seed, and revoking means disabling the account.

**The Gmail note** called mailbox authorization unimplemented and spelled the
settings group `GMAIL_` rather than `GMAIL__`. It fails closed so it cannot
break a deploy, but a Gmail UAT had no documented env block.

## Adversarial security review — and what it caught

A focused adversarial review was run against the finished boundary, told to try
to break it rather than to confirm it. It found one real hole and two pieces of
product breakage, all of which this branch introduced and all of which are now
fixed. Recording it here because the hole is the kind that passes every test you
would think to write.

### The bypass — confirmed, reproduced, fixed

**Any signed-in USER could reach the entire administrator surface by appending
`%0A` to the path.**

The policy decides by string comparison. Starlette's router decides by
`re.match("^/admin$", path)`. Python's `$` matches at end-of-string **or
immediately before a single trailing newline**, and uvicorn percent-decodes the
target before either matcher runs. So `GET /admin%0A` arrived as `/admin\n`,
which `==` called "not the admin path" and the router called `/admin`.

I reproduced it directly: `re.compile(r"^/admin$").match("/admin\n")` is `True`,
`is_admin_only_request("/admin\n", "GET")` was `False`, and a real Starlette app
serves `GET /admin%0A` with status 200.

Every path covered only by whole-string equality was reachable this way —
`/admin`, `/docs`, `/openapi.json`, `/redoc`, `/workbench`, `/campaigns`,
`/imports`, and `POST /campaigns`, which is a write. Prefix-matched paths were
never affected, because `"/admin/agents\n".startswith("/admin/")` is true. nginx
forwards `%0A` unmodified, so it would have reached staging.

Fixed in two layers. The middleware refuses any path carrying a C0 control
character or DEL before any access decision — no route in this application has
one, so this kills the family rather than the one spelling. And
`normalize_request_path` strips trailing control characters, so the policy and
the router agree on their own terms rather than only because something upstream
filtered the input. Both are pinned by new tests, including an anti-vacuity test
that ordinary paths are unaffected.

### `/review` — I had misclassified it

`/review` is not the legacy twin of `/app/review` that it looks like. It is
ambiguous-import triage: where an operator confirms whether two records are the
same person, deliberately never merged automatically because merging the wrong
two is not reversible by a retry. It is reached from a first-class decision card
on the operator's own campaign page, and there is no equivalent under `/app`.

Gating it meant a USER with ambiguous imports clicked "Review the matches" and
got a raw JSON 403, with no route to the work at all. The brief lists
"contacts" and "review" as USER product, so this was my error, not an ambiguity.
`/review` and its three child routes are USER again; `/campaigns` stays ADMIN.

### Two links the template pass missed

`/app/agents` still rendered "Open the full queue in the admin Workbench" and
"Open the verification console" to everyone. Both now render for administrators
only — the first is an admin surface by its own label, and the second opens a
console whose every action spends provider credits and refuses a USER.

### Three smaller issues in my own new code, fixed

- `bool(sequence_enabled)` treated any non-empty string as "on", so a client
  posting `sequence_enabled=false` would have opted the campaign **in**. It now
  uses the same wording the execution toggle already accepts, via a shared
  helper so the two cannot drift.
- `with_campaign_opt_in` *deleted* a malformed `cadence_config` where the reader
  only reads past it. The docstring claimed the two were symmetric and they were
  not. It now refuses with a reason rather than discarding stored data, and the
  route surfaces that refusal.
- A lost-update window on `cadence_config` (read-modify-write with no row lock).
  Latent — there is exactly one writer today — so it is recorded rather than
  fixed, since adding locking here is a wider change than this repair carries.

### What the review found that I deliberately did not change

- **logo.dev is a second metered provider, and its two callers sit on opposite
  sides of the boundary.** `POST /imports/{id}/enrich/lookup` is ADMIN via the
  `/imports` prefix; `POST /contact-captures/{id}/company/lookup` is USER, and
  passes `force=True`, which bypasses the cache and so can be replayed. This is
  pre-existing, and the brief explicitly assigns the capture workflow to USER —
  gating it would break capture promotion, which is on the UAT path. Flagged
  rather than changed, because the inconsistency is a product decision.
- **`POST /knowledge-base/generate`** is USER-reachable and fetches
  operator-named URLs then invokes the Claude CLI. Also pre-existing; KB editing
  is USER product by the same contract. Worth an explicit decision, not a
  unilateral change here.

### Categories the review cleared

No finding, having been checked specifically: case sensitivity, `%2f` and
encoded separators, null bytes, trailing dots/spaces, `;params`, Unicode
homoglyphs, trailing slashes, prefix near-misses, the verb-split patterns
against every route they cover, middleware ordering (three `await self.app(...)`
sites, all accounted for), the anonymous set being disjoint from the admin set,
role-source tampering, and extension credential escalation — which is tight,
because the contract is checked on path *and* method before the credential is
touched. And the enumeration of every caller that can reach
`HttpMillionVerifier`: **no route that reaches it is USER-reachable.**

## Four existing test files were changed — read this before the diff

A changed assertion is the easiest place to hide a regression, so every one is
listed here with its reason. **No assertion was weakened to make a failure go
away**; three of the four are identity changes that let a test reach its own
subject again, and the fourth replaces one invariant with a stricter one.

**`tests/test_v2_customer_ui.py`** asserted `/app/review` carries no `<script>`
at all. P0-5 requires copy controls there, and a clipboard write needs script —
the same static `sequence.js` the contact page already loads, which is why the
contact page was never in that list either. The invariant genuinely changed with
the product. Rather than delete the line, `/app/review` is now asserted
positively and more tightly: exactly one external same-origin script, no inline
script (the CSP has no nonce), and still no `data-live`. Every other page in
that list is untouched and still must be script-free.

**`tests/test_hosted_auth.py`**, **`tests/test_hosted_auth_raw_asgi.py`** and
**`tests/test_extension_capture_auth.py`** each sign in an operator and then
exercise something that is *not* authorization — CSRF tokens, `Origin` and
`Sec-Fetch-Site` handling, session revocation, and the operator-versus-extension
rule. They all drive those through writes to `/api/...`, which is now
administrator-only for session callers, so an ordinary operator was refused on
authorization *before* reaching the behaviour under test. Ten tests failed that
way.

The operator in those three files is now an administrator. That restores what
each test was written to prove rather than papering over it; the extension test
in particular only demonstrates "a signed-in operator is not the extension" if
the operator gets past the admin gate first — otherwise it asserts the wrong
refusal and would keep passing if the extension rule were deleted. Role itself
is covered independently in `tests/test_user_accounts.py` and
`tests/test_route_authorization.py`, so nothing is lost by these files not
testing it.

Two details from that change are worth a reviewer's eye, because both were
initially wrong:

* In `test_hosted_auth.py` the fix belongs in the autouse account fixture, not
  in the environment. That `TestClient` is built without the lifespan, so
  seeding an administrator through `AUTH__BOOTSTRAP_ADMIN_EMAIL` does nothing —
  the row comes from `seed_account`. My first attempt set the env var, which
  looked like a fix and was inert. It has been reverted rather than left in.
* That fixture now seeds a **second** administrator. Two tests disable and
  reactivate the approved account to prove a session dies with it, and with a
  single administrator in the database the user service correctly refuses the
  disable — *"This is the only active administrator"* — so the session assertion
  was never reached. The guard keeps its own coverage in
  `tests/test_user_accounts.py`.

Worth a reviewer's judgement: the alternative was to leave those tests failing
and change the boundary so authorization runs *after* CSRF. I did not, because
refusing on the stronger check first is the right order, and because the
`/api`-as-admin decision was yours rather than mine to reverse.

Everything else in `tests/` on this branch is new.

## Validation

Gate sequence, run on this exact head:

| Gate | Result |
| --- | --- |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 540 files already formatted |
| `mypy app` | Success — no issues in 274 source files |
| `alembic upgrade head` | applied to head |
| `alembic check` | No new upgrade operations detected |
| `alembic downgrade base && alembic upgrade head` | clean round trip |
| `alembic heads` | exactly one — `b45732880eff` |

The migration gates were run against a scratch database rather than the
developer database, because `downgrade base` is destructive. This branch adds no
migration: `git diff a10c1de1 HEAD -- migrations/` is empty.

### Focused suites

Run isolated, each batch against its own scratch database. That isolation is not
ceremony — see the note below.

| Batch | Result |
| --- | --- |
| `test_route_authorization.py`, the three new workflow files, `test_v2_customer_ui.py`, `test_deployment_constraints.py`, `test_campaign_pipeline_web.py`, `test_campaigns.py`, `test_phase2_orchestration.py`, `test_agent_rerun.py` | **423 passed, 0 failed** |
| `test_hosted_auth.py`, `test_hosted_auth_raw_asgi.py`, `test_hosted_auth_templates.py`, `test_user_accounts.py`, `test_extension_capture_auth.py`, `test_gmail_draft_integration.py`, `test_hosted_capture_promotion.py`, `test_email_sequence.py`, `test_config.py`, `test_dev_tooling.py` | **802 passed, 0 failed** |

That is every suite the brief asked for: user/admin authorization, auth
middleware and raw ASGI, admin users, extension bearer auth, Gmail
ownership/actions, campaign settings and create/edit, campaign execution and
controls, agent skip/hold, email sequence and review, MillionVerifier route
authorization, hosted capture promotion, and deployment/config. No migration
test was needed because no schema changed.

New tests added by this branch: **161** authorization (including the newline
bypass regression), **18** sequence control, **20** execution readiness, **14**
review copy, **7** deployment closure and env-quoting guards.

### A note on how the suite behaves under load

Running many of these files in one pytest session produced 58 failures that were
almost entirely an artefact. One test hits a genuine `InFailedSqlTransaction`,
which poisons the shared session, and everything downstream then fails with 500s
that have nothing to do with the code under test. Re-running the same files in
isolation reduced 58 failures to 2.

Both of those 2 were real, and both were mine — a missing-campaign lookup that
turned a 404 into a 200, and the `/app/review` script invariant. Both are fixed.
I am recording the artefact because the raw number is alarming and the
explanation is not obvious: **if CI reports a similar cascade, re-run the failing
file alone before believing it.**

### Pre-existing local failures, stated plainly

`tests/test_email_sequence_web.py` and `tests/test_v2_beta1_operator_ui.py`
report **25 failures on this machine, and the identical 25 on the untouched base
commit**. I verified this by stashing the branch's changes and running the same
two files: 25 failures on base, 25 on the branch, and the sets are byte-identical
(`comm` reports no difference in either direction). They involve the `=`/`+`/`-`/
`@` formula-neutralization parametrizations and cascade into
`InFailedSqlTransaction`. CI #361 was green on this exact base SHA, so these are
a local-environment artefact, not a regression, and they are not mine to fix.

Per the brief, GitHub CI is the full-suite authority.

## What I did not do

- No deployment, and nothing on the VPS was read or mutated.
- No Google Cloud configuration; Gmail was not enabled.
- No MillionVerifier or provider flag enabled anywhere, and no live provider
  call made.
- Sending was not enabled and was not made a requirement.
- No scheduler, no polling, no automatic sending.
- Agent Studio was not redesigned; only its authorization changed.
- No self-service signup or password reset.
- Gmail OAuth architecture unchanged.
- No migration; no rewrite of existing ones.
- Capture-promotion semantics untouched.
- The fixed seven-message cadence is unchanged.
- I did not merge the PR.

## Deferred, with reasons

- **`elapsed_days` editing.** The validator already accepts a per-campaign
  cadence override, and no surface exposes it. Exposing it needs seven numeric
  inputs and a write-time `CadenceError` surface, and the brief says the
  operator must not be able to edit the offsets in this repair. Left alone.
- **Splitting `POST /app/agents/{id}/control` by scope.** Campaign-scoped agent
  overrides are arguably operator work while the global variant is
  administration, but the two differ by a form field the middleware cannot see,
  so splitting them means a second enforcement site. The whole route is ADMIN
  for now, the page still shows current state to everyone, and the resume
  preflight tells an operator exactly what to ask for. Worth revisiting if
  operators find it restrictive.
- **The two apparently-dead intake routes** (`/api/intake/sales-navigator/stage`,
  `/api/intake/linkedin-profile/stage`). The extension has not produced either
  contract since 2.0 and no call site exists, but they are still mounted. They
  are ADMIN-gated now; deleting them is a separate decision.
- **The "39 documented operations" count** in the nginx config and runbook. I
  did not re-derive it, so I did not touch it. Cosmetic.
- **`APP_PORT` filed under Runtime** in the env example as though the
  application reads it. It does not — only systemd and `vmr-deploy` do.
  Cosmetic, recorded, not fixed.
- **`GMAIL__*` has no startup validation.** Enabling `FEATURES__GMAIL_DRAFTS`
  without its prerequisites produces a 404 rather than a refusal, which is
  inconsistent with the promotion and extension blocks that refuse to start on a
  half-configuration. Fails closed, so not a deploy blocker. Belongs in
  `docs/POST_LAUNCH_BACKLOG.md` if you want it.

## Remaining staging-only configuration

None of this is code, and none of it was done from here.

1. `FEATURES__EMAIL_SEQUENCES=true`, or the seven-message switch does not render
   and campaigns cannot be opted in through the UI.
2. An ADMIN account must exist, and at least one USER account for the beta
   operator. There is no public signup; an administrator creates the account.
3. Research, Insights and Personalization must be `ENABLED` before the operator
   starts a sequence campaign — the preflight now refuses otherwise and names
   them. Email and Verification will hold rather than skip, so they can be
   enabled later without loss.
4. Enabling Verification `{"live": true}` with the key already in
   `/etc/vmr/vmr.env` spends real credits. `FEATURES__MILLIONVERIFIER` does not
   prevent that and `DRY_RUN` does not either.
5. `LOGO_DEV_API_KEY` must be set if `FEATURES__CONTACT_CAPTURE_PROMOTION` is
   turned on, or the application refuses to start.
6. Gmail drafts additionally need `FEATURES__GMAIL_DRAFTS` plus the `GMAIL__*`
   keys in `docs/GMAIL_DRAFTS.md`.
7. `AUTH__ALLOWED_OPERATOR_EMAILS` in the live `/etc/vmr/vmr.env` should be
   checked for the same quoting defect the example carried.

## Claimed status

The six P0 items are implemented, and the two safety-critical ones — provider
spend and terminal auto-skip — are closed at the service boundary rather than in
the UI. I am not grading this: it needs ChatGPT's independent review against the
actual diff and CI, and the Hosted Beta workflow still needs a real UAT pass on
staging with the configuration above. Green CI does not meet that bar.

---

# Repair pass 2 — after the independent adversarial review

Cowork reviewed `ea385985cb9033fb9286bf7c535b0a3e7eca4d79` against base
`a10c1de1c535f26bdc2825a3a9ffc127557b7ecb` and returned **OPERATOR READINESS
REVIEW: REPAIR REQUIRED**. The artifact is
`OPERATOR_READINESS_ADVERSARIAL_REVIEW.md` (supplied out-of-band, not committed
here). This pass implements the review's own "minimum path to pass" and nothing
beyond it. No rebase, no squash, no force, no history rewrite, no migration, and
nothing deployed.

## What the review found, and what closing it cost

Three blockers were reproduced by the reviewer through the supported operator
UI, with no administrator and no API. All three are closed here, and each was
reproduced locally *before* being repaired — a fix with no failing test in front
of it is not evidence.

### B-1 — an empty campaign defeated the preflight entirely

`_furthest_desired_stage` returns `None` for a campaign with no contacts, so
`execution_readiness` reported `runnable=True` and Resume was accepted. The
guard was scoped to the off-to-on transition, so it never ran again. Contacts
imported afterwards walked into a disabled Research and were terminally
`SKIPPED`.

Closed by re-evaluating readiness **at enrolment**, in `enrol_contact` — the
choke point all six enrolment surfaces pass through, so the refusal is atomic by
construction. `campaign_import_confirm` asks the same question before the import
starts, because `campaign_import.confirm` catches only `SQLAlchemyError` per row
and a mid-file refusal would otherwise have escaped as a 500 with a batch
partially written.

**Refuse rather than hold, deliberately.** `enqueue=False` is not an escape
hatch: in `schedule_next` the auto-skip branch runs *before* the
`allow_enqueue` check, so a deferred enqueue burns the stage just as thoroughly.
A true hold would strand the membership — `reconcile_agent_control` only
re-schedules rows matching `next_stage == agent_id` or holding a live job, and a
held contact sits at Identity, never names Research, and has no job. Enabling
Research later would never find it. That is the silent import drop the brief
forbids. Refusal loses nothing: a Contact is permanent and never requires a
campaign.

The campaign page now renders the readiness warning **while running**, not only
before the first Resume, so the state is visible before an operator imports.

### B-2 — the operator could bypass the refusal in three clicks

Untick the sequence box, Save, Resume (now accepted), re-tick, Save. The
refusal message actively pointed at the page containing the bypass.

Closed on the **write transition**: `update_campaign` re-runs readiness when the
sequence opt-in goes false-to-true on a campaign that is running or has enrolled
contacts, and refuses above the SAVEPOINT. Nothing is partially persisted and
`settings_version` is not bumped on a refusal. The edit route is **not** made
administrator-only — that would have hidden the defect rather than fixed it.

### B-3 — one control-disable click burned an unbounded in-flight cohort

`reconcile_agent_control` selects every matching membership with no `LIMIT` and
called `schedule_next`, which terminally skipped each one, under an operator
message that read *"nothing is discarded."*

`schedule_next` gains `allow_autoskip: bool = True`; the reconcile path passes
`False`, so a control flip **holds** work at the disabled boundary instead of
destroying it. The ordinary scheduler keeps its auto-skip, which is historically
intended. `_IN_FLIGHT_NOTES[DISABLED]` no longer claims nothing is discarded.

Because `set_campaign_execution` reconciles through the same function, the
Resume-triggered burn is closed by this change too.

### H-1 and H-2 — two metered surfaces were USER-reachable

`POST /contact-captures/{id}/company/{lookup,resolve}` pass `force=True`, which
bypasses the one-lookup-per-company cache, so N presses were N billed logo.dev
calls. `POST /knowledge-base/generate` spawns the Claude CLI with `WebSearch`
and operator-supplied URLs. All three are now administrator-only.

`confirm`, `correct`, `reject` and `promote` were read before being left with
the USER and spend nothing: two write a decision onto the stored enrichment
record, `resolution.correct` records a correction with
`provider_call_made=False`, and `promote` evaluates already-stored state.
`/app/capture` is neither read-only nor administrator-only.

The false comment in `policy.py` — *"only `/contacts/{id}/verify` reaches a paid
provider"* — is corrected and now names all three paid dependencies.

### H-3 and M-2 — operations documentation that would have been acted on

`docs/HOSTED_AUTH.md` section 7 described a revocation that revokes nothing:
`is_approved` has **zero call sites in `app/`**, and `seed_from_allowlist` only
ever creates rows. Removing an address and restarting left the operator with
full access. Section 7 now states that the `users` table is authoritative, that
operators are added at `/app/admin/users`, that **disabling the account** is the
revocation, that it takes effect on the **next request** via `auth_version`, and
that **no restart is required**. Section 5's staging requirement is corrected
from a non-empty allow-list to a non-empty `AUTH__BOOTSTRAP_ADMIN_EMAIL` —
acting on the old text would have reproduced the pydantic crash this PR fixed,
at the Alembic step, after the database backup.

`deploy/README.md`, `deploy/nginx/vmr-staging.conf` and
`docs/STAGING_RUNBOOK.md` no longer claim the application has no authentication,
and no longer justify HTTP Basic Auth or IP allow-listing on that ground. The
genuine infrastructure hardening advice is kept; only the false premise is gone.

## The "25 baseline failures" claim is withdrawn

The earlier section of this handoff recorded *"25 failures in
`test_email_sequence_web.py` and `test_v2_beta1_operator_ui.py`, identical 25 on
the untouched base."* **That claim is wrong and is not repeated.** Measured
here, on this machine, with a fresh database per run:

| Tree | Result |
|---|---|
| `ea385985` (reviewed head) | **76 passed, 0 failed** |
| `a10c1de1` (untouched base) | **76 passed, 0 failed** |

Both sides clean, both files, exit code 0. The original number was almost
certainly a poisoned shared session leaking from a prior full-suite run — the
same cascade this handoff describes ten lines above it and then failed to apply
to its own measurement. A number produced under a known-contaminating condition
should not have been reported as a baseline at all.

## Deferred, deliberately, with the risk stated

Scope was cut to the review's minimum path. These remain **open** and are not
fixed here:

- **H-4 — `enrol_contacts` has no server-side batch bound.** A normal USER can
  enrol unbounded contacts, and once an administrator arms Verification with
  `{"live": true}` that is uncapped MillionVerifier/DeBounce spend through
  ordinary product actions. Campaign and contact volume is therefore a cost
  boundary. Documented in `deploy/vmr.env.example`; not capped.
- **H-5 — sequence-*disabled* campaigns still burn skippable stages.** The
  reconcile path is fixed by B-3, but ordinary worker progress still auto-skips:
  a contact completing Identity walks into a disabled Research and is terminally
  skipped. For sequence campaigns B-1 and B-2 make that state unreachable; for
  single-draft campaigns it is unguarded and unwarned, as before.
- **M-7 and M-8 — the two test gaps.** No behavioural CSRF-refusal test on an
  `/app` write, and the control-character middleware guard is still proven only
  through its pure helper. M-8 was confirmed real in passing: with the guard
  disabled in source, 74 wire-level requests failed while **every** existing
  control-character test still passed. Those wire-level tests were written and
  then removed under the scope cut; `tests/test_hosted_auth_raw_asgi.py` and
  `app/core/auth/middleware.py` are byte-identical to the reviewed head.
- **The auto-skip recursion drops `generation`.** An operator re-run at
  generation N that auto-skips schedules the following stage back at generation
  1, where `enqueue_job`'s idempotency can hand back a stale job. Not
  load-bearing for these repairs — the reconcile path no longer enters that
  branch — but real. Belongs with whoever owns `rerun.py`.
- Everything the review listed under MEDIUM and LOW that is not named above,
  including tenancy (M-10), the legacy navigation (M-1), the audit actor (M-6),
  and the JSONB warning poison row (L-12).

## Two existing test files were modified — read this before the diff

Neither is a weakened assertion.

**`tests/test_campaign_execution_readiness.py`** — a fixture *reorder* in
`test_re_affirming_execution_on_a_running_campaign_never_refuses`. It enrolled
into an already-running, already-blocked sequence campaign, which is now
refused. Contacts are enrolled first and the switch is then flipped directly,
which is the only order that state can now be reached in. Every assertion is
untouched, including `runnable is False`.

**`tests/test_import_to_sequence.py`** — one autouse fixture enabling Research,
Insights and Personalization. The module's campaigns are execution-enabled and
sequence-opted-in while those three sat at their registry default of DISABLED,
so its imports were walking contacts into a terminal `SKIPPED` at Research and
then generating sequences out-of-band through the adapter, where the pipeline
could not contradict them. The fixture states the configuration those tests
always implied. Nothing about import truth, provenance or sequence content
depended on the Agents being off. No test was deleted, skipped, xfailed or
relaxed.

## Beyond the minimum path, already complete when scope was cut

Three items were finished and green before the scope reduction and were kept
rather than reverted, because removing working, tested corrections would have
cost time and restored known-false text:

- `constraints.txt` — the glibc floor note (L-1) was wrong in both halves. The
  brittle prose is removed rather than re-stated with new numbers, because a
  number in a comment goes stale silently and is then acted on as though
  something had checked it. **No pinned version changed**; the closure is 66
  pinned lines, byte-identical to the reviewed head.
- `deploy/vmr.env.example` — records that synchronous verification spend is
  administrator-only while ordinary USER campaign operation can cause
  asynchronous Agent spend once live Verification is armed.
- `tests/test_review_copy_contract.py` — a static cross-check that the copy
  controls' JavaScript literals equal the strings the templates emit (M-9).
  Mutation-verified: renaming the live-region id, the label attribute or the
  delegation selector in the script alone each fail it.

## Validation for this pass

Local validation was scoped to the blocker reproductions, the touched-file
suites, the static gates and the dependency closure. GitHub CI is the broad
authority, as the brief directs.

One consolidated run on the assembled head, thirteen suites, fresh database:
**659 passed, 0 failed, exit code 0.** The suites are
`test_campaign_enrolment_readiness`, `test_agent_control_disable_holds`,
`test_provider_spend_authorization`, `test_campaign_execution_readiness`,
`test_campaign_sequence_control`, `test_import_to_sequence`,
`test_route_authorization`, `test_phase2_orchestration`,
`test_review_copy_contract`, `test_deployment_constraints`,
`test_extension_capture_auth`, `test_hosted_auth` and
`test_hosted_auth_raw_asgi`.

Static gates on the same tree: `ruff check .` all checks passed;
`ruff format --check .` 547 files already formatted; `mypy app` no issues in 274
source files; `VMR_TEST_MODE=1 python -c "import app.main"` clean.

Each blocker was reproduced before repair. B-1: the enrolment assertion is
deliberately taken *after* walking the contact through Identity and Company,
because enrolment starts a contact three stages short of Research and asserting
at the enrolment instant passes on broken code — the first draft of that test
did exactly that and was corrected. B-2: the re-tick was accepted on unrepaired
code. B-3: `6 of 6 terminally skipped by one click`, the reviewer's figure
verbatim. H-1 and H-2: a USER receives 403 with zero logo.dev calls and zero
subprocess spawns, while an ADMIN making the identical request produces exactly
one of each — the positive control is what makes the zero mean something.

The `vmr-deploy` install pattern **cannot be executed on this machine**:
`constraints.txt` pins `uvloop==0.22.1`, which has no Windows support, so the
requirement install fails on any tree including the untouched base. What is
provable locally, and is what matters, is that the closure content is unchanged:
66 pinned lines on both sides, byte-identical to the reviewed head, with only
comments differing. The reviewer already verified this exact closure end to end
on Linux with the real `vmr-deploy` commands.

## Claimed status

The review's minimum path is implemented and the three blockers are closed at
the service boundary. I am not grading this. It needs ChatGPT's independent
review against the actual diff and CI, and the deferred items above — H-4 and
H-5 in particular — are real, open risks that a Hosted Beta UAT pass must be
told about rather than discover.

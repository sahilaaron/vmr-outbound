# Admin configuration: operator controls versus deployment settings

What an administrator can change from the application, what only the environment
can change, and why the line is where it is.

## The finding this answers

Hosted Beta UAT: the Agent controls were enabled, and every Research job was
paused with `feature_disabled`. The screen said "Company research is switched off
for this deployment", which was true. The only way to change it was SSH to the
VPS, an edit to `/etc/vmr/vmr.env`, and a restart.

That is a deployment procedure standing in for an operating decision. An
administrator running the product should not need a shell to run the product.

## The effective-control contract

A control is in force when all three hold. The Admin Configuration screen shows
all three separately, because the answer is a conjunction and an operator has to
be able to see which part is missing:

1. **Deployment capability** — can this deployment do it at all? A provider
   credential is configured, the environment permits it, a prerequisite control
   is on. No button changes this. A switch whose provider has no API key cannot
   be turned on, and the screen names the missing key rather than accepting a
   setting that would silently do nothing.
2. **The administrator's operational setting** — the durable row in
   `operational_settings`. This is the half that moved out of the environment.
3. **The Agent or Campaign control**, where one applies. Unchanged. A campaign's
   execution switch and the Agent registry still decide what runs for whom, and
   this layer never overrules them.

Worked examples, in the screen's own terms:

```
logo.dev credential configured: YES
Automatic company-domain resolution: ON
logo.dev domain lookup: ON
```

```
logo.dev credential configured: NO
logo.dev domain lookup: cannot be enabled
Reason: the logo.dev provider credential is not configured. Set LOGO_DEV_API_KEY
        in the deployment environment; it cannot be set from this screen.
```

Secret values are never displayed. Only whether a credential is configured.

## The environment is a default, not a ceiling

With no row for a control, the deployment's `FEATURES__*` value is used. The
table is created empty, so this changed nothing on any existing deployment.

With a row, the row wins. It has to: the requirement is that an administrator can
enable Company Research from the application, and Company Research is false in
the environment of the deployment that needs it. Treating the environment as a
ceiling would have satisfied the words "operator control" while leaving the UAT
finding exactly where it was.

The capability check is the part that is *not* overridable, and it is evaluated
on every read rather than only at write time. A credential removed from the
environment after somebody turned a provider on takes effect immediately, and the
screen explains why.

## Classification

`app/services/operations/settings.py` is the single list. A `FeatureFlags` field
that appears in none of the three sets fails a test, so a flag added later cannot
be left unclassified.

### Operator product controls — administrator-controlled

| Control | Capability required |
| --- | --- |
| Company research | Claude CLI command; Claude Research availability. Research has one required source, so this cannot be in force without it. |
| Claude Research availability (legacy `research_claude_fallback` key) | Claude CLI command. Turn this on first. Off blocks Research and never restores deterministic crawling. |
| Company intelligence | Claude CLI command |
| Insights | Claude CLI command |
| Automatic company-domain resolution | — |
| logo.dev domain lookup | `LOGO_DEV_API_KEY` |
| Model domain fallback | Claude CLI command; automatic domain resolution |
| Capture promotion | automatic domain resolution; logo.dev lookup |
| MillionVerifier | `MILLIONVERIFIER_API_KEY`, except in local/dev/test/ci where the deterministic simulator is the documented substitute |
| Email discovery | — |
| Personalization drafting | Claude CLI command |
| Email sequences | — |
| Gmail drafts | Gmail OAuth client; Email sequences |
| Google Sheets add-on | `SHEETS__ALLOWED_AUDIENCES`; Email sequences |
| Spreadsheet import | — |
| Suppression management | operator interface mounted |
| Knowledge Base | operator interface mounted |
| Agent monitor and controls | operator interface mounted |

#### Every enforcement point reads the effective value

A control on this list is only real if the code that *acts* on it resolves it
through `operations.settings.enabled` / `effective_flags`. Reading
`Settings.features.<key>` directly re-introduces the exact problem this layer
exists to remove, and it does so invisibly: the Admin screen keeps reporting the
control as effective because it reads the right thing.

This is not hypothetical. Until 2026-08-16, three enforcement points for Company
Intelligence still read the raw environment flag — the shared worker's drain gate
(`scripts/run_agent_worker.py`), the standalone worker's refuse-to-start check,
and the router mount in `app/main.py`. On the staging deployment
`FEATURES__COMPANY_INTELLIGENCE` was false while an administrator had turned the
control on, so the screen said effective, the Research handoff kept enqueueing,
**24 jobs sat at `PENDING` with `attempts=0`**, and every page in the Company
Intelligence area answered 404. Nothing logged an error; the system simply did
less than it claimed.

Two rules follow:

* **Never gate behaviour on `Settings.features.<key>` for a key in this table.**
  Use `operational.enabled(session, key)`, and prefer `operational.refusal(...)`
  when the caller needs to explain itself.
* **A router may not be mounted on a product control.** `create_app` runs once
  and no database row can re-run it, so mounting on the flag makes the control
  unreachable from the product. Mount unconditionally and refuse per request —
  `app/web/company_intelligence.py::require_intelligence_enabled` and
  `app/api/integrations_sheets.py::_require_enabled` are the two worked examples.
  Controls that genuinely cannot be decided at request time belong in the
  deployment-only set below, not here.

### Deployment and security settings — environment only, no write path

`workbench`, `salesnav_intake`, `linkedin_profile_intake`,
`linkedin_profile_refresh`, `linkedin_company_intake`, `contact_capture_intake`,
`claude_mcp_bridge`.

Each is shown read-only with the reason it is not editable. Two reasons recur:

* **Startup validation.** `app/core/runtime.py` and `app/core/auth/startup.py`
  refuse unsafe combinations once, before the service starts serving. A runtime
  switch that could turn one on would walk straight past the check that exists to
  refuse exactly that state.
* **Mount time.** `workbench` decides whether the routers exist at all, which is
  settled when the process starts. No database row can change that without a
  restart, and pretending otherwise on a screen would be a lie.

Everything with a `repr=False, exclude=True` secret — `DATABASE_URL`, OAuth
client secrets, provider API keys, the encryption and session keys, trusted hosts
and proxies, `APP_ENV`, `DRY_RUN` — has no representation in this layer at all.
`set_control` refuses any key outside the product-control registry, so a
hand-crafted POST naming one is refused server-side and not merely absent from
the form.

### Declared, not consulted

`normalization`, `deduplication`, `scoring`, `saleshandy`. They exist in
`FeatureFlags` and nothing reads them. Listed on the screen under their own
heading, because offering a switch that does nothing is worse than not offering
it, and hiding them would imply the set of switches is the set of behaviours.

## Research recovery

Turning a control back on has to answer what happens to work it already refused.

A feature refusal is a **pause**, not a failure and not a skip: the Research
adapter raises `AgentBlocked("feature_disabled", ...)`, the orchestrator calls
`jobs.mark_paused`, and the stage goes to `BLOCKED`. The job, its attempt count
and its stage all survive, which is what makes this recoverable.

`orchestrator.reclaim_feature_paused_jobs` is the way back, and it is the same
mechanism `reconcile_agent_control` already uses when an Agent control is
re-enabled: `jobs.resume_paused` with an explicit set of pause classifications
the caller owns. Only `feature_disabled` is resumed. A job paused because an
operator paused the membership, because a suppression fired, because the Agent
control is off, or because the campaign's execution switch is off is untouched —
those have their own causes and their own resolutions, and a feature coming back
on says nothing about any of them.

A resumed job returns to `PENDING` with `next_run_at` now, and its stage moves
`BLOCKED → WAITING` with an `ELIGIBILITY_RESTORED` event. Nothing is skipped and
nothing is terminally consumed: every gate that refused it the first time gets to
refuse it again if it still applies.

The Admin screen reports how many jobs went back into the queue, so an operator
who turns Research on sees the consequence rather than having to go looking for
it.

## Layers, and why there are still three

The Admin Configuration screen and the Agent monitor answer different questions
and both are needed:

* **Admin Configuration** — is this capability in use in this deployment at all?
* **Agent controls (global)** — may this Agent run, and in what state?
* **Campaign overrides and campaign execution** — may this Agent run for *this*
  campaign?

The screens now say which is which and link to each other, rather than the
configuration page describing itself as read-only while a second page quietly
owned the writes. The Agent control architecture is unchanged; the Agent monitor
is administrator-only for the reason recorded in `docs/CAMPAIGN_ACCESS.md`.

## What this did not add

No sending authority of any kind. `AgentIdentifier.SENDING` still has no adapter
and is still `implemented=False` in the registry; `sending` is not a control on
this screen and cannot be created as one; no scheduling was added; the Gmail
separation is unchanged, and the Gmail control governs *draft* creation only.

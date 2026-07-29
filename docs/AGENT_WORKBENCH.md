# Workbench Agent monitor and controls (MVP-01B)

The Workbench is the operator control room for the Phase 2 Agent pipeline. It
answers, on one screen: what is every Agent doing, which Campaigns are
progressing, which Campaign Contacts are blocked, what needs a decision, and
what can safely be controlled.

It is a **read-and-command surface**. It owns no execution vocabulary and no
state. Phase 2 owns the Agent registry, the execution and job states, the
controls, the Campaign overrides, the pipeline stages, the retry lifecycle and
the event vocabulary; the Email Agent owns discovery policy and candidate
sequencing; MVP-01E owns everything about verification. The Workbench projects
those, and routes operator intent back through their services.

Issues: **#221** (MVP-01B) under **#202** (MVP-01), against the shared execution
contract in **#223** and the Verification Agent contract in **#225**.

## Running it

```bash
FEATURES__WORKBENCH=true FEATURES__AGENT_WORKBENCH=true \
  uvicorn app.main:app --reload --port 8000
# open http://127.0.0.1:8000/workbench
```

Both switches default **off**. `FEATURES__WORKBENCH` mounts the UI at all (and is
hard-locked to `APP_ENV=local`); `FEATURES__AGENT_WORKBENCH` unlocks this area.
While the second is off, `/workbench` renders the same clean "isn't available
yet" state every unbuilt area uses, and the navigation entry is disabled.

There is no execution-source setting and no transport to register: production has
exactly one backend and it is the real Phase 2 one.

## Architecture

```text
app/services/workbench_agents/
  views.py      frozen presentation DTOs — every value a Phase 2 enum Phase 2 decided
  reader.py     the read model: one narrow port, one Phase 2 implementation
  commands.py   the command path: every action calls a Phase 2 service
  sanitize.py   credential redaction for anything rendered
app/web/routes.py            thin page adapters
app/web/templates/agent_*    the pages
```

Nothing here writes a row, and no route or template may. Counts come from the
tables Phase 2 writes. Control precedence comes from
`agents.controls.effective_control` rather than being recomputed. The pipeline
order comes from `agents.registry`. Public job labels come from
`agents.jobs.public_status_for`, so a status or an Agent added to Phase 2 appears
in the Workbench without anyone editing it.

The single Phase 2 edit this build makes is two public helpers in
`app/services/agents/jobs.py` (`public_status_for`, `stored_statuses_for_public`)
that expose the module's existing private public-status map. Aggregate queries
and filter chips need the same mapping the serializers use; copying it into the
Workbench would have created a second job vocabulary.

## What the pages show

**Agent overview** — every registered Agent with its effective control and where
that control came from, queue counts by public status, terminal failures split
from retryable ones, the Campaigns overriding it, and the latest committed
pipeline event.

**Campaign execution** — enrolled Contacts, distribution across pipeline stages,
blocked and suppressed counts, active jobs, retry backlog, failed jobs, the
Campaign's Agent overrides, its Sending state, and the latest pipeline events.

**Contact execution** — the permanent Contact identity, membership state, desired
/ current / completed stages, the ordered append-only pipeline history, the
Agent Jobs with attempts and leases, the committed domain outcomes, retry or
terminal status, the blocking reason, suppression status, the Email discovery
outcome and candidate ledger, and the Verification outcome (below).

**Job inspection** — filter by Agent and by public status; drill into the durable
identity, lease, attempts, structured input, committed result, sanitized failure
and retry eligibility.

The vocabulary distinctions an operator has to read at a glance each get their
own treatment and are never merged: queued, leased, running, retry scheduled,
paused, blocked, refused, completed with a usable outcome, completed without one,
terminal failure, cancelled, disabled globally, disabled for this Campaign.

## Three rules the projection enforces

**A stage is complete only when a pipeline event committed it.** A succeeded job
is not a completed stage — the committed domain outcome decides where the Contact
goes next — so the Contact view marks whether the outcome was committed and never
infers one from the other.

**A refusal is a displayable answer, not an error.** Enabling an Agent with no
adapter, retrying a terminal failure, retrying a suppressed Contact, skipping a
critical stage: Phase 2 refuses all of these, and the page shows the service's own
reason rather than a silent no-op.

**A stale screen cannot overwrite a newer decision.** Every control form carries
the control version it rendered with. If the stored version has moved on, the
command is refused with an explanation and the newer decision survives.

## Email Agent projection

The Contact page reads the latest Email Agent job's persisted
`email_discovery` state and the durable `email_candidate_attempts` ledger. It
shows the versioned policy and employee-count evidence class, the locked
candidate order, the current and accepted positions, every child Verification
job and exact evidence reference, forced-refresh scope, and the terminal Email
outcome.

`verified_email_accepted` and `existing_accepted_email_reused` read as accepted
only when an Email stage event committed the outcome and the persisted state
names the accepted address. A Contact having an email does not create that
meaning, and a Verification child job succeeding does not create it either.

`no_verified_address` is shown as the Email Agent's truthful terminal result
after all allowed candidates are exhausted. It is not displayed as a provider
failure and it is not collapsed into a generic completed job.

The Email and Verification sections remain separate deliberately: an Email
candidate attempt is the policy sequencer's parent/child ledger, while a
Verification attempt records provider-facing work and paid-call provenance.

## Verification (MVP-01E projection)

The Contact page carries a Verification section built from the outcome MVP-01E
committed — the stage's `output_reference`, or the `detail` on the pipeline event
that recorded the transition. It shows the decision, the reason and reason code,
the normalized status and provider result, whether an event committed the
outcome, live versus simulated provenance, the evidence reference, the policy
version, and the per-attempt provider history from `verification_attempts`.

| Decision | Shown as | What it means for the operator |
| --- | --- | --- |
| `accept` | accepted · verified · pipeline-ready | Fresh live evidence. The Contact may advance. |
| `try_next_candidate` | try next candidate | A real verdict, not a usable address. Retrying spends credit for nothing. |
| `retry_later` | retry later | No answer yet; another attempt is permitted. Phase 2 owns when. |
| `stop_no_result` | stop — no result | No answer and no further attempt will help. |
| `refused` | refused | Declined before provider work. No credit spent. |

**An address reads as verified and pipeline-ready only when all three hold:** the
committed decision is `accept`, a pipeline event committed it, and the evidence
was not simulated. A succeeded queue job never contributes to that judgement.

`paid_calls` is counted from `VerificationAttempt.provider_called`, not from the
queue, because that is the only honest basis for a paid-call figure.

### One integration gap, reported not papered over

The Verification adapter's pre-provider blocks — missing candidate, malformed
address, suppression, policy mismatch, live not authorised, simulator provider —
raise before the verification domain is consulted, so they carry no decision
payload. Rather than infer `refused` from a reason code (which would be the
Workbench classifying a verification outcome), the view exposes
`refused_before_provider`, derived only from three observable facts: the stage is
held, nothing was committed, and no attempt reached a provider. The page names
that state explicitly. If the Verification thread later routes those blocks
through `decisions.refusal()`, this projection reads the committed decision and
the derived flag becomes redundant.

## The control hierarchy

Global settings define the default for each Agent; a Campaign inherits or
overrides it. `ControlView.source` carries Phase 2's own precedence word —
`registry_default`, `global`, `campaign_override`, `campaign_execution`,
`registry` — so an operator can always tell whether they are looking at a global
decision, a Campaign-scoped one, or a registry fact they cannot override.
Disabling an Agent in one Campaign changes nothing in any other.

Every control declares what it does to work already in flight:

| Control | In-flight policy |
| --- | --- |
| Enable | Work this control had paused is released; a domain-blocked job keeps its own reason. |
| Pause | Claimable and leased work is held at `paused`; nothing is discarded. |
| Disable | No new work is claimed and in-flight work is held at `paused`. |
| Retry job | The same durable job is requeued under its existing identity. |
| Sending stop | The global Sending control set to `disabled`, immediately and everywhere. |

## Safety

* **Sending is disabled by default** and its stop is the authoritative global
  control — not a second parallel flag — so it is visible in the same place as
  every other Sending state, including to the worker.
* **Resuming Sending is a request, not a switch.** It goes through the ordinary
  control path so Phase 2's checks apply unchanged; while no Sending adapter is
  registered Phase 2 refuses, and the page shows that refusal rather than hiding
  the control. Both the stop and the resume require a typed confirmation.
* **Retry cannot bypass anything.** Phase 2 decides eligibility; the page only
  explains the decision in advance. A retry cannot bypass a suppression, a
  disabled Agent, or a Campaign override — the worker re-checks all of them.
* **Nothing is discarded.** Single-job pause is deliberately not offered: Phase 2
  can pause one job but has no single-job resume, so pausing goes through
  `campaign_contacts.pause_membership` / `resume_membership`, which own the
  reversible pair, and cancelling through `pipeline.skip_current_stage`, which
  owns the cancel semantics and the event that explains it.
* **Failure text and payloads are sanitized** before rendering: credential-shaped
  substrings are redacted and sensitive keys are never emitted.
* **Every command is audited**, accepted or refused, with the operator's reason.

## No schema change

The Workbench adds no table, column, index or migration. Relative to the Email
Agent base, `alembic check` reports no un-generated changes and the Alembic head
remains `d2f6c8a104be`. The OpenAPI document is byte-identical to the Email Agent
baseline — no Workbench route was added, changed or shadowed.

## Known limitations

* Attempt history for a job is the append-only pipeline event stream plus the
  Verification attempt rows; Phase 2 keeps no per-attempt row on the job itself,
  so the job page links to the Contact's history rather than duplicating it.
* Job and Campaign Contact lists page at 50 rows, matching the rest of the
  workbench. There is no server-side search over jobs.
* The activity stream is scoped by Agent and Campaign only.

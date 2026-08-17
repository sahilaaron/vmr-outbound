# Company Intelligence backfill: operating guide (CI-001)

## What it is

A durable, bounded, resumable pass over Companies that already have committed
Research, enqueueing one Company Intelligence job for each eligible one.

It is not a script, and the difference matters. Every ad-hoc backfill script this
project could have written fails the same way: it runs half way, somebody stops
it, and afterwards nobody can say which companies it reached, which it skipped,
or why. This one records a per-company outcome with a reason code, holds a
cursor, and can be paused and resumed.

## Before you start

1. **The control must be effectively on for a live run.** That means Company
   Intelligence shows as *effective* in Admin → Configuration — either the
   deployment default `FEATURES__COMPANY_INTELLIGENCE=true`, or an
   administrator's stored setting turning it on, which overrides the default.
   Since 2026-08-16 every enforcement point reads that effective value rather
   than the environment variable, so the Admin screen is the authority. A live
   run with the control off queues nothing and records `feature_disabled`
   against every company — deliberately, because a queue that silently fills
   with work no worker will execute is worse than a run that stops and says so.
2. **Vocabularies must be seeded**, or every value will come back unmapped:
   ```python
   from app.services.company_intelligence.seed import seed_vocabularies

   seed_vocabularies(session)
   session.commit()
   ```
3. **A worker must be running** for anything queued to be executed:
   ```
   python scripts/run_company_intelligence_worker.py --max-jobs 25
   ```
   Each job is one model call. `--max-jobs` is the bound on what one invocation
   will spend; `--once` claims at most one job and exits.

## The recommended sequence

**Step 1 — dry run, small ceiling.**

`/admin/company-intelligence/backfill` → mode *Dry run*, batch size 25,
ceiling 50. Press **Advance one batch**, twice.

Nothing is queued. The per-company table shows, for every company walked, either
`would queue` with the dossier version and fact count, or `skipped` with a reason.
The dry run walks the *identical* eligibility code path as a live run, so this
report is produced by the code that will actually run — not by a second
implementation of it that can drift.

**Step 2 — read the skip reasons.**

| Reason | Means | Usually |
| --- | --- | --- |
| `no_current_dossier` | The Company has no current `CompanyDossierVersion` | Research has not run for it |
| `no_sourced_facts` | It has a dossier, but no sourced facts and no populated sections | Research ran and found nothing usable |
| `already_current_for_input` | A version already covers this exact evidence under this producer | Correct: nothing to do |
| `job_in_flight` | A job for this Company is already queued or running | Correct: it is already being handled |
| `feature_disabled` | Live run with the switch off | Turn the switch on |

If the count of `no_current_dossier` is most of the estate, the answer is more
Research, not more backfill.

**Step 3 — live run, same small ceiling.**

Mode *Live*, batch size 25, ceiling 50. Advance twice. Confirm the queue count on
`/admin/company-intelligence` rises by what you expected.

**Step 4 — run the worker, bounded.**

```
python scripts/run_company_intelligence_worker.py --max-jobs 50
```

Watch the per-job lines. Then spot-check three or four companies on
`/admin/companies/{id}/intelligence`: does the industry look right, is the
evidence the evidence you would have cited, are the unmapped values ones the
vocabulary genuinely should learn?

**Step 5 — only then, the rest.**

Open a new live run with no ceiling and advance it a batch at a time. There is no
"run everything" button on purpose.

## Guarantees

**Deterministic ordering.** `(companies.created_at, companies.id)` ascending. Not
"whatever the planner returns" — a run whose order can change cannot have a
meaningful cursor.

**Bounded batches.** One `advance` call considers at most `batch_size` companies
and returns. There is no unbounded synchronous loop anywhere in the module, so no
web request can turn into a thousand-company walk.

**Resumable.** The cursor is the last Company id processed. Restarting continues
after it.

**Idempotent, twice over.** One item row per `(run, company)`, enforced by a
unique constraint, so re-walking a company a run already recorded is a no-op even
if the cursor is lost. And even if a job were enqueued twice, the queue permits
one active job per Company and production permits one version per input digest.

**Truthful skips.** Every skipped item carries a reason code, and the reason codes
are the same ones the runner and the Admin detail page use. A backfill that
reported a company as handled when it was skipped would be worse than one that
failed loudly.

**No duplicate version for identical input and producer.** `UNIQUE (company_id,
input_digest)`, checked before the model call as well as at the database.

**No production inside the backfill.** It enqueues. A hundred model calls inside
one web request is not a backfill, it is an outage.

## Pausing, resuming, cancelling

* **Pause** stops a running run without losing its cursor.
* **Resume** continues from that cursor.
* **Cancel** ends the plan. Jobs it already queued are **left alone** — they are
  idempotent, bounded and individually cancellable, and silently killing them
  would make "cancel" mean two different things. To stop queued work as well,
  cancel the jobs.

## Interpreting the results

On `/admin/company-intelligence`:

* **Classified** — has a current version.
* **Has unresolved** — the current version contains values that are unmapped,
  unevidenced or explicitly unknown. This is the review queue.
* **Has conflicts** — the evidence disagreed with itself. Needs a person.

A large `unmapped_value` population is a vocabulary signal, not a failure: map
the aliases (from the company detail page), then re-run those companies. The
alias changes what the *next* run resolves; it does not retro-fit stored
versions, because versions are immutable.

## After a CI-002 upgrade, every company is eligible again

CI-002 changed two things that are part of the input digest: the producer's
policy version (1 → 2) and the set of active vocabularies (geography now has
one). So a company whose research has not moved will nonetheless produce a **new
version** on its next run, with geography relationships and specialty hygiene
applied.

That is correct and intended — the old version genuinely was produced under
different rules — but it means the first backfill after this upgrade is a full
one, not an incremental one. Plan it the same way as the first ever run: dry
run, small ceiling, read the skip reasons, spot-check a handful, then widen.

Operator decisions survive it. Decisions are company-scoped, so a confirmation
made against version 1 still applies to version 2 (CI-001 review semantics), and
a decision concerning a value the new version no longer proposes is reported as
`operator only` rather than discarded.

## Costs

One model call per company, per distinct input. Re-running a backfill over
companies whose research has not changed costs nothing — the digest check short-
circuits before the model call. Re-running after Research has moved on costs one
call per changed company, which is the intended behaviour.

## Recovery

A worker that dies mid-job leaves a leased job whose lease expires. The next
claim pass recovers it: back to `PENDING` with a durable `lease_expired` marker,
or `FAILED` if its attempts are spent. No separate scheduler, no manual cleanup.

A job that fails retryably (model timeout, malformed answer) backs off
exponentially — 60s, 120s, 240s, capped at 900s — up to `max_attempts` (3). A job
that fails for a reason retrying cannot fix (no dossier, no facts, feature off)
is terminal immediately, because three attempts at an impossible thing only
delays telling the operator why.

## Programmatic use

```python
from app.services.company_intelligence import backfill

run = backfill.create_run(
    session,
    label="First 50",
    dry_run=True,
    batch_size=25,
    max_companies=50,
    created_by="sahil",
)
while True:
    report = backfill.advance(session, run=run, feature_enabled=True)
    session.commit()
    print(report.considered, report.enqueued, report.skipped, report.skip_reasons)
    if report.exhausted:
        break
```

`advance` never loops internally. The loop is the caller's, so the caller can
stop.

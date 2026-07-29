# Research workers

The Research Agent does not know how to research anything. It knows how
to run **workers**, store what they return, and be honest about what they
could not find. A worker is one source of sourced company facts.

This split exists so a research source can be added, swapped or removed
without touching the pipeline, the job queue, or the evidence model.

## The contract

`app/services/research/contracts.py`. A worker is any object with:

```python
name: str
version: str

def run(self, request: ResearchRequest) -> WorkerResult:
    ...
```

It receives a `ResearchRequest` (domain, company name, timeout, and its
own slice of operator configuration) and returns a `WorkerResult`:

| Field | Meaning |
| --- | --- |
| `facts` | `SourcedFact` values — the claims, each with its source URL |
| `warnings` | anything the operator should know; survives onto the dossier |
| `raw` | the verbatim payload, preserved so a later policy change can be re-derived without re-crawling anyone |
| `sufficient` | `False` means "read the source, it says little" — a truthful outcome, not a failure |

A worker raises `ResearchWorkerError` for expected failures, with
`retryable` saying whether trying again could plausibly help. It never
raises an Agent adapter exception; the Agent owns that vocabulary.

### The rules a worker must not break

* **Every fact carries its source.** `SourcedFact` refuses a relative
  URL, a naive timestamp, a blank value, or a confidence outside `[0, 1]`.
  A value you cannot evidence must not be returned at all.
* **Facts, not conclusions.** Workers report what a source said.
  Interpreting it is MVP-02, behind the untrusted-evidence boundary
  (#181).
* **Nothing that violates a platform's terms.** No unattended scraping,
  no CAPTCHA or anti-bot evasion, no access-control bypass. The bundled
  website worker fetches and obeys `robots.txt` unconditionally.

## Registering one

`app/services/research/workers/registry.py`:

```python
register_worker("my_source", MySourceWorker)
```

Which workers actually run is operator configuration, not code:

```python
controls.set_global_control(
    session,
    agent_id=AgentIdentifier.RESEARCH,
    status=AgentControlStatus.ENABLED,
    config={
        "live": True,
        "workers": ["website", "my_source"],
        "worker_options": {"max_pages": 10},
    },
)
```

Workers run in the order listed. Naming a worker this build does not have
is an error, not a silent skip — a research run that quietly did less
than was asked is not an acceptable outcome.

## What ships today

**`website`** — the deterministic company-website collector, vendored
from the standalone `company-website-researcher` prototype into
`workers/_website/`. No model call: every fact is an explicit statement,
a structured-data value (JSON-LD / Open Graph), or a clearly-labelled
heuristic signal. Bounded to 25 pages, depth 3, with a one-second
politeness delay and a hard floor under all three — an operator can
tighten the crawl, never widen it.

The vendored directory is excluded from `ruff` and from strict `mypy`
because it is upstream code. Fix a defect upstream and re-vendor rather
than editing it in place. `collect.py` is the one file in there written
for this repository, and it is the in-memory replacement for the
prototype's filesystem pipeline.

## Planned: script workers (v2, not built)

Sahil's `find_domain.py` and `company_intel.py` already produce exactly
the shape this contract expects — JSON with a real `source_url` per item.
A `ScriptWorker` would subprocess a registered script and map its output
onto `SourcedFact`:

| Script output | `SourcedFact` |
| --- | --- |
| `headline` / `title` | `value` |
| `source_url` | `source_url` |
| `date` | `published_at` |
| process start time | `retrieved_at` |
| script name + version | `extraction_method` |

Registration would be declarative — a script path, an argument template
and a field mapping — so a new script becomes a config entry rather than
a module.

Three things must be settled before that is built, and none of them are
technical conveniences:

1. **Both scripts shell out to the Claude CLI.** That is an LLM
   invocation inside a stage whose entire contract is "deterministic,
   sourced facts, no inference". Model output must be treated as an
   untrusted, non-deterministic *interpretation*, not as a fact — which
   makes it MVP-02 work sitting behind #181, not a research worker.
2. **`docs/GOAL.md` lists paid LLM API integration as out of scope.**
   Whether a CLI on Sahil's subscription counts is a scope decision, not
   an implementation detail.
3. **A subprocess boundary is an execution boundary.** Timeouts, output
   size limits, and refusing to inherit the application environment all
   need to be settled before arbitrary scripts run inside a worker.

Tracked in `docs/POST_LAUNCH_BACKLOG.md`. Do not implement it as part of
RES-001.

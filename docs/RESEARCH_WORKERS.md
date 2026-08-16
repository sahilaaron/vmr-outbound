# Research workers

The Research Agent stores sourced facts, a raw submission and a versioned
dossier through one persistence chain. Production uses the bounded Claude CLI
web-research source described below. The registered deterministic worker system
remains available for tests, diagnostics and future explicitly approved
alternate modes; it is not part of normal production execution.

This split exists so a research source can be added, swapped or removed
without touching the pipeline, the job queue, or the evidence model.

## The contract

`app/services/research/contracts.py`. A worker is any object with:

```python
name: str
version: str


def run(self, request: ResearchRequest) -> WorkerResult: ...
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

Legacy diagnostic/experimental callers can select registered workers:

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

This configuration does not select the production Research source. The
production adapter ignores `workers` and `worker_options`, invokes Claude web
research once, and never constructs the deterministic registry. Naming a worker
this build does not have remains an error in the explicit legacy mode.

## What ships today

**`website`** — the deterministic company-website collector, vendored
from the standalone `company-website-researcher` prototype into
`workers/_website/`. No model call: every fact is an explicit statement,
a structured-data value (JSON-LD / Open Graph), or a clearly-labelled
heuristic signal. Bounded to 25 pages, depth 3, with a one-second
politeness delay and a hard floor under all three — an operator can
tighten the crawl, never widen it.

It does not run on the normal production Research path. Retaining it is not a
silent downgrade policy: if required Claude Research is unavailable or fails,
the Agent reports BLOCKED, RETRY or TERMINAL as appropriate.

The vendored directory is excluded from `ruff` and from strict `mypy`
because it is upstream code. Fix a defect upstream and re-vendor rather
than editing it in place. `collect.py` is the one file in there written
for this repository, and it is the in-memory replacement for the
prototype's filesystem pipeline.

### Vendored revision

The prototype is a plain directory with no version control, so a
re-vendor is recorded here by date and content hash rather than by
commit.

| Re-vendored | Upstream file | SHA-256 (first 16) | How it landed here |
| --- | --- | --- | --- |
| 2026-07-31 | `company_research/sitemap.py` | `8caf3b216dac0b3b` | copied whole |
| 2026-07-31 | `company_research/fetcher.py` | `4d0dfcea54c7f9ec` | corrections ported by hand |
| 2026-07-31 | `company_research/models.py` | `02b1270a00761789` | corrections ported by hand |

`sitemap.py` was byte-identical to upstream, so it was copied whole.
`fetcher.py` and `models.py` were not: this repository's copies carry
hardening the prototype has no equivalent of — an SSRF boundary,
manually validated redirects that refuse to leave the requested host, an
injectable resolver and transport, and a `FetchResult` without the
prototype's job-store types. Copying those two files over would have
removed all of it, so only the corrected regions were ported. Check that
divergence before the next re-vendor; it is deliberate.

`robots.py` also diverges deliberately: this copy is fail-closed, where
the prototype allows a crawl when robots.txt cannot be read.

## Primary Claude CLI web research

`app/services/research/fallback.py`. The file and several internal type names
retain RES-002's legacy terminology, but the runtime role is no longer a
fallback. It is **not a registered worker** and must never appear in
`config["workers"]`. Every production execution that clears Company Research,
Campaign live, Company/domain existence and domain authorization invokes it as
the required primary source. The deterministic worker is neither attempted nor
consulted first.

### What bounds it

* `FEATURES__RESEARCH_CLAUDE_FALLBACK`, default off, remains as a backward-
  compatible availability control on top of `FEATURES__COMPANY_RESEARCH`.
  Its name is legacy. Off means Research is unavailable; it does not restore
  deterministic production Research. A Campaign's legacy
  `{"claude_fallback": false}` opt-out has the same blocking semantics.
* `allowed_tools=("WebSearch", "WebFetch")` — the narrowest permission
  set that still allows finding pages and reading them. Deliberately not
  the `allowed_tools=()` Insights and Personalization run under, because
  this call *is* the gathering; equally deliberately not wider. No shell,
  no filesystem, no editing. It cannot reach this application at all.
* One call, one timeout, a ceiling on accepted sources and on accepted
  evidence items. The CLI's internal tool loop is not observable from
  this process, so the source ceiling is stated to the model as its
  budget and enforced on the way back in — on what may be *persisted*,
  not on what was requested. That is the honest boundary.

### What it may store

The same `SourcedFact` values a deterministic worker returns, through the
same validation. A claim is accepted only with an openable absolute
`source_url` **and** the supporting text from that page; anything else is
dropped and counted, never softened into a weaker fact. Field names come
from a closed vocabulary (`fallback.RESEARCH_FIELDS`), a strict subset of
the Agent's field-to-section map, so the model cannot invent a section.
Model-supplied confidence is clamped, and `retrieved_at` is this
process's wall clock rather than anything the answer claimed.

Everything it stores stays labelled: worker name `claude_web`, extraction
method `claude_cli_web_research:model_cited`, durable research mode
`claude_primary`, and dossier basis `claude_cli_web_research`. Later Insights
interpretation remains distinguishable from cited Research evidence. Neither
stage writes a canonical Company field.

### Retries

Safe to retry, and idempotent for the same job. Evidence rows are keyed
`research:{job_id}:{worker}:{index}`, so re-running writes the same rows.
A job that already committed its Claude attempt rebuilds it from the
stored raw payload rather than spending a second model call, which also
makes the resubmitted payload hash to the submission that already exists;
an identical reading of an identical submission then reuses the dossier
version instead of writing a second one.

A transient Claude CLI or web failure keeps the existing retryable semantics.
A completed Claude execution that could cite nothing does not retry: that
is a truthful finding about the company's public web presence, it is
committed with warnings, and it does not retry forever.

Web pages and search results reaching this seam are untrusted evidence.
The JSON shape is enforced by `fallback.py`, not negotiated with the
answer: an unexpected key is ignored and an unknown field name is
rejected, so nothing a page asserts can widen what is stored or what this
stage may do.

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

1. **Both scripts shell out to the Claude CLI.** Production Research already
   has one reviewed bounded Claude boundary. A registered `ScriptWorker`
   running arbitrary model-backed scripts would create a second execution and
   validation route whose permissions, evidence rules and provenance could
   diverge. That wider question remains open.
2. **`docs/GOAL.md` lists paid LLM API integration as out of scope.**
   The approved primary Research CLI uses Sahil's existing subscription;
   arbitrary additional CLI integrations remain a separate scope decision.
3. **A subprocess boundary is an execution boundary.** Timeouts, output
   size limits, and refusing to inherit the application environment all
   need to be settled before arbitrary scripts run inside a worker.

Tracked in `docs/POST_LAUNCH_BACKLOG.md`. Do not implement it as part of
RES-001.

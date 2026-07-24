# VER-007 — MillionVerifier live smoke acceptance (sanitized)

Issue #37. This is a sanitized acceptance record. It contains **no secrets, no API
key, no unredacted provider URL, and no raw provider response**.

## Branch & build

* Branch: `ver-007-millionverifier-live-smoke`
* Base: `main` @ `48798b9` (`filename change`, after #138 usage-ledger merge)
* Committer: `Sahil Aaron <sahilaaron19o@gmail.com>` (repo-configured; no AI/tool
  attribution). Not pushed from this environment (read-only GitHub); delivered as a
  git bundle for the standard bridge step.

## What this delivers

1. A deliberate, safe operator command for the one live request:
   `scripts/verify_live_smoke.py` → `app/services/verification/live_smoke.py`.
2. Simulated-vs-live evidence provenance so a simulated success can never be
   displayed as an external MillionVerifier verification.
3. Strengthened offline contract/secret tests around live selection and key safety.
4. Runbook and Phase 2 doc updates.

No schema change (no `models/` or `migrations/` edits); `alembic check` reports no
drift.

## Exact command shape (email redacted; run by the operator)

```bash
FEATURES__MILLIONVERIFIER=true \
  python scripts/verify_live_smoke.py --email <an-address-you-control> --confirm
```

The key is supplied only via the local, git-ignored `.env` variable
`MILLIONVERIFIER_API_KEY` and is never passed on the command line.

## Safety gates (all covered by tests)

The command refuses, making **no** provider call, when: the feature is disabled; no
key is configured; the key is a documented MillionVerifier test key; `--confirm` is
absent; the address is invalid; or fresh cached evidence already exists for the
address (a cache hit would not prove a live call). It selects the real HTTP client
only for a real non-test key with explicit live mode, and aborts rather than fall
back to the simulator. The key is never printed, logged, stored, placed in an
exception, or written into the result.

## Offline validation (this environment, local Postgres 16, UTF-8, 127.0.0.1:5433)

| Check | Result |
| --- | --- |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 121 files already formatted |
| `python -m mypy app` (strict) | Success: no issues in 68 source files |
| `python -m pytest` | **374 passed** (361 baseline + 13 new) |
| `alembic upgrade head` (clean DB) | OK (head `ad1e298fb49a`) |
| `alembic check` | No new upgrade operations detected |

### Contract / secret coverage (offline, no network)

* documented test keys always route to the simulator; no key → simulator;
* real key + explicit live → HTTP client; real key **without** live → simulator (no
  network);
* request construction and response parsing match the Single API contract;
* malformed responses and transport errors → retryable provider failures;
* API key redacted from URLs, exceptions, stored payloads, and settings repr/dump;
* the live smoke path (via an **injected fake transport**, no real network) stores
  **live-provenance** evidence, records a **cache-miss** ledger entry with the
  provider's credits, maps the outcome truthfully, and the status explanation reads
  as a live result; a transport failure is reported as "provider not reached" and
  never as a verified mailbox.

## Live provider call status

**Not yet executed against the real key.** No approved smoke-test address was
supplied, so per the task no real person's address was invented and no credit was
spent. The automated live-path test uses a fake transport, so **the provider
interaction in this session was simulated (test seam), not a real network call.**
The command is ready; Sahil runs the one live request locally (the only step that
requires the real key and consumes a credit).

## Operator to record after the one live run (fill in; keep sanitized)

```
date/time (local)        :
address (may redact)     :
live HTTP client used    :   (expect: yes)
provider request made    :   (expect: yes)
provider livemode        :
provider result / code   :
canonical mapped result  :
precise internal status  :
billed this call         :
credits remaining        :
evidence stored (source) :   (expect: source=live)
ledger (cache/charge)    :   (expect: cache=miss)
UI status text           :
PASS / FAIL              :
```

A live response does not have to be `valid` to pass — the acceptance is an
authentic, truthfully-mapped provider interaction.

## Secret hygiene confirmation

The API key does not appear in any commit, log, printed output, evidence file, or
database payload in this change. It is read only from the local `.env`, excluded
from `repr(settings)`/`model_dump()`, redacted in any diagnostic URL/exception, and
stripped from stored provider payloads.

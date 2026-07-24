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

### Review-round hardening (PR #149)

* The live HTTP client sends explicit `Accept: application/json` and
  `User-Agent: vmr-outbound/0.0.1` headers, built by a pure `build_request` seam
  that is unit-tested offline.
* HTTP **401/403** is classified as a provider **access rejection** (mapped to a
  non-retryable provider error, never a mailbox verdict); other HTTP statuses stay
  retryable transport failures. Key redaction is preserved on every path.
* The operator command runs a database preflight and fails with a clean one-line
  hint instead of a raw stack trace; it never prints the connection URL.

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
| `python -m pytest` | **378 passed** (361 baseline + 17 new) |
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

## Live provider call status — EXECUTED, PASS

The one deliberate live request was run by Sahil against the real key on a
VMR-controlled address. It was an authentic MillionVerifier request (not the
simulator/test seam): the live HTTP client was selected, `livemode` was reported
true, the outcome mapped truthfully, evidence was stored with **live** provenance,
and the usage ledger recorded a real cache-miss, confirmed charge.

### Sanitized run record

```
date/time (local)        : 2026-07-24 17:30:18 +05:30
address                  : sahil@verifiedmarketresearch.com (VMR-controlled)
live HTTP client used    : yes
provider request made    : yes
transport ok             : yes
provider livemode        : yes
provider result / code   : ok / 1
canonical mapped result  : valid
precise internal status  : valid
role / free / suggestion : no / no / —
subresult                : ok
billed this call         : yes
credits remaining        : 487
evidence stored (source) : yes (source=live)
evidence id              : daf755e7-8102-49d2-b050-ae27b9c31e14
ledger (cache/charge)    : cache=miss, charge=confirmed
UI status                : successful — "Fresh result: the mailbox exists.
                           Live MillionVerifier evidence — produced by an external
                           provider request."
result                   : PASS
```

A live response does not have to be `valid` to pass — the acceptance is an
authentic, truthfully-mapped provider interaction. This run returned a valid mailbox
and confirms credentials, live-mode selection, mapping, storage, ledger, and truthful
display end to end. No key or raw provider response is recorded here.

## Secret hygiene confirmation

The API key does not appear in any commit, log, printed output, evidence file, or
database payload in this change. It is read only from the local `.env`, excluded
from `repr(settings)`/`model_dump()`, redacted in any diagnostic URL/exception, and
stripped from stored provider payloads.

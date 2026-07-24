# Email verification runbook (Phase 2, operator)

Local, single-operator procedures for the email-intelligence + MillionVerifier
verification path. Nothing here schedules or sends email; dry-run stays on.

## Enable locally

Both switches default off. Enable them together with the workbench:

```bash
FEATURES__WORKBENCH=true \
FEATURES__EMAIL_GENERATION=true \
FEATURES__MILLIONVERIFIER=true \
  uvicorn app.main:app --reload --port 8000
```

On Windows, set these in `.env` instead of inline.

With the switches on, the left rail shows **Email Verification**, each contact's
email carries a four-state status icon, and the contact detail page shows the
candidate set, exact-address evidence, and verification jobs.

## API key configuration (the exact step)

The provider key is a secret and is read only from the environment. To use the
real MillionVerifier Single API, set it locally (never commit it):

```bash
# add to your local .env (which is git-ignored) — do NOT paste the key anywhere else
MILLIONVERIFIER_API_KEY=your-real-key-here
```

Then restart the app. Until a real key is set, verification runs a deterministic,
network-free **simulator** and the whole pipeline still works; the documented
test keys (`API_KEY_FOR_OK`, `API_KEY_FOR_CATCH_ALL`, `API_KEY_FOR_ERROR_*`, …)
are also recognised and routed to the simulator, never the network.

The key never appears in logs, stored payloads, screenshots, or the diagnostic
request URL (it is redacted). `settings.model_dump()` and `repr(settings)`
exclude it.

## Deterministic demo (synthetic data)

```bash
FEATURES__EMAIL_GENERATION=true FEATURES__MILLIONVERIFIER=true \
  python scripts/phase2_verification_demo.py
```

Rebuilds a "Phase 2 Verification Demo" campaign of synthetic, fictional contacts
that exercise every outcome (valid, invalid, catch-all, unknown, disposable,
role-based, provider error/retry, insufficient credits, imported vs generated,
compound/diacritic names, and an unrenderable name routed to review) plus cache
reuse. Open `/contacts` and `/verification` to inspect. Safe to re-run.

## Day-to-day operator actions

* **Verify one contact** — contact detail → *Verify selected address*. Enqueues
  the selected candidate and processes it; reuses fresh cached evidence instead
  of paying when possible.
* **Regenerate candidates** — contact detail → *Regenerate candidates* (idempotent).
* **Run pending jobs** — `/verification` → *Run pending jobs* drains the queue.
* **Recover interrupted jobs** — `/verification` → *Recover interrupted jobs*
  resets jobs whose worker lease expired back to pending.

## Reading the four states

* **Pending** (clock) — queued, checking, retry scheduled, or stale awaiting recheck.
* **Successful** (green check) — a *fresh* exact result that the active policy maps
  to canonical valid, and is not role-based.
* **Failure** (red cross) — canonical invalid / definitive blocking.
* **Warning** (amber triangle) — catch-all, unknown, disposable, role-based,
  provider error, insufficient credits, stale evidence, or conflicting evidence.
  These are never safe to treat as valid.

Hover any icon (or read its `aria-label`) for the precise underlying state and a
plain explanation. The precise state is also shown on the contact detail panel.

## The one manual acceptance item — live smoke test (VER-007)

Everything is proven offline except a single deliberate live request that confirms
real credentials and mapping end to end. There is one operator command for it:
`scripts/verify_live_smoke.py`. It performs **exactly one** real MillionVerifier
request for one address, runs the real mapping/storage/ledger/display path, and
prints a sanitized result. It refuses to run unless you have deliberately opted
into live mode, and it never falls back to a simulated success.

### Prerequisites (exact)

1. A real MillionVerifier key in your **local, git-ignored** `.env`:

   ```
   MILLIONVERIFIER_API_KEY=your-real-key-here
   ```

   Set only the variable name shown above; never commit or paste the value.
2. The verification feature enabled (also in `.env`): `FEATURES__MILLIONVERIFIER=true`.
3. The local database reachable: `python scripts/dev_up.py`.
4. **Restart the app / open a new shell after editing `.env`** — settings are read
   once per process, so a running app will not pick up a newly added key until it
   is restarted.

### Run exactly one live check

Use an address **you control** (yours or a VMR-owned mailbox) that has **no fresh
cached evidence** — the command refuses a cache hit, because a reused result would
not prove a live call. Pick an address you have not verified within its TTL.

```bash
python scripts/verify_live_smoke.py --email you@your-domain.com --confirm
```

`--confirm` is required and is your deliberate consent to spend one credit. Without
a real non-test key, without the feature enabled, or without `--confirm`, the
command refuses and makes no call. The banner and the printed
`live HTTP client used: yes` / `provider request made: yes` confirm the live path
was taken.

### Inspect the stored result and the usage ledger

The command prints everything you need (sanitized): normalized email, provider
`livemode`, provider `result`/`resultcode`, canonical mapped result, precise
internal status, role/free/suggestion, `checked at`, policy version, whether the
call was billed, credits remaining, the stored evidence id and its source, and the
ledger cache/charge status. You can also confirm it in the UI: open the contact/
address in `/contacts` (with the feature on) — the evidence row shows a
**MillionVerifier (live)** badge and the status explains it as a live result. The
`/verification` console's usage & cost card shows the call as a **cache miss** with
the credits it consumed.

A valid live response does **not** have to be `valid` for the smoke test to pass —
the acceptance is that the interaction and mapping are authentic and truthful. A
`catch_all`, `unknown`, `disposable`, `role`, provider-error, insufficient-credits,
or timeout outcome is a legitimate live result and is **never** shown as a verified
mailbox.

## Recognising simulator vs cached-live vs live evidence

The system never lets a simulated result look like an external verification:

* **Simulated result — no external verification performed.** Produced by the
  deterministic simulator (no key, a documented test key, or ordinary local
  verification without live mode). Stored provenance is `millionverifier-simulator`;
  the contact page shows an amber **simulated** badge and the status explanation
  says "Simulated result — no external verification performed."
* **Live provider result — a MillionVerifier request was performed.** Only the live
  smoke command (real non-test key + explicit live mode) produces this. Stored
  provenance is `millionverifier`; the contact page shows a **MillionVerifier (live)**
  badge.
* **Cached live evidence — no new provider request.** A previously stored live
  result reused within its TTL. The status still shows the live badge; the
  `/verification` console counts it under **cache savings** (calls avoided), and the
  contact page shows its original `checked` timestamp rather than a fresh one.

## Failure handling

* **Invalid / not-configured key** — the command refuses before any call if the key
  is missing (`no MillionVerifier API key configured`). If a *wrong* key reaches the
  provider, MillionVerifier returns an authentication error; it maps to
  **provider error** (no verdict, no address evidence), never to a mailbox result.
* **Insufficient credits** — maps to the **insufficient-credits** state (no address
  evidence, no auto-retry). Top up the plan and re-run.
* **Timeout / transport failure** — reported as `transport ok: no`; it is a retryable
  provider failure, never a verdict, and stores no evidence. Re-run when the network
  or provider recovers.
* **Provider errors (IP blocked, internal error)** — map to a retryable **provider
  error**; a definite address result is never fabricated from an error.

In every failure case the API key is redacted from messages and never written to the
database, logs, or the printed result.

## What must never be captured in evidence

Do **not** put the API key, an unredacted provider URL, or a raw provider response
(which can contain personal data) into commits, issues, screenshots, recordings, the
tracker, or the database. Record only the sanitized fields the command prints
(PASS/FAIL, mapped state, result code, credits) — that is exactly what the command
emits.

## Disabling or rotating the key after the test

The key lives only in your local `.env`. To stop live calls, remove or comment out
`MILLIONVERIFIER_API_KEY` (or set `FEATURES__MILLIONVERIFIER=false`) and restart —
verification falls back to the network-free simulator immediately. To rotate,
generate a new key in the MillionVerifier dashboard, revoke the old one there,
replace the value in `.env`, and restart. No key material is stored anywhere else,
so nothing else needs cleaning up.

## Cost visibility

`/verification` shows paid calls (and how many were billed), cache reuse (calls
avoided), each exception class, retries, recoveries, and the latest known credit
balance. Only ok/invalid/disposable are billed; catch-all/unknown are free.

A compact **MillionVerifier usage & cost** card summarises the provider-neutral
usage ledger: calls, cache savings, failures (including any *uncertain* charges
from interrupted jobs), estimated spend, remaining credits, and the projected
cost to finish the active batch. To turn credit counts into money estimates, set
your plan's effective per-email rate:

```
MILLIONVERIFIER_COST_PER_CREDIT=0.001   # example; use your plan's rate
MILLIONVERIFIER_CURRENCY=USD
```

With the rate unset (default 0), the ledger still records credits consumed and the
card shows credits with the money figure left blank — it never fabricates a price.
The same `usage_ledger_entries` table is provider-neutral and will later carry
research, AI-model, enrichment, and Saleshandy usage without a schema change; the
full multi-provider finance dashboard is out of scope for #137.

## Recovery & safety notes

* Duplicate concurrent requests for one address cannot cause duplicate paid calls
  (unique idempotency key + partial unique active-email index).
* Only transient failures retry (bounded backoff + jitter); definite results and
  insufficient-credit conditions never retry automatically.
* A worker that dies mid-job leaves its lease to expire; the job is safely
  reclaimed and a `recovered` event is recorded.

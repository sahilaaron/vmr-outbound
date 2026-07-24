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

## The one manual acceptance item — live smoke test

Everything is proven offline except a single deliberate live request that
confirms real credentials and mapping end to end (VER-007). To run it manually:

1. Put a real key in your local `.env` as above and restart the app.
2. From a Python shell in the project venv, verify one address you control:

   ```python
   from app.core.config import get_settings
   from app.services.verification.service import get_provider

   s = get_settings()
   provider = get_provider(s, live=True)  # live=True + a real (non-test) key -> HTTP client
   print(provider.verify("an-address-you-control@your-domain.com"))
   ```

3. Confirm the returned `result`/`resultcode` match MillionVerifier's dashboard
   for that address and that `credits` decremented as expected.

Do **not** paste the key or the raw response (which may include the address) into
commits, issues, screenshots, or the tracker. Record only PASS/FAIL and the
mapped internal state.

## Cost visibility

`/verification` shows paid calls (and how many were billed), cache reuse (calls
avoided), each exception class, retries, recoveries, and the latest known credit
balance. Only ok/invalid/disposable are billed; catch-all/unknown are free.

## Recovery & safety notes

* Duplicate concurrent requests for one address cannot cause duplicate paid calls
  (unique idempotency key + partial unique active-email index).
* Only transient failures retry (bounded backoff + jitter); definite results and
  insufficient-credit conditions never retry automatically.
* A worker that dies mid-job leaves its lease to expire; the job is safely
  reclaimed and a `recovered` event is recorded.

# Phase 2 — Email Verification: evidence map

> Historical subsystem plan: this document records the earlier email-
> verification Phase 2 deliverable. The current cross-application Campaign,
> Collection, Agent, queue, and pipeline backbone is documented in
> [`PHASE_2_EXECUTION_MODEL.md`](./PHASE_2_EXECUTION_MODEL.md).

This tracks the Phase 2 build: deterministic email-pattern intelligence and
exact-address verification through MillionVerifier, delivered as one cohesive
vertical path behind the `FEATURES__EMAIL_GENERATION` and
`FEATURES__MILLIONVERIFIER` switches (both default **off**). It consolidates
issues #23–#30 and #32–#35 under the coordinating issue #137. It does not touch
scoring, research, drafting, or sending; those remain later phases with their
switches off.

Phase 2 explicitly **excludes** EML-007 (#31, campaign-outcome ranking) and
VER-010 (#132, SMTP probing), which stay post-pilot.

## Safety model (why nothing false ever looks valid)

Three concepts are kept structurally distinct and never conflated:

* **Exact-address verification evidence** — `exact_email_verifications`, about one
  full normalized address. Only this can make an address "valid".
* **Domain-pattern observations** — `domain_pattern_observations`, may *reorder*
  candidates but can never verify a different mailbox.
* **Mail-domain / catch-all observations** — `mail_domain_observations`,
  uncertainty about a domain, never proof a mailbox exists.

The four visible states map from a richer set of precise underlying states
(`EmailPreciseStatus` → `EmailVisualStatus`, one authoritative map in
`app/models/enums.py::PRECISE_TO_VISUAL`):

| Visible | Precise underlying states |
| --- | --- |
| **Pending** (clock) | unverified, queued, checking, retry_scheduled, stale_recheck_scheduled |
| **Successful** (check) | valid — a *fresh* exact result mapped by the active policy to canonical valid, and **not** role-based |
| **Failure** (cross) | invalid — canonical invalid / definitive blocking |
| **Warning** (triangle) | catch_all, unknown, disposable, role_based, provider_error, insufficient_credits, stale_evidence, conflicting_evidence |

A provider error, an insufficient-credit condition, a timeout, an unknown, and a
catch-all are **distinct precise states** that happen to share the amber warning
treatment — none is ever shown as a valid mailbox or a plain invalid mailbox.

## Child-issue → implementation & tests

| Issue | Card | Implementation | Tests |
| --- | --- | --- | --- |
| **#23 EML-001** Normalize names/domains for generation | ASCII folding of diacritics, apostrophes, hyphens, particle/compound surnames, middle names; non-Latin names reported unrenderable (routed to review). Versioned engine (`ENGINE_VERSION`). | `app/services/email/normalization.py` | `tests/test_email_normalization.py` |
| **#24 EML-002** Versioned pattern generation | Bounded, ordered, duplicate-free common patterns; each candidate records its pattern + engine version. | `app/services/email/patterns.py`, `app/models/email_candidate.py` | `tests/test_email_normalization.py`, `tests/test_email_candidates.py` |
| **#25 EML-003** Separate exact/pattern/mail-domain facts | Three distinct tables preserved; candidates table says "address to check", never "valid". | `app/models/email_evidence.py`, `app/models/email_candidate.py` | `tests/test_verification_service.py` (evidence isolation), `tests/test_verification_status.py` |
| **#26 EML-006** Expose internal email intelligence | Contact detail panel: candidates, patterns, exact results, freshness, confidence, selection reasoning; verification console. | `app/services/verification/console.py`, `app/web/templates/contact_detail.html`, `app/web/templates/verification.html` | `tests/test_verification_web.py` |
| **#28 EML-006** (exposure, cont.) | Ranked candidate table + selection reason rendered; usage/exception log surfaced. | as above | `tests/test_verification_web.py` |
| **#30 EML-004** Rank candidates using internal evidence | Fresh, strong domain-pattern evidence reorders candidates (bounded boost) but never marks an address valid. | `app/services/email/candidates.py::rank_candidates` | `tests/test_email_normalization.py::test_fresh_domain_pattern_evidence_reorders_without_validating` |
| **#27 VER-002** Map provider outcomes to internal states | ok/invalid/catch_all/unknown/disposable → address evidence; error/timeout/IP-block → transient; insufficient_credits & config errors → operational, non-evidence. Role-based valid → warning. | `app/services/verification/policy.py` | `tests/test_verification_policy.py` |
| **#29 VER-003** Exact-address caching & freshness | Reuse only for the *same normalized address*; policy-versioned TTLs per result; stale → warning + recheck-eligible. | `app/services/verification/policy.py`, `app/services/verification/service.py::find_fresh_evidence` | `tests/test_verification_policy.py`, `tests/test_verification_service.py::test_cache_reuse_avoids_second_paid_call`, `::test_stale_evidence_triggers_new_call` |
| **#32 VER-001** MillionVerifier adapter | Replaceable `VerificationProvider`; network-free simulator (default + all tests) and an HTTP client behind an injectable transport; key from env, redacted everywhere. | `app/services/verification/provider.py` | `tests/test_verification_provider.py`, `tests/test_verification_secrets.py` |
| **#33 VER-004** Conservative catch-all handling | Catch-all/unknown/disposable never become valid or scheduling-ready; derivation keeps them amber. | `app/services/verification/status.py`, `policy.py` | `tests/test_verification_status.py`, `tests/test_verification_policy.py` |
| **#34 VER-005** Rate limits, retries, idempotency | Postgres-backed queue; unique idempotency key + partial unique active-email index (≤1 active job/address); leased claiming (`FOR UPDATE SKIP LOCKED`); bounded backoff+jitter for transient failures only; interrupted-worker recovery. | `app/models/verification_job.py`, `app/services/verification/queue.py`, `service.py` | `tests/test_verification_queue.py`, `tests/test_verification_service.py::test_duplicate_enqueue_makes_max_one_paid_call` |
| **#35 VER-006** Track usage & exceptions | Every call, cache reuse, provider error, timeout, insufficient-credit, retry, recovery recorded; billed vs free visible; credit balance surfaced. Plus a **provider-neutral usage/cost ledger** (below). | `app/models/verification_usage.py`, `app/services/verification/usage.py`, `console.py`, `app/models/usage_ledger.py`, `app/services/usage_ledger.py` | `tests/test_verification_service.py`, `tests/test_verification_web.py`, `tests/test_usage_ledger.py` |

### Provider-neutral usage/cost ledger

Every MillionVerifier request (and every cache hit that avoided one) writes a row
to `usage_ledger_entries`, capturing: provider, operation, campaign, a *soft*
related-job reference (`job_id` + `job_kind`, no hard FK) and request ref,
attempted time, result, cache status (hit/miss/n-a), retry number, units
consumed, estimated cost, provider-reported cost (when a provider returns one —
null for MillionVerifier), currency, and whether the charge is `confirmed`,
`uncertain`, or `none`. An interrupted (reclaimed) job records an **uncertain**
charge so a possibly-completed paid call is never lost.

The compact MillionVerifier usage summary on `/verification` shows calls, cache
savings, failures, estimated spend, remaining credits (when obtainable), and the
projected cost to finish the active batch (runnable jobs × per-credit rate, an
upper bound). Estimated cost uses `MILLIONVERIFIER_COST_PER_CREDIT`; when unset
the UI shows credits consumed and leaves the money figure blank rather than
fabricating one.

The ledger is **deliberately provider-neutral** — `provider`/`operation` are free
strings and the job link is a soft reference — so future metered services
(research APIs, AI models, enrichment providers, Saleshandy) record usage in the
*same table* with no schema replacement (`tests/test_usage_ledger.py::test_second_provider_writes_same_table`).
The complete multi-provider finance dashboard is intentionally **out of scope**
for #137; this is the shared ledger primitive it will later read from.

VER-007 (#37 provider contract tests + live smoke) is code-complete: the documented
outcomes are covered offline by contract tests (`tests/test_verification_provider.py`,
`tests/test_verification_secrets.py`), and a deliberate, safe operator command —
`scripts/verify_live_smoke.py`, backed by `app/services/verification/live_smoke.py`
and `tests/test_verification_live_smoke.py` — performs exactly one live request
through the real mapping/storage/ledger/display path. Stored evidence now records
simulated-vs-live provenance so a simulated success is never shown as an external
verification. The single live request against a real key has now been executed
(PASS) — see `docs/phase2_evidence/VER-007_live_smoke_acceptance.md` for the
sanitized record.

## MillionVerifier contract used

Single API v3: `GET https://api.millionverifier.com/api/v3?api=<key>&email=<addr>&timeout=<2..60>`.
Result codes: `1=ok, 2=catch_all, 3=unknown, 4=error, 5=disposable, 6=invalid`.
Response fields consumed: `result, resultcode, subresult, quality, free, role,
didyoumean, credits, executiontime, error, livemode`. Only ok/invalid/disposable
are billed; catch_all/unknown are free. Documented test keys
(`API_KEY_FOR_OK`, `API_KEY_FOR_CATCH_ALL`, `API_KEY_FOR_ERROR_*`, …) are
recognised and route to the simulator, never the network.

## Verification behaviour (exact)

1. **Feature gate.** Candidate generation requires `FEATURES__EMAIL_GENERATION`;
   verification requires `FEATURES__MILLIONVERIFIER`. Both default off; the
   `/verification` page renders the unavailable state while off.
2. **Candidate set.** `generate_candidates` (re)builds a ranked, duplicate-free
   candidate set for the contact and selects exactly one address (imported exact
   address first, else the top-ranked generated candidate). Unrenderable name
   with no domain/email → routed to review, no selection.
3. **Cache check.** Before enqueuing, fresh evidence for the *same exact address*
   is reused (a `cache_reuse` usage event) — no job, no paid call.
4. **Enqueue.** Otherwise an idempotent job is created. Concurrent duplicates
   collapse to one job (unique idempotency key + partial unique active-email
   index).
5. **Claim + call.** A worker leases the job (`FOR UPDATE SKIP LOCKED`),
   re-checks the cache, then makes exactly one provider call.
6. **Map + store.** Address results are stored as `exact_email_verifications`
   with the policy version and role/free/subresult signals; a `call_made` usage
   event records whether it was billed. Transient failures reschedule with
   bounded backoff+jitter; insufficient-credit and config errors fail without
   evidence and surface a distinct warning.
7. **Recovery.** A job whose worker lease expires is reclaimable (or reset by an
   explicit recovery sweep), recording a `recovered` usage event. Its attempt was
   already counted, so a partial call cannot silently double-charge.

## What is NOT done here

* No live MillionVerifier call is made anywhere in code paths exercised by tests
  or the demo. The single live smoke request is a manual acceptance item.
* No scoring, research, drafting, or sending. Those switches stay off.
* No SMTP probing (VER-010) and no campaign-outcome ranking (EML-007).

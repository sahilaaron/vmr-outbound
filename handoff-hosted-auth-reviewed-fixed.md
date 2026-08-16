# Handoff — Hosted Operator Authentication, reviewed-repair pass

This is a **successor repair**, not a rebuild. The two reviewed commits are
untouched — nothing was rebased, squashed, amended or rewritten — and the repair
is one additional commit on top of them. `main` was not modified. Nothing was
pushed and nothing was deployed.

## 1. Identity

| | |
|---|---|
| Repository | `sahilaaron/vmr-outbound` |
| Branch | `feat/hosted-operator-auth` |
| Base (unchanged) | `cb8510a73c872f67514dc0557708c30a20dc64d2` — current `origin/main`, `Merge pull request #257 …` |
| Prior reviewed head | `e26c9875490b02d49d40771ddd2f96a93f9558f4` |
| **Repair commit (all code, tests and docs)** | **`02a1581a178c65747d4add6860b5edac40e4f0a8`** |
| Branch head | one further **doc-only** commit adding this file — SHA in §8 and in the bundle |
| Commits added since the reviewed head | 2 (one repair, one doc-only) |
| Reviewed commits altered | 0 |

```
<doc-only>  Add the reviewed-repair handoff record                         <- added, this file only
02a1581     Make the authentication comparison and policy paths fail closed <- added, the whole repair
e26c987     Add the hosted-auth slice handoff record                        <- reviewed, untouched
f796542     Add hosted-operator authentication and CSRF boundary            <- reviewed, untouched
cb8510a     (base) Merge pull request #257 …
```

Same shape as the slice under review, which the reviewer verified the same way:
every code, test and documentation change is in one commit, and the handoff
commit touches nothing but this file. `git diff 02a1581 <branch head>` is
`handoff-hosted-auth-reviewed-fixed.md` and nothing else, so every gate result in
§6 was measured on a tree identical to the branch head in all executable
respects. The base is an ancestor of the head and the range is linear with no
merges.

### Preserved, as required

Centralised default-deny authentication middleware; Google auth-code + PKCE;
explicit operator allow-list; stateless signed sessions; centralised CSRF;
browser-session / future extension-auth separation; the Production Hardening
middleware and proxy contracts; no Gmail scopes or tokens; **no schema change**
(`git diff cb8510a7 -- migrations/` is empty, `alembic heads` is the same single
head `0926b59b7912`).

## 2. Changed files (repair commit only)

Fourteen files. No new module, no new dependency, no new setting, no new screen.

| File | Change |
|---|---|
| `app/core/auth/session.py` | New shared `constant_time_equal`; `_sign` no longer ASCII-encodes attacker payload; `_decode` refuses a non-ASCII token; `csrf_token_matches` uses the shared comparison |
| `app/core/auth/csrf.py` | `require_csrf` uses the shared comparison and bounds the presented token |
| `app/core/auth/identity.py` | `_constant_equal` delegates to the shared implementation |
| `app/core/auth/jwks.py` | `split_jwt` refuses a non-ASCII assertion; `verify_signature` can no longer raise `UnicodeEncodeError` |
| `app/core/auth/middleware.py` | `_is_cross_site` evaluates every signal and refuses on ambiguity; `_cookie` refuses duplicate cookie *names* |
| `app/core/auth/policy.py` | Anonymity by exact path; `/static/` as the one mount exception; corrected `SAFE_METHODS` rationale; `safe_next_path` refuses an encoded separator |
| `app/core/auth/config.py` | ASCII-only operator identities and allow-list/domain entries; NFKC removed |
| `app/core/auth/startup.py` | Comment stating the `validate_runtime_settings` ordering dependency (review INFORMATIONAL) |
| `docs/HOSTED_AUTH.md` | Anonymous allow-list and OPTIONS sections corrected |
| `docs/POST_LAUNCH_BACKLOG.md` | The deferred LOW findings, recorded with reasons |
| `handoff-hosted-auth-slice.md` | The three false claims corrected in place, each marked as a post-review correction |
| `tests/test_hosted_auth.py` | M-3 path classification, L-6 lookalikes, encoded-separator destinations |
| `tests/test_hosted_auth_templates.py` | Anonymity conformance against the live router table |
| `tests/test_hosted_auth_raw_asgi.py` | **New.** Raw-ASGI regressions for every finding `httpx` cannot express |

## 3. Disposition of each mandatory finding

### H-1 — comparison paths fail closed, never 500 — **REPAIRED**

`hmac.compare_digest` raises `TypeError` the moment a `str` argument holds a
non-ASCII character. Every value compared on this boundary arrives as
attacker-controlled latin-1-decoded text, so one non-ASCII byte in
`X-CSRF-Token` produced an unhandled 500 on a security path.

One shared `session.constant_time_equal` now compares
`.encode("utf-8", "surrogatepass")` bytes. It satisfies each stated requirement:

* **Refuses rather than raises** — a malformed token is an ordinary mismatch, so
  the caller gets the designed 403.
* **Still constant-time** — `compare_digest` is doing the comparison; only its
  argument type changed.
* **Never normalises** — no folding, no transcoding into another representation.
  `test_a_non_ascii_token_is_never_folded_into_a_valid_one` proves a fullwidth
  spelling of a genuinely valid token is still refused.
* **Cannot itself raise** — `surrogatepass` means no `str` a Python program can
  hold, including a lone surrogate, makes the encode step fail.

All four equivalent sites were repaired together: `csrf.require_csrf` (which now
also bounds the presented token), `session.SessionCodec._decode` and `_sign`,
`session.csrf_token_matches`, and `identity._constant_equal`. `_decode` also
refuses a non-ASCII token outright, because every token this codec mints is
ASCII by construction — the session path previously survived only because
`SimpleCookie` happens to drop morsels with non-ASCII values, which is
incidental rather than designed.

Covered by `tests/test_hosted_auth_raw_asgi.py`: 9 hostile CSRF header shapes
(UTF-8 accented, latin-1 high bytes, continuation bytes, BOM, fullwidth, NUL,
oversized non-ASCII, oversized ASCII, empty) and 10 hostile session cookies
(non-ASCII in either segment, high bytes, fullwidth, oversized, two-part, wrong
version, empty segments), each asserting no unhandled exception and the correct
refusal — plus an anti-vacuity test that the valid token still works.

### L-9 — `UnicodeEncodeError` in the ID-token verifier — **REPAIRED with H-1**

`split_jwt` refuses a non-ASCII assertion (a compact JWT is base64url in all
three segments, so ASCII by definition), and `verify_signature` encodes with
`surrogatepass` so it cannot raise even if called directly. Five non-ASCII token
shapes plus a direct `verify_signature` probe are covered.

### M-2 — the OPTIONS contract — **DECIDED, and made consistent**

**Decision: anonymous `OPTIONS` is refused.** The claim is withdrawn rather than
implemented, and the reasoning is deliberate rather than a preference for the
smaller diff:

* The brief permits a preflight exemption *"if this is actually required for the
  future extension architecture."* It is not required today. The only cross-origin
  candidate is the capture extension, and hosted authentication refuses its
  `POST` intake as a 401 like any other anonymous caller — the slice's own handoff
  already states this. Exempting the preflight alone would open an anonymous
  surface for a client that still could not complete a request.
* The measured behaviour was already 401 — *more* restrictive than documented, so
  the repair direction that changes no behaviour is also the safer one.
* A future authenticated cross-origin client genuinely will need a credential-less
  preflight. That exemption belongs with extension authentication, designed as a
  narrow enumerated list of intake paths with CORS headers, no body and no
  authentication implication, and tested with that work. Recorded in
  `docs/POST_LAUNCH_BACKLOG.md`. No false future-oriented promise is preserved.

`SAFE_METHODS` still contains `OPTIONS`, and that is correct and unchanged —
`OPTIONS` never changes state, so the cross-site backstop does not apply to it.
The repair is that the *comment* no longer claims this implies anonymity, which
is the confusion the finding is really about: safe ≠ anonymous.

Updated in all four places the review named: implementation (unchanged, now
correct as documented), `policy.py` comments, `docs/HOSTED_AUTH.md`, and the
committed handoff (marked as a post-review correction rather than silently
edited). Direct test: `test_anonymous_options_is_refused` over 7 paths, asserting
401 and the `unauthorized` body, plus `test_anonymous_options_carries_no_credentialed_cors_headers`.

### M-3 — broad anonymous-prefix ambiguity — **REPAIRED**

Anonymity is now granted by **exact path**, with one explicit mount exception.

* The five sign-in routes are enumerated in `_ANONYMOUS_AUTH_ROUTES`.
* `/static/` is `_ANONYMOUS_STATIC_MOUNT_PREFIX`, documented as the intentional
  `StaticFiles` mount exception rather than a generic future-route prefix.
* Bare `/auth` and bare `/static` are no longer anonymous. Bare `/static`
  mattered: Starlette answered it with a 307 to `/static/`, which told an
  anonymous caller the mount existed.

This closes both halves of the finding — a future
`app.include_router(x, prefix="/auth")` can no longer be public with every gate
green, and the 404-vs-401 tell is gone, so the handoff's route-enumeration claim
is now true rather than aspirational. `/auth/..;/app` now answers 401 instead of
404.

Conformance tests in `tests/test_hosted_auth_templates.py`:

* `test_the_policy_names_exactly_the_intended_anonymous_paths` — the set equals a
  hand-written expected list, so changing it is a visible decision.
* `test_every_auth_router_route_is_named_in_the_anonymous_set` — a sixth sign-in
  route fails until it is a decision.
* `test_no_other_router_mounts_under_an_anonymous_path` — the inverse, walked
  over the live router table.
* `test_the_static_mount_is_the_only_prefix_exception`.

Traversal behaviour proven by the review is preserved and re-asserted:
`/auth/../admin`, `/static/../admin`, `/healthz/../app`, `//app`, `/./app`,
`/%2e%2e/admin` all still refuse, and `/authx` / `/staticky` still do not inherit
anything.

### L-4 — one positive signal must not neutralise another — **REPAIRED**

`_is_cross_site` no longer returns on the first header it finds. Every supplied
signal is evaluated and any positive cross-site signal refuses; two positive
signals that disagree are themselves a refusal. The exact regression asked for —
valid CSRF token, `Sec-Fetch-Site: same-origin`, `Origin: https://evil.example`
— is `test_same_origin_fetch_metadata_cannot_neutralise_a_hostile_origin`,
asserting 403 and `cross_site_request_refused`, with an anti-vacuity test that
consistent same-origin signals are still accepted.

### L-5 — duplicate `Origin` fails closed — **REPAIRED**

`len(origins) != 1` used to read as "absent", which silently disabled layer 1 for
any front end or proxy that duplicated the header. More than one `Origin` now
refuses. Raw-ASGI tests cover valid-then-evil, evil-then-valid and identical
duplicates. The same ambiguity on `Sec-Fetch-Site` is closed the same way.

### L-6 — normalisation must not widen authorisation — **REPAIRED**

NFKC removed. The contract is now: operator identities and configured allow-list
entries **must be ASCII**; non-ASCII is refused rather than folded; case is
normalised (a plain per-character mapping on an ASCII-only value); surrounding
whitespace is stripped and interior whitespace remains unusable. The same rule
runs on both sides, so a lookalike cannot enter from either direction, and a
non-ASCII allow-list or `AUTH__ALLOWED_GOOGLE_DOMAIN` entry now **refuses at
config load**, which means the process refuses to start.

Tests: 8 lookalikes (fullwidth `o`, fullwidth local part, fullwidth domain,
Cyrillic `е`, Cyrillic `о`, zero-width space, non-breaking space, BOM), 5
accepted case/whitespace variants, and startup refusal for both a non-ASCII
allow-list entry and a non-ASCII Workspace domain. The lookalikes are written as
`\uXXXX` escapes on purpose — a literal is exactly what an editor or a paste
silently flattens back to ASCII, which is how the reviewer's own suite lost two
cases (see §5).

### L-7 — duplicate session-cookie names — **REPAIRED**

`_cookie` counts occurrences of the cookie *name* before parsing, because parsing
collapses them, and refuses anything other than exactly one. Neither first-wins
nor last-wins: an attacker who can set a domain-scoped cookie from a sibling host
does not get to choose which credential the boundary reads. Both orders are
tested directly. The existing refusal of multiple `Cookie` headers is unchanged
and re-asserted, and an anti-vacuity test confirms an ordinary multi-cookie
browser jar still authenticates.

### Additional hardening (volunteered, small, labelled)

`safe_next_path` refuses a percent-encoded path separator (`%2f`/`%5c`, either
case). Not a live defect — such a value stays same-origin in every browser that
resolves it — but it survived the filter and reached the rendered page, and a
value that survives one more decoding step than it was checked against is how a
redirect filter is eventually escaped. No operator destination needs one. This is
what turns the reviewer's `test_next_cannot_leave_the_site[/%2f%2fevil.example]`
green by making the code satisfy the assertion, rather than by arguing with it.

## 4. Deferred, as instructed

Recorded in `docs/POST_LAUNCH_BACKLOG.md` with reasons; none implemented.

L-8 (multiple `Cookie` headers / HTTP-2 nuance — now commented in
`middleware._cookie`), L-10 (shortening already-minted sessions), L-11 (JWKS
provider-failure refetch), L-12 (external-action / dynamic Jinja forms), strict
`parse_jwks` liveness for unusual future Google keys, per-operator audit
attribution, extension remote capture, Gmail, Sending, Sheets. The
`AUTH__PUBLIC_BASE_URL` userinfo / `AUTH__COOKIE_DOMAIN` cross-check
informational item is recorded there too.

The one M-2-adjacent design question — the narrow preflight exemption a future
authenticated cross-origin client will need — is recorded as a follow-up rather
than promised in documentation.

## 5. Reviewer attack suite — run unmodified, results explained

The three supplied files were run **exactly as delivered**, with no assertion
altered or weakened, against both heads. They were run out of tree and removed
afterwards, so the branch contains none of them; their contracts were re-encoded
as asserting tests in the repository instead (see §6).

| | Tests | Failures |
|---|---|---|
| Prior reviewed head `e26c987` | 341 | **20** |
| Repaired head `02a1581` | 341 | **18** |

Command: `pytest tests/zz_raw_attacks.py tests/zz_review_attacks.py tests/zz_jwt_attacks.py -W "ignore::DeprecationWarning"`.
The `-W` flag is needed only because this repository sets
`filterwarnings = ["error::DeprecationWarning"]` and `httpx` 0.28 deprecates
per-request `cookies=`; without it, 21 further failures are that deprecation and
nothing else.

**Four failures fixed by the repair**, each the finding it belongs to:

* `zz_raw_attacks::test_non_ascii_csrf_header_is_a_refusal_not_a_crash` — H-1, the
  500. Now 403.
* `zz_raw_attacks::test_path_forms_never_reach_a_protected_handler_anonymously[/auth/..;/app]` — M-3. Now 401.
* `zz_review_attacks::test_next_cannot_leave_the_site[/%2f%2fevil.example]` — the
  encoded-separator hardening.
* `zz_jwt_attacks::test_non_ascii_in_the_payload_segment` — L-9.

**Two failures are new, and are the intentional M-3 redefinition** — flagged
here rather than absorbed, because the brief requires exactly that:

* `zz_review_attacks::test_anonymous_path_classification[/auth-True]`
* `zz_review_attacks::test_anonymous_path_classification[/static-True]`

These assert that bare `/auth` and bare `/static` are anonymous. That was true,
and M-3 is the instruction to stop it being true: an anonymous bare `/auth` is
the prefix rule the finding asks to remove, and an anonymous bare `/static`
returns a 307 to `/static/` that discloses the mount. The expectations were
replaced with **stronger** contract tests, not deleted —
`test_an_unmounted_path_under_a_former_anonymous_prefix_is_protected` (9 paths),
`test_the_static_mount_is_the_only_prefix_exception`, and
`test_unmounted_paths_are_indistinguishable_from_protected_ones` (12 paths) — each
of which fails if either path becomes anonymous again.

**Fourteen failures are harness artefacts or internal contradictions in the
supplied suite, not product behaviour.** Stated precisely, because "the
reviewer's tests are wrong" is a claim that has to be shown:

1. `test_non_ascii_session_cookie_does_not_500` ×3 and
   `test_non_ascii_csrf_header_does_not_500` (4) — these fail **inside `httpx`**
   with `UnicodeEncodeError` while *building* the request; the application is
   never called. This is the exact limitation the review names as its reason for
   writing the raw-ASGI driver, and the raw-ASGI equivalents of all four now
   pass. The repository's new `test_hosted_auth_raw_asgi.py` covers these shapes
   permanently.
2. `test_unknown_environment_names` ×7 — calls `validate_hosted_auth_settings`
   in isolation. The refusal of an unknown `APP_ENV` lives in
   `validate_runtime_settings`, which `create_app` calls immediately after; the
   review verified this itself and recorded it as INFORMATIONAL, not as a
   defect. Behaviour is unchanged and correct; the ordering dependency is now
   commented in `app/core/auth/startup.py` as the review suggested.
3. `test_origin_matrix_with_valid_token[HTTPS://VMR.REVIEW.INVALID]`,
   `[https://VMR.review.INVALID]` and
   `test_valid_token_with_hostile_origin_is_refused[HTTPS://VMR.REVIEW.INVALID]`
   (3) — a blanket parametrisation that contradicts the review's own prose: §4
   states that accepting an uppercase `Origin` is **correct** per RFC 6454
   (scheme and host are case-insensitive) and that it was confirmed not to be a
   case-folding bug. Behaviour deliberately unchanged.
4. `test_near_miss_addresses_are_refused` ×2 — both entries are **pure ASCII**
   (verified by codepoint): one is byte-identical to the approved address, the
   other is that address with a trailing space, which the suite's own
   `test_case_and_whitespace_variants_match` asserts *must* be approved. Two
   intended lookalikes were flattened to ASCII somewhere between authoring and
   delivery. The real L-6 evidence in that file,
   `test_nfkc_widening_of_the_allow_list`, only prints and never asserts, so it
   passed on both heads. L-6 was reproduced independently before repairing it,
   and the repository's replacement tests use `\uXXXX` escapes so they cannot be
   flattened the same way.

## 6. Validation — actual outcomes

Environment built from the repository's own pinned closure and matching the
review's: PostgreSQL 16.13 on `127.0.0.1:5433`, `fastapi==0.141.1`,
`starlette==1.6.0`, `httpx==0.28.1`, `cryptography==46.0.7`, CPython 3.11.15.
**No live Google calls were made** — all identity work uses locally minted RSA
keys and `httpx.MockTransport`.

| Gate | Result |
|---|---|
| Full `pytest` | **3368 passed, 0 failed** (30:10). Baseline on the reviewed head was 3264; +104 tests |
| Hosted auth (`test_hosted_auth.py`, `_templates.py`, `_raw_asgi.py`) | 413 passed |
| Production Hardening, v2 UI, Beta1 operator UI, Workbench, Admin Workbench, SalesNav workbench, Campaigns, API, Phase-2 API, contact capture intake, capture promotion, campaign pipeline, migrations | 432 passed |
| Reviewer hostile attack suite | 341 tests, 18 failures — all accounted for in §5 |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 496 files already formatted |
| `mypy app` | Success, 252 source files |
| `alembic heads` | `0926b59b7912` — single head, unchanged from base |
| `alembic upgrade head` → `alembic check` | No new upgrade operations detected |
| `alembic downgrade base && alembic upgrade head` | Round trip clean |
| `git diff --check cb8510a7` | No whitespace errors |
| Node / extension suite | Correctly not run — `git diff cb8510a7 -- extensions/` is empty |

### Mutation check of the repaired security paths

Each repair was reverted on purpose and the hosted-auth suite re-run. A repair
whose mutation stays green is a repair with no test behind it.

| Mutation | Result |
|---|---|
| `constant_time_equal` → raw `hmac.compare_digest` | **CAUGHT** (9 raw-ASGI CSRF tests) |
| JWT non-ASCII guard removed | **CAUGHT** (5 tests) |
| `verify_signature` back to `.encode("ascii")` | **CAUGHT** |
| `Sec-Fetch-Site` short-circuit restored | **CAUGHT** |
| Duplicate `Origin` treated as absent | **CAUGHT** (3 tests) |
| Duplicate cookie-name check removed | **CAUGHT** |
| NFKC folding restored | **CAUGHT** (7 tests) |
| Anonymous `/auth/` prefix restored | **CAUGHT** (9 tests) |
| Bare `/static` anonymous again | **CAUGHT** (3 test modules) |
| Anonymous `OPTIONS` exempted in the middleware | **CAUGHT** (7 tests) |
| `safe_next_path` encoded-separator guard removed | **CAUGHT** (5 tests) |
| Session-token `isascii()` guard removed | **SURVIVED** — reported below |
| CSRF presented-token length bound removed | **SURVIVED** — reported below |

The two survivors are honest and are **defence in depth on top of a repair that
is itself covered**, not untested behaviour:

* Removing the session `isascii()` guard leaves `constant_time_equal` and the
  `surrogatepass` signing material in place, so a non-ASCII cookie still fails
  signature comparison and still refuses. The guard exists so the rest of
  `_decode` only ever sees the shape it was written for.
* Removing the CSRF length bound leaves a 100 KB token still mismatching and
  still 403. The bound exists so a megabyte of attacker text is not hashed and
  compared before being rejected.

Both are stated rather than papered over with a test that would only assert the
mutation-equivalent behaviour.

## 7. What remains manual, and what to watch

* **Nothing here proves compatibility with a real Google ID token.** Every
  fixture is locally minted. The review's caveat stands unchanged and unsoftened:
  treat the first live sign-in as the acceptance test, not a formality, and have
  a rollback ready for the maintenance window.
* **Enabling `AUTH__ENABLED=true` breaks the SalesNav capture extension** — its
  `POST` intake becomes a 401. Unchanged by this repair, and the M-2 decision
  deliberately does not paper over it with a preflight exemption. Plan it as an
  operational gate.
* The allow-list is still edited in `/etc/vmr/vmr.env` followed by a restart. One
  new failure mode to know about: a non-ASCII address pasted from a document now
  makes the process **refuse to start** rather than boot and silently approve
  nobody. That is the intended direction, and the error names the variable
  without echoing the value.
* Anonymous `/version` still discloses the running SHA; keep
  `vmr-probe-access.conf` closed, as the original handoff says.

## 8. Delivery

| | |
|---|---|
| Bundle | `vmr-outbound-hosted-auth-reviewed-fixed.bundle` |
| Contents | `refs/heads/feat/hosted-operator-auth` @ the doc-only head |
| Requires | `cb8510a73c872f67514dc0557708c30a20dc64d2` (incremental against the base, as instructed) |
| `git bundle verify` | `is okay` |

The branch head SHA and the bundle SHA-256 are in
`HOSTED-AUTH-REPAIR-DELIVERY.txt`, delivered beside the bundle, rather than in
this file: a commit cannot contain its own hash, and a bundle cannot contain the
hash of a file that contains the bundle's hash. Verify against that note, and
against the commands below.

Not pushed. Not deployed. `main` untouched. No reviewed commit rewritten.

To stage locally:

```
git -C <repo> bundle verify <path>\vmr-outbound-hosted-auth-reviewed-fixed.bundle
git -C <repo> fetch <path>\vmr-outbound-hosted-auth-reviewed-fixed.bundle feat/hosted-operator-auth:feat/hosted-operator-auth
git -C <repo> merge-base --is-ancestor cb8510a7 02a1581a && echo ancestor
git -C <repo> log --format="%H %s" cb8510a7..02a1581a
```

## 9. Verdict

Every mandatory repair is implemented, every corrected claim is consistent across
implementation, `policy.py`, `docs/HOSTED_AUTH.md` and the committed handoff, and
each repaired path has a test that fails when the repair is removed. The
reviewer's suite was run unmodified against both heads and every remaining
failure is accounted for above — four fixed, two the intentional M-3
redefinition replaced by stronger contract tests, and fourteen harness artefacts
or internal contradictions in the supplied suite, each shown rather than
asserted.

This is a builder's statement of completion, not an acceptance. Independent
re-review still owns the verdict.

`HOSTED AUTH REVIEWED REPAIR READY FOR FINAL RE-REVIEW`

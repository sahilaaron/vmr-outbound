# Hosted Beta: admin-created users with email/password first-login setup

Issue #270. First implementation slice of #269.

## SHAs, branch, commits

| | |
|---|---|
| Base SHA | `8720b7f4cbda8e9b193551355998ecfc363987be` (origin/main, "Merge pull request #266 from sahilaaron/feat/extension-capture-auth") |
| Branch | `feat/email-password-user-auth` |
| Head SHA | `0729f8ea` (see the delivery note for the full value) |
| Commits | 2 |

1. `9518f1f` — *Give the hosted Beta real user accounts, with a password path*. The whole slice.
2. `0729f8e` — *Repair what the security review found*. Eight findings from the independent adversarial pass, plus their regression tests.

Nothing on `main` was modified. Nothing was merged, pushed or deployed. The Gmail
draft branch (#267) and the hosted capture-promotion/UAT work were not read,
touched or depended on.

## Files changed

41 files, +5562 / −188.

**New — authentication core**
`app/core/auth/accounts.py`, `admin.py`, `passwords.py`, `ratelimit.py`

**New — model and services**
`app/models/user.py`, `app/services/users/{__init__,service,tokens}.py`

**New — surfaces**
`app/web/v2/admin_users.py`, `app/web/v2/templates/admin_users.html`,
`app/web/templates/auth/password_setup{,_done,_invalid}.html`

**New — migration**
`migrations/versions/b8f13a6c47d2_users_accounts_and_credential_tokens.py`

**New — tests**
`tests/test_user_accounts.py` (92 tests)

**Modified**
`app/core/auth/{config,middleware,policy,session,startup}.py`, `app/core/http.py`,
`app/db/base.py`, `app/main.py`, `app/models/enums.py`, `app/web/auth_routes.py`,
`app/web/v2/{context,routes}.py`, three auth templates, `app/web/v2/templates/base.html`,
`deploy/nginx/vmr-staging.conf`, `deploy/vmr.env.example`, `.env.example`,
`docs/HOSTED_AUTH.md`, `pyproject.toml`, and four test modules.

## Migrations

One revision, `b8f13a6c47d2`, `down_revision = 0926b59b7912`.

* Two tables: `users`, `user_credential_tokens`. Three enum types: `user_role`,
  `user_state`, `user_credential_token_purpose` — labels are the enum **member
  names** (`ADMIN`/`USER`), matching what `Enum(PythonEnum)` emits and what every
  other enum type in this schema already uses.
* Two unique indexes carry the design, not an optimisation:
  `ix_users_email_normalized` makes "one person is one account" a database fact,
  and `ix_users_google_subject` does the same for the provider identity (NULLs are
  distinct in Postgres, so any number of accounts may be unlinked).
* **Nothing is back-filled and no ownership is guessed.** Existing `created_by`
  and `actor` columns are free text that cannot be resolved to an account with any
  confidence; they keep their values and keep meaning what they meant. Historical
  activity stays honestly unattributed.
* No plaintext secret appears in the migration.

Validated against a scratch database:

```
alembic heads                      -> b8f13a6c47d2 (head)      single head
alembic upgrade 0926b59b7912       -> ok                        current main schema
alembic upgrade head               -> ok                        the new revision
alembic downgrade -1               -> ok                        clean
alembic upgrade head               -> ok                        re-upgrade clean
alembic check                      -> "No new upgrade operations detected."
```

## User/auth architecture

**The one sentence:** Google proves identity; the VMR account record grants access.

* `users` — UUID id, `email` (as typed) + `email_normalized` (unique, the only
  thing anything compares), display name, `role` (ADMIN/USER), `state`
  (ACTIVE/DISABLED), `password_hash` (nullable = *cannot* authenticate with a
  password, never "empty password"), `password_set_at`, `google_subject` (unique
  when present) + `google_linked_at`, `auth_version`, `created_at`, `updated_at`,
  `last_login_at`, `created_by`.
* `user_credential_tokens` — one row per issued password link: digest, purpose,
  expiry, `consumed_at`, `superseded_at`, `issued_by`.
* `AccountDirectory` (`app/core/auth/accounts.py`) is the seam between the session
  cookie and the account row. The live implementation reads the database; tests
  inject a deterministic one, exactly as `IdentityProvider` already works.

**What `AUTH__ALLOWED_OPERATOR_EMAILS` became.** A one-time seed, not a gate. On
the first start after the migration, every address in it with no account gets one
(role USER, active, no password), so nobody who could sign in yesterday is locked
out today. It may now be empty; the startup contract refuses an empty
`AUTH__BOOTSTRAP_ADMIN_EMAIL` instead. **Removing an address from it no longer
revokes anything** — disable the account, which is stronger and needs no restart.

## Admin bootstrap

`AUTH__BOOTSTRAP_ADMIN_EMAIL`, default `sahil@verifiedmarketresearch.com`, applied
in the FastAPI lifespan handler on every start. Idempotent, audited
(`user.bootstrap_admin_ensured`), and inert when `AUTH__ENABLED` is false.

* No account → create as ADMIN, active, **no password**.
* Account exists as ADMIN → do nothing.
* Account exists as USER → promote and record it (the path a deployment takes
  when the address was already a seeded allow-list entry).
* Account exists but is **disabled** → left disabled. Disabling is an explicit
  act; a restart must not undo it.

Never inferred from a domain. `test_another_vmr_domain_address_is_not_automatically_an_administrator`
pins that a second `@verifiedmarketresearch.com` account is a plain USER.

Best-effort by design: a database unreachable at boot logs
`account_bootstrap_deferred` and does not stop the process, because the probes and
the sign-in page are exactly what an operator needs in that situation and both
work without it. The next successful start converges.

**No ambiguity was found in the historical hosted identity.** The previous slice
had no user records at all — access came from an environment variable — so there
was no identity to link and nothing to guess between.

## Password hashing

Argon2id via `argon2-cffi` (new dependency, `>=23.1,<26.0`), at OWASP's minimum
configuration: **19456 KiB memory, time_cost 2, parallelism 1, 32-byte hash,
16-byte salt**. ~40 ms per verification.

Rejected and recorded in `app/core/auth/passwords.py`: bcrypt (silently truncates
at 72 bytes, which collides with the requirement to accept 64+ character
password-manager output), PBKDF2 (stdlib but not memory-hard), and any bare hash.

Policy: minimum 15, maximum 256, at least 64 accepted, spaces and generated
strings permitted, no composition rules, no expiry, a bounded blocklist of ~35
common values, and a refusal to use the account's own address. Paste and autofill
work — there is no `onpaste` handler anywhere and the forms carry `autocomplete="username"`
/ `"current-password"` / `"new-password"`.

## Token lifetime and storage

* 32 bytes of `secrets.token_urlsafe` entropy. **Only `sha256(raw)` is stored**
  (hex, 64 chars). SHA-256 rather than Argon2id is correct here and the reasoning
  is in the module docstring: the input is 256 bits of entropy, so there is
  nothing to slow down.
* 24-hour bounded expiry. Issuing supersedes every outstanding link for that
  account in the same transaction.
* Four independent refusals, each with its own column or lookup: consumed,
  expired, superseded, disabled account. One indistinguishable outcome for all
  four.
* The raw value is returned once and never again: not stored, not logged, not in
  the audit payload, not re-rendered, not exposed by any API. `IssuedToken.__repr__`
  redacts it. It is held between the POST and the GET under an unguessable handle
  bound to the issuing session, and dropped the first time it is rendered.
* `GET /auth/setup` validates but does **not** consume, so a chat client's link
  preview cannot burn somebody's only way in.

## Session revocation

The cookie carries `uid` and `av` (the account's `auth_version` at mint time). The
middleware resolves the account on every authenticated request and compares.

`auth_version` is incremented by: disable, reactivate, role change, password
set/reset. So disabling refuses live sessions on the next request; a password
reset invalidates earlier sessions; and reactivation does **not** resurrect the
sessions that disabling revoked.

Cookies minted before this slice carry neither claim, fail to decode, and their
holders sign in again. One-time cost, safe direction.

**What this costs, and what bounds it.** One indexed primary-key lookup per
authenticated request; authentication now depends on the database. Probes, the
whole `/auth/*` surface and the `/static/` mount are decided without a lookup
(`is_identity_free_path`, deliberately narrower than `is_anonymous_path` — that
distinction is what keeps `POST /auth/logout` CSRF-protected). A lookup that
cannot be answered returns **503 with the session cookie intact**, so an outage is
never mistaken for a mass sign-out, and an extension capture is unaffected because
its bearer credential never needed the directory.

No existing cookie, CSRF, same-origin or logout protection was weakened.

## Google / local linking rules

1. By `sub` when already linked — stable across a Workspace address rename.
2. By address otherwise, recording the `sub` on that first successful sign-in.

Refused rather than resolved, because each would create a second identity for one
person: a `sub` already linked to a different account than the address resolves
to, and a new `sub` on an address that already carries a different one.

Scopes are unchanged: `openid`, `email`, `profile`. No Gmail scope. Extension
bearer auth untouched. The UI says "Sign in with Google", never "Gmail".

## Routes and UI added

| Route | Who |
|---|---|
| `POST /auth/password` | anonymous — email/password sign-in, rate-limited |
| `GET /auth/setup` | anonymous — first-login form, authorized by a one-time token |
| `POST /auth/setup` | anonymous — consumes the token, stores the hash, does **not** sign in |
| `GET /app/admin/users` | ADMIN |
| `POST /app/admin/users/create` | ADMIN |
| `POST /app/admin/users/{id}/state` | ADMIN |
| `POST /app/admin/users/{id}/role` | ADMIN |
| `POST /app/admin/users/{id}/link` | ADMIN |

`require_admin` is a **router-level** dependency, so a route added to that module
later is authorized the moment it is registered. The account-menu entry is a
courtesy; typing the URL gets a 403 with `{"error": "admin_required"}`.

The sign-in page now leads with the password form and offers Google below it. The
account chip names the signed-in person on a hosted deployment and is unchanged
locally.

## Validation

| Gate | Result |
|---|---|
| `ruff check app tests migrations` | clean |
| `ruff format --check` | 427 files already formatted |
| `mypy app` (strict) | clean, 262 source files |
| Focused: `tests/test_user_accounts.py` | **92 passed** |
| Regression: `tests/test_hosted_auth.py` | **205 passed** |
| Regression: `tests/test_hosted_auth_templates.py` | **146 passed** |
| Regression: `tests/test_extension_capture_auth.py` | **80 passed** |
| Regression: `tests/test_hosted_auth_raw_asgi.py` | **73 passed** |
| Migrations: `tests/test_migrations.py` | **14 passed** |
| **Full suite** | **3562 passed, 0 failed, 0 errors** |
| Baseline full suite at `8720b7f` | **3465 passed, 0 failed** |

**No pre-existing failures.** The baseline initially showed 13 failures in
`tests/test_migrations.py`; all 13 were the sandbox running pytest without the
virtualenv's `bin` on `PATH`, so the tests' `subprocess.run(["alembic", ...])`
raised `FileNotFoundError`. Re-run with `PATH` set, the baseline is fully green.
Both suites here were run with `PATH` set.

Net **+97 tests**, all green.

### Existing tests that were changed, and why

Four assertions encoded the *previous* authorization model and could not survive a
change of authority. Each was replaced rather than deleted, and the replacement
asserts the same property under the new model:

* `test_removing_an_operator_from_the_allow_list_revokes_a_live_session` →
  `test_disabling_the_account_revokes_a_live_session`, plus a new
  `test_reactivating_an_account_does_not_resurrect_its_old_sessions`. The old
  mechanism needed a file edit and a restart; the new one is one `UPDATE`,
  effective on the next request, asserted on the same cookie with nothing rebuilt.
* `test_a_verified_google_identity_outside_the_allow_list_is_refused` →
  `..._with_no_account_is_refused`. Same 403, different authority.
* The startup-contract case `AUTH__ALLOWED_OPERATOR_EMAILS=[]` →
  `AUTH__BOOTSTRAP_ADMIN_EMAIL=""`. The guard moved because the authority moved.
* `EXPECTED_ANONYMOUS_PATHS` gained `/auth/password` and `/auth/setup`, recorded as
  a deliberate decision in both the policy and the test.

The `operator_claims` fixture now derives a distinct Google `sub` per address. A
single hard-coded subject was harmless when the allow-list decided from the email;
under `sub`-first resolution it would have made every "a different Google account"
test silently resolve to the same VMR account and assert nothing.

## Security review

One focused adversarial pass, independent, read-only, run against `9518f1f`.
Verdict: **FAIL** — two substantive findings. Both are fixed in `0729f8e`, each
with a regression test. Re-reading the fixed paths, no finding survives.

Explicitly **not** found, after end-to-end reading: no authentication bypass, no
non-admin path to `/app/admin/*`, no CSRF regression, no open redirect, no hash
exposure in any template, response or `repr()`, no raw token in any audit payload,
no rollback-then-reuse defect, no migration data loss.

| Sev | Finding | Fix |
|---|---|---|
| HIGH | The per-client rate-limit bucket keyed on the raw ASGI peer, which behind nginx is `127.0.0.1` for **every** request. One bucket, 20 attempts, shared by the whole deployment — any anonymous caller could exhaust it and refuse password sign-in site-wide, indefinitely, at ~4 requests/minute. | The hardening boundary now publishes `state["client_ip"]` (the address it already resolved under the one forwarded-header trust rule); the limiter reads that and **skips** the client bucket when there is no resolvable address, rather than sharing an "unknown" one. |
| MEDIUM | The login path did not bound the password before NFKC + Argon2. The no-account branch spends a *fixed* dummy verification, so only the real-account branch grew with input size — a timing oracle on the endpoint that is otherwise careful to be non-enumerating, plus unauthenticated CPU. | Truncated to `MAX_PASSWORD_CHARS + 1` before verification, so an over-long value stays a mismatch instead of becoming a shorter, possibly-correct password. |
| MEDIUM | The setup token would have been written to nginx's access log via `$request` — the one place a live 24-hour single-use link was recorded, readable from log rotations, backups and shippers. The app's own log was already clean. | `location = /auth/setup { ... access_log off; }` in the shipped config, and the deployment note in `docs/HOSTED_AUTH.md` now points at it. |
| LOW | The one-time link was held under the **target account's id**, which every row of the users table renders — so a second administrator could poll `?issued=<uuid>`, drain a colleague's link and take over that account while the audit trail named the colleague as issuer. | An unguessable handle, bound to the session that created it; a wrong-session read consumes the handle and returns nothing. |
| LOW | A disabled account had its Google `sub` linked and **committed** before the state check refused the sign-in — and since a *different* `sub` is refused afterwards, the real owner would be locked out of the Google path for good after reactivation. | The state check moved above the linking block. |
| LOW | `seed_from_allowlist` ran on every start, so an account deleted by hand in an emergency came back active at the next restart. | Genuinely one-shot: it returns immediately once the directory holds an ordinary account the list does not explain. |
| LOW | `POST /auth/setup` returned 500 during a directory outage where the `GET` returned 503. | Same `unavailable.html` 503 branch on both. |
| LOW | An over-long email in account creation reached the driver as a `DataError`, bypassing the admin screen's own error handling and becoming a 500. | `UserServiceError` at 320 characters. |
| INFO | Issuing a reset link does not itself end the account's live session. | Behaviour kept (issuing a *first* link must not sign anyone out); the screen copy now says so and tells the administrator to disable the account when reacting to a compromise. |
| INFO | A directory outage 503'd an extension capture that happened to carry a stale session cookie. | The capture credential is now resolved before the directory verdict is acted on. |

## Staging env / config changes required

`/etc/vmr/vmr.env` — one new key:

```
AUTH__BOOTSTRAP_ADMIN_EMAIL=sahil@verifiedmarketresearch.com
```

Startup **refuses** without it. `AUTH__ALLOWED_OPERATOR_EMAILS` may stay exactly
as it is; its entries become accounts on the first start and it stops being a
gate. Everything else is unchanged.

nginx — take the new `location = /auth/setup` block from
`deploy/nginx/vmr-staging.conf` into the deployed config, then `nginx -t` and
reload. Without it the reverse proxy logs live password links.

Python dependency — `argon2-cffi>=23.1,<26.0`. Pure-Python install with
prebuilt wheels; no service, no key, no recurring cost. Reinstall the venv on the
VPS as part of the deploy.

## Google Cloud Console change still required

The OAuth client is on the `vmr-outbound` project with audience **Internal**, so
today only `verifiedmarketresearch.com` Google accounts can complete a Google
sign-in. A colleague on a personal Gmail account can still use VMR — with an
email address and a password — but their *Google* button will fail at Google's
end, not at ours.

If Google sign-in is wanted for accounts outside the Workspace, the audience must
be changed to **External** on the Google Auth Platform screen (and Publishing
status considered). That is a console/deployment task and is deliberately not
faked in application code; the VMR side already works the moment the audience
allows it.

## What remains manual

* Sending a password link to its owner. There is no transactional email, so the
  administrator copies the link from the screen and sends it out of band.
* Password reset is administrator-initiated for the same reason.
* Changing a display name after creation.
* Changing the bootstrap administrator (an env edit and a restart).

## Deliberately deferred

* Microsoft/Entra OAuth — the password path covers Microsoft 365 colleagues.
* Transactional email for links.
* Self-service "forgot password".
* Second factor.
* Per-user attribution of campaigns and costs, and the #269 dashboard. The `User`
  model is shaped so that lands without redoing authentication.
* A shared-store rate limiter. In-process is right for one uvicorn process; with
  multiple workers the effective limit multiplies by the worker count, which is
  written down in the module rather than hidden.
* Making `AUTH__GOOGLE_CLIENT_ID/SECRET` optional so a password-only deployment
  can start. Unchanged on purpose — it is not needed for #270 and relaxing a
  startup guard without a present need is the wrong direction.

## Merge / deploy / UAT sequence

1. Push `feat/email-password-user-auth`; open the PR against `main`. **Do not
   merge yet.**
2. CI green.
3. ChatGPT review. Point it at the two SHAs and at the security-review table
   above — the "explicitly not found" list is the part most useful to attack.
4. Merge after review.
5. **Before** deploying: add `AUTH__BOOTSTRAP_ADMIN_EMAIL` to `/etc/vmr/vmr.env`
   and take the `location = /auth/setup` block into the deployed nginx config
   (`nginx -t`, reload).
6. Deploy: pull, reinstall the venv (picks up `argon2-cffi`), `alembic upgrade
   head`, restart. Confirm `/readyz` and check the log for
   `account_bootstrap_deferred` — if it appears, the database was not reachable
   at boot and a restart after it is will converge.
7. Staging UAT, in this order:
   1. Sign in as `sahil@verifiedmarketresearch.com` with Google. Confirm the
      account menu shows **People**.
   2. Create a test account on a Gmail address. Copy the link **once**; reload the
      page and confirm it is gone.
   3. Open the link in a private window, set a 15+ character password, confirm it
      lands on the sign-in page and did **not** sign in.
   4. Sign in with that email and password. Confirm the account chip names them
      and **People** is absent.
   5. In that second window type `/app/admin/users` — expect 403.
   6. As the administrator, disable that account. Reload the second window —
      expect to be signed out on the next request, no restart.
   7. Reactivate, issue a new link, and confirm the old link now fails.
   8. Sign in with a Google account that has no VMR account — expect the "does not
      have a VMR Outbound account" page and confirm no row was created.
   9. Fail a password sign-in six times and confirm the 429 with `Retry-After`.
8. Update the tracker sheet (Sahil / ChatGPT own that). Close #270 with the UAT
   evidence and note that #269's attribution work is unblocked.

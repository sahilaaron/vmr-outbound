# Handoff — hosted-operator authentication slice

**Issue:** #247 — Production integration: secure campaign write endpoints before VPS exposure
**Branch:** `feat/hosted-operator-auth`
**Exact base:** `cb8510a73c872f67514dc0557708c30a20dc64d2`
**Code commit (the entire slice):** `f796542b875fe633c50f5f097f7190475cd86455`
**Exact final head:** the branch tip, which is one further commit adding only this
document. Its SHA and the bundle SHA-256 are stated in the delivery message rather
than here, because a file cannot contain the hash of the commit that introduces it.
**Migration head:** unchanged — **no schema change in this slice**
**Not pushed. Not deployed. `main` untouched. No merge, rebase, squash or history rewrite.**

---

## 1. What #247 turned out to be

The issue named two endpoints. Reading `main` at the frozen base found the real
surface is materially larger, and that is what this slice secures.

| Finding at `cb8510a` | Detail |
|---|---|
| **119 state-changing routes, none authenticated** | 94 web (`routes.py` 69, `v2/routes.py` 15, `company_intelligence.py` 8, `admin_workbench.py` 2) + 25 API (`phase2.py` 19, `api/routes.py` 6) |
| **No authentication of any kind existed** | No session, no user, no token, no `Authorization` read anywhere, no `app/core/auth*` module |
| **The only guards were environmental, not identity-based** | `FEATURES__WORKBENCH` + `APP_ENV=local`; `_LOCAL_ONLY_FEATURES`; `_origin_allowed()` (returns `True` for a **missing** `Origin`, so curl passed it); `_same_origin()` (deliberately allowed `Origin: null`, covered 4 of 94 web writes) |
| **No CSRF anywhere** | 111 POST forms across 47 templates, four independent Jinja environments, four independent `_render` helpers |
| **`phase2.py` fully public in OpenAPI** | All 28 routes; only `GET /api/campaigns` had any gate at all |
| **`srv1885453.hstgr.cloud` appears nowhere in the repo** | `deploy/nginx/vmr-staging.conf`, `deploy/vmr.env.example`, `deploy/deploy.conf.example` all carry `REPLACE_WITH_STAGING_DNS_NAME`, and `vmr-deploy` exits 2 if the three disagree |

`POST /api/campaigns` (`phase2.py:339`) and `POST /campaigns` (`api/routes.py:679`)
are two different routes, not duplicates — the rich Phase-2 creator and a Phase-1
thin shell. Both are now protected, along with the other 117.

---

## 2. Architecture

One central, default-deny boundary. Not 119 route patches.

```
nginx
  -> ProductionHTTPMiddleware        request id, security headers, size ceiling, access log
  -> CanonicalTrustedHostMiddleware  Host allow-list
  -> OperatorAuthenticationMiddleware   <-- the boundary, BEFORE routing
  -> routing
  -> require_csrf router dependency
  -> handler
```

**Default-deny.** Anonymous callers may reach exactly: `/healthz`, `/health`,
`/readyz`, `/ready`, `/version`, the five enumerated sign-in routes
(`/auth/login`, `/auth/google/start`, `/auth/callback`, `/auth/logout`,
`/auth/signed-out`) and assets under the `/static/` mount. Everything else
requires an approved operator — including `/docs`, `/redoc`, `/openapi.json`,
bare `/auth`, bare `/static` and paths that do not exist. `OPTIONS` is **not**
anonymous: it is refused like any other method. Because the decision runs before
routing, an anonymous caller cannot distinguish a 404 from a protected route,
and an alternate spelling of a path cannot dodge the match.

> **Corrected 2026-08-10 (post-review).** This paragraph originally claimed
> `/auth/*` and `/static/*` were anonymous *prefixes* and that `OPTIONS` was
> answered anonymously on any path. The independent hostile review found that
> the `OPTIONS` claim was never implemented (the measured behaviour was, and
> remains, `401`), and that the prefix form let an unmounted `/auth/x` answer
> `404` while every other unknown path answered `401` — a route-enumeration
> difference the same section claimed was impossible. Findings M-2 and M-3. The
> code, `policy.py`, `docs/HOSTED_AUTH.md` and this paragraph now agree, and
> both properties are pinned by tests.

**Identity.** Google authorization-code flow with PKCE, identity scopes only
(`openid email profile`). Full verification, every item mandatory:

| Check | Module |
|---|---|
| `state` vs the signed single-use transaction cookie | `app/web/auth_routes.py` |
| PKCE `code_verifier` | `app/web/auth_routes.py` |
| **RS256 signature vs Google's JWKS**, exact `kid`, single accepted algorithm, self-nominated keys (`jwk`/`jku`/`x5u`/`x5c`) refused | `app/core/auth/jwks.py` |
| `iss`, `aud` (constant-time), `nonce` (constant-time), `exp`/`iat`, `email_verified` | `app/core/auth/identity.py` |
| approved-operator allow-list | `app/core/auth/config.py` |

No email, name or identifier is ever taken from a request parameter, form field
or header. There is no code path in the package that turns an unverified token
into claims.

**Authorization.** An explicit configured allow-list of addresses, not a Google
domain. Empty means nobody. Re-checked on **every request**, so removing an
address revokes that operator's live session on their next request.

**Session.** Signed stateless cookie. `HttpOnly`, `Secure`, `SameSite=Lax`,
host-only, 12-hour absolute expiry, no sliding renewal, fresh session identifier
on every sign-in. Three independently derived keys (session / CSRF / login
transaction) from one secret.

**CSRF.** Two layers, both fail closed:
1. an origin backstop in the middleware — refuses any unsafe method with a
   positive cross-site signal, *including* `Origin: null` and empty;
2. a per-session token compared with `hmac.compare_digest`, from the `_csrf`
   form field or the `X-CSRF-Token` header.

The token reaches all 111 forms through a Jinja **compile-time** extension
(`app/core/auth/templating.py`) — **zero templates were hand-edited**.
Enforcement is one `dependencies=[Depends(require_csrf)]` per router. Two
conformance tests assert the coverage of both declarations.

**Roles.** None. `/app` and `/admin` have identical access semantics today and
the same people use both; one role gets added the day a real distinction exists.
Recorded in ADR 0011.

**Extension boundary preserved.** The middleware records `auth_credential`, and
the CSRF dependency skips any credential that is not the cookie — a bearer token
is not attached by the browser automatically. Nothing issues a bearer token in
this slice, so an extension-origin request is refused like any other anonymous
caller. No extension code was touched.

---

## 3. Changed files

**New (13)**

```
app/core/auth/__init__.py          package docstring only (avoids an import cycle)
app/core/auth/config.py            AuthSettings, email normalisation, approval policy
app/core/auth/context.py           the two request-scoped context variables
app/core/auth/csrf.py              csrf_field(), require_csrf, register_csrf
app/core/auth/google.py            Google OAuth client (identity scopes only)
app/core/auth/identity.py          IdentityProvider seam + claim rules
app/core/auth/jwks.py              RS256 / JWKS signature verification
app/core/auth/middleware.py        OperatorAuthenticationMiddleware
app/core/auth/policy.py            anonymous allow-list, path normalisation, safe_next_path
app/core/auth/session.py           signed session + CSRF derivation + login transaction
app/core/auth/startup.py           the startup contract
app/core/auth/templating.py        the compile-time CSRF form extension
app/web/auth_routes.py             /auth/login, /auth/google/start, /auth/callback, /auth/logout, /auth/signed-out
```

```
app/web/templates/auth/base.html         self-contained sign-in shell (inline CSS, no /static, no third-party fonts)
app/web/templates/auth/sign_in.html
app/web/templates/auth/denied.html
app/web/templates/auth/signed_out.html
app/web/templates/auth/unavailable.html
docs/HOSTED_AUTH.md
docs/decisions/0011-hosted-operator-authentication.md
tests/hosted_auth_factory.py             real RSA keys, real RS256 signatures, MockTransport JWKS
tests/test_hosted_auth.py
tests/test_hosted_auth_templates.py
```

**Modified (20)**

| File | Change |
|---|---|
| `app/main.py` | startup contract replaces the workbench rule; auth middleware; auth router; `CsrfError` handler; `identity_provider` factory parameter; `WorkbenchConfigurationError` kept as an alias |
| `app/core/config.py` | `auth: AuthSettings` nested block |
| `app/core/http.py` | publishes `state["forwarded_scheme"]` and `state["trusted_proxy"]` — 2 lines, so nothing downstream re-parses proxy headers |
| `app/api/routes.py`, `app/api/phase2.py` | `dependencies=[Depends(require_csrf)]` |
| `app/web/routes.py`, `app/web/v2/routes.py`, `app/web/admin_workbench.py`, `app/web/company_intelligence.py` | `dependencies=[Depends(require_csrf)]` + `register_csrf(templates.env)` |
| `app/web/templates/base.html`, `app/web/v2/templates/base.html` | sign-out control, rendered only when a session exists |
| `app/web/static/app.css`, `app/web/static/v2.css` | styles for those two controls |
| `.env.example`, `deploy/vmr.env.example` | the `AUTH__*` block |
| `docs/STAGING_RUNBOOK.md`, `docs/PRODUCTION_HARDENING.md`, `docs/POST_LAUNCH_BACKLOG.md` | contract change, startup refusals, deferred items |
| `tests/test_workbench_web.py`, `tests/test_capture_promotion.py` | the three tests that asserted the replaced rule |

No file under `extensions/` was touched. No migration. No new dependency.

---

## 4. New configuration keys

All under the `AUTH__` prefix, nested exactly like `FEATURES__`.

| Key | Default | Notes |
|---|---|---|
| `AUTH__ENABLED` | `false` | Mandatory in staging/production |
| `AUTH__SESSION_SECRET` | — | **secret**; `repr=False`, `exclude=True`; ≥32 chars |
| `AUTH__GOOGLE_CLIENT_ID` | — | Identity client only |
| `AUTH__GOOGLE_CLIENT_SECRET` | — | **secret**; `repr=False`, `exclude=True` |
| `AUTH__ALLOWED_OPERATOR_EMAILS` | `[]` | JSON array; empty = nobody |
| `AUTH__ALLOWED_GOOGLE_DOMAIN` | unset | Optional *extra* gate |
| `AUTH__PUBLIC_BASE_URL` | unset | Canonical origin; the redirect URI is built from it |
| `AUTH__SESSION_MAX_AGE_SECONDS` | `43200` | Absolute, non-renewable |
| `AUTH__LOGIN_TRANSACTION_MAX_AGE_SECONDS` | `600` | |
| `AUTH__COOKIE_SECURE` | `true` | Refused as false in staging/production |
| `AUTH__COOKIE_DOMAIN` | unset | Host-only by default |
| `AUTH__GOOGLE_AUTHORIZATION_ENDPOINT` / `_TOKEN_ENDPOINT` / `_ISSUERS` / `_REQUEST_TIMEOUT_SECONDS` | documented Google values | Overridable for tests only |

---

## 5. Exact staging configuration

Add to `/etc/vmr/vmr.env` (0640 root:vmr), alongside the existing block:

```sh
AUTH__ENABLED=true
AUTH__SESSION_SECRET=<python3 -c "import secrets;print(secrets.token_urlsafe(48))">
AUTH__GOOGLE_CLIENT_ID=<from Google Cloud Console>
AUTH__GOOGLE_CLIENT_SECRET=<from Google Cloud Console>
AUTH__ALLOWED_OPERATOR_EMAILS=["sahil@<your-workspace-domain>"]
AUTH__PUBLIC_BASE_URL=https://srv1885453.hstgr.cloud
AUTH__COOKIE_SECURE=true

# Optional — mounts /app and /admin. Only legal behind the block above.
FEATURES__WORKBENCH=true
```

The same DNS name must already appear in `TRUSTED_HOSTS`, the nginx
`server_name` and `VMR_HEALTH_HOST`; `vmr-deploy` exits 2 if they disagree.
`AUTH__PUBLIC_BASE_URL` now has to match them too.

> ### Deployment ordering — read this before deploying
> A staging box with no `AUTH__*` block **will refuse to start** on the first
> release containing this slice. That is the intended behaviour: the alternative
> is a working-looking site with 119 anonymous write endpoints. Update
> `/etc/vmr/vmr.env` **in the same maintenance window** as the release. The
> deploy gate will otherwise fail at `/healthz` and `vmr-deploy` will repoint the
> previous release (exit 4).

**nginx.** `deploy/nginx/vmr-access.conf` ships as `deny all;`. Opening
`location /` to the Internet so operators can reach the sign-in page is your
decision, not something this slice changed. The probe snippet should stay closed
— `/version` still discloses the running SHA. Nothing else in the proxy
configuration needs to change: `X-Forwarded-Proto` handling is already correct
and the auth layer reads the hardening middleware's resolved verdict rather than
re-parsing it.

---

## 6. Google Cloud Console

**APIs & Services → Credentials → Create credentials → OAuth client ID → Web application.**

| Field | Exact value |
|---|---|
| Authorised JavaScript origin | `https://srv1885453.hstgr.cloud` |
| Authorised redirect URI | `https://srv1885453.hstgr.cloud/auth/callback` |

No trailing slash on either. Consent screen: **Internal** if the Workspace allows
it. Scopes: `openid`, `email`, `profile` — nothing else.

**Do not add a Gmail scope to this client and do not reuse it for mailbox
authorization later.** Signing a person in to VMR and authorising a mailbox are
different grants with different blast radii, and merging them would mean every
sign-in silently carried mailbox authority.

---

## 7. Threat and bypass analysis

**Refused, with a test for each**

| Attack | Outcome |
|---|---|
| Anonymous read of `/app`, `/admin`, any API, `/docs`, `/redoc`, `/openapi.json` | 401 (or a sign-in redirect for a browser navigation) |
| Anonymous `POST /api/campaigns`, `POST /campaigns`, and 10 other writes across all six routers | 401 |
| A write answered with a redirect a client might read as success | Never — writes get 401/403, never 3xx |
| Route enumeration by 404-vs-401 | Impossible anonymously; the decision precedes routing, and no anonymous *prefix* exists for an unmounted path to answer 404 under (corrected post-review — see M-3) |
| Path-form bypass: `//admin`, `/admin/`, `/healthz/../admin`, `/static/../admin`, `/auth/../admin` | Normalised to the protected form and refused |
| Open redirect via `?next=`: `https://evil`, `//evil`, `/\evil`, CRLF injection | Falls back to `/app` |
| Valid, fully verified Google identity that is not approved | 403 with a page naming no other operator |
| Forged `state` | 403, no session |
| Callback replayed a second time, or into a different browser | 403 — the transaction cookie is single-use and deleted as the session is minted |
| Expired sign-in transaction | 403 |
| ID token signed by the wrong key | Refused |
| ID token with a tampered payload | Refused |
| `alg: none`, `HS256`, `RS512`, `ES256`, empty | Refused — one accepted value, compared by equality |
| Token nominating its own key (`jwk`/`jku`/`x5u`/`x5c`) | Refused |
| Unknown `kid` (with a rate-limited single refresh, so forgeries cannot drive outbound traffic) | Refused; genuine key rotation still works |
| Wrong `iss`, wrong `aud`, wrong `nonce`, expired, `email_verified: false` | Refused |
| Forged session signature | Refused (constant-time compare) |
| Expired session, future-dated session, malformed/oversized cookie | Refused |
| Operator removed from the allow-list, presenting a still-valid cookie | Refused on the next request |
| CSRF missing / wrong / borrowed from another session | 403 |
| Cross-site write with a **valid** token and `Origin: https://evil`, `Origin: null`, `Sec-Fetch-Site: cross-site`/`same-site`, or a scheme downgrade | 403 at the origin backstop, before the token is consulted |
| Forged `Host` | 400, before authentication runs |
| Multiple `Cookie` or `Host` headers (request-smuggling shapes) | Not reassembled; treated as absent |

**Accepted risks, stated rather than hidden**

1. **A stolen session cookie stays valid until its absolute expiry** (12 h) or
   until the allow-list or signing secret changes. Mitigations: `HttpOnly`
   removes the XSS path, `Secure` + HSTS removes the plaintext path, the lifetime
   is bounded and non-renewable, and rotating `AUTH__SESSION_SECRET` invalidates
   every live session at once. This is the explicit trade for having no session
   table — see ADR 0011.
2. **A missing `Origin` *and* missing `Sec-Fetch-Site` is not itself a refusal.**
   It falls through to the token check, which fails closed. No browser omits
   `Origin` on a cross-site form post, so this affects scripted clients only —
   and they still need a valid per-session token.
3. **~~`OPTIONS` is answered anonymously.~~ Withdrawn 2026-08-10 (M-2).** This
   was never implemented — an anonymous `OPTIONS` has always been refused with
   `401` — and it is not being implemented, because nothing needs it yet. The
   capture extension is refused by hosted authentication regardless of the
   preflight, so exempting the preflight alone would open an anonymous surface
   for a client that still could not complete a request. The narrow, enumerated
   preflight exemption a future authenticated cross-origin client will need is
   recorded in `docs/POST_LAUNCH_BACKLOG.md` and belongs with extension
   authentication, not ahead of it.
4. **The CSRF token is stable for a session's lifetime.** Combined with response
   compression at the proxy this is theoretically BREACH-adjacent. The
   application sets `Cache-Control: no-store` on every non-static response and
   `Referrer-Policy: no-referrer`, and the token never appears in a URL. If nginx
   gzip is ever enabled for HTML on this host, revisit.
5. **The ID-token verification code is ours, not a vendor library's.** It is
   written against named attacks and covered by tests that mint real forged
   tokens. Swapping in PyJWT touches `app/core/auth/jwks.py` only. Flagged as a
   deliberate, reversible choice — your call, not mine.
6. **Writes are still attributed to the constant `OPERATOR_ACTOR`.** A verified
   identity now exists on every request, so per-operator attribution is possible
   for the first time; it is deliberately deferred (backlog) because it needs
   three decisions — existing rows, email vs subject id, and how the
   session-less Agent worker attributes its own writes.

**Explicitly out of scope and untouched:** Gmail OAuth, drafts, sending, reply
ingestion, Sheets, provider cadence, SalesHandy/Instantly, password signup,
customer tenancy, billing, broad RBAC, remote extension capture.

---

## 8. Test results

````
Full pytest suite (4 shards, each against its own PostgreSQL 16 database):

  shard 0   773 passed   0 failed   11m08s
  shard 1   737 passed   0 failed   10m26s
  shard 2   993 passed   0 failed   13m59s
  shard 3   761 passed   0 failed   13m39s
  ------------------------------------------
  TOTAL    3264 passed   0 failed

  Of which 304 are new (tests/test_hosted_auth.py 273, tests/test_hosted_auth_templates.py 31).
  Sharding is a local wall-clock measure only; the file set is the complete
  tests/ directory with no exclusions. CI runs its own 8-shard split.

ruff check .            All checks passed!
ruff format --check .   494 files already formatted
python -m mypy app      Success: no issues found in 252 source files

alembic heads           0926b59b7912 (head)      <- single head, unchanged from base
alembic upgrade head    applied cleanly
alembic check           No new upgrade operations detected.
alembic downgrade base && alembic upgrade head   round trip OK

git diff --check        no whitespace errors

Extension node suite    not run: no file under extensions/ was changed
                        (`git diff --stat cb8510a -- extensions/` is empty)
```

Suites named in the request, all inside the totals above and all green:

| Suite | Result |
|---|---|
| `tests/test_hosted_auth.py`, `tests/test_hosted_auth_templates.py` (new) | 304 passed |
| `tests/test_v2_customer_ui.py` (customer UI) | passed |
| `tests/test_workbench_web.py`, `tests/test_admin_workbench_web.py` | passed |
| `tests/test_phase2_api.py`, `tests/test_api.py`, `tests/test_campaigns.py` | passed |
| `tests/test_contact_capture_intake.py`, `tests/test_salesnav_intake.py`, `tests/test_linkedin_*_intake.py` | passed |
| `tests/test_production_hardening.py` | 61 passed |
| `tests/test_migrations.py` | passed |`

Non-vacuousness was checked by mutation, not assumed:

* neutering `require_csrf` → 5 tests fail
* neutering the Jinja form injection → 11 tests fail

---

## 9. What remains manual

* Creating the Google OAuth client and pasting the two values (§6).
* Writing `/etc/vmr/vmr.env` on the server (§5) — **in the release window**.
* Deciding whether to open nginx `location /` to the Internet.
* Adding or removing an operator: edit the allow-list, restart `vmr-web`.
* Push, PR, review, merge — per the operating model, none of which I do.

---

## 10. Bundle

```
File:    vmr-outbound-hosted-auth-slice.bundle
Base:    cb8510a73c872f67514dc0557708c30a20dc64d2   (required ref; incremental bundle)
Head:    refs/heads/feat/hosted-operator-auth
```

The branch-tip SHA and the bundle SHA-256 are in the delivery message. They are
deliberately not written here: this document is inside the commit they identify,
so any value printed here would be stale the moment it was committed. To confirm
the pair yourself:

```
git bundle verify vmr-outbound-hosted-auth-slice.bundle
certutil -hashfile vmr-outbound-hosted-auth-slice.bundle SHA256
```

`git bundle verify` and `git merge-base` output are in §11 of the delivery
message.

---

## Verdict

`**HOSTED AUTH SLICE READY FOR INDEPENDENT REVIEW**`

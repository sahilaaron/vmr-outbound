# Decision 0011 — Hosted-operator authentication for the internal Beta

Status: Accepted

Date: 2026-08-10

Supersedes the temporary rule recorded in `app/main.py` that
`FEATURES__WORKBENCH` was permitted only when `APP_ENV=local`.

## Context

Decision 0010 committed the delivery cycle to a hosted manual-copy Beta on
`https://srv1885453.hstgr.cloud`, with the Chrome Extension capturing real
prospects into an *authenticated* staging application. The VPS infrastructure
smoke has passed. Nothing about authentication existed yet.

Issue #247 framed the remaining work as securing `POST /api/campaigns` and
`POST /campaigns` before exposing the VPS. A read of `main` at `cb8510a` showed
the real surface is materially larger:

* **119 state-changing routes**, none of them authenticated — 94 across the four
  web routers and 25 across `app/api` (19 of those in `phase2.py` alone, all
  public in OpenAPI).
* The only guards that existed were **environmental, not identity-based**:
  `FEATURES__WORKBENCH` + `APP_ENV=local`, the `_LOCAL_ONLY_FEATURES` refusals,
  and `_origin_allowed()` / `_same_origin()` — the first of which returns `True`
  for a missing `Origin`, and the second of which deliberately allows
  `Origin: null` and covers 4 of 94 web writes.
* **No CSRF token anywhere**, across 111 POST forms in 47 templates and four
  independent Jinja environments.

Patching the two endpoints named in the issue would have left the other 117 open
and would have set the precedent that each new route needs its own guard.

## Decision

Build **one central, default-deny hosted authentication and CSRF boundary**, and
replace the workbench environment rule with a startup contract.

1. **Default-deny before routing.** A single pure-ASGI middleware, mounted inside
   the trusted-host check and inside the production hardening boundary, decides
   access for every request against an explicit anonymous allow-list (health
   probes, `/auth/*`, `/static/*`). Everything else — including `/docs`,
   `/redoc`, `/openapi.json` and paths that do not exist — requires an approved
   operator. Paths are normalised before matching, and normalisation can only
   ever make a path *more* protected.

2. **Google identity, allow-list authorization.** Authorization-code flow with
   PKCE, identity scopes only (`openid email profile`), full verification:
   RS256 signature against Google's JWKS with a single accepted algorithm and
   exact `kid` selection, plus `iss`, `aud`, `nonce`, `exp`/`iat` and
   `email_verified`. Authorization is a configured allow-list of addresses, not
   a Google domain, and is re-checked on every request.

3. **Signed stateless session cookie**, `HttpOnly` / `Secure` / `SameSite=Lax`,
   12-hour absolute expiry, no sliding renewal, new session identifier on every
   sign-in.

4. **Two-layer CSRF**: an origin backstop in the middleware that no route can
   forget, plus a per-session token compared in constant time. The token is
   emitted into every POST form by a Jinja **compile-time** extension rather than
   by editing 111 forms, and enforcement is declared once per router.

5. **Startup contract.** Staging *must* have a complete boundary or the process
   refuses to start; production refuses the workbench outright; local development
   is untouched.

## Alternatives considered

**Guard the two endpoints in #247.** Rejected: leaves 117 unguarded writes and
makes every future route a new opportunity to forget.

**Per-route dependencies on all 119 routes.** Rejected: the security of the
system would rest on nobody ever omitting one, and the omission would be silent.

**A database-backed session table.** Rejected for this Beta. Revocation is
already immediate through the allow-list re-check; a session table would add a
weaker second revocation path, put the boundary behind the database, and create a
second copy of operator identity at rest. The cost — a *stolen* cookie stays
valid until expiry — is accepted, mitigated (`HttpOnly`, `Secure`, bounded
non-renewable lifetime) and reversible by rotating the signing secret.

**Trusting the ID token without verifying its signature.** OpenID Connect Core
§3.1.3.7 permits it for the code flow, since the token arrives over a direct
authenticated TLS channel. Rejected anyway: it makes the security of sign-in
depend on a property that is invisible in the code and silently lost the moment
anyone accepts an assertion from anywhere else. Verification makes the guarantee
local, explicit and testable.

**Adding an OAuth/JWT library.** `cryptography` — the primitive those libraries
wrap — is already a pinned dependency, and the verification surface here is one
algorithm and one key source. Implementing it directly avoided a new supply-chain
dependency and a `constraints.txt` regeneration that could not be validated
against the real deploy path from this environment. This was **not** a decision
to minimise dependency count for its own sake, and it is cheaply reversible:
swapping in PyJWT would touch `app/core/auth/jwks.py` only. The trade accepted is
that the verification code is ours to get right, which is why it is written
against named attacks (algorithm confusion, key confusion, self-nominated keys,
unknown-`kid` flooding, size exhaustion) and covered by tests that mint real
forged tokens.

**Roles for `/admin` versus `/app`.** Rejected. Both surfaces have identical
access semantics today and the same two or three people use both. One role is
added the day a real distinction exists.

## Consequences

* A staging deployment with no `AUTH__*` configuration **will refuse to start**.
  This is intended, and it makes `/etc/vmr/vmr.env` part of the same maintenance
  window as the release.
* `FEATURES__WORKBENCH=true` becomes legal in staging for the first time, which
  is what unblocks the Beta.
* Local development is unchanged: authentication defaults off, forms render
  byte-identically, and no developer signs in to use localhost.
* `WorkbenchConfigurationError` survives as an alias so an external caller
  catching the old name still catches the refusal.
* The next slice — authenticated remote Chrome Extension capture — gets a clean
  seam: the middleware already records which credential authenticated a request,
  and the CSRF dependency already skips non-cookie credentials.
* Per-operator write attribution is now possible for the first time and is
  deliberately deferred to `docs/POST_LAUNCH_BACKLOG.md`.

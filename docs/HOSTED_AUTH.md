# Hosted-operator authentication

How an approved internal VMR operator reaches `/app` and `/admin` on a hosted
deployment, and how everyone else is refused.

This document covers the browser sign-in boundary only. Gmail mailbox
authorization, Gmail drafts, sending, reply ingestion, Sheets and the remote
Chrome Extension capture path are **not** part of it and are not implemented.

---

## 1. What the boundary actually is

One default-deny decision, applied before routing, to every request:

```
nginx  ->  ProductionHTTPMiddleware      (request id, security headers, size ceiling)
       ->  CanonicalTrustedHostMiddleware (Host allow-list)
       ->  OperatorAuthenticationMiddleware   <-- the boundary
       ->  routing -> require_csrf dependency -> handler
```

The middleware answers one question: *is this caller an approved internal VMR
operator?* If not, the only paths that still resolve are the ones in the explicit
anonymous allow-list. Everything else — `/app`, `/admin`, every API, `/docs`,
`/redoc`, `/openapi.json`, and any route that does not exist yet — is refused.

That direction matters more than any individual rule. A router added next month
is protected the moment it is mounted; nobody has to remember to guard it.

### The anonymous allow-list

| Path | Why |
|---|---|
| `/healthz`, `/health`, `/readyz`, `/ready`, `/version` | The deploy gate has to reach these before any human signs in. nginx restricts them separately at the network edge. |
| `/auth/login`, `/auth/google/start`, `/auth/callback`, `/auth/logout`, `/auth/signed-out` | The sign-in surface itself. It is the only way in, so it cannot be behind the thing it is the way into. |
| `/static/<asset>` | The `StaticFiles` mount: compiled CSS, one SVG mark and two progressive-enhancement scripts. No operator data. |

The five `/auth` paths are enumerated exactly, and `/static/` is the one mount
exception. Neither is a prefix rule. A prefix would grant anonymity to routes
that do not exist yet — `app.include_router(x, prefix="/auth")` would become
publicly reachable with every gate green — and it would also leak, because an
unmounted path under an anonymous prefix answers `404` while every other unknown
path answers `401`. Bare `/auth` and bare `/static` are protected like any other
non-asset path. `tests/test_hosted_auth_templates.py` fails if the anonymous set
and the live router table ever disagree.

**`OPTIONS` is not anonymous.** An anonymous `OPTIONS` is refused with `401` on
every protected and unmounted path, exactly like an anonymous `GET`. `OPTIONS`
is a *safe* method — the cross-site backstop does not apply to it — but safe is
not the same as anonymous. The `@router.options` handlers in `app/api/routes.py`
exist for the capture extension's CORS preflight; the extension is itself
refused once hosted authentication is on, because its `POST` intake becomes a
`401` like any other anonymous caller. Exempting the preflight alone would open
an anonymous surface for a client that still could not complete a request, so it
is not exempted. The narrow preflight exemption a future authenticated
cross-origin client needs is recorded in `docs/POST_LAUNCH_BACKLOG.md` and will
be designed and tested together with extension authentication.

### Refusal shapes

| Caller | Response |
|---|---|
| Browser navigation (`GET`/`HEAD`, `Accept: text/html`, `Sec-Fetch-Mode: navigate`) | `303` to `/auth/login?next=<original path>` |
| Anything else, including every write | `401 {"error": "unauthorized", ...}` |

A write is **never** answered with a redirect. A `303` on a `POST` is followed as
a `GET` and can look like success to a client that cannot see the address bar.

---

## 2. Identity

Google Sign-In, authorization-code flow with PKCE, used **only** to establish who
the person is.

* Scopes requested: `openid email profile`. Nothing else, ever.
* No Gmail scope is requested and no Gmail token is stored.
* Signing in to VMR does not imply mailbox authorization. When mailbox access is
  built, it will be a separate grant with a separate client and a separate
  consent screen.

### What is proven before anyone is signed in

Every one of these must pass. None is optional and none is inferred.

| Check | Where |
|---|---|
| `state` matches the signed, single-use transaction cookie | `app/web/auth_routes.py` |
| PKCE `code_verifier` binds the code to this process | `app/web/auth_routes.py` |
| RS256 **signature** against Google's published JWKS, key chosen by exact `kid` | `app/core/auth/jwks.py` |
| `alg` is exactly `RS256` — no `none`, no HMAC, no algorithm table | `app/core/auth/jwks.py` |
| The token may not nominate its own key (`jwk`/`jku`/`x5u`/`x5c` refused) | `app/core/auth/jwks.py` |
| `iss` is a documented Google issuer | `app/core/auth/identity.py` |
| `aud` equals this deployment's client id (constant-time) | `app/core/auth/identity.py` |
| `nonce` equals the nonce minted for *this* browser's sign-in | `app/core/auth/identity.py` |
| `exp` / `iat` freshness with a 60-second symmetric leeway | `app/core/auth/identity.py` |
| `email_verified` is explicitly true | `app/core/auth/identity.py` |
| the address is on the approved-operator list | `app/core/auth/config.py` |

No email, name or identifier is ever read from a request parameter, a form field
or a header. Identity comes from the verified assertion only.

### The approved-operator list

Access is an explicit allow-list of addresses in configuration
(`AUTH__ALLOWED_OPERATOR_EMAILS`), not "anyone in the Google domain".

* Empty means **nobody**. Every decision path treats an empty list as a refusal,
  and a hosted deployment refuses to start with one.
* Comparison is NFKC-normalised, lower-cased and whitespace-stripped.
* Gmail's dot-insensitivity and `+tag` stripping are deliberately **not** applied.
  Folding them would make `a.b@x` match an allow-list entry of `ab@x` — a
  widening of access nobody configured.
* `AUTH__ALLOWED_GOOGLE_DOMAIN` is an optional *additional* gate, never a
  replacement for the list.

Approval is re-checked on **every request**, not only at sign-in. Removing an
address and restarting therefore revokes that operator's live session on their
next request.

### Roles

There are none. `/app` and `/admin` are two views of one internal application
for the same two or three people, and today they have identical access semantics
(namely: none). Inventing a role model to express a distinction that does not
exist would be complexity with no present decision behind it. If `/admin` ever
needs to exclude someone who may use `/app`, that is the moment to add exactly
one role, and not before.

---

## 3. Sessions

A signed, stateless cookie. `HttpOnly`, `Secure`, `SameSite=Lax`, host-only by
default, absolute 12-hour expiry with no sliding renewal.

**Why not a database session table.** Revocation is already strong without one
(see above); authentication must not depend on the database, or a readiness blip
locks operators out of the screens they would use to diagnose it; and there is no
second copy of an operator's identity at rest to leak, migrate or clean up.

**The accepted cost, stated plainly.** A cookie that is *stolen* stays valid
until its absolute expiry or until the allow-list changes. Mitigations in place:
`HttpOnly` removes the XSS-to-theft path, `Secure` + HSTS removes the plaintext
path, the lifetime is bounded and non-renewable, and the population is a handful
of named people. If a token is known to be stolen, rotating
`AUTH__SESSION_SECRET` invalidates every live session immediately.

Signing keys for the session, the CSRF token and the sign-in transaction are
derived from one secret with three distinct labels, so a token minted for one
purpose can never be presented as another.

---

## 4. CSRF

Two independent layers. A cross-site write has to defeat both.

**Layer 1 — origin backstop, in the middleware.** Any unsafe method carrying a
positive cross-site signal is refused: a `Sec-Fetch-Site` that is not
`same-origin`/`none`, or an `Origin` naming somewhere other than this site. A
duplicated copy of either header is ambiguity and refuses too. This layer needs
no cooperation from a route or a template.

An **opaque `Origin`** — `null` or empty — is the one signal not read literally,
because on this deployment it is not evidence of anything. The hardening
boundary sends `Referrer-Policy: no-referrer`, and under that policy the Fetch
standard serialises `Origin` as `null` on every non-GET/HEAD, non-CORS request,
including a perfectly ordinary same-origin form post. Reading it as hostile
refused every write in the hosted UI and was found on the sign-out button during
Beta UAT (#264).

`Sec-Fetch-Site` decides that case, and it is the signal an attacker cannot
supply: it is a forbidden header name, so no page script may set, clear or alter
it, and the browser computes it from the request's real initiator rather than
from the referrer policy. An opaque `Origin` is therefore accepted **only**
alongside a single, positive `Sec-Fetch-Site: same-origin`; absent metadata,
`none`, `same-site`, `cross-site` and any duplicate all still refuse. A
cross-site post cannot reach that combination from a browser, and a non-browser
client that writes both headers by hand still has to satisfy layer 2.

**Layer 2 — a per-session token.** Derived from the session identifier with a
dedicated key, compared with `hmac.compare_digest`, required on every
cookie-authenticated unsafe request, accepted from the `_csrf` form field or the
`X-CSRF-Token` header.

Absent fetch metadata is not treated as a pass: it falls through to layer 2,
which fails closed on its own. That is what keeps a scripted client with a valid
token working while a real browser — which always sends `Origin` on a cross-site
form post — is stopped at layer 1 before the body is read.

### How the token reaches 111 forms

It is not hand-written anywhere. `CsrfFormExtension`
(`app/core/auth/templating.py`) rewrites template *source at compile time*,
inserting `{{ csrf_field() }}` after every opening POST `<form>` tag. A template
added later is covered the moment it is compiled. Enforcement is declared once
per router as `dependencies=[Depends(require_csrf)]`.

Two conformance tests keep both declarations honest: one asserts every POST form
in every template receives a token, the other asserts every router owning a
state-changing route declares the dependency.

`POST /auth/logout` is the single deliberate exemption from the router
dependency: it checks the token inside the handler, because a caller whose
session has already expired must still land on the signed-out page rather than a
403.

### The pre-existing `_same_origin()` and `_origin_allowed()`

Both survive untouched and are now redundant defence rather than the control.
`_same_origin()` in `app/web/v2/routes.py` allowed `Origin: null` and missing
headers, and covered 4 of 94 web writes; `_origin_allowed()` in
`app/api/routes.py` returns `True` for a missing `Origin`, so any non-browser
client passed it. Neither is authentication and neither was ever sufficient as
CSRF protection. Both now sit *behind* the boundary above.

---

## 5. The startup contract

This replaces the old rule "`FEATURES__WORKBENCH` is only permitted when
`APP_ENV=local`". That rule guarded the UI and said nothing about the 25
state-changing API routes that mount in every environment.

| Environment | Contract |
|---|---|
| `local` / `development` / `test` / `ci` | Unchanged. Auth defaults off; nobody is pushed through Google to use localhost. Turning it on locally applies the same completeness checks. |
| `staging` | `AUTH__ENABLED` is **mandatory** — because the application is reachable from the Internet, not because the workbench is on. Session secret, non-empty allow-list, complete Google client, HTTPS public origin and secure cookies are all required. `FEATURES__WORKBENCH` is then permitted. |
| `production` | `FEATURES__WORKBENCH` is refused outright: the production access policy is not decided, so it fails closed. Authentication is still mandatory for the API. |

Every problem is reported at once, so first-time setup takes one restart rather
than four. No value is ever echoed in the message.

> **Deployment ordering.** A staging box that has no `AUTH__*` configuration will
> now **refuse to start**. That is the intended behaviour and the reason this
> slice lands before the next deploy — but it means `/etc/vmr/vmr.env` must be
> updated in the same maintenance window as the release.

---

## 6. Google Cloud Console setup

Create an **OAuth 2.0 Client ID** of type **Web application** under
APIs & Services → Credentials.

| Field | Value |
|---|---|
| Authorised JavaScript origin | `https://srv1885453.hstgr.cloud` |
| Authorised redirect URI | `https://srv1885453.hstgr.cloud/auth/callback` |

Both must match byte for byte — no trailing slash on the origin, no trailing
slash on the redirect URI. The redirect URI is built from
`AUTH__PUBLIC_BASE_URL`, never from the `Host` header.

On the OAuth consent screen choose **Internal** if the Google Workspace allows
it. Scopes: `openid`, `email`, `profile` only. Do **not** add a Gmail scope to
this client; when mailbox authorization is built it gets its own client.

---

## 7. Operating it

**Adding an operator.** Add the address to `AUTH__ALLOWED_OPERATOR_EMAILS` in
`/etc/vmr/vmr.env`, `systemctl restart vmr-web`. There is no screen and no table.

**Removing an operator.** Remove the address and restart. Their live session
stops working on their next request.

**Rotating the signing secret.** Replace `AUTH__SESSION_SECRET` and restart.
Every operator signs in again. Do this if a cookie is known to be stolen.

**nginx.** `deploy/nginx/vmr-access.conf` ships as `deny all;`. With this
boundary in place, `location /` may be opened to the Internet so operators can
reach the sign-in page — that is an explicit decision for the maintainer, not
something this slice changes. The probe snippet stays as it is: `/version` still
leaks the running SHA and has no reason to be public.

---

## 8. What this slice deliberately does not do

* No Gmail OAuth, drafts, sending, reply ingestion or Sheets.
* No remote Chrome Extension capture. The extension's future credential is a
  **bearer token on the API**, kept architecturally separate from this browser
  cookie: the middleware records `auth_credential` and the CSRF dependency skips
  any credential that is not the cookie, because a bearer token is not attached
  by the browser automatically. Nothing issues one today, so an extension-origin
  request is refused exactly like any other anonymous caller.
* No password signup, customer tenancy, billing or broad RBAC.
* No per-operator audit trail. Writes still record the constant `OPERATOR_ACTOR`.
  Now that a verified identity exists on every request, attributing writes to it
  is a genuinely useful next slice — logged in
  `docs/POST_LAUNCH_BACKLOG.md`, not implemented here.

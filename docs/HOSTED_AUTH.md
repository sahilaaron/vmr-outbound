# Hosted-operator authentication

How an approved internal VMR operator reaches `/app` and `/admin` on a hosted
deployment, and how everyone else is refused.

This document covers the browser sign-in boundary only. Sending, reply
ingestion and Sheets are **not** part of it and are not implemented.

Gmail mailbox authorization is also not part of it, and that separation is the
point rather than an omission. #267 added a Gmail *draft* grant with its own
OAuth client, its own consent screen, its own secret and its own routes under
`/gmail/*` — none of which is on the anonymous allow-list below. Signing in to
VMR still requests `openid email profile` and nothing else, and no configuration
change here can turn a sign-in into mailbox access. See
[`GMAIL_DRAFTS.md`](GMAIL_DRAFTS.md).

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

#### What `next` may carry

`next` is the **whole** original target — path *and* query string — and
`safe_next_path` (`app/core/auth/policy.py`) decides whether it survives. Every
rule that keeps it a single-slash-rooted local path applies to the entire value.

The encoded-separator rule (`%2f`, `%5c`) applies to the **path only**, and that
distinction is load-bearing rather than cosmetic. `GET /extension/authorize`
carries `redirect_uri=https%3A%2F%2F<extension id>.chromiumapp.org%2F`, which a
browser must percent-encode. Applying the rule to the whole value discarded that
destination, so an operator signing in inside
`chrome.identity.launchWebAuthFlow` landed on the dashboard instead of back at
the authorization they were completing — and because the auth window then never
reached `https://<id>.chromiumapp.org/`, the flow ended only when they closed
it. An encoded separator after the `?` is query data and cannot change which
origin a root-relative path resolves against; in the path it still can, so it is
still refused there. See #280.

---

## 2. Identity

Google Sign-In, authorization-code flow with PKCE, used **only** to establish who
the person is.

* Scopes requested: `openid email profile`. Nothing else, ever.
* No Gmail scope is requested here and no Gmail token is stored by this path.
* Signing in to VMR does not imply mailbox authorization. Mailbox access is a
  separate grant with a separate client, a separate secret and a separate
  consent screen, requested only from an explicit Connect Gmail click — see
  [`GMAIL_DRAFTS.md`](GMAIL_DRAFTS.md).

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
| the address resolves to an active account in the `users` table | `app/core/auth/accounts.py` |

No email, name or identifier is ever read from a request parameter, a form field
or a header. Identity comes from the verified assertion only.

### Who may sign in — the account directory

**Changed by issue #270.** Access used to be an allow-list of addresses in
configuration. It is now a row in the `users` table.

> **Google proves identity. The VMR account record grants access.**

A fully valid, fully verified Google assertion for an address with no active
account is refused, and creates nothing. So is a password login for one. There is
no public signup: an account exists because an administrator created it at
`/app/admin/users`.

What happened to `AUTH__ALLOWED_OPERATOR_EMAILS`:

* It is now a **one-time seed**, not a gate. On each start, every address listed
  there that has no account gets one created — role `USER`, active, no password —
  so a deployment that worked the day before the accounts migration works the day
  after with nobody locked out.
* It may now legitimately be **empty**, and the startup contract no longer refuses
  an empty one. What it refuses instead is an empty `AUTH__BOOTSTRAP_ADMIN_EMAIL`.
* Removing an address from it **no longer revokes anything**. Disable the account
  instead — see *Sessions* below, which is stronger: it takes effect on the next
  request with no restart.

Address comparison is unchanged and is the same function everywhere — the typed
address, the Google claim, the seed list and the stored column all go through
`normalize_operator_email`: ASCII-only, lower-cased, whitespace-stripped, with
Gmail's dot-insensitivity and `+tag` folding deliberately **not** applied.
`AUTH__ALLOWED_GOOGLE_DOMAIN` remains an optional additional gate on the seed
decision.

### Linking a Google account to a VMR account

1. **By `sub`, when already linked.** Google's subject is stable across a
   Workspace address rename, so an account linked under an old address keeps
   working under the new one.
2. **By address otherwise**, and the `sub` is recorded on that first successful
   sign-in. From then on rule 1 applies.

Two shapes are refused rather than resolved, because each would create a second
identity for one person: a `sub` already linked to a *different* account than the
address resolves to, and a *new* `sub` presented for an address that already
carries a different one (an address reissued to somebody else).

Both login paths therefore land on the same row, and no combination of them
produces two accounts for one person — the unique index on `email_normalized`
makes that a database fact rather than an application convention.

### Roles

Two: `ADMIN` and `USER`, on the account record.

`ADMIN` is the only role that may reach `/app/admin/users`, and it is granted in
exactly one place — the configured `AUTH__BOOTSTRAP_ADMIN_EMAIL`
(`sahil@verifiedmarketresearch.com` by default), applied idempotently at startup.
It is never inferred from an email domain: "works at Verified Market Research" and
"may create accounts" are different facts, and conflating them would make every
colleague an administrator the moment their account existed.

The role is read from the account record on **every request** and put in the
request scope; the admin dependency reads it from there. A demotion therefore
applies to the next request, and a session cookie cannot assert a privilege the
directory no longer grants. Hiding the menu entry is a courtesy — typing the URL
gets a 403.

### Passwords

Argon2id via `argon2-cffi`, at OWASP's minimum configuration (19 MiB, t=2, p=1).
Only the PHC hash is stored; there is no column, log line, template or API
response through which it is readable. `app/core/auth/passwords.py` records why
Argon2id rather than bcrypt (72-byte truncation collides with the 64+ character
requirement) or PBKDF2 (not memory-hard).

Policy: minimum 8 characters, at least 64 accepted, every character permitted
including spaces, no composition rules, no expiry, and a bounded blocklist of
obviously common values. The forms support autofill and paste and carry the
`autocomplete` tokens password managers key on.

Login refusals — unknown address, wrong password, disabled account, password
never set — produce one message, one status and the same Argon2id work, so
neither the text nor a stopwatch distinguishes them. Failed attempts are
rate-limited per address and per client in a fixed window; there is deliberately
**no lockout**, because a lockout an attacker can trigger is a way to keep a named
colleague out of the application indefinitely.

### First-login password setup

Admin-created accounts begin with **no usable password**, and no temporary
password is ever generated. Creating an account mints one cryptographically
secure link; only its SHA-256 digest is stored; it lasts 24 hours; issuing a new
one supersedes every earlier one. The raw link is rendered to the administrator
**once**, on the response to the action that created it, and is dropped from the
process — it is never stored, logged, put in an audit payload, or shown again.

Setting a password consumes the link permanently, bumps the account's
`auth_version`, and **does not sign the person in**: they go to the sign-in form
and use the credential, which is what proves it. Replayed, expired, superseded and
disabled-account links all fail, with one message for all four. A password that
fails the policy does *not* burn the link.

Password reset is the same mechanism under a different name: an administrator
issues a new link. No existing password is ever revealed, because there is nothing
to reveal.

---

## 3. Sessions

A signed cookie. `HttpOnly`, `Secure`, `SameSite=Lax`, host-only by default,
absolute 12-hour expiry with no sliding renewal.

**Revocation (#270).** The cookie carries the account's `user_id` and the
`auth_version` it was minted under. Every authenticated request resolves that
account and compares the counter, so:

* **disabling an account refuses its existing sessions on the next request** — no
  restart, no waiting for expiry;
* **a password change or reset invalidates every earlier session**;
* **reactivating does not resurrect** the sessions that disabling revoked, because
  reactivation bumps the counter too;
* revoking everything for one account is one `UPDATE` on one row.

A cookie minted before this slice carries neither claim, fails to decode, and its
holder signs in again — a one-time cost, in the safe direction.

**Why still no session table.** The version counter does the job a table would,
without a second copy of each operator's identity at rest and without a cleanup
job to forget about.

**What this costs, stated plainly.** One indexed primary-key lookup per
authenticated request, and authentication now depends on the database. Three
things bound that: probes, the whole sign-in surface and the `/static/` mount are
decided without a lookup, so a deployment whose database is down still answers
`/readyz`, still renders its sign-in page and still serves its stylesheet; a
lookup that *cannot be answered* returns 503 and leaves the session cookie in
place, so the browser is signed in again the moment the database is (an outage is
never mistaken for a mass sign-out); and the directory is a seam
(`AccountDirectory`), so the boundary is testable without a database.

**The accepted cost of a stolen cookie, stated plainly.** It stays valid until
its absolute expiry, until the account is disabled, or until its password is
reset. Mitigations in place:
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
| `staging` | `AUTH__ENABLED` is **mandatory** — because the application is reachable from the Internet, not because the workbench is on. A session secret, a **non-empty `AUTH__BOOTSTRAP_ADMIN_EMAIL`**, a complete Google client, an HTTPS public origin and secure cookies are all required. `FEATURES__WORKBENCH` is then permitted. |
| `production` | `FEATURES__WORKBENCH` is refused outright: the production access policy is not decided, so it fails closed. Authentication is still mandatory for the API. |

Every problem is reported at once, so first-time setup takes one restart rather
than four. No value is ever echoed in the message.

> **`AUTH__ALLOWED_OPERATOR_EMAILS` is not on that list, and must not be treated
> as though it were.** The rule that required a non-empty allow-list was replaced
> by the bootstrap-administrator rule when the authority moved to the `users`
> table (`app/core/auth/startup.py`), and an empty `'[]'` is now correct on a
> fresh deployment. Filling it in to satisfy a startup requirement that no longer
> exists is actively harmful: the field validator refuses anything that is not a
> well-formed ASCII address, so a placeholder like
> `["REPLACE_WITH_OPERATOR_EMAIL"]` makes the process refuse to start — and under
> `vmr-deploy` that surfaces at `alembic upgrade head`, *after* the database
> backup and before the symlink moves.

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
this client. Mailbox authorization has its own client, its own consent screen
and its own secret — see [`GMAIL_DRAFTS.md`](GMAIL_DRAFTS.md) §8.

---

## 7. Operating it

**The `users` table is the authority.** Everything below follows from that. An
account row grants access; configuration does not. `AUTH__ALLOWED_OPERATOR_EMAILS`
is legacy bootstrap-seeding compatibility only — it creates rows on a first start
after the accounts migration (`seed_from_allowlist`, which only ever *creates*)
and is read nowhere on the request path. `is_approved()` survives in
`app/core/auth/config.py` for that seeding decision and has no call site in the
application.

**Adding an operator.** An administrator creates the account at
`/app/admin/users` and hands over the one-time password link it returns. No
restart, no environment file, no editing `AUTH__ALLOWED_OPERATOR_EMAILS` — an
address added there is seeded only in the narrow migration case described above
and is not how operators are added.

**Removing an operator — this is the part that used to be documented wrongly.**
Removing the address from `AUTH__ALLOWED_OPERATOR_EMAILS` and restarting
**revokes nothing**: it is not consulted when a request is authorised, and the
account row it once created is still there and still active.

The revocation action is **disabling the account** at `/app/admin/users`. That
bumps the row's `auth_version`, and because every authenticated request resolves
the account and compares that counter against the cookie's, the person's existing
session is refused on their **next request** — with **no restart**, and without
waiting for the 12-hour expiry. See *Sessions* above. The row is kept; accounts
are never deleted.

**Rotating the signing secret.** Replace `AUTH__SESSION_SECRET` and restart.
Every operator signs in again. That is the blunt instrument for "a cookie is
known to be stolen and I do not know whose"; to revoke one named person, disable
their account instead.

**nginx.** `deploy/nginx/vmr-access.conf` ships as `deny all;`. With this
boundary in place, `location /` may be opened to the Internet so operators can
reach the sign-in page — that is an explicit decision for the maintainer, not
something this slice changes. The probe snippet stays as it is: `/version` still
leaks the running SHA and has no reason to be public.

---

## 7a. The Chrome capture extension credential

The operator session cookie above is for a **human at a browser**. The Chrome
capture extension is not one: it has no sign-in surface, cannot complete an
OAuth redirect, and must never be handed the operator's cookie — a cookie is
ambient, carries the operator's identity, and unlocks the whole application.

It therefore has its own credential, and this deployment now holds three that
are kept apart on purpose. None substitutes for another:

| Credential | Who holds it | What it unlocks |
| --- | --- | --- |
| Operator session cookie (`AUTH__*`) | a signed-in human | every operator surface |
| Google identity client (`AUTH__GOOGLE_*`) | the server, at sign-in only | learning who that human is |
| Extension capture credential (`EXTENSION_AUTH__*`) | one extension install | the capture intake contract, nothing else |
| Gmail mailbox grant (`GMAIL__*`) | one operator, per connected mailbox | creating a draft in that mailbox, nothing else |

The Gmail grant (#267) is the fourth, and it is not this one. It is stored
encrypted, bound to one operator identity, and reaches only
`users.drafts.create` and a bounded `users.drafts.list` lookup.

### The contract, enumerated

`app/core/auth/extension.py` holds the whole authorisation surface as a table.
A credential authorises these and refuses everything else in the application:

| Method | Path | Why the extension needs it |
| --- | --- | --- |
| `POST` | `/api/intake/contact-captures` | the capture itself |
| `GET` | `/api/contact-labels` | offer existing labels before saving |
| `GET` | `/api/contacts/lookup` | label the button Save or Refresh |
| `GET` | `/api/campaigns` | the optional Campaign filing selector |

Deliberately absent: the legacy campaign-era intakes (the extension has not
produced those contracts since 2.0), the company-page intake (a separate
surface, still local-only), and every other write in the application. A valid
credential on `/admin`, on `POST /api/campaigns`, or on `DELETE` of the capture
path is worth exactly as much as no credential at all — pinned by tests.

### Shape, verification and storage

Presented as `Authorization: Bearer vmrx1.<key_id>.<secret>`.

`key_id` is a short non-secret label — the only part that may appear in a log
line, and what revocation names. `secret` is 32+ characters of
`secrets.token_urlsafe`.

**The server never stores the secret.** `EXTENSION_AUTH__CREDENTIALS` carries
`<key_id>:<sha256-hex-of-secret>`; verification hashes what was presented and
compares digests in constant time. A plain SHA-256 is right here and a password
KDF would not be: the input is full-entropy random, so there is no dictionary to
slow down, and the property that matters is that a reader of `/etc/vmr/vmr.env`,
a leaked backup, or a settings dump learns nothing replayable. The field also
carries `repr=False`/`exclude=True`, so even the digest stays out of dumps.

On the extension side the credential lives in `chrome.storage.session`: in
memory for the browser session, never written to disk, and unreadable from a
content script on a LinkedIn page. The cost is deliberate and stated in the
panel — Chrome clears it on restart and the operator pastes it again.

### Origin binding

A credential alone is not enough. The request must also come from an approved
`chrome-extension://` origin, matched exactly against
`EXTENSION_AUTH__ALLOWED_ORIGINS`. "Any extension origin" is not a boundary once
the application is on the Internet: every extension in the operator's browser
would qualify, including one installed tomorrow.

The rule differs by method class, and precisely:

* **The capture `POST` requires an approved `Origin`.** The Fetch standard
  appends `Origin` to every non-GET/HEAD request regardless of mode, so a real
  capture always carries one. This is what makes a *stolen* credential replayed
  from `https://evil.example` fail even though the credential itself verifies.
* **The three reads accept an absent `Origin`, but never a wrong one.** An
  extension holding a host permission may have its cross-origin GET treated as
  same-origin, and the standard then omits the header. A *present* origin is
  still checked, so the arbitrary-web-origin case is refused on every method.

### CORS and preflight

The minimum the extension needs, and nothing more:

* `Access-Control-Allow-Origin` reflects only an approved extension origin, on
  the enumerated paths only, and never on an error path where the value had not
  yet been checked.
* `Access-Control-Allow-Methods` is the contract's methods for that path.
* `Access-Control-Allow-Headers` is `Authorization, Content-Type,
  Idempotency-Key` — the headers the extension actually sends.
* **`Access-Control-Allow-Credentials` is never emitted.** The extension
  authenticates with a header it sets itself and needs no ambient cookie; a
  credentialed CORS grant would be a way to reach this API with the operator's
  session instead. There is no wildcard origin anywhere.
* `OPTIONS` is answered by the middleware — 204, no body, no authentication
  implication — only for an enumerated path, an approved origin, and a requested
  method inside that path's contract. This closes the M-2 preflight item that
  `docs/POST_LAUNCH_BACKLOG.md` deferred until extension authentication existed.

The rest of the application's CORS behaviour is unchanged.

### A session cookie is not an extension request

Stated as an acceptance rule because it is the one an implementation drifts on:
a signed-in operator's cookie does not turn a capture `POST` into an
authenticated extension request. The middleware records a key id only when a
credential actually verified, and the intake route requires that key id. A
cookie-only capture is a 403.

Conversely, a verified credential outranks an ambient cookie on the contract:
one credential decides one request, and an explicitly presented bearer is the
stronger signal. An extension is not an operator, so such a request carries no
operator email and no CSRF token — correctly, since a bearer credential is not
attached automatically by a browser and is therefore not forgeable cross-site.

### Operating it

**Issuing a credential.**

```bash
python scripts/mint_extension_credential.py --key-id beta-sahil-laptop
```

It prints two lines that go to two different places: the credential, pasted once
into the extension's Settings; and the digest entry, added to
`EXTENSION_AUTH__CREDENTIALS` in `/etc/vmr/vmr.env`. Then
`systemctl restart vmr-web`.

**Approving an install.** Open the extension's Settings — it shows its own
extension ID. Add `chrome-extension://<that-id>` to
`EXTENSION_AUTH__ALLOWED_ORIGINS` and restart.

**Revoking.** Two paths, both fail-closed and effective on restart:

1. Add the key id to `EXTENSION_AUTH__REVOKED_KEY_IDS` — the preferred one.
   Revocation is checked *before* the digest, so a revoked id stays dead even if
   a stale credential entry still carries it.
2. Remove the entry from `EXTENSION_AUTH__CREDENTIALS`.

The first is safer under pressure: deleting the right line out of a list is a
chance to delete the wrong one.

**Turning the whole thing off.** `EXTENSION_AUTH__ENABLED=false` and restart.
Every capture credential stops working immediately; the operator surfaces are
unaffected.

**Local development is unchanged, and this boundary is inert there.** The
middleware that reads the credential returns early when `AUTH__ENABLED` is
false, so setting `EXTENSION_AUTH__*` on a laptop enforces nothing: the intake
keeps its existing rule (`APP_ENV=local`, loopback or any `chrome-extension://`
origin, no credential). Staging cannot reach that inert combination — the
startup contract requires `AUTH__ENABLED` alongside it. The extension mirrors
this: it attaches a credential only to a request bound for a named hosted
deployment, never to a loopback one.

### What the startup contract refuses

Enabled with no credential, enabled with no approved origin, enabled without
`FEATURES__CONTACT_CAPTURE_INTAKE`, enabled without `AUTH__ENABLED` in a hosted
environment, or enabled in production at all. Each one would otherwise produce a
deployment that starts cleanly, serves every screen, and refuses every capture,
with nothing to say why.

`FEATURES__CONTACT_CAPTURE_INTAKE` used to be refused outright outside local
development, because the intake had no authentication and "local only" was the
entire boundary. It is now *credential-gated* instead: permitted hosted exactly
when this credential boundary is configured. The other intakes did not move and
remain local-only.

---

## 8. What this slice deliberately does not do

* No sending, reply ingestion or Sheets. (Gmail *draft* creation was added
  later by #267 as a separate grant with its own routes and its own client;
  it authorizes against the `User` record this slice introduced, and changed
  nothing about how that record is authenticated.)
* No public signup, customer tenancy, billing or broad RBAC beyond the two roles.
* No Microsoft/Entra OAuth. A Microsoft 365 colleague signs in with an email
  address and a password, which is exactly what the password path is for.
* No transactional email. Password links are handed to the administrator once and
  sent out of band; wiring an email provider is a later, separate decision.
* No per-operator audit trail on *domain* writes. Account changes are fully
  audited (section 9), but campaign and contact writes still record the constant
  `OPERATOR_ACTOR`. The `User` model is shaped so issue #269 can attribute them
  later without redoing authentication; it is logged in
  `docs/POST_LAUNCH_BACKLOG.md`, not implemented here.
* No change to the extension bearer credential, which is a separate boundary with
  separate configuration and is untouched by this slice.

---

## 9. Account administration and audit

`/app/admin/users`, administrator only, server-side enforced. An administrator
can list accounts, create one (email, optional display name; role is always
`USER`), disable, reactivate, grant or remove the administrator role, see
`created_at`, `last_login_at` and whether a password is set, and issue a new
one-time password link.

The screen never renders a password hash, and the service refuses to disable or
demote the last active administrator. Accounts are never deleted: disabling keeps
the row, its history and any future attribution while refusing every credential it
holds — including a link already sitting in somebody's inbox.

Every one of these is an `AuditEvent`: `user.created`, `user.disabled`,
`user.reactivated`, `user.role_changed`, `user.credential_link_issued`,
`user.password_setup_completed`, `user.password_reset_completed`,
`user.google_identity_linked`, `user.bootstrap_admin_ensured`,
`user.seeded_from_configuration_allowlist`. None of them carries a password, a
password hash, a raw or digested link, a session secret or an OAuth token.

### One deployment note about the reverse proxy

A password-setup link carries its one-time token in the query string. The
application access log records the matched **route template** and never the query
string, so no raw token reaches it. nginx's own `access_log` does log the full
request line — if that log is retained or shipped anywhere, either exclude
`/auth/setup` from it or accept that a 24-hour single-use token appears there.

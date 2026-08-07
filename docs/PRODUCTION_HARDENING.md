# Production Hardening

This document describes the application-layer protections that exist before a
staging deployment. It does not claim that authentication, sending, Gmail,
Google OAuth, Sheets, or a production deployment exists.

## Health and build identity

The unauthenticated system endpoints have deliberately small contracts:

| Endpoint | Success | Failure | What it proves |
| --- | --- | --- | --- |
| `GET /healthz` | `200 {"status":"ok"}` | Process/server failure | This web process can answer HTTP. It performs no database or provider call. |
| `GET /readyz` | `200`, status `ready` | `503`, status `not_ready` | Startup configuration passed and PostgreSQL answered one bounded `SELECT 1`. |
| `GET /version` | `200 {"version":"..."}` | — | The deployment-provided `RELEASE_ID`; `unknown` when unset. |

`/health` and `/ready` remain compatibility aliases, but return the same safe
contracts. Neither probe calls Claude, MillionVerifier, Gmail, Sheets, or any
external provider. A readiness failure returns only `database: failed`; it never
returns a DSN, host, SQL, exception type, driver text, filesystem path, or stack
trace.

Readiness uses its own one-connection pool rather than the application's session
pool. One process-local probe may run at a time. Pool contention, the overall
probe response, and SQL execution are each bounded by
`READINESS_TIMEOUT_SECONDS` (default 2 seconds); driver connection establishment
uses the lower of that value and `DATABASE_CONNECT_TIMEOUT_SECONDS` (default 5
seconds). Each probe invalidates its socket rather than reusing it. A final
daemon-thread deadline returns failure even if a driver violates its configured
timeout, while the one-probe permit prevents repeated requests from creating an
unbounded number of stuck DB operations.

### Worker limitation

The web process has no authoritative heartbeat from the separate Agent worker.
Queue rows and leases describe work, not whether a worker process is currently
alive. `/readyz` therefore makes no worker claim. A future worker-health feature
needs a dedicated authoritative mechanism; this layer does not create a table or
pretend that an idle queue proves health.

## Request IDs and logs

Every HTTP response carries `X-Request-ID`. A caller-provided value is accepted
only when it is 1–64 ASCII characters in the conservative set
`A-Z a-z 0-9 . _ : -`, begins with an alphanumeric character, and contains no
newline. Invalid, duplicate, or oversized values are replaced by a random
128-bit hexadecimal ID. The final value is available through request state and
the request context used by internal logs.

Caller-supplied request IDs are public, loggable metadata. Clients and operators
must never put credentials, tokens, email addresses, or other personal data in
them. The service cannot distinguish a grammar-valid secret-looking ID from an
ordinary deployment correlation ID.

The `vmr.http` logger emits one compact JSON object per request with:

- UTC timestamp;
- request ID;
- method;
- matched route template, such as `/items/{item_id}`;
- status code and duration in milliseconds;
- immediate peer, conservatively derived client address, scheme, and whether
  the immediate peer was a trusted proxy.

The request event never logs the query string, request/response body, uploaded spreadsheet,
email or personalization text, cookies, authorization values, tokens, or full
database URLs. Unmatched paths are logged as `/<unmatched>` so attacker-chosen
path content does not become log content.

An unhandled exception produces a generic JSON 500 with the request ID. Internal
logs retain correlated, bounded exception-class and stack-location metadata
(module, function, and line) without exception messages, locals, SQL/driver
text, or formatter-generated traceback text. Typed FastAPI/HTTP and domain
responses continue through their existing handlers.

## Host and reverse-proxy boundary

`TRUSTED_HOSTS` is the Host-header allow-list. Entries are canonicalized to
lowercase and one terminal DNS root dot is removed. Configured entries never
contain ports; an incoming valid port is ignored for matching. Bracketed IPv6
literals are parsed and matched explicitly. Local defaults are `localhost`,
`127.0.0.1`, `[::1]`, and `testserver`. Staging and production must supply their
real hostnames and cannot start with wildcard hosts. Duplicate or malformed Host
fields are rejected.

`X-Forwarded-For` and `X-Forwarded-Proto` are ignored unless the immediate TCP
peer belongs to `TRUSTED_PROXY_CIDRS`. Local defaults trust only loopback, which
fits Nginx on the same machine. A remote reverse proxy requires its exact private
network to be configured; `0.0.0.0/0` and `::/0` are refused in staging and
production.

For client attribution, the app walks the forwarded chain from the immediate
proxy towards the caller and stops at the first untrusted address. This is only
truthful when Nginx overwrites or correctly appends forwarding headers. The app
does not claim perfect client identity from headers alone.

Configure Uvicorn/Nginx so untrusted clients cannot connect directly to the app
port. Do not independently enable broad Uvicorn proxy-header trust; keep this
application allow-list and the server boundary aligned.

## Response protection

Central middleware applies:

- `X-Content-Type-Options: nosniff`;
- `Referrer-Policy: no-referrer`;
- `X-Frame-Options: DENY` and CSP `frame-ancestors 'none'`;
- a Permissions Policy denying camera, microphone, and geolocation;
- a same-origin Content Security Policy;
- `Cache-Control: no-store` on non-static responses;
- `Cache-Control: public, max-age=3600` on `/static/*`;
- HSTS only when the direct scheme is HTTPS or a trusted proxy supplies
  `X-Forwarded-Proto: https`.

The main CSP keeps scripts same-origin. It temporarily permits inline styles
because existing server-rendered templates use many `style=` attributes. That is
the exact blocker to removing `'unsafe-inline'` from `style-src`. FastAPI's local
documentation pages receive a separate policy for their current inline bootstrap
and jsDelivr assets; application pages do not inherit that exception.

## Startup refusal

`local`, `development`, `test`, and `ci` retain developer-friendly defaults.
`staging` and `production` refuse startup when a known-dangerous setting is
present, including:

- debug mode;
- `DRY_RUN=false` before a separately approved send-capable deployment;
- wildcard or local-only trusted hosts;
- an invalid or all-address trusted-proxy network;
- a malformed, non-PostgreSQL, loopback/container-service, development, or
  maintenance database URL;
- local-only intake/promotion features;
- the unauthenticated Workbench outside `APP_ENV=local` (the pre-existing hard
  guard remains).

Messages name the unsafe setting but never echo its value. There is no invented
session/auth secret requirement because the application does not yet have that
system.

## Request-size boundary

`MAX_REQUEST_BYTES` defaults to 25 MiB, matching the existing spreadsheet
upload ceiling, and cannot be configured below `MAX_UPLOAD_BYTES`. Middleware
rejects a valid declared `Content-Length` above the ceiling with 413 and rejects
malformed or duplicate values with 400 before a route reads the body. Existing
smaller intake-specific limits and the upload route's bounded streaming checks
remain authoritative within that global ceiling.

This application check cannot completely constrain an absent-length or chunked
request before Starlette/Uvicorn buffers or streams it. Complete body-size
enforcement belongs at the reverse proxy/server boundary. Set Nginx
`client_max_body_size` to the reviewed application ceiling and apply appropriate
header/time limits there as well.

## Diagnostic values

`app.core.diagnostics.serialize_diagnostic` provides a generic JSON-safe output
boundary with bounded strings, depth, collection output **and collection
iteration**, explicit truncation markers, HTML escaping, exception-message
withholding, URI-userinfo removal, hostile-key handling, and redaction for
secret-looking keys such as password, token, authorization, cookie, secret, API
key, DSN, and database URL. Small mapping output remains key-sorted; large or
arbitrary mappings consume only the first bounded window rather than scanning
the full input. It is infrastructure for future health/error/admin output and
does not rewrite import or sequence lineage.

## Staging settings

At minimum, staging supplies values equivalent to:

```dotenv
APP_ENV=staging
DEBUG=false
DRY_RUN=true
DATABASE_URL=postgresql+psycopg://<user>:<secret>@<private-host>/<dedicated-db>
TRUSTED_HOSTS=["staging.example.com"]
TRUSTED_PROXY_CIDRS=["127.0.0.1/32"]
RELEASE_ID=release-2026.08.07
MAX_REQUEST_BYTES=26214400
MAX_UPLOAD_BYTES=26214400
```

Supply the database value through deployment secrets, never source control or a
committed `.env`. Binding addresses, TLS termination, connection limits, request
timeouts, and complete body limits remain Nginx/Uvicorn/deployment concerns.

# VMR staging server runbook

Infrastructure and deployment procedure for the staging VPS. **No release has
been deployed yet.** This document describes what deployment does, what it
refuses to do, and how to verify it — not a system already running.

## What staging actually serves

**Staging now requires hosted-operator authentication, and may serve the operator
UI behind it.**

This replaced the earlier rule. Previously the whole server-rendered interface —
`/app` (customer-facing) and `/admin` (Workbench) — mounted only when
`FEATURES__WORKBENCH` was on, and `create_app()` refused to start with that flag
enabled outside `APP_ENV=local`, so a staging deployment published 39 API
operations plus three probes and nothing to click.

The contract today, in full, is:

* `AUTH__ENABLED=true` is **mandatory** in staging — not because the workbench is
  on, but because the application is reachable from the Internet at all.
  Everything except the health probes, `/auth/*` and `/static/*` requires an
  approved operator session, including every campaign write endpoint and the
  OpenAPI schema.
* `FEATURES__WORKBENCH=true` is now *permitted* in staging, and only behind a
  complete `AUTH__*` boundary. `create_app()` refuses every partial combination.
* `FEATURES__CONTACT_CAPTURE_INTAKE=true` is *permitted* in staging behind a
  configured `EXTENSION_AUTH__*` boundary — a per-install bearer credential
  bound to the enumerated capture contract and to an approved
  `chrome-extension://` origin. That replaced the blanket "local only" rule for
  this one switch; enabling it hosted *without* the credential boundary still
  refuses to start. See `docs/HOSTED_AUTH.md` §7a.
* `FEATURES__CONTACT_CAPTURE_PROMOTION=true` is *permitted* in staging behind
  its own prerequisite boundary, and refused outright in production. It requires
  `FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION=true`,
  `FEATURES__SALESNAV_DOMAIN_ENRICHMENT=true` and a configured
  `LOGO_DEV_API_KEY`; with any one missing, `validate_runtime_settings` refuses
  to start. That refusal is the point: without those values the resolution
  services fail closed and leave every capture untouched, so a half-configured
  box would accept captures, record every Capture job as succeeded, and promote
  nothing, with no error to explain it. See `docs/CAPTURE_PROMOTION.md`.
* The remaining local-only intake switches (`salesnav_intake`,
  `linkedin_profile_intake`, `linkedin_company_intake`) are still refused in
  staging by `validate_runtime_settings` and must stay unset.

> **This changes deployment ordering.** A staging box whose `/etc/vmr/vmr.env`
> has no `AUTH__*` block **will refuse to start** on the first release that
> contains this boundary. Update the environment file in the same maintenance
> window as the release, not after it. See `docs/HOSTED_AUTH.md` for the full
> key list and the Google Cloud Console values.

The foundation scope of the first deployment is unchanged and still what a deploy
proves: server, accounts, sandboxing, proxy contract, migrations, health gating,
backup, rollback, reboot survival — against real code.

## Health, readiness and version

Three endpoints, deliberately small contracts:

| Endpoint | Success | Failure | What it proves |
|---|---|---|---|
| `GET /healthz` | `200 {"status":"ok"}` | process/server failure | this web process can answer HTTP; no database or provider call |
| `GET /readyz` | `200`, status `ready` | `503`, status `not_ready` | startup configuration passed and PostgreSQL answered one bounded `SELECT 1` |
| `GET /version` | `200 {"version":"<RELEASE_ID>"}` | — | which commit is live |

`/healthz` and `/readyz` are authoritative. `/health` and `/ready` still exist in
the application as compatibility aliases returning the same hardened contracts,
but the nginx site deliberately does **not** proxy them: publishing four paths
for two contracts is an invitation to point a monitor at the deprecated pair.

`/readyz` makes **no worker claim**. Queue rows describe work, not whether a
worker process is alive. `systemctl is-active vmr-worker` plus
`journalctl -u vmr-worker` is the only honest worker check today.

### The Host header is not optional

The application's Host allow-list applies to the probes too. A bare
`curl http://127.0.0.1:8000/healthz` sends `Host: 127.0.0.1` and gets
**400 Invalid host header**, which looks exactly like a broken deployment.

Every probe must carry the staging hostname:

```bash
HOST=<the value of VMR_HEALTH_HOST in /etc/vmr/deploy.conf>

curl -sS -H "Host: $HOST" http://127.0.0.1:8000/healthz
curl -sS -H "Host: $HOST" http://127.0.0.1:8000/readyz
curl -sS -H "Host: $HOST" http://127.0.0.1:8000/version

# hardening headers present; HSTS correctly ABSENT over plain HTTP
curl -sS -D - -o /dev/null -H "Host: $HOST" http://127.0.0.1:8000/healthz \
  | grep -iE 'x-request-id|x-content-type-options|referrer-policy|x-frame-options|content-security-policy|cache-control|strict-transport'

# the Host guard itself is live - expect 400
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/healthz
```

`scripts/smoke.py` accepts a base URL as its first argument, so
`python scripts/smoke.py https://$HOST` works through nginx. Its *default*
(`http://127.0.0.1:8000`) sends the wrong Host and returns 400 — do not use the
default form as a deployment gate. `vmr-deploy` runs the gate itself.

### Proving the proxy boundary is the application's

HSTS is **not** the test — uvicorn would set the scheme itself if its proxy
handling were left on, and the header would appear either way. The authoritative
check is the `vmr.http` log line:

```bash
curl -sS https://$HOST/healthz >/dev/null
journalctl -u vmr-web -n 1 --no-pager -o cat | python3 -m json.tool
```

* **Correct:** `"peer_ip": "127.0.0.1"`, `"trusted_proxy": true`, `"client_ip"`
  the real caller — the application's own boundary did the work.
* **Wrong:** `"peer_ip"` is the real caller and `"trusted_proxy": false` — uvicorn
  rewrote the peer, `TRUSTED_PROXY_CIDRS` never matched, and the application's
  conservative `X-Forwarded-For` chain walk never ran. Check that
  `--no-proxy-headers` is still on the `ExecStart` line.

## Deploying

```bash
vmr-deploy --sha <exact-approved-sha>              # dry run: prints the plan
vmr-deploy --sha <exact-approved-sha> --execute    # does it
vmr-deploy --list                                  # releases, newest first
```

Order, and why it is this order:

1. preconditions — root, `/etc/vmr/vmr.env` present and `0640 root:vmr`,
   `APP_ENV=staging` actually set, `/etc/vmr/deploy.conf` present, and
   `VMR_HEALTH_HOST` genuinely present in `TRUSTED_HOSTS`
2. export the exact commit from the bare mirror at `/srv/vmr/repo.git` into
   `/srv/vmr/releases/<timestamp>-<short-sha>` — code only, no `.git`
3. release venv; install the pinned closure from `constraints.txt`, then the
   project `--no-deps`
4. import-check the release — catches a dependency added to `pyproject.toml` but
   not regenerated into `constraints.txt`
5. `vmr-db-backup`
6. `alembic upgrade head`
7. write `RELEASE_ID` to `/srv/vmr/shared/runtime/release.env`
8. **move `/srv/vmr/app` to the new release**
9. restart `vmr-web`
10. gate: `/healthz` within 30 s, `/readyz` within 60 s, and `/version` reporting
    this SHA
11. only then start `vmr-worker`

**Step 8 before step 9 is the whole point.** Both units name `/srv/vmr/app`
absolutely in `WorkingDirectory` and `ExecStart`, so the symlink is what *loads*
a release. Restarting first would health-check the previous release and never
load the new one — and on a first deployment, where `/srv/vmr/app` does not exist
yet, the unit could not start at all.

The safety property that ordering was reaching for — *a release is not kept
unless it passes its gate* — is preserved by the failure path instead:

* **gate fails, previous release exists** → repoint the previous release, restore
  its `RELEASE_ID`, restart, re-verify, and put the worker back in the state it
  was in before the run. Exit 4.
* **gate fails, first deployment** → stop `vmr-web`, remove the symlink, leave the
  failed release on disk for inspection. Exit 4.
* **restoration itself fails** → exit 5, manual intervention.

The schema is never downgraded in any of those paths.

## Rolling back

```bash
vmr-rollback --list
vmr-rollback --to <release-dir-name>              # dry run
vmr-rollback --to <release-dir-name> --execute
```

Same sequence: repoint, rewrite `RELEASE_ID` so `/version` keeps telling the
truth, restart web, gate, then the worker.

### Rollback policy — read this

Code rollback and database rollback are **not symmetrical**.

* Code: repoint `/srv/vmr/app`, restart. Safe, reversible.
* Schema: `alembic downgrade` runs destructive DDL. Dropped data does not come
  back. Downgrade paths are rarely exercised and therefore rarely correct.

Neither script contains any downgrade logic. A failed health check rolls back
code and leaves the schema in place, because a well-formed migration is backward
compatible with the previous release. If a migration is not backward compatible,
code rollback will not save you — take a fresh backup and involve a human before
deploying it at all.

Always run `vmr-db-backup vmr_staging` before any migration. `vmr-deploy` does
this itself, at step 5.

## Database

    sudo vmr-db-backup vmr_staging                       # timestamped custom-format dump
    sudo vmr-db-restore --file <dump> --database <name>  # explicit target REQUIRED

Backups: `/srv/vmr/shared/backups`, `2750 root:root`, dumps `0600`, each with a
`.sha256` companion that restore verifies. Both scripts use peer authentication
as the `postgres` system user, so no credential is stored in or read by them.

**Retention is intentionally NOT automated.** Dumps accumulate. Review with
`ls -lh /srv/vmr/shared/backups`. Add pruning only once a retention requirement
is agreed — silently deleting staging backups is not a safe default.

### Topology, and why loopback is refused

Staging startup **refuses** a loopback or container-service database host:
`127.0.0.1`, `::1`, `localhost`, a bare Unix socket, the legacy numeric spellings
of loopback, and the names `postgres` / `db` / `database`. That guard is
deliberate and must not be relaxed.

The supported topology is PostgreSQL **on this VPS**, reachable on a
**non-loopback private address**:

* `postgresql.conf` — `listen_addresses` includes that private address
* `pg_hba.conf` — scoped to that address and the `vmr_staging` role only
* host firewall — TCP/5432 **denied** from the public Internet

The guard exists to stop staging pointing at a developer machine or a container
default. A dedicated, firewalled staging database on a private address satisfies
both its letter and its intent. A hosts-file alias pointing back at `127.0.0.1`
would satisfy neither and must not be used.

The address depends on this host's interfaces and is deliberately not invented in
the repository. It is a required deployment variable inside `DATABASE_URL`.

## Request and upload sizing

Three limits, ordered `upload < request < proxy` so each layer's error is the
right error rather than an accident of which fired first:

| Limit | Value | Bytes | Set in |
|---|---|---|---|
| `MAX_UPLOAD_BYTES` | 25 MiB | `26214400` | `/etc/vmr/vmr.env` |
| `MAX_REQUEST_BYTES` | 26 MiB | `27262976` | `/etc/vmr/vmr.env` |
| nginx `client_max_body_size` | 28m | `29360128` | the nginx site |

The 1 MiB of headroom on `MAX_REQUEST_BYTES` is not decoration. A multipart
upload declares the file **plus** its form framing — measured at 230–269 bytes
for the import route. Setting the global request ceiling equal to the upload
ceiling makes that framing tip a maximum-size upload over the global limit, so
the caller gets the hardening middleware's generic
`413 {"error":"request_too_large"}` instead of the application's own explanatory
message, and the friendly message becomes unreachable over HTTP. Startup permits
the equality; do not use it.

nginx sitting **above** the application ceiling is also intentional: an oversized
declared body then reaches the app and the caller gets its structured JSON 413
rather than an nginx HTML error page, while nginx still backstops the one case
the application provably cannot bound — a body with no `Content-Length`, or
chunked transfer encoding.

Note for completeness: the import route compares the request's *whole*
`Content-Length` against `MAX_UPLOAD_BYTES`, so a file of exactly 26 214 400
bytes is still refused — but now by the application's own friendly message rather
than a generic 413. The effective file ceiling is 25 MiB minus a few hundred
bytes.

## Security headers

The application owns every security header: `X-Content-Type-Options`,
`Referrer-Policy`, `X-Frame-Options`, `Permissions-Policy`,
`Content-Security-Policy`, `Cache-Control` and `Strict-Transport-Security`. Its
middleware strips any same-named header before the response leaves the app, so
there is exactly one owner.

nginx `add_header` **appends**. Do not add a CSP, an HSTS header, or any other
security header to the nginx site or its snippets — the result is two competing
values, not twice the safety. `HSTS_MAX_AGE_SECONDS` in `/etc/vmr/vmr.env` is the
supported control, including setting it to `0` during an HTTPS rollout.

Verify after the TLS cutover:

```bash
curl -sS -D - -o /dev/null https://$HOST/healthz | grep -ci strict-transport-security   # expect 1
```

## The access boundary

Two boundaries, in series. They are not interchangeable, and the difference
decides how you configure the outer one.

**The application authenticates.** `app/core/auth/policy.py` is default-deny:
only the health probes, the `/auth/*` sign-in surface and the `/static/` mount
are anonymous. Everything else needs a signed-in operator holding an active row
in the `users` table — the operator UI, the whole `/api` surface, root-level
`POST /campaigns` from the unprefixed router in `app/api/routes.py`, `/docs`,
`/redoc` and `/openapi.json`. It also decides *which* operator: the administrator
surface, `/api` and the provider-spend routes require an `ADMIN` account rather
than merely a signed-in one. Staging refuses to start without that boundary
configured — see `docs/HOSTED_AUTH.md`.

**nginx is the outer network boundary.** The site **default-denies**. Only
`/.well-known/acme-challenge/` is public. Everything else, probes included, goes
through an access snippet that ships as `deny all;`:

* `/etc/nginx/snippets/vmr-access.conf` — the application surface
* `/etc/nginx/snippets/vmr-probe-access.conf` — `/healthz`, `/readyz`, `/version`

Relax the probe snippet only if remote infrastructure monitoring genuinely needs
it; the deployment gate probes loopback on this host and does not need them
published.

Relax the application snippet on infrastructure grounds — not because the
application is unauthenticated, because it is not. The options are an operator IP
allow-list, HTTP Basic Auth over HTTPS, or both (`satisfy all`), and two of them
have a cost worth stating before you install one:

* An **IP allow-list** is a real hardening win for an operator with a fixed
  address, and a liability for one without. A beta operator on a residential
  connection is locked out every time the ISP hands out a new address, and the
  symptom is indistinguishable from an outage. It is not the product's
  authentication mechanism; the account directory is.
* **HTTP Basic Auth** puts a second credential prompt in front of Google
  sign-in. Add it only when the deployment wants an independent factor at the
  network edge — not to supply a login the application already has.
* **Opening `location /`** so operators can reach the sign-in page over the
  Internet is a legitimate choice with the application boundary in place. It is
  a deliberate decision, and it does not extend to the probe snippet.

Neither live snippet nor any htpasswd file is ever committed.

## Inspecting logs

journald is authoritative for both services:

    journalctl -u vmr-web -f
    journalctl -u vmr-worker -f
    journalctl -u vmr-web --since '1 hour ago'
    journalctl -u vmr-web -p err          # errors only
    journalctl -u nginx --since today

Both units set `PYTHONUNBUFFERED=1`, so `journalctl -f` keeps up during an
incident instead of lagging behind Python's block buffering.

Journal retention is bounded: 500M max, 1G kept free, 30-day retention
(`/etc/systemd/journald.conf.d/10-vmr.conf`).

Application file logs, if the app writes any, go to `/var/log/vmr/` and are
rotated daily, 14 generations, compressed, `0640 vmr:adm`
(`/etc/logrotate.d/vmr`). Nginx VMR logs rotate via `/etc/logrotate.d/vmr-nginx`.

The hardening middleware's request log is one compact JSON object per request on
the `vmr.http` logger. It never logs query strings, bodies, uploaded
spreadsheets, email or personalization text, cookies, authorization values,
tokens, or full database URLs. It can still render a propagated exception message
through the outer Starlette/Uvicorn loggers once a response has started — treat
journald access as sensitive and keep `/var/log/vmr` at `2750 vmr:adm`.

## Service control

    systemctl status vmr-web vmr-worker
    systemctl restart vmr-web
    systemctl enable vmr-web vmr-worker      # reboot survival; after a healthy deploy

Installation leaves both units **disabled and inactive**. `vmr-deploy` starts
them; `systemctl enable` is a separate, deliberate step once a release has passed
its gate. Until then a reboot correctly brings up nothing.

After enabling, prove it: reboot, then re-run the probe block above unattended.

## Nginx

    nginx -t
    ln -s /etc/nginx/sites-available/vmr-staging.conf /etc/nginx/sites-enabled/
    systemctl reload nginx

`nginx -t` **fails** unless all four companion files are installed:
`conf.d/vmr-upgrade-map.conf`, `snippets/vmr-proxy.conf`,
`snippets/vmr-access.conf`, `snippets/vmr-probe-access.conf`.

The site is present but **not enabled**. Enabling it before a release is deployed
publishes a 502. Note that the distribution default site currently owns the `_`
catch-all, so either set the real DNS name in `server_name` or remove the default
site.

## Remaining prerequisites before the first deployment

1. **A staging DNS hostname**, then a TLS certificate. The same string must appear
   in `TRUSTED_HOSTS`, nginx `server_name`, and `VMR_HEALTH_HOST` —
   `vmr-deploy` refuses to run when they disagree.
2. **A private non-loopback PostgreSQL address**, with `pg_hba` scoped to it and
   TCP/5432 firewalled from the Internet.
3. **`/etc/vmr/vmr.env`**, written by hand, `0640 root:vmr`, from
   `deploy/vmr.env.example`. Values containing spaces must be quoted:
   `vmr-deploy` sources this file to run Alembic, and systemd's `EnvironmentFile`
   syntax is more permissive than the shell's.
4. **`/etc/vmr/deploy.conf`** with a repository URL and the health Host.
5. **Git read access from the server** — a read-only HTTPS credential helper or a
   read-only deploy key.
6. **An approved application commit** — an exact SHA, never a branch.
7. **The operator source address** for `vmr-access.conf`, or an htpasswd file
   created on the server.
8. Explicit decisions on Gmail / OAuth / Sheets / verification-provider
   credentials. None are configured, and none should be until authorised.

## What is deliberately not automated

* **Backup retention.** Dumps accumulate until a retention requirement is agreed.
* **`systemctl enable`.** Reboot survival is a deliberate act after a healthy
  release, not a side effect of deployment.
* **PostgreSQL setup and the firewall.** `vmr-provision` creates directories and
  accounts; it does not touch the database or the firewall, because those need a
  human looking at this specific host.
* **Certificate issuance.** `certbot` is run by hand once DNS resolves.
* **Anything that would create a credential.** No script here writes
  `/etc/vmr/vmr.env`, an htpasswd file, or a database password.

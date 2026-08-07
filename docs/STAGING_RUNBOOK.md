# VMR staging server runbook

Infrastructure only. The application is NOT deployed.

## Health and readiness (Phase 11)

The application is expected to expose a liveness and a readiness endpoint on the
loopback backend. Nginx proxies both, and deployment verification will call them.

    backend base URL : http://127.0.0.1:8000     (APP_PORT in /etc/vmr/vmr.env)
    through nginx    : http://<host>/<path>

**UNRESOLVED â€” must be confirmed before deployment.** Two different path pairs
are in play and I have not invented a winner:

| Source | Liveness | Readiness |
|---|---|---|
| `docs/DEVELOPMENT.md` (current repo) | `/health` | `/ready` |
| Infrastructure brief | `/healthz` | `/readyz` |

The nginx site proxies **all four** so whichever the application actually ships
will work. Once the health-endpoint work lands, delete the unused pair from
`/etc/nginx/sites-available/vmr-staging.conf` and set `HEALTH_PATH` /
`READY_PATH` in `/usr/local/sbin/vmr-deploy`.

Expected semantics (to be confirmed against the implementation, not assumed):

* **liveness** â€” process is up. Cheap, no dependencies. Used to decide whether to
  restart the unit.
* **readiness** â€” dependencies (database) are reachable. Used to decide whether a
  release may be marked active.

Deployment success criteria:

1. `systemctl is-active vmr-web` is `active`
2. liveness returns HTTP 200 within 30s of restart
3. readiness returns HTTP 200 within 60s of restart
4. only then is the `/srv/vmr/app` symlink switched

If readiness fails, the release is NOT marked active and `vmr-rollback` is used.

## Inspecting logs

journald is authoritative for both services:

    journalctl -u vmr-web -f
    journalctl -u vmr-worker -f
    journalctl -u vmr-web --since '1 hour ago'
    journalctl -u vmr-web -p err          # errors only
    journalctl -u nginx --since today

Journal retention is bounded: 500M max, 1G kept free, 30-day retention
(`/etc/systemd/journald.conf.d/10-vmr.conf`).

Application file logs, if the app writes any, go to `/var/log/vmr/` and are
rotated daily, 14 generations, compressed, `0640 vmr:adm`
(`/etc/logrotate.d/vmr`). Nginx VMR logs rotate via `/etc/logrotate.d/vmr-nginx`.

## Database

    sudo vmr-db-backup vmr_staging                       # timestamped custom-format dump
    sudo vmr-db-restore --file <dump> --database <name>  # explicit target REQUIRED

Backups: `/srv/vmr/shared/backups`, `0750 root:root`, dumps `0600`, each with a
`.sha256` companion that restore verifies.

**Retention is intentionally NOT automated.** Dumps accumulate. Review with
`ls -lh /srv/vmr/shared/backups`. Add pruning only once a retention requirement
is agreed â€” silently deleting staging backups is not a safe default.

## Rollback policy â€” READ THIS

Code rollback and database rollback are **not symmetrical**.

* Code: repoint `/srv/vmr/app`, restart. Safe, reversible.
* Schema: `alembic downgrade` runs destructive DDL. Dropped data does not come
  back. Downgrade paths are rarely tested.

`vmr-rollback` therefore touches **only** code and contains no database logic.
A failed health check rolls back code and leaves the schema in place, because a
well-formed migration is backward compatible with the previous release. If a
migration is not backward compatible, code rollback will not save you â€” take a
fresh backup and involve a human before deploying it at all.

Always run `vmr-db-backup vmr_staging` immediately before any migration.

## Service control

    systemctl status vmr-web vmr-worker
    systemctl restart vmr-web
    systemctl enable --now vmr-web      # only after a real deployment exists

Both units are currently **disabled and inactive** by design.

## Nginx

    nginx -t
    ln -s /etc/nginx/sites-available/vmr-staging.conf /etc/nginx/sites-enabled/
    systemctl reload nginx

`vmr-staging.conf` is present but **not enabled**. Enabling it before the app is
deployed publishes a 502. Note the default site currently owns the `_` catch-all.

## Request-size limit

`client_max_body_size 30m`, chosen against the application's
`MAX_UPLOAD_BYTES = 25 MB` (`app/core/config.py`). Keep the two in step: raising
the app limit without raising nginx produces a confusing 413 at the proxy.

## Remaining prerequisites before deployment

1. A real DNS hostname pointing at this VPS, then a TLS certificate.
2. Confirmed health/readiness paths (see above).
3. **A dependency lock file.** `pyproject.toml` declares version *ranges* and the
   repository has no `uv.lock` / `poetry.lock` / `requirements.txt`. Deployments
   are therefore not byte-reproducible today. Generate a constraints/lock file
   and commit it before the first real deploy.
4. A merged, production-ready application head (an exact approved SHA).
5. Git remote access from the server, or an approved artefact transfer method.
6. Completion of `/usr/local/sbin/vmr-deploy` (it refuses to run while
   placeholders remain).
7. Explicit decisions on Gmail / OAuth / Sheets / verification-provider
   credentials. None are configured, and none should be until authorised.

# Deployment infrastructure templates

Templates for the VMR staging server. **These are templates, not live
configuration.** The running server holds the concrete copies; these exist so the
infrastructure is reviewable, diffable and reproducible.

Host-specific values (hostnames, IP addresses) are deliberately replaced with
`REPLACE_*` placeholders. Nothing here contains a secret, and nothing here
should ever be given one.

## Layout

| Template | Installs to | Mode |
|---|---|---|
| `nginx/vmr-staging.conf` | `/etc/nginx/sites-available/vmr-staging.conf` | 0644 root:root |
| `nginx/vmr-proxy.conf` | `/etc/nginx/snippets/vmr-proxy.conf` | 0644 root:root |
| `nginx/vmr-upgrade-map.conf` | `/etc/nginx/conf.d/vmr-upgrade-map.conf` | 0644 root:root |
| `nginx/vmr-access.conf.example` | `/etc/nginx/snippets/vmr-access.conf` | 0644 root:root |
| `nginx/vmr-probe-access.conf.example` | `/etc/nginx/snippets/vmr-probe-access.conf` | 0644 root:root |
| `systemd/vmr-web.service` | `/etc/systemd/system/vmr-web.service` | 0644 root:root |
| `systemd/vmr-worker.service` | `/etc/systemd/system/vmr-worker.service` | 0644 root:root |
| `ssh/10-vmr-hardening.conf` | `/etc/ssh/sshd_config.d/10-vmr-hardening.conf` | 0644 root:root |
| `journald/10-vmr.conf` | `/etc/systemd/journald.conf.d/10-vmr.conf` | 0644 root:root |
| `logrotate/vmr` | `/etc/logrotate.d/vmr` | 0644 root:root |
| `logrotate/vmr-nginx` | `/etc/logrotate.d/vmr-nginx` | 0644 root:root |
| `sbin/vmr-provision` | `/usr/local/sbin/vmr-provision` | 0750 root:root |
| `sbin/vmr-deploy` | `/usr/local/sbin/vmr-deploy` | 0750 root:root |
| `sbin/vmr-rollback` | `/usr/local/sbin/vmr-rollback` | 0750 root:root |
| `sbin/vmr-db-backup` | `/usr/local/sbin/vmr-db-backup` | 0750 root:root |
| `sbin/vmr-db-restore` | `/usr/local/sbin/vmr-db-restore` | 0750 root:root |
| `deploy.conf.example` | `/etc/vmr/deploy.conf` | 0644 root:root |
| `vmr.env.example` | shape of `/etc/vmr/vmr.env` | 0640 root:vmr — **values never committed** |

The operational runbook is `docs/STAGING_RUNBOOK.md`.

## Server layout

Created idempotently by `vmr-provision`, not by hand:

```
/srv/vmr                  2750 root:vmr        deployment root
├── app -> releases/<release>                  symlink; moved by vmr-deploy
├── repo.git/                                  bare mirror; exact commits are exported from it
├── releases/             2775 root:vmrdeploy  operator writes, service account only reads
└── shared/               2750 vmr:vmr
    ├── uploads/          2770 vmr:vmr         STAGED_UPLOADS_DIR points here
    ├── backups/          2750 root:root       dumps 0600, service account cannot read
    ├── runtime/          2770 vmr:vmr         release.env lives here
    │   └── home/         2700 vmr:vmr         HOME for both units
    └── logs/             2770 vmr:vmr
/etc/vmr                  0750 root:vmr        vmr.env 0640 root:vmr, deploy.conf 0644 root:root
/var/log/vmr              2750 vmr:adm
```

`/srv/vmr/shared` and `/var/log/vmr` are the only paths in the units'
`ReadWritePaths`. Everything else the services can see is read-only, including
the release they are running from.

## Accounts

| Account | Purpose |
|---|---|
| `root` | Emergency/system administration only |
| `sahil` | Interactive operator. Groups: `sudo`, `vmr`, `vmrdeploy` |
| `vmr` | Service account. `nologin`, locked password, **no sudo**, no SSH access |
| `vmrdeploy` | Group granting write access to `releases/` — deliberately separate from `vmr` |

The split matters: `vmr` runs the application and can **read** its code but never
**write** it, so a compromised application cannot rewrite itself and persist.

## Install order

```
vmr-provision                       # dry run first, then --execute
# write /etc/vmr/vmr.env by hand, 0640 root:vmr, from vmr.env.example
# write /etc/vmr/deploy.conf from deploy.conf.example
# PostgreSQL: private non-loopback listen address, pg_hba scope, firewall
install -m0750 sbin/* /usr/local/sbin/
install -m0644 systemd/*.service /etc/systemd/system/ && systemctl daemon-reload
install -m0644 journald/10-vmr.conf /etc/systemd/journald.conf.d/
install -m0644 logrotate/vmr logrotate/vmr-nginx /etc/logrotate.d/
install -m0644 nginx/vmr-upgrade-map.conf /etc/nginx/conf.d/
install -m0644 nginx/vmr-proxy.conf /etc/nginx/snippets/
install -m0644 nginx/vmr-access.conf.example /etc/nginx/snippets/vmr-access.conf
install -m0644 nginx/vmr-probe-access.conf.example /etc/nginx/snippets/vmr-probe-access.conf
install -m0644 nginx/vmr-staging.conf /etc/nginx/sites-available/
nginx -t                            # site still disabled; snippets must exist
vmr-deploy --sha <approved-sha>     # dry run first, then --execute
```

The nginx site stays out of `sites-enabled` until a release is deployed and
healthy; enabling it earlier publishes a 502.

## The access boundary

The application has no authentication. Its staging route table is 39 documented
operations and includes unauthenticated state-changing calls — the whole `/api`
surface, and root-level `POST /campaigns` from the unprefixed router in
`app/api/routes.py`.

So `vmr-staging.conf` **default-denies**. The only genuinely public path is the
ACME challenge. Everything else, probes included, goes through an access snippet
that ships as `deny all;`. A path allow-list would have been the wrong shape: it
would have missed root-level `POST /campaigns`, and it would silently fail to
cover whatever the next merge adds.

`vmr-access.conf` supports an IP allow-list, temporary HTTP Basic Auth, or both.
Neither the live snippet nor any htpasswd file is ever committed. This is a
network boundary, not authentication — it buys time until authenticated remote
access exists.

## Two things this deployment gets right that are easy to get wrong

**The symlink moves before the restart.** Both units name `/srv/vmr/app`
absolutely in `WorkingDirectory` and `ExecStart`, so the symlink is what *loads*
a release. Restarting first would health-check the previous release; on a first
deployment the unit could not start at all. `vmr-deploy` therefore switches, then
restarts, then gates — and repoints the previous release if the gate fails.

**uvicorn runs with `--no-proxy-headers`.** Its proxy-header handling is on by
default and trusts loopback, so it would rewrite the client address and scheme
before the application's own trusted-proxy allow-list ever saw them, making
`TRUSTED_PROXY_CIDRS` dead configuration. Disabling it leaves exactly one
component deciding what to trust.

## Deployment variables that must be supplied

| Variable | Where | Why it is not invented here |
|---|---|---|
| `REPLACE_WITH_STAGING_DNS_NAME` | `TRUSTED_HOSTS` in `vmr.env`, `server_name` in the nginx site, `VMR_HEALTH_HOST` in `deploy.conf` | The staging hostname is not assigned yet. All three must be the same string; `vmr-deploy` refuses to run when they disagree. |
| `REPLACE_WITH_PRIVATE_DB_ADDRESS` | `DATABASE_URL` in `vmr.env` | Depends on this host's interfaces. Staging startup refuses loopback, so a private non-loopback address is required. |
| `REPLACE_WITH_REPOSITORY_URL` | `VMR_REPO_URL` in `deploy.conf` | Depends on the credential mechanism chosen for the server. |
| operator source address | `vmr-access.conf` on the server | Never committed. |

## Dependency reproducibility

`constraints.txt` at the repository root is a pinned runtime closure (64
packages, CPython 3.11 / Linux). `vmr-deploy` installs it as a requirements file
and then installs the project `--no-deps`, so a release gets exactly the reviewed
versions while the source tree stays authoritative for templates and static
assets. It is a pip constraints file on purpose — pip + `pyproject.toml` is this
repository's package management, and a second tool would not be an improvement.

`vmr-deploy` import-checks the release before the symlink moves, which is what
catches a dependency added to `pyproject.toml` but not regenerated into
`constraints.txt`.

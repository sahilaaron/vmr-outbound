# Deployment infrastructure templates

Templates for the VMR staging server. **These are templates, not live
configuration.** The running server holds the concrete copies; these exist so the
infrastructure is reviewable, diffable and reproducible.

Host-specific values (hostnames, IP addresses) are deliberately replaced with
`REPLACE_*` placeholders. Nothing here contains a secret, and nothing here
should ever be given one.

## Layout

| Template | Installs to |
|---|---|
| `nginx/vmr-staging.conf` | `/etc/nginx/sites-available/vmr-staging.conf` |
| `nginx/vmr-proxy.conf` | `/etc/nginx/snippets/vmr-proxy.conf` |
| `nginx/vmr-upgrade-map.conf` | `/etc/nginx/conf.d/vmr-upgrade-map.conf` |
| `systemd/vmr-web.service` | `/etc/systemd/system/vmr-web.service` |
| `systemd/vmr-worker.service` | `/etc/systemd/system/vmr-worker.service` |
| `ssh/10-vmr-hardening.conf` | `/etc/ssh/sshd_config.d/10-vmr-hardening.conf` |
| `journald/10-vmr.conf` | `/etc/systemd/journald.conf.d/10-vmr.conf` |
| `logrotate/vmr` | `/etc/logrotate.d/vmr` |
| `logrotate/vmr-nginx` | `/etc/logrotate.d/vmr-nginx` |
| `sbin/vmr-db-backup` | `/usr/local/sbin/vmr-db-backup` (0750 root:root) |
| `sbin/vmr-db-restore` | `/usr/local/sbin/vmr-db-restore` (0750 root:root) |
| `sbin/vmr-deploy` | `/usr/local/sbin/vmr-deploy` (0750 root:root) |
| `sbin/vmr-rollback` | `/usr/local/sbin/vmr-rollback` (0750 root:root) |
| `vmr.env.example` | shape of `/etc/vmr/vmr.env` (0640 root:vmr) — **values never committed** |

The operational runbook is `docs/STAGING_RUNBOOK.md`.

## Server layout these assume

```
/srv/vmr              2750 root:vmr        deployment root
├── app -> releases/<release>              symlink, created by deployment
├── releases/         2775 root:vmrdeploy  operator writes, service account only reads
└── shared/           2750 vmr:vmr
    ├── uploads/      2770 vmr:vmr
    ├── backups/      2750 root:root       dumps 0600, service account cannot read
    ├── runtime/      2770 vmr:vmr
    └── logs/         2770 vmr:vmr
/etc/vmr              0750 root:vmr        config; vmr.env is 0640 root:vmr
/var/log/vmr          2750 vmr:adm
```

## Accounts

| Account | Purpose |
|---|---|
| `root` | Emergency/system administration only |
| `sahil` | Interactive operator. Groups: `sudo`, `vmr`, `vmrdeploy` |
| `vmr` | Service account. `nologin`, locked password, **no sudo**, no SSH access |
| `vmrdeploy` | Group granting write access to `releases/` — deliberately separate from `vmr` |

The split matters: `vmr` runs the application and can **read** its code but never
**write** it, so a compromised application cannot rewrite itself and persist.

## Two things to resolve before first deployment

1. **Health/readiness paths are unconfirmed.** `docs/DEVELOPMENT.md` documents
   `/health` and `/ready`; the infrastructure brief specified `/healthz` and
   `/readyz`. The nginx template proxies all four. Delete the unused pair once
   the application's actual endpoints are known.
2. **There is no dependency lock file.** `pyproject.toml` declares version ranges
   and the repository has no `uv.lock` / `poetry.lock` / `requirements.txt`, so
   deployments are not byte-reproducible. `vmr-deploy` expects a constraints file;
   generate and commit one first.

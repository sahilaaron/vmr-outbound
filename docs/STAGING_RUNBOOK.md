# Hosted Beta staging runbook

**Status date:** 14 August 2026

This is a runbook for an already-deployed Hosted Beta. Older wording that said no release had been deployed is superseded.

For the exact current merged/live distinction and UAT state, see [`CURRENT_PRODUCT_STATE.md`](CURRENT_PRODUCT_STATE.md).

## 1. Current verified state

Hosted Beta is live at:

`srv1885453.hstgr.cloud`

Last independently verified live release:

`d9750b008919bf2bfe42a848b0b454eeedd66f1f`

Last verified release directory:

`/srv/vmr/releases/20260813T112854Z-d9750b008919`

Current merged main is newer:

`c1bd054e45e09a22d3d8cf1e7aec629226f352e4`

Never infer that merged main is live. `/version` is the deployment authority.

## 2. Health / readiness / version

Authoritative probes:

- `GET /healthz`
- `GET /readyz`
- `GET /version`

Expected healthy shape:

- `/healthz` → 200 `{"status":"ok"}`
- `/readyz` → 200 with configuration/database checks ready
- `/version` → exact deployed release SHA

`/readyz` does not prove the worker is alive. Check `vmr-worker` separately.

## 3. Services

Hosted Beta uses separate systemd services for web and worker.

After a controlled configuration/release change verify:

```bash
systemctl is-active vmr-web
systemctl is-active vmr-worker
```

Then check recent logs without printing secrets.

The current deployment also uses a systemd drop-in that disables uvicorn access logging; structured application route logs remain. Do not remove that boundary casually.

## 4. Environment file

Primary runtime environment:

`/etc/vmr/vmr.env`

Expected permissions remain restricted (`0640 root:vmr`).

Before editing:

1. create a timestamped root-owned backup;
2. change only the intended keys;
3. do not print provider/OAuth/encryption/session secret values;
4. validate using the application's real runtime parser before restarting services;
5. restart only the services that actually require the changed settings.

## 5. Current capture-promotion runtime group

Hosted Beta currently has these effective switches enabled:

```text
FEATURES__CONTACT_CAPTURE_PROMOTION=true
FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION=true
FEATURES__SALESNAV_DOMAIN_ENRICHMENT=true
FEATURES__MODEL_COMPANY_DOMAIN_LOOKUP=true
```

`LOGO_DEV_API_KEY` is configured/present. Never print its value.

This group was added during real UAT after diagnostics proved captures were being stored with correct Campaign filing intent but automatic promotion was never attempted because the switches were absent/default false.

The application runtime validator accepted the resulting staging configuration before service restart.

## 6. Capture recovery behavior proven in Hosted Beta

The normal `vmr-worker` pending-resolution/backfill path can recover captures staged while capture promotion was disabled.

During real UAT, restarting the worker after enabling the runtime group allowed the supported `resolve_pending → resolution_service.resolve` flow to process the pending cohort. No manual SQL and no hand-created Contact/CampaignContact rows were used.

The system deliberately leaves ambiguous company/domain cases unresolved rather than guessing.

Unresolved decisions are not automatically retried forever; once a current `UNRESOLVED` decision exists, operator confirmation or an explicit forced re-resolution is required.

## 7. Provider/runtime capability

Logo.dev is configured and was successfully called during recovery.

Model company-domain fallback is enabled as a feature but the live VPS currently lacks the `claude` CLI executable on PATH. Calls therefore return `API_UNAVAILABLE`.

Do not treat the feature switch alone as proof that the runtime capability exists.

## 8. Hosted identity

`AUTH__ENABLED=true` is required on Hosted Beta.

Durable VMR `users` are authority. Admin creates users at `/app/admin/users`; normal users do not self-register.

Google identity and email/password login are hosted identity mechanisms. Gmail mailbox authorization is separate.

See [`HOSTED_AUTH.md`](HOSTED_AUTH.md).

## 9. Extension authorization

Current merged main after PR #275 uses VMR account-linked extension authorization with authorization-code + PKCE.

Ordinary hosted users should not configure backend URL or paste a reusable capture credential.

However, the last independently verified live release predates PR #275. Until `/version` proves deployment of a containing SHA, treat account-linked extension auth as merged-not-yet-live.

The extension's hosted authority remains limited to the exact four-route capture contract documented in [`HOSTED_AUTH.md`](HOSTED_AUTH.md).

## 10. Gmail drafts

Gmail draft integration is configured separately from hosted login.

Current known runtime state:

- Gmail drafts enabled;
- Email sequences enabled;
- no automatic Sending implementation.

A Gmail OAuth client/secret/token is sensitive deployment configuration and must never be printed in diagnostics or docs.

## 11. Research and ordinary product controls

The live Campaign UAT is currently blocked at Research because Company Research is effectively off.

That is an operator-control problem under repair, not a reason to edit random runtime flags ad hoc.

Branch `feat/uat-operator-controls` is building a durable Admin-operated operational-control layer so ordinary product operation does not require SSH/`.env` edits.

Until that branch is merged/deployed, changes to current live operational switches remain controlled VPS changes and must follow the backup → validate → restart → verify sequence above.

## 12. Deployment

Use the existing deployment tool with an exact approved SHA:

```bash
vmr-deploy --sha <exact-approved-sha>
vmr-deploy --sha <exact-approved-sha> --execute
vmr-deploy --list
```

Normal deployment sequence remains:

1. verify preconditions;
2. export exact commit into a new release directory;
3. create/install the release venv/dependency closure;
4. import-check release;
5. database backup;
6. `alembic upgrade head`;
7. write exact `RELEASE_ID`;
8. move `/srv/vmr/app` symlink to the new release;
9. restart web;
10. gate `/healthz`, `/readyz`, `/version`;
11. start/restart worker only after the web gate succeeds.

A failed deployment may roll code back to the previous release, but the deployment tooling does not automatically downgrade the database schema.

## 13. Database backup / rollback

Use the existing backup/restore utilities before migrations or other risky DB changes.

Code rollback and schema rollback are not equivalent. Schema downgrades may be destructive; do not improvise an `alembic downgrade` as an automatic failure response.

## 14. Reverse proxy / logs

Hosted Beta terminates TLS at nginx and the application owns the trusted forwarded-header boundary.

The exact `/auth/setup` route must not write one-time password tokens into nginx access logs.

The current live host also disables uvicorn access logging so request query strings are not duplicated there. Structured route logging remains the intended observability path.

Repository nginx templates/configuration should be kept aligned with live hardening; do not assume a manual VPS fix automatically updated the repository template.

## 15. Deployment acceptance checklist

After every release/configuration maintenance window record:

- exact deployed SHA from `/version`;
- `vmr-web` active;
- `vmr-worker` active;
- `/healthz` 200;
- `/readyz` 200;
- Alembic at intended head;
- hosted sign-in still works;
- Admin/user authorization boundaries still work;
- extension path appropriate to that release works;
- Gmail remains draft-only;
- no automatic send side effect;
- any provider-spend action is deliberately authorized and bounded.

## 16. Current UAT reference

The current real Campaign UAT has already proven Hosted Beta capture connectivity, filing intent, capture promotion and Campaign membership creation.

The next product gate is downstream progression beginning at Research, not another infrastructure re-provisioning exercise.

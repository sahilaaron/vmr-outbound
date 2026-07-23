# Backend handoff contract (v1) — Sales Navigator capture → VMR intake

This is the **versioned integration contract** between the extension and the VMR
backend. It is intentionally implemented here only against a **mock receiver**
(`tools/mock-receiver.js`); the real endpoint belongs to the backend and lands
after PR #120 (staged imports / workbench) merges. See *Backend adapter still
required* at the bottom.

Contract version: `salesnav-capture/1.0.0` (the `schema_version` field).

## Boundary

The extension's responsibility ends when it hands an operator-authorized batch to
a narrow intake endpoint. The endpoint **stages** data only. It must NOT, in this
call: create/update contacts, apply authoritative normalization, deduplicate
against the database, enforce or bypass suppressions, verify emails, or score.
Those are downstream backend/operator-workbench steps (GOAL.md, AGENTS.md).

## Endpoint

```
POST {backend_base_url}/api/intake/sales-navigator/stage
```

`backend_base_url` defaults to `http://127.0.0.1:8000` and is operator-configurable
(loopback only). The final path should be reconciled to repository routing
conventions when the backend adapter is built; the extension centralizes it in
`src/common/constants.js` (`INTAKE_PATH`) so it changes in exactly one place.

Also consumed (optional, for campaign selection):

```
GET {backend_base_url}/api/campaigns?fields=id,name,status
```

Returns a minimal list of `{ id, name, status }`. The extension requests only
these fields and ignores everything else.

## Request

`Content-Type: application/json`. Headers also include
`Idempotency-Key: <client_batch_id>` and `X-Client-Batch-Id: <client_batch_id>`.

Body shape — full JSON Schema in [`intake.schema.json`](./intake.schema.json),
representative body in [`fixtures/payload.example.json`](./fixtures/payload.example.json):

| Field | Type | Notes |
| --- | --- | --- |
| `schema_version` | string | `"salesnav-capture/1.0.0"` |
| `client_batch_id` | string (uuid) | Idempotency key; stable per draft batch |
| `campaign_id` | string \| null | Operator-selected, may be null in dev |
| `captured_at` | ISO-8601 | Draft batch creation time |
| `source` | string | `"chrome-extension:salesnav-capture"` |
| `current_search_url` | string \| null | Last SN search URL captured from |
| `extraction_metadata` | object | version, pages, statuses, warnings summary |
| `records[]` | array | 1–500 raw records; see schema |

Each **record** carries only result-page-visible values plus capture provenance.
Every field is nullable; a missing value is an explicit `null` **plus a warning**,
never a guess. `rawFullName` preserves the exact visible Unicode string (no
translation, no ASCII folding — those are backend normalization concerns). URLs
are normalized to canonical `https://www.linkedin.com/...` form; the volatile
search-context suffix is stripped from `/sales/lead/` URLs so the same lead has a
stable identity across pages.

## Response

Representative bodies in `fixtures/response.success.json`,
`fixtures/response.idempotent.json`, `fixtures/response.error.json`. Schema in
[`intake.response.schema.json`](./intake.response.schema.json).

Success (`201 Created`, or `200 OK` on idempotent replay):

| Field | Type | Notes |
| --- | --- | --- |
| `staging_id` | string | Backend staging/intake record id |
| `client_batch_id` | string | Echoed |
| `record_count` | integer | Records staged |
| `warnings` | array | Backend-side staging warnings (may be empty) |
| `received_at` | ISO-8601 | |
| `expires_at` | ISO-8601 | When the un-committed staged batch expires |
| `operator_workbench_url` | string (loopback) | Deep link to review mapping/dry-run |
| `already_received` | boolean | `true` when this `client_batch_id` was seen before |

The extension only renders `operator_workbench_url` as a clickable link when it is
a loopback origin.

## Idempotency

Keyed on `client_batch_id`. Re-POSTing the same batch id must **not** create a
second staging record; it returns the original result with `already_received:
true`. The extension keeps a batch's `client_batch_id` stable until the operator
explicitly clears the batch, so a retry after a timeout is safe.

## Error responses

Deterministic, JSON, with a stable `error` code. The extension surfaces the code,
HTTP status, and body, and offers a **Retry** (safe because of idempotency).

| Status | `error` | Meaning |
| --- | --- | --- |
| 400 | `invalid_json` | Body was not valid JSON |
| 401/403 | `unauthorized` | Local auth (if any) failed |
| 409 | `campaign_invalid` | `campaign_id` unknown / not acceptable |
| 413 | `payload_too_large` | Body exceeded the receiver limit |
| 422 | `validation_failed` | Body failed schema validation (`details[]`) |
| 429 | `rate_limited` | Too many intake calls |
| 5xx | `internal_error` | Backend fault; retry is safe |

The extension additionally handles transport failures locally: `timeout`
(no response within 15 s) and `network_error` (receiver unreachable).

## Versioning rules

- `schema_version` is `MAJOR.MINOR.PATCH` under the `salesnav-capture/` namespace.
- **MAJOR** bump = breaking change to request/response shape or field meaning. The
  backend should reject an unknown MAJOR with `422 validation_failed`.
- **MINOR** = additive, backward-compatible fields. Receivers must ignore unknown
  additive fields.
- **PATCH** = clarifications/non-structural. The single source of truth for the
  version is `SCHEMA_VERSION` in `src/common/constants.js`.

## CORS and local-origin assumptions

- The extension talks only to **loopback** origins (`127.0.0.1`, `localhost`,
  `[::1]`). It refuses any non-loopback target and never embeds a remote URL.
- The loopback hosts (`http://127.0.0.1/*`, `http://localhost/*`) are declared as
  **optional** host permissions and are requested at runtime (with a user gesture)
  before the first send / campaign fetch. Once granted, the service worker can POST
  cross-origin without a CORS preflight grant. For robustness (and browser-page
  testing), the backend should
  still answer `OPTIONS` preflight and reflect the request origin with
  `Access-Control-Allow-Methods: POST, GET, OPTIONS` and allow the
  `Content-Type, Idempotency-Key, X-Client-Batch-Id` headers. The mock receiver
  does exactly this.
- LinkedIn origins are **read-only** surfaces; the extension never POSTs to them.

## Integration note for the backend session

When PR #120 is merged and the staged-import/workbench services exist, wire a thin
adapter — do not re-plumb the extension:

1. Add route `POST /api/intake/sales-navigator/stage` (reconcile the name to repo
   conventions; update `INTAKE_PATH` in `constants.js` if it differs).
2. Validate the body against `intake.schema.json` (MAJOR version gate).
3. Map each raw record onto the existing **staged import row** model. The natural
   mapping to the contact-input contract (`docs/contact_input_contract.md`):
   `firstName→first_name`, `lastName→last_name`, `companyName→company_name`,
   `title→title`, `linkedinProfileUrl→linkedin_url`, `location→country/location`
   (operator-reviewed), `rawFullName`/lead & company URLs/`visibleCompanyMetadata`
   retained as **raw provenance**. **`company_domain` is intentionally absent** —
   the extension does not guess domains; the operator/backend supplies it during
   mapping. Preserve every raw value verbatim on the write-once raw row
   (DAT-003 / DAT-005 provenance).
4. Create a staging/intake record (NOT contacts). Honour idempotency on
   `client_batch_id`. Return the response shape above, including a real
   `operator_workbench_url` into the PR #120 workbench.
5. Keep the endpoint local-only and behind the same environment guard the
   workbench uses.

Until then the extension defaults to the **mock receiver** and JSON/CSV export;
nothing is sent anywhere without an explicit operator “Send”.

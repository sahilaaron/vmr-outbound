# Contact-first capture contract — `linkedin-contact-capture/2.0.0`

The contract between the capture extension and the VMR backend for **acquiring a
person**. Schema files (the single source of truth for the wire shape):

- request — [`contact-capture.schema.json`](./contact-capture.schema.json)
- response — [`contact-capture.response.schema.json`](./contact-capture.response.schema.json)
- examples — [`fixtures/contact-capture.profile.example.json`](./fixtures/contact-capture.profile.example.json),
  [`fixtures/contact-capture.salesnav.example.json`](./fixtures/contact-capture.salesnav.example.json)

## The product boundary

> Save the person first. Decide what to do with them later.

```
LinkedIn / Sales Navigator  →  Chrome extension  →  permanent Contact
                                                          ↓
              company + contact research → qualification → email discovery
              → verification → saved audience → campaign → outreach
```

Contacts are permanent. Campaigns are a later, temporary use of a saved
audience. **There is no campaign in this contract, and none is required to save
a person.** Everything downstream of acquisition belongs to the backend.

The extension captures observations. The backend decides identity, freshness,
labels, and canonical truth.

## Endpoint

```
POST {backend_base_url}/api/intake/contact-captures
Content-Type: application/json
Idempotency-Key: <client_submission_id>
```

Companion reads (same feature switch, same local-only and origin guards):

```
GET {backend_base_url}/api/contact-labels
GET {backend_base_url}/api/contacts/lookup?linkedin_profile_url=…
```

`/api/contacts/lookup` answers **existence only** —
`{"match": "none" | "exact" | "ambiguous" | "unknown", "contact_count": n}` — so
the panel can label its primary action *Save Contact* or *Refresh Contact*
without pulling any contact field into the browser.

Local only: gated behind `FEATURES__CONTACT_CAPTURE_INTAKE` (default off),
refused unless `APP_ENV=local`, and restricted to the extension origin or a
loopback origin.

## Request shape

| Field | Type | Meaning |
| --- | --- | --- |
| `schema_version` | const | `linkedin-contact-capture/2.0.0` |
| `client_submission_id` | string (8–128) | Idempotency key for the whole reviewed submission |
| `capture_mode` | enum | `linkedin_profile` \| `salesnav_people_search` |
| `submitted_at` | ISO-8601 | When the reviewed submission was assembled |
| `source` | const | `chrome-extension:linkedin-contact-capture` |
| `extension_version` | string \| null | Manifest version |
| `operator_metadata` | object | `{ labels: [names], note: string \| null }` — both optional |
| `contacts` | array (1–500) | The people the operator explicitly included |

Each entry of `contacts`:

| Field | Meaning |
| --- | --- |
| `client_capture_id` | Idempotency key for this one person |
| `captured_at` | When the page was read |
| `source` | `{ surface, url, page_title, operator_triggered: true }` |
| `person` | Visible identity and top-card fields, including `about_text` |
| `current_employment_hint` | The visible current role — a hint, never a decision |
| `experience_observations` | Nested entries in on-page order (empty for a results row) |
| `extraction` | Adapter version, status, missing/excluded sections, page warnings |
| `operator_metadata` | Per-person labels/note; overrides the submission note |
| `raw_snapshot` | The verbatim adapter output, preserved as immutable evidence |

`source.operator_triggered` is `const: true`. The extension has no unattended
capture path, and the contract makes that structurally unrepresentable.

### Identity rules

- `person.linkedin_profile_url` is either **null** or an `https` `linkedin.com`
  **MAIN** profile URL (`/in/<id>`). Sub-routes, company pages, and deceptive
  hosts are refused by both the schema pattern and the extension validator.
- `person.salesnav_lead_url` is context only. A Sales Navigator lead URL can
  recognise the same row twice inside one submission, but it can **never** match
  a stored contact and is never promoted into the profile-URL slot.
- A capture must carry at least one of profile URL, lead URL, or name.
  Otherwise it is an empty record and is refused.
- A Sales Navigator result row that shows no `/in/` URL keeps `null`. The
  uncertainty is preserved, never repaired.

## Response shape

```json
{
  "submission_id": "uuid",
  "client_submission_id": "uuid",
  "received_at": "ISO-8601",
  "already_received": false,
  "counts": {
    "submitted": 2, "created": 0, "refreshed_exact_match": 1,
    "exact_match_unchanged": 0, "staged_unmatched": 1, "staged_ambiguous": 0,
    "duplicate_in_submission": 0, "suppressed": 0,
    "labels_applied": 2, "notes_recorded": 2
  },
  "results": [ { "client_capture_id": "…", "capture_id": "…",
                 "outcome": "exact_match_refreshed", "matched_contact_id": "…",
                 "contact_url": "…", "capture_url": "…",
                 "review_candidate_count": 0, "labels_applied": ["Healthcare"],
                 "warnings": [] } ],
  "operator_workbench_url": "http://127.0.0.1:8000/contact-captures/submissions/…"
}
```

### Outcomes

| Outcome | Meaning |
| --- | --- |
| `exact_match_refreshed` | Exactly one contact carries this normalized URL; ≥1 field changed under the freshness policy |
| `exact_match_unchanged` | Exactly one match; evidence recorded, nothing newer than the current winners |
| `unmatched_staged` | No URL match (or no URL at all). Permanent capture evidence; weak candidates stored for review |
| `ambiguous_review` | More than one contact carries this URL. Surfaced, never merged |
| `duplicate_in_submission` | The same person appeared earlier in this submission. Evidence kept; reconciled once |
| `suppressed` | The matched contact is suppressed. Evidence linked, no canonical field touched, suppression untouched |

`rejected_invalid` is not an outcome: a rejected submission is never persisted,
so it is reported as an HTTP error instead of a stored row.

### Why `created` is always 0 today

A canonical contact requires a company **domain** — it is the deduplication and
email-generation key and is `NOT NULL`. A LinkedIn page never shows one, and
inferring a domain from a company name would be fabricated evidence. So an
unmatched person is stored as a permanent, reviewable capture awaiting domain
resolution and operator promotion, and the response reports `created: 0`
honestly. Promotion is tracked as follow-up work, not silently faked here.

## Labels

Labels classify permanent contacts. They are **not** campaigns, audiences, or
an eligibility signal, and applying one can never make a contact
outreach-eligible or lift a suppression.

- The extension **requests** label names. The backend slugs them
  (`"Venture Capital"`, `"venture  capital"`, `"Venture-Capital!"` → `venture-capital`),
  finds or creates the canonical row, and assigns it.
- Assignment happens only for a capture that matched exactly one contact and is
  not suppressed. Otherwise the requested names stay on the capture as evidence
  and apply when it is promoted.
- Assignment is additive and idempotent: an existing label is never duplicated
  and existing labels are never removed.

## Notes

- One optional submission note, plus an optional per-person note that overrides
  it for that person.
- Notes are **append-only**. A refresh appends a row and leaves every earlier
  note intact; nothing in this path updates or deletes one.
- Each note records its text, scope (`submission` / `contact`), author, capture,
  submission, matched contact when known, and creation time.

## Idempotency and errors

Retrying the same `client_submission_id` with identical content replays the
original truthful response (`already_received: true`, HTTP 200). Different
content under the same id is a conflict, not a silent overwrite.

| HTTP | `error` | When |
| --- | --- | --- |
| 400 | `invalid_json` | Body is not a JSON object |
| 403 | `unauthorized` | Non-local environment or a disallowed origin |
| 409 | `client_submission_id_conflict` | Same submission id, different content |
| 409 | `client_capture_id_conflict` | A capture id already belongs to another submission |
| 413 | `payload_too_large` | Body exceeds `CONTACT_CAPTURE_INTAKE_MAX_BYTES` |
| 422 | `validation_failed` | Schema or semantic validation failed |
| 422 | `unsupported_contract` | A legacy or unknown contract was posted here |
| 504 | `timeout` | The submission exceeded its budget; everything rolled back |

## Legacy contracts

The campaign-era contracts still exist so previously staged batches and
snapshots remain readable, and so the transition is explicit rather than
implicit:

| Contract | Route | Status |
| --- | --- | --- |
| `salesnav-capture/1.0.0` | `/api/intake/sales-navigator/stage` | Legacy. Campaign-required staged import. The extension no longer produces it. |
| `linkedin-profile-capture/1.0.0` | `/api/intake/linkedin-profile/stage` | Legacy. Superseded by this contract. The extension no longer produces it. |
| `linkedin-company-capture/1.0.0` | `/api/intake/linkedin-company/stage` | Current. Company evidence is not a person; it keeps its own contract, now always with a null campaign. |

Posting a legacy body to `/api/intake/contact-captures` returns
`422 unsupported_contract` **naming the route it belongs to**. A legacy payload
is never reinterpreted as a contact-first submission: its idempotency keys may
already have been accepted under the old contract, so replaying it would either
conflict or split one person's evidence in two.

Local extension state is migrated on install and on browser start
(`src/common/migration.js`): campaign-era drafts are archived verbatim under one
key and can be downloaded, the live draft keys and stale staged-result summaries
are cleared, and the campaign preference is dropped. The panel shows a one-time
notice explaining exactly that.

## What a capture never does

Create a campaign or campaign membership · score or qualify · discover or verify
an email · research a company website · generate, approve, or schedule outreach ·
merge an ambiguous identity · remove or weaken a suppression · make any contact
outreach-eligible · reach the database from browser code.

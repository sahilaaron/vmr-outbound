# Contact-first capture contract — `linkedin-contact-capture/2.1.0`

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

Contacts are permanent. Campaigns are a later operating context over permanent
Contacts. **Campaign selection is optional and is never required to save a
person.** When selected, it requests a separate idempotent Campaign Contact
filing; it does not change Contact ownership or capture success.

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
GET {backend_base_url}/api/campaigns
```

`/api/contacts/lookup` answers **existence only** —
`{"match": "none" | "exact" | "ambiguous" | "unknown", "contact_count": n}` — so
the panel can label its primary action *Save Contact* or *Refresh Contact*
without pulling any contact field into the browser.

Gated behind `FEATURES__CONTACT_CAPTURE_INTAKE`, which is off by default. Two
deployment shapes are supported, and they differ only in what authorises the
request — the wire contract above is identical in both.

### Local development

Unchanged. No authentication, no credential header. Restricted to a loopback
origin or any `chrome-extension://` origin, and refused unless `APP_ENV=local`.

### Hosted

The four requests above are the **only** thing a hosted deployment authorises
for the extension. Each one must carry a VMR capture credential:

```
Authorization: Bearer vmrx1.<key_id>.<secret>
```

This is a VMR application credential issued per install. It is not the hosted
sign-in cookie, not a Google token, and not a Gmail grant, and an operator's
browser session does **not** authorise a capture in its place. It is revocable
server-side by `key_id`.

The capture `POST` must also come from an approved `chrome-extension://` origin;
the three reads must not come from an unapproved one. A credential presented
against any other path or method in the application authorises nothing.

Failures are distinguishable and actionable:

| Status | Meaning | What the operator does |
| --- | --- | --- |
| `401` | no usable credential — absent, malformed, wrong, or revoked | re-paste it in Settings, or ask for a new one |
| `403` | credential read, but this install is not approved | send the extension ID from Settings to be added |

The extension refuses to send a hosted request with no credential at all, so
nothing leaves the browser in that case. See `docs/HOSTED_AUTH.md` §7a in the
backend repository for issuing, approving and revoking.

Company evidence (`/api/intake/linkedin-company/stage`) is **not** in the hosted
contract and stays local-backend only.

## Request shape

| Field | Type | Meaning |
| --- | --- | --- |
| `schema_version` | const | `linkedin-contact-capture/2.1.0` |
| `client_submission_id` | string (8–128) | Idempotency key for the whole reviewed submission |
| `campaign_id` | UUID string \| null | Optional Campaign Contact filing; null means capture only |
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
    "submitted": 2, "created": 1, "refreshed_exact_match": 1,
    "exact_match_unchanged": 0, "staged_unmatched": 0, "staged_ambiguous": 0,
    "duplicate_in_submission": 0, "suppressed": 0,
    "labels_applied": 2, "notes_recorded": 2,
    "campaign_filings_applied": 2,
    "campaign_filings_pending": 0,
    "campaign_filings_failed": 0,
    "auto_resolved": 1
  },
  "results": [ { "client_capture_id": "…", "capture_id": "…",
                 "outcome": "exact_match_refreshed", "matched_contact_id": "…",
                 "contact_url": "…", "capture_url": "…",
                 "review_candidate_count": 0, "labels_applied": ["Healthcare"],
                 "campaign_filing": { "status": "applied",
                   "requested_campaign_id": "…",
                   "campaign_contact_id": "…" },
                 "warnings": [] } ],
  "operator_workbench_url": "http://127.0.0.1:8000/contact-captures/submissions/…"
}
```

### Outcomes

**Intake stores evidence; it does not create Contacts.** A submission always
persists permanent per-person capture evidence, and refreshes a Contact only
where it matches an exact LinkedIn identity the backend already knows. A person
the backend has not seen before stays **staged**: a `Contact` requires a company
domain, a LinkedIn page never shows one, and guessing is forbidden. Creating the
Contact is promotion's job, and it happens inside this request only when
automatic company-domain resolution is enabled and succeeds — reported by the
`auto_resolved` count. Otherwise the agent worker or an operator finishes it.

| Outcome | Meaning |
| --- | --- |
| `created` | A permanent Contact exists for this capture. Only reachable when in-request automatic domain resolution promoted it (see `auto_resolved`); unobserved name/company fields remain `null` and block dependent Agents |
| `exact_match_refreshed` | Exactly one contact carries this normalized URL; ≥1 field changed under the freshness policy |
| `exact_match_unchanged` | Exactly one match; evidence recorded, nothing newer than the current winners |
| `unmatched_staged` | No exact identity matched. Evidence is stored permanently and the person stays staged until promotion resolves a company domain — **no Contact is created** |
| `ambiguous_review` | More than one contact carries this URL. Surfaced, never merged |
| `duplicate_in_submission` | The same person appeared earlier in this submission. Evidence kept; reconciled once |
| `suppressed` | The matched contact is suppressed. Evidence linked, no canonical field touched, suppression untouched |

`rejected_invalid` is not an outcome: a rejected submission is never persisted,
so it is reported as an HTTP error instead of a stored row.

### Permanent evidence, unresolved fields

Capture always stores the permanent per-person evidence record, and updates a
Contact where one already matches exactly. It does not create a new Contact: a
LinkedIn page usually does not expose a company domain, inferring one from a
company name would be fabricated evidence, and a `Contact` cannot exist without
one. Unmatched people therefore stay staged rather than becoming a half-built
Contact. Where a value is genuinely missing the backend stores `null`; Company
and Email Agents remain blocked until later evidence resolves it. It never
creates placeholder names or domains merely to satisfy storage.

The extension plays no part in resolution: it never calls a domain provider,
never holds a provider key, and never claims an inferred domain was observed.

### Optional Campaign filing

When `campaign_id` is a UUID, the backend records a durable filing intent and
upserts one Campaign Contact for `(campaign_id, contact_id)`. Filing runs behind
a savepoint. Where no Contact exists yet the intent is held as
`campaign_filing.status: "pending"` and applied when promotion creates one. A
missing or archived Campaign produces a truthful
`campaign_filing.status: "failed"` while the capture — and any Contact it
matched — still commits. An identical submission replay cannot duplicate the
Campaign Contact. A value that is not a UUID is refused outright with HTTP 422
`validation_failed`.

## Collections (shown as Labels)

Collections classify permanent contacts. They are **not** Campaign membership
or an eligibility signal, and applying one can never make a contact
outreach-eligible or lift a suppression.

- The extension **requests** label names. The backend slugs them
  (`"Venture Capital"`, `"venture  capital"`, `"Venture-Capital!"` → `venture-capital`),
  finds or creates the canonical row, and assigns it.
- Assignment happens after the capture has a permanent, unsuppressed Contact.
  Ambiguous identity conflicts keep requested names on capture evidence until
  an operator resolves the person.
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

The selected Campaign is stored as a separate filing preference and survives
browser sessions. It is not embedded in capture drafts, so choosing another
Campaign never rewrites previously reviewed people.

## What a capture never does

Create a Campaign · require Campaign selection · score or qualify · discover or
verify an email · research a company website · generate, approve, or schedule
outreach · merge an ambiguous identity · remove or weaken a suppression · make
any contact outreach-eligible · reach the database from browser code.

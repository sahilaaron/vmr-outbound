# LinkedIn profile & company capture contracts (DAT-012A)

This document defines the versioned contracts between the extension and the
VMR backend for **operator-opened ordinary LinkedIn pages**:

| Contract | Version | Schema | Endpoint |
| --- | --- | --- | --- |
| Person profile capture | `linkedin-profile-capture/1.0.0` | `docs/profile-intake.schema.json` | `POST /api/intake/linkedin-profile/stage` |
| Company page capture | `linkedin-company-capture/1.0.0` | `docs/company-intake.schema.json` | `POST /api/intake/linkedin-company/stage` |

The existing Sales Navigator contract (`salesnav-capture/1.0.0`,
`docs/intake.schema.json`, `POST /api/intake/sales-navigator/stage`) is
**unchanged and backward compatible**. The three contracts use independent
namespaces so each can evolve without touching the others. Versioning rule for
all three: MAJOR bump on any breaking payload change; the backend rejects
unsupported MAJOR versions with `validation_failed`.

## Division of responsibility

The extension **reads facts** from the page the operator already opened and
stages a reviewed snapshot. The backend performs identity matching,
provenance/freshness resolution (DAT-005), suppression enforcement (DAT-006),
canonical updates, QA policy evaluation, and audit logging. Browser code never
updates a canonical contact.

## Person profile capture (`linkedin-profile-capture/1.0.0`)

One payload = one reviewed snapshot of one MAIN profile page
(`https://www.linkedin.com/in/<public-identifier>`). Sub-routes (details
overlays, activity feeds) are not supported surfaces.

Envelope:

- `client_capture_id` — client-minted stable capture id (≥ 8 chars, UUID in
  practice). This is the **idempotency key**: it stays the same across retries
  of the same reviewed draft and changes when the operator re-captures.
- `source_url` — the raw page URL at capture time (immutable provenance).
- `captured_at` — ISO-8601 capture timestamp; every nested observation also
  carries its own `observed_at`.
- `schema_version`, `source` — fixed identifiers (see schema).
- `extraction` — adapter/extension versions, `status` (`ok` | `partial`),
  `missing_sections`, operator `excluded_sections`, and `page_warnings`.
  A payload is only sendable from `ok`/`partial` states; challenge, login,
  unavailable-profile and unrecognized-structure states are never staged.

Profile fields (first release): normalized profile URL, public identifier
(when safely derivable), full name, headline, displayed profile location,
connection count (when visible; the raw token is preserved alongside the
parsed integer), open-to-work signal (when visible), capture timestamp,
extraction warnings, and the immutable raw top-card lines.

Experience entries are **nested, never flattened**: company name, company
LinkedIn URL and id, job title, timeline text, parsed start/end dates **only
when deterministic** (`dates_reliable`), duration text, employment type, role
location, workplace/location type, current-role indicator, per-entry warnings,
observed timestamp, and the immutable raw lines.

Location semantics: the person's `displayed_location`, each experience's
`role_location`, and a company's `headquarters_text` are three distinct facts
and are never substituted for one another.

## Company page capture (`linkedin-company-capture/1.0.0`)

One payload = one reviewed snapshot of one operator-opened company page
(`/company/<id>` or its About page). The extension **never navigates** to a
company page from a person profile — the operator opens it manually.

Company fields (when visible): name, normalized company LinkedIn URL and
identifier, website, industry, size range (verbatim bucket), displayed
employee count (raw + parsed), headquarters text (verbatim), founded year
(raw + parsed), specialties, capture timestamp, warnings, raw lines.

## Idempotency

Same `client_capture_id` + same payload content → the backend replays the
original result (`already_received: true`, HTTP 200). Same id + different
content → `client_capture_id_conflict` (409); the operator re-captures with a
fresh id. First accept → HTTP 201. Mirrors the Sales Navigator batch
idempotency contract.

## Identity rules (backend, first release)

An existing contact may be matched and automatically refreshed **only**
through an exact normalized LinkedIn profile URL match (or the equivalent
stable public identifier). Normalization is the backend's
`normalize_linkedin_url` applied identically to existing contacts, imports,
SalesNav rows and profile captures.

The following may produce **operator-review candidates only** and never
trigger automatic merging: matching name; name+company; name+title;
name+location; fuzzy URL resemblance; inferred identity. An unmatched profile
remains staged. An ambiguous profile (more than one contact carries the same
normalized URL) remains unresolved for review. The backend never silently
creates a duplicate or merges two people on weak evidence.

## Backend outcomes

The staging response reports a truthful `outcome`:

| Outcome | Meaning |
| --- | --- |
| `stored` | Snapshot persisted; reconciliation not yet enabled (DAT-012D baseline). |
| `exact_match_refreshed` | Exact URL match; at least one canonical field refreshed under freshness policy. |
| `exact_match_unchanged` | Exact URL match; evidence recorded; nothing newer than current winners. |
| `unmatched_staged` | No exact match; snapshot staged; weak candidates (if any) queued for review. |
| `ambiguous_review` | Multiple contacts share the normalized URL; operator review required. |
| `suppressed` | Matched contact is suppressed; evidence recorded; no canonical refresh, suppression untouched. |
| `rejected` | Payload failed validation; nothing persisted. |

A new profile observation never automatically: makes a contact
outreach-ready, verifies an email, removes a suppression, adds campaign
approval, or schedules outreach. Older observations never silently replace
newer evidence (DAT-005 freshness order).

Response body (both contracts):

```json
{
  "snapshot_id": "<uuid>",
  "client_capture_id": "<id>",
  "outcome": "stored",
  "already_received": false,
  "received_at": "<iso>",
  "warnings": [],
  "operator_workbench_url": "http://127.0.0.1:8000/..." 
}
```

Error body mirrors the Sales Navigator contract: `{ "error": "<code>",
"status": <http>, "details": [...] }` with codes `invalid_json`,
`validation_failed`, `campaign_invalid`, `client_capture_id_conflict`,
`payload_too_large`, `unauthorized`, `timeout`.

## Sensitive-data exclusions

The payloads never contain: credentials, cookies, tokens or auth headers;
browser storage contents; private messages or InMail; contact-info panel data
(emails, phones, addresses); connection lists; profile photos or media;
anything from pages the operator did not open. Raw values are limited to the
visible text lines of the captured card/sections. The extension never posts
to LinkedIn and only talks to the configured loopback backend.

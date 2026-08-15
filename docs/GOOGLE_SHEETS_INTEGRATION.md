# Google Sheets integration

A Google Sheets add-on that takes rows containing a person's name and their
company and gives back a verified email address and the canonical seven-message
sequence, using the existing VMR Outbound product for all of it.

Feature switch: `FEATURES__GOOGLE_SHEETS_INTEGRATION` (off by default).
Configuration block: `SHEETS__*`. Add-on source: `integrations/google-sheets/`.

---

## 1. It is a thin client, and that is the whole design

The spreadsheet is an **intake and output surface**. It is not a second
intelligence system and it is not a system of record.

```
Google Sheet rows
  -> POST /integrations/sheets/batches
     -> permanent Contact + Campaign membership   (existing services)
     -> durable Agent pipeline                    (existing worker)
        Identity -> Company -> Research -> Email -> Verification
                 -> Insights -> Personalization
  -> POST /integrations/sheets/results
     -> verified address + seven messages written back into the sheet
```

Nothing about research, company-domain policy, email discovery, verification,
insight generation or message generation exists in Apps Script. The add-on
detects columns, mints row keys, posts rows, and writes answers back.

VMR Outbound remains authoritative for Contacts, Companies, Campaign membership,
research, evidence, verification, Insights, sequence generation and provenance.
**Deleting the spreadsheet deletes nothing.** The rows it submitted became
permanent Contacts, and they stay.

## 2. What a sheet must contain

Required per row: **First Name**, **Last Name**, **Company Name**.

Optional and never required: Job Title, LinkedIn URL, Context.

Not required and not accepted as input: company domain, email address, research.
The product derives the domain itself (§7) and discovers and verifies the address
itself.

Headers are detected, not assumed. The add-on scans the first ten rows for the
best-matching header row and maps common spellings (`Surname`, `Organisation`,
`Given Name`, …). Column letters are never hard-coded, and a sheet whose header
row is row 4 works exactly like one whose header row is row 1.

## 3. What it writes back

| Column | Contents |
| --- | --- |
| `VMR Status` | Pending / Processing / Ready / Could not prepare |
| `Email Address` | The verified address, when there is one |
| `Email 1` … `Email 7` | `Day <n> — Subject: <subject>` then a blank line then the body |
| `VMR Note` | The reason a row stopped, or what it is waiting on |
| `VMR Last Updated` | When the add-on last wrote to that row |
| `VMR Contact ID`, `VMR Campaign Contact ID` | For cross-referencing in the app |
| `VMR Row Key` | Hidden. Row identity — see §6 |

Deliberately **not** written: company dossiers, Insights JSON, evidence
internals, job ids, provider names, stage names or policy versions. A sales
operator opening this sheet should see an address and seven messages, and the
detail stays one click away in the app where it has a surface built for it.

Existing VMR columns are reused wherever the operator moved them; new ones are
appended after the operator's own data. **No input column is ever written to.**

## 4. Row states

Four words, and deliberately not the nine Agent names:

- **Pending** — accepted, waiting its turn.
- **Processing** — an Agent currently holds it.
- **Ready** — a usable verified address **and** a complete validated
  seven-message sequence. Both halves, always: an address with no sequence is not
  usable, and seven messages addressed to an unverified mailbox are worse than
  nothing.
- **Could not prepare** — it stopped and a person has to do something. The reason
  is written into `VMR Note`, sanitized (§10).

There is no approval step. This surface produces text for a person to use; it
creates no draft, no schedule and no send, so an approval gate here would protect
nothing. Approval remains where it means something — before a Gmail draft, in the
app.

"Ready" is decided by existing policy, not by this surface: the address must be
`VALID` under `app/services/verification` (catch-all, unknown, role-based and
vendor-claimed addresses are all not ready), and the sequence must be
`generation_status = COMPLETE`, not `validation_status = FAILED`, and exactly
seven messages.

## 5. Account linking

The add-on presents a **Google-signed OpenID Connect ID token** for the person
running the sheet, minted fresh by `ScriptApp.getIdentityToken()` on every
execution.

There is deliberately no API key field. A key pasted into a spreadsheet travels
with every copy of that spreadsheet, and the person who copies it is not always
the person who pasted it.

The backend, in order:

1. verifies the RS256 signature against Google's published key set
   (`app/core/auth/jwks.py`, the same verifier the browser sign-in uses);
2. checks `iss`, `exp`, `iat` and `email_verified`;
3. checks `aud` against `SHEETS__ALLOWED_AUDIENCES` — the confused-deputy check,
   and the one that stops a valid Google token minted for somebody else's
   application being replayed here;
4. resolves the identity to an **active** `users` row through
   `users.resolve_google_identity`, the same function the browser sign-in uses;
5. evaluates Campaign access from that account's current role and assignments.

Properties this buys:

- **Nothing durable exists to steal.** No token row, no digest, no refresh
  secret, nothing written into a cell, a log or a script property.
- **It expires on its own**, in about an hour, without this application operating
  an expiry schedule.
- **Revocation is the account.** The owning row is re-read on every request, so
  disabling an account stops the add-on on its next call.
- **Narrow reach.** The credential is accepted on three paths and nowhere else.

Google scopes requested: `spreadsheets.currentonly` (the open document only, not
Drive), `script.container.ui`, `script.external_request`, `openid`,
`userinfo.email`. **No Gmail scope and no Drive scope.**

Trade-off, stated plainly: there is no *per-account* revoke for this surface
specifically. Stopping one person means disabling their VMR account; stopping
everybody is one switch on the Admin screen (§5a). That is deferred, not
overlooked — see §13.

### 5a. The switch an administrator actually holds

`google_sheets_integration` is an operator control, not an environment-only flag:
it appears on `/admin` under **Operator interface** as "Google Sheets add-on", and
every route reads it through `operations.settings.enabled`, so turning it off
takes effect on the next request with no deploy and no restart. A control the
routes ignored would be worse than no control at all.

It is unavailable — shown with the reason rather than as a broken toggle — until
two things are true: `SHEETS__ALLOWED_AUDIENCES` is configured (that value is a
security boundary and deliberately has no write path from any screen), and
`email_sequences` is on, because without sequences no row could ever reach Ready
and accepting work this surface cannot finish would be a lie told politely.

## 6. Row identity and idempotency

A spreadsheet row number is not an identity. Sorting renames every row at once,
and a result written back by position lands on the wrong person.

So every row carries an opaque key in the hidden `VMR Row Key` column, minted
once, before the request is sent. Writing keys before sending is what makes a
timed-out request recoverable: the sheet can still identify its own rows, and a
retry presents the same keys.

The server derives the enrolment idempotency key rather than trusting one:

```
sha256( installation_id | spreadsheet_id | sheet_id | campaign_id | client_row_id | generation )
```

length-prefixed before hashing, so two different identities cannot collide by
shifting a separator. `installation_id` is per user per install, so two people
sharing one spreadsheet cannot collide on each other's rows.

Submitting the same rows twice:

- the key is found on an existing `campaign_contact_sources` row;
- the existing membership is returned, marked `already_submitted`;
- **no Contact is considered, no provider is called and nothing is spent.**

`generation` is the deliberate escape hatch: incrementing it changes every
derived key, so an operator can ask for the same row to be prepared again on
purpose. It reaches the same membership through a second provenance record — not
a second Contact.

Existing product idempotency is unchanged and is what actually enforces this:
`campaign_contacts.enrol_contact` idempotency, the
`(campaign_id, contact_id)` unique constraint, and the pipeline's own
`stage_job_key`.

## 7. Company domain, without a domain column

The Company Agent needs `contact.company_domain` and exactly one permanent
`Company` with that domain. The capture path satisfies this through
`app/services/resolution`; a spreadsheet row reaches the same place through
`app/services/integrations/sheets/companies.py`, which reuses the same policy,
the same provider client and the same Company writer:

1. Evidence is assembled from domains an operator has already confirmed for that
   name (`prior_confirmed_domains`) and from permanent Companies whose own domain
   is established (`resolution.store.company_state`).
2. `resolution.policy.evaluate` — pure and unchanged — decides.
3. If nothing established it, at most **one** logo.dev lookup per distinct
   company name, gated on `automatic_company_domain_resolution` and
   `salesnav_domain_enrichment` as read through the operator-controlled resolver.

**Only `CONFIRMED` is accepted. `PROVISIONAL` is refused.** On the capture path a
provisional domain is safe because it is recorded in `company_domain_resolutions`
and the downstream gates read it; that ledger is keyed per capture, and a
spreadsheet row is not a capture. A provisional domain accepted here would create
a Company indistinguishable from an established one — domain laundering — so the
state is refused outright and the row says so.

Cost: a name that resolves creates a permanent Company, so every later row naming
that company is answered with no provider call at all. The batch ceiling bounds
what one click can buy.

## 8. Async model

The add-on never holds a request open while the pipeline runs.

**Submit** is bounded, deterministic database work plus at most one brand-matcher
lookup per distinct new company name. It returns one identifier per row.

**Refresh** asks about the identifiers the sheet already holds, skipping rows that
are already Ready, chunked against the server's own stated `max_result_ids`.

Everything expensive happens afterwards in `scripts/run_agent_worker.py`. **The
worker must be running** or rows stay Pending forever. The operator can close the
spreadsheet and come back tomorrow.

## 9. API

All three refuse with `404` when the feature switch is off, before any credential
is read.

### `GET /integrations/sheets/campaigns`

```json
{
  "schema_version": "google-sheets-batch/1",
  "account": { "email": "...", "display_name": "..." },
  "limits": { "max_batch_rows": 50, "max_result_ids": 200, "max_context_chars": 1000 },
  "campaigns": [{ "id": "...", "name": "...", "status": "active", "execution_enabled": true }]
}
```

### `POST /integrations/sheets/batches`

```json
{
  "campaign_id": "uuid",
  "installation_id": "...",
  "spreadsheet_id": "...",
  "sheet_id": "0",
  "generation": 1,
  "rows": [
    { "client_row_id": "k1", "first_name": "...", "last_name": "...",
      "company_name": "...", "job_title": "...", "linkedin_url": "...", "context": "..." }
  ]
}
```

Response: `batch_id`, `counts`, and per row `client_row_id`, `status`,
`submission_id`, `contact_id`, `already_submitted`, `safe_failure_reason`,
`failure_code`.

`400` for a request the contract refuses whole — an oversized batch, a duplicate
`client_row_id`, a missing identifier. A row-level problem is **never** a 400: it
comes back as that row's `could_not_prepare`, so one bad cell never costs the
operator the rest of their selection.

### `POST /integrations/sheets/results`

```json
{ "submission_ids": ["uuid", "..."] }
```

Response rows carry `status`, `email_address`, `messages` (only when ready, and
then always exactly seven), `safe_failure_reason`, `note`, `updated_at`.

A `GET /batches/{id}` was considered and rejected: it needs a batch table this
integration does not otherwise need, and the add-on already holds one identifier
per row because it has to. Asking about exactly those identifiers is both the
smaller server and the more precise question, and it refreshes a partly-finished
sheet without re-reading rows that are done.

An identifier this account cannot reach is **omitted**, not refused. Telling a
caller that an id exists but belongs to somebody else turns a result set into an
enumeration oracle.

## 10. Safety properties

- **Suppression is asked before anything is created.** A suppressed identity
  leaves no Contact and no membership behind, and the ledger is untouched.
- **No merging.** An exact normalized LinkedIn profile URL may match an existing
  Contact; the deterministic name-plus-domain natural key may. Two candidates is
  an ambiguity the operator resolves — the row is refused rather than merged.
- **A spreadsheet fills blanks and overwrites nothing.** Re-submitting a row can
  never undo work done in the product.
- **A spreadsheet URL is not an observation.** A LinkedIn URL typed into a cell
  is stored on the Contact but creates no `linkedin_identity_links` row, so a typo
  cannot become permanent matching authority.
- **Context is labelled, not laundered.** The optional Context cell is stored as
  `{"kind": "operator_supplied", "verified": false}` in the membership's
  provenance. It is never a sourced fact, has no URL and no retrieval time, and
  cannot be cited later as research.
- **Failure text is sanitized** through `workbench_agents.sanitize.sanitize_text`
  before it reaches a cell, because a spreadsheet is a shared file. A provider
  key in an error string is redacted, not written.
- **No sending, ever.** No Gmail scope, no draft, no schedule, no reply
  detection. The Sending Agent has no production adapter, which is where the
  guarantee actually comes from.
- **Nothing bypasses a gate.** Campaign live opt-in, Agent controls, provider
  authorization, verification usage accounting, worker concurrency and the
  evidence rules all belong to the code this surface calls.

## 11. Scale posture

The first UAT is one user and one sheet. Nothing here forecloses 100–500:

- every request is **stateless**; no per-user in-memory job map exists;
- durable work stays in the existing PostgreSQL Agent queue — no Redis, no Kafka,
  nothing new;
- **bounded** batches and bounded result reads, both configured, both refused
  whole when exceeded;
- **idempotent** by derived key, so client retries are free;
- authorization is evaluated per request from the current account;
- provider spend stays attributable through the existing usage ledger and Agent
  jobs.

What 500 users would additionally need — per-account rate limiting, a job-level
fair-share across accounts, published Marketplace distribution — is listed in
§13 and deliberately not built.

## 12. Installing for the first user

See `integrations/google-sheets/README.md` for the exact steps. In outline:

1. Turn on `FEATURES__GOOGLE_SHEETS_INTEGRATION` and restart.
2. Create the Apps Script project bound to the spreadsheet, attach it to the
   deployment's Google Cloud project, and push `integrations/google-sheets/src/`.
3. Open the sidebar; it shows the `aud` of the token it actually mints.
4. Put that value in `SHEETS__ALLOWED_AUDIENCES` and restart.
5. Make sure `scripts/run_agent_worker.py` is running.

Marketplace publication is **not** required for the first user.

## 13. Deliberately deferred

| Item | Why |
| --- | --- |
| Per-account revoke for this surface alone | The credential is minted by Google per execution and never stored, so there is nothing to revoke that disabling the account does not already stop, and the whole surface has an administrator switch (§5a). A per-account revoke list would be a new table for a guarantee already held twice over. |
| A batch table and `GET /batches/{id}` | Schema for a question the id-list endpoint answers more precisely. |
| Per-account rate limiting | The batch ceiling bounds one request; a per-account budget across requests is a 100-user question with no data behind it yet. |
| Marketplace publication, admin install, billing | Not needed for the first user, and each is a distribution decision rather than a product one. |
| Writing dossier / Insights columns | The output contract is an address and seven messages. Anything else belongs on a surface built to explain it. |
| Editing a message in the sheet and sending it back | Sequence messages are immutable versions with a review path in the app; a second editing surface would be a second authority. |
| Scheduling and sending from the sheet | Out of scope by design; see `docs/GOAL.md`. |
| `resolution.service._existing_company_matches` prefilter | Its SQL `LIKE` prefilter uses the first six characters of the *space-stripped* folded name, so `"Kiln Systems"` never matches its own permanent row. On the capture path that is only a missed cache; the Sheets path works around it locally rather than changing shared capture behaviour. Recorded in `POST_LAUNCH_BACKLOG.md`. |

## 14. Where the code is

| Concern | File |
| --- | --- |
| Credential rules | `app/core/auth/sheets_assertion.py` |
| Anonymity classification | `app/core/auth/policy.py` |
| Settings | `app/core/sheets_config.py` |
| Routes | `app/api/integrations_sheets.py` |
| Wire contract (pure) | `app/services/integrations/sheets/contract.py` |
| Account resolution | `app/services/integrations/sheets/identity.py` |
| Company from a name | `app/services/integrations/sheets/companies.py` |
| Submit | `app/services/integrations/sheets/submit.py` |
| Results read model | `app/services/integrations/sheets/results.py` |
| Add-on | `integrations/google-sheets/` |
| Backend tests | `tests/test_google_sheets_integration.py` |
| Add-on tests | `integrations/google-sheets/test/` |

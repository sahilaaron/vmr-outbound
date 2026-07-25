# LinkedIn profile & company capture — acceptance (DAT-012H)

Two layers of acceptance evidence for the DAT-012 epic:

1. **Sanitized backend acceptance (completed, reproducible)** — run against a
   live local backend with committed sanitized fixtures. No LinkedIn contact,
   no real profile data, no credentials. Reproduce with the commands below.
2. **Authenticated operator pass (manual, Sahil)** — the in-browser checks only
   a logged-in operator can perform. The extension is operator-controlled; this
   runbook never authorizes unattended LinkedIn automation.

Sensitive-data policy: fixtures use fictitious people/companies; the one live
operator pass uses the operator's own judgment about which profile to open, and
**no credentials, cookies, auth headers, browser storage, private messages,
contact-info panel data, or unnecessary raw personal information may be pasted
into evidence**. Redact names/URLs from anything committed.

## Layer 1 — sanitized backend acceptance (evidence)

Environment: local Postgres (fresh `vmr_accept` DB, `alembic upgrade head`),
app started with `FEATURES__SALESNAV_INTAKE=true`,
`FEATURES__LINKEDIN_PROFILE_INTAKE=true`, `FEATURES__LINKEDIN_PROFILE_REFRESH=true`,
`FEATURES__LINKEDIN_COMPANY_INTAKE=true`, `FEATURES__WORKBENCH=true`.
Seed: one contact ("Morgan Vale", stale title `Operations Manager`,
`linkedin_url` stored with mixed case + query string) and one same-name decoy
contact with no LinkedIn URL. Payloads: the committed
`extensions/salesnav-capture/docs/fixtures/profile.payload.example.json` /
`company.payload.example.json` with fresh `client_capture_id`s.

| # | Scenario | Result |
| --- | --- | --- |
| 1 | Existing SalesNav listing capture still works | `POST /api/intake/sales-navigator/stage` staged 2 records, returned `/imports/<batch>` workbench link; all 54 pre-existing extension tests and the DAT-009 backend suite unchanged and passing |
| 2 | Manually opened profile can be captured | `POST /api/intake/linkedin-profile/stage` → 201, snapshot stored verbatim with nested experience observations |
| 3 | Exact URL match refreshes the correct contact | Outcome `exact_match_refreshed`; seeded contact's title `Operations Manager` → `Director of Operations` under freshness-v1; same-name decoy untouched |
| 4 | Unchanged recapture is idempotent | Identical payload replay → HTTP 200, `already_received: true`, same `snapshot_id`, no second snapshot |
| 5 | Older evidence cannot replace newer | Back-dated capture (2019) with a different title → `exact_match_unchanged`; title kept its newer value |
| 6 | Suppression remains authoritative | With an active `email opt_out`: outcome `suppressed`; evidence linked; no field refreshed; suppression untouched |
| 7 | Weak matches never merge | New-URL capture matching two contacts by name/name+company → `unmatched_staged`, review candidates with `auto_merge: false`, zero contacts created/changed |
| 8 | QA policy produces versioned recommendation | `profile-employment-qa/1.0.0` evaluation stored with outcome, reason codes, signals+thresholds, evidence refs, explanation, recommended action |
| 9 | Operator-opened company page can be captured | `POST /api/intake/linkedin-company/stage` → 201 with deterministic HQ parse and `/company-profiles/<id>` record page (renders 200) |
| 10 | Recoverable failure preserves the reviewed draft | Extension keeps the reviewed draft + `client_capture_id` on every recoverable send failure and offers Retry (idempotent); proven by `test/profile-handoff.test.js` + service-worker send paths; in-browser confirmation is step L6 below |

Automated backstops at this branch head: backend `pytest` 463 passed (incl.
`test_linkedin_profile_intake.py`, `test_profile_refresh.py`, `test_qa_policy.py`,
`test_linkedin_company_intake.py`); extension `node --test` 109 passed; `alembic`
upgrade/check/downgrade round trips clean on a fresh database.

## Layer 2 — authenticated operator runbook (manual, ~15 min)

Prereqs: local backend running with the four feature switches above; extension
loaded unpacked; you are logged in to LinkedIn in the same Chrome profile.
Every step is an explicit operator action.

- **L1 (scenario 1).** Open a Sales Navigator people-search results page →
  panel shows *Sales Navigator Listings* → Capture visible records → review →
  Send → staged batch opens in the workbench Imports list.
- **L2 (scenario 2).** Open the MAIN profile page (`linkedin.com/in/…`) of one
  contact that already exists in your workbench → panel shows *LinkedIn Person
  Profile* → "Read this profile page" → verify name/headline/location/current
  role/experience count/connections/open-to-work and warnings match what you
  see on the page. Scroll the page and recapture if Experience shows missing.
- **L3 (scenarios 3+8).** Send → response shows `exact_match_refreshed` (or
  `exact_match_unchanged`) → "Open snapshot record" → check the refresh summary
  and, on the contact, the refreshed field(s) and the QA evaluation outcome.
- **L4 (scenario 4).** Press Send again without recapturing → "already
  received — idempotent", same snapshot id.
- **L5 (scenario 7).** Open a profile that is NOT in your data → Send →
  `unmatched_staged`; confirm any same-name candidates appear as review-only
  and no contact was created.
- **L6 (scenario 10).** Stop the backend, press Send → clear failure, draft
  and review stay intact → restart the backend → Retry → succeeds with the
  SAME capture id.
- **L7 (scenario 9).** Open the company's page yourself (the extension will
  not navigate there) → *LinkedIn Company Profile* mode → capture on the About
  page → verify firmographics → Send → open the company snapshot record.
- **L8 (challenge surface).** If LinkedIn ever shows a login/checkpoint during
  the pass, confirm the panel switches to *Challenge / Login Required* and no
  capture is possible until you resolve it yourself.

Record PASS/FAIL per step (redacting profile names/URLs) and file defects as
GitHub issues against epic #139 before closing #147.

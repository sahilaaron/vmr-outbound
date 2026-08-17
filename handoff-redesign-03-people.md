# Handoff — Redesign slice 3: People and Companies as records

**Branch:** `redesign/03-people-companies` · based on current `origin/main` `79467032` (merged Slices 1–2 + PR #294) via history-preserving merges `973fc690` (repaired Slice 2) and `be73a9eb` (current main) · merge after PR 2
**Spec:** VMR_OUTBOUND_UX_IA_PASS2.md sections D.5–D.6, F.3, Phase 5. Locked with Sahil: the person page is a record, not an email surface; emails are read and acted on in the Campaign.

## What changed

**People (`/app/people`)** — permanent records, no Campaign required. Columns Person · Company · Email status · Campaigns (count) · Added. Checkbox selection with **Add to Campaign** (`POST /app/people/add-to-campaign` → the existing `campaign_contacts.enrol_contacts`; the target must be Draft/Active/Paused, and enrolling never makes anyone outreach-eligible). `?campaign=` preselects the target, which is what the Campaign's *Add people → Choose existing people* card links to (fourth card, added here).

**Person (`/app/people/{id}`)** — identity (name, title, company link, email + status, LinkedIn, website label), **Campaigns** table (each membership: Campaign, lifecycle, outcome — Processing / Ready for Sending / Could not prepare with the plain reason, progress "n of 7 actioned" for ready people, **Open in Campaign** deep link into the sending desk), *add to another Campaign* select, **About** (what we know, from canonical fields + Company Intelligence + dossier facts, values only), **Sources**, **Activity**. The seven emails, approve/discard controls and the Agent ledger are gone from this page; the admin's **Diagnostics** button (`/admin/contacts/{id}`) keeps the raw view one click away.

**Companies (`/app/companies`, `/app/companies/{id}`)** — Website label (Confirmed / Best available / Missing, from the domain-resolution state), People, Active Campaigns, Knowledge updated; the company page shows *What we know* with provenance tags, People, Campaign impact (how many people in which Campaigns), Sources and changes.

`app/services/people_workspace.py` is the projection (campaign counts, membership rows, website labels, facts, campaign impact). Legacy `/app/contacts*` URLs still 308-redirect to `/app/people*` (slice 1).

**Follow-up after the Slice 2 repair merge** — `_sequence.html` had no embedder left and is deleted; `sequence.js` keeps only the `full` copy kind the desk emits; `tests/test_review_copy_contract.py` (repaired in Slice 2) names `_desk.html` as its required surface instead of `contact.html`; `docs/EMAIL_SEQUENCE.md` §12 says where availability states surface now.

## Validation
- ruff / ruff format / mypy clean.
- Route authorization: 101 user-reachable (100 + `POST /app/people/add-to-campaign`).
- Tests that assumed the person page carried the seven emails were repointed at the desk or removed with the controls (`test_v2_customer_ui`, `test_customer_operating_model`, `test_v2_beta1_operator_ui`, `test_email_sequence_web`, `test_email_sequence_defects`, `test_gmail_draft_integration`): 29 retargeted, 12 deleted; all six files green (168 + 56 + 74 passed).
- Screenshots reviewed: People list with selection, Person with Campaigns table and About, Companies, Company.

- On the main-reconciled head `be73a9eb`: 16-file batch (customer UI, operating model, beta1, sequence web/defects, Gmail, sending desk, copy contract, route auth, extension account linking, extension capture auth, hosted auth, CI effective control, production hardening, resolution, review) **1020/1023**; the 3 failures are `/readyz` tests that fail identically on pristine `79467032` in the same environment (Windows psycopg-async).

## Notes for review
- No schema change.
- `app/core/http.py`, `app/web/extension_link_routes.py`, `app/main.py`, `tests/test_extension_account_linking.py` are byte-identical to `origin/main` — the PR #294 consent-CSP behaviour is untouched.
- A sequence whose generation/validation FAILED shows as Processing on the person page (it never becomes Ready); the failure detail is on Admin › Diagnostics. Same as slice 1's rule that customer pages never name Agents.

## Proposed tracker payload
| Field | Value |
| --- | --- |
| Item | UX Pass 2 — slice 3: People and Companies as records |
| Branch | `redesign/03-people-companies` @ head (stacked on slice 2) |
| State | Built; targeted tests green; awaiting PR + CI + review after slice 2 |
| Risk | Read-mostly; one write (`add-to-campaign`) reuses the existing enrolment service; no schema |
| UAT | People → select → Add to Campaign → Person shows the membership → Open in Campaign lands on the desk; Company shows website label + people |

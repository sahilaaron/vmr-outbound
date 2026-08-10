# Post-launch backlog (do not build before the first-campaign review)

This is a holding place for useful ideas surfaced during development that are
**out of scope** for the first 100-contact campaign. Recording an item here does
**not** authorize building it. Moving anything into launch scope requires an
explicit update to `GOAL.md` (see the Scope-Change Rule there and the Backlog
Admission Test in `GITHUB_BACKLOG.md`).

The canonical parked list lives in `GITHUB_BACKLOG.md` (P1/P2 cards and the
`FUT-*` parked backlog). This file only captures ideas that come up mid-build so
they are not lost or implemented opportunistically.

## Captured during Phase 0

- **psycopg 3.3.x compatibility.** We pinned `psycopg[binary]<3.3` because 3.3.x
  returned text columns as bytes and broke the SQLAlchemy dialect in local
  testing. Revisit the pin once a fixed 3.3.x / SQLAlchemy combination is
  available. (Engineering hygiene, not launch-blocking.)
- **Structured application logging.** Phase 0 ships without a logging framework.
  Add safe, secret-free structured logs when the first background jobs land
  (OPS-003), not before.
- **Health/readiness for external providers.** `/ready` currently checks only the
  database. Extend the system-health view to provider reachability when
  MillionVerifier/Saleshandy adapters exist (VER-006 / SHY-*).

## Captured during Phase 1 (Data & Campaigns, first slice)

- **Company entity and company-level dedup.** This slice normalizes company
  name/domain on the contact but has no `companies` table. Introduce one when
  company-contact saturation controls (CMP-004) or company insights (INS-*) need
  a shared company record. (DAT-004 full.)
- **Uncertain-match review queue.** Ambiguous natural-key matches are currently
  kept separate (a possible false duplicate, never a wrong merge) with an
  explanatory note. Add a human review/reconciliation queue when real import
  volume shows it is needed. (DAT-004.)
- **Immutability enforcement at the database.** `import_rows.raw_data` is treated
  as write-once by convention. Add a DB trigger/rule to hard-enforce immutability
  if a later requirement demands it.
- **Country and title canonicalization.** Normalization stays conservative (no
  synonym maps). Add curated country/title canonicalization only if scoring or
  targeting proves it necessary.

Add new items as `- **Title.** One or two sentences, and which real trigger
would justify building it.`

- **Operator UI for resolving profile-snapshot review candidates.** Weak-match
  and ambiguous snapshots (DAT-012E) are stored with review candidates but the
  workbench only displays them; add confirm/reject actions (reusing the
  DAT-004 identity-resolution flow) when real captures produce enough review
  volume to justify it.
- **QA-policy threshold configuration surface.** profile-employment-qa/1.0.0
  thresholds are code defaults recorded per evaluation; expose them as
  operator settings only if the pilot shows the defaults misfire.
- **Contact-side company linking from profile captures.** Experience
  observations carry company LinkedIn URLs/ids; linking them to the companies
  table (beyond DAT-012G's evidence matching) is deferred until scoring or
  research needs it.
- **About/skills/education capture on person profiles.** First release
  captures top card + experience only; add further sections when a concrete
  scoring or drafting need exists.

## Deferred by DAT-013 (contact-first acquisition)

Deliberately out of the contact-first refactor. Each is a real idea; none was
needed to make acquisition independently useful, and several would have weakened
the truthful-extraction standard if forced in.

| Idea | Why deferred |
| --- | --- |
| Creating a canonical Contact directly from a capture | A contact requires a company domain, which a LinkedIn page never shows. Inventing one would be fabricated evidence. Tracked as DAT-014, behind domain resolution. |
| Education observations | The main profile page's education block has no fixture coverage and no proven selector strategy. Adding it blind would ship a parser that fails silently. |
| Visible contact information (websites, public email/phone) | LinkedIn renders these behind the contact-info modal. Opening it is UI automation the safety rules exclude; there is no reliable already-rendered source. |
| Recent activity capture | Reliable activity capture needs navigation to the activity feed. The extension never navigates, so this stays deferred rather than half-implemented. |
| Individual Sales Navigator lead page as a capture surface | Investigated and NOT added: it would need a new host permission scope, and the lead page exposes no canonical `/in/` URL, so every capture would stay unmatched — the same outcome the results-row path already produces safely. |
| Label management UI (rename, merge, delete, colour) | The extension must not become a taxonomy manager. The backend owns the registry; management belongs in the workbench if real use proves the need. |
| Bulk label/note editing across saved contacts | No evidence of the need before the first real acquisition run. |

## Deferred by DAT-020A (Sales Navigator resolving alias)

| Idea | Why deferred |
| --- | --- |
| **DAT-020B — cross-tab redirect provenance** | Attaching a redirected vanity capture to the Contact created from a member id needs evidence the extension currently discards: that it opened alias for member `X` in tab `T`, and the capture happened in `T` after that redirect. Without it there is no deterministic link, and name/company bridging is forbidden. Full paste-ready issue in [`DAT_020B_FOLLOW_UP.md`](DAT_020B_FOLLOW_UP.md). |

## Deferred by RES-001 (Research Agent)

| Idea | Why deferred |
| --- | --- |
| **Script workers — plug `.py` collectors into the Research Agent** | The worker seam ships in RES-001, so a new source is already a module rather than a pipeline change. Running *arbitrary registered scripts* as workers is the v2 step. Design, mapping table and the three blocking questions are in [`RESEARCH_WORKERS.md`](RESEARCH_WORKERS.md#planned-script-workers-v2-not-built). The blockers are real: both candidate scripts invoke the Claude CLI, which makes their output an untrusted AI *interpretation* rather than a sourced fact, and that belongs behind AIC-002 / #181 in MVP-02, not inside a deterministic research stage. |
| Playwright / JS-rendered site fallback | A headless browser is a heavyweight dependency and was not needed to read the first sites. A JS-only site currently reports insufficient evidence, truthfully, rather than silently escalating. Revisit only if real pilot data shows it blocking usable companies. |
| Contact-level research | RES-001 researches the permanent Company, which is where reuse across Contacts and Campaigns comes from. Per-person research has no demonstrated need before the first campaign. |
| Promoting sourced facts onto canonical Company fields | Research stores claims with evidence; it does not overwrite canonical values. Turning a sourced fact into a canonical field is a separate, reviewable decision with its own provenance rules (`CompanyFieldSource.RESEARCH_DOSSIER` already exists for it). |
| Generalised Agent parent/child jobs | The orchestrator's parent/child machinery is hard-coded to the EMAIL to VERIFICATION pair. RES-001 needs no children, so generalising it stays unbuilt until a second Agent actually needs one. |

## Deferred by IMP-001 (campaign contact file import)

Each is a real idea. None was needed to make a campaign-bound Apollo import
independently useful, and several would have required guessing about a person to
force in — see [`CAMPAIGN_FILE_IMPORT.md`](CAMPAIGN_FILE_IMPORT.md).

| Idea | Why deferred |
| --- | --- |
| Operator-driven column mapping on the campaign path | The Apollo reader recognizes headers by name, so nothing has to be mapped. A file it does not recognize is refused with the exact missing headers named, and the existing generic mapped importer at `/imports` still handles arbitrary schemas. Adding a mapping UI here before a second real schema exists would be a screen with one supported answer. |
| Additional vendor schemas (ZoomInfo, Lusha, Cognism) | The reader is a schema *profile* — an alias table plus a row reader — so a second vendor is a new profile rather than a new pipeline. Nothing is built until a real file exists to test against; guessing at another vendor's column names is exactly the kind of silent mis-mapping this design refuses. |
| Delimiter sniffing for semicolon/tab CSVs | Deliberately not attempted. A mis-sniffed delimiter produces a single-column file that fails header recognition anyway, and the actionable "missing required header" message is a better outcome than a heuristic that is wrong occasionally and invisibly. |
| Resolving review-required rows in the customer UI | Held rows are recorded with their reason and shown on the batch page. Deciding an ambiguous identity is the existing DAT-004 review path, and giving it a second, campaign-scoped entry point would be two places to make the same decision. |
| Promoting a secondary address to primary | Retained with full provider metadata and never promoted. Which address to write to is a judgement about a person; the file does not license anyone to make it, and a malformed primary with a valid secondary is flagged rather than swapped. |
| Re-running the imported-email path after an operator edits an address | The Email stage reads the imported record only while it still matches the Contact's current address. A deliberate operator change therefore falls back to ordinary discovery, which is correct but silent; an explicit re-import or refresh action is the follow-up. |
| Per-user import ownership | The application is single-operator: an import is scoped to a Campaign, not a person, and the page says so. Real ownership arrives with the user-account system, not before it. |
| CSV export of row errors | The batch page shows every row's outcome. An export needs the formula-neutralization path exercised end to end (`neutralize_formula` exists and is tested) and a decision about who may download PII; neither was needed to run the first import. |

## Deferred by the hosted-operator authentication slice

The boundary itself is in [`HOSTED_AUTH.md`](HOSTED_AUTH.md) and
[`decisions/0011-hosted-operator-authentication.md`](decisions/0011-hosted-operator-authentication.md).
Each idea below is real. None was needed to let an approved operator reach `/app`
and `/admin` on the staging hostname, which is the whole of the authorised slice.

| Idea | Why deferred |
| --- | --- |
| **Per-operator write attribution** | The strongest follow-up, and the one this slice makes possible for the first time. Every write still records the constant `OPERATOR_ACTOR`, even though a verified identity is now on every request. Doing it properly means deciding what happens to the existing rows, whether the actor is the email or a stable subject id, and how the Agent worker (which has no session) attributes its own writes — three decisions with no forcing need before the first campaign. |
| Roles separating `/admin` from `/app` | Both surfaces have identical access semantics today and the same people use both. One role gets added the day a real distinction exists, and not to anticipate one. |
| A database-backed session store with server-side revocation | Revocation is already immediate via the allow-list re-check, and the stateless cookie keeps authentication independent of the database. Revisit only if a *stolen* cookie surviving to its 12-hour expiry becomes an actual concern — e.g. more operators, or a shared machine. |
| An operator-management screen | The allow-list is two or three addresses in `/etc/vmr/vmr.env`. A screen to edit it would be a new write surface, a new permission question and a new audit requirement, to replace a one-line edit and a restart. |
| Sliding session renewal / "remember me" | Deliberately absent: an absolute non-renewable lifetime is what stops a session being kept alive indefinitely. Add only with a real complaint about the 12-hour window. |
| ID-token signature verification via PyJWT or Authlib | Verification is implemented directly against `cryptography`, which is already a pinned dependency. Swapping in a vendor library touches `app/core/auth/jwks.py` only, and is worth doing if the maintainer prefers a third-party-audited implementation over a reviewed local one. |
| Rate-limiting the sign-in and callback routes | Sign-in is bounded by Google, the transaction cookie is single-use and short-lived, and the JWKS client already rate-limits its own refreshes. A general request limiter belongs with the reverse proxy, not the application. |
| Retiring the now-redundant `_same_origin()` / `_origin_allowed()` checks | Both survive as harmless additional defence behind the real boundary. Removing them is a cleanup with no security value, and touching 4 sequence-write routes and the intake guards to do it is churn the launch does not need. |
| Structured authentication audit events | The access log already records every refusal with a request id, and the application has no sign-in event stream to write to. A real audit trail belongs with per-operator attribution above. |

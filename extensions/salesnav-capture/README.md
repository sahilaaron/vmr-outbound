# VM Prospector — Chrome extension

**VM Prospector** is the operator-driven acquisition of **visible** LinkedIn
and Sales Navigator people for the VMR Outbound Agent. It is the contact-acquisition **edge** of the
system: it reads what the operator is already looking at, lets them review and
annotate it, and submits those people to a narrow VMR intake endpoint (or a
JSON/CSV export). Its responsibility ends there.

> **Save the person first. Decide what to do with them later.**
>
> Contacts are permanent. Campaigns are a later, temporary use of a saved
> audience — the extension does not select one, require one, or store one.

> Manifest V3 · no bundler · **zero runtime dependencies** · no remote code.

The folder name (`salesnav-capture`) is historical: the extension began as a
Sales Navigator listing capture. The path is kept so the committed contract
schemas, backend loaders, and issue history stay continuous.

## Project boundary — what this does NOT do

By deliberate design, the extension does **not**: connect to PostgreSQL/RDS,
create or update contacts, run authoritative normalization, deduplicate database
records, resolve identity, resolve or create labels, enforce or bypass
suppressions, discover or verify emails, research companies, score or qualify
contacts, or generate, approve, or schedule outreach. It also does not select,
create, or require a campaign. All of that stays in the VMR backend or later in
the workflow.

It does **not** store LinkedIn credentials/cookies/tokens, automate login, solve
CAPTCHAs, evade rate limits, auto-paginate, or call undocumented LinkedIn APIs.
It operates only through pages the operator has opened and authenticated
themselves.

## How the notebook was translated (conceptually)

The existing `SN Extractor v2.ipynb` (Selenium/Jupyter) is the behavioural
reference — see [`docs/NOTEBOOK_BEHAVIOUR_MAP.md`](./docs/NOTEBOOK_BEHAVIOUR_MAP.md)
for the full field/selector/pagination/error map. Key translation decisions:

- The durable hooks are the `data-anonymize="*"` attributes; the `artdeco-* /
  pl3 pv3` layout classes are fragile. Extraction therefore **discovers rows
  structurally** (nearest list-item ancestor of a person-name node) and runs an
  **ordered list of strategies per field**, rather than matching exact class
  strings. See [`docs/SELECTORS.md`](./docs/SELECTORS.md).
- Enrichment the notebook did *after* capture — opening company **/about/**
  pages, scraping company websites, harvesting emails, translating names,
  guessing domains — is **out of scope**. It belongs to the backend/vendors.
- The notebook's randomized human-like sleeps, overflow-menu clicking, and
  auto-Next are **not** reproduced (they edge toward anti-bot behaviour).
  Pagination is operator-driven.
- Missing values become explicit `null` + a warning; a results page that yields
  zero rows **fails visibly** instead of returning apparently-valid empty data.

## Install as an unpacked extension (local)

1. Open `chrome://extensions`.
2. Toggle **Developer mode** (top-right).
3. Click **Load unpacked** and select this folder
   (`extensions/salesnav-capture/`).
4. Pin the extension and click its icon to open the **side panel**.
5. (Optional, for the send flow) start the mock receiver:
   `npm run mock-receiver` (listens on `http://127.0.0.1:8787`).

Minimum Chrome version: 116 (side panel API).

## Permissions requested — and why

| Permission | Why |
| --- | --- |
| `storage` | Persist non-secret preferences and the recoverable draft batch |
| `sidePanel` | The review/controls UI |
| `downloads` | JSON / CSV export |
| `activeTab` + `scripting` | Inject the reader into the current tab if needed |
| host `https://www.linkedin.com/sales/*` (required) | Read the results page the operator opened (read-only), narrowly scoped |
| host `https://www.linkedin.com/in/*` (required, DAT-012) | Read the MAIN profile page the operator opened (read-only) |
| host `https://www.linkedin.com/company/*` (required, DAT-012) | Read the company page the operator opened (read-only) |
| host `http://127.0.0.1/*`, `http://localhost/*` (**optional**) | POST the submission to the local VMR backend / mock receiver, and read the label list and the existence-only lookup |

The loopback hosts are declared as **optional** host permissions and are
**requested explicitly, with a user gesture, before the first backend/mock
save** (and before reading the label list). If the operator declines, the send is blocked
with a clear message and a Retry — nothing is transmitted. See the granted vs
denied evidence in `docs/screenshots/` (`02_side_panel.png`,
`03_side_panel_permission_denied.png`). No `history`, no broad `<all_urls>`, no
analytics, no third-party hosts. LinkedIn is a read surface; the extension never
POSTs to it.

## The side panel (VM Prospector)

One shell, three automatically detected interfaces, and one dominant action per
step. The shell is: header (product, connection state, settings) · detected-page
strip · three-step rail · one scrolling body · one sticky action footer.
Designed at 360px, fluid from 320 to ~520.

| Step | Listings | Person profile | Company page |
| --- | --- | --- | --- |
| 1 | Select prospects | Review person | Review company |
| 2 | Review the selected set | Confirm capture (+ Review details) | Confirm identity |
| 3 | Saving → outcome | Saving → outcome | Saving → outcome |

Shared states: classifying the page, unsupported page, sign-in / security check,
page unavailable, loopback permission needed, archived drafts, settings.

Rendered snapshots of every state are in
[`docs/screenshots/panel/`](./docs/screenshots/panel/), produced by
`node tools/render-panel-states.js <outdir>` — the shipped HTML, CSS and
controllers driven through a stubbed `chrome.*`, so a snapshot is the panel that
ships, not a mock-up.

Typography is the design system's own: **Manrope** for headings, names, buttons
and badges; **Source Sans 3** for body and helper text; **IBM Plex Mono** for
counts, identifiers, status text and URLs. The faces are bundled as woff2 under
`src/sidepanel/fonts/` and registered in `fonts.css` — the design system loads
them from Google Fonts, which an extension page must not do. Only the subsets
and weights the panel uses ship; the platform stacks stay behind each family so
text outside those subsets still renders. Licences: `src/sidepanel/fonts/OFL.txt`.

Status is never carried by colour alone: every tone is paired with a word, and
badges carry a shape. A field the page did not show stays visibly empty and is
labelled as missing — it is never filled in, and never quietly dropped.

## Capture modes and supported surfaces (DAT-012)

The side panel is one product with three automatically detected interfaces —
there is no manual mode selector. It classifies the page the operator already
opened and shows exactly one of:

| Mode | Surface | What it captures |
| --- | --- | --- |
| Sales Navigator Listings | `/sales/search/people`, `/sales/search/results/people` | Visible people-search result rows into a reviewable batch, saved as contacts |
| LinkedIn Person Profile | `https://www.linkedin.com/in/<id>` (MAIN page only) | Top card, visible About text, and nested experience entries, saved as one contact |
| LinkedIn Company Profile | `/company/<id>` home or About | Displayed firmographics as company evidence — **not** a contact (`linkedin-company-capture/1.0.0`) |
| Unsupported Page | everything else (incl. `/in/<id>/details/...` sub-routes) | Nothing — the panel explains what to open |
| Challenge / Login Required | checkpoint, captcha, authwall | Nothing — the operator resolves it themselves |

Both person surfaces submit the same contact-first contract,
`linkedin-contact-capture/2.0.0` — see
[`docs/CONTACT_CAPTURE_CONTRACT.md`](./docs/CONTACT_CAPTURE_CONTRACT.md).

Every mode is **operator-controlled**: the extension reads only the page the
operator already opened, only on an explicit click, and sends only on an
explicit Send. There is no navigation, no pagination, no timing simulation, and
no automatic hop from a person profile to their company page. This extension
does not authorize unattended LinkedIn automation of any kind. Identity
matching, freshness resolution, suppression enforcement, canonical updates, QA
evaluation, and audit logging all happen in the VMR backend
(`docs/PROFILE_CONTRACT.md`); browser code never updates a canonical record.

## Supported Sales Navigator surfaces

**Only** Sales Navigator lead/people **search results** routes:
`/sales/search/people` and `/sales/search/results/people`. There is no broad
`/search/` fallback. Account/company **search** pages
(`/sales/search/company`, `/sales/search/accounts`), company pages
(`/sales/company/...`), and every other Sales Navigator surface are **explicitly
rejected** and captured from never (see `isRejectedSalesSurface`). Unsupported
pages are reported with a reason (`rejected_sales_surface` vs `not_people_search`),
not silently processed.

## Operating instructions

### Save one person from their profile

1. Open the person's MAIN profile page yourself (`linkedin.com/in/…`). The panel
   shows **LinkedIn Person Profile**.
2. Click *Read this profile page*. If Experience shows as missing, scroll the
   page so it loads and capture again.
3. Review the name, headline, location, current role, LinkedIn URL, About
   excerpt, connections, open-to-work, and any warnings. Optionally exclude the
   experience section.
4. Optionally add **labels** and a **note** (both optional, both plain text).
5. Click *Save Contact* — or *Refresh Contact*, which the panel offers when the
   backend already knows that exact profile URL.
6. The result reports what actually happened (created / refreshed / staged /
   ambiguous / duplicate / suppressed) and links to the contact and the capture
   record.

### Save several people from a results page

1. Open and authenticate a Sales Navigator people search; the panel confirms a
   supported page (or warns / halts on a challenge).
2. Click *Capture visible contacts*. The reader scrolls the current page once
   (bounded) to materialize lazy rows, then extracts them.
3. Review counts (included / excluded / missing fields / uncertain identity /
   selector fails / pages) and per-row warnings, and **exclude** any row you do
   not want. Move to the next page in Sales Navigator yourself and capture
   again — rows accumulate into one draft batch, de-duplicated by stable URL.
4. Optionally add labels and a note for the submission.
5. Click *Review selected (N)*, check the set, then *Capture N prospects* — or
   *Download JSON* / *Download CSV*.
   Nothing is sent without this explicit action.
6. Open the saved contacts from the returned submission record.

The draft batch is persisted in `chrome.storage.local`, so it survives closing
the side panel or refreshing the page. Use *Clear batch* to start over.

### Upgrading from the campaign-era extension

On install and on browser start, campaign-era local state is retired
**explicitly**: any v1 draft is archived verbatim under one storage key (and can
be downloaded from the panel's one-time notice), the live draft keys and stale
staged-result summaries are cleared, and the remembered campaign is dropped. A
v1 draft is never resubmitted — its idempotency keys may already have been
accepted under the old contract, so replaying it would conflict or split one
person's evidence in two. Capture again to save those people contact-first.

## Export fallback and mock receiver

Until the backend adapter lands, three output modes exist: **Download JSON**,
**Download CSV**, and **Send to a configurable local mock/HTTP receiver**
(`tools/mock-receiver.js`). The production-facing default sends nowhere without an
explicit operator action, and no remote URL is embedded — only loopback origins
are permitted.

## VMR backend contracts

The live contract is
[`docs/CONTACT_CAPTURE_CONTRACT.md`](./docs/CONTACT_CAPTURE_CONTRACT.md)
(`linkedin-contact-capture/2.0.0`, `POST /api/intake/contact-captures`),
idempotent on `client_submission_id`, with no campaign field of any kind.

Company evidence keeps its own contract
([`docs/PROFILE_CONTRACT.md`](./docs/PROFILE_CONTRACT.md), company section) —
a company page is not a person.

[`docs/BACKEND_CONTRACT.md`](./docs/BACKEND_CONTRACT.md) and the
`linkedin-profile-capture/1.0.0` half of `PROFILE_CONTRACT.md` are the **legacy**
campaign-era contracts. They stay documented and accepted at their own routes so
previously staged batches and snapshots remain readable; the extension no longer
produces them.

## Known fragility of page selectors

LinkedIn markup changes without notice, and the extension is built to fail
visibly when it does rather than to guess.

- **Results rows** — `data-anonymize` attributes are the most stable hooks but
  are not guaranteed. If they disappear, extraction falls back to
  structural/class strategies; if **nothing** matches, the capture fails with
  `structure_unrecognized` rather than returning an empty "success". Treat a
  sudden drop in captured fields as a signal to update
  `src/common/extraction.js` and its tests.
- **Person profiles** — the adapter anchors on `componentkey` attributes
  (`*topcard*`, `*entity-collection-item*`, `*about*`) with classic-DOM and
  heading-text fallbacks. Timeline dates are parsed only from deterministic
  forms ("Jan 2021 - Present", "2019 - 2022"); anything else stays raw text with
  `dates_reliable: false`. Visible About text is read only from the section the
  page already rendered — never expanded, fetched, or summarized.
- **Company pages** — the adapter anchors on the About page's definition list
  (`dt`/`dd` label pairs) read by DOM adjacency.

Fix a break by updating the ordered strategy lists in
`src/common/extraction.js` / `src/common/profile-extraction.js` /
`src/common/company-extraction.js` and their fixtures, never by guessing.

## Safe failure behaviour

- Security challenge / checkpoint → capture halts, nothing read.
- Unsupported page → reported, nothing read.
- Empty search → reported as `empty`, never a false success.
- Changed structure → `structure_unrecognized`, nothing fabricated.
- Malformed / non-LinkedIn URLs → flagged, never "repaired".
- Send timeout / rejection → surfaced with detail + a safe (idempotent) retry.

## Explicit exclusions

No unattended scraping, no login automation, no CAPTCHA solving, no rate-limit
or platform-limit bypass, no credential/cookie/token storage, no analytics.

## Development & tests

```bash
npm install          # dev-only (jsdom); the extension ships no runtime deps
npm test             # node --test: extraction, normalize, dedupe, contracts,
                     # campaign decoupling, local-state migration, receiver
npm run mock-receiver
```

`test/browser-check.html` is a manual in-browser harness (serve the folder over
http and open it). `node tools/render-panel-states.js <outdir>` renders the real
side panel — the shipped HTML, CSS and controllers driven through a stubbed
`chrome.*` — into one standalone page per state; the captured images are in
[`docs/screenshots/`](./docs/screenshots/).

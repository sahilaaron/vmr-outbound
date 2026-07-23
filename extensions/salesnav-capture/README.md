# VMR Sales Navigator Capture — Chrome extension

Operator-driven capture of **visible** Sales Navigator records for the VMR
Outbound Agent. It is the contact-acquisition **edge** of the system: it reads
what the operator is already looking at, lets them review it, and hands an
authorized batch to a narrow VMR intake endpoint (or a JSON/CSV export). Its
responsibility ends there.

> Manifest V3 · no bundler · **zero runtime dependencies** · no remote code.

## Project boundary — what this does NOT do

By deliberate design, the extension does **not**: connect to PostgreSQL/RDS,
create or update contacts, run authoritative normalization, deduplicate database
records, enforce or bypass suppressions, verify emails, score contacts, or
schedule outreach. Those remain in the VMR backend. It also does **not** store
LinkedIn credentials/cookies/tokens, automate login, solve CAPTCHAs, evade rate
limits, auto-paginate, or call undocumented LinkedIn APIs. It operates only
through pages the operator has opened and authenticated themselves.

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
| `activeTab` + `scripting` | Inject the reader into the current SN tab if needed |
| host `https://www.linkedin.com/sales/*` | Read the results page the operator opened (read-only) |
| host `http://127.0.0.1/*`, `http://localhost/*` | POST the batch to the local VMR backend / mock receiver only |

No `history`, no broad `<all_urls>`, no analytics, no third-party hosts. LinkedIn
is a read surface; the extension never POSTs to it.

## Supported Sales Navigator surfaces

Lead/people **search results** pages under `https://www.linkedin.com/sales/...`
(`/search/people`, results lists). Account/company result capture and profile
pages are out of scope for this slice (the notebook's account path was enrichment
-heavy). Unsupported pages are reported, not silently processed.

## Operating instructions

1. **Page** — open and authenticate a Sales Navigator people search; the panel
   confirms a supported page (or warns / halts on a challenge).
2. **Capture** — click *Capture visible records*. The reader scrolls the current
   page once (bounded) to materialize lazy rows, then extracts them.
3. **Review** — inspect counts (included / excluded / missing fields / uncertain
   identity / selector fails / pages), per-record warnings, and exclude any rows.
   Move to the next page in Sales Navigator yourself and capture again — records
   accumulate into one draft batch, de-duplicated by stable URL.
4. **Campaign** — pick a campaign (fetched from the local backend) or type an ID.
5. **Send or export** — *Download JSON* / *Download CSV*, or *Send batch* to the
   mock receiver / local backend. Nothing is sent without this explicit action.
6. **Workbench** — on success, open the staged batch in the operator workbench.

The draft batch is persisted in `chrome.storage.local`, so it survives closing
the side panel or refreshing the page. Use *Clear batch* to start over.

## Export fallback and mock receiver

Until the backend adapter lands, three output modes exist: **Download JSON**,
**Download CSV**, and **Send to a configurable local mock/HTTP receiver**
(`tools/mock-receiver.js`). The production-facing default sends nowhere without an
explicit operator action, and no remote URL is embedded — only loopback origins
are permitted.

## Planned VMR backend contract

See [`docs/BACKEND_CONTRACT.md`](./docs/BACKEND_CONTRACT.md),
[`docs/intake.schema.json`](./docs/intake.schema.json), and the fixtures in
`docs/fixtures/`. Versioned as `salesnav-capture/1.0.0`; idempotent on
`client_batch_id`; stages data only. The small backend adapter still required is
listed at the end of that document.

## Known fragility of page selectors

LinkedIn markup changes without notice. The `data-anonymize` attributes are the
most stable hooks but are not guaranteed. If they disappear, extraction falls
back to structural/class strategies; if **nothing** matches on a results page,
the capture **fails visibly** (`structure_unrecognized`) rather than returning
empty "success". Treat a sudden drop in captured fields as a signal to update
`src/common/extraction.js` selectors (and its tests).

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
npm test             # node --test: extraction, normalize, dedupe, schema, receiver
npm run mock-receiver
```

`test/browser-check.html` and `test/sidepanel-preview.html` are manual in-browser
harnesses (serve the folder over http and open them). Screenshots of both are in
[`docs/screenshots/`](./docs/screenshots/).

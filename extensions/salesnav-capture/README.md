# VM Prospector — Chrome extension

**VM Prospector** is the operator-driven acquisition of **visible** LinkedIn
and Sales Navigator people for the VMR Outbound Agent. It is the contact-acquisition **edge** of the
system: it reads what the operator is already looking at, lets them review and
annotate it, and submits those people to a narrow VMR intake endpoint. Its
responsibility ends there. There is no export, download or offline copy: a
reviewed contact is saved into the operator's VMR Outbound account or it is not
saved at all.

> **Save the person first. Decide what to do with them later.**
>
> Contacts are permanent. Campaign selection is optional: it adds an idempotent
> Campaign Contact filing but never gates or owns the saved person.

> Manifest V3 · no bundler · **zero runtime dependencies** · no remote code.

The folder name (`salesnav-capture`) is historical: the extension began as a
Sales Navigator listing capture. The path is kept so the committed contract
schemas, backend loaders, and issue history stay continuous.

## Project boundary — what this does NOT do

By deliberate design, the extension does **not**: connect to PostgreSQL/RDS,
create or update contacts, run authoritative normalization, deduplicate database
records, resolve identity, resolve or create labels, enforce or bypass
suppressions, discover or verify emails, research companies, score or qualify
contacts, or generate, approve, or schedule outreach. It may remember one
operator-selected Campaign filing target, but does not create a Campaign or
require selection. All authoritative work stays in the VMR backend.

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
| `storage` | Persist non-secret preferences, the recoverable draft batch, and the durable push job |
| `unlimitedStorage` | A reviewed set of 5,000 contacts plus the chunk copies that make a save recoverable is tens of megabytes; `storage.local` is capped at 10 MB without it |
| `alarms` | The only unattended wake-up a Manifest V3 service worker has. A save whose next chunk is waiting out a backoff must resume without anybody doing anything |
| `downloads` | The local export — saves the file the operator explicitly asked for. Reaches no network |
| `sidePanel` | The review/controls UI |
| `activeTab` + `scripting` | Inject the reader into the current tab if needed |
| `identity` | `launchWebAuthFlow` for the one-click VMR Outbound sign-in that links this install to the operator's own account (see below) |
| host `https://www.linkedin.com/sales/*` (required) | Read the results page the operator opened (read-only), narrowly scoped |
| host `https://www.linkedin.com/in/*` (required, DAT-012) | Read the MAIN profile page the operator opened (read-only) |
| host `https://www.linkedin.com/company/*` (required, DAT-012) | Read the company page the operator opened (read-only) |
| host the named hosted VMR deployment (**required**) | POST the submission to the product's hosted backend and read the label list, campaigns and the existence-only lookup, under the account link |
| host `http://127.0.0.1/*`, `http://localhost/*` (**optional**) | The same four routes against a local VMR backend / mock receiver, for development |

None of the three permissions added for the large-capture save and the restored
export widens what the extension can READ, where it can SEND, or who it can talk
to — the host permissions below are unchanged. `alarms` fires only while a save
is unfinished and is released the moment one settles; `downloads` is reachable
only from an explicit click on the review screen.

### Required vs optional, and why the hosted origin moved

The hosted VMR deployment is a **required** host permission, granted at install.
It used to be optional, which meant pressing *Sign in to VMR Outbound* first
opened a Chrome dialog naming an unfamiliar server — and dismissing that dialog
left the panel with a message and no sign-in window at all, a click that reads
as a no-op. The origin is fixed product configuration, not an operator choice,
so nothing was being decided by asking.

The permission is not what protects the deployment. The extension holds an
account-linked token bound to one approved `chrome-extension://` origin and the
server admits it to exactly four routes; a host permission only decides which
addresses the extension may open a connection to, and one exact HTTPS host is
narrower than the dialog it replaced.

The **loopback** hosts stay optional and are still **requested explicitly, with
a user gesture, before the first local save** (and before reading the label
list). If a developer declines, the send is blocked with a clear message and a
Retry — nothing is transmitted. See the granted vs denied evidence in
`docs/screenshots/` (`02_side_panel.png`, `03_side_panel_permission_denied.png`).

No `history`, no broad `<all_urls>`, no wildcard host, no analytics, no
third-party hosts. LinkedIn is a read surface; the extension never POSTs to it.

## Connecting to VMR Outbound

There is nothing to configure and nothing to paste. Captures go to VM
Prospector's own hosted deployment, authorised by the operator's **own VMR
Outbound account**:

1. Open the panel. If the operator is already signed in to VMR Outbound and this
   install is approved, it links itself silently — no window, no click.
2. Otherwise the panel offers one action: **Sign in to VMR Outbound**. That runs
   a first-party PKCE authorization-code flow through
   `chrome.identity.launchWebAuthFlow`; VMR's own sign-in and consent pages are
   the only thing the operator sees.
3. The install then holds a short-lived access token (`vmre1.…`, in
   `chrome.storage.session`, memory only) and a rotating refresh token
   (`vmrr1.…`, in `chrome.storage.local`). **A Chrome restart re-authorizes from
   the refresh token alone** — no re-entry of anything, ever.
4. **Disconnect** on the connection screen revokes the link server-side.

The click asks for no permission and opens no dialog of its own — the hosted
origin is a required host permission (above) — so the first visible thing after
pressing the button is VMR's own page. While the window is opening the panel
says so, because a button that only greys out is indistinguishable from one that
did nothing.

### When sign-in does not complete

The panel names the category rather than guessing a cause. Each is decided from
a status code, the server's own two-word error name, or Chrome's description of
the auth *window* — never from a code, token, verifier or response body, so none
of these messages can carry credential material:

| Category | What the operator is told |
| --- | --- |
| `sign_in_cancelled` | The window closed before it finished. Nothing changed. |
| `sign_in_declined` | The connection request was refused at the consent page. |
| `sign_in_incomplete` | The window came back without completing the connection. |
| `authorization_expired` | The request is valid for a minute; complete it without pausing. |
| `extension_not_authorized` | This install is not approved for this deployment — retrying cannot help. |
| `account_link_revoked` | Disconnected, expired, or the account was disabled. Sign in again. |
| `backend_unreachable` / `token_endpoint_error` | VMR could not be reached, or reported a problem. Nothing was sent. |
| `state_mismatch` | The response did not match this request and was discarded. |
| `sign_in_failed` | Anything else — deliberately generic. |

This replaced one catch-all — *"The window was closed, or VMR Outbound declined
this install"* — which named two unrelated causes at once and was wrong about
both whenever the real cause was a third thing.

No shared secret is shown to, typed by, or stored for the operator. The refresh
token is bound to one VMR user, one approved extension id and one installation
id; the server rotates it on every use and revokes the link if an old one is
replayed. `test/account-linking.test.js` holds all of this closed.

The legacy `vmrx1` shared capture credential and the backend/mock target fields
remain **only** behind a development gate: an object at `chrome.storage.local`
key `vmr_dev_overrides` with `enabled: true`. Nothing in the extension writes
that key — no control, no message — so it can only be created by hand from the
extension's own devtools console on an unpacked build, and an ordinary
staging/production install can neither see nor reach any of it.

## The side panel (VM Prospector)

One shell, three automatically detected interfaces, and one dominant action per
step. The shell is: header (product, connection state, connection screen) · detected-page
strip · three-step rail · one scrolling body · one sticky action footer.
Designed at 360px, fluid from 320 to ~520.

| Step | Listings | Person profile | Company page |
| --- | --- | --- | --- |
| 1 | Select prospects | Review person | Review company |
| 2 | Review the selected set | Confirm capture (+ Review details) | Confirm identity |
| 3 | Saving → outcome | Saving → outcome | Saving → outcome |

Shared states: classifying the page, unsupported page, sign-in / security check,
page unavailable, loopback permission needed, archived drafts, sign-in needed,
connection screen.

The listing read pass is **operator-cancellable** (DAT-018 D). While it runs the
panel shows the rows loaded so far and a *Stop reading this page* control, which
routes `CANCEL_CAPTURE` → `CS_CANCEL_SCROLL` and halts the pass. Stopping is an
operator action, not a failure: the scroller still returns the view to the top,
every row already loaded is kept and shown, the batch and the reviewed draft
survive, nothing is submitted, and the next capture starts clean. A cancel that
arrives with no pass running cancels nothing and is reported as such.

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
`linkedin-contact-capture/2.1.0` — see
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
4. Optionally add **Collections (Labels)**, a note, and a Campaign filing target.
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
4. Optionally add Collections (Labels), a note, and a Campaign filing target.
5. Click *Review selected (N)*, check the set, then *Capture N prospects*.
   Nothing is sent without this explicit action.
6. Open the saved contacts from the returned submission record.

The draft batch is persisted in `chrome.storage.local`, so it survives closing
the side panel or refreshing the page. Use *Clear batch* to start over.

#### One save, up to 5,000 contacts

**Maximum contacts in one save: 5,000.** A capture beyond that is refused at
capture time, and a reviewed set beyond it is refused before anything is sent —
by number, naming the limit, not as a size error.

Pressing *Capture N prospects* does not send a single large request. The save is
planned into bounded chunks (**at most 100 contacts and 2 MB each**), written to
local storage, and delivered one chunk at a time by the service worker:

```
reviewed set -> durable job + chunk copies -> chunk 1 -> chunk 2 -> ... -> done
                                  progress written down after every chunk
```

What that buys the operator:

* **Save returns immediately.** The panel shows progress; it does not hold the
  operation. Close the side panel, leave Sales Navigator, close the LinkedIn
  tab, carry on working — the save continues without any of them.
* **It survives suspension.** Chrome may stop the service worker at any moment.
  Progress is on disk, so the worker resumes from the remaining contacts rather
  than starting over, woken by its own alarm or by the panel opening.
* **Retries cannot duplicate.** Each chunk keeps one idempotency key for its
  whole life, so a request whose response was lost is replayed by the backend
  rather than committed twice.
* **Partial failure is survivable.** A failed chunk does not undo the accepted
  ones or stop the later ones; it stays retryable and the panel offers *Retry
  what failed*, which re-sends only the gaps.
* **Nothing is thrown away early.** The reviewed capture is never cleared by
  starting a save, and while a save is unfinished it cannot be cleared, changed
  or added to — the rows are still being delivered. Each chunk's copy is deleted
  as the backend accepts it, so a long save uses less storage as it goes, and a
  copy no save can still send is reclaimed rather than left on disk.
* **Saving again never re-sends anybody.** See below.

#### Saving again after the capture changes

A saved contact carries an id the backend owns for ever. Offering that id again
under a new submission is refused — permanently — so the extension keeps a
**delivery ledger**: for each captured person, has this one ever left the
browser?

A new Save therefore contains **only people who have never been transmitted**.
You can:

* exclude or re-include rows after a save, and save again — the people already
  saved are simply not offered, and you are told how many;
* capture more contacts and save — only the new ones go;
* retry what genuinely failed — a chunk that failed recoverably is carried into
  the next save unchanged, under its own original submission id, so retrying it
  is a replay and never a second copy.

A contact that was sent and never confirmed is never re-sent either. The browser
cannot tell "the request never arrived" from "it arrived and the reply was lost",
so those people are reported as sent-without-confirmation and left for you to
check in VMR Outbound, rather than gambled on.

#### Cancelling a save

While a save is unfinished it holds the reviewed set. If it cannot finish — the
account link was revoked, the deployment will not authorise this install — press
**Cancel push**.

Cancelling is **not** an undo. Contacts the backend already accepted stay
accepted; nothing in the browser can reach across and un-save them. What it does
is stop offering the rest, hand back the reviewed set, reclaim the copies it will
never send, and tell you both halves:

```
642 contacts already saved
1,858 not sent
Push cancelled
```

Transient problems do not need this: a network outage or a sign-in that can be
renewed still recovers on its own.

Reopening the panel reports where the save actually got to — *Saving 650 of
2,843…*, *Retrying…*, *2,843 contacts saved* — never "done" because the first
chunk landed. For a large save the outcome card separates the two numbers that
are easy to confuse: every contact is processed, while a bounded number of
per-contact detail rows is retained for display.

### Download your captured contacts

On the review screen, *Download CSV* and *Download JSON* write the contacts you
have captured to a file. This is a **local** action:

* it never contacts VMR Outbound, so it works when the app does not;
* it never clears or changes the capture — the same rows are still there and
  still saveable afterwards, and both paths can be used on the same batch;
* it exports the rows you have **included**, matching what excluding a row means
  for the save;
* it happens only on your click. Nothing downloads by itself.

CSV is the flat review sheet, in the column contract this extension has always
used, with `linkedin_member_id` and `linkedin_alias_url` appended for the two
identifiers capture gained since. JSON is the exact submission body a save would
send. Both are written from what the page actually showed — an empty column is a
value LinkedIn did not display, never a guess.

### Upgrading from the campaign-era extension

On install and on browser start, campaign-era local state is retired
**explicitly**: the live draft keys and stale staged-result summaries are
cleared, and the legacy campaign value is dropped from preferences. Contract 2.1
stores any new optional filing preference under a separate key. A v1 draft is
never resubmitted — its idempotency keys may already have been accepted under
the old contract, so replaying it would conflict or split one person's evidence
in two. Capture again to save those people contact-first.

Earlier versions also *archived* each v1 draft verbatim under
`cc_legacy_v1_archive` and showed an "Archived drafts can still be downloaded"
card. That card was the archive's only reader and it is gone — so the migration
no longer writes an archive, and it **clears** `cc_legacy_v1_archive` and
`cc_migration_notice` from any install that still carries them rather than
stranding captured personal data under keys nothing can show or remove. That
clearing branch is the only legacy handling retained, and it goes when no
install can still be carrying those keys.

The reviewed-capture export above does **not** bring that archive back, and the
distinction is the point: the export writes the capture you are looking at right
now, under the current contract. The archive holds drafts reviewed under a
contract this extension no longer speaks, which is exactly why it is cleared
rather than offered.

## Mock receiver

Captures go to the hosted VMR backend under the operator's account link, or —
in development only — to a local VMR backend or the configurable mock/HTTP
receiver (`tools/mock-receiver.js`). Nothing is sent without an explicit
operator action, and no remote URL other than the named hosted deployment is
reachable.

There is no export fallback. **Download JSON**, **Download CSV** and the
archived-draft download were an offline path from before hosted capture; they
have been removed along with the `downloads` permission, the worker's download
handlers and the CSV writer in `common/schema.js`.

## VMR backend contracts

The live contract is
[`docs/CONTACT_CAPTURE_CONTRACT.md`](./docs/CONTACT_CAPTURE_CONTRACT.md)
(`linkedin-contact-capture/2.1.0`, `POST /api/intake/contact-captures`),
idempotent on `client_submission_id`, with optional nullable `campaign_id`.

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

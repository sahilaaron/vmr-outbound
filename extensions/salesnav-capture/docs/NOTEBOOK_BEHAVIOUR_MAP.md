# Notebook behaviour map — `SN Extractor v2.ipynb`

This maps the behaviour of the existing Selenium/Jupyter extractor so the Chrome
extension reproduces its **intent** (which fields, from which page surfaces, with
which fallbacks and failure handling) without mechanically porting Selenium code.

The notebook is **behavioural reference only**. It is preserved unchanged; nothing
here modifies it. Where a notebook behaviour is unsafe, out of scope, or invalid
in a browser-extension context, it is called out under *Assumptions that do not
carry over*.

---

## 1. What the notebook does, end to end

Two Chrome windows are driven by ChromeDriver (`get_chrome`): a **primary** logged
into Sales Navigator and a **secondary** used to load company "About" pages while
the primary keeps its place. A campaign is created or reopened, the operator
applies Sales Navigator filters manually, and `main_function` then loops over
result pages, extracting either **Contacts (Leads)** or **Accounts (Companies)**
into an `.xlsx`, paginating via the **Next** button until it is disabled.

The extension keeps only the **capture** half of this: read the visible records on
the current authenticated result page, accumulate across operator-driven
pagination, review, and hand off. Everything the notebook does *after* capture
(company "About" enrichment, website scraping, email harvesting, translation,
Excel authoring) is **out of scope** for the extension and belongs to the VMR
backend — see §8.

---

## 2. Collected fields

### Contacts / Leads (`audience_type == "Contacts"`)
| Field | Notebook source |
| --- | --- |
| `profile_name` (raw full name) | `span[data-anonymize="person-name"]` text |
| first / last name | `profile_name` split on the **first space** |
| `profile_title` | `span[data-anonymize="title"]` text |
| `profile_company` | `a[data-anonymize="company-name"]` text |
| `profile_company_url` | built from the company link's slug/id |
| `profile_location` | `span[data-anonymize="location"]` text |
| `profile_sn_url` (lead URL) | first `<a>` href in the row, prefixed with `https://www.linkedin.com` |
| `profile_url` (public LinkedIn) | derived from the lead URL, or read from the row's **overflow menu → "View LinkedIn profile"** |
| company industry / size / website | loaded later from the company **/about/** page (enrichment) |

### Accounts / Companies (`audience_type == "Accounts"`)
| Field | Notebook source |
| --- | --- |
| company name | `a[data-control-name="view_company_via_result_name"]` text |
| company LinkedIn URL | same anchor's `href`, `"/sales/"` → `"/"`, query stripped |
| industry | `span[data-anonymize="industry"]` |
| website, secondary industry, HQ, size, followers, founded, specialties | company **/about/** page |
| meta keywords / description / emails | fetched from the company **website** |

The extension captures the **result-page-visible** subset only (names, titles,
company name/URL, location, lead URL, any visible company metadata) plus capture
provenance. It does **not** open About pages or company websites.

## 3. Selectors (and their fragility)

| Purpose | Notebook selector | Stability |
| --- | --- | --- |
| Result row | `li.artdeco-list__item.pl3.pv3` (also XPath `//li[@class='artdeco-list__item pl3 pv3 ']`) | **Fragile** — utility/layout classes, exact `class=` match, trailing-space variants |
| Person name | `span[data-anonymize="person-name"]` | **Stable-ish** — semantic data hook |
| Title | `span[data-anonymize="title"]` | Stable-ish |
| Company name | `a[data-anonymize="company-name"]` | Stable-ish |
| Location | `span[data-anonymize="location"]` | Stable-ish |
| Industry (accounts) | `span[data-anonymize="industry"]` | Stable-ish |
| Company anchor (accounts) | `a[data-control-name="view_company_via_result_name"]` | Medium |
| Lead overflow menu | `button[aria-label="Open actions overflow menu"]`, `div#hue-web-menu-outlet li` | Fragile — internal ids |
| Next button | `button[aria-label="Next"]`, disabled = `artdeco-button--disabled` | Medium |
| Results loader / scroll target | `section.flex-1.relative` | Fragile |
| Company "About" details | `dl.overflow-hidden` `dt`/`dd` pairs; `More info` offset hack | Fragile (enrichment, not in extension) |

**Design consequence:** the `data-anonymize="*"` attributes are the durable hooks;
the `artdeco-*`/`pl3 pv3` classes are not. The extension therefore anchors on the
`data-anonymize` semantics and **discovers result rows structurally** (nearest
list-item ancestor of a person-name node) instead of matching exact class strings.
See `docs/SELECTORS.md` and `src/common/extraction.js`.

## 4. Selector fallbacks in the notebook
- Public profile URL: primary = derive from lead URL; **fallback** = open the lead
  overflow menu and read "View LinkedIn profile".
- Company "About" `Headquarters`/`Founded`/`Specialties`: index offset when a
  `More info` button is present (`target_idx = index (+1 if has_more_info)`).
- Website fetch: HTTPS first, then retry over HTTP on SSL/connection error.
- Company memory cache: `get_company_from_memory` reuses an already-seen company's
  enrichment by company id to avoid re-loading `/about/`.

The extension keeps the *idea* of layered fallbacks (multiple strategies per field,
each independently testable) but not the enrichment-specific ones (About-page
offsets, website retries) which it never performs.

## 5. Pagination logic
- Find `button[aria-label="Next"]`. No button → "Completed (no Next button)".
- Class contains `artdeco-button--disabled` → "Completed (Next disabled)".
- Otherwise move mouse to it, `scrollIntoView`, click, wait for `current_url` to
  change; retry twice on stale/intercepted/timeout; if still stuck, **wait for the
  operator to advance manually**, else mark "Stuck on page; manual action needed".

**Extension equivalent:** pagination is **operator-driven**. The extension never
auto-clicks Next. It detects the current page (via the `page=` URL parameter and
the results header), captures on demand, and the operator moves to the next page
themselves — matching the notebook's safe "manual action" fallback as the *only*
mode. This is a deliberate safety choice (no unattended paging).

## 6. Scrolling / wait behaviour
- `sn_results_loader`: repeatedly move the mouse to the last `section.flex-1.relative`
  with random 1–3s sleeps to force lazy rows to render.
- Random human-like sleeps throughout (`randint(3,8)`, `randint(5,22)`, etc.).
- `get_li_url_from_sn`: spin-wait until the overflow menu button exists.

**Extension equivalent:** a single, bounded, user-initiated "load visible rows"
pass that scrolls the results container to materialize lazy rows, then reads them.
No randomized human-mimicking delays (that is anti-bot evasion behaviour and is
explicitly out of scope). Capture is a discrete action the operator triggers.

## 7. Cleaning / dedupe / errors / outputs
**Cleaning (notebook):** `remove_non_ascii` (NFKD + ASCII drop), `GoogleTranslator`
translation of non-English names, `.strip()`, company id parsing from URL, domain
via `urlparse().netloc` minus `www.`.

**Dedup (notebook):** `company_list_memory` caches company enrichment by company id
(a *performance* cache, not record dedup). There is **no** person-level dedup —
every visible lead row is written.

**Errors (notebook):** skip a row if title or company anchor is missing
(`continue`); treat `company/unavailable` About pages as empty; broad try/except on
website fetch; manual-intervention wait when Next fails.

**Outputs (notebook):** an `.xlsx` workbook (Contacts or Accounts sheet + a `logs`
sheet holding campaign name/date/status and the live extraction URL), saved after
every row.

**Extension equivalents:**
- Cleaning is limited to **trim + preserve raw**. The extension does **not**
  translate, ASCII-fold, or derive domains — those are backend normalization
  responsibilities (`app/services/imports/normalization.py`). Raw visible values
  are preserved verbatim (Unicode kept).
- Dedup is done **in the draft batch** by stable Sales Navigator lead / LinkedIn
  URL; when no stable URL is available the record is kept and flagged as an
  uncertain-identity duplicate rather than dropped.
- Missing fields become explicit `null` + a per-record warning; a results page that
  yields **zero** rows while looking like a results page **fails visibly** (it is
  never reported as a successful empty capture).
- Output is a reviewable draft batch → JSON / CSV download or an explicit POST to
  the VMR intake endpoint (mock receiver until the backend adapter lands). No Excel.

## 8. Assumptions that do NOT carry over to the extension
1. **Two ChromeDriver windows / Selenium.** Replaced by one content script in the
   operator's own authenticated tab. No ChromeDriver, no second browser, no
   `webdriver` automation surface.
2. **Company "About" enrichment & website scraping** (industry, size, HQ, founded,
   specialties, followers, meta keywords/description, harvested emails). Out of
   scope — the extension captures only result-page-visible data; enrichment,
   verification and email discovery are backend/vendor responsibilities.
3. **Translation + ASCII folding of names.** Out of scope; the extension preserves
   raw Unicode and lets backend normalization decide. (Guessing an anglicized name
   in the client would corrupt provenance.)
4. **Domain guessing** via `urlparse(company_url).netloc`. Out of scope; a company
   URL/name is not verified evidence of a domain (`AGENTS.md` email rules). The
   backend owns domain/email intelligence.
5. **Random human-like sleeps, overflow-menu clicking, spin-waits, auto-Next.**
   These edge toward rate-limit/anti-bot behaviour. The extension is strictly
   operator-driven with bounded, visible actions and no automated paging.
6. **Excel authoring + MySQL/`mysql.connector` import.** Replaced by JSON/CSV
   export and a staged POST to the backend intake endpoint. The extension never
   touches a database.
7. **Overflow-menu "View LinkedIn profile" fallback.** The extension does not open
   menus/click through the UI; if a public `/in/` URL is not visibly present it is
   left `null` with a warning rather than derived from the opaque lead id (the
   notebook's `.replace('/sales/lead/','/in/')` yields an unverifiable URL).

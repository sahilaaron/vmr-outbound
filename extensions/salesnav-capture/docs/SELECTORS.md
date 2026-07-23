# Extraction strategy & selectors

Extraction lives in `src/common/extraction.js` and is deliberately layered so a
single LinkedIn markup change degrades gracefully (fewer fields, visible
warnings) instead of silently breaking.

## Row discovery (fragility-resistant)

Rather than matching the fragile `li.artdeco-list__item pl3 pv3` class string the
notebook used, rows are discovered **structurally**:

1. Find all `[data-anonymize="person-name"]` nodes.
2. For each, climb to the nearest ancestor matching
   `li[data-x-search-result], li.artdeco-list__item, li[role="listitem"],
   [role="listitem"], li`.
3. Fallbacks: nearest `[class*="entity-lockup"]` / `article`; else the name
   node's parent.
4. If **no** person-name nodes exist at all, fall back to explicit
   `li.artdeco-list__item` / `[data-x-search-result]` /
   `.search-results__result-item` containers.

If discovery still yields zero rows on a supported results URL, the page result
is `structure_unrecognized` (or `empty` when an explicit no-results marker is
present) — never a successful empty capture.

## Per-field strategies (ordered; first match wins)

| Field | Strategy order |
| --- | --- |
| name | `[data-anonymize="person-name"]` → `a[href*="/sales/lead/"] span[dir="ltr"]` → `.artdeco-entity-lockup__title a` → `.artdeco-entity-lockup__title` |
| title | `[data-anonymize="title"]` → `.artdeco-entity-lockup__subtitle` → `[class*="entity-lockup__subtitle"]` |
| companyName | `a[data-anonymize="company-name"]` → `[data-anonymize="company-name"]` → `a[data-control-name="view_company_via_result_name"]` → `.artdeco-entity-lockup__subtitle a` |
| location | `[data-anonymize="location"]` → `[class*="entity-lockup__caption"]` |
| lead URL | `a[data-anonymize="person-name"]` → `a[href*="/sales/lead/"]` → `a[href*="/sales/people/"]` → `.artdeco-entity-lockup__title a` |
| company URL | `a[data-anonymize="company-name"]` → `a[data-control-name="view_company_via_result_name"]` → `a[href*="/sales/company/"]` → `a[href*="/company/"]` |
| public profile URL | `a[href*="/in/"]` (only if visibly present; never derived) |
| visible company metadata | `[data-anonymize="industry"]`, `.artdeco-entity-lockup__metadata` (raw, de-duplicated, unparsed) |

## URL normalization

`normalizeLinkedInUrl` (in `normalize.js`) absolutizes path-only/protocol-relative
hrefs against `www.linkedin.com`, lower-cases the host, strips query + fragment +
trailing slash, and **strips the volatile search-context suffix** after the first
comma in `/sales/lead/` and `/sales/people/` paths so the same lead has a stable
identity across pages/searches. Non-LinkedIn hosts and unparseable values are
rejected (flagged `malformed_url`), never repaired.

## What is intentionally NOT derived

- Public `/in/` profile URLs from the opaque lead id (the notebook's
  `.replace('/sales/lead/','/in/')` produced an unverifiable URL).
- Company domains from a company URL/name (`AGENTS.md` email-intelligence rule).
- Anglicized/ASCII-folded names (raw Unicode is preserved; normalization is a
  backend concern).

Update this table and the constants in `extraction.js` together with their tests
in `test/extraction.test.js` whenever LinkedIn markup shifts.

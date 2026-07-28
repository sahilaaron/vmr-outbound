# Extraction strategy & selectors

Two extractors, two documents in one:

- **Sales Navigator search results** — `src/common/extraction.js`, described
  first below.
- **The LinkedIn profile top card** — `src/common/profile-extraction.js`,
  described in *Profile top card (DAT-016)* at the end.

Both are deliberately layered so a single LinkedIn markup change degrades
gracefully (fewer fields, visible warnings) instead of silently breaking.

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
| public profile URL | `a[href*="/in/"]` if visibly present; otherwise **derived** from the lead URL's member id (DAT-018) |
| visible company metadata | `[data-anonymize="industry"]`, `.artdeco-entity-lockup__metadata` (raw, de-duplicated, unparsed) |

## URL normalization

`normalizeLinkedInUrl` (in `normalize.js`) absolutizes path-only/protocol-relative
hrefs against `www.linkedin.com`, lower-cases the host, strips query + fragment +
trailing slash, and **strips the volatile search-context suffix** after the first
comma in `/sales/lead/` and `/sales/people/` paths so the same lead has a stable
identity across pages/searches. Non-LinkedIn hosts and unparseable values are
rejected (flagged `malformed_url`), never repaired.

## Canonical profile URL from the lead URL (DAT-018)

`/sales/lead/<member-id>` carries the member identifier. It is read and kept as
`linkedinMemberId` — **an identifier, never a URL**. The public profile URL is
recorded only when an `/in/` link is actually visible on the row.

**DAT-019 reversed the DAT-018 derivation.** DAT-018 built
`https://www.linkedin.com/in/<member-id>` whenever no link was visible and put
it in the canonical profile-URL slot. That alias does resolve — LinkedIn's
`/in/` route accepts the member id and redirects to the person — so the problem
was never a dead link. It is that identity is matched by **exact normalized
string**: the alias and the person's published handle are two different keys for
one human, so capturing them from a results row and from their own profile page
produced two identities that could never reconcile.

The consequence was predicted in this file and filed as issue #195 once it was
observed live in the DAT-011 trial.

The rule now:

- only `/sales/lead/` is read — `/sales/people/` carries a different segment;
- query strings, fragments, the volatile `,NAME_SEARCH,…` suffix and any extra
  route material are stripped before the identifier is read;
- the identifier must match `^[A-Za-z0-9_-]{3,128}$`. Anything else is refused
  with a reason rather than repaired;
- the identifier is preserved **verbatim and case-sensitively** — the alias
  LinkedIn accepts carries the case through, so folding it breaks the value;
- the original Sales Navigator URL is preserved unchanged as `salesNavLeadUrl`;
- `linkedinProfileUrlSource` is `observed` or null. There is no third state,
  because nothing is derived any more;
- a row that shows **both** a lead URL and a real `/in/` link keeps both. That
  pair, observed together on one page, is what relates the two identifier forms
  for one person without anything being inferred.

A clickable link may still be built from the member id for display. That is a
convenience for the operator, never an identity.

## What is intentionally NOT derived

- Public `/in/` profile URLs for any route other than `/sales/lead/`, and for
  any lead URL whose identifier fails validation.
- Company domains from a company URL/name (`AGENTS.md` email-intelligence rule).
- Anglicized/ASCII-folded names (raw Unicode is preserved; normalization is a
  backend concern).

## Capture eligibility (DAT-018)

A visible row with no Company Name — absent, empty, or whitespace-only — is
**not** offered as capturable: the downstream flow is company-first, so such a
row cannot be used. The company is never inferred from the headline, a school, a
location, or a neighbouring row. Skipped rows are reported truthfully
(`skipped`, `skippedCount`, and a `rows_skipped` page warning carrying the
count), never silently dropped, and skipping one row never aborts the others.

Update this table and the constants in `extraction.js` together with their tests
in `test/extraction.test.js`, `test/salesnav-identity.test.js` and
`test/panel-layout.test.js` whenever LinkedIn markup shifts.

---

# Profile top card (DAT-016)

`src/common/profile-extraction.js`. This section supersedes the original
top-card parser, which read the Nth visible line after the name.

## Why positional parsing had to go

Nine real top cards were inspected while designing this. They differ not only in
values but in **node count and node order**:

- the degree badge appears zero, one or two times, and its absence is not a
  self-view-only case;
- an unlabelled line can sit between the name and the degree badge;
- the company · school line is present on some views and absent on others;
- the connection region renders as one, two or four separate nodes — or as a
  container that is completely empty;
- the headline can be LinkedIn's literal `--` placeholder;
- the action-button set is open-ended and profile-specific.

Between the widest and the narrowest, the position of the location line moves by
more than four. Any `nth-child` or array-offset rule is therefore wrong on most
profiles, which is the defect #167 describes.

## What is stable, and what is not

**Not stable.** Every class in the top card is a hashed build token
(`_687a5045`, `_8c535ff6`, `_3ab7a3ad`, …). There is no `pv-text-details__…`, no
`text-heading-xlarge`, no `artdeco-*`. The extractor uses **none** of them, and
`test/profile-topcard.test.js` proves extraction is byte-identical with every
`class` attribute removed.

One token was actively misleading. On a two-profile reading `_3ab7a3ad` looked
like "the location class"; a third profile carried it on an unrelated line in the
name row. Treating it as a selector would have produced a *silently wrong*
location — worse than `null`. Hence the rule: a hashed class may corroborate,
never select.

**Stable.** `svg[id]` values are semantic and have held across every sample:
`verified-small` / `verified-medium`, `company-accent-*`, `school-accent-*`,
`person-accent-*`, `connect-small`. These carry the structural strategies.

Note that a `<figure>` may hold the placeholder `<svg>` and **no `<img>`**. The
slot existing and a logo rendering are two different facts and are not conflated.

## Container discovery (ordered)

| # | Strategy |
| --- | --- |
| A | `[componentkey*="topcard"]` that contains the name heading; **smallest** such match, since a wider one means the card's own wrapper was missed |
| B | `.pv-top-card` / `[class*="pv-top-card"]` (classic markup) |
| C | Measured climb from the name heading (below) |

**Strategy A is the one that fires on live profiles.** Authenticated C1
acceptance established that `componentkey` attributes containing `topcard` do
exist on the current DOM (typically five per page, two of them holding the name
heading). The measured climb in Strategy C is therefore a fallback in practice,
not the main path — worth knowing before trusting a fixture that exercises only
the climb.

It also means the container is only as tight as LinkedIn's own card, and on at
least one live profile that card contained considerably more than a top card.
See *The interrupted card* below.

Strategy C replaced a climb to the nearest `<section>`. The current DOM has no
`<section>` in the top card, so that climb ran to `<main>` and swallowed About
and the activity feed — a 20-node card became a 200-node capture. The climb now
stops at the block boundary, decided by two independent measurements:

- **headings** — every sibling block on a profile (About, Experience, Education,
  …) is introduced by its own heading, while the top card contains exactly one:
  the name. An ancestor holding two or more headings has absorbed a neighbour.
- **size** — a sudden jump in text-block count between two rungs is a second,
  independent signal of the same boundary, used to trim the ladder first.

A floor (`TOPCARD_MIN_BLOCKS`) matters as much as the boundary: without it the
first rung — a wrapper holding only the name — looks like a valid card and the
capture collapses to the name alone.

## The interrupted card (authenticated C1 finding)

One live profile's top-card container also held a promotional line, a block of
interface controls, and an ad-preferences panel — roughly seventy `<option>`,
`<legend>` and `<label>` elements — sitting **between the name and the real
top-card rows**. 124 text blocks in what should have been about twenty.

Two fields came back confidently wrong, with no warning: the headline was the
promo line, and the location was a dropdown option that happened to be shaped
like a place. That is the exact failure mode this work exists to remove, and no
synthetic fixture had predicted it.

Three tightenings followed, all of which can only ever *remove* a candidate —
none can invent a value, so the worst they can do is leave a field null with a
warning:

**1. The candidate window is anchored at both ends.** The upper bound was
already the connection region. The lower bound is no longer the name heading —
which assumed the heading is immediately followed by the rest of the card — but
the **last name-row anchor** (degree badge, pronoun line, or the name itself)
that still precedes the connection region. On an ordinary profile that anchor is
the degree badge directly under the name and nothing changes; on the interrupted
layout it lands below the interruption, which is where the card really starts.

**2. Non-profile subtrees are never collected as blocks.** `select`, `option`,
`optgroup`, `legend`, `fieldset`, `label`, `input`, `textarea`, `form`,
`dialog`, anything with an ARIA role of `listbox`/`option`/`menu`/`menuitem`/
`dialog`/`tablist`/`tab`/`radiogroup`/`combobox`/`form`/`search`, and anything
`aria-hidden="true"`. A dropdown option is not a person's location.

**3. Any element with `role="button"` is a control.** The check previously
matched `<button>` and `<a role="button">` only. Live profiles render actions as
`<div role="button">` at least as often, and an unrecognised action label
competes for the headline and the location. `menuitem` and `tab` are covered too.

`raw_lines` is deliberately **not** filtered by rule 2 — it is verbatim page text
kept so the backend can re-derive a value this parser version did not understand.
Page furniture appearing there is noise in the evidence, not corruption of a
field.

Fixture: `test/fixtures-profile/profile-interrupted-topcard.html`.

## Per-field strategies (ordered; first that resolves wins)

Anything unresolved is `null` **plus an explicit warning**. Never a guess, never
a positional fallback.

| Field | Strategy 1 (structural) | Strategy 2 (pattern) | Corroboration only |
| --- | --- | --- | --- |
| `full_name` | first `h1, h2, h3, [role=heading]` in the card **whose text is not a section heading**; own text read first so a nested verification badge cannot contribute; captured verbatim | first unclassified block | — |
| `headline` | first unclassified block in the anchored window (last name-row anchor → connection region) | `--` ⇒ `null` + `placeholder_value` | `_8c535ff6` |
| `displayed_location` | the unclassified block sharing a row with the **Contact info** control | last unclassified block before the connection region, only when ≥2 exist | `fb33e5ec` on the row |
| `connection_count` | token scan of the connection region (below) | `^(\d[\d,]*\+?)\s+connections$` | — |
| followers | same token scan | `^(\d[\d,]*\+?)\s+followers$` | — |
| `open_to_work` | "Open to work" in the card, or an `#OPEN_TO_WORK` photo frame | — | — |
| verification | `svg[id^="verified-"]` anywhere in the name row, bare or wrapped in an `<a>` | — | — |
| company · school row | `<figure>` containing `svg[id^="company-"]` or `svg[id^="school-"]` | — | — |
| name row | smallest ancestor of the heading also holding a degree badge, pronoun line **or verification badge**, bounded by `MAX_NAME_ROW_BLOCKS` | heading alone | — |

**Block classification.** Before any field resolves, every text block in the card
is classified by shape and role — name, credentials, separator, degree, pronoun,
contact-info, count, mutuals, action — so field resolution only ever considers
text that is genuinely unaccounted for. Action controls are recognised by being
`<button>` or `[role=button]`, not by matching a label list, because the button
set is open-ended.

**Pronouns** are matched as an exact set (`he/him`, `she/her`, …), never by the
presence of a slash: a headline of `Founder/CEO` must survive.

## The connection region

Observed shapes: `500+ connections` (one node); `500+` + `connections` (two);
`29,777 followers` + `·` + `500+` + `connections` (four); and an entirely empty
container. No fixed arity can read all four, so the region is read as tokens:
`^\d[\d,]*\+?$` is a count, `^(followers?|connections?)$` is a label, and each
count pairs with its nearest following label across separators.

Three outcomes, deliberately distinguished:

| Page shows | Result | Warning |
| --- | --- | --- |
| nothing | `null` | `missing_field:connection_count` |
| a region we could not pair (followers only, unlabelled count) | `null` | `unparsed_value:connection_count` |
| a paired count | the exact value | — |

`null` is never `0`. A hidden connection count and a person with zero
connections are different facts.

The scan is scoped to the top card. It deliberately does **not** fall back to the
whole document: other people's connection counts appear elsewhere on a profile
page, and a wrong count attributed to this person is worse than no count.

## Attributes that are never read

Each of these appears in the live DOM and carries identity. They may be used to
*recognise* an element; their values never enter a capture, a fixture, or a log:

- `componentkey` — embeds the profile handle and `urn:li:member:<id>`
- `href` on connect / message / verification — `vanityName`, `profileUrn`,
  `connectionOf`, `/in/<handle>`
- `img src` — `media.licdn.com` URLs with signed tokens
- `aria-label` on the connect button — the person's full name
- outbound "Learn more" links — tracking hashes

## Fixtures

`test/fixtures-profile/profile-modern-topcard.html`,
`profile-empty-connections.html`, `profile-placeholder-headline.html`,
`profile-name-suffix.html`, `profile-followers-only.html`.

Every one is **synthetic**, authored from the structural description above. No
captured markup and no real profile content is committed — no names, URLs,
member identifiers, image URLs, tracking hashes or component keys.

## Known gap

No sample carries a **"Talks about"** line. The strategies above are
container-relative rather than index-relative, so a "Talks about" node should not
displace anything — but that is a prediction, not an observation. Treat this row
as unconfirmed until a real sample is seen.

Update this section, `profile-extraction.js` and `test/profile-topcard.test.js`
together whenever LinkedIn markup shifts.

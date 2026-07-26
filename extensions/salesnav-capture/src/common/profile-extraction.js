/**
 * LinkedInPersonProfileAdapter (DAT-012B): extraction for a MAIN public
 * LinkedIn profile page (`/in/<id>`) the operator already opened.
 *
 * Behavioural reference: the legacy QA bot's `parse_linkedin_topcard`,
 * `li_profile_parser` (basic + chained experience layouts), `_parse_connections`
 * and `_is_open_to_work`. Only the parsing behaviour is reused — no navigation,
 * no search, no waits, no automation of any kind lives here.
 *
 * Design rules (same as the SalesNav extractor):
 *   - Ordered strategies per field; first hit wins; a miss is a warning, never
 *     a guess. Nulls are preserved.
 *   - Raw visible text lines are preserved verbatim (`raw_lines`) so the
 *     backend can re-derive values later; parsed fields are convenience views.
 *   - Unknown structure fails VISIBLY (`structure_unrecognized`); a parsed page
 *     with missing sections is `partial` with explicit `missing_sections`.
 *   - Dates are parsed from timeline text only when deterministic; otherwise
 *     the raw text stands alone and `dates_reliable` is false.
 *
 * DOM-agnostic: callers pass a `document` (real DOM in the content script,
 * jsdom in tests). No network access, no page mutation.
 *
 * UMD module -> Node CommonJS + self.SNCapture.profileExtraction
 */
(function (root, factory) {
  const g = typeof self !== "undefined" ? self : root;
  const isNode = typeof module !== "undefined" && module.exports;
  const mod = factory(
    isNode ? require("./constants.js") : g.SNCapture.constants,
    isNode ? require("./normalize.js") : g.SNCapture.normalize,
    isNode ? require("./surface.js") : g.SNCapture.surface
  );
  if (isNode) module.exports = mod;
  g.SNCapture = Object.assign(g.SNCapture || {}, { profileExtraction: mod });
})(typeof globalThis !== "undefined" ? globalThis : this, function (constants, normalize, surface) {
  "use strict";

  const { WARNINGS, CAPTURE_STATUS, SURFACES } = constants;
  const ADAPTER_VERSION = "linkedin-person-profile-adapter/1";

  // ---- Small helpers -------------------------------------------------------

  /**
   * Visible text of an element as line-broken text. Chrome's `innerText`
   * yields rendered lines even in minified DOM; jsdom (tests) lacks it, so we
   * fall back to `textContent`, which relies on source whitespace — fixtures
   * are authored with newlines between block elements for this reason.
   */
  function elementText(el) {
    if (!el) return "";
    if (typeof el.innerText === "string") return el.innerText;
    return el.textContent || "";
  }

  function cleanLines(elOrText) {
    const text = typeof elOrText === "string" ? elOrText : elementText(elOrText);
    return String(text || "")
      .split("\n")
      .map((l) => normalize.cleanText(l))
      .filter((l) => l && l !== "… more" && l !== "…more" && l !== "see more");
  }

  function attr(el, name) {
    return (el && typeof el.getAttribute === "function" && el.getAttribute(name)) || "";
  }

  function lowerComponentKey(el) {
    return attr(el, "componentkey").toLowerCase();
  }

  // Employment-type keywords LinkedIn renders as a `· <type>` suffix or a bare
  // line. Mirrors the legacy `_JOB_TYPE_KW` set.
  const EMPLOYMENT_TYPES = new Set([
    "Full-time",
    "Part-time",
    "Contract",
    "Self-employed",
    "Freelance",
    "Internship",
    "Apprenticeship",
    "Seasonal",
    "Temporary",
  ]);

  // Workplace / location types LinkedIn renders after the role location.
  const WORKPLACE_TYPES = new Set(["On-site", "Hybrid", "Remote"]);

  // ---- Timeline / date parsing --------------------------------------------

  const MONTHS = {
    jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6,
    jul: 7, aug: 8, sep: 9, sept: 9, oct: 10, nov: 11, dec: 12,
  };
  const MONTH_RE = "(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*";
  const YEAR_RE = "((?:19|20)\\d{2})";
  const SEP_RE = "\\s*(?:-|–|—|to)\\s*";

  // Groups: 1=start month, 2=start year, 3=end month, 4=end year.
  const RANGE_MONTH_RE = new RegExp(
    `^${MONTH_RE}\\.?\\s+${YEAR_RE}${SEP_RE}(?:${MONTH_RE}\\.?\\s+${YEAR_RE}|present)$`,
    "i"
  );
  const RANGE_YEAR_RE = new RegExp(`^${YEAR_RE}${SEP_RE}(?:${YEAR_RE}|present)$`, "i");
  const SINGLE_MONTH_RE = new RegExp(`^${MONTH_RE}\\.?\\s+${YEAR_RE}$`, "i");

  function monthNumber(token) {
    if (!token) return null;
    const key = String(token).slice(0, 4).toLowerCase() === "sept"
      ? "sept"
      : String(token).slice(0, 3).toLowerCase();
    return MONTHS[key] || null;
  }

  /**
   * Deterministically parse a timeline string ("Jan 2020 - Present",
   * "Mar 2015 - Jun 2018", "2019 - 2022", "May 2024").
   * Returns { start, end, isCurrent, reliable } where start/end are
   * { year, month|null } or null. Anything unrecognized -> reliable:false and
   * all-null dates. Never guesses.
   */
  function parseTimeline(timelineText) {
    const none = { start: null, end: null, isCurrent: null, reliable: false };
    const t = normalize.cleanText(timelineText);
    if (!t) return none;

    const isCurrent = /\bpresent\b/i.test(t);

    let m = t.match(RANGE_MONTH_RE);
    if (m) {
      // Groups: 1=start month, 2=start year, 3=end month (may be undefined), 4=end year
      const start = { year: parseInt(m[2], 10), month: monthNumber(m[1]) };
      let end = null;
      if (!isCurrent && m[4]) end = { year: parseInt(m[4], 10), month: monthNumber(m[3]) };
      return { start, end, isCurrent, reliable: true };
    }
    m = t.match(RANGE_YEAR_RE);
    if (m) {
      const start = { year: parseInt(m[1], 10), month: null };
      const end = !isCurrent && m[2] ? { year: parseInt(m[2], 10), month: null } : null;
      return { start, end, isCurrent, reliable: true };
    }
    m = t.match(SINGLE_MONTH_RE);
    if (m) {
      return {
        start: { year: parseInt(m[2], 10), month: monthNumber(m[1]) },
        end: null,
        isCurrent,
        reliable: true,
      };
    }
    return { start: null, end: null, isCurrent: isCurrent || null, reliable: false };
  }

  /** Split "Jan 2020 - Present · 3 yrs 2 mos" into { timeline, duration }. */
  function splitTimelineDuration(line) {
    const parts = String(line || "").split("·").map((p) => normalize.cleanText(p));
    return { timeline: parts[0] || null, duration: parts[1] || null };
  }

  /** True when a line looks like a timeline line (mirrors the legacy check). */
  function looksLikeTimeline(line) {
    if (!line) return false;
    const l = line.toLowerCase();
    const hasRangeSep = / - | – | — | to /.test(l) || /present/.test(l);
    const hasDateToken = /present|(?:19|20)\d{2}|\byr\b|\byrs\b|\bmo\b|\bmos\b/.test(l);
    return hasRangeSep && hasDateToken;
  }

  // ---- Company URL / id ----------------------------------------------------

  function companyUrlFromElement(el) {
    if (!el || typeof el.querySelectorAll !== "function") return null;
    const anchors = el.querySelectorAll('a[href*="/company/"]');
    for (const a of Array.from(anchors)) {
      const href = a.getAttribute("href");
      const norm = normalize.normalizeLinkedInUrl(href);
      if (norm.valid && /\/company\//.test(norm.url)) return norm.url;
    }
    return null;
  }

  function companyIdFromUrl(companyUrl) {
    if (!companyUrl) return null;
    const m = String(companyUrl).match(/\/company\/([^/?#]+)/);
    return m ? m[1] : null;
  }

  // ---- Topcard (DAT-016: structural, never positional) ----------------------
  //
  // The current profile top card carries no semantic classes at all: every
  // class is a hashed build token, the name sits in an `<h2>` rather than an
  // `<h1>`, and there is no `<section>` wrapper. Worse for parsing, the *number
  // and order* of text blocks differ between profiles. Across the real top
  // cards inspected while designing this:
  //
  //   - the degree badge appears zero, one or two times;
  //   - an extra unlabelled line can sit between the name and the degree badge;
  //   - the company · school line is present on some views and absent on others;
  //   - the connection region renders as one, two or four separate nodes — or
  //     as a completely empty container;
  //   - the headline can be LinkedIn's literal `--` placeholder;
  //   - the action-button set is open-ended and differs per profile.
  //
  // Between the widest and the narrowest, the position of the location line
  // moves by more than four. So nothing below indexes into a flat list of
  // lines. Each field is resolved by ordered strategies over DOM structure and
  // over content patterns; the first that resolves wins; anything unresolved
  // stays null and raises a warning. A hashed class is never used to select a
  // field — see docs/SELECTORS.md.

  // Content patterns. These describe LinkedIn's own rendering, not our markup.
  const DEGREE_RE = /^·?\s*(1st|2nd|3rd)(\s+degree)?$/i;
  const SEPARATOR_RE = /^[·•|-]$/;
  const CONTACT_INFO_RE = /^(see\s+)?contact info$/i;
  const MUTUAL_RE = /\b(mutual connections?|is a mutual connection)\b/i;
  // LinkedIn renders an absent headline as a literal "--". That is a
  // placeholder, not content, and must not be stored as a headline.
  const HEADLINE_PLACEHOLDER_RE = /^-{2,}$/;

  const COUNT_RE = /^\d[\d,]*\+?$/;
  const COUNT_LABEL_RE = /^(followers?|connections?)$/i;
  const COUNT_WITH_LABEL_RE = /^(\d[\d,]*\+?)\s+(followers?|connections?)$/i;

  // Matched as an exact set rather than by substring: a headline like
  // "Founder/CEO" must never be mistaken for a pronoun line.
  const PRONOUN_SET = new Set([
    "he/him", "she/her", "they/them", "he/they", "she/they",
    "they/he", "they/she", "ze/hir", "ze/zir", "xe/xem",
  ]);

  // Fallback only: buttons are recognised structurally first (see
  // `isActionBlock`). This set exists for markup that renders an action as
  // plain text.
  const ACTION_TEXT = new Set([
    "connect", "message", "more", "follow", "following", "pending",
    "open to", "open to work", "add profile section", "enhance profile",
    "resources", "view my verifications", "save to pdf", "about this profile",
    "send profile in a message", "edit", "promoted",
  ]);

  function directText(el) {
    if (!el || !el.childNodes) return "";
    let out = "";
    for (const child of Array.from(el.childNodes)) {
      if (child.nodeType === 3) out += child.nodeValue || "";
    }
    return normalize.cleanText(out);
  }

  function hasAnyText(el) {
    return !!normalize.cleanText(el && el.textContent);
  }

  /**
   * Ordered, leaf-ish text blocks inside a container.
   *
   * A "block" is the smallest element that owns a run of visible text. An
   * element that mixes its own text with text-bearing children is emitted once,
   * whole, so a line such as `Company · <a>School</a>` stays one block instead
   * of fragmenting. Returned in document order.
   */
  function collectBlocks(root) {
    const out = [];
    const visit = (el) => {
      const children = Array.from((el && el.children) || []);
      const own = directText(el);
      const childrenHaveText = children.some(hasAnyText);
      if (own && childrenHaveText) {
        out.push({ el, text: normalize.cleanText(el.textContent) });
        return;
      }
      if (own) {
        out.push({ el, text: own });
        return;
      }
      children.forEach(visit);
    };
    if (root) visit(root);
    return out;
  }

  /** True when the block sits inside an interactive control. */
  function isActionBlock(el, topcardEl) {
    let node = el;
    const stop = topcardEl && topcardEl.parentElement;
    while (node && node !== stop) {
      const tag = (node.tagName || "").toLowerCase();
      if (tag === "button") return true;
      if (tag === "a" && (attr(node, "role") || "").toLowerCase() === "button") return true;
      node = node.parentElement;
    }
    return false;
  }

  function isPronounText(text) {
    return PRONOUN_SET.has(String(text).replace(/[()]/g, "").trim().toLowerCase());
  }

  /**
   * Classify every block so field resolution only ever considers text that is
   * genuinely unaccounted for. Classification is by *shape and role*, never by
   * position.
   */
  function classifyBlocks(blocks, topcardEl, headingEl) {
    const credentials = findCredentialElements(topcardEl, blocks);
    return blocks.map((b) => {
      const text = b.text;
      let kind = null;
      if (headingEl && (b.el === headingEl || headingEl.contains(b.el))) kind = "name";
      else if (credentials.has(b.el)) kind = "credentials";
      else if (SEPARATOR_RE.test(text)) kind = "separator";
      else if (DEGREE_RE.test(text)) kind = "degree";
      else if (isPronounText(text)) kind = "pronoun";
      else if (CONTACT_INFO_RE.test(text)) kind = "contact_info";
      else if (COUNT_WITH_LABEL_RE.test(text)) kind = "count";
      else if (COUNT_RE.test(text)) kind = "count";
      else if (COUNT_LABEL_RE.test(text)) kind = "count";
      else if (MUTUAL_RE.test(text)) kind = "mutuals";
      else if (isActionBlock(b.el, topcardEl)) kind = "action";
      else if (ACTION_TEXT.has(text.toLowerCase())) kind = "action";
      return Object.assign({}, b, { kind });
    });
  }

  // Headings that introduce a *section* of the profile rather than name the
  // person. If the first heading found is one of these, the container is not
  // the top card — returning it would capture the section title as the
  // person's name, which is precisely the kind of silent wrong answer #167
  // exists to remove.
  const SECTION_HEADINGS = new Set([
    "about", "activity", "experience", "education", "skills", "interests",
    "recommendations", "licenses & certifications", "licenses and certifications",
    "volunteering", "volunteer experience", "courses", "projects", "languages",
    "honors & awards", "honors and awards", "publications", "patents",
    "organizations", "test scores", "causes", "featured", "highlights",
    "people also viewed", "more profiles for you", "analytics", "resources",
    "suggested for you", "top voice", "recommended for you",
  ]);

  function isSectionHeading(el) {
    const text = normalize.cleanText(el && el.textContent).toLowerCase();
    return SECTION_HEADINGS.has(text);
  }

  /**
   * The heading that names the person.
   *
   * The name is an `<h2>` on the current DOM and an `<h1>` on older markup;
   * `[role=heading]` covers a div styled as a heading. Whichever it is, a
   * section heading is never the name.
   */
  function findHeadingElement(container) {
    if (!container || typeof container.querySelectorAll !== "function") return null;
    const headings = Array.from(container.querySelectorAll("h1, h2, h3, [role='heading']"));
    for (const el of headings) {
      if (!isSectionHeading(el)) return el;
    }
    return null;
  }

  /**
   * The verification badge, when present.
   *
   * `svg[id]` is the only semantically stable hook in the top card —
   * `verified-small`, `verified-medium`, `company-accent-*`, `school-accent-*`
   * survive the class rotation that destroys everything else. The badge is
   * sometimes a bare `<svg>` and sometimes nested inside an `<a>` carrying the
   * profile handle; matching the icon rather than the wrapper covers both.
   *
   * Used here to locate the *name row*, not to read a value: the wrapper's
   * attributes carry identity and are never touched.
   */
  function findVerificationIcon(topcardEl) {
    if (!topcardEl || typeof topcardEl.querySelector !== "function") return null;
    return topcardEl.querySelector("svg[id^='verified-']");
  }

  /**
   * Blocks belonging to the company · school line, identified by the logo slots
   * beside them rather than by their position or their text.
   *
   * A `<figure>` containing `svg[id^="company-"]` or `svg[id^="school-"]` marks
   * the row. Note that the slot existing does not mean a logo rendered — one
   * observed profile has the placeholder `<svg>` and no `<img>` — so presence
   * of the figure is used only to locate the row.
   */
  function findCredentialElements(topcardEl, blocks) {
    const marked = new Set();
    if (!topcardEl || typeof topcardEl.querySelectorAll !== "function") return marked;
    const figures = Array.from(topcardEl.querySelectorAll("figure")).filter(
      (f) => f.querySelector && f.querySelector("svg[id^='company-'], svg[id^='school-']")
    );
    for (const figure of figures) {
      let node = figure.parentElement;
      let steps = 0;
      while (node && steps < 3 && topcardEl.contains(node)) {
        const inside = blocks.filter((b) => node.contains(b.el));
        if (inside.length) {
          // A row that already holds several lines is not the credential row —
          // it is a wrapper. Stop rather than classifying the whole card.
          if (inside.length <= MAX_CREDENTIAL_ROW_BLOCKS) {
            inside.forEach((b) => marked.add(b.el));
          }
          break;
        }
        node = node.parentElement;
        steps += 1;
      }
    }
    return marked;
  }

  const MAX_CREDENTIAL_ROW_BLOCKS = 3;

  /** Count text-bearing blocks without building the array (cheap enough here). */
  function blockCount(el) {
    return collectBlocks(el).length;
  }

  function findTopcardElement(doc) {
    // Strategy A (componentkey DOM): a key containing 'topcard'. Prefer the one
    // that actually holds the name heading — that excludes the nav mini-card,
    // which repeats the name in a <p> — and among those prefer the *smallest*,
    // since a wider match means the card's own wrapper was missed.
    const byKey = Array.from(doc.querySelectorAll("[componentkey]")).filter((el) =>
      lowerComponentKey(el).includes("topcard")
    );
    if (byKey.length) {
      const withHeading = byKey.filter((el) => findHeadingElement(el));
      const pool = withHeading.length ? withHeading : byKey;
      return pool.reduce((best, el) =>
        (el.textContent || "").length < (best.textContent || "").length ? el : best
      );
    }
    // Strategy B (classic DOM): the pv-top-card block.
    const classic = doc.querySelector(
      ".pv-top-card, section.pv-top-card, [class*='pv-top-card']"
    );
    if (classic) return classic;
    // Strategy C: climb from the name heading, and stop at the block boundary.
    //
    // The old implementation climbed to the nearest <section>. The current DOM
    // has no <section> in the top card, so that climb ran to <main> and
    // swallowed the About section and the activity feed — which is how a
    // 20-node top card became a 200-node capture.
    //
    // Two independent measurements decide where the card ends:
    //
    //   headings — every sibling block on a profile (About, Experience,
    //     Education, …) is introduced by its own heading, while the top card
    //     contains exactly one: the name. An ancestor holding two or more
    //     headings has therefore absorbed a neighbouring block.
    //   size — a genuine top card is a bounded number of text blocks; a sudden
    //     jump between two rungs is a second, independent signal of the same
    //     boundary, used to trim the ladder before the heading rule is applied.
    //
    // The floor matters as much as the boundary: without it, the first rung — a
    // wrapper holding only the name — looks like a valid card and the capture
    // collapses to the name alone.
    const heading = findHeadingElement(doc.querySelector("main") || doc) || findHeadingElement(doc);
    if (!heading) return null;
    const ladder = [];
    let node = heading.parentElement;
    while (node && node.tagName !== "MAIN" && node.tagName !== "BODY" && node.tagName !== "HTML") {
      ladder.push({ el: node, blocks: blockCount(node), headings: headingCount(node) });
      node = node.parentElement;
    }
    if (!ladder.length) return heading;

    let cut = ladder.length - 1;
    for (let i = 0; i + 1 < ladder.length; i += 1) {
      if (ladder[i].blocks < TOPCARD_MIN_BLOCKS) continue;
      if (ladder[i + 1].blocks > ladder[i].blocks * TOPCARD_JUMP_RATIO + TOPCARD_JUMP_SLACK) {
        cut = i;
        break;
      }
    }
    const trimmed = ladder.slice(0, cut + 1);
    const bounded = trimmed.filter((r) => r.headings <= 1 && r.blocks <= TOPCARD_MAX_BLOCKS);

    // Widest rung that is still plausibly the card, preferring one large enough
    // to hold a card's worth of text.
    const sized = bounded.filter((r) => r.blocks >= TOPCARD_MIN_BLOCKS);
    if (sized.length) return sized[sized.length - 1].el;
    if (bounded.length) return bounded[bounded.length - 1].el;
    return ladder[0].el;
  }

  // Bounds for the measured climb above. A genuine top card is roughly 6–25
  // text blocks; About or the activity feed arriving pushes it far past that.
  const TOPCARD_MIN_BLOCKS = 6;
  const TOPCARD_MAX_BLOCKS = 40;
  const TOPCARD_JUMP_RATIO = 1.6;
  const TOPCARD_JUMP_SLACK = 4;

  function headingCount(el) {
    if (!el || typeof el.querySelectorAll !== "function") return 0;
    return el.querySelectorAll("h1, h2, h3, [role='heading']").length;
  }

  // The name row is the wrapper holding the heading together with its badges.
  // Anything inside it that we did not classify (an unlabelled line observed on
  // at least one real profile) must not be mistaken for the headline. Bounded
  // so that a flat card — where the "row" would be the whole top card — falls
  // back to excluding nothing beyond the heading itself.
  const MAX_NAME_ROW_BLOCKS = 6;

  /**
   * Smallest ancestor of the heading that also holds a degree or pronoun badge.
   * Returns the heading itself when no such bounded wrapper exists, which is the
   * safe answer: it excludes nothing that was not already classified.
   */
  function findNameRow(topcardEl, headingEl, classified, verificationIcon) {
    if (!headingEl) return null;
    const markers = classified
      .filter((b) => b.kind === "degree" || b.kind === "pronoun")
      .map((b) => b.el);
    // The verification badge is a name-row marker too, and it is the only one
    // available on a profile that shows neither a degree badge nor pronouns.
    if (verificationIcon) markers.push(verificationIcon);
    if (!markers.length) return headingEl;
    let node = headingEl.parentElement;
    let steps = 0;
    while (node && steps < 6 && topcardEl && topcardEl.contains(node)) {
      if (node !== topcardEl && markers.some((el) => node.contains(el))) {
        return blockCount(node) <= MAX_NAME_ROW_BLOCKS ? node : headingEl;
      }
      node = node.parentElement;
      steps += 1;
    }
    return headingEl;
  }

  /**
   * The location line, resolved structurally.
   *
   * Strategy 1: the unclassified block that shares a row with the "Contact
   * info" link. This is what the live DOM actually guarantees, and it is
   * immune to every count/badge/company-line variation above it.
   *
   * Strategy 2: the last unclassified block before the connection region —
   * used only when at least two unclassified blocks exist, so a profile with a
   * headline and no location does not have its headline read as a location.
   */
  function findLocationBlock(topcardEl, classified, candidates) {
    const contact = classified.find((b) => b.kind === "contact_info");
    if (contact) {
      let node = contact.el.parentElement;
      let steps = 0;
      while (node && steps < 5 && topcardEl && topcardEl.contains(node)) {
        const inside = candidates.filter((b) => node.contains(b.el));
        if (inside.length === 1) return inside[0];
        if (inside.length > 1) break; // the row widened past the location — stop
        node = node.parentElement;
        steps += 1;
      }
    }
    if (candidates.length >= 2) return candidates[candidates.length - 1];
    return null;
  }

  /**
   * Followers and connections, read as tokens rather than as a fixed arity.
   *
   * Observed shapes: `500+ connections` (one node); `500+` + `connections`
   * (two); `29,777 followers` + `·` + `500+` + `connections` (four); and an
   * entirely empty container. A count with no label is left unpaired rather
   * than assumed to be connections.
   */
  function parseCountRegion(blocks) {
    const out = {
      connections: null,
      connectionsRaw: null,
      followers: null,
      followersRaw: null,
      sawRegion: false,
      unpaired: false,
    };
    const toNumber = (raw) => {
      const n = parseInt(String(raw).replace(/[,+]/g, ""), 10);
      return Number.isFinite(n) ? n : null;
    };
    const assign = (label, raw) => {
      const l = String(label).toLowerCase();
      const value = toNumber(raw);
      if (value == null) return;
      if (l.startsWith("connection")) {
        if (out.connections == null) {
          out.connections = value;
          out.connectionsRaw = `${raw} connections`;
        }
      } else if (out.followers == null) {
        out.followers = value;
        out.followersRaw = `${raw} followers`;
      }
    };

    for (let i = 0; i < blocks.length; i += 1) {
      const text = blocks[i].text;
      const combined = text.match(COUNT_WITH_LABEL_RE);
      if (combined) {
        out.sawRegion = true;
        assign(combined[2], combined[1]);
        continue;
      }
      if (COUNT_LABEL_RE.test(text)) {
        out.sawRegion = true;
        continue;
      }
      if (!COUNT_RE.test(text)) continue;
      out.sawRegion = true;
      let paired = false;
      for (let j = i + 1; j < blocks.length && j <= i + 3; j += 1) {
        const next = blocks[j].text;
        if (SEPARATOR_RE.test(next)) continue;
        if (COUNT_LABEL_RE.test(next)) {
          assign(next, text);
          paired = true;
        }
        break;
      }
      if (!paired) out.unpaired = true;
    }
    return out;
  }

  /**
   * Resolve the top card. `warnings` is appended to, never replaced: an
   * unresolved field is always null *plus* a warning, never a guess.
   */
  function parseTopcard(topcardEl, warnings) {
    const rawLines = cleanLines(topcardEl);
    const headingEl = findHeadingElement(topcardEl);
    const blocks = collectBlocks(topcardEl);
    const classified = classifyBlocks(blocks, topcardEl, headingEl);

    // --- name ---------------------------------------------------------------
    // Verbatim, as one string. Names carry commas, professional suffixes and
    // parenthesised alternates; none of that may be split or normalized here.
    // Read the heading's own text first so a verification badge nested inside
    // the heading cannot contribute to it.
    let fullName = null;
    if (headingEl) {
      const inner = collectBlocks(headingEl);
      fullName = directText(headingEl) || (inner[0] && inner[0].text) || normalize.cleanText(headingEl.textContent);
    }
    if (!fullName) {
      const firstUnclassified = classified.find((b) => b.kind == null);
      fullName = (firstUnclassified && firstUnclassified.text) || null;
    }
    if (!fullName) warnings.push({ code: WARNINGS.MISSING_FIELD, field: "full_name" });

    // --- candidate pool -----------------------------------------------------
    const nameRow = findNameRow(topcardEl, headingEl, classified, findVerificationIcon(topcardEl));
    const headingIndex = headingEl ? classified.findIndex((b) => b.el === headingEl || headingEl.contains(b.el)) : -1;
    const firstCountIndex = classified.findIndex((b) => b.kind === "count");

    const candidates = classified.filter((b, index) => {
      if (b.kind != null) return false;
      if (headingIndex >= 0 && index <= headingIndex) return false;
      if (nameRow && nameRow !== headingEl && nameRow.contains(b.el)) return false;
      if (firstCountIndex >= 0 && index > firstCountIndex) return false;
      return true;
    });

    // --- location -----------------------------------------------------------
    const locationBlock = findLocationBlock(topcardEl, classified, candidates);
    const displayedLocation = locationBlock ? locationBlock.text : null;

    // --- headline -----------------------------------------------------------
    // The first unaccounted-for block outside the name row. Everything that
    // could precede it — badges, pronouns, an unlabelled name-row line — is
    // already excluded structurally, and everything that follows it (company ·
    // school, "Talks about", location) is either classified or later in
    // document order.
    const headlineBlock = candidates.find((b) => b !== locationBlock) || null;
    let headline = headlineBlock ? headlineBlock.text : null;
    let headlinePlaceholder = false;
    if (headline && HEADLINE_PLACEHOLDER_RE.test(headline)) {
      headlinePlaceholder = true;
      headline = null;
    }

    if (headlinePlaceholder) {
      warnings.push({
        code: WARNINGS.PLACEHOLDER_VALUE,
        field: "headline",
        raw: headlineBlock.text,
        message: "LinkedIn rendered an empty-headline placeholder; no headline was captured.",
      });
    } else if (!headline) {
      warnings.push({ code: WARNINGS.MISSING_FIELD, field: "headline" });
    }
    if (!displayedLocation) {
      warnings.push({ code: WARNINGS.MISSING_FIELD, field: "displayed_location" });
    }

    return { fullName, headline, displayedLocation, rawLines, blocks: classified };
  }

  /**
   * Connection count, scoped to the top card only.
   *
   * Deliberately not falling back to the whole document: other people's
   * connection counts appear elsewhere on a profile page, and a wrong count
   * attributed to this person is worse than no count. An empty connection
   * region yields null — never zero, because "hidden" and "zero" are different
   * facts.
   */
  function parseConnections(doc, topcardEl, precomputedBlocks) {
    if (!topcardEl) return { count: null, raw: null, followers: null, sawRegion: false, unpaired: false };
    const blocks = precomputedBlocks || collectBlocks(topcardEl);
    const region = parseCountRegion(blocks);
    return {
      count: region.connections,
      raw: region.connectionsRaw,
      followers: region.followers,
      sawRegion: region.sawRegion,
      unpaired: region.unpaired,
    };
  }

  function detectOpenToWork(doc, topcardEl) {
    if (topcardEl && /open to work/i.test(topcardEl.textContent || "")) return true;
    const images = [];
    if (topcardEl && typeof topcardEl.querySelectorAll === "function") {
      images.push(...Array.from(topcardEl.querySelectorAll("img")));
    }
    const legacy = doc.querySelector('img[class*="pv-top-card-profile-picture"]');
    if (legacy) images.push(legacy);
    for (const img of images) {
      const badge = `${attr(img, "alt")} ${attr(img, "title")}`.toLowerCase();
      if (badge.includes("open to work") || badge.includes("#open_to_work")) return true;
    }
    const card = doc.querySelector("section[class*='pv-open-to-carousel-card--enrolled']");
    if (card && /open to work/i.test(card.textContent || "")) return true;
    return false;
  }

  // ---- Experience section --------------------------------------------------

  function findExperienceSection(doc) {
    // Strategy A: a section/heading whose text is exactly "Experience".
    const sections = Array.from(doc.querySelectorAll("section"));
    for (const sec of sections) {
      const heading = sec.querySelector(
        "h2, .pvs-header__title-text, .pv-profile-section__card-heading"
      );
      const headingText = heading && normalize.cleanText(heading.textContent);
      if (headingText && /^experience$/i.test(headingText)) return sec;
    }
    // Strategy B (2025 DOM): componentkey contains 'experience' (not activity).
    for (const sec of sections) {
      const ck = lowerComponentKey(sec);
      if (ck.includes("experience") && !ck.includes("activity")) return sec;
    }
    // Strategy C: any section that mentions Experience and contains
    // entity-collection items.
    for (const sec of sections.slice().reverse()) {
      const text = (sec.textContent || "").toLowerCase();
      if (!text.includes("experience")) continue;
      if (findEntryElements(sec).length) return sec;
    }
    return null;
  }

  function findEntryElements(sectionEl) {
    if (!sectionEl || typeof sectionEl.querySelectorAll !== "function") return [];
    // 2025 DOM: componentkey entity-collection-item wrappers.
    const byKey = Array.from(sectionEl.querySelectorAll("[componentkey]")).filter((el) =>
      lowerComponentKey(el).includes("entity-collection-item")
    );
    // Keep only top-level items (a chained item nests its roles in ul/li, not
    // in nested entity-collection-item keys; but be defensive anyway).
    const topLevel = byKey.filter(
      (el) => !byKey.some((other) => other !== el && other.contains && other.contains(el))
    );
    if (topLevel.length) return topLevel;
    // Classic DOM: artdeco list items directly under the section list.
    const classic = sectionEl.querySelectorAll("li.artdeco-list__item, li[class*='pvs-list__paged-list-item']");
    return Array.from(classic);
  }

  /** 'Chained' when the entry nests multiple roles under one company. */
  function entryLayout(entryEl) {
    const uls = entryEl.querySelectorAll("ul");
    for (const ul of Array.from(uls)) {
      const lis = Array.from(ul.children || []).filter((c) => c.tagName === "LI");
      if (lis.length >= 1) {
        // Nested role list => chained layout (legacy: ul_count >= 1).
        return "chained";
      }
    }
    return "basic";
  }

  function baseEntry(layout, observedAt) {
    return {
      layout,
      company_name: null,
      company_linkedin_url: null,
      company_linkedin_id: null,
      job_title: null,
      timeline_text: null,
      start_date: null,
      end_date: null,
      dates_reliable: false,
      duration_text: null,
      employment_type: null,
      role_location: null,
      workplace_type: null,
      is_current: null,
      raw_lines: [],
      warnings: [],
      observed_at: observedAt,
    };
  }

  function applyTimeline(entry, line) {
    const { timeline, duration } = splitTimelineDuration(line);
    entry.timeline_text = timeline;
    entry.duration_text = duration;
    const parsed = parseTimeline(timeline);
    entry.start_date = parsed.start;
    entry.end_date = parsed.end;
    entry.dates_reliable = parsed.reliable;
    if (parsed.isCurrent != null) entry.is_current = parsed.isCurrent;
    if (!parsed.reliable && timeline) {
      entry.warnings.push({ code: WARNINGS.UNPARSED_TIMELINE, field: "timeline_text", raw: timeline });
    }
  }

  function applyLocationLine(entry, line) {
    const parts = String(line).split("·").map((p) => normalize.cleanText(p));
    if (parts.length === 1 && parts[0] && WORKPLACE_TYPES.has(parts[0])) {
      // A bare "Remote"/"Hybrid"/"On-site" line is a workplace type, not a place.
      entry.workplace_type = parts[0];
      return;
    }
    entry.role_location = parts[0] || null;
    if (parts[1]) entry.workplace_type = parts[1]; // preserve the displayed value verbatim
  }

  /** A location line: "City, Country · Hybrid" or a bare workplace type. */
  function looksLikeLocation(line) {
    if (!line) return false;
    if (WORKPLACE_TYPES.has(line)) return true;
    const parts = line.split("·").map((p) => p.trim());
    if (parts.length > 1 && WORKPLACE_TYPES.has(parts[parts.length - 1])) return true;
    // "City, State/Country" style — contains a comma, no digits or date tokens.
    return /,/.test(line) && !looksLikeTimeline(line) && !/\d{4}/.test(line);
  }

  function parseBasicEntry(entryEl, observedAt) {
    const entry = baseEntry("basic", observedAt);
    const lines = cleanLines(entryEl);
    entry.raw_lines = lines;

    entry.job_title = lines[0] || null;
    if (!entry.job_title) entry.warnings.push({ code: WARNINGS.MISSING_FIELD, field: "job_title" });

    if (lines.length > 1) {
      const parts = lines[1].split("·").map((p) => normalize.cleanText(p));
      entry.company_name = parts[0] || null;
      if (parts[1] && EMPLOYMENT_TYPES.has(parts[1])) entry.employment_type = parts[1];
    }
    if (!entry.company_name) entry.warnings.push({ code: WARNINGS.MISSING_FIELD, field: "company_name" });

    // Timeline: the first line after [title, company] that looks like one
    // (defensive against extra badge lines between).
    let timelineIdx = null;
    for (let i = 2; i < lines.length; i += 1) {
      if (looksLikeTimeline(lines[i])) {
        timelineIdx = i;
        applyTimeline(entry, lines[i]);
        break;
      }
      if (EMPLOYMENT_TYPES.has(lines[i]) && !entry.employment_type) {
        entry.employment_type = lines[i];
      }
    }
    if (entry.timeline_text == null) {
      entry.warnings.push({ code: WARNINGS.MISSING_FIELD, field: "timeline_text" });
    }

    if (timelineIdx != null && timelineIdx + 1 < lines.length && looksLikeLocation(lines[timelineIdx + 1])) {
      applyLocationLine(entry, lines[timelineIdx + 1]);
    }

    entry.company_linkedin_url = companyUrlFromElement(entryEl);
    entry.company_linkedin_id = companyIdFromUrl(entry.company_linkedin_url);
    return [entry];
  }

  function parseChainedEntry(entryEl, observedAt) {
    const results = [];
    const allLines = cleanLines(entryEl);
    const companyName = allLines[0] || null;
    const companyUrl = companyUrlFromElement(entryEl);
    const companyId = companyIdFromUrl(companyUrl);

    // Company header line 2 is usually "<employment type> · <total duration>"
    // or "Full-time · 8 yrs" — keep it raw only.
    let nestedItems = [];
    const uls = Array.from(entryEl.querySelectorAll("ul"));
    for (const ul of uls) {
      const lis = Array.from(ul.children || []).filter((c) => c.tagName === "LI");
      if (lis.length) {
        nestedItems = lis;
        break;
      }
    }

    for (const item of nestedItems) {
      const entry = baseEntry("chained", observedAt);
      entry.company_name = companyName;
      entry.company_linkedin_url = companyUrl;
      entry.company_linkedin_id = companyId;

      const lines = cleanLines(item);
      entry.raw_lines = lines;
      if (!lines.length) continue;

      entry.job_title = lines[0] || null;

      let timelineIdx = null;
      for (let i = 1; i < lines.length; i += 1) {
        const line = lines[i];
        if (looksLikeTimeline(line)) {
          timelineIdx = i;
          applyTimeline(entry, line);
          break;
        }
        if (EMPLOYMENT_TYPES.has(line)) entry.employment_type = line;
        // "Full-time · …" prefix form
        const first = normalize.cleanText(line.split("·")[0]);
        if (!entry.employment_type && first && EMPLOYMENT_TYPES.has(first)) {
          entry.employment_type = first;
        }
      }
      if (entry.timeline_text == null) {
        entry.warnings.push({ code: WARNINGS.MISSING_FIELD, field: "timeline_text" });
      }
      if (timelineIdx != null && timelineIdx + 1 < lines.length && looksLikeLocation(lines[timelineIdx + 1])) {
        applyLocationLine(entry, lines[timelineIdx + 1]);
      }
      if (!entry.company_name) {
        entry.warnings.push({ code: WARNINGS.MISSING_FIELD, field: "company_name" });
      }
      results.push(entry);
    }

    if (!results.length) {
      // A chained wrapper with no parseable nested roles: fail visibly for this
      // entry (parse nothing) rather than inventing a role.
      const entry = baseEntry("chained", observedAt);
      entry.company_name = companyName;
      entry.company_linkedin_url = companyUrl;
      entry.company_linkedin_id = companyId;
      entry.raw_lines = allLines;
      entry.warnings.push({ code: WARNINGS.UNRECOGNIZED_LAYOUT, field: "experience_entry" });
      results.push(entry);
    }
    return results;
  }

  // ---- About section -------------------------------------------------------

  // Longest About text kept, mirroring the wire contract's bound. A profile
  // longer than this is truncated at a whole line rather than mid-sentence.
  const MAX_ABOUT_CHARS = 8000;

  /** The rendered About section, or null. Heading text first, component key second. */
  function findAboutSection(doc) {
    if (!doc || typeof doc.querySelectorAll !== "function") return null;
    for (const sec of Array.from(doc.querySelectorAll("section"))) {
      const heading = sec.querySelector("h2, .pvs-header__title-text");
      const t = heading && normalize.cleanText(heading.textContent);
      if (t && /^about$/i.test(t)) return sec;
      if (lowerComponentKey(sec).includes("about")) return sec;
    }
    return null;
  }

  /**
   * The visible About body, verbatim, minus the section heading and the
   * expand/collapse affordances LinkedIn renders inside it. Returns null when
   * the section is present but shows no readable body.
   */
  function aboutTextFrom(sectionEl) {
    const lines = cleanLines(sectionEl).filter((line) => {
      if (/^about$/i.test(line)) return false;
      if (/^(…|\.\.\.)?\s*see more$/i.test(line)) return false;
      if (/^see less$/i.test(line)) return false;
      return true;
    });
    if (!lines.length) return null;
    let text = lines.join("\n");
    if (text.length > MAX_ABOUT_CHARS) {
      const cut = text.slice(0, MAX_ABOUT_CHARS);
      const lastBreak = cut.lastIndexOf("\n");
      text = lastBreak > 0 ? cut.slice(0, lastBreak) : cut;
    }
    return text || null;
  }

  // ---- Public entry point --------------------------------------------------

  /**
   * Extract the first-release profile fields from an operator-opened MAIN
   * LinkedIn profile page.
   *
   * @param {Document} doc
   * @param {{sourceUrl?: string, capturedAt?: string}} options
   * @returns {{status, surface, profile, experiences, missingSections,
   *            pageWarnings, sourceUrl, capturedAt, adapterVersion}}
   */
  function extractProfile(doc, options) {
    const opts = options || {};
    const sourceUrl = opts.sourceUrl || null;
    const capturedAt = opts.capturedAt || null;
    const pageWarnings = [];

    const detected = surface.detectSurface(sourceUrl, doc);
    if (detected.surface === SURFACES.CHALLENGE) {
      return failure(CAPTURE_STATUS.CHALLENGE_DETECTED, detected, sourceUrl, capturedAt, [
        { code: "challenge", reason: detected.reason },
      ]);
    }
    if (detected.surface === SURFACES.UNAVAILABLE) {
      return failure(CAPTURE_STATUS.UNAVAILABLE_PROFILE, detected, sourceUrl, capturedAt, [
        { code: "unavailable_profile", reason: detected.reason },
      ]);
    }
    if (detected.surface !== SURFACES.PERSON_PROFILE) {
      return failure(CAPTURE_STATUS.UNSUPPORTED_PAGE, detected, sourceUrl, capturedAt, [
        {
          code: "unsupported_page",
          url: sourceUrl,
          reason: detected.reason || "not_person_profile",
          message:
            "Only a main LinkedIn profile page (linkedin.com/in/…) can be captured in this mode.",
        },
      ]);
    }

    const normalizedProfileUrl = normalize.normalizeLinkedInUrl(sourceUrl);
    const topcardEl = findTopcardElement(doc);
    const missingSections = [];
    const profileWarnings = [];

    if (!topcardEl) {
      // No name/headline block at all on a /in/ URL: the structure is not one
      // we recognize. Fail visibly; capture nothing.
      return failure(
        CAPTURE_STATUS.STRUCTURE_UNRECOGNIZED,
        detected,
        sourceUrl,
        capturedAt,
        [
          {
            code: "structure_unrecognized",
            message:
              "Profile URL detected but no top card could be parsed. The page structure " +
              "may have changed. Nothing was captured.",
          },
        ]
      );
    }

    const topcard = parseTopcard(topcardEl, profileWarnings);
    const connections = parseConnections(doc, topcardEl, topcard.blocks);
    const openToWork = detectOpenToWork(doc, topcardEl);

    const profile = {
      linkedin_profile_url: normalizedProfileUrl.valid ? normalizedProfileUrl.url : null,
      public_identifier: detected.publicIdentifier || null,
      full_name: topcard.fullName,
      headline: topcard.headline,
      displayed_location: topcard.displayedLocation,
      connection_count: connections.count,
      connection_count_raw: connections.raw,
      open_to_work: openToWork,
      observed_at: capturedAt,
      raw_lines: topcard.rawLines,
      warnings: profileWarnings,
    };
    if (!profile.linkedin_profile_url) {
      profileWarnings.push({ code: WARNINGS.MALFORMED_URL, field: "linkedin_profile_url", raw: sourceUrl });
    }
    if (connections.count == null) {
      // Distinguish "the page did not show a connection region at all" from
      // "it showed something we could not pair". Both leave the count null;
      // only the second means the parser met a shape it did not understand.
      profileWarnings.push(
        connections.sawRegion
          ? {
              code: WARNINGS.UNPARSED_VALUE,
              field: "connection_count",
              message:
                "A connection region was present but no count could be paired with a " +
                "'connections' label. Nothing was assumed.",
            }
          : { code: WARNINGS.MISSING_FIELD, field: "connection_count" }
      );
    }

    // Experience.
    let experiences = [];
    const expSection = findExperienceSection(doc);
    if (!expSection) {
      missingSections.push("experience");
      pageWarnings.push({ code: WARNINGS.MISSING_SECTION, section: "experience" });
    } else {
      const entries = findEntryElements(expSection);
      if (!entries.length) {
        missingSections.push("experience_entries");
        pageWarnings.push({
          code: WARNINGS.UNRECOGNIZED_LAYOUT,
          section: "experience",
          message:
            "An Experience section was found but no entries could be parsed. The layout may have changed.",
        });
      }
      for (const entryEl of entries) {
        const layout = entryLayout(entryEl);
        const parsed =
          layout === "chained"
            ? parseChainedEntry(entryEl, capturedAt)
            : parseBasicEntry(entryEl, capturedAt);
        experiences = experiences.concat(parsed);
      }
      experiences.forEach((e, i) => {
        e.position_index = i + 1;
      });
    }

    // About section. Only what is already rendered on the opened page is read:
    // the extension never expands "see more", never fetches, and never
    // summarizes. An absent section is reported rather than guessed at.
    const aboutSection = findAboutSection(doc);
    const aboutText = aboutSection ? aboutTextFrom(aboutSection) : null;
    if (!aboutSection) missingSections.push("about");
    else if (!aboutText) missingSections.push("about_text");
    profile.about_text = aboutText;

    const status =
      missingSections.length || pageWarnings.length || profileWarnings.length
        ? CAPTURE_STATUS.PARTIAL
        : CAPTURE_STATUS.OK;

    return {
      status,
      surface: detected.surface,
      profile,
      experiences,
      missingSections,
      pageWarnings,
      sourceUrl,
      capturedAt,
      adapterVersion: ADAPTER_VERSION,
    };
  }

  function failure(status, detected, sourceUrl, capturedAt, pageWarnings) {
    return {
      status,
      surface: detected.surface,
      profile: null,
      experiences: [],
      missingSections: [],
      pageWarnings,
      sourceUrl,
      capturedAt,
      adapterVersion: ADAPTER_VERSION,
    };
  }

  return {
    ADAPTER_VERSION,
    extractProfile,
    // exported for tests
    _internals: {
      parseTimeline,
      splitTimelineDuration,
      looksLikeTimeline,
      looksLikeLocation,
      parseTopcard,
      parseConnections,
      parseCountRegion,
      collectBlocks,
      classifyBlocks,
      findHeadingElement,
      findNameRow,
      detectOpenToWork,
      findTopcardElement,
      findExperienceSection,
      findEntryElements,
      entryLayout,
      companyIdFromUrl,
      cleanLines,
      findAboutSection,
      aboutTextFrom,
    },
  };
});

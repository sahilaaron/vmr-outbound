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

  // ---- Topcard -------------------------------------------------------------

  const TOPCARD_NOISE = new Set([
    "contact info", "connect", "message", "more", "follow", "connections",
    "followers", "mutual connections", "open to", "add profile section",
    "enhance profile", "resources", "pending", "view my verifications",
  ]);
  const PRONOUN_MARKERS = ["he/him", "she/her", "they/them"];
  const DEGREE_MARKERS = ["· 1st", "· 2nd", "· 3rd", "1st degree", "2nd degree", "3rd degree"];

  function findTopcardElement(doc) {
    // Strategy A (2025 DOM): componentkey containing 'topcard'. When several
    // match, prefer the one containing an h1/h2 (the main card), then the one
    // with the most text (the nav mini-card is short) — mirrors the legacy
    // "index [1]" workaround without hardcoding an index.
    const byKey = Array.from(doc.querySelectorAll("[componentkey]")).filter((el) =>
      lowerComponentKey(el).includes("topcard")
    );
    if (byKey.length) {
      const withHeading = byKey.filter((el) => el.querySelector && el.querySelector("h1, h2"));
      const pool = withHeading.length ? withHeading : byKey;
      return pool.reduce((best, el) =>
        (el.textContent || "").length > (best.textContent || "").length ? el : best
      );
    }
    // Strategy B (classic DOM): the section containing the pv-top-card block.
    const classic = doc.querySelector(
      ".pv-top-card, section.pv-top-card, [class*='pv-top-card']"
    );
    if (classic) return classic;
    // Strategy C: the section containing the page <h1>.
    const h1 = doc.querySelector("main h1, h1");
    if (h1) {
      let node = h1;
      while (node && node.parentElement && node.tagName !== "SECTION") node = node.parentElement;
      return node || h1;
    }
    return null;
  }

  function isNoiseLine(line) {
    const l = line.toLowerCase();
    if (TOPCARD_NOISE.has(l)) return true;
    if (l === "·") return true;
    if (/^\d[\d,]*\+?\s+(connections|followers)$/.test(l)) return true;
    if (/^(contact info|connect|message|more|follow)$/.test(l)) return true;
    return false;
  }

  function parseTopcard(topcardEl, warnings) {
    const rawLines = cleanLines(topcardEl);
    const clean = rawLines.filter((l) => !isNoiseLine(l));

    // Prefer the explicit heading for the name when present.
    let fullName = null;
    const heading = topcardEl && topcardEl.querySelector && topcardEl.querySelector("h1, h2");
    if (heading) fullName = normalize.cleanText(heading.textContent);
    if (!fullName) fullName = clean[0] || null;
    if (!fullName) warnings.push({ code: WARNINGS.MISSING_FIELD, field: "full_name" });

    const afterName = [];
    let seenName = !fullName;
    for (const line of clean) {
      if (!seenName) {
        if (line === fullName) seenName = true;
        continue;
      }
      const l = line.toLowerCase();
      if (PRONOUN_MARKERS.some((m) => l.includes(m))) continue;
      if (DEGREE_MARKERS.some((m) => l.includes(m))) continue;
      if (/^(1st|2nd|3rd)$/.test(l)) continue;
      afterName.push(line);
    }

    const headline = afterName[0] || null;
    const displayedLocation = afterName[1] || null;
    if (!headline) warnings.push({ code: WARNINGS.MISSING_FIELD, field: "headline" });
    if (!displayedLocation) warnings.push({ code: WARNINGS.MISSING_FIELD, field: "displayed_location" });

    return { fullName, headline, displayedLocation, rawLines };
  }

  // Connection count: regex over the topcard/body text (legacy behaviour, but
  // WITHOUT the legacy `>= 15` clamp — the true visible value is preserved and
  // policy decisions belong to the backend).
  function parseConnections(doc, topcardEl) {
    const scopes = [];
    if (topcardEl && topcardEl.textContent) scopes.push(topcardEl.textContent);
    if (doc && doc.body && doc.body.textContent) scopes.push(doc.body.textContent);
    for (const text of scopes) {
      const m = String(text).toLowerCase().match(/(\d[\d,]*\+?)\s+connections/);
      if (m) {
        const raw = m[1];
        const n = parseInt(raw.replace(/,/g, "").replace(/\+/g, ""), 10);
        if (Number.isFinite(n)) {
          return { count: n, raw: `${raw} connections` };
        }
      }
    }
    return { count: null, raw: null };
  }

  function detectOpenToWork(doc, topcardEl) {
    if (topcardEl && /open to work/i.test(topcardEl.textContent || "")) return true;
    const img = doc.querySelector('img[class*="pv-top-card-profile-picture"]');
    if (img) {
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
    const connections = parseConnections(doc, topcardEl);
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
      profileWarnings.push({ code: WARNINGS.MISSING_FIELD, field: "connection_count" });
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

    // About section presence (content itself is out of first-release scope,
    // but its absence is reported so the operator sees what was on the page).
    const hasAbout = Array.from(doc.querySelectorAll("section")).some((sec) => {
      const heading = sec.querySelector("h2, .pvs-header__title-text");
      const t = heading && normalize.cleanText(heading.textContent);
      if (t && /^about$/i.test(t)) return true;
      return lowerComponentKey(sec).includes("about");
    });
    if (!hasAbout) missingSections.push("about");

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
      detectOpenToWork,
      findTopcardElement,
      findExperienceSection,
      findEntryElements,
      entryLayout,
      companyIdFromUrl,
      cleanLines,
    },
  };
});

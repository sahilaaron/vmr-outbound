/**
 * Core extraction logic for Sales Navigator result pages.
 *
 * Design principle (from the notebook behaviour map): the durable hooks are the
 * `data-anonymize="*"` semantic attributes; the `artdeco-* / pl3 pv3` layout
 * classes are fragile. So extraction:
 *   1. Discovers result rows STRUCTURALLY (nearest list-item ancestor of a
 *      person-name node) rather than matching exact class strings.
 *   2. Runs an ORDERED list of strategies per field; the first that yields a
 *      value wins, and a missing value produces an explicit warning, never a
 *      guess.
 *   3. Fails VISIBLY: a page that looks like SN results but yields zero rows is
 *      reported as `structure_unrecognized`, never as a successful empty capture.
 *
 * This module is DOM-agnostic: callers pass a `document` (real DOM in the content
 * script, jsdom in tests). It performs no network access and no mutation of the
 * page.
 *
 * UMD module -> Node CommonJS + self.SNCapture.extraction
 */
(function (root, factory) {
  const factoryResult = factory(
    typeof module !== "undefined" && module.exports
      ? require("./normalize.js")
      : (typeof self !== "undefined" ? self : root).SNCapture.normalize,
    typeof module !== "undefined" && module.exports
      ? require("./constants.js")
      : (typeof self !== "undefined" ? self : root).SNCapture.constants
  );
  if (typeof module !== "undefined" && module.exports) module.exports = factoryResult;
  const g = typeof self !== "undefined" ? self : root;
  g.SNCapture = Object.assign(g.SNCapture || {}, { extraction: factoryResult });
})(typeof globalThis !== "undefined" ? globalThis : this, function (normalize, constants) {
  "use strict";

  const { WARNINGS, CAPTURE_STATUS } = constants;

  // ---- Page classification ------------------------------------------------

  function isSalesNavHost(u) {
    return /(^|\.)linkedin\.com$/.test(u.hostname);
  }

  // Known lead/people search RESULT routes (path only; filters are query params).
  const PEOPLE_RESULT_ROUTES = [
    /^\/sales\/search\/people$/,
    /^\/sales\/search\/results\/people$/,
  ];

  // Sales Navigator surfaces that are explicitly NOT people search results and
  // must be rejected (account/company search + company pages/lists).
  const REJECTED_SALES_ROUTES = [
    /^\/sales\/search\/company$/,
    /^\/sales\/search\/companies$/,
    /^\/sales\/search\/accounts$/,
    /^\/sales\/search\/results\/company$/,
    /^\/sales\/company(\/|$)/,
    /^\/sales\/lists\/company(\/|$)/,
  ];

  /**
   * True ONLY for a Sales Navigator lead/people search RESULT page. There is no
   * broad `/search/` fallback: account/company search and every other Sales
   * Navigator surface are unsupported.
   */
  function isSupportedResultsUrl(url) {
    if (!url) return false;
    let u;
    try {
      u = new URL(url);
    } catch (_e) {
      return false;
    }
    if (!isSalesNavHost(u)) return false;
    const path = u.pathname.replace(/\/+$/, "") || "/";
    return PEOPLE_RESULT_ROUTES.some((re) => re.test(path));
  }

  /**
   * True for a Sales Navigator surface we explicitly reject (account/company
   * search, company pages). Exposed so callers/tests can distinguish "wrong SN
   * surface" from "not Sales Navigator at all". Both are unsupported for capture.
   */
  function isRejectedSalesSurface(url) {
    if (!url) return false;
    let u;
    try {
      u = new URL(url);
    } catch (_e) {
      return false;
    }
    if (!isSalesNavHost(u)) return false;
    const path = u.pathname.replace(/\/+$/, "") || "/";
    return REJECTED_SALES_ROUTES.some((re) => re.test(path));
  }

  /**
   * Detect a login / checkpoint / security-challenge state so the extension can
   * HALT visibly instead of returning empty data. Conservative: only flags on
   * clear signals.
   */
  function detectChallenge(doc, url) {
    if (url && /\/checkpoint\/|\/authwall|\/uas\/login|\/challenge/i.test(url)) {
      return { detected: true, reason: "challenge_url" };
    }
    const bodyText = (doc && doc.body && doc.body.textContent) || "";
    const signals = [
      /let'?s do a quick security check/i,
      /unusual activity/i,
      /verify (?:you'?re|that you are) (?:a )?human/i,
      /security verification/i,
      /confirm your identity/i,
      /captcha/i,
      /please complete this security check/i,
    ];
    if (signals.some((re) => re.test(bodyText))) {
      return { detected: true, reason: "challenge_text" };
    }
    // Known challenge iframe/container hooks.
    if (
      doc &&
      typeof doc.querySelector === "function" &&
      doc.querySelector(
        'iframe[src*="challenge"], #captcha-internal, [data-test-challenge], .challenge-dialog'
      )
    ) {
      return { detected: true, reason: "challenge_element" };
    }
    return { detected: false, reason: null };
  }

  // ---- Row discovery ------------------------------------------------------

  const ROW_ANCESTOR_SELECTOR =
    'li[data-x-search-result], li.artdeco-list__item, li[role="listitem"], [role="listitem"], li';

  /**
   * Find result-row containers. Strategy order:
   *   A. Structural: nearest list-item ancestor of each person-name node.
   *   B. Fallback: explicit `li.artdeco-list__item` nodes that contain a
   *      person-name.
   *   C. Fallback: entity-lockup blocks that contain a person-name.
   * Returns a de-duplicated, document-ordered array of Elements.
   */
  function findResultContainers(doc) {
    const nameNodes = Array.from(
      doc.querySelectorAll('[data-anonymize="person-name"]')
    );
    const containers = [];
    const seen = new Set();

    const push = (el) => {
      if (el && !seen.has(el)) {
        seen.add(el);
        containers.push(el);
      }
    };

    for (const nameNode of nameNodes) {
      // Strategy A: climb to the nearest sensible list-item ancestor.
      let container = closestMatch(nameNode, ROW_ANCESTOR_SELECTOR);
      // Strategy C fallback: an entity-lockup wrapper.
      if (!container) {
        container = closestMatch(
          nameNode,
          '[class*="entity-lockup"], [class*="result-lockup"], article'
        );
      }
      // Last resort: the name node's parent element.
      if (!container) container = nameNode.parentElement || nameNode;
      push(container);
    }

    // If structural discovery found nothing, try explicit fragile selectors as a
    // pure fallback so we still work if `data-anonymize` ever disappears.
    if (containers.length === 0) {
      const explicit = doc.querySelectorAll(
        'li.artdeco-list__item, li[data-x-search-result], .search-results__result-item'
      );
      for (const el of Array.from(explicit)) push(el);
    }
    return containers;
  }

  /**
   * Detect an explicit "no results" state so a legitimately empty search is
   * reported as EMPTY rather than as a broken/changed structure. Conservative.
   */
  function detectNoResults(doc) {
    if (!doc) return false;
    if (
      typeof doc.querySelector === "function" &&
      doc.querySelector(
        '.search-results__no-results, [data-test-search-no-results], .artdeco-empty-state'
      )
    ) {
      return true;
    }
    const text = (doc.body && doc.body.textContent) || "";
    return /no results found|couldn't find any results|we couldn't find|try a different search|0 results/i.test(
      text
    );
  }

  /** closest() with a manual fallback for environments/quirks. */
  function closestMatch(el, selector) {
    let node = el;
    while (node && node.nodeType === 1) {
      if (typeof node.matches === "function" && node.matches(selector)) return node;
      node = node.parentElement;
    }
    return null;
  }

  // ---- Field strategies ---------------------------------------------------

  function firstText(container, selectors) {
    for (const sel of selectors) {
      const el = container.querySelector(sel);
      if (el) {
        const t = normalize.cleanText(el.textContent);
        if (t) return { value: t, selector: sel, el };
      }
    }
    return { value: null, selector: null, el: null };
  }

  function firstHref(container, selectors) {
    for (const sel of selectors) {
      const el = container.querySelector(sel);
      if (el) {
        const href = el.getAttribute("href");
        if (normalize.cleanText(href)) return { value: href, selector: sel };
      }
    }
    return { value: null, selector: null };
  }

  const NAME_SELECTORS = [
    '[data-anonymize="person-name"]',
    'a[href*="/sales/lead/"] span[dir="ltr"]',
    ".artdeco-entity-lockup__title a",
    ".artdeco-entity-lockup__title",
  ];
  const TITLE_SELECTORS = [
    '[data-anonymize="title"]',
    ".artdeco-entity-lockup__subtitle",
    '[class*="entity-lockup__subtitle"]',
  ];
  const COMPANY_NAME_SELECTORS = [
    'a[data-anonymize="company-name"]',
    '[data-anonymize="company-name"]',
    'a[data-control-name="view_company_via_result_name"]',
    ".artdeco-entity-lockup__subtitle a",
  ];
  const LOCATION_SELECTORS = [
    '[data-anonymize="location"]',
    '[class*="entity-lockup__caption"]',
  ];
  const LEAD_HREF_SELECTORS = [
    'a[data-anonymize="person-name"]',
    'a[href*="/sales/lead/"]',
    'a[href*="/sales/people/"]',
    ".artdeco-entity-lockup__title a",
  ];
  const COMPANY_HREF_SELECTORS = [
    'a[data-anonymize="company-name"]',
    'a[data-control-name="view_company_via_result_name"]',
    'a[href*="/sales/company/"]',
    'a[href*="/company/"]',
  ];
  const PUBLIC_PROFILE_SELECTORS = ['a[href*="/in/"]'];
  const PUBLIC_COMPANY_SELECTORS = ['a[href*="linkedin.com/company/"]'];

  // The subtitle line(s) of a row that may carry an employer.
  //
  // Deliberately NOT `[class*="entity-lockup__subtitle"]`. That pattern matches
  // whatever LinkedIn happens to hang a subtitle-ish class on — a degree badge,
  // a school row, an insight line — and a fallback that reads "the next
  // subtitle-shaped thing" is not reading the employer, it is reading the next
  // thing. UCR-001: it produced `3rd degree connection`, `Munich, Germany` and
  // `University of Somewhere` as company names.
  const SUBTITLE_BLOCK_SELECTORS = [".artdeco-entity-lockup__subtitle"];

  // Nodes that can NEVER be the employer, however they are laid out. Structural
  // (attribute/class), not semantic: the durable `data-anonymize` hooks say what
  // a node IS, and a location is not an employer no matter what it reads like.
  const NOT_EMPLOYER_SELECTOR = [
    '[data-anonymize="location"]',
    '[data-anonymize="industry"]',
    '[data-anonymize="school"]',
    '[data-anonymize="degree"]',
    '[data-anonymize="person-name"]',
    '[data-anonymize="title"]',
    '[class*="entity-lockup__caption"]',
    '[class*="entity-lockup__metadata"]',
    '[class*="entity-lockup__degree"]',
    '[class*="entity-lockup__badge"]',
    '[class*="member-insights"]',
    '[class*="shared-connections"]',
  ].join(", ");

  // Interface furniture that is never a company name. A CLOSED list of things
  // LinkedIn renders about a relationship, not a guess about arbitrary text: a
  // string that is not one of these is left exactly alone, never "interpreted".
  const NOT_EMPLOYER_TEXT_RE = [
    /^\d+(?:st|nd|rd|th)\s+degree(?:\s+connection)?$/i,
    /^(?:1st|2nd|3rd)$/i,
    /^\d[\d,]*\+?\s+(?:connections?|followers?|mutual connections?)$/i,
    /^\d[\d,]*\+?\s+shared\s+connections?$/i,
    /^shared\s+connections?$/i,
  ];

  // The separators LinkedIn actually renders when it hangs extra facts off one
  // line, and nothing else. `-` and `,` are ordinary punctuation inside real
  // company names ("Cliffside Software, Inc.", "Harbor-Freight"); `|` appears
  // inside headlines and could appear inside a name; the KATAKANA MIDDLE DOT
  // U+30FB is a letter-level separator in Japanese names and is deliberately NOT
  // here. Cutting on any of those would truncate the employer rather than trim
  // the metadata, which is the failure this exists to prevent, inverted.
  const METADATA_SEPARATOR_RE = /\s*[·•]\s*/;

  // What visibly joins a title to its employer: "VP Sales AT Acme",
  // "VP Sales · Acme". Applied ONLY to the text run that directly abuts the
  // title element — see `employerAfterTitle`. Applying it to the whole flattened
  // remainder is UCR-003: it cannot tell the connective "at" from the first word
  // of "At Home Group", and turns a real employer into "Home Group".
  const COMPANY_CONNECTOR_RE = /^(?:at|@)(?:\s+|$)|^[·•|,\-–—]\s*/i;

  function isNotEmployerNode(el) {
    if (!el || el.nodeType !== 1) return false;
    if (typeof el.matches === "function" && el.matches(NOT_EMPLOYER_SELECTOR)) return true;
    return typeof el.querySelector === "function" && !!el.querySelector(NOT_EMPLOYER_SELECTOR);
  }

  /**
   * Reduce a candidate string to the employer it can honestly claim to name, or
   * null.
   *
   * Trims at the first metadata separator — `Acme Ltd · 500+ connections`
   * describes a company AND a connection count, and storing both as the company
   * is contaminated evidence (UCR-002). Then refuses the result outright if what
   * survives is interface furniture rather than a name.
   */
  function employerFromText(raw) {
    const text = normalize.cleanText(raw);
    if (!text) return null;
    const head = normalize.cleanText(text.split(METADATA_SEPARATOR_RE)[0]);
    if (!head) return null;
    if (NOT_EMPLOYER_TEXT_RE.some((re) => re.test(head))) return null;
    return head;
  }

  /** The row's employer-eligible subtitle blocks, document-ordered. */
  function subtitleBlocks(container) {
    const seen = new Set();
    const blocks = [];
    for (const sel of SUBTITLE_BLOCK_SELECTORS) {
      container.querySelectorAll(sel).forEach((el) => {
        if (!seen.has(el)) {
          seen.add(el);
          blocks.push({ el, selector: sel });
        }
      });
    }
    return blocks;
  }

  /** The block's own direct child that contains `el`, or null. */
  function childHolding(block, el) {
    for (const child of Array.from(block.childNodes)) {
      if (child === el || (child.nodeType === 1 && child.contains && child.contains(el))) {
        return child;
      }
    }
    return null;
  }

  /**
   * The employer named on the SAME subtitle line as the title, after it.
   *
   * Read NODE BY NODE rather than as one flattened string, because the two
   * questions "is this text the connective?" and "may this node be an employer?"
   * can only be answered per node:
   *
   *   • a connective can only appear in the text run that directly abuts the
   *     title, so that is the only place one is ever removed, and only once.
   *     `<span title>Buyer</span> at At Home Group` yields `At Home Group`;
   *     `<span title>Buyer</span><span> At Home Group</span>` is element-sourced,
   *     so nothing is stripped and the name survives intact.
   *   • a location/school/degree node is refused wherever it sits, so
   *     `<span title>CFO</span> at <span location>Munich</span>` yields nothing
   *     rather than a city.
   */
  function employerAfterTitle(block, titleEl) {
    const holder = childHolding(block, titleEl);
    if (!holder) return null;
    const children = Array.from(block.childNodes);
    const after = children.slice(children.indexOf(holder) + 1);
    let out = "";
    let firstContribution = true;
    for (const node of after) {
      if (node.nodeType === 3) {
        let text = node.nodeValue;
        if (firstContribution) {
          // The one place a connective can legitimately be, and one only.
          text = String(text).replace(/^\s+/, "").replace(COMPANY_CONNECTOR_RE, "");
        }
        if (normalize.cleanText(text)) firstContribution = false;
        out += " " + text;
        continue;
      }
      if (node.nodeType !== 1) continue;
      if (isNotEmployerNode(node)) {
        // A node that cannot be the employer ENDS the employer: whatever follows
        // belongs to the thing that node introduced, not to the company.
        break;
      }
      if (normalize.cleanText(node.textContent)) firstContribution = false;
      out += " " + node.textContent;
    }
    return employerFromText(out);
  }

  /**
   * The visible company NAME, whether or not the company is a link.
   *
   * A company page URL is enrichment. The employer's name is what the row shows,
   * and Sales Navigator shows it as a company anchor, as a dedicated unlinked
   * node, as text beside the title on one subtitle line, or on a subtitle line
   * of its own. Only the first two had a strategy, and the ordered list ended at
   * `.artdeco-entity-lockup__subtitle a` — an ANCHOR — so an employer that
   * happened not to be linked read as no employer at all.
   *
   * Every strategy is scoped to this one row's container and reads only text
   * already on screen. A node that is structurally something else (location,
   * school, degree, industry, caption, metadata) is never read as the employer,
   * and text that is recognisable interface furniture is refused rather than
   * stored. When the evidence does not clearly name an employer the answer is
   * `null`: an absent company stays absent, and is never inferred from whatever
   * happened to be rendered next to it.
   *
   * @param {Element} container the row
   * @param {Element|null} titleEl the element the title was read from, if any
   */
  function extractCompanyName(container, titleEl) {
    // 1. A dedicated company node — linked or not. Unchanged behaviour.
    const direct = firstText(container, COMPANY_NAME_SELECTORS);
    if (direct.value) return direct;

    // The remaining strategies are anchored on the TITLE element: they read what
    // the same visible line shows around it. With no title element there is no
    // anchor, and no company is read.
    if (!titleEl) return { value: null, selector: null };
    const titleText = normalize.cleanText(titleEl.textContent);
    const blocks = subtitleBlocks(container);

    // 2. The subtitle line that HOLDS the title, after the title itself.
    for (const block of blocks) {
      if (block.el === titleEl || !block.el.contains(titleEl)) continue;
      const value = employerAfterTitle(block.el, titleEl);
      if (value) return { value, selector: block.selector + " (unlinked)" };
    }

    // 3. A SEPARATE subtitle line that is not the title's own — the unlinked
    //    twin of `.artdeco-entity-lockup__subtitle a`. Bounded the same way: a
    //    block that is or contains a location, school, degree, industry or
    //    caption is not an employer line and is skipped rather than read.
    for (const block of blocks) {
      if (block.el === titleEl || block.el.contains(titleEl) || titleEl.contains(block.el)) {
        continue;
      }
      if (isNotEmployerNode(block.el)) continue;
      const value = employerFromText(block.el.textContent);
      if (value && value !== titleText) {
        return { value, selector: block.selector + " (unlinked)" };
      }
    }

    return { value: null, selector: null };
  }

  // Extra visible company / caption metadata lines (kept raw, never parsed into
  // authoritative fields).
  const METADATA_SELECTORS = [
    '[data-anonymize="industry"]',
    ".artdeco-entity-lockup__metadata",
    '[class*="entity-lockup__metadata"]',
  ];

  /**
   * Extract one record from a container. Pushes warnings for missing/failed
   * fields. Never invents values.
   */
  function extractRecord(container, ctx, index) {
    const warnings = [];
    const selectorsUsed = {};

    const nameHit = firstText(container, NAME_SELECTORS);
    const rawFullName = nameHit.value;
    if (!rawFullName) {
      warnings.push({ code: WARNINGS.SELECTOR_FAILURE, field: "rawFullName" });
    } else {
      selectorsUsed.name = nameHit.selector;
    }

    const { firstName, lastName } = normalize.splitName(rawFullName);
    if (rawFullName && !lastName) {
      warnings.push({ code: WARNINGS.MISSING_FIELD, field: "lastName" });
    }

    const titleHit = firstText(container, TITLE_SELECTORS);
    if (!titleHit.value) warnings.push({ code: WARNINGS.MISSING_FIELD, field: "title" });
    else selectorsUsed.title = titleHit.selector;

    // The company NAME and the company URL are two independent facts. The name
    // is read from whatever the row visibly shows; the URL, below, is read only
    // if a company anchor happens to exist. Neither one gates the other, and
    // neither one gates the person.
    const companyHit = extractCompanyName(container, titleHit.el);
    if (!companyHit.value) warnings.push({ code: WARNINGS.MISSING_FIELD, field: "companyName" });
    else selectorsUsed.companyName = companyHit.selector;

    const locationHit = firstText(container, LOCATION_SELECTORS);
    if (!locationHit.value) warnings.push({ code: WARNINGS.MISSING_FIELD, field: "location" });
    else selectorsUsed.location = locationHit.selector;

    // Lead (Sales Navigator) URL.
    const leadHit = firstHref(container, LEAD_HREF_SELECTORS);
    const lead = resolveUrl(leadHit.value);
    if (leadHit.value && !lead.valid) {
      warnings.push({ code: WARNINGS.MALFORMED_URL, field: "salesNavLeadUrl", raw: leadHit.value });
    }
    if (!lead.url) warnings.push({ code: WARNINGS.MISSING_FIELD, field: "salesNavLeadUrl" });
    else selectorsUsed.leadUrl = leadHit.selector;

    // Public /in/ profile URL. Only a link actually visible on the page counts.
    //
    // DAT-018 A used to synthesise `/in/<member-id>` from the lead URL whenever
    // no link was visible. That URL does resolve — LinkedIn's /in/ route accepts
    // the opaque member id and redirects to the person. The problem is not that
    // it points nowhere; it is that identity here is matched by exact normalized
    // string, so the alias and the vanity handle are two different keys for one
    // person. Capture the same human from a results row and from their own
    // profile page and you get two identities that can never match (DAT-019 /
    // #195). Three committed contracts already said not to do it: identity is
    // never repaired from a lead URL, a missing profile URL stays honestly
    // uncertain, and the lead URL is never an identity key.
    //
    // So the canonical URL stays null and the member identifier is captured
    // under its own name. It is a real, stable identifier — it is simply not the
    // person's public handle, and only the handle belongs in the identity slot.
    // A clickable link can still be built from the member id for display; that
    // is a convenience, not an identity.
    const profileHit = firstHref(container, PUBLIC_PROFILE_SELECTORS);
    const profile = resolveUrl(profileHit.value);
    if (profileHit.value && !profile.valid) {
      warnings.push({ code: WARNINGS.MALFORMED_URL, field: "linkedinProfileUrl", raw: profileHit.value });
    }

    const linkedinProfileUrl = profile.url;
    const linkedinProfileUrlSource = profile.url ? "observed" : null;
    let linkedinMemberId = null;
    let linkedinAliasUrl = null;

    // Read the member id whenever a lead URL is present — including when a real
    // profile URL is also visible. A row showing both is the one place the two
    // identifier forms are observed together for one person, and that pair is
    // what lets the backend relate them later without inferring anything.
    if (lead.url) {
      const member = normalize.salesNavMemberId(lead.url);
      if (member.id) {
        linkedinMemberId = member.id;
        // DAT-020. LinkedIn's /in/ route accepts the member id and redirects to
        // the person, so this alias is a genuinely useful way to open a profile
        // whose handle is not known yet. It is kept in its OWN field: it is
        // navigation and evidence, never the published handle, and it must not
        // reach `linkedinProfileUrl`. Built from the verbatim id — folding the
        // case would break the very redirect it exists for.
        linkedinAliasUrl = "https://www.linkedin.com/in/" + member.id;
      } else {
        // A lead URL we cannot read an identifier out of. Refuse rather than
        // fabricate; the row keeps its lead URL as evidence.
        warnings.push({
          code: WARNINGS.MALFORMED_URL,
          field: "salesNavMemberId",
          reason: member.reason,
        });
      }
    }
    if (!linkedinProfileUrl) {
      // The published handle really is absent — but if a resolving alias was
      // built from the member id, the row still has a working way to open this
      // person, and the panel shows it. Reporting that as a missing field made
      // the review screen contradict the list beside it: an icon on one screen,
      // "missing: linkedinProfileUrl" on the next, for the same row.
      //
      // So the two cases are now distinguished. An alias present is provenance
      // — a note about where the link came from — not a fault. Nothing is
      // fabricated either way, and the alias still never becomes the canonical
      // handle.
      warnings.push(
        linkedinAliasUrl
          ? { code: WARNINGS.DERIVED_VALUE, field: "linkedinProfileUrl" }
          : { code: WARNINGS.MISSING_FIELD, field: "linkedinProfileUrl" }
      );
    }

    // Company URLs — capture the raw visible link. Classify by surface; do not
    // fabricate a public company URL from an id.
    const companyHref = firstHref(container, COMPANY_HREF_SELECTORS);
    const companyUrl = resolveUrl(companyHref.value);
    let salesNavCompanyUrl = null;
    let companyLinkedInUrl = null;
    if (companyHref.value && !companyUrl.valid) {
      warnings.push({ code: WARNINGS.MALFORMED_URL, field: "companyUrl", raw: companyHref.value });
    }
    if (companyUrl.url) {
      const kind = normalize.classifyLinkedInUrl(companyUrl.url);
      if (kind === "sales_company") salesNavCompanyUrl = companyUrl.url;
      else if (kind === "public_company") companyLinkedInUrl = companyUrl.url;
      else salesNavCompanyUrl = companyUrl.url; // keep raw, best-effort bucket
    }
    const publicCompanyHit = firstHref(container, PUBLIC_COMPANY_SELECTORS);
    if (!companyLinkedInUrl && publicCompanyHit.value) {
      const pc = resolveUrl(publicCompanyHit.value);
      if (pc.url) companyLinkedInUrl = pc.url;
    }

    // Raw visible company/caption metadata lines (unparsed, de-duplicated —
    // the selectors overlap on nested nodes).
    const metaSeen = new Set();
    const visibleCompanyMetadata = [];
    for (const sel of METADATA_SELECTORS) {
      container.querySelectorAll(sel).forEach((el) => {
        const t = normalize.cleanText(el.textContent);
        if (t && !metaSeen.has(t)) {
          metaSeen.add(t);
          visibleCompanyMetadata.push(t);
        }
      });
    }

    // Identity keys off the observed lead URL, not the derived profile URL: the
    // lead URL is what was actually on the page, and deriving does not make the
    // row any more identifiable than it already was.
    const stableKey = lead.url || profile.url || null;
    if (!stableKey) warnings.push({ code: WARNINGS.NO_STABLE_IDENTITY, field: "stableKey" });

    return {
      // identity / people
      firstName,
      lastName,
      rawFullName,
      title: titleHit.value,
      companyName: companyHit.value,
      location: locationHit.value,
      linkedinProfileUrl,
      linkedinProfileUrlSource,
      linkedinMemberId,
      linkedinAliasUrl,
      salesNavLeadUrl: lead.url,
      companyLinkedInUrl,
      salesNavCompanyUrl,
      visibleCompanyMetadata: visibleCompanyMetadata.length ? visibleCompanyMetadata : null,
      // provenance
      sourceSearchUrl: ctx.sourceSearchUrl || null,
      sourcePageNumber: ctx.sourcePageNumber != null ? ctx.sourcePageNumber : null,
      sourcePosition: index + 1,
      capturedAt: ctx.capturedAt,
      // internal / review aids
      _stableKey: stableKey,
      _selectorsUsed: selectorsUsed,
      warnings,
    };
  }

  function resolveUrl(href) {
    if (!href) return { url: null, valid: false };
    return normalize.normalizeLinkedInUrl(href);
  }

  // ---- Public entry point -------------------------------------------------

  /**
   * Extract all visible records from a results document.
   * @param {Document} doc
   * @param {{sourceSearchUrl?:string, capturedAt?:string}} options
   * @returns {{status, records, pageWarnings, sourcePageNumber, sourceSearchUrl, capturedAt, count}}
   */
  function extractPage(doc, options) {
    const opts = options || {};
    const sourceSearchUrl = opts.sourceSearchUrl || null;
    const capturedAt = opts.capturedAt || null;
    const sourcePageNumber = normalize.pageNumberFromUrl(sourceSearchUrl);

    const challenge = detectChallenge(doc, sourceSearchUrl);
    if (challenge.detected) {
      return {
        status: CAPTURE_STATUS.CHALLENGE_DETECTED,
        records: [],
        pageWarnings: [{ code: "challenge", reason: challenge.reason }],
        sourcePageNumber,
        sourceSearchUrl,
        capturedAt,
        count: 0,
      };
    }

    if (!isSupportedResultsUrl(sourceSearchUrl)) {
      const rejected = isRejectedSalesSurface(sourceSearchUrl);
      return {
        status: CAPTURE_STATUS.UNSUPPORTED_PAGE,
        records: [],
        pageWarnings: [
          {
            code: "unsupported_page",
            url: sourceSearchUrl,
            reason: rejected ? "rejected_sales_surface" : "not_people_search",
            message: rejected
              ? "Account/company Sales Navigator surfaces are not supported; only people/lead search results are captured."
              : "Not a supported Sales Navigator people/lead search results page.",
          },
        ],
        sourcePageNumber,
        sourceSearchUrl,
        capturedAt,
        count: 0,
      };
    }

    const containers = findResultContainers(doc);
    if (containers.length === 0) {
      // Distinguish a legitimately empty search from a broken/changed structure.
      if (detectNoResults(doc)) {
        return {
          status: CAPTURE_STATUS.EMPTY,
          records: [],
          pageWarnings: [{ code: "empty", message: "The search returned no results." }],
          sourcePageNumber,
          sourceSearchUrl,
          capturedAt,
          count: 0,
        };
      }
      // Looks like a results URL but no rows recognized: fail visibly. This is
      // NOT reported as a successful empty capture.
      return {
        status: CAPTURE_STATUS.STRUCTURE_UNRECOGNIZED,
        records: [],
        pageWarnings: [
          {
            code: "structure_unrecognized",
            message:
              "Sales Navigator results URL detected but no result rows could be parsed. " +
              "The page structure may have changed. Nothing was captured.",
          },
        ],
        sourcePageNumber,
        sourceSearchUrl,
        capturedAt,
        count: 0,
      };
    }

    const ctx = { sourceSearchUrl, sourcePageNumber, capturedAt };

    // Capturing a person and resolving their employer are separate concerns.
    //
    // DAT-018 B used to withhold any row whose Company Name could not be read,
    // on the reasoning that the downstream flow is company-first. In production
    // that gate cost a very large share of otherwise valid contacts, because an
    // employer shown as plain text — no company page to link to — read as no
    // employer at all. The eligibility rule and the extraction weakness
    // compounded: one produced the null, the other deleted the person for it.
    //
    // A company page URL is optional enrichment and a company name is
    // best-effort. Neither decides whether the Contact exists. What still
    // decides that is person identity, unchanged: a row with no name and no URL
    // carries `no_stable_identity` and is refused further down the path.
    //
    // Nothing here loosened the no-inference promise. An absent company is
    // still absent — reported as `missing_field: companyName`, never filled in
    // from a headline, a school, a location or a neighbouring row.
    const records = containers.map((c, i) => extractRecord(c, ctx, i));

    return {
      status: CAPTURE_STATUS.OK,
      records,
      pageWarnings: [],
      sourcePageNumber,
      sourceSearchUrl,
      capturedAt,
      count: records.length,
      visibleCount: records.length,
    };
  }

  return {
    isSupportedResultsUrl,
    isRejectedSalesSurface,
    detectChallenge,
    detectNoResults,
    findResultContainers,
    extractRecord,
    extractPage,
    // exported for tests
    extractCompanyName,
    _internals: { closestMatch, resolveUrl, employerFromText, employerAfterTitle },
  };
});

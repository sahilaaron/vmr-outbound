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
        if (t) return { value: t, selector: sel };
      }
    }
    return { value: null, selector: null };
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

    const companyHit = firstText(container, COMPANY_NAME_SELECTORS);
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

    // Public /in/ profile URL — only if visibly present. Never derived from the
    // opaque lead id (see notebook map §8.7).
    const profileHit = firstHref(container, PUBLIC_PROFILE_SELECTORS);
    const profile = resolveUrl(profileHit.value);
    if (profileHit.value && !profile.valid) {
      warnings.push({ code: WARNINGS.MALFORMED_URL, field: "linkedinProfileUrl", raw: profileHit.value });
    }
    if (!profile.url) {
      warnings.push({ code: WARNINGS.MISSING_FIELD, field: "linkedinProfileUrl" });
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

    const stableKey = profile.url || lead.url || null;
    if (!stableKey) warnings.push({ code: WARNINGS.NO_STABLE_IDENTITY, field: "stableKey" });

    return {
      // identity / people
      firstName,
      lastName,
      rawFullName,
      title: titleHit.value,
      companyName: companyHit.value,
      location: locationHit.value,
      linkedinProfileUrl: profile.url,
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
    const records = containers.map((c, i) => extractRecord(c, ctx, i));

    return {
      status: CAPTURE_STATUS.OK,
      records,
      pageWarnings: [],
      sourcePageNumber,
      sourceSearchUrl,
      capturedAt,
      count: records.length,
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
    _internals: { closestMatch, resolveUrl },
  };
});

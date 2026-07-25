/**
 * LinkedInCompanyProfileAdapter (DAT-012G): extraction for an operator-opened
 * LinkedIn company page (`/company/<id>` home or About).
 *
 * Behavioural reference: the legacy QA bot's `extract_company_info` (the About
 * page's definition list of Website / Industry / Company size / Headquarters /
 * Founded / Specialties, including the "associated members" adjacent-cell
 * quirk). Only the parsing behaviour is reused — the extension NEVER navigates
 * to a company page itself; the operator opens it manually, and there is no
 * automatic hop from a person profile.
 *
 * Same rules as the other adapters: ordered strategies, nulls + warnings, raw
 * lines preserved verbatim, unknown structure fails visibly, no fabrication.
 * Headquarters is captured as the DISPLAYED text only — it is never derived
 * from a person's location or a role location, and any city/state/country
 * splitting happens backend-side and only when deterministic.
 *
 * UMD module -> Node CommonJS + self.SNCapture.companyExtraction
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
  g.SNCapture = Object.assign(g.SNCapture || {}, { companyExtraction: mod });
})(typeof globalThis !== "undefined" ? globalThis : this, function (constants, normalize, surface) {
  "use strict";

  const { WARNINGS, CAPTURE_STATUS, SURFACES } = constants;
  const ADAPTER_VERSION = "linkedin-company-profile-adapter/1";

  function elementText(el) {
    if (!el) return "";
    if (typeof el.innerText === "string") return el.innerText;
    return el.textContent || "";
  }

  function cleanLines(el) {
    return String(elementText(el) || "")
      .split("\n")
      .map((l) => normalize.cleanText(l))
      .filter(Boolean);
  }

  // ---- URL identity --------------------------------------------------------

  function normalizedCompanyUrl(sourceUrl) {
    const norm = normalize.normalizeLinkedInUrl(sourceUrl);
    if (!norm.valid) return null;
    // Strip a trailing /about segment so home and About normalize identically.
    const url = norm.url.replace(/\/about$/, "");
    return /\/company\//.test(url) ? url : null;
  }

  function companyIdFromUrl(companyUrl) {
    if (!companyUrl) return null;
    const m = String(companyUrl).match(/\/company\/([^/?#]+)/);
    return m ? m[1] : null;
  }

  // ---- Field extraction ----------------------------------------------------

  // Label -> payload field. Labels are matched case-insensitively and exactly.
  const LABEL_FIELDS = {
    website: "website",
    industry: "industry",
    "company size": "size_range",
    headquarters: "headquarters_text",
    founded: "founded_raw",
    specialties: "specialties",
  };

  /**
   * Read the About definition list as (dt -> [dd...]) pairs by DOM adjacency
   * (robust against the legacy index-offset quirk: each dt owns every dd that
   * follows it until the next dt).
   */
  function readDefinitionList(dl) {
    const pairs = [];
    let currentLabel = null;
    let currentValues = [];
    for (const child of Array.from(dl.children || [])) {
      if (child.tagName === "DT") {
        if (currentLabel != null) pairs.push({ label: currentLabel, values: currentValues });
        currentLabel = normalize.cleanText(elementText(child));
        currentValues = [];
      } else if (child.tagName === "DD") {
        const v = normalize.cleanText(elementText(child));
        if (v) currentValues.push(v);
      }
    }
    if (currentLabel != null) pairs.push({ label: currentLabel, values: currentValues });
    return pairs;
  }

  function findAboutDl(doc) {
    const dls = Array.from(doc.querySelectorAll("dl"));
    for (const dl of dls) {
      const labels = readDefinitionList(dl).map((p) => (p.label || "").toLowerCase());
      if (labels.some((l) => l in LABEL_FIELDS)) return dl;
    }
    return null;
  }

  function parseEmployeeCount(raw) {
    if (!raw) return null;
    const m = String(raw).replace(/,/g, "").match(/(\d+)\s*(?:associated members|employees)/i);
    return m ? parseInt(m[1], 10) : null;
  }

  function parseFoundedYear(raw) {
    if (!raw) return null;
    const m = String(raw).match(/\b(1[0-9]{3}|20[0-9]{2})\b/);
    return m ? parseInt(m[1], 10) : null;
  }

  function findCompanyName(doc) {
    const h1 = doc.querySelector("main h1, h1");
    const name = h1 && normalize.cleanText(elementText(h1));
    return name || null;
  }

  /** Visible "X employees" text anywhere in the top card / summary area. */
  function findDisplayedEmployeeCount(doc) {
    const body = (doc.body && elementText(doc.body)) || "";
    const m = body.replace(/,/g, "").match(/(\d[\d.]*[KM]?)\+?\s+employees\b/i);
    if (!m) return { raw: null, count: null };
    const token = m[1];
    let count = null;
    if (/^\d+$/.test(token)) count = parseInt(token, 10);
    return { raw: `${m[1]} employees`, count };
  }

  // ---- Public entry point --------------------------------------------------

  function extractCompany(doc, options) {
    const opts = options || {};
    const sourceUrl = opts.sourceUrl || null;
    const capturedAt = opts.capturedAt || null;
    const pageWarnings = [];
    const missingSections = [];

    const detected = surface.detectSurface(sourceUrl, doc);
    if (detected.surface === SURFACES.CHALLENGE) {
      return failure(CAPTURE_STATUS.CHALLENGE_DETECTED, detected, sourceUrl, capturedAt, [
        { code: "challenge", reason: detected.reason },
      ]);
    }
    if (detected.surface === SURFACES.UNAVAILABLE) {
      return failure(CAPTURE_STATUS.UNAVAILABLE_PROFILE, detected, sourceUrl, capturedAt, [
        { code: "unavailable_company", reason: detected.reason },
      ]);
    }
    if (detected.surface !== SURFACES.COMPANY_PROFILE) {
      return failure(CAPTURE_STATUS.UNSUPPORTED_PAGE, detected, sourceUrl, capturedAt, [
        {
          code: "unsupported_page",
          url: sourceUrl,
          reason: detected.reason || "not_company_profile",
          message: "Only a LinkedIn company page (linkedin.com/company/…) can be captured here.",
        },
      ]);
    }

    const companyUrl = normalizedCompanyUrl(sourceUrl);
    const warnings = [];
    const name = findCompanyName(doc);
    if (!name) warnings.push({ code: WARNINGS.MISSING_FIELD, field: "name" });

    const company = {
      company_linkedin_url: companyUrl,
      company_linkedin_id: companyIdFromUrl(companyUrl),
      name,
      website: null,
      industry: null,
      size_range: null,
      employee_count_raw: null,
      employee_count: null,
      headquarters_text: null,
      founded_year: null,
      founded_raw: null,
      specialties: null,
      observed_at: capturedAt,
      raw_lines: [],
      warnings,
    };

    if (!companyUrl) {
      warnings.push({ code: WARNINGS.MALFORMED_URL, field: "company_linkedin_url", raw: sourceUrl });
    }

    const dl = findAboutDl(doc);
    if (!dl) {
      // Home page (or About not rendered): firmographics are simply absent —
      // reported, never guessed. The operator can open the About page instead.
      missingSections.push("about_details");
      pageWarnings.push({
        code: WARNINGS.MISSING_SECTION,
        section: "about_details",
        message:
          "No firmographic details list found on this page. Open the company's About page and capture again for website/industry/size/headquarters.",
      });
    } else {
      company.raw_lines = cleanLines(dl);
      for (const pair of readDefinitionList(dl)) {
        const key = LABEL_FIELDS[(pair.label || "").toLowerCase()];
        if (!key || !pair.values.length) continue;
        const value = pair.values[0];
        if (key === "size_range") {
          company.size_range = value;
          // Legacy quirk: the adjacent extra dd carries "N associated members".
          const members = pair.values.find((v) => /associated members/i.test(v));
          if (members) {
            company.employee_count_raw = members;
            company.employee_count = parseEmployeeCount(members);
          }
        } else if (key === "founded_raw") {
          company.founded_raw = value;
          company.founded_year = parseFoundedYear(value);
          if (company.founded_year == null) {
            warnings.push({ code: WARNINGS.UNPARSED_VALUE, field: "founded_year", raw: value });
          }
        } else if (key === "website") {
          company.website = value;
        } else {
          company[key] = value;
        }
      }
    }

    if (company.employee_count_raw == null) {
      const displayed = findDisplayedEmployeeCount(doc);
      if (displayed.raw) {
        company.employee_count_raw = displayed.raw;
        company.employee_count = displayed.count;
      }
    }

    for (const f of ["website", "industry", "size_range", "headquarters_text"]) {
      if (company[f] == null) warnings.push({ code: WARNINGS.MISSING_FIELD, field: f });
    }

    if (!name && !dl) {
      return failure(CAPTURE_STATUS.STRUCTURE_UNRECOGNIZED, detected, sourceUrl, capturedAt, [
        {
          code: "structure_unrecognized",
          message:
            "Company URL detected but neither a company name nor an About details list " +
            "could be parsed. Nothing was captured.",
        },
      ]);
    }

    const status =
      missingSections.length || warnings.length ? CAPTURE_STATUS.PARTIAL : CAPTURE_STATUS.OK;

    return {
      status,
      surface: detected.surface,
      company,
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
      company: null,
      missingSections: [],
      pageWarnings,
      sourceUrl,
      capturedAt,
      adapterVersion: ADAPTER_VERSION,
    };
  }

  return {
    ADAPTER_VERSION,
    extractCompany,
    _internals: {
      readDefinitionList,
      findAboutDl,
      parseEmployeeCount,
      parseFoundedYear,
      normalizedCompanyUrl,
      companyIdFromUrl,
    },
  };
});

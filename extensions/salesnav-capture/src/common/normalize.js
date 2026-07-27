/**
 * Pure normalization helpers. Client-side normalization is intentionally
 * MINIMAL: trim whitespace and normalize URLs only. The extension must NOT
 * implement authoritative normalization (name translation, ASCII folding,
 * domain derivation, email verification) — those belong to the VMR backend
 * (app/services/imports/normalization.py). Raw visible values are preserved
 * verbatim elsewhere; these helpers only produce convenience views.
 *
 * UMD module -> Node CommonJS + self.SNCapture.normalize
 */
(function (root, factory) {
  const mod = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = mod;
  const g = typeof self !== "undefined" ? self : root;
  g.SNCapture = Object.assign(g.SNCapture || {}, { normalize: mod });
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  /** Collapse internal whitespace and trim. Returns null for empty/nullish. */
  function cleanText(value) {
    if (value == null) return null;
    const s = String(value).replace(/\s+/g, " ").trim();
    return s.length ? s : null;
  }

  /**
   * Best-effort split of a raw full name into first / last for a convenience
   * view. The RAW full name is always preserved separately by the caller; this
   * split never mutates or translates it. Mirrors the notebook's "split on first
   * space" intent but keeps Unicode intact.
   */
  function splitName(rawFullName) {
    const clean = cleanText(rawFullName);
    if (!clean) return { firstName: null, lastName: null };
    const idx = clean.indexOf(" ");
    if (idx === -1) return { firstName: clean, lastName: null };
    return {
      firstName: clean.slice(0, idx).trim() || null,
      lastName: clean.slice(idx + 1).trim() || null,
    };
  }

  /**
   * Normalize a LinkedIn / Sales Navigator URL for use as a stable identity key
   * and for storage. Absolutizes protocol-relative and path-only hrefs against
   * www.linkedin.com, lower-cases the host, strips query string and fragment and
   * a single trailing slash. Returns { url, valid, reason }.
   *
   * Does NOT invent or "repair" a malformed URL — a value that cannot be parsed
   * to an http(s) linkedin.com URL is reported invalid, not silently fixed.
   */
  function normalizeLinkedInUrl(href) {
    const raw = cleanText(href);
    if (!raw) return { url: null, valid: false, reason: "empty" };

    let candidate = raw;
    if (candidate.startsWith("//")) candidate = "https:" + candidate;
    else if (candidate.startsWith("/")) candidate = "https://www.linkedin.com" + candidate;
    else if (!/^https?:\/\//i.test(candidate)) {
      // Bare host or garbage. Only accept if it clearly starts with a linkedin host.
      if (/^([a-z0-9-]+\.)*linkedin\.com\//i.test(candidate)) candidate = "https://" + candidate;
      else return { url: null, valid: false, reason: "unparseable" };
    }

    let parsed;
    try {
      parsed = new URL(candidate);
    } catch (_e) {
      return { url: null, valid: false, reason: "unparseable" };
    }

    if (!/^https?:$/.test(parsed.protocol)) {
      return { url: null, valid: false, reason: "bad_protocol" };
    }
    const host = parsed.hostname.toLowerCase();
    if (!/(^|\.)linkedin\.com$/.test(host)) {
      return { url: null, valid: false, reason: "non_linkedin_host" };
    }

    let path = parsed.pathname.replace(/\/{2,}/g, "/");
    // Sales Navigator lead/people URLs embed a volatile search-context suffix
    // after a comma in the id segment (e.g. /sales/lead/ABC123,NAME_SEARCH...).
    // That suffix changes between searches for the SAME lead, so drop it to get
    // a stable identity — mirrors the notebook's `profile_sn_url[:find(",")]`.
    if (/\/sales\/(lead|people)\//.test(path) && path.indexOf(",") !== -1) {
      path = path.slice(0, path.indexOf(","));
    }
    if (path.length > 1) path = path.replace(/\/$/, "");
    const normalized = "https://" + host + path;
    return { url: normalized, valid: true, reason: null };
  }

  /** Classify a normalized LinkedIn URL by surface. */
  function classifyLinkedInUrl(normalizedUrl) {
    if (!normalizedUrl) return "unknown";
    if (/\/sales\/lead\//.test(normalizedUrl)) return "sales_lead";
    if (/\/sales\/people\//.test(normalizedUrl)) return "sales_lead";
    if (/\/sales\/company\//.test(normalizedUrl)) return "sales_company";
    if (/\/in\//.test(normalizedUrl)) return "public_profile";
    if (/\/company\//.test(normalizedUrl)) return "public_company";
    return "unknown";
  }

  // A Sales Navigator member identifier as it appears in /sales/lead/<id>.
  // Deliberately conservative: an opaque token of URL-safe characters. Anything
  // outside this shape is refused rather than repaired, so a malformed lead URL
  // can never become a fabricated profile URL.
  const SALESNAV_MEMBER_ID = /^[A-Za-z0-9_-]{3,128}$/;

  /**
   * Read the member identifier out of a supported Sales Navigator lead URL.
   *
   * Supported shape (DAT-018): `/sales/lead/<member-id>`, with any query string,
   * fragment, volatile comma-suffix, or extra route material removed first.
   * `/sales/people/…` is NOT supported here — it is a different route whose
   * segment is not the member identifier, so deriving from it would be a guess.
   *
   * @returns {{id: string|null, reason: string|null}}
   */
  function salesNavMemberId(leadUrl) {
    const normalized = normalizeLinkedInUrl(leadUrl);
    if (!normalized.valid) return { id: null, reason: normalized.reason || "unparseable" };

    let pathname;
    try {
      pathname = new URL(normalized.url).pathname;
    } catch (_e) {
      return { id: null, reason: "unparseable" };
    }
    const match = /^\/sales\/lead\/([^/]+)/.exec(pathname);
    if (!match) return { id: null, reason: "not_a_lead_url" };

    // normalizeLinkedInUrl already drops the ",NAME_SEARCH,…" suffix; strip it
    // again defensively in case this is called with a pre-normalized value.
    const id = match[1].split(",")[0];
    if (!id) return { id: null, reason: "missing_identifier" };
    if (!SALESNAV_MEMBER_ID.test(id)) return { id: null, reason: "malformed_identifier" };
    return { id, reason: null };
  }

  /**
   * Derive the canonical public profile URL encoded in a Sales Navigator lead
   * URL: `/sales/lead/<member-id>` -> `https://www.linkedin.com/in/<member-id>`.
   *
   * This is a DERIVATION, not an observation. Callers must record which of the
   * two it was, and must prefer an actually visible `/in/` URL when one exists.
   * Returns `{url: null}` with a reason rather than fabricating anything.
   *
   * @returns {{url: string|null, memberId: string|null, reason: string|null}}
   */
  function profileUrlFromSalesNavLead(leadUrl) {
    const { id, reason } = salesNavMemberId(leadUrl);
    if (!id) return { url: null, memberId: null, reason };
    return { url: "https://www.linkedin.com/in/" + id, memberId: id, reason: null };
  }

  /** Extract the `page` query parameter from a search URL, if present. */
  function pageNumberFromUrl(searchUrl) {
    const raw = cleanText(searchUrl);
    if (!raw) return null;
    try {
      const u = new URL(raw);
      const p = u.searchParams.get("page");
      if (p == null) return null;
      const n = parseInt(p, 10);
      return Number.isFinite(n) && n > 0 ? n : null;
    } catch (_e) {
      return null;
    }
  }

  return {
    cleanText,
    splitName,
    normalizeLinkedInUrl,
    classifyLinkedInUrl,
    salesNavMemberId,
    profileUrlFromSalesNavLead,
    pageNumberFromUrl,
  };
});

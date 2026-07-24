/**
 * PageSurfaceDetector (DAT-012B): classify the page the operator already
 * opened into exactly one supported surface.
 *
 *   PageSurfaceDetector
 *   ├── SalesNavigatorPeopleResultsAdapter  (src/common/extraction.js)
 *   ├── LinkedInPersonProfileAdapter        (src/common/profile-extraction.js)
 *   └── LinkedInCompanyProfileAdapter       (PR for DAT-012G)
 *
 * Detection is URL-first, then conservative DOM checks (login walls,
 * checkpoints, unavailable profiles). It never navigates, never retries, and
 * never guesses: a page that matches nothing is UNSUPPORTED, and a page that
 * matches a supported route but shows a challenge/login wall is CHALLENGE.
 *
 * UMD module -> Node CommonJS + self.SNCapture.surface
 */
(function (root, factory) {
  const g = typeof self !== "undefined" ? self : root;
  const isNode = typeof module !== "undefined" && module.exports;
  const mod = factory(
    isNode ? require("./constants.js") : g.SNCapture.constants,
    isNode ? require("./extraction.js") : g.SNCapture.extraction
  );
  if (isNode) module.exports = mod;
  g.SNCapture = Object.assign(g.SNCapture || {}, { surface: mod });
})(typeof globalThis !== "undefined" ? globalThis : this, function (constants, extraction) {
  "use strict";

  const { SURFACES } = constants;

  function isLinkedInHost(hostname) {
    return /(^|\.)linkedin\.com$/.test(String(hostname || "").toLowerCase());
  }

  function parseUrl(url) {
    if (!url) return null;
    try {
      return new URL(url);
    } catch (_e) {
      return null;
    }
  }

  // ---- Person profile routes ----------------------------------------------
  //
  // Supported: the MAIN public profile page only.
  //   https://www.linkedin.com/in/<public-identifier>[/]
  // Rejected (reviewable detail overlays, sub-resources, anything else):
  //   /in/<id>/details/..., /in/<id>/recent-activity/..., /in/<id>/overlay/...
  // The public identifier may contain Unicode word characters, digits, hyphens
  // and percent-encoded bytes. An empty identifier is not a profile.
  const PERSON_PATH_RE = /^\/in\/([^/]+)\/?$/;

  function isSupportedPersonProfileUrl(url) {
    const u = parseUrl(url);
    if (!u || !isLinkedInHost(u.hostname)) return false;
    const m = u.pathname.match(PERSON_PATH_RE);
    return !!(m && m[1]);
  }

  /**
   * Derive the LinkedIn public identifier (the `/in/<slug>` segment) from a
   * profile URL when safely possible. Returns the decoded slug or null; never
   * invents one.
   */
  function publicIdentifierFromUrl(url) {
    const u = parseUrl(url);
    if (!u || !isLinkedInHost(u.hostname)) return null;
    const m = u.pathname.match(PERSON_PATH_RE);
    if (!m || !m[1]) return null;
    try {
      return decodeURIComponent(m[1]);
    } catch (_e) {
      return m[1];
    }
  }

  // ---- Company profile routes ---------------------------------------------
  //
  // Supported: the public company home page and its About page (the About page
  // carries the firmographic fields). School pages are intentionally NOT
  // supported in the first release.
  const COMPANY_PATH_RE = /^\/company\/([^/]+)(?:\/(?:about\/?)?)?$/;

  function isSupportedCompanyProfileUrl(url) {
    const u = parseUrl(url);
    if (!u || !isLinkedInHost(u.hostname)) return false;
    const m = u.pathname.match(COMPANY_PATH_RE);
    return !!(m && m[1] && m[1].toLowerCase() !== "unavailable");
  }

  // ---- Unavailable-profile detection --------------------------------------

  const UNAVAILABLE_SIGNALS = [
    /this profile is not available/i,
    /profile was not found/i,
    /this page doesn'?t exist/i,
    /page not found/i,
    /hmm, we can'?t find that page/i,
    /this company is not available/i,
    /check your url or return to linkedin home/i,
  ];

  function detectUnavailable(doc, url) {
    if (url && /\/company\/unavailable(\/|$)/i.test(url)) {
      return { detected: true, reason: "unavailable_url" };
    }
    const bodyText = (doc && doc.body && doc.body.textContent) || "";
    if (UNAVAILABLE_SIGNALS.some((re) => re.test(bodyText))) {
      return { detected: true, reason: "unavailable_text" };
    }
    return { detected: false, reason: null };
  }

  // ---- Login-wall detection ------------------------------------------------
  //
  // The generic challenge detector (extraction.detectChallenge) covers
  // checkpoint/captcha URLs and texts. A logged-out "authwall" render of a
  // profile also shows join/sign-in calls to action; treat that as CHALLENGE
  // too so the operator is told to log in rather than shown an empty capture.

  function detectLoginWall(doc) {
    if (!doc || typeof doc.querySelector !== "function") return { detected: false, reason: null };
    if (
      doc.querySelector(
        'form[action*="login"], form.join-form, [data-test-id="guest-homepage"], .authwall-join-form'
      )
    ) {
      return { detected: true, reason: "login_form" };
    }
    const bodyText = (doc.body && doc.body.textContent) || "";
    if (/sign in to view|join linkedin to view|sign in to continue/i.test(bodyText)) {
      return { detected: true, reason: "login_text" };
    }
    return { detected: false, reason: null };
  }

  // ---- Classifier ----------------------------------------------------------

  /**
   * Classify (url, doc) into a surface.
   * @returns {{surface: string, reason: string|null, publicIdentifier: string|null}}
   */
  function detectSurface(url, doc) {
    const challenge = extraction.detectChallenge(doc, url);
    if (challenge.detected) {
      return { surface: SURFACES.CHALLENGE, reason: challenge.reason, publicIdentifier: null };
    }

    const u = parseUrl(url);
    if (!u || !isLinkedInHost(u.hostname)) {
      return { surface: SURFACES.UNSUPPORTED, reason: "not_linkedin", publicIdentifier: null };
    }

    if (extraction.isSupportedResultsUrl(url)) {
      return { surface: SURFACES.SALESNAV_PEOPLE_RESULTS, reason: null, publicIdentifier: null };
    }
    if (extraction.isRejectedSalesSurface(url) || /^\/sales(\/|$)/.test(u.pathname)) {
      return {
        surface: SURFACES.UNSUPPORTED,
        reason: "unsupported_sales_surface",
        publicIdentifier: null,
      };
    }

    const unavailable = detectUnavailable(doc, url);
    const login = detectLoginWall(doc);

    if (isSupportedPersonProfileUrl(url)) {
      if (unavailable.detected) {
        return { surface: SURFACES.UNAVAILABLE, reason: unavailable.reason, publicIdentifier: null };
      }
      if (login.detected) {
        return { surface: SURFACES.CHALLENGE, reason: login.reason, publicIdentifier: null };
      }
      return {
        surface: SURFACES.PERSON_PROFILE,
        reason: null,
        publicIdentifier: publicIdentifierFromUrl(url),
      };
    }

    if (isSupportedCompanyProfileUrl(url)) {
      if (unavailable.detected) {
        return { surface: SURFACES.UNAVAILABLE, reason: unavailable.reason, publicIdentifier: null };
      }
      if (login.detected) {
        return { surface: SURFACES.CHALLENGE, reason: login.reason, publicIdentifier: null };
      }
      return { surface: SURFACES.COMPANY_PROFILE, reason: null, publicIdentifier: null };
    }

    if (/^\/in\//.test(u.pathname)) {
      // A profile sub-route (details overlay, activity feed, …) — explicitly
      // unsupported so the operator opens the main profile page instead.
      return { surface: SURFACES.UNSUPPORTED, reason: "profile_subroute", publicIdentifier: null };
    }
    if (unavailable.detected) {
      return { surface: SURFACES.UNAVAILABLE, reason: unavailable.reason, publicIdentifier: null };
    }

    return { surface: SURFACES.UNSUPPORTED, reason: "unrecognized_route", publicIdentifier: null };
  }

  return {
    detectSurface,
    isSupportedPersonProfileUrl,
    isSupportedCompanyProfileUrl,
    publicIdentifierFromUrl,
    detectUnavailable,
    detectLoginWall,
  };
});

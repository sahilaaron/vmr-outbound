/**
 * Content script: runs inside the operator's authenticated Sales Navigator tab.
 *
 * It ONLY reads the DOM the operator is already viewing. It does not automate
 * navigation, click through warnings, mimic human timing, or touch cookies /
 * tokens. Pagination stays operator-driven: this script captures the *current*
 * page on request; the operator advances pages themselves.
 *
 * Messages handled:
 *   CS_DETECT  -> report page support / challenge / page number / visible count
 *   CS_CAPTURE -> materialize lazy rows (bounded), extract, return a page result
 */
(function () {
  "use strict";
  const NS = self.SNCapture;
  if (!NS || !NS.extraction) {
    // Shared modules failed to load; fail visibly rather than silently.
    // (Should not happen: manifest loads them before this file.)
    // eslint-disable-next-line no-console
    console.warn("[salesnav-capture] shared modules missing");
    return;
  }
  const { extraction, constants } = NS;

  function nowIso() {
    return new Date().toISOString();
  }

  /** Best-effort discovery of the scrollable results container. */
  function findScrollContainer() {
    const candidates = [
      "#search-results-container",
      ".search-results-container",
      ".artdeco-list",
      'ol.artdeco-list',
      "main",
    ];
    for (const sel of candidates) {
      const el = document.querySelector(sel);
      if (el && el.scrollHeight > el.clientHeight + 40) return el;
    }
    return null;
  }

  /**
   * Scroll the results container in steps to force lazy rows to render, bounded
   * by a time budget and stabilization of the row count. Returns a promise.
   * This is a single, discrete, operator-initiated pass — not a background loop.
   */
  async function materializeRows() {
    const budgetMs = constants.LIMITS.CAPTURE_SCROLL_BUDGET_MS;
    const container = findScrollContainer();
    const scroller = container || document.scrollingElement || document.documentElement;
    const start = Date.now();
    let lastCount = -1;
    let stable = 0;

    while (Date.now() - start < budgetMs && stable < 3) {
      const count = document.querySelectorAll('[data-anonymize="person-name"]').length;
      if (count === lastCount) stable += 1;
      else stable = 0;
      lastCount = count;

      const step = Math.max(400, Math.floor((scroller.clientHeight || 600) * 0.9));
      scroller.scrollBy ? scroller.scrollBy(0, step) : window.scrollBy(0, step);
      await sleep(250);
    }
    // Return to top so the operator's view is not left scrolled away.
    if (scroller.scrollTo) scroller.scrollTo(0, 0);
    else window.scrollTo(0, 0);
    await sleep(100);
  }

  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  function detect() {
    const url = location.href;
    const challenge = extraction.detectChallenge(document, url);
    const supported = extraction.isSupportedResultsUrl(url);
    const visibleCount = supported
      ? document.querySelectorAll('[data-anonymize="person-name"]').length
      : 0;
    return {
      url,
      supported,
      challengeDetected: challenge.detected,
      challengeReason: challenge.reason,
      visibleCount,
    };
  }

  async function capture() {
    // Re-check challenge before doing anything.
    const pre = extraction.detectChallenge(document, location.href);
    if (pre.detected) {
      return extraction.extractPage(document, {
        sourceSearchUrl: location.href,
        capturedAt: nowIso(),
      });
    }
    await materializeRows();
    return extraction.extractPage(document, {
      sourceSearchUrl: location.href,
      capturedAt: nowIso(),
    });
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || !msg.type) return;
    if (msg.type === "CS_DETECT") {
      sendResponse(detect());
      return; // sync
    }
    if (msg.type === "CS_CAPTURE") {
      capture().then(sendResponse).catch((e) =>
        sendResponse({
          status: constants.CAPTURE_STATUS.STRUCTURE_UNRECOGNIZED,
          records: [],
          pageWarnings: [{ code: "capture_exception", message: String(e && e.message) }],
          sourceSearchUrl: location.href,
          capturedAt: nowIso(),
          count: 0,
        })
      );
      return true; // async response
    }
  });
})();

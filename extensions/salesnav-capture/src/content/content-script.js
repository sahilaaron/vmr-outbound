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
  if (!NS || !NS.extraction || !NS.scroller) {
    // Shared modules failed to load; fail visibly rather than silently.
    // (Should not happen: manifest loads them before this file.)
    // eslint-disable-next-line no-console
    console.warn("[salesnav-capture] shared modules missing");
    return;
  }
  const { extraction, constants, scroller } = NS;

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
   * One operator-initiated incremental pass over the results already on screen.
   * Smooth, bounded and cancellable; never paginates, navigates, or runs on its
   * own. See src/common/scroller.js for the stop conditions and the note on why
   * a small bounded jitter exists (DOM/layout timing, not detection avoidance).
   */
  let cancelRequested = false;

  function requestScrollCancel() {
    cancelRequested = true;
  }

  async function materializeRows(onProgress) {
    cancelRequested = false;
    const container = findScrollContainer();
    const target = container || document.scrollingElement || document.documentElement;
    return scroller.runScrollPass({
      scroller: {
        get scrollTop() {
          return target.scrollTop;
        },
        get clientHeight() {
          return target.clientHeight || window.innerHeight || 0;
        },
        get scrollHeight() {
          return target.scrollHeight || 0;
        },
        scrollTo(opt) {
          if (typeof target.scrollTo === "function") target.scrollTo(opt);
          else target.scrollTop = opt.top;
        },
      },
      countRows: () => document.querySelectorAll('[data-anonymize="person-name"]').length,
      sleep,
      now: () => Date.now(),
      random: () => Math.random(),
      isCancelled: () => cancelRequested,
      onProgress: onProgress || (() => {}),
    });
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
    const scrollResult = await materializeRows((progress) => {
      // Progress is advisory: the panel may not be listening, and a delivery
      // failure must never abort the pass the operator started.
      try {
        chrome.runtime.sendMessage({ type: "CS_SCROLL_PROGRESS", progress });
      } catch (_e) {
        /* panel closed */
      }
    });
    const page = extraction.extractPage(document, {
      sourceSearchUrl: location.href,
      capturedAt: nowIso(),
    });
    page.scroll = {
      stopReason: scrollResult.stopReason,
      steps: scrollResult.steps,
      rows: scrollResult.rows,
      startRows: scrollResult.startRows,
      elapsedMs: scrollResult.elapsedMs,
    };
    return page;
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || !msg.type) return;
    if (msg.type === "CS_DETECT") {
      sendResponse(detect());
      return; // sync
    }
    if (msg.type === "CS_CANCEL_SCROLL") {
      requestScrollCancel();
      sendResponse({ ok: true });
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

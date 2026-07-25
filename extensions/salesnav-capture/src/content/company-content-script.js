/**
 * Content script for operator-opened LinkedIn COMPANY pages (/company/…).
 *
 * Reads only the page the operator already opened, only on an explicit
 * side-panel action. No navigation (the extension never hops here from a
 * person profile), no scrolling automation, no storage/cookie access.
 *
 * Messages handled:
 *   CO_DETECT  -> classify the current page surface
 *   CO_CAPTURE -> extract the current company page and return the result
 */
(function () {
  "use strict";
  const NS = self.SNCapture;
  if (!NS || !NS.companyExtraction || !NS.surface) {
    // eslint-disable-next-line no-console
    console.warn("[salesnav-capture] company modules missing");
    return;
  }
  const { companyExtraction, surface, constants } = NS;

  function nowIso() {
    return new Date().toISOString();
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || !msg.type) return;
    if (msg.type === "CO_DETECT") {
      const detected = surface.detectSurface(location.href, document);
      sendResponse({ url: location.href, surface: detected.surface, reason: detected.reason });
      return; // sync
    }
    if (msg.type === "CO_CAPTURE") {
      try {
        sendResponse(
          companyExtraction.extractCompany(document, {
            sourceUrl: location.href,
            capturedAt: nowIso(),
          })
        );
      } catch (e) {
        sendResponse({
          status: constants.CAPTURE_STATUS.STRUCTURE_UNRECOGNIZED,
          surface: constants.SURFACES.COMPANY_PROFILE,
          company: null,
          missingSections: [],
          pageWarnings: [{ code: "capture_exception", message: String(e && e.message) }],
          sourceUrl: location.href,
          capturedAt: nowIso(),
        });
      }
      return; // sync
    }
  });
})();

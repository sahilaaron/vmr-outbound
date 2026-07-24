/**
 * Content script for operator-opened MAIN LinkedIn profile pages (/in/…).
 *
 * It ONLY reads the DOM the operator is already viewing, and only when the
 * operator explicitly clicks Capture in the side panel. It performs no
 * navigation, no scrolling automation, no timing tricks, and never touches
 * cookies, tokens, or browser storage. If the Experience section has not
 * rendered yet, the capture reports it missing and the operator scrolls the
 * page themselves and captures again.
 *
 * Messages handled:
 *   PS_DETECT  -> classify the current page surface (person/company/challenge/…)
 *   PS_CAPTURE -> extract the current profile page and return the result
 */
(function () {
  "use strict";
  const NS = self.SNCapture;
  if (!NS || !NS.profileExtraction || !NS.surface) {
    // eslint-disable-next-line no-console
    console.warn("[salesnav-capture] profile modules missing");
    return;
  }
  const { profileExtraction, surface, constants } = NS;

  function nowIso() {
    return new Date().toISOString();
  }

  function detect() {
    const url = location.href;
    const detected = surface.detectSurface(url, document);
    let expEntryCount = null;
    if (detected.surface === constants.SURFACES.PERSON_PROFILE) {
      const section = profileExtraction._internals.findExperienceSection(document);
      expEntryCount = section
        ? profileExtraction._internals.findEntryElements(section).length
        : 0;
    }
    return {
      url,
      surface: detected.surface,
      reason: detected.reason,
      publicIdentifier: detected.publicIdentifier,
      experienceEntryCount: expEntryCount,
    };
  }

  function capture() {
    return profileExtraction.extractProfile(document, {
      sourceUrl: location.href,
      capturedAt: nowIso(),
    });
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || !msg.type) return;
    if (msg.type === "PS_DETECT") {
      sendResponse(detect());
      return; // sync
    }
    if (msg.type === "PS_CAPTURE") {
      try {
        sendResponse(capture());
      } catch (e) {
        sendResponse({
          status: constants.CAPTURE_STATUS.STRUCTURE_UNRECOGNIZED,
          surface: constants.SURFACES.PERSON_PROFILE,
          profile: null,
          experiences: [],
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

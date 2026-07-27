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

  // ---- UI-011: tell the panel when this page changed under it --------------
  //
  // Signals only. The content script never pushes an extraction: it says "this
  // page moved" or "this page grew", and the panel decides whether to read. A
  // chatty page therefore cannot drive extraction, and nothing here scrolls,
  // paginates or navigates — it observes what the operator's own browsing did.

  const NAVIGATED = "PS_PAGE_NAVIGATED";
  const CHANGED = "PS_DOM_CHANGED";

  function notify(type) {
    try {
      chrome.runtime.sendMessage({ type, url: location.href });
    } catch (e) {
      // The panel may be closed; a signal with no listener is not an error.
    }
  }

  // LinkedIn is a single-page app: most profile-to-profile moves never reload.
  // Patching the history methods is the only way to see them from in here.
  let lastHref = location.href;
  function onLocationMaybeChanged() {
    if (location.href === lastHref) return;
    lastHref = location.href;
    notify(NAVIGATED);
  }
  for (const method of ["pushState", "replaceState"]) {
    const original = history[method];
    if (typeof original !== "function") continue;
    history[method] = function patched() {
      const result = original.apply(this, arguments);
      onLocationMaybeChanged();
      return result;
    };
  }
  window.addEventListener("popstate", onLocationMaybeChanged);

  // Sections such as Experience render only once the operator scrolls to them.
  // The observer reports that growth; it does not cause it.
  let mutationTimer = null;
  const observer = new MutationObserver(() => {
    onLocationMaybeChanged();
    if (mutationTimer != null) return; // one signal per burst; the panel debounces too
    mutationTimer = setTimeout(() => {
      mutationTimer = null;
      notify(CHANGED);
    }, 250);
  });
  if (document.body) {
    observer.observe(document.body, { childList: true, subtree: true });
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

/**
 * Live profile synchronisation for the side panel (UI-011).
 *
 * The panel used to require two clicks for something it already knew: press
 * Refresh to learn which page you were on, press Read to see what was on it.
 * Worse, the Mode card kept the *previous* profile's URL while the review below
 * it showed the current one — a capture tool whose provenance disagreed with its
 * payload.
 *
 * This module owns the "which page are we on, and what does it say" state
 * machine. It is deliberately transport-free: it receives a `chrome`-shaped
 * object, a `detect` function and a `preview` function, and calls `onState` with
 * what the panel should render. That is what makes the whole thing testable
 * against a fake browser instead of only in a real one.
 *
 * PREVIEW IS NOT PERSISTENCE
 * --------------------------
 * Everything here reads. Nothing it can reach writes: it never posts to the
 * backend, never creates a contact, never promotes, never touches a campaign.
 * Saving stays an explicit operator action on a button this module does not
 * own. `createLiveSync` is given exactly two capabilities — detect and preview —
 * so "automatic activity performs zero backend writes" is a property of the
 * wiring, not a promise in a comment.
 *
 * WHAT IT DOES NOT DO
 * -------------------
 * It never scrolls, never paginates, never navigates, and has no timing
 * designed to resemble a human. It reacts to pages the operator opened and to
 * content their own scrolling caused to load. A reread is triggered *by* the
 * page changing, not by us changing the page.
 *
 * STALENESS
 * ---------
 * Every sync takes a generation number and the page key it was started for. An
 * async result is applied only if both still match when it returns. A slow
 * parse of profile A that lands after the operator has moved to profile B is
 * dropped, not rendered — which is the same defect class as the stale Mode card,
 * just arriving from the other direction.
 *
 * UMD module -> Node CommonJS + self.SNCapture.liveSync
 */
(function (root, factory) {
  const g = typeof self !== "undefined" ? self : root;
  const isNode = typeof module !== "undefined" && module.exports;
  const mod = factory(isNode ? require("./constants.js") : g.SNCapture.constants);
  if (isNode) module.exports = mod;
  g.SNCapture = Object.assign(g.SNCapture || {}, { liveSync: mod });
})(typeof globalThis !== "undefined" ? globalThis : this, function (constants) {
  "use strict";

  const { SURFACES } = constants;

  /** Operator-facing phases. Each is a truthful statement about right now. */
  const PHASES = {
    /** No supported page is in the active tab. */
    WAITING: "waiting_for_supported_profile",
    /** A supported profile is open; nothing has been read yet. */
    DETECTED: "profile_detected",
    /** A read is in flight. */
    LOADING: "loading_profile_content",
    /** A preview is rendered and complete. */
    READY: "preview_ready",
    /** A reread picked up content that was not there before. */
    UPDATED: "additional_content_loaded",
    /** The page is a LinkedIn surface this workflow does not handle. */
    UNSUPPORTED: "unsupported_surface",
    /** A preview is rendered but the extraction reported warnings. */
    WARNINGS: "completed_with_warnings",
  };

  /** Surfaces this controller will read. Anything else is never parsed. */
  const PREVIEWABLE = new Set([SURFACES.PERSON_PROFILE]);

  const DEFAULT_DEBOUNCE_MS = 400;

  /**
   * Reduce a URL to the identity of the page.
   *
   * Query strings and fragments change without the profile changing (LinkedIn
   * appends tracking parameters on in-app navigation), so they are dropped. Two
   * URLs with the same key are the same person; a different key is a different
   * page and invalidates anything in flight.
   */
  function pageKeyOf(url) {
    if (!url) return null;
    const raw = String(url).trim();
    if (!raw) return null;
    const withoutFragment = raw.split("#")[0];
    const withoutQuery = withoutFragment.split("?")[0];
    return withoutQuery.replace(/\/+$/, "").toLowerCase() || null;
  }

  /** Whether an extraction result carries anything the operator should see. */
  function hasWarnings(draft) {
    if (!draft) return false;
    if (Array.isArray(draft.missingSections) && draft.missingSections.length) return true;
    if (Array.isArray(draft.pageWarnings) && draft.pageWarnings.length) return true;
    const profileWarnings = draft.profile && draft.profile.warnings;
    return Array.isArray(profileWarnings) && profileWarnings.length > 0;
  }

  /**
   * Whether a draft is complete enough that rereading it cannot add anything.
   *
   * This is what bounds rereading. A LinkedIn profile mutates in bursts rather
   * than continuously: measured on a live profile, ~50 mutation batches in the
   * first ten seconds after arrival and ~34 across ten seconds of operator
   * scrolling, then none at all while the page sat idle. So the failure mode is
   * not an endless loop but amplification — ungated, one scroll could cost a
   * dozen full re-parses, and each re-parse also costs a contact-lookup request.
   * Once every supported section has been read, further mutations have nothing
   * to offer and are ignored until the page actually changes.
   */
  function isComplete(draft) {
    if (!draft) return false;
    if (Array.isArray(draft.missingSections) && draft.missingSections.length) return false;
    return draft.status === "ok";
  }

  /**
   * How much a draft actually contains, used to tell "a reread found more" from
   * "a reread found the same thing". Only counts sections that arrive late.
   */
  function contentSignature(draft) {
    if (!draft) return "none";
    const experiences = Array.isArray(draft.experiences) ? draft.experiences.length : 0;
    const missing = Array.isArray(draft.missingSections) ? draft.missingSections.slice().sort() : [];
    return `${experiences}|${draft.status || ""}|${missing.join(",")}`;
  }

  /**
   * Create the controller.
   *
   * @param {object} options
   * @param {object} options.chrome        chrome-shaped API (tabs, runtime)
   * @param {function} options.detect      () => Promise<{surface, url}>
   * @param {function} options.preview     () => Promise<draftView|null>
   * @param {function} options.onState     (state) => void
   * @param {number}  [options.debounceMs]
   * @param {function}[options.setTimeoutFn]
   * @param {function}[options.clearTimeoutFn]
   */
  function createLiveSync(options) {
    const opts = options || {};
    const browser = opts.chrome;
    const detect = opts.detect;
    const preview = opts.preview;
    const emit = opts.onState || function () {};
    const debounceMs = opts.debounceMs == null ? DEFAULT_DEBOUNCE_MS : opts.debounceMs;
    const setTimer = opts.setTimeoutFn || ((fn, ms) => setTimeout(fn, ms));
    const clearTimer = opts.clearTimeoutFn || ((id) => clearTimeout(id));
    const nowMs = opts.nowFn || (() => Date.now());
    //: Floor between mutation-driven rereads. Navigation ignores this.
    const minRereadMs = opts.minRereadMs == null ? 1500 : opts.minRereadMs;

    // `generation` increments on every *page* change. `syncSeq` increments on
    // every sync attempt. A result must match both to be applied: generation
    // catches "the operator moved on", syncSeq catches "a newer read of the
    // same page already landed".
    let generation = 0;
    let syncSeq = 0;
    let started = false;
    let debounceTimer = null;
    let disposers = [];

    const state = {
      phase: PHASES.WAITING,
      surface: null,
      url: null,
      pageKey: null,
      draft: null,
      warnings: false,
      lastSignature: "none",
      previews: 0,
      // Previews of the CURRENT page only. "Additional content loaded" must mean
      // this page gained something on a reread — not that we arrived somewhere
      // new, which is every field being different by definition.
      pagePreviews: 0,
      lastReadAt: null,
      writes: 0, // stays 0 forever; asserted in tests as a property of the design
    };

    function publish(patch) {
      Object.assign(state, patch || {});
      emit(Object.assign({}, state));
    }

    /**
     * Run one detect + preview cycle.
     *
     * `reason` is carried through only for observability. `force` is used by the
     * manual Read button, which must work even when the controller believes the
     * page is unchanged.
     */
    async function sync(reason, force) {
      const mySeq = ++syncSeq;

      let detected = null;
      try {
        detected = await detect();
      } catch (err) {
        // A detect failure is not a reason to keep showing another page's data.
        if (mySeq !== syncSeq) return;
        publish({ phase: PHASES.WAITING, surface: null, url: null, pageKey: null, draft: null });
        return;
      }
      if (mySeq !== syncSeq) return; // a newer sync started while we awaited

      const surface = (detected && detected.surface) || SURFACES.UNSUPPORTED;
      const url = (detected && detected.url) || null;
      const key = pageKeyOf(url);

      // A different page invalidates everything in flight, including this cycle
      // if it is superseded later.
      if (key !== state.pageKey) {
        generation += 1;
        publish({
          surface,
          url,
          pageKey: key,
          draft: null,
          warnings: false,
          lastSignature: "none",
          pagePreviews: 0,
          // The reread floor is per page. Carrying the previous page's read time
          // forward would mute this page's first lazy-load burst for no reason.
          lastReadAt: null,
          phase: PREVIEWABLE.has(surface) ? PHASES.DETECTED : PHASES.WAITING,
        });
      } else {
        publish({ surface, url });
      }

      // Provenance first, always: the source card now names the page we are
      // about to parse, so the two can never disagree.
      if (!PREVIEWABLE.has(surface)) {
        publish({
          phase: surface === SURFACES.UNSUPPORTED ? PHASES.WAITING : PHASES.UNSUPPORTED,
          draft: null,
        });
        return;
      }

      const myGeneration = generation;
      const myKey = key;
      publish({ phase: PHASES.LOADING });

      let draft = null;
      try {
        draft = await preview();
      } catch (err) {
        if (mySeq !== syncSeq || myGeneration !== generation) return;
        publish({ phase: PHASES.DETECTED, draft: null });
        return;
      }

      // The stale-result guard. Three ways a result can be too old to use.
      if (mySeq !== syncSeq) return;
      if (myGeneration !== generation) return;
      if (myKey !== state.pageKey) return;

      const signature = contentSignature(draft);
      // "Additional content loaded" is a claim about THIS page gaining
      // something between two reads. Arriving on a new profile is not that —
      // there, every field differs by definition.
      const grew = state.pagePreviews > 0 && signature !== state.lastSignature;
      const warned = hasWarnings(draft);

      publish({
        draft,
        warnings: warned,
        lastSignature: signature,
        previews: state.previews + 1,
        pagePreviews: state.pagePreviews + 1,
        lastReadAt: nowMs(),
        phase: warned ? PHASES.WARNINGS : grew ? PHASES.UPDATED : PHASES.READY,
      });
      void force;
      void reason;
    }

    /**
     * Coalesce bursts of events into one read.
     *
     * Mutation-driven rereads carry two extra brakes that navigation-driven
     * ones do not need: they stop entirely once the page has been read
     * completely, and they never run more often than `minRereadMs`. Navigation
     * is always honoured — a new page is new information by definition.
     */
    function scheduleSync(reason) {
      if (reason === "dom_mutation") {
        if (isComplete(state.draft)) return; // nothing left to gain from this page
        if (state.lastReadAt != null && nowMs() - state.lastReadAt < minRereadMs) return;
      }
      if (debounceTimer != null) clearTimer(debounceTimer);
      debounceTimer = setTimer(() => {
        debounceTimer = null;
        void sync(reason, false);
      }, debounceMs);
    }

    /** A page change: invalidate anything in flight before the debounce runs. */
    function invalidate() {
      generation += 1;
    }

    function on(target, event, handler) {
      if (!target || !target[event] || typeof target[event].addListener !== "function") return;
      target[event].addListener(handler);
      disposers.push(() => {
        if (typeof target[event].removeListener === "function") {
          target[event].removeListener(handler);
        }
      });
    }

    function start() {
      // Idempotent: opening the panel twice, or a re-init, must not register a
      // second copy of every listener and read the page twice per event.
      if (started) {
        void sync("restart", true);
        return;
      }
      started = true;

      const tabs = browser && browser.tabs;
      const runtime = browser && browser.runtime;

      // Active-tab change.
      on(tabs, "onActivated", () => {
        invalidate();
        scheduleSync("tab_activated");
      });

      // URL change, reload, and SPA history navigation all surface here.
      // `changeInfo.url` is populated for tabs the extension has host
      // permission for, which is exactly the LinkedIn pages it may read — so
      // this needs no additional `tabs` permission.
      on(tabs, "onUpdated", (_tabId, changeInfo) => {
        const info = changeInfo || {};
        if (info.url) {
          invalidate();
          scheduleSync("url_changed");
          return;
        }
        if (info.status === "complete") scheduleSync("load_complete");
      });

      on(tabs, "onRemoved", () => {
        invalidate();
        scheduleSync("tab_removed");
      });

      // The content script reports DOM mutations and history navigation it can
      // see from inside the page. It sends a signal, never a payload: the panel
      // decides whether to read, so a chatty page cannot drive extraction.
      on(runtime, "onMessage", (msg) => {
        if (!msg || typeof msg !== "object") return;
        if (msg.type === "PS_PAGE_NAVIGATED") {
          invalidate();
          scheduleSync("spa_navigation");
          return;
        }
        if (msg.type === "PS_DOM_CHANGED") scheduleSync("dom_mutation");
      });

      // Opening the panel is itself a reason to look.
      void sync("panel_opened", true);
    }

    function stop() {
      if (debounceTimer != null) clearTimer(debounceTimer);
      debounceTimer = null;
      disposers.forEach((dispose) => dispose());
      disposers = [];
      started = false;
    }

    return {
      PHASES,
      start,
      stop,
      /** The manual Read button. Retained as a retry/debug control. */
      refresh: () => sync("manual", true),
      /** Test/observability hooks. */
      get state() {
        return Object.assign({}, state);
      },
      get started() {
        return started;
      },
      get listenerCount() {
        return disposers.length;
      },
      _pageKeyOf: pageKeyOf,
    };
  }

  return { createLiveSync, PHASES, PREVIEWABLE, pageKeyOf, hasWarnings, contentSignature, isComplete };
});

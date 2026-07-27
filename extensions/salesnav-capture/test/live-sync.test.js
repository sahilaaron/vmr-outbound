"use strict";
/**
 * UI-011 — the panel must follow the tab, and must never save while doing it.
 *
 * Two properties carry most of the weight here and both are about *not* doing
 * something: an automatic preview never writes anything to a backend, and a
 * slow result from the page the operator has left never overwrites the page
 * they are on. The rest is event plumbing, which is worth testing mainly
 * because plumbing is where duplicate listeners and lost updates hide.
 *
 * The browser is faked rather than driven. That is deliberate: a real Chrome
 * cannot be made to deliver a stale async result at the exact moment needed to
 * prove the guard works.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");

const { createLiveSync, PHASES, pageKeyOf } = require("../src/common/live-sync.js");
const { SURFACES } = require("../src/common/constants.js");

const PROFILE_A = "https://www.linkedin.com/in/profile-a";
const PROFILE_B = "https://www.linkedin.com/in/profile-b";

// --- fakes -------------------------------------------------------------------

/** A chrome-shaped event that records its listeners so we can count and fire them. */
function fakeEvent() {
  const listeners = [];
  return {
    listeners,
    addListener: (fn) => listeners.push(fn),
    removeListener: (fn) => {
      const i = listeners.indexOf(fn);
      if (i >= 0) listeners.splice(i, 1);
    },
    fire: (...args) => listeners.slice().forEach((fn) => fn(...args)),
  };
}

function fakeChrome() {
  return {
    tabs: {
      onActivated: fakeEvent(),
      onUpdated: fakeEvent(),
      onRemoved: fakeEvent(),
    },
    runtime: { onMessage: fakeEvent() },
  };
}

/** A controllable clock so debounce is deterministic rather than slept through. */
function fakeClock() {
  let seq = 0;
  const timers = new Map();
  return {
    setTimeoutFn: (fn) => {
      const id = ++seq;
      timers.set(id, fn);
      return id;
    },
    clearTimeoutFn: (id) => timers.delete(id),
    /** Run everything currently scheduled. */
    tick() {
      const pending = Array.from(timers.entries());
      timers.clear();
      pending.forEach(([, fn]) => fn());
    },
    get pending() {
      return timers.size;
    },
  };
}

function draft(overrides) {
  return Object.assign(
    {
      status: "ok",
      profile: { full_name: "Person", headline: "Headline", warnings: [] },
      experiences: [],
      experienceCount: 0,
      missingSections: [],
      pageWarnings: [],
      currentRoles: [],
      excludedSections: [],
    },
    overrides || {}
  );
}

/**
 * Build a controller over a scripted browser.
 *
 * `backend` counts anything that would constitute a write. Nothing in the
 * controller can reach it — which is the point of asserting on it.
 */
function harness(script) {
  const cfg = script || {};
  const chrome = fakeChrome();
  const clock = fakeClock();
  const calls = { detect: 0, preview: 0 };
  const backend = { writes: 0 };
  const states = [];

  let current = cfg.initial || { surface: SURFACES.PERSON_PROFILE, url: PROFILE_A };
  let previewResult = cfg.draft || draft();
  let previewGate = null;

  let fakeNow = 100000;
  const sync = createLiveSync({
    chrome,
    debounceMs: 5,
    minRereadMs: cfg.minRereadMs == null ? 0 : cfg.minRereadMs,
    nowFn: () => fakeNow,
    setTimeoutFn: clock.setTimeoutFn,
    clearTimeoutFn: clock.clearTimeoutFn,
    detect: async () => {
      calls.detect += 1;
      return current;
    },
    preview: async () => {
      calls.preview += 1;
      if (previewGate) await previewGate;
      return typeof previewResult === "function" ? previewResult() : previewResult;
    },
    onState: (s) => states.push(s),
  });

  return {
    chrome,
    clock,
    calls,
    backend,
    states,
    sync,
    last: () => states[states.length - 1],
    setPage: (page) => {
      current = page;
    },
    setDraft: (d) => {
      previewResult = d;
    },
    advance: (ms) => {
      fakeNow += ms;
    },
    gate: () => {
      let release;
      previewGate = new Promise((r) => (release = r));
      return () => {
        previewGate = null;
        release();
      };
    },
  };
}

const settle = () => new Promise((r) => setImmediate(r));

// --- 1. Profile A -> Profile B ----------------------------------------------

test("navigating from one profile to another updates URL and preview automatically", async () => {
  const h = harness();
  h.sync.start();
  await settle();
  assert.equal(h.sync.state.url, PROFILE_A);

  h.setPage({ surface: SURFACES.PERSON_PROFILE, url: PROFILE_B });
  h.chrome.tabs.onUpdated.fire(1, { url: PROFILE_B });
  h.clock.tick();
  await settle();
  await settle();

  assert.equal(h.sync.state.url, PROFILE_B);
  assert.equal(h.sync.state.pageKey, pageKeyOf(PROFILE_B));
  assert.equal(h.sync.state.phase, PHASES.READY);
});

// --- 2. Reload cannot preserve stale provenance ------------------------------

test("a reload re-reads provenance rather than trusting what was displayed", async () => {
  const h = harness();
  h.sync.start();
  await settle();

  // The tab reloads onto a different profile — the exact shape that produced
  // the stale Mode card: displayed URL from the old page, data from the new.
  h.setPage({ surface: SURFACES.PERSON_PROFILE, url: PROFILE_B });
  h.chrome.tabs.onUpdated.fire(1, { status: "complete" });
  h.clock.tick();
  await settle();
  await settle();

  assert.equal(h.sync.state.url, PROFILE_B, "the source card must never lag the page");
});

// --- 3. Active-tab switching -------------------------------------------------

test("switching the active tab updates the panel", async () => {
  const h = harness();
  h.sync.start();
  await settle();

  h.setPage({ surface: SURFACES.PERSON_PROFILE, url: PROFILE_B });
  h.chrome.tabs.onActivated.fire({ tabId: 2 });
  h.clock.tick();
  await settle();
  await settle();

  assert.equal(h.sync.state.url, PROFILE_B);
});

// --- 4. SPA navigation -------------------------------------------------------

test("LinkedIn in-app navigation is detected without a page load", async () => {
  const h = harness();
  h.sync.start();
  await settle();
  const before = h.calls.preview;

  h.setPage({ surface: SURFACES.PERSON_PROFILE, url: PROFILE_B });
  h.chrome.runtime.onMessage.fire({ type: "PS_PAGE_NAVIGATED", url: PROFILE_B });
  h.clock.tick();
  await settle();
  await settle();

  assert.equal(h.sync.state.url, PROFILE_B);
  assert.ok(h.calls.preview > before);
});

// --- 5. Opening the panel ----------------------------------------------------

test("opening the panel on a profile previews it without a click", async () => {
  const h = harness();
  assert.equal(h.calls.preview, 0);

  h.sync.start();
  await settle();
  await settle();

  assert.equal(h.calls.preview, 1);
  assert.equal(h.sync.state.phase, PHASES.READY);
  assert.ok(h.sync.state.draft);
});

// --- 6. Zero backend writes --------------------------------------------------

test("automatic preview performs no backend write", async () => {
  const h = harness();
  h.sync.start();
  await settle();

  h.setPage({ surface: SURFACES.PERSON_PROFILE, url: PROFILE_B });
  h.chrome.tabs.onUpdated.fire(1, { url: PROFILE_B });
  h.clock.tick();
  await settle();
  h.chrome.runtime.onMessage.fire({ type: "PS_DOM_CHANGED" });
  h.clock.tick();
  await settle();
  await settle();

  assert.equal(h.backend.writes, 0);
  assert.equal(h.sync.state.writes, 0);
  // Stronger than a counter: the controller was given exactly two capabilities,
  // neither of which can write.
  assert.equal(h.calls.detect > 0, true);
  assert.equal(h.calls.preview > 0, true);
});

// --- 7. Debounced reread on mutation ----------------------------------------

test("a burst of DOM mutations causes one reread, not one per mutation", async () => {
  const h = harness({ draft: draft({ status: "partial", missingSections: ["experience"] }) });
  h.sync.start();
  await settle();
  const before = h.calls.preview;

  for (let i = 0; i < 8; i += 1) h.chrome.runtime.onMessage.fire({ type: "PS_DOM_CHANGED" });
  assert.equal(h.calls.preview, before, "nothing runs before the debounce elapses");

  h.clock.tick();
  await settle();
  await settle();

  assert.equal(h.calls.preview, before + 1, "eight mutations, one read");
});

// --- 8. Lazy-loaded experience ----------------------------------------------

test("content that loads after the operator scrolls updates the preview", async () => {
  const h = harness({ draft: draft({ experiences: [], missingSections: ["experience"] }) });
  h.sync.start();
  await settle();
  await settle();
  assert.equal(h.sync.state.phase, PHASES.WARNINGS, "missing experience is a warning, not silence");

  h.setDraft(draft({ experiences: [{ position_index: 1 }], experienceCount: 1 }));
  h.chrome.runtime.onMessage.fire({ type: "PS_DOM_CHANGED" });
  h.clock.tick();
  await settle();
  await settle();

  assert.equal(h.sync.state.draft.experiences.length, 1);
  assert.equal(h.sync.state.phase, PHASES.UPDATED);
});

// --- 9. Stable fields --------------------------------------------------------

test("a reread that finds the same content leaves the fields unchanged", async () => {
  // Deliberately an INCOMPLETE draft: a complete one is not reread at all,
  // which is the loop guard and is covered separately below.
  const h = harness({ draft: draft({ status: "partial", missingSections: ["experience"] }) });
  h.sync.start();
  await settle();
  await settle();
  const first = h.sync.state.draft;

  h.chrome.runtime.onMessage.fire({ type: "PS_DOM_CHANGED" });
  h.clock.tick();
  await settle();
  await settle();

  assert.deepEqual(h.sync.state.draft.profile, first.profile);
  assert.equal(h.sync.state.phase, PHASES.WARNINGS, "an incomplete page keeps warning");
  assert.equal(h.sync.state.pagePreviews, 2, "it did reread");
});

// --- 10. Stale results -------------------------------------------------------

test("a slow read of the previous profile cannot overwrite the current one", async () => {
  const h = harness();
  h.sync.start();
  await settle();
  await settle();

  // Start a read of A and hold it open.
  const release = h.gate();
  h.setDraft(draft({ profile: { full_name: "Person A", warnings: [] } }));
  h.chrome.runtime.onMessage.fire({ type: "PS_DOM_CHANGED" });
  h.clock.tick();
  await settle();

  // The operator moves to B while A is still in flight, and B resolves first.
  h.setPage({ surface: SURFACES.PERSON_PROFILE, url: PROFILE_B });
  h.setDraft(draft({ profile: { full_name: "Person B", warnings: [] } }));
  h.chrome.tabs.onUpdated.fire(1, { url: PROFILE_B });
  h.clock.tick();
  await settle();
  await settle();

  // Now let the stale read of A finish.
  release();
  await settle();
  await settle();

  assert.equal(h.sync.state.url, PROFILE_B);
  assert.equal(h.sync.state.draft.profile.full_name, "Person B", "A's result must be discarded");
});

// --- 11. Duplicate listeners -------------------------------------------------

test("starting twice does not register a second set of listeners", async () => {
  const h = harness();
  h.sync.start();
  await settle();
  const count = h.sync.listenerCount;
  const activated = h.chrome.tabs.onActivated.listeners.length;

  h.sync.start();
  await settle();

  assert.equal(h.sync.listenerCount, count);
  assert.equal(h.chrome.tabs.onActivated.listeners.length, activated);

  // And one event still causes one read.
  const before = h.calls.preview;
  h.chrome.tabs.onActivated.fire({ tabId: 1 });
  h.clock.tick();
  await settle();
  await settle();
  assert.equal(h.calls.preview, before + 1);
});

test("stop() removes every listener it added", async () => {
  const h = harness();
  h.sync.start();
  await settle();
  assert.ok(h.sync.listenerCount > 0);

  h.sync.stop();
  assert.equal(h.sync.listenerCount, 0);
  assert.equal(h.chrome.tabs.onActivated.listeners.length, 0);
  assert.equal(h.chrome.runtime.onMessage.listeners.length, 0);
});

// --- 12. Repeated events do not duplicate data -------------------------------

test("repeated preview events neither submit nor accumulate drafts", async () => {
  const h = harness({ draft: draft({ status: "partial", missingSections: ["experience"] }) });
  h.sync.start();
  await settle();
  await settle();

  for (let i = 0; i < 5; i += 1) {
    h.chrome.runtime.onMessage.fire({ type: "PS_DOM_CHANGED" });
    h.clock.tick();
    await settle();
    await settle();
  }

  assert.equal(h.backend.writes, 0);
  assert.ok(!Array.isArray(h.sync.state.draft), "the draft is replaced, never appended to");
  assert.equal(h.sync.state.draft.experiences.length, 0);
});

// --- 13. Unsupported surfaces ------------------------------------------------

test("an unsupported LinkedIn surface is never parsed", async () => {
  const h = harness({ initial: { surface: SURFACES.COMPANY_PROFILE, url: "https://www.linkedin.com/company/x" } });
  h.sync.start();
  await settle();
  await settle();

  assert.equal(h.calls.preview, 0, "the profile parser must not run on a company page");
  assert.equal(h.sync.state.phase, PHASES.UNSUPPORTED);
  assert.equal(h.sync.state.draft, null);
});

test("a page that is not LinkedIn at all leaves the panel waiting", async () => {
  const h = harness({ initial: { surface: SURFACES.UNSUPPORTED, url: "https://example.com/" } });
  h.sync.start();
  await settle();
  await settle();

  assert.equal(h.calls.preview, 0);
  assert.equal(h.sync.state.phase, PHASES.WAITING);
});

test("moving from a profile to an unsupported page clears the previous draft", async () => {
  const h = harness();
  h.sync.start();
  await settle();
  await settle();
  assert.ok(h.sync.state.draft);

  h.setPage({ surface: SURFACES.UNSUPPORTED, url: "https://example.com/" });
  h.chrome.tabs.onUpdated.fire(1, { url: "https://example.com/" });
  h.clock.tick();
  await settle();
  await settle();

  assert.equal(h.sync.state.draft, null, "another page's data must not linger on screen");
});

// --- 14. Extension reload ----------------------------------------------------

test("a fresh controller restores the current page without operator action", async () => {
  // An extension reload destroys the panel's state entirely. Starting again
  // must recover from the tab, not from anything remembered.
  const h = harness({ initial: { surface: SURFACES.PERSON_PROFILE, url: PROFILE_B } });
  h.sync.start();
  await settle();
  await settle();

  assert.equal(h.sync.state.url, PROFILE_B);
  assert.equal(h.sync.state.phase, PHASES.READY);
});

// --- 15. The manual Read button ---------------------------------------------

test("the manual Read action still works as a retry", async () => {
  const h = harness();
  h.sync.start();
  await settle();
  await settle();
  const before = h.calls.preview;

  await h.sync.refresh();
  await settle();

  assert.equal(h.calls.preview, before + 1);
  assert.equal(h.sync.state.phase, PHASES.READY);
});

// --- page identity -----------------------------------------------------------

test("tracking parameters do not count as a different page", () => {
  assert.equal(
    pageKeyOf("https://www.linkedin.com/in/person/?trk=feed&originalSubdomain=uk"),
    pageKeyOf("https://www.linkedin.com/in/person")
  );
  assert.notEqual(pageKeyOf(PROFILE_A), pageKeyOf(PROFILE_B));
  assert.equal(pageKeyOf(null), null);
});

test("a query-only change does not reset the draft", async () => {
  const h = harness();
  h.sync.start();
  await settle();
  await settle();
  const first = h.sync.state.draft;

  h.setPage({ surface: SURFACES.PERSON_PROFILE, url: PROFILE_A + "?trk=nav" });
  h.chrome.tabs.onUpdated.fire(1, { url: PROFILE_A + "?trk=nav" });
  h.clock.tick();
  await settle();
  await settle();

  assert.equal(h.sync.state.pageKey, pageKeyOf(PROFILE_A));
  assert.deepEqual(h.sync.state.draft.profile, first.profile);
});


// --- the reread loop guard ---------------------------------------------------

test("a fully read page ignores further DOM mutations", async () => {
  // LinkedIn mutates continuously — images resolve, the rail updates, trackers
  // fiddle with attributes. Without this, the panel would re-parse the page
  // every debounce window for as long as it stayed open.
  const h = harness();
  h.sync.start();
  await settle();
  await settle();
  const after = h.calls.preview;
  assert.equal(h.sync.state.phase, PHASES.READY);

  for (let i = 0; i < 20; i += 1) {
    h.chrome.runtime.onMessage.fire({ type: "PS_DOM_CHANGED" });
    h.clock.tick();
    await settle();
  }

  assert.equal(h.calls.preview, after, "a complete page is not re-parsed on churn");
});

test("navigation is honoured even when the current page was complete", async () => {
  const h = harness();
  h.sync.start();
  await settle();
  await settle();
  const after = h.calls.preview;

  h.setPage({ surface: SURFACES.PERSON_PROFILE, url: PROFILE_B });
  h.chrome.tabs.onUpdated.fire(1, { url: PROFILE_B });
  h.clock.tick();
  await settle();
  await settle();

  assert.equal(h.calls.preview, after + 1, "a new page is new information regardless");
  assert.equal(h.sync.state.url, PROFILE_B);
});

test("mutation rereads are floored to a minimum interval", async () => {
  const h = harness({
    draft: draft({ status: "partial", missingSections: ["experience"] }),
    minRereadMs: 1500,
  });
  h.sync.start();
  await settle();
  await settle();
  const after = h.calls.preview;

  // Immediately after a read, a mutation is ignored.
  h.chrome.runtime.onMessage.fire({ type: "PS_DOM_CHANGED" });
  h.clock.tick();
  await settle();
  assert.equal(h.calls.preview, after, "too soon");

  // Once the floor has elapsed it is honoured.
  h.advance(2000);
  h.chrome.runtime.onMessage.fire({ type: "PS_DOM_CHANGED" });
  h.clock.tick();
  await settle();
  await settle();
  assert.equal(h.calls.preview, after + 1);
});

test("the reread floor is per page, so a failed first read can retry at once", async () => {
  // The floor exists to stop one page being re-parsed in a loop. It must not
  // carry across a navigation: if the first read of a NEW page fails, the very
  // next mutation there should be allowed to retry, not wait out a timer that
  // was started by the previous profile.
  const h = harness({ minRereadMs: 1500 });
  h.sync.start();
  await settle();
  await settle();

  h.setDraft(() => {
    throw new Error("read failed");
  });
  h.setPage({ surface: SURFACES.PERSON_PROFILE, url: PROFILE_B });
  h.chrome.tabs.onUpdated.fire(1, { url: PROFILE_B });
  h.clock.tick();
  await settle();
  await settle();
  const after = h.calls.preview;
  assert.equal(h.sync.state.draft, null, "the failed read left no draft");

  // No time has passed on the fake clock, so only the per-page reset can let
  // this through.
  h.setDraft(draft());
  h.chrome.runtime.onMessage.fire({ type: "PS_DOM_CHANGED" });
  h.clock.tick();
  await settle();
  await settle();

  assert.equal(h.calls.preview, after + 1, "the new page retried immediately");
  assert.equal(h.sync.state.phase, PHASES.READY);
});

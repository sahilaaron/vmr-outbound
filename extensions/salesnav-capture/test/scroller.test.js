/**
 * DAT-018 D — operator-controlled incremental scrolling.
 *
 * Every test drives the pass with an INJECTED clock and random source, so the
 * behaviour is fully deterministic: no real timers, no wall-clock, no reliance
 * on jitter being "small enough". That is the point of injecting them.
 *
 * These tests exist to hold three lines:
 *   1. the pass is bounded and stops for a stated reason;
 *   2. the operator can cancel it and cancellation is honoured promptly;
 *   3. it never navigates, paginates, or clicks anything.
 */

const test = require("node:test");
const assert = require("node:assert/strict");

global.self = global;
require("../src/common/constants.js");
const { runScrollPass, STOP } = require("../src/common/scroller.js");

/** A scrollable element double that records every scroll instruction. */
function fakeScroller({ contentHeight = 4000, viewport = 800 } = {}) {
  let top = 0;
  const calls = [];
  return {
    calls,
    get scrollTop() {
      return top;
    },
    get clientHeight() {
      return viewport;
    },
    get scrollHeight() {
      return contentHeight;
    },
    scrollTo(opt) {
      calls.push(opt);
      top = Math.max(0, Math.min(opt.top, contentHeight - viewport));
    },
  };
}

/** Deterministic clock: sleeping advances virtual time, nothing else does. */
function fakeClock() {
  let t = 0;
  return {
    now: () => t,
    sleep: async (ms) => {
      t += ms;
    },
    get elapsed() {
      return t;
    },
  };
}

function run(overrides = {}) {
  const clock = fakeClock();
  const scroller = overrides.scroller || fakeScroller();
  let rows = overrides.startRows != null ? overrides.startRows : 10;
  const deps = {
    scroller,
    countRows: overrides.countRows || (() => rows),
    sleep: clock.sleep,
    now: clock.now,
    random: overrides.random || (() => 0),
    isCancelled: overrides.isCancelled || (() => false),
    onProgress: overrides.onProgress,
  };
  return {
    clock,
    scroller,
    setRows: (n) => {
      rows = n;
    },
    promise: runScrollPass(deps, overrides.options || {}),
  };
}

test("a page that never grows stops as stabilized, not by timeout", async () => {
  const h = run();
  const r = await h.promise;
  assert.equal(r.stopReason, STOP.STABILIZED);
  assert.ok(r.steps >= 1);
  assert.ok(r.elapsedMs < 20000, "must stop well inside the time budget");
});

test("scrolling is incremental: each step is a fraction of the viewport", async () => {
  const scroller = fakeScroller({ viewport: 800, contentHeight: 100000 });
  const r = await run({ scroller }).promise;
  const advances = scroller.calls.filter((c) => c.top > 0);
  assert.ok(advances.length > 0);
  // 0.35 * 800 = 280px per step: far less than one screen, so nothing is
  // scrolled past before it can render.
  assert.equal(advances[0].top, 280);
  assert.ok(
    advances.every((c) => c.behavior === "smooth"),
    "every scroll must be smooth, never an abrupt jump"
  );
  assert.ok(r.steps <= 120);
});

test("a very short viewport still advances by the minimum step", async () => {
  const scroller = fakeScroller({ viewport: 100, contentHeight: 100000 });
  await run({ scroller }).promise;
  assert.equal(scroller.calls[0].top, 120); // MIN_STEP_PX, not 35
});

test("new rows reset the stability counter and earn a longer settle", async () => {
  let rows = 10;
  let reads = 0;
  const clock = fakeClock();
  const scroller = fakeScroller({ contentHeight: 100000 });
  const r = await runScrollPass(
    {
      scroller,
      countRows: () => {
        reads += 1;
        if (reads === 2 || reads === 4) rows += 5; // two growth events
        return rows;
      },
      sleep: clock.sleep,
      now: clock.now,
      random: () => 0,
    },
    {}
  );
  assert.equal(r.startRows, 10);
  assert.equal(r.rows, 20);
  assert.equal(r.stopReason, STOP.STABILIZED);
});

test("the operator can cancel, and cancellation wins over every other outcome", async () => {
  // The page keeps growing, so nothing but cancellation can end this pass —
  // otherwise the test would pass for the wrong reason.
  let rows = 10;
  let checks = 0;
  const clock = fakeClock();
  const scroller = fakeScroller({ contentHeight: 10 ** 9 });
  const r = await runScrollPass(
    {
      scroller,
      countRows: () => (rows += 2),
      sleep: clock.sleep,
      now: clock.now,
      random: () => 0,
      isCancelled: () => ++checks > 3,
    },
    {}
  );
  assert.equal(r.stopReason, STOP.CANCELLED);
  assert.equal(r.steps, 3, "must stop at the first check after cancellation");
});

test("cancellation before the first step does no scrolling at all", async () => {
  const scroller = fakeScroller();
  const r = await run({ scroller, isCancelled: () => true }).promise;
  assert.equal(r.stopReason, STOP.CANCELLED);
  assert.equal(r.steps, 0);
  // Only the return-to-top call, never a downward scroll.
  assert.ok(scroller.calls.every((c) => c.top === 0));
});

test("the step ceiling bounds an endlessly growing page", async () => {
  // An infinite feed: content and rows grow faster than we consume them. The
  // pass must still terminate — there is no infinite scrolling here.
  let rows = 10;
  let height = 10000;
  const clock = fakeClock();
  let top = 0;
  const scroller = {
    get scrollTop() {
      return top;
    },
    get clientHeight() {
      return 800;
    },
    get scrollHeight() {
      height += 5000;
      return height;
    },
    scrollTo(opt) {
      top = opt.top;
    },
  };
  const r = await runScrollPass(
    {
      scroller,
      countRows: () => (rows += 3),
      sleep: clock.sleep,
      now: clock.now,
      random: () => 0,
    },
    { budgetMs: Number.MAX_SAFE_INTEGER }
  );
  assert.equal(r.stopReason, STOP.STEP_LIMIT);
  assert.equal(r.steps, 120);
});

test("the time budget bounds a page that keeps mounting rows slowly", async () => {
  let rows = 10;
  const clock = fakeClock();
  const scroller = fakeScroller({ contentHeight: 10 ** 9 });
  const r = await runScrollPass(
    {
      scroller,
      countRows: () => (rows += 1),
      sleep: clock.sleep,
      now: clock.now,
      random: () => 0,
    },
    { maxSteps: 10 ** 6, budgetMs: 5000 }
  );
  assert.equal(r.stopReason, STOP.TIME_BUDGET);
  assert.ok(r.elapsedMs >= 5000);
});

test("reaching the end of loaded content stops the pass", async () => {
  // Content barely taller than the viewport: one step reaches the bottom.
  const scroller = fakeScroller({ contentHeight: 900, viewport: 800 });
  const r = await run({ scroller }).promise;
  assert.equal(r.stopReason, STOP.END_OF_CONTENT);
});

test("the view is returned to the top so the operator is not left adrift", async () => {
  const scroller = fakeScroller({ contentHeight: 100000 });
  const r = await run({ scroller }).promise;
  assert.equal(r.returnedToTop, true);
  assert.equal(scroller.calls[scroller.calls.length - 1].top, 0);
  assert.equal(scroller.scrollTop, 0);
});

test("returning to the top can be turned off", async () => {
  const scroller = fakeScroller({ contentHeight: 100000 });
  const r = await run({ scroller, options: { returnToTop: false } }).promise;
  assert.equal(r.returnedToTop, false);
  assert.ok(scroller.scrollTop > 0);
});

test("jitter is bounded, deterministic, and never negative", async () => {
  // Same page, same clock, two different random sources: the timing differs by
  // no more than the documented jitter per pause, and both are reproducible.
  const quiet = await run({ random: () => 0 }).promise;
  const jittered = await run({ random: () => 0.999 }).promise;
  assert.equal(quiet.steps, jittered.steps, "jitter must not change the plan");
  assert.ok(jittered.elapsedMs >= quiet.elapsedMs);
  const pauses = quiet.steps + 2;
  assert.ok(
    jittered.elapsedMs - quiet.elapsedMs <= 60 * pauses * 2,
    "jitter must stay inside its documented bound"
  );
});

test("the same inputs always produce the same pass", async () => {
  const a = await run({ random: () => 0.5 }).promise;
  const b = await run({ random: () => 0.5 }).promise;
  assert.deepEqual(a, b);
});

test("progress is reported so the operator can watch and stop", async () => {
  const seen = [];
  const scroller = fakeScroller({ contentHeight: 100000 });
  const r = await run({ scroller, onProgress: (p) => seen.push(p) }).promise;
  assert.equal(seen[0].phase, "start");
  assert.equal(seen[seen.length - 1].phase, "done");
  assert.equal(seen[seen.length - 1].stopReason, r.stopReason);
  assert.ok(seen.some((p) => p.phase === "step"));
  for (const p of seen) {
    assert.equal(typeof p.steps, "number");
    assert.equal(typeof p.rows, "number");
    assert.equal(typeof p.elapsedMs, "number");
  }
});

test("the pass only ever scrolls: it never clicks, navigates or paginates", async () => {
  // The scroller double exposes exactly four members. If the implementation
  // reached for anything else — a click(), a location assignment, a "next page"
  // button — it would throw here rather than silently acquiring a new power.
  const scroller = fakeScroller({ contentHeight: 100000 });
  const guarded = new Proxy(scroller, {
    get(target, prop) {
      const allowed = ["scrollTop", "clientHeight", "scrollHeight", "scrollTo", "calls"];
      if (typeof prop === "string" && !allowed.includes(prop)) {
        throw new Error("scroll pass touched forbidden member: " + prop);
      }
      return target[prop];
    },
  });
  const r = await run({ scroller: guarded }).promise;
  assert.equal(r.stopReason, STOP.STABILIZED);
  assert.ok(scroller.calls.length > 0);
});

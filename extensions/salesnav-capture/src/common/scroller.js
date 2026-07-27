/**
 * Operator-controlled incremental scrolling for an already-open Sales Navigator
 * results page (DAT-018 D).
 *
 * What this is: one discrete, operator-initiated pass over the results list that
 * is currently on screen, advancing a fraction of a viewport at a time and
 * pausing long enough for lazily-mounted rows to render. It stops on the first
 * of: no new rows after a bounded number of checks, operator cancellation, the
 * end of the scrollable content, a step ceiling, or a time budget.
 *
 * What this deliberately is NOT: pagination, navigation, unattended traversal,
 * infinite scrolling, CAPTCHA handling, stealth, fingerprint manipulation, or
 * rate-limit evasion. It never clicks anything, never changes the URL, and never
 * runs unless the operator started it.
 *
 * On jitter: each pause carries a small bounded jitter. Its only purpose is to
 * stop the polling interval locking in phase with the page's own render cadence,
 * which otherwise causes row counts to be read mid-mount. It is not intended to
 * look human and is far too small and too regular to serve that purpose. The
 * clock and the random source are both injected, so tests are fully
 * deterministic and the pass can be verified step by step.
 *
 * UMD module -> Node CommonJS + self.SNCapture.scroller
 */
(function (root, factory) {
  const factoryResult = factory(
    typeof module !== "undefined" && module.exports
      ? require("./constants.js")
      : (typeof self !== "undefined" ? self : root).SNCapture.constants
  );
  if (typeof module !== "undefined" && module.exports) module.exports = factoryResult;
  const g = typeof self !== "undefined" ? self : root;
  g.SNCapture = Object.assign(g.SNCapture || {}, { scroller: factoryResult });
})(typeof globalThis !== "undefined" ? globalThis : this, function (constants) {
  "use strict";

  const { SCROLL, LIMITS } = constants;

  const STOP = {
    STABILIZED: "stabilized",
    CANCELLED: "cancelled",
    END_OF_CONTENT: "end_of_content",
    STEP_LIMIT: "step_limit",
    TIME_BUDGET: "time_budget",
  };

  /**
   * Run one incremental scroll pass.
   *
   * @param {object} deps
   * @param {{scrollTop:number, clientHeight:number, scrollHeight:number, scrollTo:Function}} deps.scroller
   *   The scrollable element (or a test double). Only these four members are used.
   * @param {() => number} deps.countRows        Current visible row count.
   * @param {(ms:number) => Promise<void>} deps.sleep  Injected clock.
   * @param {() => number} deps.now              Injected clock (ms).
   * @param {() => number} [deps.random]         Injected [0,1) source for jitter.
   * @param {() => boolean} [deps.isCancelled]   Polled before every increment.
   * @param {(progress:object) => void} [deps.onProgress]
   * @param {object} [options]                   Overrides for SCROLL/LIMITS values.
   * @returns {Promise<{stopReason:string, steps:number, rows:number, startRows:number,
   *                    elapsedMs:number, returnedToTop:boolean}>}
   */
  async function runScrollPass(deps, options) {
    const opts = Object.assign(
      {
        stepRatio: SCROLL.STEP_RATIO,
        minStepPx: SCROLL.MIN_STEP_PX,
        settleMs: SCROLL.SETTLE_MS,
        growthSettleMs: SCROLL.GROWTH_SETTLE_MS,
        stableChecks: SCROLL.STABLE_CHECKS,
        maxSteps: SCROLL.MAX_STEPS,
        jitterMs: SCROLL.JITTER_MS,
        budgetMs: LIMITS.CAPTURE_SCROLL_BUDGET_MS,
        returnToTop: true,
      },
      options || {}
    );

    const { scroller, countRows, sleep, now } = deps;
    const random = deps.random || (() => 0);
    const isCancelled = deps.isCancelled || (() => false);
    const onProgress = deps.onProgress || (() => {});

    const startedAt = now();
    const startRows = countRows();
    let rows = startRows;
    let steps = 0;
    let stable = 0;
    let stopReason = STOP.STABILIZED;

    // Bounded jitter: [0, jitterMs). Documented above; deterministic in tests.
    const pause = (base) => sleep(base + Math.floor(random() * opts.jitterMs));

    const report = (phase) =>
      onProgress({
        phase,
        steps,
        rows,
        startRows,
        elapsedMs: now() - startedAt,
        scrollTop: scroller.scrollTop,
        stopReason: phase === "done" ? stopReason : null,
      });

    report("start");

    while (true) {
      if (isCancelled()) {
        stopReason = STOP.CANCELLED;
        break;
      }
      if (steps >= opts.maxSteps) {
        stopReason = STOP.STEP_LIMIT;
        break;
      }
      if (now() - startedAt >= opts.budgetMs) {
        stopReason = STOP.TIME_BUDGET;
        break;
      }

      const viewport = scroller.clientHeight || 0;
      const maxTop = Math.max(0, (scroller.scrollHeight || 0) - viewport);
      if (scroller.scrollTop >= maxTop) {
        // Already at the bottom of what is loaded. Give the page one settle
        // window to mount anything new, then decide on the row count.
        await pause(opts.settleMs);
        const after = countRows();
        const grew = after > rows;
        rows = after;
        if (grew) {
          stable = 0;
          report("grew");
          continue;
        }
        stable += 1;
        if (stable >= opts.stableChecks) {
          stopReason = STOP.END_OF_CONTENT;
          break;
        }
        report("waiting");
        continue;
      }

      const step = Math.max(opts.minStepPx, Math.floor(viewport * opts.stepRatio));
      scroller.scrollTo({ top: scroller.scrollTop + step, behavior: "smooth" });
      steps += 1;

      await pause(opts.settleMs);
      const after = countRows();
      if (after > rows) {
        rows = after;
        stable = 0;
        // A batch just mounted; give layout a longer moment before measuring.
        await pause(opts.growthSettleMs);
        report("grew");
      } else {
        stable += 1;
        report("step");
        if (stable >= opts.stableChecks) {
          stopReason = STOP.STABILIZED;
          break;
        }
      }
    }

    // Leave the operator's view where they left it rather than scrolled away.
    let returnedToTop = false;
    if (opts.returnToTop) {
      scroller.scrollTo({ top: 0, behavior: "smooth" });
      returnedToTop = true;
      await pause(opts.settleMs);
    }

    rows = countRows();
    report("done");
    return {
      stopReason,
      steps,
      rows,
      startRows,
      elapsedMs: now() - startedAt,
      returnedToTop,
    };
  }

  return { runScrollPass, STOP };
});

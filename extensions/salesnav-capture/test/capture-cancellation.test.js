"use strict";
/**
 * Operator cancellation of a Sales Navigator read pass (DAT-018 D).
 *
 * The scrolling pass has always been cancellable inside the content script, but
 * until this route existed the operator had no way to invoke it: the panel had
 * no control and the worker had no message. These tests hold the whole path
 * open — panel control → worker route → content script — and hold the line that
 * cancelling is an operator action, not a failure: whatever loaded is kept,
 * nothing is submitted, and a fresh capture still works afterwards.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const { createWorker, SALES_TAB } = require("./worker-harness.js");
const { createPanel, DEFAULT_PREFS, fixtures } = require("./panel-harness.js");
const { stripComments } = require("./strip-comments.js");

const SRC = path.join(__dirname, "..", "src");
const SURFACES = require("../src/common/constants.js").SURFACES;

/** A promise plus its resolver, for holding a capture in flight. */
function deferred() {
  let resolve;
  const promise = new Promise((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

// --- the service-worker route -------------------------------------------------

test("the worker exposes a CANCEL_CAPTURE route", async () => {
  const worker = createWorker({
    tabs: [SALES_TAB],
    onTabMessage: () => ({ ok: true, cancelled: true, passId: 3 }),
  });
  const response = await worker.dispatch({ type: "CANCEL_CAPTURE" });
  assert.ok(response, "CANCEL_CAPTURE is not routed");
  assert.equal(response.ok, true);
});

test("cancelling reaches the content script while a capture is in flight", async () => {
  const held = deferred();
  const worker = createWorker({
    tabs: [SALES_TAB],
    onTabMessage: (_id, message) => {
      if (message.type === "CS_CAPTURE") return held.promise;
      if (message.type === "CS_CANCEL_SCROLL") return { ok: true, cancelled: true, passId: 1 };
      return {};
    },
  });

  const capturing = worker.dispatch({ type: "CAPTURE_ACTIVE_PAGE" });
  await new Promise((r) => setTimeout(r, 0));

  // Cancel lands while the pass is still running — this is "promptly": the
  // stop is delivered during the pass, not queued behind its natural end.
  const cancelled = await worker.dispatch({ type: "CANCEL_CAPTURE" });
  assert.equal(cancelled.cancelled, true);
  assert.ok(
    worker.tabMessages.some((m) => m.message.type === "CS_CANCEL_SCROLL"),
    "the worker never told the content script to stop"
  );

  held.resolve({
    status: "ok",
    records: [],
    pageWarnings: [],
    sourceSearchUrl: SALES_TAB.url,
    capturedAt: "2026-07-27T10:00:00.000Z",
    count: 0,
    scroll: { stopReason: "cancelled", steps: 2, rows: 0, startRows: 0, returnedToTop: true },
  });
  const result = await capturing;
  assert.equal(result.ok, true, "a cancelled capture still resolves, it does not error");
});

test("cancelling with no capture running touches nothing and says so", async () => {
  const worker = createWorker({
    tabs: [SALES_TAB],
    onTabMessage: () => {
      throw new Error("the content script must not be contacted");
    },
  });
  const response = await worker.dispatch({ type: "CANCEL_CAPTURE" });
  // Spread first: the worker runs in its own realm, so a cross-realm object
  // would fail a strict prototype comparison for reasons unrelated to the fix.
  assert.deepEqual({ ...response }, { ok: true, cancelled: false, reason: "no_active_capture" });
  assert.equal(worker.tabMessages.length, 0, "an idle cancel must not message the tab");
  assert.equal(worker.injected.length, 0, "an idle cancel must not inject the content script");
});

test("a cancelled capture keeps the rows that had already loaded", async () => {
  const held = deferred();
  const worker = createWorker({
    tabs: [SALES_TAB],
    onTabMessage: (_id, message) => (message.type === "CS_CAPTURE" ? held.promise : { ok: true, cancelled: true }),
  });
  const capturing = worker.dispatch({ type: "CAPTURE_ACTIVE_PAGE" });
  await new Promise((r) => setTimeout(r, 0));
  await worker.dispatch({ type: "CANCEL_CAPTURE" });

  held.resolve({
    status: "ok",
    records: [
      {
        rawFullName: "Dana Whitfield",
        firstName: "Dana",
        lastName: "Whitfield",
        title: "Head of Operations",
        companyName: "Northwind Logistics",
        linkedinProfileUrl: "https://www.linkedin.com/in/danawhitfield",
        warnings: [],
      },
    ],
    pageWarnings: [],
    sourceSearchUrl: SALES_TAB.url,
    sourcePageNumber: 1,
    capturedAt: "2026-07-27T10:00:00.000Z",
    count: 1,
    scroll: { stopReason: "cancelled", steps: 3, rows: 1, startRows: 0, returnedToTop: true },
  });

  const result = await capturing;
  assert.equal(result.added, 1, "rows loaded before the stop must be kept");
  assert.equal(result.batchView.records.length, 1);
  assert.equal(result.scroll.stopReason, "cancelled");
  assert.equal(result.scroll.returnedToTop, true, "the operator's view is put back");
});

test("a capture stopped before any row loaded still reports the cancellation", async () => {
  const worker = createWorker({
    tabs: [SALES_TAB],
    onTabMessage: () => ({
      status: "empty",
      records: [],
      pageWarnings: [],
      sourceSearchUrl: SALES_TAB.url,
      capturedAt: "2026-07-27T10:00:00.000Z",
      count: 0,
      scroll: { stopReason: "cancelled", steps: 0, rows: 0, startRows: 0, returnedToTop: true },
    }),
  });
  const result = await worker.dispatch({ type: "CAPTURE_ACTIVE_PAGE" });
  // The non-OK path must carry the scroll outcome, or the panel cannot tell a
  // cancelled read apart from a page with nothing on it.
  assert.equal(result.captureStatus, "empty");
  assert.equal(result.scroll.stopReason, "cancelled");
});

// --- the content script's own guard --------------------------------------------

/** Load the real content script into jsdom with the real shared modules. */
function loadContentScript() {
  const dom = new JSDOM("<!DOCTYPE html><body></body>", {
    url: SALES_TAB.url,
    runScripts: "dangerously",
  });
  const window = dom.window;
  const listeners = [];
  const sent = [];
  window.chrome = {
    runtime: {
      onMessage: { addListener: (fn) => listeners.push(fn) },
      sendMessage: (m) => sent.push(m),
    },
  };
  for (const file of ["constants.js", "normalize.js", "extraction.js", "scroller.js"]) {
    window.eval(fs.readFileSync(path.join(SRC, "common", file), "utf8"));
  }
  window.eval(fs.readFileSync(path.join(SRC, "content", "content-script.js"), "utf8"));

  function message(msg) {
    return new Promise((resolve) => {
      const kept = listeners[0](msg, { id: "t" }, resolve);
      if (kept !== true) setTimeout(() => resolve(undefined), 0);
    });
  }
  return { window, message, sent, dom };
}

test("a cancel that arrives with no pass running cancels nothing", async () => {
  const cs = loadContentScript();
  const response = await cs.message({ type: "CS_CANCEL_SCROLL" });
  assert.equal(response.ok, true);
  assert.equal(response.cancelled, false);
  assert.equal(response.reason, "no_active_pass");
});

test("a stale cancel does not arm the next capture", async () => {
  const cs = loadContentScript();
  // Cancel with nothing running — the classic stale event.
  await cs.message({ type: "CS_CANCEL_SCROLL" });
  // The next capture must run normally rather than stopping immediately.
  const page = await cs.message({ type: "CS_CAPTURE" });
  assert.notEqual(
    page.scroll.stopReason,
    "cancelled",
    "a cancel from before the pass leaked into it"
  );
});

test("repeated cancels during one pass are reported once and stay safe", async () => {
  const cs = loadContentScript();
  const capturing = cs.message({ type: "CS_CAPTURE" });
  await new Promise((r) => setTimeout(r, 0));
  const first = await cs.message({ type: "CS_CANCEL_SCROLL" });
  const second = await cs.message({ type: "CS_CANCEL_SCROLL" });
  assert.equal(first.cancelled, true);
  // The second is honoured idempotently: still the same pass, no error, and it
  // cannot arm anything once the pass ends.
  assert.equal(second.ok, true);
  const page = await capturing;
  assert.equal(page.scroll.stopReason, "cancelled");

  const afterwards = await cs.message({ type: "CS_CANCEL_SCROLL" });
  assert.equal(afterwards.cancelled, false, "a cancel after the pass must cancel nothing");
});

test("a fresh capture after a cancelled one runs to its own conclusion", async () => {
  const cs = loadContentScript();
  const first = cs.message({ type: "CS_CAPTURE" });
  await new Promise((r) => setTimeout(r, 0));
  await cs.message({ type: "CS_CANCEL_SCROLL" });
  const cancelled = await first;
  assert.equal(cancelled.scroll.stopReason, "cancelled");

  const second = await cs.message({ type: "CS_CAPTURE" });
  assert.notEqual(second.scroll.stopReason, "cancelled");
  assert.ok(second.scroll.passId > cancelled.scroll.passId, "each pass gets its own id");
});

// --- the panel control ---------------------------------------------------------

const PANEL_BASE = {
  GET_STATE: { ok: true, prefs: DEFAULT_PREFS, metadata: { labels: [], note: null }, batchView: null },
  PROFILE_GET_STATE: { ok: true, prefs: DEFAULT_PREFS, draftView: null },
  COMPANY_GET_STATE: { ok: true, draftView: null },
  FETCH_LABELS: { ok: true, labels: [] },
  DETECT_SURFACE: { ok: true, surface: SURFACES.SALESNAV_PEOPLE_RESULTS, url: SALES_TAB.url },
  DETECT_ACTIVE_PAGE: { ok: true, page: { supported: true, url: SALES_TAB.url, visibleCount: 12 } },
};

async function panelMidCapture(captureResponse) {
  const held = deferred();
  const p = await createPanel({
    responses: Object.assign({}, PANEL_BASE, {
      CAPTURE_ACTIVE_PAGE: () => held.promise,
      CANCEL_CAPTURE: { ok: true, cancelled: true },
    }),
  });
  await p.flush();
  await p.click("capture-btn");
  return { p, finish: () => held.resolve(captureResponse) };
}

test("the panel offers a stop control only while a read pass is running", async () => {
  const { p, finish } = await panelMidCapture({ ok: true, captureStatus: "ok", added: 0, batchView: fixtures.batchView([]) });
  assert.equal(p.view(), "loading");
  assert.equal(p.$("capture-progress").hidden, false, "the progress card is not shown");
  assert.equal(p.$("capture-cancel-btn").hidden, false, "the stop control is not offered");

  finish();
  await p.flush();
  assert.equal(p.$("capture-cancel-btn").hidden, true, "the stop control outlived the pass");
  assert.equal(p.$("capture-progress").hidden, true);
});

test("the stop control sends CANCEL_CAPTURE, once, however often it is pressed", async () => {
  const { p, finish } = await panelMidCapture({
    ok: true,
    captureStatus: "ok",
    added: 1,
    batchView: fixtures.batchView([fixtures.record()]),
    scroll: { stopReason: "cancelled", rows: 1, returnedToTop: true },
  });
  await p.click("capture-cancel-btn");
  await p.click("capture-cancel-btn");
  await p.click("capture-cancel-btn");
  const cancels = p.sent.filter((m) => m.type === "CANCEL_CAPTURE");
  assert.equal(cancels.length, 1, "repeated presses must not repeat the message");
  assert.equal(p.$("capture-cancel-btn").disabled, true, "the control stays pressed-once");

  finish();
  await p.flush();
  assert.equal(p.view(), "listings-select");
});

test("a cancelled capture keeps and shows what loaded, and submits nothing", async () => {
  const { p, finish } = await panelMidCapture({
    ok: true,
    captureStatus: "ok",
    added: 2,
    collapsed: 0,
    uncertain: 0,
    batchView: fixtures.batchView([
      fixtures.record(),
      fixtures.record({ rawFullName: "Wei Zhang", _stableKey: "k2" }),
    ]),
    scroll: { stopReason: "cancelled", rows: 2, returnedToTop: true },
  });
  await p.click("capture-cancel-btn");
  finish();
  await p.flush();

  assert.equal(p.view(), "listings-select");
  assert.match(p.$("capture-feedback").textContent, /Stopped/);
  assert.match(p.$("capture-feedback").textContent, /\+2 added/);
  assert.match(p.$("capture-feedback").textContent, /read the page again/);
  assert.match(p.viewText(), /Dana Whitfield/);
  assert.match(p.viewText(), /Wei Zhang/);
  // Cancelling is not a failure and never sends anything.
  assert.ok(!p.sent.some((m) => m.type === "SAVE_INCLUDED_CONTACTS"), "a cancel must not submit");
  assert.ok(!p.sent.some((m) => m.type === "CLEAR_BATCH"), "a cancel must not clear the batch");
  assert.equal(p.$("listings-review-btn").hidden, false, "the reviewed set is still reachable");
});

test("stopping before any row loaded is reported as the operator's action", async () => {
  const { p, finish } = await panelMidCapture({
    ok: true,
    captureStatus: "empty",
    added: 0,
    pageWarnings: [],
    batchView: fixtures.batchView([]),
    scroll: { stopReason: "cancelled", rows: 0, returnedToTop: true },
  });
  await p.click("capture-cancel-btn");
  finish();
  await p.flush();
  assert.equal(p.view(), "listings-empty");
  const detail = p.$("listings-empty-detail").textContent;
  assert.match(detail, /Stopped before any rows loaded/);
  assert.ok(!/No visible prospects/.test(detail), "a cancellation must not read as an empty page");
});

test("a fresh capture after cancelling starts clean and is not still cancelled", async () => {
  const { p, finish } = await panelMidCapture({
    ok: true,
    captureStatus: "ok",
    added: 1,
    batchView: fixtures.batchView([fixtures.record()]),
    scroll: { stopReason: "cancelled", rows: 1, returnedToTop: true },
  });
  await p.click("capture-cancel-btn");
  finish();
  await p.flush();

  // Second pass: normal completion, no lingering cancelled wording.
  p.responses.CAPTURE_ACTIVE_PAGE = {
    ok: true,
    captureStatus: "ok",
    added: 1,
    collapsed: 0,
    uncertain: 0,
    batchView: fixtures.batchView([fixtures.record(), fixtures.record({ rawFullName: "Wei Zhang", _stableKey: "k2" })]),
    scroll: { stopReason: "stabilized", rows: 2, returnedToTop: true },
  };
  await p.click("capture-btn");
  assert.equal(p.view(), "listings-select");
  assert.ok(!/Stopped/.test(p.$("capture-feedback").textContent), "the cancelled state leaked forward");
  assert.equal(p.$("capture-cancel-btn").hidden, true);
});

test("progress from a cancelled pass cannot repaint the panel", async () => {
  const { p, finish } = await panelMidCapture({
    ok: true,
    captureStatus: "ok",
    added: 1,
    batchView: fixtures.batchView([fixtures.record()]),
    scroll: { stopReason: "cancelled", rows: 1, returnedToTop: true },
  });
  await p.emit({ type: "CS_SCROLL_PROGRESS", passId: 1, progress: { phase: "step", rows: 4 } });
  assert.match(p.$("capture-progress-detail").textContent, /4 rows loaded so far/);

  await p.click("capture-cancel-btn");
  const afterCancel = p.$("capture-progress-detail").textContent;
  // A late event from the pass the operator just stopped must change nothing.
  await p.emit({ type: "CS_SCROLL_PROGRESS", passId: 1, progress: { phase: "step", rows: 9 } });
  assert.equal(p.$("capture-progress-detail").textContent, afterCancel);
  assert.equal(p.$("capture-cancel-btn").disabled, true);

  finish();
  await p.flush();
  // And an event from an older pass arriving after the fact repaints nothing.
  await p.emit({ type: "CS_SCROLL_PROGRESS", passId: 1, progress: { phase: "step", rows: 99 } });
  assert.equal(p.view(), "listings-select");
  assert.ok(!/99 rows/.test(p.text()));
});

// --- the non-goals, still --------------------------------------------------------

test("the cancellation path introduces no navigation or pagination", () => {
  const sources = ["content/content-script.js", "common/scroller.js", "background/service-worker.js"].map(
    (rel) => stripComments(fs.readFileSync(path.join(SRC, rel), "utf8"))
  );
  const forbidden = [
    /location\s*=\s*/,
    /location\.assign/,
    /location\.replace/,
    /window\.open/,
    /next-?page/i,
    /paginat/i,
    /captcha/i,
  ];
  for (const src of sources) {
    for (const re of forbidden) {
      assert.ok(!re.test(src), `the capture path must not contain ${re}`);
    }
  }
  // Cancelling stops a pass. It must never become a way to move the operator.
  const worker = stripComments(fs.readFileSync(path.join(SRC, "background", "service-worker.js"), "utf8"));
  const cancelFn = worker.slice(worker.indexOf("async function cancelActiveCapture"));
  const body = cancelFn.slice(0, cancelFn.indexOf("\n}\n"));
  assert.ok(!/executeScript/.test(body), "cancelling must not inject anything");
  assert.ok(!/chrome\.tabs\.update/.test(body), "cancelling must not navigate the tab");
});

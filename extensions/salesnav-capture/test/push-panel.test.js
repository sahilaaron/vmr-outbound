"use strict";
/**
 * What the operator sees while a large save is running.
 *
 * The rule every assertion here defends: the panel REPORTS the push, it does not
 * hold it. Save returns as soon as the job is durable, the screen stays usable,
 * closing the panel changes nothing about the delivery, and reopening it shows
 * where the push actually got to — never "done" because the first chunk landed,
 * and never "nothing happened" because this panel instance did not start it.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");

const { createPanel, DEFAULT_PREFS, fixtures } = require("./panel-harness.js");
const constants = require("../src/common/constants.js");

const { SURFACES } = constants;

function pushView(overrides) {
  return Object.assign(
    {
      jobId: "job-1",
      logicalSubmissionId: "sub-1",
      status: "running",
      createdAt: "2026-08-20T09:15:04.000Z",
      updatedAt: "2026-08-20T09:15:06.000Z",
      totalContacts: 2843,
      contactsAccepted: 650,
      contactsFailed: 0,
      contactsPending: 2193,
      totalChunks: 29,
      completedChunks: 7,
      pendingChunks: 22,
      failedChunks: 0,
      retryableChunks: 0,
      campaignId: null,
      counts: { submitted: 650, created: 650 },
      results: [],
      resultsSeen: 650,
      resultsRetained: 500,
      resultsTruncated: true,
      failures: [],
      oversized: [],
      workbenchUrl: null,
      nextAttemptAt: null,
    },
    overrides || {}
  );
}

async function panelAtReview(t, extra) {
  const p = await createPanel({
    responses: Object.assign(
      {
        GET_STATE: {
          ok: true,
          prefs: DEFAULT_PREFS,
          metadata: { labels: [], note: null },
          batchView: fixtures.batchView([
            fixtures.record(),
            fixtures.record({ rawFullName: "Wei Zhang", _stableKey: "k2" }),
          ]),
        },
        DETECT_SURFACE: {
          ok: true,
          surface: SURFACES.SALESNAV_PEOPLE_RESULTS,
          url: "https://www.linkedin.com/sales/search/people",
        },
        DETECT_ACTIVE_PAGE: {
          ok: true,
          page: {
            supported: true,
            url: "https://www.linkedin.com/sales/search/people",
            visibleCount: 2,
          },
        },
        PROBE_BACKEND: { ok: true, state: "connected" },
        GET_ACCOUNT_STATE: { ok: true, account: { connected: true, accountEmail: "a@b.c" } },
        FETCH_LABELS: { ok: true, labels: [] },
        FETCH_CAMPAIGNS: { ok: true, campaigns: [] },
      },
      extra || {}
    ),
  });
  await p.flush();
  // A panel with a live progress poll keeps the event loop alive. Closing it
  // through the runner means a failing assertion cannot hang the suite.
  t.after(() => p.close());
  return p;
}

test("Save reports progress instead of a finished outcome", async (t) => {
  const p = await panelAtReview(t, {
    SAVE_INCLUDED_CONTACTS: { ok: true, push: pushView() },
    PUSH_STATE: { ok: true, push: pushView(), pushActive: true },
  });
  await p.click("listings-review-btn");
  await p.click("save-btn");
  await p.flush();

  assert.equal(p.view(), "outcome");
  assert.equal(p.$("push-card").hidden, false);
  assert.equal(p.$("save-card").hidden, true, "there is no outcome yet to paint");
  assert.match(p.$("push-title").textContent, /Saving 650 of 2843/);
  assert.match(p.$("push-detail").textContent, /7 of 29 batches delivered/);
  assert.match(p.$("push-detail").textContent, /2193 still to send/);
});

test("the screen offers a way out and says the save does not need watching", async (t) => {
  const p = await panelAtReview(t, {
    SAVE_INCLUDED_CONTACTS: { ok: true, push: pushView() },
    PUSH_STATE: { ok: true, push: pushView(), pushActive: true },
  });
  await p.click("listings-review-btn");
  await p.click("save-btn");
  await p.flush();

  assert.equal(p.actions(), "pushing", "the operator is not stuck behind a disabled button");
  assert.equal(p.$("push-back").disabled, false);
  assert.match(p.$("push-note").textContent, /close this panel/i);
  // And leaving the screen does not cancel or pause anything: the panel has no
  // message that could, and none is sent.
  await p.click("push-back");
  await p.flush();
  assert.equal(p.sent.some((m) => /CANCEL|ABORT|STOP/.test(String(m.type))), false);
});

test("reopening the panel resumes reporting an unfinished push", async (t) => {
  // The panel that started the push is gone. This is a COLD open: everything it
  // knows comes from the worker's stored job.
  const p = await panelAtReview(t, {
    GET_STATE: {
      ok: true,
      prefs: DEFAULT_PREFS,
      metadata: { labels: [], note: null },
      batchView: fixtures.batchView([fixtures.record()]),
      push: pushView({ contactsAccepted: 2100, completedChunks: 21, contactsPending: 743 }),
      pushActive: true,
    },
    PUSH_STATE: {
      ok: true,
      push: pushView({ contactsAccepted: 2100, completedChunks: 21, contactsPending: 743 }),
      pushActive: true,
    },
  });
  assert.equal(p.view(), "outcome");
  assert.match(p.$("push-title").textContent, /Saving 2100 of 2843/);
  assert.equal(p.actions(), "pushing");
});

test("a completed push hands over to the outcome, with truthful totals", async (t) => {
  const done = pushView({
    status: "completed",
    contactsAccepted: 2843,
    contactsPending: 0,
    completedChunks: 29,
    pendingChunks: 0,
    counts: { submitted: 2843, created: 2843 },
  });
  const p = await panelAtReview(t, {
    SAVE_INCLUDED_CONTACTS: { ok: true, push: done },
    PUSH_STATE: { ok: true, push: done, pushActive: false },
    GET_STATE: (msg) => {
      void msg;
      return {
        ok: true,
        prefs: DEFAULT_PREFS,
        metadata: { labels: [], note: null },
        batchView: fixtures.batchView([fixtures.record()]),
        lastResult: {
          submissionId: null,
          clientSubmissionId: "sub-1",
          alreadyReceived: false,
          counts: { submitted: 2843, created: 2843 },
          results: new Array(500).fill({ outcome: "created", captureUrl: null, contactUrl: null }),
          resultsSeen: 2843,
          resultsRetained: 500,
          resultsTruncated: true,
          workbenchUrl: null,
          submittedAt: "2026-08-20T09:15:04.000Z",
        },
        lastResultContext: { kind: "listings", url: null },
      };
    },
  });
  await p.click("listings-review-btn");
  await p.click("save-btn");
  await p.flush();
  await p.flush();

  assert.match(p.$("push-title").textContent, /2843 contacts saved/);
  // The number that matters: 2,843 processed, not 500. A bounded display list
  // must never be printed as the size of what happened.
  const text = p.viewText();
  assert.match(text, /2843 of 2843 prospects saved|2843 contacts saved/);
  assert.match(text, /All 2843 were processed/);
  assert.match(text, /500 most recent/);
});

test("a push that ends with failures says so, and offers only the safe retry", async (t) => {
  const partial = pushView({
    status: "completed_with_failures",
    contactsAccepted: 2743,
    contactsFailed: 100,
    contactsPending: 0,
    completedChunks: 28,
    pendingChunks: 0,
    failedChunks: 1,
    retryableChunks: 1,
    failures: [{ chunk: 12, contactCount: 100, code: "network_error", attempts: 5, status: "failed" }],
  });
  const p = await panelAtReview(t, {
    SAVE_INCLUDED_CONTACTS: { ok: true, push: partial },
    PUSH_STATE: { ok: true, push: partial, pushActive: false },
    RETRY_PUSH: { ok: true, push: pushView({ status: "running", contactsAccepted: 2743 }) },
  });
  await p.click("listings-review-btn");
  await p.click("save-btn");
  await p.flush();

  assert.match(p.$("push-title").textContent, /2743 of 2843 saved/);
  assert.match(p.$("push-detail").textContent, /100 in 1 failed batch/);
  const buttons = Array.from(p.$("push-actions").querySelectorAll("button")).map((b) =>
    b.textContent.trim()
  );
  assert.ok(buttons.includes("Retry what failed"));
  assert.ok(buttons.includes("Done"));
});

test("a capture larger than one save may hold is refused by number, not by bytes", async (t) => {
  const p = await panelAtReview(t, {
    SAVE_INCLUDED_CONTACTS: {
      ok: false,
      error: "push_limit_exceeded",
      limit: 5000,
      count: 5001,
      message: "One save may contain up to 5000 contacts. This capture has 5001.",
    },
  });
  await p.click("listings-review-btn");
  await p.click("save-btn");
  await p.flush();

  const text = p.viewText();
  assert.match(text, /up to 5000 contacts/);
  assert.match(text, /5001/);
  assert.match(text, /Nothing was sent/);
  // The old wording sent the operator looking for a size problem they did not
  // have. It must not be what they are told.
  assert.ok(!/payload/i.test(text));
});

test("a cancelled push says what was saved and what was not, and never claims a rollback", async (t) => {
  const cancelled = pushView({
    status: "cancelled",
    contactsAccepted: 642,
    contactsPending: 0,
    contactsCancelled: 1858,
    totalContacts: 2500,
    completedChunks: 7,
    pendingChunks: 0,
    cancelledChunks: 19,
  });
  const p = await panelAtReview(t, {
    SAVE_INCLUDED_CONTACTS: { ok: true, push: pushView({ totalContacts: 2500 }) },
    PUSH_STATE: { ok: true, push: cancelled, pushActive: false },
    CANCEL_PUSH: { ok: true, push: cancelled, accepted: 642, notSent: 1858 },
    GET_STATE: () => ({
      ok: true,
      prefs: DEFAULT_PREFS,
      metadata: { labels: [], note: null },
      batchView: fixtures.batchView([fixtures.record()]),
      lastResult: {
        submissionId: null,
        clientSubmissionId: "sub-1",
        alreadyReceived: false,
        counts: { submitted: 642, created: 642 },
        results: [],
        resultsSeen: 642,
        resultsRetained: 0,
        workbenchUrl: null,
        submittedAt: "2026-08-20T09:15:04.000Z",
        push: { status: "cancelled", contactsAccepted: 642, contactsCancelled: 1858 },
      },
      lastResultContext: { kind: "listings", url: null },
    }),
  });
  await p.click("listings-review-btn");
  await p.click("save-btn");
  await p.flush();

  // The escape hatch is on screen while the push is unfinished.
  assert.equal(p.$("push-cancel").hidden, false);
  await p.click("push-cancel");
  await p.flush();
  await p.flush();

  const text = p.viewText();
  assert.match(text, /642/);
  assert.match(text, /1858/);
  assert.match(text, /cancelled/i);
  assert.match(text, /not sent/i);
  // Both halves, and no suggestion that anything was undone.
  assert.match(text, /Nothing that had already been saved was undone|had already been saved was kept/i);
  assert.doesNotMatch(text, /2500 contacts saved/);
});

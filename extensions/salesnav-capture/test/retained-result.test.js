"use strict";
/**
 * UI-016 / #194 — the retained result survives a panel reopen, and stays where
 * it belongs.
 *
 * DAT-011 recorded S5 as partial (A9: "draft yes, result no") and diagnosed it
 * as D-8: `sidepanel.js` restores the saved outcome and renders it, then
 * `sidepanel-profile.js` runs its first `paintMode()` and switches the body to
 * the detected surface's default view. The sticky guard could not stop it,
 * because on a cold open `currentMode` is null and "no previous mode" is
 * indistinguishable from "the page changed".
 *
 * These tests drive the REAL panel and the REAL worker. The panel tests assert
 * what an operator would see after reopening; the worker tests assert the stored
 * shape those views depend on. Two of them fail on the pre-fix code — the first
 * two reopen cases — which is what makes the rest meaningful.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");

const { createPanel, DEFAULT_PREFS, fixtures } = require("./panel-harness.js");
const { createWorker } = require("./worker-harness.js");

const constants = require("../src/common/constants.js");
const { SURFACES, PROFILE_STORAGE, STORAGE } = constants;

const PROFILE_A = "https://www.linkedin.com/in/danawhitfield";
const PROFILE_B = "https://www.linkedin.com/in/weizhang";

/** A saved-submission result as `handoff.sanitizeContactSubmissionResult` shapes it. */
function savedResult(overrides) {
  return Object.assign(
    {
      submissionId: "01366e2e-0000-4000-8000-000000000001",
      clientSubmissionId: "77ae7ae0-0000-4000-8000-000000000001",
      alreadyReceived: false,
      counts: { submitted: 1, created: 1 },
      results: [
        {
          outcome: "created",
          captureUrl: "http://127.0.0.1:8000/contact-captures/submissions/01366e2e",
          contactUrl: "http://127.0.0.1:8000/contacts/44",
          reviewCandidateCount: 0,
          labelsApplied: 0,
        },
      ],
      workbenchUrl: "http://127.0.0.1:8000/contact-captures/submissions/01366e2e",
      submittedAt: "2026-07-27T10:05:00.000Z",
    },
    overrides
  );
}

const BASE = {
  GET_STATE: {
    ok: true,
    prefs: DEFAULT_PREFS,
    metadata: { labels: [], note: null },
    batchView: null,
  },
  PROFILE_GET_STATE: { ok: true, prefs: DEFAULT_PREFS, draftView: null },
  COMPANY_GET_STATE: { ok: true, draftView: null },
  FETCH_LABELS: { ok: true, labels: [] },
  PROFILE_MATCH_STATE: { ok: true, match: "none" },
  PROFILE_DETECT: {
    ok: true,
    page: { surface: SURFACES.PERSON_PROFILE, experienceEntryCount: 1 },
  },
  // The panel's live follower reads the page for itself as soon as it opens.
  PROFILE_CAPTURE: { ok: true, captureStatus: "ok", draftView: fixtures.profileDraftView() },
};

/**
 * Reopen the panel on `url` with a profile result already in storage.
 * This is precisely the A9 re-run: close, reopen, touch nothing.
 */
function reopenOnProfile(options) {
  const o = options || {};
  const draft = fixtures.profileDraftView();
  draft.profile.linkedin_profile_url = o.draftUrl || o.url;
  return createPanel({
    responses: Object.assign({}, BASE, {
      DETECT_SURFACE: { ok: true, surface: SURFACES.PERSON_PROFILE, url: o.url },
      PROFILE_GET_STATE: {
        ok: true,
        prefs: DEFAULT_PREFS,
        draftView: "draftView" in o ? o.draftView : draft,
        lastResult: "lastResult" in o ? o.lastResult : savedResult(),
        lastResultContext:
          "lastResultContext" in o ? o.lastResultContext : { kind: "profile", url: o.resultUrl || o.url },
      },
    }),
  });
}

/** The messages that reach the backend. None may appear from a restore. */
const SUBMIT_TYPES = ["SAVE_CONTACT", "SAVE_INCLUDED_CONTACTS", "COMPANY_SEND"];

function submissionsMade(panel) {
  return panel.sent.filter((m) => SUBMIT_TYPES.includes(m && m.type)).map((m) => m.type);
}

// --- the defect itself --------------------------------------------------------

test("a completed result is still on screen after the panel is reopened", async () => {
  const p = await reopenOnProfile({ url: PROFILE_A });
  await p.flush();

  // Pre-fix this was "person-review": the outcome was rendered, then replaced.
  assert.equal(p.view(), "outcome");
  assert.match(p.viewText(), /Prospect saved/);
  assert.equal(p.steps().text, "Done");
});

test("the returned workbench link comes back with the restored result (S6)", async () => {
  const p = await reopenOnProfile({ url: PROFILE_A });
  await p.flush();

  const links = Array.from(p.document.querySelectorAll("#save-actions a")).map((a) => ({
    text: a.textContent.trim(),
    href: a.getAttribute("href"),
  }));
  const workbench = links.find((l) => /capture record/i.test(l.text));
  assert.ok(workbench, "the panel's own route to the exact submission must survive a reopen");
  assert.equal(workbench.href, savedResult().workbenchUrl);
});

test("the first page detection does not paint over the restored outcome", async () => {
  const p = await reopenOnProfile({ url: PROFILE_A });
  await p.flush();

  // The detection ran — the strip is painted from it — and the body stayed put.
  assert.match(p.contextLabel(), /LinkedIn · Person profile/);
  assert.equal(p.view(), "outcome");

  // And it is genuinely held, not merely slow: further flushes change nothing.
  await p.flush(10);
  assert.equal(p.view(), "outcome");
});

test("restoring a result sends nothing to the backend", async () => {
  const p = await reopenOnProfile({ url: PROFILE_A });
  await p.flush(10);

  assert.deepEqual(submissionsMade(p), []);
  // Restoration is a read of stored state and nothing else.
  assert.ok(p.sent.some((m) => m.type === "PROFILE_GET_STATE"));

  // The panel does read the page for itself on open. Every one of those reads
  // is marked `live`, which is what stops the follower from discarding the very
  // result the operator reopened the panel to get back.
  const reads = p.sent.filter((m) => m.type === "PROFILE_CAPTURE");
  assert.ok(reads.length > 0, "the live follower reads the open page");
  assert.ok(reads.every((m) => m.live === true));
});

test("the live follower does not discard the retained result", async () => {
  // The operator reopens twice without touching anything. Both times the result
  // must be there — a fix that survives only the first reopen is not a fix.
  const w = createWorker({
    tabs: [{ id: 3, active: true, url: PROFILE_A, title: "Dana Whitfield | LinkedIn" }],
    storage: {
      [PROFILE_STORAGE.LAST_PROFILE_RESULT]: {
        v: 1,
        result: savedResult(),
        context: { kind: "profile", url: PROFILE_A },
      },
    },
    onTabMessage: () => ({
      ok: true,
      status: "ok",
      capturedAt: "2026-07-27T11:00:00.000Z",
      profile: {
        full_name: "Dana Whitfield",
        linkedin_profile_url: PROFILE_A,
        headline: null,
        displayed_location: null,
        connection_count: null,
        about_text: null,
        open_to_work: null,
        warnings: [],
      },
      experiences: [],
      missingSections: [],
      pageWarnings: [],
    }),
  });

  await w.dispatch({ type: "PROFILE_CAPTURE", live: true });
  const first = await w.dispatch({ type: "PROFILE_GET_STATE" });
  assert.ok(first.lastResult, "a live preview is not an operator recapture");

  await w.dispatch({ type: "PROFILE_CAPTURE", live: true });
  const second = await w.dispatch({ type: "PROFILE_GET_STATE" });
  assert.ok(second.lastResult, "and it still is not one the second time");
  assert.equal(second.lastResultContext.url, PROFILE_A);
});

// --- placing the result on the right page -------------------------------------

test("a result saved on profile A never appears on profile B", async () => {
  const p = await reopenOnProfile({ url: PROFILE_B, resultUrl: PROFILE_A, draftUrl: PROFILE_B });
  await p.flush();

  assert.equal(p.view(), "person-review");
  assert.doesNotMatch(p.viewText(), /Prospect saved/);
  assert.doesNotMatch(p.viewText(), /01366e2e/);
});

test("a result with no recorded page is not put back on a cold open", async () => {
  // A result stored before UI-016 cannot be placed. Not knowing where it belongs
  // is a reason to leave it, never a reason to guess.
  const p = await reopenOnProfile({ url: PROFILE_A, lastResultContext: null });
  await p.flush();

  assert.equal(p.view(), "person-review");
});

test("a listings result is restored on the listings surface", async () => {
  const rows = [fixtures.record()];
  const p = await createPanel({
    responses: Object.assign({}, BASE, {
      DETECT_SURFACE: {
        ok: true,
        surface: SURFACES.SALESNAV_PEOPLE_RESULTS,
        url: "https://www.linkedin.com/sales/search/people?page=4",
      },
      DETECT_ACTIVE_PAGE: {
        ok: true,
        page: { supported: true, url: "https://www.linkedin.com/sales/search/people", visibleCount: 4 },
      },
      GET_STATE: {
        ok: true,
        prefs: DEFAULT_PREFS,
        metadata: { labels: [], note: null },
        batchView: fixtures.batchView(rows),
        lastResult: savedResult({ counts: { submitted: 30, created: 30 } }),
        lastResultContext: { kind: "listings", url: null },
      },
    }),
  });
  await p.flush();

  // D-8 was reproduced in this exact configuration and gave "listings-select".
  assert.equal(p.view(), "outcome");
  assert.match(p.viewText(), /saved/);
  assert.deepEqual(submissionsMade(p), []);
});

test("a listings result is not restored onto a person profile", async () => {
  const p = await createPanel({
    responses: Object.assign({}, BASE, {
      DETECT_SURFACE: { ok: true, surface: SURFACES.PERSON_PROFILE, url: PROFILE_A },
      GET_STATE: {
        ok: true,
        prefs: DEFAULT_PREFS,
        metadata: { labels: [], note: null },
        batchView: null,
        lastResult: savedResult(),
        lastResultContext: { kind: "listings", url: null },
      },
    }),
  });
  await p.flush();

  assert.equal(p.view(), "person-review");
});

// --- newer unsent work wins ---------------------------------------------------

test("a newer unsent draft is not covered by an older result", async () => {
  // Reading a profile again removes the retained result in the worker (asserted
  // below), so by the time the panel asks, the newer draft is what there is.
  const p = await reopenOnProfile({ url: PROFILE_A, lastResult: null, lastResultContext: null });
  await p.flush();

  assert.equal(p.view(), "person-review");
  assert.match(p.viewText(), /Dana Whitfield/);
});

test("reading a profile again discards the result that described the old read", async () => {
  const w = createWorker({
    tabs: [{ id: 3, active: true, url: PROFILE_A, title: "Dana Whitfield | LinkedIn" }],
    storage: {
      [PROFILE_STORAGE.LAST_PROFILE_RESULT]: {
        v: 1,
        result: savedResult(),
        context: { kind: "profile", url: PROFILE_A },
      },
    },
    onTabMessage: () => ({
      ok: true,
      status: "ok",
      capturedAt: "2026-07-27T11:00:00.000Z",
      profile: {
        full_name: "Dana Whitfield",
        linkedin_profile_url: PROFILE_A,
        headline: null,
        displayed_location: null,
        connection_count: null,
        about_text: null,
        open_to_work: null,
        warnings: [],
      },
      experiences: [],
      missingSections: [],
      pageWarnings: [],
    }),
  });

  const before = await w.dispatch({ type: "PROFILE_GET_STATE" });
  assert.ok(before.lastResult, "precondition: a result is retained");

  await w.dispatch({ type: "PROFILE_CAPTURE" });

  const after = await w.dispatch({ type: "PROFILE_GET_STATE" });
  assert.equal(after.lastResult, null);
  assert.ok(after.draftView, "the newer read is the draft the operator now has");
});

test("capturing more rows discards the result that described the smaller batch", async () => {
  const w = createWorker({
    tabs: [
      { id: 7, active: true, url: "https://www.linkedin.com/sales/search/people", title: "Search" },
    ],
    storage: {
      [STORAGE.LAST_RESULT]: { v: 1, result: savedResult(), context: { kind: "listings", url: null } },
    },
    onTabMessage: () => ({
      ok: true,
      status: "ok",
      sourcePageNumber: 5,
      records: [
        {
          rawFullName: "Wei Zhang",
          firstName: "Wei",
          lastName: "Zhang",
          title: "COO",
          companyName: "Delta Manufacturing",
          location: "Seattle",
          linkedinProfileUrl: PROFILE_B,
          salesNavLeadUrl: null,
          companyLinkedInUrl: null,
          warnings: [],
          _stableKey: PROFILE_B,
        },
      ],
      pageWarnings: [],
    }),
  });

  const before = await w.dispatch({ type: "GET_STATE" });
  assert.ok(before.lastResult, "precondition: a result is retained");

  await w.dispatch({ type: "CAPTURE_ACTIVE_PAGE" });

  const after = await w.dispatch({ type: "GET_STATE" });
  assert.equal(after.lastResult, null);
  assert.equal(after.batchView.records.length, 1);
});

// --- genuine navigation still gets through ------------------------------------

test("navigating from the restored result to another profile updates the panel", async () => {
  let url = PROFILE_A;
  const draft = fixtures.profileDraftView();
  draft.profile.linkedin_profile_url = PROFILE_A;

  const p = await createPanel({
    responses: Object.assign({}, BASE, {
      // The tab the panel is following moves; every detection reports where it is.
      DETECT_SURFACE: () => ({ ok: true, surface: SURFACES.PERSON_PROFILE, url }),
      PROFILE_GET_STATE: {
        ok: true,
        prefs: DEFAULT_PREFS,
        draftView: draft,
        lastResult: savedResult(),
        lastResultContext: { kind: "profile", url: PROFILE_A },
      },
    }),
  });
  await p.flush();
  assert.equal(p.view(), "outcome");

  url = PROFILE_B;
  await p.click("refresh-mode");
  await p.flush();

  assert.equal(p.view(), "person-review");
  assert.doesNotMatch(p.viewText(), /Prospect saved/);
});

test("navigating to an unsupported page releases the restored result", async () => {
  let detected = { ok: true, surface: SURFACES.PERSON_PROFILE, url: PROFILE_A };
  const draft = fixtures.profileDraftView();
  draft.profile.linkedin_profile_url = PROFILE_A;

  const p = await createPanel({
    responses: Object.assign({}, BASE, {
      DETECT_SURFACE: () => detected,
      PROFILE_GET_STATE: {
        ok: true,
        prefs: DEFAULT_PREFS,
        draftView: draft,
        lastResult: savedResult(),
        lastResultContext: { kind: "profile", url: PROFILE_A },
      },
    }),
  });
  await p.flush();
  assert.equal(p.view(), "outcome");

  detected = {
    ok: true,
    surface: SURFACES.UNSUPPORTED,
    reason: "not_linkedin",
    url: "https://example.com/",
  };
  await p.click("refresh-mode");
  await p.flush();

  assert.equal(p.view(), "unsupported");
});

test("staying on the same profile keeps the restored result in place", async () => {
  const p = await reopenOnProfile({ url: PROFILE_A });
  await p.flush();
  assert.equal(p.view(), "outcome");

  // Re-detecting the SAME page is not navigation and must not disturb anything.
  await p.click("refresh-mode");
  await p.flush();
  assert.equal(p.view(), "outcome");
});

test("a trailing slash on the tab URL is still the same page", async () => {
  const p = await reopenOnProfile({
    url: PROFILE_A + "/",
    resultUrl: PROFILE_A,
    draftUrl: PROFILE_A,
  });
  await p.flush();
  assert.equal(p.view(), "outcome");
});

// --- what the panel is handed -------------------------------------------------

test("the worker stores the page a saved result belongs to, and returns it", async () => {
  const captured = {
    ok: true,
    status: "ok",
    capturedAt: "2026-07-27T10:00:00.000Z",
    profile: {
      full_name: "Dana Whitfield",
      linkedin_profile_url: PROFILE_A,
      headline: null,
      displayed_location: null,
      connection_count: null,
      about_text: null,
      open_to_work: null,
      warnings: [],
    },
    experiences: [],
    missingSections: [],
    pageWarnings: [],
  };
  const w = createWorker({
    tabs: [{ id: 3, active: true, url: PROFILE_A, title: "Dana Whitfield | LinkedIn" }],
    onTabMessage: () => captured,
    fetch: () =>
      Promise.resolve({
        ok: true,
        status: 201,
        text: () =>
          Promise.resolve(
            JSON.stringify({
              submission_id: "01366e2e-0000-4000-8000-000000000001",
              client_submission_id: "77ae7ae0-0000-4000-8000-000000000001",
              already_received: false,
              counts: { submitted: 1, created: 1 },
              results: [{ outcome: "created" }],
            })
          ),
      }),
  });

  await w.dispatch({ type: "PROFILE_CAPTURE" });
  const save = await w.dispatch({ type: "SAVE_CONTACT" });
  assert.equal(save.ok, true);
  assert.equal(save.resultContext.kind, "profile");
  assert.equal(save.resultContext.url, PROFILE_A);

  const stored = w.store[PROFILE_STORAGE.LAST_PROFILE_RESULT];
  assert.equal(stored.v, 1);
  assert.equal(stored.context.kind, "profile");
  assert.equal(stored.context.url, PROFILE_A);

  const state = await w.dispatch({ type: "PROFILE_GET_STATE" });
  assert.equal(state.lastResultContext.url, PROFILE_A);
  assert.equal(state.lastResult.submissionId, "01366e2e-0000-4000-8000-000000000001");
});

test("a result stored before UI-016 is returned with no page rather than a wrong one", async () => {
  const legacy = savedResult();
  const w = createWorker({
    storage: { [PROFILE_STORAGE.LAST_PROFILE_RESULT]: legacy },
  });

  const state = await w.dispatch({ type: "PROFILE_GET_STATE" });
  assert.equal(state.lastResult.submissionId, legacy.submissionId);
  assert.equal(state.lastResult.workbenchUrl, legacy.workbenchUrl);
  assert.equal(state.lastResultContext, null);
});

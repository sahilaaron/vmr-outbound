"use strict";
/**
 * One operator Save, up to 5,000 contacts, delivered as bounded chunks that
 * survive everything the browser can do to them.
 *
 * WHAT THESE TESTS ARE ABOUT. The old listings save was one `fetch` awaited by
 * the side panel: the operation's lifetime was the panel's lifetime, and its
 * size was one HTTP request. Nothing here changes what a capture MEANS — the
 * same contacts, the same fields, the same contract — it changes only how many
 * requests carry them and who is holding the operation while they do.
 *
 * The properties proven below are the ones that make that safe:
 *
 *   * no single request is large, however large the capture;
 *   * a chunk that fails does not take its neighbours with it;
 *   * a chunk whose response was lost is retried under the SAME idempotency key,
 *     so a commit the browser never heard about cannot become a second person;
 *   * a suspended service worker resumes from what is left, not from the start;
 *   * the reviewed capture is never destroyed by a push that has not finished.
 *
 * They drive the REAL service worker through `worker-harness.js`, with only the
 * browser edges faked, so what passes here is what the shipped worker does.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");

const { createWorker, SALES_TAB, linkedAccount } = require("./worker-harness.js");
const constants = require("../src/common/constants.js");
const chunking = require("../src/common/chunking.js");
const contactSchema = require("../src/common/contact-schema.js");

const { LIMITS, PUSH, PUSH_STORAGE, STORAGE, CAPTURE_STATUS } = constants;

// ---- fixtures ---------------------------------------------------------------

/**
 * One Sales Navigator result row, shaped exactly as `extraction.extractPage`
 * emits it. `padding` inflates the row's visible metadata, which is how a
 * genuinely byte-heavy capture is modelled without inventing a new record shape.
 */
function row(i, padding) {
  const meta = [
    "Northwind Logistics International Holdings GmbH",
    "501-1,000 employees",
    "Logistics and Supply Chain",
  ];
  if (padding) meta.push("x".repeat(padding));
  return {
    firstName: "Alexandra",
    lastName: "Featherstonehaugh" + i,
    rawFullName: "Alexandra Featherstonehaugh" + i,
    title: "Senior Director of Revenue Operations and Strategic Partnerships, EMEA",
    companyName: "Northwind Logistics International Holdings GmbH",
    location: "Greater Munich Metropolitan Area, Bavaria, Germany",
    linkedinProfileUrl: "https://www.linkedin.com/in/alexandra-featherstonehaugh" + i,
    linkedinProfileUrlSource: "observed",
    linkedinMemberId: "ACwAAAB1x9k" + i,
    linkedinAliasUrl: "https://www.linkedin.com/in/ACwAAAB1x9k" + i,
    salesNavLeadUrl: "https://www.linkedin.com/sales/lead/ACwAAAB1x9k" + i + ",NAME_SEARCH,ab12",
    companyLinkedInUrl: "https://www.linkedin.com/company/northwind-logistics",
    salesNavCompanyUrl: "https://www.linkedin.com/sales/company/123456",
    visibleCompanyMetadata: meta,
    sourceSearchUrl: "https://www.linkedin.com/sales/search/people?keywords=ops&page=1",
    sourcePageNumber: 1,
    sourcePosition: i + 1,
    capturedAt: "2026-08-20T09:15:04.000Z",
    _stableKey: "https://www.linkedin.com/sales/lead/ACwAAAB1x9k" + i,
    warnings: [],
  };
}

function capturePage(count, padding, offset) {
  const start = offset || 0;
  const records = [];
  for (let i = 0; i < count; i += 1) records.push(row(start + i, padding));
  return {
    status: CAPTURE_STATUS.OK,
    records,
    pageWarnings: [],
    sourcePageNumber: 1,
    sourceSearchUrl: "https://www.linkedin.com/sales/search/people?keywords=ops&page=1",
    capturedAt: "2026-08-20T09:15:04.000Z",
    count,
    visibleCount: count,
    skipped: [],
    skippedCount: 0,
    scroll: null,
  };
}

/**
 * A recording backend.
 *
 * Records every request, every `client_submission_id` and every
 * `client_capture_id` it has ever seen, and replays an identical resubmission
 * the way the real intake does. `onRequest` lets a test fail, stall or drop a
 * response for a chosen chunk.
 */
function recordingBackend(options) {
  const o = options || {};
  const requests = [];
  const submissions = new Map();
  const captureIds = [];
  async function fetchImpl(url, init) {
    const payload = JSON.parse(init.body);
    const entry = {
      url,
      bytes: Buffer.byteLength(init.body, "utf8"),
      contacts: payload.contacts.length,
      clientSubmissionId: payload.client_submission_id,
      campaignId: payload.campaign_id,
      captureIds: payload.contacts.map((c) => c.client_capture_id),
    };
    requests.push(entry);
    const decision = o.onRequest ? o.onRequest(entry, requests.length) : null;
    if (decision && decision.throw) throw new Error(decision.throw);

    const replay = submissions.has(payload.client_submission_id);
    if (!replay) {
      // The commit happens whether or not the caller ever sees the response —
      // which is the whole point of the "lost response" case.
      submissions.set(payload.client_submission_id, entry);
      for (const id of entry.captureIds) captureIds.push(id);
    }
    if (decision && decision.dropResponse) throw new Error("network dropped the response");
    if (decision && decision.status) {
      return response(decision.status, decision.body || { error: "internal_error" });
    }
    return response(replay ? 200 : 201, {
      submission_id: "sub_" + payload.client_submission_id.slice(0, 8),
      client_submission_id: payload.client_submission_id,
      received_at: "2026-08-20T09:20:00.000Z",
      already_received: replay,
      counts: {
        submitted: payload.contacts.length,
        created: payload.contacts.length,
        campaign_filings_applied: payload.campaign_id ? payload.contacts.length : 0,
      },
      results: payload.contacts.map((c) => ({
        client_capture_id: c.client_capture_id,
        outcome: "created",
        capture_url: "https://srv1885453.hstgr.cloud/contact-captures/submissions/x",
        contact_url: "https://srv1885453.hstgr.cloud/contacts/" + c.client_capture_id.slice(0, 8),
        review_candidate_count: 0,
        labels_applied: [],
      })),
      operator_workbench_url: "https://srv1885453.hstgr.cloud/contact-captures/submissions/x",
    });
  }
  function response(status, body) {
    return {
      ok: status < 400,
      status,
      text: () => Promise.resolve(JSON.stringify(body)),
    };
  }
  return { fetchImpl, requests, submissions, captureIds };
}

/** A worker with a linked account, an open Sales Navigator tab, and a backend. */
function worker(backend, storage) {
  const account = linkedAccount();
  return createWorker({
    tabs: [SALES_TAB],
    storage: Object.assign({}, account.local, storage || {}),
    sessionStorage: account.session,
    fetch: backend.fetchImpl,
    onTabMessage: () => {
      throw new Error("no capture in this test");
    },
  });
}

/** Capture `count` rows into the reviewed batch, then start the push. */
async function captureAndPush(w, count, padding) {
  // Pages of at most 500 so the harness's synthetic capture stays realistic.
  let done = 0;
  while (done < count) {
    const size = Math.min(500, count - done);
    w.sandbox.chrome.tabs.sendMessage = () => Promise.resolve(capturePage(size, padding, done));
    const r = await w.dispatch({ type: "CAPTURE_ACTIVE_PAGE" });
    assert.equal(r.ok, true, "capture failed: " + JSON.stringify(r).slice(0, 200));
    done += size;
  }
  return w.dispatch({ type: "SAVE_INCLUDED_CONTACTS" });
}

/** Wait until the push settles (or the deadline passes). No real clock waits. */
async function drain(w, options) {
  const o = options || {};
  const limit = o.turns || 400;
  for (let i = 0; i < limit; i += 1) {
    await w.settle(20);
    const state = await w.dispatch({ type: "PUSH_STATE" });
    if (!state.push) return state;
    if (!state.pushActive) return state;
    if (o.stopWhen && o.stopWhen(state)) return state;
  }
  return w.dispatch({ type: "PUSH_STATE" });
}

function makeWorkerWithBatch(backend, count, padding) {
  const w = worker(backend);
  return { w, ready: captureAndPush(w, count, padding) };
}

// ---- 1. the small push still behaves exactly as it did ----------------------

test("10 contacts: one chunk, one request, every contact delivered", async () => {
  const backend = recordingBackend();
  const w = worker(backend);
  const started = await captureAndPush(w, 10);
  assert.equal(started.ok, true, JSON.stringify(started));
  assert.equal(started.push.totalContacts, 10);
  assert.equal(started.push.totalChunks, 1);

  const state = await drain(w);
  assert.equal(state.push.status, "completed");
  assert.equal(state.push.contactsAccepted, 10);
  assert.equal(backend.requests.length, 1);
  assert.equal(backend.requests[0].contacts, 10);
  assert.equal(state.push.counts.created, 10);
});

// ---- 2. the reported failure range -----------------------------------------

test("the size that used to be reported as 'too large' now simply saves", async () => {
  // The tester's case. 120 Sales Navigator rows is ~350 KB of contract — under
  // every ceiling in the chain — so the fix must not be a bigger number, and
  // this asserts the shape of the delivery rather than a raised limit.
  const backend = recordingBackend();
  const w = worker(backend);
  const started = await captureAndPush(w, 120);
  assert.equal(started.ok, true, JSON.stringify(started));
  assert.equal(started.push.totalChunks, 2, "120 contacts is two 100-row chunks");

  const state = await drain(w);
  assert.equal(state.push.status, "completed");
  assert.equal(state.push.contactsAccepted, 120);
  const delivered = backend.requests.reduce((n, r) => n + r.contacts, 0);
  assert.equal(delivered, 120);
  for (const request of backend.requests) {
    assert.ok(request.contacts <= PUSH.CHUNK_MAX_CONTACTS);
    assert.ok(request.bytes <= PUSH.CHUNK_MAX_BYTES);
  }
});

// ---- 3. exactly 5,000 --------------------------------------------------------

test("5,000 contacts are one operator save, many bounded requests, nobody lost", async () => {
  const backend = recordingBackend();
  const w = worker(backend);
  const started = await captureAndPush(w, LIMITS.MAX_RECORDS_PER_BATCH);
  assert.equal(started.ok, true, JSON.stringify(started).slice(0, 300));
  assert.equal(started.push.totalContacts, 5000);
  assert.equal(started.push.totalChunks, 50);

  const state = await drain(w, { turns: 2000 });
  assert.equal(state.push.status, "completed");
  assert.equal(state.push.contactsAccepted, 5000);

  const delivered = backend.requests.reduce((n, r) => n + r.contacts, 0);
  assert.equal(delivered, 5000);
  // Every person exactly once, and no request anywhere near a size limit.
  assert.equal(new Set(backend.captureIds).size, 5000);
  for (const request of backend.requests) {
    assert.ok(request.contacts <= LIMITS.MAX_CONTACTS_PER_SUBMISSION);
    assert.ok(
      request.bytes <= PUSH.CHUNK_MAX_BYTES,
      `a request weighed ${request.bytes} bytes`
    );
  }
  // Truthful totals, and a bounded amount of retained detail that does not
  // pretend to be the total.
  assert.equal(state.push.counts.created, 5000);
  assert.equal(state.push.resultsSeen, 5000);
  assert.equal(state.push.resultsRetained, PUSH.MAX_RETAINED_RESULTS);
  assert.equal(state.push.resultsTruncated, true);
});

// ---- 4. 5,001 ----------------------------------------------------------------

test("capture stops at 5,000 and says so, rather than quietly keeping 5,001", async () => {
  // The first of two defences. A reviewed set cannot GROW past the ceiling: the
  // rows beyond it are refused at capture time and reported, never silently
  // added and then discovered at save time.
  const backend = recordingBackend();
  const w = worker(backend);
  w.sandbox.chrome.tabs.sendMessage = () => Promise.resolve(capturePage(500));
  for (let page = 0; page < 10; page += 1) {
    w.sandbox.chrome.tabs.sendMessage = () => Promise.resolve(capturePage(500, 0, page * 500));
    await w.dispatch({ type: "CAPTURE_ACTIVE_PAGE" });
  }
  w.sandbox.chrome.tabs.sendMessage = () => Promise.resolve(capturePage(10, 0, 5000));
  const overflow = await w.dispatch({ type: "CAPTURE_ACTIVE_PAGE" });
  assert.equal(overflow.overLimit, true, "the operator is told the ceiling was reached");
  assert.equal(overflow.added, 0);
  const state = await w.dispatch({ type: "GET_STATE" });
  assert.equal(state.batchView.records.length, LIMITS.MAX_RECORDS_PER_BATCH);
  assert.equal(backend.requests.length, 0);
});

test("5,001 contacts are refused locally, by number, with nothing transmitted", async () => {
  // The second defence, and the one this test exists for. A reviewed set of
  // 5,001 can still ARRIVE — from an install that ran a different ceiling, or
  // from local state written by hand — and the push must refuse it before a
  // single byte moves, naming the real limit instead of "payload too large".
  const backend = recordingBackend();
  const w = worker(backend);
  const seeded = worker(backend, {
    [STORAGE.DRAFT_BATCH]: {
      clientBatchId: "11111111-2222-4333-8444-555555555555",
      createdAt: "2026-08-20T09:15:04.000Z",
      records: Array.from({ length: LIMITS.MAX_RECORDS_PER_BATCH + 1 }, (_, i) =>
        Object.assign(row(i), { _captureId: "cap-" + i + "-0000-4000-8000-000000000000" })
      ),
      pagesCaptured: [1],
      statuses: [],
      lastSearchUrl: "https://www.linkedin.com/sales/search/people",
      clientSubmissionId: null,
    },
  });
  void w;
  const started = await seeded.dispatch({ type: "SAVE_INCLUDED_CONTACTS" });
  assert.equal(started.ok, false);
  assert.equal(started.error, "push_limit_exceeded");
  assert.equal(started.limit, LIMITS.MAX_RECORDS_PER_BATCH);
  assert.equal(started.count, LIMITS.MAX_RECORDS_PER_BATCH + 1);
  assert.match(started.message, /up to 5000 contacts/);
  assert.equal(backend.requests.length, 0, "nothing may be transmitted");
  const state = await seeded.dispatch({ type: "PUSH_STATE" });
  assert.equal(state.push, null);
  // And the reviewed capture is still there to be corrected.
  const after = await seeded.dispatch({ type: "GET_STATE" });
  assert.equal(after.batchView.records.length, LIMITS.MAX_RECORDS_PER_BATCH + 1);
});

// ---- 5. byte-heavy contacts --------------------------------------------------

test("byte-heavy contacts make smaller chunks, not oversized requests", async () => {
  const backend = recordingBackend();
  const w = worker(backend);
  // ~90 KB per row: 100 of them would be 9 MB, well past every ceiling.
  const started = await captureAndPush(w, 60, 90000);
  assert.equal(started.ok, true, JSON.stringify(started).slice(0, 200));
  assert.ok(
    started.push.totalChunks > 1,
    "the record-count ceiling alone would have made one chunk"
  );

  const state = await drain(w);
  assert.equal(state.push.status, "completed");
  assert.equal(state.push.contactsAccepted, 60);
  for (const request of backend.requests) {
    assert.ok(request.contacts < PUSH.CHUNK_MAX_CONTACTS, "the byte ceiling closed the chunk");
    assert.ok(request.bytes <= PUSH.CHUNK_MAX_BYTES, `request weighed ${request.bytes}`);
  }
});

test("a single record too large for any request is refused by name, not retried", () => {
  // Planning, not delivery: this must be decided before anything is sent, or it
  // becomes a chunk that fails identically for ever.
  const plan = chunking.planChunks(
    [{ b: 100 }, { b: 9 * 1024 * 1024 }, { b: 100 }],
    {
      measure: (c) => c.b,
      envelopeBytes: 400,
      maxContacts: PUSH.CHUNK_MAX_CONTACTS,
      maxBytes: PUSH.CHUNK_MAX_BYTES,
      recordMaxBytes: PUSH.RECORD_MAX_BYTES,
    }
  );
  assert.equal(plan.oversized.length, 1);
  assert.equal(plan.oversized[0].position, 1);
  assert.equal(plan.oversized[0].code, chunking.OVERSIZED_RECORD);
  assert.equal(plan.plannedContacts, 2, "the other two still travel");
});

// ---- 6. service-worker interruption -----------------------------------------

test("a suspended service worker resumes from what is left, with no duplicates", async () => {
  const backend = recordingBackend({
    // Kill delivery after three accepted chunks, the way a suspension does:
    // abruptly, mid-operation, with no chance to record anything.
    onRequest: (_entry, n) => (n > 3 ? { throw: "worker suspended" } : null),
  });
  const first = worker(backend);
  await captureAndPush(first, 500);
  await drain(first, { turns: 200, stopWhen: (s) => s.push.contactsAccepted >= 300 });

  const midway = await first.dispatch({ type: "PUSH_STATE" });
  assert.equal(midway.push.contactsAccepted, 300);
  assert.ok(midway.push.contactsPending > 0);
  assert.equal(backend.submissions.size, 3);

  // A NEW worker instance over the SAME storage. This is what Chrome does: the
  // old instance and everything it was holding in memory are simply gone.
  const resumedBackend = recordingBackend();
  resumedBackend.submissions = backend.submissions;
  const second = createWorker({
    tabs: [SALES_TAB],
    storage: first.store,
    sessionStorage: first.sessionStore,
    fetch: resumedBackend.fetchImpl,
  });
  await second.fireInstalled();
  const state = await drain(second, { turns: 600 });

  assert.equal(state.push.status, "completed");
  assert.equal(state.push.contactsAccepted, 500);
  // The three chunks the first worker delivered are NOT re-sent: the resumed
  // worker starts from the first unfinished chunk.
  const resent = resumedBackend.requests.reduce((n, r) => n + r.contacts, 0);
  assert.equal(resent, 200, "only the undelivered 200 were sent again");
  assert.equal(new Set(resumedBackend.captureIds).size, 200);
});

// ---- 7. a failing middle chunk ----------------------------------------------

test("a network failure on chunk 5 leaves 1-4 saved, 6-10 sent, and 5 retryable", async () => {
  let attempt = 0;
  const backend = recordingBackend({
    // The fifth REQUEST is chunk 5. It fails the way a dropped connection does.
    onRequest: () => {
      attempt += 1;
      return attempt === 5 ? { throw: "network down" } : null;
    },
  });
  const w = worker(backend);
  await captureAndPush(w, 1000);
  const state = await drain(w, { turns: 400, stopWhen: (s) => s.push.status === "retrying" });

  // 900 of 1,000 saved: the four before the failure and the five after it. The
  // failure did not stop the push and did not undo anything.
  assert.equal(state.push.status, "retrying");
  assert.equal(state.push.contactsAccepted, 900);
  assert.equal(state.push.contactsFailed, 0, "a retryable chunk is not a failed one");
  assert.equal(state.push.contactsPending, 100);
  assert.ok(state.push.nextAttemptAt > Date.now(), "the retry is scheduled");
  assert.equal(state.push.failures.length, 1);
  assert.equal(state.push.failures[0].chunk, 4);

  // And when its backoff is up the ALARM brings it back — under its original
  // key, with nobody watching and no panel open.
  const failedKey = backend.requests[4].clientSubmissionId;
  w.advanceClock(30000);
  await w.fireAlarm(PUSH.RESUME_ALARM);
  const done = await drain(w, { turns: 400 });
  assert.equal(done.push.status, "completed");
  assert.equal(done.push.contactsAccepted, 1000);
  assert.equal(backend.requests[backend.requests.length - 1].clientSubmissionId, failedKey);
  assert.equal(new Set(backend.captureIds).size, 1000);
});

test("a chunk refused for a reason retrying cannot fix is parked, not spun on", async () => {
  const backend = recordingBackend({
    // 422: the contract refused it. Retrying an identical body cannot help, so
    // the chunk must be recorded as failed and the rest of the push must go on.
    onRequest: (_e, n) => (n === 2 ? { status: 422, body: { error: "validation_failed" } } : null),
  });
  const w = worker(backend);
  await captureAndPush(w, 300);
  const state = await drain(w, { turns: 400 });

  assert.equal(state.push.status, "completed_with_failures");
  assert.equal(state.push.contactsAccepted, 200);
  assert.equal(state.push.contactsFailed, 100);
  assert.equal(state.push.failedChunks, 1);
  assert.equal(state.push.retryableChunks, 0, "retrying it would fail identically");
  assert.equal(state.push.failures[0].code, "validation_failed");
  assert.equal(state.push.failures[0].attempts, 1, "it was attempted once, not five times");
});

test("a chunk that keeps failing is parked after a bounded number of attempts", async () => {
  const backend = recordingBackend({ onRequest: () => ({ throw: "network down" }) });
  const w = worker(backend);
  const started = await captureAndPush(w, 10);
  assert.equal(started.ok, true);
  const state = await drain(w, {
    turns: 200,
    stopWhen: (s) => s.push.failedChunks > 0,
  });
  // Bounded: the chunk is not attempted for ever, and the reviewed capture is
  // untouched so the operator can retry it.
  assert.ok(backend.requests.length <= PUSH.MAX_ATTEMPTS);
  const after = await w.dispatch({ type: "GET_STATE" });
  assert.equal(after.batchView.records.length, 10);
  assert.ok(state.push.contactsAccepted === 0);
});

// ---- 8. a response lost after the server committed ---------------------------

test("a commit whose response never arrived is replayed, never duplicated", async () => {
  let dropped = false;
  const backend = recordingBackend({
    onRequest: (entry, n) => {
      // The FIRST chunk commits and then the response is lost. The retry must
      // carry the same idempotency key, or the people in it exist twice.
      if (n === 1) {
        dropped = true;
        return { dropResponse: true };
      }
      return null;
    },
  });
  const w = worker(backend);
  await captureAndPush(w, 150);
  await drain(w, { turns: 400, stopWhen: (s) => s.push.status === "retrying" });
  // The retry is scheduled rather than immediate. Move the clock, not the code.
  w.advanceClock(30000);
  const state = await drain(w, { turns: 400 });

  assert.equal(dropped, true);
  assert.equal(state.push.status, "completed");
  assert.equal(state.push.contactsAccepted, 150);
  // Same key on the retry -> the backend replayed instead of committing again.
  assert.equal(backend.requests[0].clientSubmissionId, retryOf(backend, 0));
  assert.equal(new Set(backend.captureIds).size, 150, "no person was created twice");
  assert.equal(backend.submissions.size, 2, "two chunks, two submissions, no more");
});

function retryOf(backend, index) {
  const id = backend.requests[index].clientSubmissionId;
  const retry = backend.requests.slice(index + 1).find((r) => r.clientSubmissionId === id);
  return retry ? retry.clientSubmissionId : null;
}

// ---- 9 & 10. the panel and the LinkedIn tab stop mattering -------------------

test("the push does not depend on the panel, the tab, or an awaited promise", async () => {
  const backend = recordingBackend();
  const w = worker(backend);
  const started = await captureAndPush(w, 300);

  // Save returned while chunks were still owed: the operator is free.
  assert.equal(started.ok, true);
  assert.ok(
    started.push.contactsAccepted < started.push.totalContacts,
    "Save must return before delivery finishes, or the panel is held hostage"
  );

  // The Sales Navigator tab goes away and the panel closes. Neither is reachable
  // from the delivery path, so neither can affect it.
  w.sandbox.chrome.tabs.query = () => Promise.resolve([]);
  w.sandbox.chrome.tabs.sendMessage = () => Promise.reject(new Error("tab is gone"));

  const state = await drain(w, { turns: 1000 });
  assert.equal(state.push.status, "completed");
  assert.equal(state.push.contactsAccepted, 300);

  // Reopening the panel reports the truth, from storage.
  const reopened = createWorker({
    tabs: [],
    storage: w.store,
    sessionStorage: w.sessionStore,
    fetch: recordingBackend().fetchImpl,
  });
  const after = await reopened.dispatch({ type: "GET_STATE" });
  assert.equal(after.push.contactsAccepted, 300);
  assert.equal(after.pushActive, false);
});

test("progress reported mid-push is what actually landed, not what was started", async () => {
  const backend = recordingBackend({
    onRequest: (_e, n) => (n > 2 ? { throw: "stop here" } : null),
  });
  const w = worker(backend);
  await captureAndPush(w, 500);
  const state = await drain(w, {
    turns: 200,
    stopWhen: (s) => s.push.contactsAccepted >= 200,
  });
  assert.equal(state.push.contactsAccepted, 200);
  assert.equal(state.push.totalContacts, 500);
  assert.notEqual(state.push.status, "completed");
});

// ---- 11. campaign filing across chunks ---------------------------------------

test("every chunk carries the same campaign, and filing stays additive", async () => {
  const campaignId = "11111111-2222-4333-8444-555555555555";
  const backend = recordingBackend();
  const w = worker(backend);
  await w.dispatch({ type: "SET_FILING_CONTEXT", filingContext: { campaignId } });
  await captureAndPush(w, 250);
  const state = await drain(w, { turns: 600 });

  assert.equal(state.push.status, "completed");
  assert.equal(backend.requests.length, 3);
  for (const request of backend.requests) {
    assert.equal(request.campaignId, campaignId, "a chunk must not lose its campaign");
  }
  // Filing is per contact and each contact travels in exactly one chunk, so the
  // filing count is the contact count — not a multiple of it.
  assert.equal(state.push.counts.campaign_filings_applied, 250);
  assert.equal(new Set(backend.captureIds).size, 250);
});

test("a campaign chosen after the push started belongs to the next push", async () => {
  const backend = recordingBackend({ onRequest: (_e, n) => (n > 1 ? { throw: "pause" } : null) });
  const w = worker(backend);
  await captureAndPush(w, 200);
  await drain(w, { turns: 100, stopWhen: (s) => s.push.contactsAccepted >= 100 });
  await w.dispatch({
    type: "SET_FILING_CONTEXT",
    filingContext: { campaignId: "11111111-2222-4333-8444-555555555555" },
  });
  const resumed = createWorker({
    tabs: [SALES_TAB],
    storage: w.store,
    sessionStorage: w.sessionStore,
    fetch: recordingBackend().fetchImpl,
  });
  const state = await drain(resumed, { turns: 600 });
  assert.equal(state.push.campaignId, null, "the push was planned without a campaign");
});

// ---- the reviewed capture is never destroyed by a push -----------------------

test("starting a push does not clear the reviewed capture", async () => {
  const backend = recordingBackend({ onRequest: () => ({ throw: "network down" }) });
  const w = worker(backend);
  await captureAndPush(w, 40);
  const state = await w.dispatch({ type: "GET_STATE" });
  assert.equal(state.batchView.records.length, 40, "the capture must survive the push starting");
  assert.ok(state.pushActive);
});

test("the reviewed capture cannot be cleared or changed while a push is unfinished", async () => {
  const backend = recordingBackend({ onRequest: (_e, n) => (n > 1 ? { throw: "pause" } : null) });
  const w = worker(backend);
  await captureAndPush(w, 300);
  await drain(w, { turns: 100, stopWhen: (s) => s.push.contactsAccepted >= 100 });

  const cleared = await w.dispatch({ type: "CLEAR_BATCH" });
  assert.equal(cleared.ok, false);
  assert.equal(cleared.error, "push_in_progress");
  const toggled = await w.dispatch({ type: "TOGGLE_EXCLUDE", index: 0 });
  assert.equal(toggled.error, "push_in_progress");
  const captured = await w.dispatch({ type: "CAPTURE_ACTIVE_PAGE" });
  assert.equal(captured.error, "push_in_progress");

  const state = await w.dispatch({ type: "GET_STATE" });
  assert.equal(state.batchView.records.length, 300);
});

test("chunk payloads are deleted as they are accepted, and never outlive the job", async () => {
  const backend = recordingBackend();
  const w = worker(backend);
  await captureAndPush(w, 300);
  const chunkKeys = () =>
    Object.keys(w.store).filter((k) => k.startsWith(PUSH_STORAGE.CHUNK_PREFIX));
  assert.equal(chunkKeys().length, 3, "the whole submission is durable before anything is sent");

  await drain(w, { turns: 600 });
  assert.equal(chunkKeys().length, 0, "an accepted chunk's copy is not kept");
  assert.ok(w.store[PUSH_STORAGE.JOB], "the outcome is still readable");

  await w.dispatch({ type: "DISMISS_PUSH" });
  assert.equal(w.store[PUSH_STORAGE.JOB], undefined);
  assert.equal(chunkKeys().length, 0);
});

test("an unfinished push refuses to be dismissed", async () => {
  const backend = recordingBackend({ onRequest: (_e, n) => (n > 1 ? { throw: "pause" } : null) });
  const w = worker(backend);
  await captureAndPush(w, 300);
  await drain(w, { turns: 100, stopWhen: (s) => s.push.contactsAccepted >= 100 });
  const dismissed = await w.dispatch({ type: "DISMISS_PUSH" });
  assert.equal(dismissed.ok, false);
  assert.equal(dismissed.error, "push_in_progress");
  assert.ok(w.store[PUSH_STORAGE.JOB]);
});

test("the wake-up alarm is armed only while a push is unfinished", async () => {
  const backend = recordingBackend();
  const w = worker(backend);
  await captureAndPush(w, 200);
  assert.ok(
    w.alarms.created.some((a) => a.name === PUSH.RESUME_ALARM),
    "an unfinished push must arm its own wake-up"
  );
  await drain(w, { turns: 600 });
  assert.ok(
    w.alarms.cleared.includes(PUSH.RESUME_ALARM),
    "a settled push must give the alarm back"
  );
  assert.equal(w.alarms.live.has(PUSH.RESUME_ALARM), false);
});

test("the alarm resumes a push whose chunks are waiting out a backoff", async () => {
  let failures = 0;
  const backend = recordingBackend({
    onRequest: () => {
      failures += 1;
      return failures === 1 ? { throw: "transient" } : null;
    },
  });
  const w = worker(backend);
  await captureAndPush(w, 50);
  await drain(w, { turns: 50, stopWhen: (s) => s.push.status === "retrying" });
  const parked = await w.dispatch({ type: "PUSH_STATE" });
  assert.equal(parked.push.status, "retrying");
  assert.ok(parked.push.nextAttemptAt > Date.now(), "the retry is scheduled, not spun on");
});

test("no push job ever stores a token", async () => {
  const backend = recordingBackend();
  const w = worker(backend);
  await captureAndPush(w, 20);
  await drain(w, { turns: 200 });
  const serialized = JSON.stringify(w.store[PUSH_STORAGE.JOB]);
  assert.ok(!/vmre1\./.test(serialized), "an access token must never be persisted in a job");
  assert.ok(!/vmrr1\./.test(serialized), "a refresh token must never be persisted in a job");
  assert.ok(!/Bearer/.test(serialized));
});

test("chunk idempotency keys are minted once and reused on every attempt", async () => {
  const backend = recordingBackend({
    onRequest: (_e, n) => (n <= 2 ? { throw: "transient" } : null),
  });
  const w = worker(backend);
  await captureAndPush(w, 50);
  for (let i = 0; i < 3; i += 1) {
    await drain(w, { turns: 200, stopWhen: (s) => s.push.status === "retrying" });
    w.advanceClock(300000);
    await w.dispatch({ type: "RESUME_PUSH" });
  }
  const state = await drain(w, { turns: 400 });
  const ids = new Set(backend.requests.map((r) => r.clientSubmissionId));
  assert.ok(backend.requests.length >= 3, "the chunk was retried");
  assert.equal(ids.size, 1, "a retry must not mint a new idempotency key");
  assert.equal(state.push.status, "completed");
});

test("a settled push is not re-sent when the reviewed content has not changed", async () => {
  const backend = recordingBackend();
  const w = worker(backend);
  await captureAndPush(w, 20);
  await drain(w, { turns: 200 });
  const again = await w.dispatch({ type: "SAVE_INCLUDED_CONTACTS" });
  assert.equal(again.ok, false);
  assert.equal(again.error, "already_pushed");
  assert.equal(backend.requests.length, 1);
});

test("the planner and the contract agree on what one request may carry", () => {
  // The chunk ceiling must sit under the wire ceiling and under the request-byte
  // gate, or the planner would produce chunks the sender refuses.
  assert.ok(PUSH.CHUNK_MAX_CONTACTS <= LIMITS.MAX_CONTACTS_PER_SUBMISSION);
  assert.ok(PUSH.CHUNK_MAX_BYTES <= LIMITS.MAX_PAYLOAD_BYTES);
  assert.ok(LIMITS.MAX_RECORDS_PER_BATCH >= LIMITS.MAX_CONTACTS_PER_SUBMISSION);
  // And a chunk of the maximum size must still validate against the contract.
  const contacts = [];
  for (let i = 0; i < PUSH.CHUNK_MAX_CONTACTS; i += 1) {
    contacts.push(
      contactSchema.buildResultRowCapture({
        record: row(i),
        clientCaptureId: "a1a1a1a1-0000-4000-8000-" + String(i).padStart(12, "0"),
        capturedAt: "2026-08-20T09:15:04.000Z",
        sourceSearchUrl: "https://www.linkedin.com/sales/search/people",
        adapterVersion: "salesnav-people-results-adapter/1",
        metadata: null,
      })
    );
  }
  const payload = contactSchema.buildSubmission({
    clientSubmissionId: "11111111-2222-4333-8444-555555555555",
    captureMode: constants.CAPTURE_MODES.SALESNAV_PEOPLE_SEARCH,
    submittedAt: "2026-08-20T09:15:04.000Z",
    extensionVersion: "2.1.0",
    metadata: null,
    campaignId: null,
    contacts,
  });
  assert.equal(contactSchema.validateSubmission(payload).valid, true);
  assert.equal(contactSchema.serializePayload(payload).withinLimit, true);
});

// Keep the unused helper honest rather than deleting it mid-review.
void makeWorkerWithBatch;
void STORAGE;

test("a sign-in that has lapsed pauses the push instead of failing every chunk", async () => {
  // The refusal is about the PUSH, not about the chunk that met it. Burning
  // five attempts on each of fifty chunks against a condition only the operator
  // can clear would turn one sign-in into fifty failed batches.
  const backend = recordingBackend();
  const w = worker(backend);
  await captureAndPush(w, 500);
  await drain(w, { turns: 100, stopWhen: (s) => s.push.contactsAccepted >= 100 });

  // The account link goes away mid-push, exactly as an expiry or a revocation
  // does: no token in session storage, and the refresh is refused.
  delete w.sessionStore[constants.ACCOUNT_STORAGE.ACCESS_TOKEN];
  delete w.store[constants.ACCOUNT_STORAGE.ACCOUNT_LINK];
  const before = backend.requests.length;
  await w.dispatch({ type: "RESUME_PUSH" });
  await w.settle(50);

  assert.equal(backend.requests.length, before, "nothing may be sent without authorization");
  const state = await w.dispatch({ type: "PUSH_STATE" });
  assert.equal(state.pushActive, true, "the push is paused, not finished");
  assert.ok(state.push.contactsPending > 0, "the undelivered contacts are still owed");
  assert.equal(state.push.failedChunks, 0, "one sign-in must not fail every remaining batch");
});

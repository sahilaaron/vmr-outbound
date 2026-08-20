"use strict";
/**
 * LP-003 — the operator must be able to stop a push that cannot finish.
 *
 * THE DEFECT THIS FILE EXISTS FOR. An unfinished push holds the reviewed set, on
 * purpose: the rows are still being delivered, so capturing, excluding, clearing
 * and dismissing are all refused while it runs. That is right while a push CAN
 * finish. When it cannot — an account link revoked mid-push, a deployment that
 * will never authorise this install again — the push stayed unfinished for ever,
 * the alarm kept waking it to be refused again, and every control the operator
 * had was blocked by a condition they could not clear. The extension was wedged.
 *
 * Cancelling is the escape, and it is deliberately NOT a rollback. Contacts the
 * backend accepted stay accepted; nothing here can reach across and un-commit
 * somebody's data, and pretending otherwise would be a lie about it. What Cancel
 * does is stop offering the rest, reclaim what will never be sent, release the
 * reviewed set, and say plainly how the two halves ended up.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");

const { createWorker, SALES_TAB } = require("./worker-harness.js");
const {
  recordingBackend,
  worker,
  captureAndPush,
  drain,
  capturePage,
} = require("./push-fixtures.js");
const constants = require("../src/common/constants.js");

const { PUSH_STORAGE, ACCOUNT_STORAGE, PUSH } = constants;

function chunkKeys(w) {
  return Object.keys(w.store).filter((k) => k.startsWith(PUSH_STORAGE.CHUNK_PREFIX));
}

/** Revoke the account link the way an expiry or an administrator does. */
function revokeAuthorization(w) {
  delete w.sessionStore[ACCOUNT_STORAGE.ACCESS_TOKEN];
  delete w.store[ACCOUNT_STORAGE.ACCOUNT_LINK];
}

// ---- revoked authorization: the wedge, and the way out ----------------------

test("a revoked account link wedges nothing once the push can be cancelled", async () => {
  const backend = recordingBackend({ onRequest: (_e, n) => (n > 2 ? { throw: "pause" } : null) });
  const w = worker(backend);
  await captureAndPush(w, 500);
  await drain(w, { turns: 400, stopWhen: (s) => s.push.status === "retrying" });
  const accepted = (await w.dispatch({ type: "PUSH_STATE" })).push.contactsAccepted;
  assert.ok(accepted >= 200);

  // The authorization goes away for good. Every resume from here is refused
  // before anything reaches the network.
  revokeAuthorization(w);
  const before = backend.requests.length;
  for (let i = 0; i < 3; i += 1) {
    w.advanceClock(300000);
    await w.fireAlarm(PUSH.RESUME_ALARM);
    await w.settle(30);
  }
  assert.equal(backend.requests.length, before, "an unauthorised push transmits nothing");
  const stuck = await w.dispatch({ type: "PUSH_STATE" });
  assert.equal(stuck.pushActive, true, "and it stays unfinished, which is the wedge");
  assert.equal(
    (await w.dispatch({ type: "CLEAR_BATCH" })).error,
    "push_in_progress",
    "the reviewed set is held while it is unfinished"
  );

  // The escape.
  const cancelled = await w.dispatch({ type: "CANCEL_PUSH" });
  assert.equal(cancelled.ok, true, JSON.stringify(cancelled).slice(0, 200));
  assert.equal(cancelled.accepted, accepted, "what was saved is reported as saved");
  assert.equal(cancelled.accepted + cancelled.notSent, 500, "every contact is accounted for");
  assert.equal(cancelled.push.status, "cancelled");

  // 1. no further requests, ever.
  w.advanceClock(300000);
  await w.fireAlarm(PUSH.RESUME_ALARM);
  await w.settle(30);
  assert.equal(backend.requests.length, before);
  // 2. the alarm is given back.
  assert.equal(w.alarms.live.has(PUSH.RESUME_ALARM), false);
  // 3. accepted counts survive; 4. unsent contacts are NOT marked saved.
  const after = await w.dispatch({ type: "PUSH_STATE" });
  assert.equal(after.push.contactsAccepted, accepted);
  assert.equal(after.push.contactsCancelled, 500 - accepted);
  assert.equal(after.pushActive, false);
  // 5. storage for chunks that will never be sent is reclaimed.
  assert.deepEqual(chunkKeys(w), []);
  // 6. the controls come back.
  const cleared = await w.dispatch({ type: "CLEAR_BATCH" });
  assert.equal(cleared.ok, true, "the operator can work again");
});

test("cancelling never claims that unsent contacts were saved", async () => {
  const backend = recordingBackend({ onRequest: (_e, n) => (n > 1 ? { throw: "pause" } : null) });
  const w = worker(backend);
  await captureAndPush(w, 400);
  await drain(w, { turns: 200, stopWhen: (s) => s.push.contactsAccepted >= 100 });
  const cancelled = await w.dispatch({ type: "CANCEL_PUSH" });

  assert.equal(cancelled.accepted, 100);
  assert.equal(cancelled.notSent, 300);
  // The retained outcome the panel restores says the same thing.
  const state = await w.dispatch({ type: "GET_STATE" });
  assert.equal(state.lastResult.push.status, "cancelled");
  assert.equal(state.lastResult.push.contactsAccepted, 100);
  assert.equal(state.lastResult.counts.created, 100, "counts describe what the backend did");
});

// ---- partial completion -----------------------------------------------------

test("cancelling mid-push keeps the accepted chunks and resubmits nothing", async () => {
  const backend = recordingBackend({ onRequest: (_e, n) => (n > 2 ? { throw: "pause" } : null) });
  const w = worker(backend);
  await captureAndPush(w, 500);
  await drain(w, { turns: 400, stopWhen: (s) => s.push.status === "retrying" });
  const sent = backend.requests.length;
  const owned = backend.captureOwner.size;

  await w.dispatch({ type: "CANCEL_PUSH" });
  await w.settle(50);

  assert.equal(backend.requests.length, sent, "cancelling sends nothing itself");
  assert.equal(backend.captureOwner.size, owned, "and un-commits nothing");
  assert.deepEqual(backend.conflicts, []);
  assert.deepEqual(chunkKeys(w), [], "pending chunk data is reclaimed");
});

test("after cancelling, a new capture can be started and saved", async () => {
  const backend = recordingBackend({ onRequest: (_e, n) => (n > 1 ? { throw: "pause" } : null) });
  const w = worker(backend);
  await captureAndPush(w, 300);
  await drain(w, { turns: 200, stopWhen: (s) => s.push.contactsAccepted >= 100 });
  await w.dispatch({ type: "CANCEL_PUSH" });

  // Capture more, then save. Only the never-transmitted people go, so the
  // backend is never offered an id it already owns.
  w.sandbox.chrome.tabs.sendMessage = () => Promise.resolve(capturePage(100, 0, 300));
  const captured = await w.dispatch({ type: "CAPTURE_ACTIVE_PAGE" });
  assert.equal(captured.ok, true, "capture works again");

  const fresh = recordingBackend();
  fresh.captureOwner = backend.captureOwner;
  w.sandbox.fetch = fresh.fetchImpl;
  const saved = await w.dispatch({ type: "SAVE_INCLUDED_CONTACTS" });
  assert.equal(saved.ok, true, JSON.stringify(saved).slice(0, 300));
  const state = await drain(w, { turns: 600 });
  assert.equal(state.push.status, "completed");
  assert.deepEqual(fresh.conflicts, [], "nothing the backend already owns is re-offered");
});

// ---- restart after cancel ----------------------------------------------------

test("a cancelled push does not come back when the worker restarts", async () => {
  const backend = recordingBackend({ onRequest: (_e, n) => (n > 1 ? { throw: "pause" } : null) });
  const w = worker(backend);
  await captureAndPush(w, 300);
  await drain(w, { turns: 200, stopWhen: (s) => s.push.contactsAccepted >= 100 });
  await w.dispatch({ type: "CANCEL_PUSH" });
  const sent = backend.requests.length;

  // Chrome discards the worker and starts a fresh one over the same storage.
  const resumedBackend = recordingBackend();
  resumedBackend.captureOwner = backend.captureOwner;
  const restarted = createWorker({
    tabs: [SALES_TAB],
    storage: w.store,
    sessionStorage: w.sessionStore,
    fetch: resumedBackend.fetchImpl,
  });
  await restarted.fireInstalled();
  await restarted.dispatch({ type: "GET_STATE" });
  await restarted.fireAlarm(PUSH.RESUME_ALARM);
  await restarted.settle(50);

  assert.equal(resumedBackend.requests.length, 0, "a cancelled push is not resumed");
  assert.equal(backend.requests.length, sent);
  const state = await restarted.dispatch({ type: "PUSH_STATE" });
  assert.equal(state.push.status, "cancelled");
  assert.equal(state.pushActive, false);
  assert.equal(restarted.alarms.live.has(PUSH.RESUME_ALARM), false);
});

// ---- what cancel is not ------------------------------------------------------

test("a settled push cannot be cancelled — there is nothing to stop", async () => {
  const backend = recordingBackend();
  const w = worker(backend);
  await captureAndPush(w, 50);
  await drain(w, { turns: 200 });
  const r = await w.dispatch({ type: "CANCEL_PUSH" });
  assert.equal(r.ok, false);
  assert.equal(r.error, "push_not_running");
  assert.equal(r.push.status, "completed");
});

test("cancelling with no push at all is refused, not invented", async () => {
  const w = worker(recordingBackend());
  const r = await w.dispatch({ type: "CANCEL_PUSH" });
  assert.equal(r.ok, false);
  assert.equal(r.error, "no_push");
});

// ---- transient failures stay recoverable ------------------------------------

test("a transient outage still recovers on its own — cancel is not the only exit", async () => {
  // The counterweight to everything above: cancelling must be available, not
  // compulsory. A network that comes back must still finish the push by itself.
  let down = true;
  const backend = recordingBackend({ onRequest: () => (down ? { throw: "network down" } : null) });
  const w = worker(backend);
  await captureAndPush(w, 200);
  await drain(w, { turns: 200, stopWhen: (s) => s.push.status === "retrying" });
  assert.equal((await w.dispatch({ type: "PUSH_STATE" })).push.contactsAccepted, 0);

  down = false;
  w.advanceClock(300000);
  await w.fireAlarm(PUSH.RESUME_ALARM);
  const state = await drain(w, { turns: 600 });
  assert.equal(state.push.status, "completed");
  assert.equal(state.push.contactsAccepted, 200);
  assert.deepEqual(backend.conflicts, []);
});

// ---- concurrent resume safety ------------------------------------------------

test("alarm, panel and startup racing each other cannot double-send a chunk", async () => {
  const backend = recordingBackend();
  const w = worker(backend);
  await captureAndPush(w, 500);
  // Every wake-up path at once, repeatedly, while the drive loop is live.
  for (let i = 0; i < 6; i += 1) {
    w.dispatch({ type: "GET_STATE" });
    w.dispatch({ type: "RESUME_PUSH" });
    w.fireAlarm(PUSH.RESUME_ALARM);
    await w.fireInstalled();
    await w.settle(10);
  }
  const state = await drain(w, { turns: 800 });

  assert.equal(state.push.status, "completed");
  assert.equal(state.push.contactsAccepted, 500);
  // One request per chunk: no wake-up path may send a chunk a second time.
  assert.equal(backend.requests.length, 5);
  assert.equal(new Set(backend.requests.map((r) => r.clientSubmissionId)).size, 5);
  assert.equal(new Set(backend.captureIds).size, 500);
  assert.deepEqual(backend.conflicts, [], "and never an id the backend already owned");
});

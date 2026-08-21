"use strict";
/**
 * LP-002 — a chunk payload is captured personal data, and it must not outlive
 * the job that could send it.
 *
 * THE DEFECT THIS FILE EXISTS FOR. Starting a new push overwrote the job pointer
 * and nothing else. The previous job's chunk keys stayed in
 * `chrome.storage.local` — unreachable by any code path, because every reader
 * went through the current job — holding the reviewed contacts of the abandoned
 * attempt. Repeated pushes accumulated them without bound.
 *
 * The ownership rule now: a `cc_push_chunk:*` key survives only while the
 * CURRENT job still has to send it. Accepted chunks have done their work,
 * cancelled chunks will never be attempted again, and a key belonging to no
 * current job is reachable by nothing. Everything else is reclaimed, by
 * enumerating storage rather than by consulting an index — an index only ever
 * finds the keys it was told about, and a key left behind by an interrupted
 * write is exactly the kind that has to be found.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");

const { createWorker, SALES_TAB, linkedAccount } = require("./worker-harness.js");
const { recordingBackend, worker, captureAndPush, drain, capturePage } = require("./push-fixtures.js");
const constants = require("../src/common/constants.js");

const { PUSH_STORAGE } = constants;

function chunkKeys(w) {
  return Object.keys(w.store).filter((k) => k.startsWith(PUSH_STORAGE.CHUNK_PREFIX));
}

/** Anything under the chunk prefix that the current job cannot reach. */
function unreachableChunkKeys(w) {
  const job = w.store[PUSH_STORAGE.JOB];
  const live = new Set(
    (job && job.chunks ? job.chunks : [])
      .filter((c) => c.status === "pending" || c.status === "failed")
      .map((c) => PUSH_STORAGE.CHUNK_PREFIX + c.clientSubmissionId)
  );
  return chunkKeys(w).filter((k) => !live.has(k));
}

async function captureMore(w, count, offset) {
  w.sandbox.chrome.tabs.sendMessage = () => Promise.resolve(capturePage(count, 0, offset));
  const r = await w.dispatch({ type: "CAPTURE_ACTIVE_PAGE" });
  assert.equal(r.ok, true, JSON.stringify(r).slice(0, 200));
  return r;
}

// ---- the lifecycle, end to end ----------------------------------------------

test("an accepted chunk's payload is gone the moment it is accepted", async () => {
  const backend = recordingBackend({ onRequest: (_e, n) => (n > 1 ? { throw: "pause" } : null) });
  const w = worker(backend);
  await captureAndPush(w, 300);
  assert.equal(chunkKeys(w).length, 3, "the whole submission is durable before anything is sent");

  await drain(w, { turns: 200, stopWhen: (s) => s.push.contactsAccepted >= 100 });
  assert.equal(chunkKeys(w).length, 2, "the delivered chunk's copy is not kept");
  assert.deepEqual(unreachableChunkKeys(w), [], "and nothing is left that no job can reach");
});

test("a completed push leaves no chunk payload at all", async () => {
  const backend = recordingBackend();
  const w = worker(backend);
  await captureAndPush(w, 300);
  await drain(w, { turns: 400 });
  assert.deepEqual(chunkKeys(w), []);
  assert.ok(w.store[PUSH_STORAGE.JOB], "the outcome is still readable");
});

// ---- the LP-002 reproduction: a replaced job -------------------------------

test("replacing a job reclaims the payloads the old job left behind", async () => {
  // The exact shape of the leak. The first push is stopped with work still
  // owed, so its chunks are on disk; then a NEW push is planned. Before the
  // sweep, the old job's keys survived the replacement with nothing able to
  // read them.
  const backend = recordingBackend({ onRequest: (_e, n) => (n > 1 ? { throw: "stop" } : null) });
  const w = worker(backend);
  await captureAndPush(w, 300);
  await drain(w, { turns: 200, stopWhen: (s) => s.push.contactsAccepted >= 100 });
  const strandedByCancel = chunkKeys(w).slice();
  assert.ok(strandedByCancel.length >= 2, "the stopped push still holds its undelivered chunks");

  await w.dispatch({ type: "CANCEL_PUSH" });
  assert.deepEqual(chunkKeys(w), [], "cancelling reclaims every chunk it will not send");

  // A second push, then a third, to prove this is a rule and not a one-off.
  const fresh = recordingBackend({ onRequest: (_e, n) => (n > 1 ? { throw: "stop" } : null) });
  w.sandbox.fetch = fresh.fetchImpl;
  await captureMore(w, 200, 300);
  await w.dispatch({ type: "SAVE_INCLUDED_CONTACTS" });
  await drain(w, { turns: 200, stopWhen: (s) => s.push.contactsAccepted >= 100 });
  await w.dispatch({ type: "CANCEL_PUSH" });
  assert.deepEqual(chunkKeys(w), []);

  await captureMore(w, 200, 500);
  await w.dispatch({ type: "SAVE_INCLUDED_CONTACTS" });
  await drain(w, { turns: 200, stopWhen: (s) => s.push.contactsAccepted >= 100 });
  await w.dispatch({ type: "CANCEL_PUSH" });
  assert.deepEqual(chunkKeys(w), [], "three cycles, nothing accumulated");
});

test("a new Save carries an undelivered chunk forward instead of orphaning it", async () => {
  // The other half of the rule: reclaiming must not eat a payload the operator
  // still needs. A chunk that failed recoverably is inherited by the next job —
  // same submission id, same storage key, no copy — and must survive the sweep.
  const backend = recordingBackend({
    onRequest: (_e, n) => (n === 2 ? { throw: "network down" } : null),
  });
  const w = worker(backend);
  await captureAndPush(w, 300);
  await drain(w, { turns: 200, stopWhen: (s) => s.push.status === "retrying" });
  const owed = chunkKeys(w).slice();
  assert.ok(owed.length >= 1);

  await captureMore(w, 100, 300).catch(() => {});
  // The push is still unfinished, so the reviewed set is held; settle it first.
  w.advanceClock(300000);
  await w.fireAlarm(constants.PUSH.RESUME_ALARM);
  await drain(w, { turns: 400 });
  assert.deepEqual(chunkKeys(w), [], "once delivered, nothing is left behind");
  assert.deepEqual(backend.conflicts, []);
});

// ---- seeded storage: what the sweep may and may not touch --------------------

test("the sweep reclaims every unreachable chunk and keeps every reachable one", async () => {
  const backend = recordingBackend({ onRequest: (_e, n) => (n > 1 ? { throw: "pause" } : null) });
  const w = worker(backend);
  await captureAndPush(w, 300);
  await drain(w, { turns: 200, stopWhen: (s) => s.push.contactsAccepted >= 100 });

  const job = w.store[PUSH_STORAGE.JOB];
  const liveKeys = job.chunks
    .filter((c) => c.status === "pending" || c.status === "failed")
    .map((c) => PUSH_STORAGE.CHUNK_PREFIX + c.clientSubmissionId);
  assert.ok(liveKeys.length >= 2);

  // Seed the four kinds of leftover the reviewer asked about.
  const seeded = {
    // 1. an old, unreachable job's chunk
    [PUSH_STORAGE.CHUNK_PREFIX + "11111111-1111-4111-8111-111111111111"]: [{ x: 1 }],
    // 2. a completed job's remnant, under a plausible-looking id
    [PUSH_STORAGE.CHUNK_PREFIX + "22222222-2222-4222-8222-222222222222"]: [{ x: 2 }],
    // 3. a malformed key that no code writes any more (the old jobId:index form)
    [PUSH_STORAGE.CHUNK_PREFIX + "some-job-id:7"]: [{ x: 3 }],
    // 4. an empty/interrupted write
    [PUSH_STORAGE.CHUNK_PREFIX]: [],
  };
  Object.assign(w.store, seeded);
  // A key that is NOT a chunk must be left completely alone.
  w.store.sn_preferences = { maxRecordsPerBatch: 5000 };
  w.store.cc_operator_metadata = { labels: ["Healthcare"], note: null };

  // Any push-lifecycle event runs the sweep; a resume is the cheapest.
  await w.dispatch({ type: "RESUME_PUSH" });
  await w.settle(30);

  for (const key of Object.keys(seeded)) {
    assert.equal(w.store[key], undefined, `${key} must be reclaimed`);
  }
  for (const key of liveKeys) {
    assert.ok(w.store[key], `${key} is still owed and must survive`);
  }
  assert.ok(w.store.sn_preferences, "the sweep touches nothing outside its prefix");
  assert.ok(w.store.cc_operator_metadata);
});

test("the sweep works without chrome.storage.local.getKeys", async () => {
  // `getKeys` arrived in Chrome 130; the manifest allows 116. The fallback reads
  // the values and throws them away, which is slower and must still be correct.
  const backend = recordingBackend({ onRequest: (_e, n) => (n > 1 ? { throw: "pause" } : null) });
  const w = worker(backend);
  delete w.sandbox.chrome.storage.local.getKeys;
  await captureAndPush(w, 200);
  await drain(w, { turns: 200, stopWhen: (s) => s.push.contactsAccepted >= 100 });
  w.store[PUSH_STORAGE.CHUNK_PREFIX + "orphan-on-an-old-browser"] = [{ x: 1 }];
  await w.dispatch({ type: "RESUME_PUSH" });
  await w.settle(30);
  assert.equal(w.store[PUSH_STORAGE.CHUNK_PREFIX + "orphan-on-an-old-browser"], undefined);
  assert.deepEqual(unreachableChunkKeys(w), []);
});

test("a worker starting up reclaims what a previous build stranded", async () => {
  // An install carrying orphans written before this rule existed. Nothing can
  // reach them, and they are captured personal data, so install and browser
  // start both sweep.
  const account = linkedAccount();
  const backend = recordingBackend();
  const w = createWorker({
    tabs: [SALES_TAB],
    storage: Object.assign({}, account.local, {
      [PUSH_STORAGE.CHUNK_PREFIX + "legacy-a"]: [{ person: "left behind" }],
      [PUSH_STORAGE.CHUNK_PREFIX + "legacy-b"]: [{ person: "also left behind" }],
    }),
    sessionStorage: account.session,
    fetch: backend.fetchImpl,
  });
  await w.fireInstalled();
  await w.settle(30);
  assert.deepEqual(chunkKeys(w), [], "an install with no job owns no chunks");
  assert.equal(backend.requests.length, 0, "and reclaiming storage sends nothing");
});

test("no unreachable capture payload survives a completed-then-replaced cycle", async () => {
  const backend = recordingBackend();
  const w = worker(backend);
  await captureAndPush(w, 150);
  await drain(w, { turns: 400 });
  await captureMore(w, 150, 150);
  await w.dispatch({ type: "SAVE_INCLUDED_CONTACTS" });
  await drain(w, { turns: 400 });
  assert.deepEqual(chunkKeys(w), []);
  assert.deepEqual(backend.conflicts, []);
  // And the storage that remains is the small stuff: a job summary and a ledger.
  const heavy = Object.keys(w.store).filter((k) => k.startsWith(PUSH_STORAGE.CHUNK_PREFIX));
  assert.deepEqual(heavy, []);
});

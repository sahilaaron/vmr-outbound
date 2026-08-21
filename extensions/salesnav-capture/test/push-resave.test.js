"use strict";
/**
 * LP-001 — saving again after the reviewed set changes.
 *
 * THE DEFECT THIS FILE EXISTS FOR. A `client_capture_id` is unique across the
 * whole capture table, not per submission: once the backend has committed one,
 * it belongs to that submission for ever, and any LATER submission carrying it
 * is refused with `client_capture_id_conflict` — a 409 that no retry can clear.
 *
 * The first implementation planned every Save from the whole included set, so
 * excluding one row after a successful push, or capturing a hundred more,
 * re-planned people the backend already held into brand-new chunks under
 * brand-new submission ids. The result was a permanent, non-retryable wedge on
 * an operation the operator had every right to perform.
 *
 * The rule now: a capture id that has ever LEFT THE BROWSER is never planned
 * into a new chunk. It is either already saved, or it belongs to a frozen chunk
 * that can only ever be replayed under its own original submission id. Only
 * captures that were never transmitted are plannable.
 *
 * These tests drive the REAL service worker against a backend double that
 * enforces the intake's own uniqueness rules (see `push-fixtures.js`).
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");

const {
  capturePage,
  recordingBackend,
  worker,
  captureAndPush,
  drain,
} = require("./push-fixtures.js");
const constants = require("../src/common/constants.js");

const { PUSH_STORAGE } = constants;

/** Capture `count` more rows into an existing reviewed set, without saving. */
async function captureMore(w, count, offset) {
  w.sandbox.chrome.tabs.sendMessage = () => Promise.resolve(capturePage(count, 0, offset));
  const r = await w.dispatch({ type: "CAPTURE_ACTIVE_PAGE" });
  assert.equal(r.ok, true, "capture failed: " + JSON.stringify(r).slice(0, 200));
  return r;
}

/** Every capture id the backend has ever committed, and who owns it. */
function owners(backend) {
  return [...backend.captureOwner.entries()];
}

function chunkKeys(w) {
  return Object.keys(w.store).filter((k) => k.startsWith(PUSH_STORAGE.CHUNK_PREFIX));
}

// ---- Case A -----------------------------------------------------------------

test("Case A: 250 accepted, exclude one, Save — nothing is resent and nothing conflicts", async () => {
  const backend = recordingBackend();
  const w = worker(backend);
  await captureAndPush(w, 250);
  const first = await drain(w, { turns: 400 });
  assert.equal(first.push.status, "completed");
  assert.equal(first.push.contactsAccepted, 250);
  const afterFirst = backend.requests.length;
  assert.equal(backend.captureOwner.size, 250);

  // The push is settled, so the reviewed set is editable again.
  await w.dispatch({ type: "DISMISS_PUSH" });
  const excluded = await w.dispatch({ type: "TOGGLE_EXCLUDE", index: 7 });
  assert.equal(excluded.ok, true, JSON.stringify(excluded).slice(0, 200));

  const again = await w.dispatch({ type: "SAVE_INCLUDED_CONTACTS" });
  await drain(w, { turns: 200 });

  // The heart of LP-001: not one already-accepted capture id may be offered
  // again, and the backend must never have had to refuse anything.
  assert.deepEqual(backend.conflicts, [], "a resave must not produce a 409");
  assert.equal(backend.requests.length, afterFirst, "nothing may be transmitted");
  assert.equal(backend.captureOwner.size, 250, "no capture id was created twice");
  // The refusal is local, truthful, and names what happened.
  assert.equal(again.ok, false);
  assert.equal(again.error, "nothing_to_send");
  assert.equal(again.alreadySaved, 249, "the excluded row is simply not offered");
});

// ---- Case B -----------------------------------------------------------------

test("Case B: 250 accepted, capture 100 new, Save — only the 100 new are sent", async () => {
  const backend = recordingBackend();
  const w = worker(backend);
  await captureAndPush(w, 250);
  await drain(w, { turns: 400 });
  const sentFirst = backend.requests.reduce((n, r) => n + r.contacts, 0);
  assert.equal(sentFirst, 250);

  await w.dispatch({ type: "DISMISS_PUSH" });
  await captureMore(w, 100, 250);

  const second = await w.dispatch({ type: "SAVE_INCLUDED_CONTACTS" });
  assert.equal(second.ok, true, JSON.stringify(second).slice(0, 300));
  assert.equal(second.push.totalContacts, 100, "the plan is the new work only");
  const state = await drain(w, { turns: 400 });

  assert.deepEqual(backend.conflicts, []);
  assert.equal(state.push.status, "completed");
  assert.equal(state.push.contactsAccepted, 100);
  const sentTotal = backend.requests.reduce((n, r) => n + r.contacts, 0);
  assert.equal(sentTotal, 350, "250 + 100, never 250 + 350");
  assert.equal(backend.captureOwner.size, 350);
  assert.equal(new Set(owners(backend).map(([id]) => id)).size, 350);
});

// ---- Case C -----------------------------------------------------------------

test("Case C: a terminally failed chunk survives a later Save, and the new work still goes", async () => {
  // Chunk 2 of 3 is refused for a reason retrying cannot fix, so it is parked.
  const backend = recordingBackend({
    onRequest: (_e, n) => (n === 2 ? { status: 422, body: { error: "validation_failed" } } : null),
  });
  const w = worker(backend);
  await captureAndPush(w, 250);
  const first = await drain(w, { turns: 400 });
  assert.equal(first.push.status, "completed_with_failures");
  assert.equal(first.push.contactsAccepted, 150);
  assert.equal(first.push.contactsFailed, 100);

  await w.dispatch({ type: "DISMISS_PUSH" });
  await captureMore(w, 100, 250);
  const second = await w.dispatch({ type: "SAVE_INCLUDED_CONTACTS" });
  assert.equal(second.ok, true, JSON.stringify(second).slice(0, 300));

  // The 100 genuinely new contacts are sent. The 100 that were already
  // transmitted once are NOT re-planned — the backend may be holding them, and
  // a new submission id for them is precisely the conflict LP-001 is about.
  assert.equal(second.push.totalContacts, 100);
  assert.equal(second.alreadySaved, 150);
  assert.equal(second.notRetried, 100, "the parked contacts are counted, not hidden");

  const state = await drain(w, { turns: 400 });
  assert.deepEqual(backend.conflicts, [], "no 409 may ever be provoked");
  assert.equal(state.push.contactsAccepted, 100);
  assert.equal(backend.captureOwner.size, 250, "150 + 100; the refused chunk committed nothing");
});

test("Case C: a RETRYABLE failure is retried under its own original submission id", async () => {
  let failures = 0;
  const backend = recordingBackend({
    onRequest: (_e, n) => {
      if (n === 2 && failures === 0) {
        failures += 1;
        return { throw: "network down" };
      }
      return null;
    },
  });
  const w = worker(backend);
  await captureAndPush(w, 250);
  await drain(w, { turns: 200, stopWhen: (s) => s.push.status === "retrying" });
  const parkedKey = backend.requests[1].clientSubmissionId;

  w.advanceClock(30000);
  await w.fireAlarm(constants.PUSH.RESUME_ALARM);
  const state = await drain(w, { turns: 400 });

  assert.equal(state.push.status, "completed");
  assert.equal(state.push.contactsAccepted, 250);
  // The retry carried the ORIGINAL submission id, so the backend either
  // committed it once or replayed it — never a second copy under a new id.
  const retried = backend.requests.filter((r) => r.clientSubmissionId === parkedKey);
  assert.ok(retried.length >= 2, "the same chunk was attempted twice");
  assert.deepEqual(backend.conflicts, []);
  assert.equal(backend.captureOwner.size, 250);
});

// ---- Case D -----------------------------------------------------------------

test("Case D: a commit whose response was lost cannot be wedged by editing the batch", async () => {
  const backend = recordingBackend({
    onRequest: (_e, n) => (n === 1 ? { dropResponse: true } : null),
  });
  const w = worker(backend);
  await captureAndPush(w, 250);
  await drain(w, { turns: 200, stopWhen: (s) => s.push.status === "retrying" });

  // The backend HAS chunk 1's people; the browser does not know it. The
  // operator meanwhile edits the reviewed set — which must not change what the
  // frozen chunk will re-send. (Later chunks kept going while chunk 1 waited
  // out its backoff, so the count is at least chunk 1's hundred.)
  assert.ok(backend.captureOwner.size >= 100);
  const blocked = await w.dispatch({ type: "TOGGLE_EXCLUDE", index: 3 });
  assert.equal(blocked.error, "push_in_progress", "an unfinished push holds the set");

  w.advanceClock(30000);
  await w.fireAlarm(constants.PUSH.RESUME_ALARM);
  const state = await drain(w, { turns: 400 });

  assert.equal(state.push.status, "completed");
  assert.equal(state.push.contactsAccepted, 250);
  assert.deepEqual(backend.conflicts, [], "the replay path must absorb this, not a 409");
  assert.equal(backend.captureOwner.size, 250);
});

// ---- the ledger itself -----------------------------------------------------

test("a capture that has left the browser is never planned into a new chunk", async () => {
  const backend = recordingBackend({ onRequest: (_e, n) => (n > 1 ? { throw: "stop" } : null) });
  const w = worker(backend);
  await captureAndPush(w, 250);
  await drain(w, { turns: 200, stopWhen: (s) => s.push.contactsAccepted >= 100 });

  const sentIds = new Set(backend.requests.flatMap((r) => r.captureIds));
  await w.dispatch({ type: "CANCEL_PUSH" });
  await captureMore(w, 50, 250);
  const next = await w.dispatch({ type: "SAVE_INCLUDED_CONTACTS" });
  assert.equal(next.ok, true, JSON.stringify(next).slice(0, 300));
  await drain(w, { turns: 400 });

  const plannedIds = new Set(
    backend.requests.slice(1).flatMap((r) => r.captureIds)
  );
  for (const id of plannedIds) {
    assert.ok(
      !sentIds.has(id) || backend.captureOwner.get(id) !== undefined,
      "a transmitted capture id was re-planned under a new submission"
    );
  }
  assert.deepEqual(backend.conflicts, []);
});

test("the delivery ledger is cleared with the reviewed set it describes", async () => {
  const backend = recordingBackend();
  const w = worker(backend);
  await captureAndPush(w, 20);
  await drain(w, { turns: 200 });
  assert.ok(w.store[PUSH_STORAGE.LEDGER], "a ledger exists while the captures do");

  await w.dispatch({ type: "DISMISS_PUSH" });
  await w.dispatch({ type: "CLEAR_BATCH" });
  assert.equal(w.store[PUSH_STORAGE.LEDGER], undefined, "clearing the capture clears its ledger");
  assert.deepEqual(chunkKeys(w), []);
});

test("cross-chunk membership is disjoint: no capture id can appear in two chunks", async () => {
  const backend = recordingBackend();
  const w = worker(backend);
  await captureAndPush(w, 450);
  await drain(w, { turns: 600 });
  const all = backend.requests.flatMap((r) => r.captureIds);
  assert.equal(all.length, 450);
  assert.equal(new Set(all).size, 450, "one person, one chunk, one capture id");
  // And the stub would have said so if not: this is the rule it enforces.
  assert.deepEqual(backend.conflicts, []);
});

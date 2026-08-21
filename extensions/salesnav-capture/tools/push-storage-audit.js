"use strict";
/**
 * Not a test — a measurement, run by hand. It lives in tools/ and not test/
 * because everything under test/ is executed by `node --test`.
 *
 *   node tools/push-storage-audit.js
 *
 * Prints what `chrome.storage.local` holds before and after a job is replaced,
 * cancelled and completed, so the "unreachable payload" claim is a number
 * rather than an assertion.
 */
const { recordingBackend, worker, captureAndPush, drain, capturePage } = require("../test/push-fixtures.js");
const constants = require("../src/common/constants.js");

const { PUSH_STORAGE } = constants;

function audit(w, label) {
  const job = w.store[PUSH_STORAGE.JOB];
  const live = new Set(
    (job && job.chunks ? job.chunks : [])
      .filter((c) => c.status === "pending" || c.status === "failed")
      .map((c) => PUSH_STORAGE.CHUNK_PREFIX + c.clientSubmissionId)
  );
  const keys = Object.keys(w.store).filter((k) => k.startsWith(PUSH_STORAGE.CHUNK_PREFIX));
  const bytes = (k) => Buffer.byteLength(JSON.stringify(w.store[k]), "utf8");
  const reachable = keys.filter((k) => live.has(k));
  const unreachable = keys.filter((k) => !live.has(k));
  const sum = (list) => list.reduce((n, k) => n + bytes(k), 0);
  console.log(
    `${label.padEnd(46)} chunks=${String(keys.length).padStart(2)}  ` +
      `reachable=${String(reachable.length).padStart(2)} (${sum(reachable)} B)  ` +
      `UNREACHABLE=${String(unreachable.length).padStart(2)} (${sum(unreachable)} B)  ` +
      `ledger=${w.store[PUSH_STORAGE.LEDGER] ? Buffer.byteLength(JSON.stringify(w.store[PUSH_STORAGE.LEDGER]), "utf8") : 0} B`
  );
}

async function main() {
  const backend = recordingBackend({ onRequest: (_e, n) => (n > 1 ? { throw: "stop" } : null) });
  const w = worker(backend);

  await captureAndPush(w, 300);
  audit(w, "1. push planned (300 contacts, 3 chunks)");
  await drain(w, { turns: 200, stopWhen: (s) => s.push.contactsAccepted >= 100 });
  audit(w, "2. one chunk accepted, two owed");

  await w.dispatch({ type: "CANCEL_PUSH" });
  audit(w, "3. after Cancel");

  w.sandbox.chrome.tabs.sendMessage = () => Promise.resolve(capturePage(200, 0, 300));
  await w.dispatch({ type: "CAPTURE_ACTIVE_PAGE" });
  const fresh = recordingBackend({ onRequest: (_e, n) => (n > 1 ? { throw: "stop" } : null) });
  fresh.captureOwner = backend.captureOwner;
  w.sandbox.fetch = fresh.fetchImpl;
  await w.dispatch({ type: "SAVE_INCLUDED_CONTACTS" });
  await drain(w, { turns: 200, stopWhen: (s) => s.push.contactsAccepted >= 100 });
  audit(w, "4. second push planned and part-delivered");

  const third = recordingBackend();
  third.captureOwner = fresh.captureOwner;
  w.sandbox.fetch = third.fetchImpl;
  w.advanceClock(300000);
  await w.fireAlarm(constants.PUSH.RESUME_ALARM);
  await drain(w, { turns: 600 });
  audit(w, "5. second push completed");
  console.log("conflicts provoked:", backend.conflicts.length + fresh.conflicts.length + third.conflicts.length);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});

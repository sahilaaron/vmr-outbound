"use strict";
/**
 * CROSS-FEATURE INTEGRATION — unlinked-company retention x durable large push.
 *
 * Two changes land together on this branch, and each already has its own suite:
 *
 *   unlinked-company.test.js   a person is captured whether or not their
 *                              employer has a page to link to, and the company
 *                              fields stay null rather than being invented;
 *   large-push / push-resave / one logical Save of up to 5,000 contacts is
 *   push-storage / push-cancel delivered as bounded background chunks that
 *                              survive a worker restart, never re-offer a
 *                              capture the backend already owns, and can be
 *                              cancelled.
 *
 * Neither suite can fail the way the two features fail TOGETHER. The push
 * suites drive one homogeneous fixture row that always has a linked company, so
 * they never carry a null company field through chunk planning, durable chunk
 * storage, a restart, a cancel, or the export. The extraction suite proves the
 * null is produced, never that it survives the journey. In the gap between them
 * a nullable field can be defaulted, borrowed from the row next to it, or lost
 * by a serialisation step, and both files would still pass.
 *
 * So every push here carries all four company shapes at once, interleaved:
 *
 *   linked      a Sales Navigator company page (name + salesNavCompanyUrl)
 *   plaintext   an employer shown as text, with no page to link to (name only)
 *   nocompany   an identifiable person with no readable employer (both null)
 *   companyurl  a linkedin.com/company page (name + companyLinkedInUrl)
 *
 * The company evidence is not invented here. Each archetype is taken from the
 * REAL extractor running over the REAL committed fixtures, then cloned to
 * volume with fresh person identities and the company fields untouched.
 * `assertCompanyTruthful` re-checks, for every contact that reaches the wire or
 * the export, that it carries exactly what its own archetype had — so both a
 * fabricated value and a value borrowed from a neighbouring row fail.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");

global.self = global;
const ex = require("../src/common/extraction.js");
const constants = require("../src/common/constants.js");
const { loadFixtureDoc, SUPPORTED_URL } = require("./helpers.js");
const { createWorker, SALES_TAB } = require("./worker-harness.js");
const { recordingBackend, worker, drain } = require("./push-fixtures.js");

const { CAPTURE_STATUS, LIMITS, PUSH, PUSH_STORAGE } = constants;

// ---- archetype templates, straight out of the extractor ---------------------

function extractFixture(name) {
  const doc = loadFixtureDoc(name, SUPPORTED_URL);
  return ex.extractPage(doc, {
    sourceSearchUrl: SUPPORTED_URL,
    capturedAt: "2026-08-20T00:00:00.000Z",
  });
}

function pick(result, fullName) {
  const rec = result.records.find((r) => r.rawFullName === fullName);
  assert.ok(rec, "fixture row not found: " + fullName);
  return rec;
}

const unlinked = extractFixture("results-unlinked-company.html");
const connective = extractFixture("results-company-connective-only.html");

// A. a company with a Sales Navigator company page (linked enrichment present)
const T_LINKED = pick(unlinked, "Jane Doe");
// B. a company shown as PLAIN TEXT: a name, and no page to link to
const T_PLAIN = pick(unlinked, "Lena Fischer");
// C. an identifiable person with NO company information at all
const T_NONE = pick(connective, "Rosa Iglesias");
// D. a company with a linkedin.com/company page (the other linked shape)
const T_COMPANY_URL = Object.assign({}, T_LINKED, {
  companyLinkedInUrl: "https://www.linkedin.com/company/acme-corporation",
});

const ARCHETYPES = [T_LINKED, T_PLAIN, T_NONE, T_COMPANY_URL];
const ARCHETYPE_NAMES = ["linked", "plaintext", "nocompany", "companyurl"];

// What each archetype is ALLOWED to put on the wire. Anything else is either a
// fabrication or contamination from a neighbouring row.
const EXPECTED = ARCHETYPES.map((t) => ({
  company_name: t.companyName === undefined ? null : t.companyName,
  company_linkedin_url: t.companyLinkedInUrl === undefined ? null : t.companyLinkedInUrl,
}));

/** One reviewed row: an archetype's company fields, a brand-new person. */
function clone(i) {
  const k = i % ARCHETYPES.length;
  const t = ARCHETYPES[k];
  const tag = ARCHETYPE_NAMES[k] + "-" + i;
  const rec = Object.assign({}, t, {
    firstName: "Person",
    lastName: tag,
    rawFullName: "Person " + tag,
    linkedinProfileUrl: "https://www.linkedin.com/in/person-" + tag,
    salesNavLeadUrl: "https://www.linkedin.com/sales/lead/ACw" + i + ",NAME_SEARCH,ab12",
    linkedinMemberId: "ACw" + i,
    linkedinAliasUrl: "https://www.linkedin.com/in/ACw" + i,
    sourcePosition: i + 1,
    _stableKey: "https://www.linkedin.com/sales/lead/ACw" + i,
    warnings: (t.warnings || []).slice(),
  });
  delete rec._selectorsUsed;
  return rec;
}

function mixedPage(count, offset) {
  const start = offset || 0;
  const records = [];
  for (let i = 0; i < count; i += 1) records.push(clone(start + i));
  return {
    status: CAPTURE_STATUS.OK,
    records,
    pageWarnings: [],
    sourcePageNumber: 1,
    sourceSearchUrl: SUPPORTED_URL,
    capturedAt: "2026-08-20T00:00:00.000Z",
    count,
    visibleCount: count,
    scroll: null,
  };
}

async function captureMixed(w, count, offset) {
  let done = 0;
  const start = offset || 0;
  while (done < count) {
    const size = Math.min(500, count - done);
    w.sandbox.chrome.tabs.sendMessage = () => Promise.resolve(mixedPage(size, start + done));
    const r = await w.dispatch({ type: "CAPTURE_ACTIVE_PAGE" });
    assert.equal(r.ok, true, "capture failed: " + JSON.stringify(r).slice(0, 200));
    done += size;
  }
}

/** Minimal RFC-4180 field split, enough for the export's own quoting rules. */
function splitCsvLine(line) {
  const out = [];
  let cur = "";
  let quoted = false;
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (quoted) {
      if (ch === '"' && line[i + 1] === '"') {
        cur += '"';
        i += 1;
      } else if (ch === '"') {
        quoted = false;
      } else {
        cur += ch;
      }
    } else if (ch === '"') {
      quoted = true;
    } else if (ch === ",") {
      out.push(cur);
      cur = "";
    } else {
      cur += ch;
    }
  }
  out.push(cur);
  return out;
}

function archetypeOf(fullName) {
  const m = /^Person ([a-z]+)-/.exec(fullName || "");
  return m ? ARCHETYPE_NAMES.indexOf(m[1]) : -1;
}

/**
 * Assert every contact the backend received carries exactly the company fields
 * its own archetype had - nothing invented, nothing borrowed from a neighbour.
 */
function assertCompanyTruthful(bodies) {
  let checked = 0;
  for (const payload of bodies) {
    for (const c of payload.contacts) {
      const k = archetypeOf(c.person.full_name);
      assert.ok(k >= 0, "unrecognised row on the wire: " + c.person.full_name);
      const hint = c.current_employment_hint;
      assert.equal(
        hint.company_name,
        EXPECTED[k].company_name,
        "company_name contaminated for " + c.person.full_name
      );
      assert.equal(
        hint.company_linkedin_url,
        EXPECTED[k].company_linkedin_url,
        "company_linkedin_url fabricated/contaminated for " + c.person.full_name
      );
      // A URL may never exist without a name behind it.
      if (hint.company_linkedin_url !== null) {
        assert.ok(hint.company_name, "a company URL with no company name");
      }
      checked += 1;
    }
  }
  return checked;
}

/** Record the exact request bodies, since the shared double keeps only counts. */
function recordingBackendWithBodies(options) {
  const b = recordingBackend(options);
  const bodies = [];
  const inner = b.fetchImpl;
  b.bodies = bodies;
  b.fetchImpl = (url, init) => {
    bodies.push(JSON.parse(init.body));
    return inner(url, init);
  };
  return b;
}

// =============================================================================
// A. Mixed-company large push
// =============================================================================

test("A. a large mixed-company push delivers every eligible contact, truthfully", async () => {
  const backend = recordingBackendWithBodies();
  const w = worker(backend);
  await captureMixed(w, 2000);

  const state0 = await w.dispatch({ type: "GET_STATE" });
  assert.equal(state0.batchView.summary.total, 2000, "all mixed rows were retained");
  assert.equal(state0.batchView.summary.included, 2000);

  const started = await w.dispatch({ type: "SAVE_INCLUDED_CONTACTS" });
  assert.equal(started.ok, true, JSON.stringify(started).slice(0, 300));
  const final = await drain(w, { turns: 2000 });

  assert.equal(final.push.status, "completed", JSON.stringify(final.push));
  assert.equal(final.push.contactsAccepted, 2000, "every eligible contact accounted for");

  // no duplicate client_capture_id, anywhere
  assert.equal(backend.captureIds.length, 2000);
  assert.equal(new Set(backend.captureIds).size, 2000, "no duplicate client_capture_id");
  assert.equal(backend.conflicts.length, 0, "zero conflicts");

  // requests remain bounded
  for (const r of backend.requests) {
    assert.ok(r.contacts <= PUSH.CHUNK_MAX_CONTACTS, "chunk over the contact ceiling");
    assert.ok(r.contacts <= LIMITS.MAX_CONTACTS_PER_SUBMISSION, "over the wire ceiling");
    assert.ok(r.bytes <= PUSH.CHUNK_MAX_BYTES, "chunk over the byte ceiling");
  }
  assert.equal(backend.requests.length, 20, "2,000 contacts in 100-contact chunks");

  // accepted contacts are not resent
  const sent = backend.requests.reduce((n, r) => n + r.contacts, 0);
  assert.equal(sent, 2000, "nothing was transmitted twice");

  // company truth
  const checked = assertCompanyTruthful(backend.bodies);
  assert.equal(checked, 2000);

  // and every archetype genuinely travelled
  const seen = new Set(
    backend.bodies.flatMap((p) => p.contacts.map((c) => archetypeOf(c.person.full_name)))
  );
  assert.deepEqual([...seen].sort(), [0, 1, 2, 3]);
});

test("A2. the full 5,000-contact ceiling holds with mixed company evidence", async () => {
  const backend = recordingBackendWithBodies();
  const w = worker(backend);
  await captureMixed(w, LIMITS.MAX_RECORDS_PER_BATCH);

  const state0 = await w.dispatch({ type: "GET_STATE" });
  assert.equal(state0.batchView.summary.total, 5000, "the whole ceiling is retained");

  await w.dispatch({ type: "SAVE_INCLUDED_CONTACTS" });
  const final = await drain(w, { turns: 4000 });
  assert.equal(final.push.status, "completed", JSON.stringify(final.push).slice(0, 300));
  assert.equal(final.push.contactsAccepted, 5000);
  assert.equal(new Set(backend.captureIds).size, 5000, "no duplicate client_capture_id");
  assert.equal(backend.conflicts.length, 0);
  assert.equal(backend.requests.length, 50, "50 bounded requests, not one 5,000-row body");
  for (const r of backend.requests) {
    assert.ok(r.contacts <= PUSH.CHUNK_MAX_CONTACTS);
    assert.ok(r.bytes <= PUSH.CHUNK_MAX_BYTES);
  }
  assert.equal(assertCompanyTruthful(backend.bodies), 5000);
});

// =============================================================================
// B. Save-more-after-success
// =============================================================================

test("B. saving 500 new contacts after 2,000 succeeded transmits only the 500", async () => {
  const backend = recordingBackendWithBodies();
  const w = worker(backend);
  await captureMixed(w, 2000);
  await w.dispatch({ type: "SAVE_INCLUDED_CONTACTS" });
  const first = await drain(w, { turns: 2000 });
  assert.equal(first.push.contactsAccepted, 2000);
  const afterFirst = backend.requests.length;

  await w.dispatch({ type: "DISMISS_PUSH" });
  // 500 genuinely new people, then Save again over the whole reviewed set.
  await captureMixed(w, 500, 2000);
  const state = await w.dispatch({ type: "GET_STATE" });
  assert.equal(state.batchView.summary.total, 2500, "the reviewed set holds all 2,500");

  await w.dispatch({ type: "SAVE_INCLUDED_CONTACTS" });
  const second = await drain(w, { turns: 2000 });
  assert.equal(second.push.status, "completed", JSON.stringify(second.push));

  const resent = backend.requests.slice(afterFirst).reduce((n, r) => n + r.contacts, 0);
  assert.equal(resent, 500, "only the 500 new contacts transmitted");
  assert.equal(backend.captureIds.length, 2500);
  assert.equal(new Set(backend.captureIds).size, 2500);
  assert.equal(
    backend.conflicts.filter((c) => c.kind === "client_capture_id_conflict").length,
    0,
    "zero client_capture_id_conflicts"
  );
  assertCompanyTruthful(backend.bodies);
});

// =============================================================================
// C. Mixed-company + service-worker restart
// =============================================================================

test("C. a mixed push survives a service-worker restart with no loss and no duplicates", async () => {
  const backend = recordingBackendWithBodies({
    onRequest: (_e, n) => (n > 5 ? { throw: "worker suspended" } : null),
  });
  const first = worker(backend);
  await captureMixed(first, 1200);
  await first.dispatch({ type: "SAVE_INCLUDED_CONTACTS" });
  await drain(first, { turns: 600, stopWhen: (s) => s.push.contactsAccepted >= 500 });

  const midway = await first.dispatch({ type: "PUSH_STATE" });
  assert.equal(midway.push.contactsAccepted, 500);
  assert.ok(midway.push.contactsPending > 0);

  // The panel is gone and Chrome discarded the worker: a NEW instance over the
  // SAME storage, and a backend that still owns the ids already committed.
  const resumedBackend = recordingBackendWithBodies();
  resumedBackend.submissions = backend.submissions;
  for (const [id, owner] of backend.captureOwner) resumedBackend.captureOwner.set(id, owner);
  const snapshot = Object.assign({}, first.store);
  const snapshotSession = Object.assign({}, first.sessionStore);
  // The suspended instance really is gone: its in-flight drive loop must not
  // keep running beside the resumed one, or the two log the same job id and
  // race over storage that only one of them still owns.
  first.sandbox.chrome.storage.local.get = () => new Promise(() => {});
  first.sandbox.fetch = () => new Promise(() => {});
  const second = createWorker({
    tabs: [SALES_TAB],
    storage: snapshot,
    sessionStorage: snapshotSession,
    fetch: resumedBackend.fetchImpl,
  });
  await second.fireInstalled();
  let state = await drain(second, { turns: 2000 });
  // A chunk the suspended worker had already failed once comes back in backoff.
  // Chrome's periodic alarm is what wakes it; the clock is what makes it due.
  for (let i = 0; i < 8 && state.pushActive; i += 1) {
    second.advanceClock(300000);
    await second.fireAlarm(PUSH.RESUME_ALARM);
    state = await drain(second, { turns: 2000 });
  }

  assert.equal(state.push.status, "completed", JSON.stringify(state.push));
  assert.equal(state.push.contactsAccepted, 1200, "no capture loss");
  const resent = resumedBackend.requests.reduce((n, r) => n + r.contacts, 0);
  assert.equal(resent, 700, "only the undelivered 700 were sent again");
  assert.equal(
    resumedBackend.conflicts.filter((c) => c.kind === "client_capture_id_conflict").length,
    0,
    "the ledger stopped the accepted 500 being re-offered"
  );
  const allIds = backend.captureIds.concat(resumedBackend.captureIds);
  assert.equal(new Set(allIds).size, allIds.length, "no duplicates across the restart");
  assertCompanyTruthful(backend.bodies.concat(resumedBackend.bodies));
});

// =============================================================================
// D. Cancel
// =============================================================================

test("D. cancelling a mixed push keeps what was accepted and reclaims the rest", async () => {
  const backend = recordingBackendWithBodies({
    onRequest: (_e, n) => (n > 4 ? { throw: "backend unreachable" } : null),
  });
  const w = worker(backend);
  await captureMixed(w, 1000);
  await w.dispatch({ type: "SAVE_INCLUDED_CONTACTS" });
  await drain(w, { turns: 600, stopWhen: (s) => s.push.contactsAccepted >= 400 });

  const before = await w.dispatch({ type: "PUSH_STATE" });
  assert.equal(before.push.contactsAccepted, 400);
  const chunkKeysBefore = Object.keys(w.store).filter((k) =>
    k.startsWith(PUSH_STORAGE.CHUNK_PREFIX)
  );
  assert.ok(chunkKeysBefore.length > 0, "pending chunks are on disk while the push runs");

  const cancelled = await w.dispatch({ type: "CANCEL_PUSH" });
  assert.equal(cancelled.ok, true, JSON.stringify(cancelled).slice(0, 300));
  await w.settle(50);

  const after = await w.dispatch({ type: "PUSH_STATE" });
  assert.equal(after.push.status, "cancelled", JSON.stringify(after.push));
  assert.equal(after.push.contactsAccepted, 400, "accepted contacts remain accepted");
  assert.ok(after.push.contactsCancelled >= 600, "the rest truthfully remain unsaved");

  const chunkKeysAfter = Object.keys(w.store).filter((k) =>
    k.startsWith(PUSH_STORAGE.CHUNK_PREFIX)
  );
  assert.equal(chunkKeysAfter.length, 0, "pending chunk storage reclaimed");

  // the extension is usable again: a fresh capture is accepted
  await w.dispatch({ type: "DISMISS_PUSH" });
  await captureMixed(w, 10, 5000);
  const usable = await w.dispatch({ type: "GET_STATE" });
  assert.ok(usable.batchView.summary.total > 0, "the extension is usable again");
  assertCompanyTruthful(backend.bodies);
});

// =============================================================================
// E. Export
// =============================================================================

test("E. exporting a mixed reviewed set represents nullable company fields honestly", async () => {
  const backend = recordingBackendWithBodies();
  const w = worker(backend);
  await captureMixed(w, 40);

  const csv = await w.dispatch({ type: "EXPORT_CAPTURED_CONTACTS", format: "csv" });
  assert.equal(csv.ok, true, JSON.stringify(csv).slice(0, 300));
  const lines = csv.text.trim().split(/\r?\n/);
  assert.equal(lines.length, 41, "header plus every reviewed row");
  const header = lines[0].split(",");
  const nameCol = header.indexOf("company_name");
  const urlCol = header.indexOf("company_linkedin_url");
  assert.ok(nameCol >= 0 && urlCol >= 0);

  const json = await w.dispatch({ type: "EXPORT_CAPTURED_CONTACTS", format: "json" });
  assert.equal(json.ok, true);
  const parsed = JSON.parse(json.text);
  const rows = parsed.contacts;
  assert.ok(Array.isArray(rows), "the JSON export carries the contact-first payload");
  assert.equal(rows.length, 40);
  let nulls = 0;
  for (const r of rows) {
    const k = archetypeOf(r.person.full_name);
    assert.ok(k >= 0);
    const hint = r.current_employment_hint;
    assert.equal(hint.company_name, EXPECTED[k].company_name, "export invented a company name");
    assert.equal(
      hint.company_linkedin_url,
      EXPECTED[k].company_linkedin_url,
      "export invented a company URL"
    );
    if (hint.company_name === null) nulls += 1;
  }
  assert.ok(nulls > 0, "the no-company rows really are in the export");

  // The CSV must agree, cell for cell, with what the JSON says.
  let csvNulls = 0;
  for (const line of lines.slice(1)) {
    const cells = splitCsvLine(line);
    const k = archetypeOf(cells[header.indexOf("raw_full_name")]);
    assert.ok(k >= 0, "unrecognised CSV row");
    const expectedName = EXPECTED[k].company_name === null ? "" : EXPECTED[k].company_name;
    const expectedUrl =
      EXPECTED[k].company_linkedin_url === null ? "" : EXPECTED[k].company_linkedin_url;
    assert.equal(cells[nameCol], expectedName, "CSV company_name is not what was captured");
    assert.equal(cells[urlCol], expectedUrl, "CSV company_linkedin_url is not what was captured");
    if (expectedName === "") csvNulls += 1;
  }
  assert.equal(csvNulls, nulls, "CSV and JSON disagree about the no-company rows");

  // the export does not mutate the reviewed batch
  const after = await w.dispatch({ type: "GET_STATE" });
  assert.equal(after.batchView.summary.total, 40);
  assert.equal(after.batchView.summary.excluded, 0);
  const csv2 = await w.dispatch({ type: "EXPORT_CAPTURED_CONTACTS", format: "csv" });
  assert.equal(csv2.text, csv.text, "a second export is byte-identical");
});

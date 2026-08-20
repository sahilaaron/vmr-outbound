"use strict";
/**
 * The operator's own copy of what they captured.
 *
 * The JSON/CSV export was removed in #280 on the reasoning that a reviewed
 * contact is saved into VMR Outbound or it is not saved at all. That is true of
 * ACQUISITION — nothing here creates a Contact, files a Campaign, or is offered
 * as a substitute for saving — and it says nothing about the operator's own
 * backup of a capture that may have taken hours to assemble.
 *
 * So the export is back, with these properties, each of which is asserted below:
 *
 *   * it is LOCAL — the backend is never contacted, so an unreachable app does
 *     not cost the operator their file;
 *   * it is EXPLICIT — a click, never a side effect of anything else;
 *   * it does NOT CONSUME the capture — the reviewed rows and the save that
 *     would deliver them are exactly as they were afterwards;
 *   * it is the SAME FORMAT it was, so anything written against the old file
 *     still reads a new one;
 *   * it respects INCLUSION — an excluded row is not part of this capture, and
 *     that decision means the same thing for the file as for the save.
 *
 * The worker tests drive the REAL service worker; the panel tests drive the REAL
 * panel and read the bytes it hands to Chrome.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");

const { createWorker, SALES_TAB, linkedAccount } = require("./worker-harness.js");
const { createPanel, DEFAULT_PREFS, fixtures } = require("./panel-harness.js");
const constants = require("../src/common/constants.js");
const schema = require("../src/common/schema.js");

const { CAPTURE_STATUS, LIMITS, SURFACES, STORAGE } = constants;

// ---- fixtures ---------------------------------------------------------------

function row(i) {
  return {
    firstName: "Dana",
    lastName: "Whitfield" + i,
    rawFullName: "Dana Whitfield" + i,
    title: "Head of Operations",
    companyName: "Northwind Logistics",
    location: "Greater Chicago Area",
    linkedinProfileUrl: "https://www.linkedin.com/in/danawhitfield" + i,
    linkedinProfileUrlSource: "observed",
    linkedinMemberId: "ACwAAAB1x9k" + i,
    linkedinAliasUrl: "https://www.linkedin.com/in/ACwAAAB1x9k" + i,
    salesNavLeadUrl: "https://www.linkedin.com/sales/lead/ACwAAAB1x9k" + i,
    companyLinkedInUrl: "https://www.linkedin.com/company/northwind",
    salesNavCompanyUrl: "https://www.linkedin.com/sales/company/1234",
    visibleCompanyMetadata: ["Northwind Logistics", "501-1,000 employees"],
    sourceSearchUrl: "https://www.linkedin.com/sales/search/people?keywords=ops",
    sourcePageNumber: 1,
    sourcePosition: i + 1,
    capturedAt: "2026-08-20T09:15:04.000Z",
    _stableKey: "https://www.linkedin.com/sales/lead/ACwAAAB1x9k" + i,
    warnings: [],
  };
}

function capturePage(count, offset) {
  const start = offset || 0;
  const records = [];
  for (let i = 0; i < count; i += 1) records.push(row(start + i));
  return {
    status: CAPTURE_STATUS.OK,
    records,
    pageWarnings: [],
    sourcePageNumber: 1,
    sourceSearchUrl: "https://www.linkedin.com/sales/search/people?keywords=ops",
    capturedAt: "2026-08-20T09:15:04.000Z",
    count,
    visibleCount: count,
    skipped: [],
    skippedCount: 0,
    scroll: null,
  };
}

/**
 * A worker whose backend REFUSES EVERYTHING.
 *
 * Deliberate: every export test runs against an app that cannot be reached, so
 * "the export works offline" is a property of the wiring rather than a claim.
 */
function offlineWorker(storage) {
  const account = linkedAccount();
  const calls = [];
  const w = createWorker({
    tabs: [SALES_TAB],
    storage: Object.assign({}, account.local, storage || {}),
    sessionStorage: account.session,
    fetch: (url) => {
      calls.push(url);
      return Promise.reject(new Error("backend unreachable"));
    },
  });
  w.networkCalls = calls;
  return w;
}

async function captured(w, count) {
  let done = 0;
  while (done < count) {
    const size = Math.min(500, count - done);
    w.sandbox.chrome.tabs.sendMessage = () => Promise.resolve(capturePage(size, done));
    const r = await w.dispatch({ type: "CAPTURE_ACTIVE_PAGE" });
    assert.equal(r.ok, true);
    done += size;
  }
  return w;
}

function parseCsv(text) {
  const lines = text.split("\r\n");
  return { header: lines[0].split(","), rows: lines.slice(1) };
}

// ---- 12. the file is built from the reviewed capture ------------------------

test("the export is built from the reviewed capture, in both formats", async () => {
  const w = offlineWorker();
  await captured(w, 3);

  const csv = await w.dispatch({ type: "EXPORT_CAPTURED_CONTACTS", format: "csv" });
  assert.equal(csv.ok, true);
  assert.equal(csv.records, 3);
  assert.equal(csv.mime, "text/csv");
  assert.match(csv.filename, /^vmr_captured_contacts_.*\.csv$/);
  const parsed = parseCsv(csv.text);
  assert.equal(parsed.rows.length, 3);
  assert.match(parsed.rows[0], /Dana Whitfield0/);

  const json = await w.dispatch({ type: "EXPORT_CAPTURED_CONTACTS", format: "json" });
  assert.equal(json.ok, true);
  assert.equal(json.mime, "application/json");
  assert.match(json.filename, /\.json$/);
  const payload = JSON.parse(json.text);
  // The JSON export is the exact body a save would send, so what was exported
  // and what would be delivered are provably the same thing.
  assert.equal(payload.schema_version, constants.CONTACT_CAPTURE_SCHEMA_VERSION);
  assert.equal(payload.contacts.length, 3);
  assert.equal(
    require("../src/common/contact-schema.js").validateSubmission(payload).valid,
    true
  );
});

// ---- 13. the format is the one that existed before --------------------------

test("the CSV header and column order are the established contract", async () => {
  const w = offlineWorker();
  await captured(w, 1);
  const csv = await w.dispatch({ type: "EXPORT_CAPTURED_CONTACTS", format: "csv" });
  const { header } = parseCsv(csv.text);
  assert.deepEqual(header, schema.CSV_COLUMNS.map(([, name]) => name));
  assert.deepEqual(header.slice(0, 16), [
    "raw_full_name",
    "first_name",
    "last_name",
    "title",
    "company_name",
    "location",
    "linkedin_profile_url",
    "sales_nav_lead_url",
    "company_linkedin_url",
    "sales_nav_company_url",
    "visible_company_metadata",
    "source_search_url",
    "source_page_number",
    "source_position",
    "captured_at",
    "warnings",
  ]);
});

test("the CSV writer quotes, joins and defuses exactly as it did before", () => {
  // Regression on the writer itself rather than through the worker, because
  // these are the cases a spreadsheet gets wrong when the writer is rewritten.
  const text = schema.toCsv([
    {
      rawFullName: 'Ada "Ace" Lovelace, Jr.',
      visibleCompanyMetadata: ["Analytical Engines", "11-50 employees"],
      warnings: [{ code: "missing_field", field: "location" }],
      title: "=1+1",
      location: null,
    },
  ]);
  const row0 = text.split("\r\n")[1];
  assert.match(row0, /"Ada ""Ace"" Lovelace, Jr\."/, "RFC-4180 quoting");
  assert.match(row0, /Analytical Engines \| 11-50 employees/, "arrays join with a pipe");
  assert.match(row0, /'=1\+1/, "a leading = is defused so a spreadsheet cannot execute it");
  assert.ok(row0.includes(",,"), "a value the page did not show stays empty, never invented");
});

// ---- 14. the maximum capture ------------------------------------------------

test("a 5,000-contact capture exports completely", async () => {
  const w = offlineWorker();
  await captured(w, LIMITS.MAX_RECORDS_PER_BATCH);
  const csv = await w.dispatch({ type: "EXPORT_CAPTURED_CONTACTS", format: "csv" });
  assert.equal(csv.ok, true);
  assert.equal(csv.records, LIMITS.MAX_RECORDS_PER_BATCH);
  const { rows } = parseCsv(csv.text);
  assert.equal(rows.length, LIMITS.MAX_RECORDS_PER_BATCH, "every captured row is in the file");
  assert.match(rows[0], /Dana Whitfield0,/);
  assert.match(rows[rows.length - 1], /Dana Whitfield4999,/);
});

// ---- 15 & 16. downloading is not saving, and not clearing --------------------

test("exporting leaves the capture, its inclusion flags and its save untouched", async () => {
  const w = offlineWorker();
  await captured(w, 5);
  await w.dispatch({ type: "TOGGLE_EXCLUDE", index: 4 });
  const before = JSON.stringify(w.store[STORAGE.DRAFT_BATCH]);

  await w.dispatch({ type: "EXPORT_CAPTURED_CONTACTS", format: "csv" });
  await w.dispatch({ type: "EXPORT_CAPTURED_CONTACTS", format: "json" });

  assert.equal(
    JSON.stringify(w.store[STORAGE.DRAFT_BATCH]),
    before,
    "an export must not write anything into the reviewed capture — not even an id"
  );
  const state = await w.dispatch({ type: "GET_STATE" });
  assert.equal(state.batchView.records.length, 5);
  assert.equal(state.batchView.summary.included, 4);
  assert.equal(state.push, null, "exporting is not saving");
});

test("the export reaches no network at all, so an unreachable app costs nothing", async () => {
  const w = offlineWorker();
  await captured(w, 4);
  const csv = await w.dispatch({ type: "EXPORT_CAPTURED_CONTACTS", format: "csv" });
  const json = await w.dispatch({ type: "EXPORT_CAPTURED_CONTACTS", format: "json" });
  assert.equal(csv.ok, true);
  assert.equal(json.ok, true);
  assert.deepEqual(w.networkCalls, [], "not one request may be made to produce a local file");
});

// ---- 17. excluded rows -------------------------------------------------------

test("excluded rows are not exported, matching what they mean for the save", async () => {
  // The behaviour the export had before it was removed: it was built from the
  // submission, which contains the INCLUDED rows only. Preserved deliberately —
  // an export that quietly contained rows the operator had removed would be a
  // different set of people from the one they were looking at.
  const w = offlineWorker();
  await captured(w, 4);
  await w.dispatch({ type: "TOGGLE_EXCLUDE", index: 1 });
  const csv = await w.dispatch({ type: "EXPORT_CAPTURED_CONTACTS", format: "csv" });
  assert.equal(csv.records, 3);
  const { rows } = parseCsv(csv.text);
  assert.equal(rows.length, 3);
  assert.ok(!rows.some((r) => r.startsWith("Dana Whitfield1,")), "the excluded row is not in it");
});

test("an empty capture is refused rather than producing an empty file", async () => {
  const w = offlineWorker();
  const csv = await w.dispatch({ type: "EXPORT_CAPTURED_CONTACTS", format: "csv" });
  assert.equal(csv.ok, false);
  assert.equal(csv.error, "empty_batch");
});

test("an unrecognised format falls back to CSV rather than inventing one", async () => {
  const w = offlineWorker();
  await captured(w, 2);
  const r = await w.dispatch({ type: "EXPORT_CAPTURED_CONTACTS", format: "xlsx" });
  assert.equal(r.ok, true);
  assert.equal(r.format, "csv");
});

// ---- the panel side: an explicit click, and the bytes it saves ---------------

async function panelAtReview(exportResponse) {
  const p = await createPanel({
    responses: {
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
      PROBE_BACKEND: { ok: false, state: "unreachable" },
      GET_ACCOUNT_STATE: { ok: true, account: { connected: true, accountEmail: "a@b.c" } },
      FETCH_LABELS: { ok: false, error: "network_error" },
      FETCH_CAMPAIGNS: { ok: false, error: "network_error" },
      EXPORT_CAPTURED_CONTACTS: exportResponse,
    },
  });
  await p.flush();
  await p.click("listings-review-btn");
  return p;
}

test("the panel saves exactly the bytes the worker produced, on a click", async () => {
  const p = await panelAtReview({
    ok: true,
    format: "csv",
    filename: "vmr_captured_contacts_2026-08-20.csv",
    mime: "text/csv",
    text: "raw_full_name\r\nDana Whitfield",
    records: 2,
  });
  assert.equal(p.$("export-row").hidden, false);
  assert.equal(p.downloads.length, 0, "nothing downloads by itself");

  await p.click("export-csv");
  await p.flush();

  assert.equal(p.downloads.length, 1);
  assert.equal(p.downloads[0].filename, "vmr_captured_contacts_2026-08-20.csv");
  assert.equal(p.downloads[0].saveAs, true, "the operator chooses where it goes");
  assert.equal(await p.downloadedText(0), "raw_full_name\r\nDana Whitfield");
  assert.match(p.$("export-feedback").textContent, /2 contact\(s\)/);
  assert.match(p.$("export-feedback").textContent, /untouched/i);
});

test("downloading does not disturb the review screen or the Save button", async () => {
  const p = await panelAtReview({
    ok: true,
    format: "json",
    filename: "vmr_captured_contacts.json",
    mime: "application/json",
    text: "{}",
    records: 2,
  });
  const saveLabel = p.$("save-btn").textContent.trim();
  await p.click("export-json");
  await p.flush();
  assert.equal(p.view(), "listings-review", "the operator stays where they were");
  assert.equal(p.$("save-btn").textContent.trim(), saveLabel);
  assert.equal(p.$("save-btn").disabled, false, "saving is still available afterwards");
  // And the panel never asked the backend for anything to produce the file.
  const asked = p.sent.filter((m) => m.type === "SAVE_INCLUDED_CONTACTS");
  assert.equal(asked.length, 0);
});

test("a refused export says so and still leaves the capture saveable", async () => {
  const p = await panelAtReview({ ok: false, error: "empty_batch", message: "Nothing to export." });
  await p.click("export-csv");
  await p.flush();
  assert.equal(p.downloads.length, 0);
  assert.match(p.$("export-feedback").textContent, /Nothing to export/);
  assert.equal(p.$("save-btn").disabled, false);
});

"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const ex = require("../src/common/extraction.js");
const sc = require("../src/common/schema.js");
const { loadFixtureDoc, SUPPORTED_URL } = require("./helpers.js");

function captureRecords(fixture) {
  const doc = loadFixtureDoc(fixture, SUPPORTED_URL);
  return ex.extractPage(doc, { sourceSearchUrl: SUPPORTED_URL, capturedAt: "2026-07-23T00:00:00.000Z" }).records;
}

test("buildPayload produces a valid payload; internal _fields are stripped", () => {
  const records = captureRecords("results-normal.html");
  const payload = sc.buildPayload({
    records,
    clientBatchId: sc.newBatchId(),
    campaignId: "camp_1",
    capturedAt: "2026-07-23T00:00:00.000Z",
    currentSearchUrl: SUPPORTED_URL,
    extractionMeta: { extension_version: "1.0.0", pages_captured: 1 },
  });
  const v = sc.validatePayload(payload);
  assert.equal(v.valid, true, v.errors.join("; "));
  for (const r of payload.records) {
    assert.equal(r._stableKey, undefined);
    assert.equal(r._selectorsUsed, undefined);
    assert.ok(Array.isArray(r.warnings));
  }
});

test("validatePayload rejects empty records and wrong schema_version", () => {
  const base = sc.buildPayload({
    records: [],
    clientBatchId: "abcdefgh-1",
    campaignId: null,
    capturedAt: "2026-07-23T00:00:00.000Z",
    currentSearchUrl: null,
  });
  assert.equal(sc.validatePayload(base).valid, false);

  const good = captureRecords("results-normal.html");
  const p = sc.buildPayload({ records: good, clientBatchId: "abcdefgh-1", campaignId: null, capturedAt: "2026-07-23T00:00:00.000Z", currentSearchUrl: null });
  p.schema_version = "salesnav-capture/9.9.9";
  assert.equal(sc.validatePayload(p).valid, false);
});

test("validatePayload rejects a record with neither name nor url", () => {
  const good = captureRecords("results-normal.html");
  const p = sc.buildPayload({ records: good, clientBatchId: "abcdefgh-1", campaignId: null, capturedAt: "2026-07-23T00:00:00.000Z", currentSearchUrl: null });
  p.records.push({
    firstName: null, lastName: null, rawFullName: null, title: null, companyName: null,
    location: null, linkedinProfileUrl: null, salesNavLeadUrl: null, companyLinkedInUrl: null,
    salesNavCompanyUrl: null, visibleCompanyMetadata: null, sourceSearchUrl: null,
    sourcePageNumber: null, sourcePosition: null, capturedAt: null, warnings: [],
  });
  const v = sc.validatePayload(p);
  assert.equal(v.valid, false);
  assert.ok(v.errors.some((e) => /empty record/.test(e)));
});

test("committed example payload fixture validates against the validator", () => {
  const p = JSON.parse(
    fs.readFileSync(path.join(__dirname, "..", "docs", "fixtures", "payload.example.json"), "utf8")
  );
  const v = sc.validatePayload(p);
  assert.equal(v.valid, true, v.errors.join("; "));
});

test("the CSV writer is back, with the column contract it had before", () => {
  // Restored with the export. The first sixteen columns are the historical
  // contract, unchanged in name and order, so anything written against the old
  // file still reads a new one.
  assert.equal(typeof sc.toCsv, "function");
  assert.deepEqual(
    sc.CSV_COLUMNS.slice(0, 16).map(([, header]) => header),
    [
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
    ]
  );
  // Two identifiers the capture gained after the export was removed. APPENDED,
  // so an existing reader keyed on the old headers is unaffected.
  assert.deepEqual(
    sc.CSV_COLUMNS.slice(16).map(([, header]) => header),
    ["linkedin_member_id", "linkedin_alias_url"]
  );
});

test("serializePayload flags oversize payloads", () => {
  const records = captureRecords("results-normal.html");
  const payload = sc.buildPayload({ records, clientBatchId: "abcdefgh-1", campaignId: null, capturedAt: "2026-07-23T00:00:00.000Z", currentSearchUrl: null });
  const s = sc.serializePayload(payload);
  assert.equal(s.withinLimit, true);
  assert.ok(s.bytes > 0);
});

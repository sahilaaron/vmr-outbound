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

test("the CSV writer is gone with the export it existed for (#280)", () => {
  // `toCsv`/`CSV_COLUMNS` had exactly one caller: the panel's "Download CSV"
  // button. With the button, its JSON twin and the `downloads` permission
  // removed, a second serializer of captured personal data with no caller is
  // not something to keep.
  assert.equal(typeof sc.toCsv, "undefined");
  assert.equal(typeof sc.CSV_COLUMNS, "undefined");
});

test("serializePayload flags oversize payloads", () => {
  const records = captureRecords("results-normal.html");
  const payload = sc.buildPayload({ records, clientBatchId: "abcdefgh-1", campaignId: null, capturedAt: "2026-07-23T00:00:00.000Z", currentSearchUrl: null });
  const s = sc.serializePayload(payload);
  assert.equal(s.withinLimit, true);
  assert.ok(s.bytes > 0);
});

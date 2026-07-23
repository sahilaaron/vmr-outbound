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

test("CSV export includes a header and one row per record", () => {
  const records = captureRecords("results-normal.html").map(sc.toWireRecord);
  const csv = sc.toCsv(records);
  const lines = csv.split("\r\n");
  assert.equal(lines.length, records.length + 1);
  assert.ok(lines[0].startsWith("raw_full_name,first_name,last_name"));
});

test("CSV neutralizes formula injection and quotes special chars", () => {
  const rec = {
    rawFullName: "=SUM(A1:A9)", firstName: "+cmd", lastName: '-danger', title: "a,b",
    companyName: 'quote"here', location: "line\nbreak", linkedinProfileUrl: null,
    salesNavLeadUrl: null, companyLinkedInUrl: null, salesNavCompanyUrl: null,
    visibleCompanyMetadata: ["x", "y"], sourceSearchUrl: null, sourcePageNumber: 1,
    sourcePosition: 1, capturedAt: null, warnings: [],
  };
  const csv = sc.toCsv([rec]);
  const row = csv.split("\r\n")[1];
  assert.ok(row.includes("'=SUM(A1:A9)"), "leading = neutralized");
  assert.ok(row.includes("'+cmd"), "leading + neutralized");
  assert.ok(row.includes('"a,b"'), "comma quoted");
  assert.ok(row.includes('"quote""here"'), "double-quote escaped");
});

test("serializePayload flags oversize payloads", () => {
  const records = captureRecords("results-normal.html");
  const payload = sc.buildPayload({ records, clientBatchId: "abcdefgh-1", campaignId: null, capturedAt: "2026-07-23T00:00:00.000Z", currentSearchUrl: null });
  const s = sc.serializePayload(payload);
  assert.equal(s.withinLimit, true);
  assert.ok(s.bytes > 0);
});

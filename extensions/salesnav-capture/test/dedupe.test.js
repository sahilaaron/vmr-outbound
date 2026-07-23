"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const ex = require("../src/common/extraction.js");
const dd = require("../src/common/dedupe.js");
const { WARNINGS } = require("../src/common/constants.js");
const { loadFixtureDoc, SUPPORTED_URL } = require("./helpers.js");

function captureRecords(fixture) {
  const doc = loadFixtureDoc(fixture, SUPPORTED_URL);
  return ex.extractPage(doc, { sourceSearchUrl: SUPPORTED_URL, capturedAt: "t" }).records;
}

test("mergeBatch collapses exact stable-key dupes and keeps uncertain ones", () => {
  const incoming = captureRecords("results-duplicates.html");
  const { records, added, collapsed, uncertain } = dd.mergeBatch([], incoming);
  assert.equal(collapsed, 1); // one Chris row collapsed
  assert.equal(uncertain, 1); // Anonymous Prospect kept + flagged
  assert.equal(records.length, 3); // Chris(1) + Wei + Anonymous
  const chris = records.find((r) => r.rawFullName === "Chris Alvarez");
  assert.ok((chris.warnings || []).some((w) => w.code === WARNINGS.DUPLICATE_COLLAPSED));
  assert.equal(chris._duplicateHits, 2);
  const anon = records.find((r) => r.rawFullName === "Anonymous Prospect");
  assert.ok((anon.warnings || []).some((w) => w.code === WARNINGS.DUPLICATE_UNCERTAIN));
  assert.equal(added, 3);
});

test("mergeBatch accumulates across successive captures (batch persistence model)", () => {
  const page1 = captureRecords("results-normal.html");
  const page2 = captureRecords("results-duplicates.html");
  let state = dd.mergeBatch([], page1);
  assert.equal(state.records.length, 3);
  // Re-capturing page1 adds nothing new (all stable keys already present).
  const again = dd.mergeBatch(state.records, page1);
  assert.equal(again.added, 0);
  assert.equal(again.collapsed, 3);
  // Adding page2 grows the batch by its distinct records.
  const merged = dd.mergeBatch(again.records, page2);
  assert.equal(merged.records.length, 3 + 3);
});

test("merge order does not change the final distinct set", () => {
  const a = captureRecords("results-normal.html");
  const b = captureRecords("results-duplicates.html");
  const ab = dd.mergeBatch(dd.mergeBatch([], a).records, b).records.length;
  const ba = dd.mergeBatch(dd.mergeBatch([], b).records, a).records.length;
  assert.equal(ab, ba);
});

test("summarize reports included/excluded/missing counts", () => {
  const recs = captureRecords("results-missing-fields.html");
  const merged = dd.mergeBatch([], recs).records;
  merged[0]._excluded = true;
  const s = dd.summarize(merged);
  assert.equal(s.total, merged.length);
  assert.equal(s.excluded, 1);
  assert.equal(s.included, merged.length - 1);
  assert.ok(s.withMissingFields >= 1);
});

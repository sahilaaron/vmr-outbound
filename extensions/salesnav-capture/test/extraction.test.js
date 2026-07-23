"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const ex = require("../src/common/extraction.js");
const { WARNINGS, CAPTURE_STATUS } = require("../src/common/constants.js");
const { loadFixtureDoc, SUPPORTED_URL } = require("./helpers.js");

function capture(fixture, url) {
  const doc = loadFixtureDoc(fixture, url || SUPPORTED_URL);
  return ex.extractPage(doc, { sourceSearchUrl: url || SUPPORTED_URL, capturedAt: "2026-07-23T00:00:00.000Z" });
}
function codes(rec) {
  return (rec.warnings || []).map((w) => w.code);
}

test("normal page: extracts all rows with core fields and provenance", () => {
  const r = capture("results-normal.html");
  assert.equal(r.status, CAPTURE_STATUS.OK);
  assert.equal(r.count, 3);
  const first = r.records[0];
  assert.equal(first.rawFullName, "Dana Whitfield");
  assert.equal(first.firstName, "Dana");
  assert.equal(first.lastName, "Whitfield");
  assert.equal(first.title, "Head of Operations");
  assert.equal(first.companyName, "Northwind Logistics");
  assert.equal(first.location, "Greater Chicago Area");
  assert.equal(first.salesNavLeadUrl, "https://www.linkedin.com/sales/lead/ACwAAAB1x9k");
  assert.equal(first.salesNavCompanyUrl, "https://www.linkedin.com/sales/company/1234567");
  assert.deepEqual(first.visibleCompanyMetadata, ["Logistics & Supply Chain"]);
  assert.equal(first.sourcePageNumber, 2);
  assert.equal(first.sourcePosition, 1);
  assert.equal(first.capturedAt, "2026-07-23T00:00:00.000Z");
  // No public /in/ url present -> null + missing warning, never fabricated.
  assert.equal(first.linkedinProfileUrl, null);
  assert.ok(codes(first).includes(WARNINGS.MISSING_FIELD));
});

test("missing fields: explicit nulls + missing_field warnings, no guessing", () => {
  const r = capture("results-missing-fields.html");
  assert.equal(r.status, CAPTURE_STATUS.OK);
  // The name-less "ghost" row has no anchor and is not fabricated into a record.
  assert.equal(r.count, 2);
  const jordan = r.records.find((x) => x.rawFullName === "Jordan Field");
  assert.equal(jordan.title, null);
  assert.equal(jordan.companyName, null);
  assert.ok(codes(jordan).includes(WARNINGS.MISSING_FIELD));
  const madonna = r.records.find((x) => x.rawFullName === "Madonna");
  assert.equal(madonna.lastName, null);
  assert.ok(codes(madonna).includes(WARNINGS.MISSING_FIELD));
});

test("alternate/changed selectors: falls back to structural discovery + class selectors", () => {
  const r = capture("results-alternate-selectors.html");
  assert.equal(r.status, CAPTURE_STATUS.OK);
  assert.equal(r.count, 2);
  const lena = r.records[0];
  assert.equal(lena.rawFullName, "Lena Fischer");
  assert.equal(lena.title, "Chief Financial Officer");
  assert.equal(lena.location, "Munich, Bavaria, Germany");
  assert.equal(lena.salesNavLeadUrl, "https://www.linkedin.com/sales/lead/ACwAAAF9ghi");
  // company absent under alternate structure -> missing, not guessed
  assert.equal(lena.companyName, null);
});

test("empty search: reported as empty, never a successful capture", () => {
  const r = capture("results-empty.html");
  assert.equal(r.status, CAPTURE_STATUS.EMPTY);
  assert.equal(r.count, 0);
});

test("structure unrecognized: supported URL but no rows and no no-results marker", () => {
  const doc = loadFixtureDoc("results-challenge.html", SUPPORTED_URL);
  // Strip challenge signals to simulate a bare/changed page with no rows.
  doc.body.innerHTML = "<main><div>totally different layout</div></main>";
  const r = ex.extractPage(doc, { sourceSearchUrl: SUPPORTED_URL, capturedAt: "t" });
  assert.equal(r.status, CAPTURE_STATUS.STRUCTURE_UNRECOGNIZED);
  assert.equal(r.count, 0);
});

test("duplicates: same lead collapses on normalization; url-less kept as uncertain", () => {
  const r = capture("results-duplicates.html");
  assert.equal(r.status, CAPTURE_STATUS.OK);
  // 4 rows captured at page level (dedupe happens in dedupe.mergeBatch, tested there),
  // but rows 1 and 3 must normalize to the SAME stable key.
  assert.equal(r.count, 4);
  const chrisRows = r.records.filter((x) => x.rawFullName === "Chris Alvarez");
  assert.equal(chrisRows.length, 2);
  assert.equal(chrisRows[0]._stableKey, chrisRows[1]._stableKey);
  const anon = r.records.find((x) => x.rawFullName === "Anonymous Prospect");
  assert.equal(anon._stableKey, null);
  assert.ok(codes(anon).includes(WARNINGS.NO_STABLE_IDENTITY));
});

test("malformed urls: flagged, never repaired or fabricated", () => {
  const r = capture("results-malformed-urls.html");
  assert.equal(r.status, CAPTURE_STATUS.OK);
  const broken = r.records[0];
  assert.equal(broken.salesNavLeadUrl, null);
  assert.ok(codes(broken).includes(WARNINGS.MALFORMED_URL));
  // name/title still captured
  assert.equal(broken.rawFullName, "Broken Link Person");
  const offPlatform = r.records[1];
  // evil.example.com/in/phish must NOT become a captured profile url
  assert.equal(offPlatform.linkedinProfileUrl, null);
  assert.ok(codes(offPlatform).includes(WARNINGS.MALFORMED_URL));
});

test("unicode: raw names preserved verbatim, not translated/ascii-folded", () => {
  const r = capture("results-unicode.html");
  assert.equal(r.count, 3);
  assert.equal(r.records[0].rawFullName, "大角 知也");
  assert.equal(r.records[0].title, "事業開発マネージャー");
  assert.equal(r.records[1].rawFullName, "أحمد الطيب");
  assert.equal(r.records[2].rawFullName, "José Ñoño-Müller");
});

test("challenge: halts with challenge_detected and captures nothing", () => {
  const r = capture("results-challenge.html");
  assert.equal(r.status, CAPTURE_STATUS.CHALLENGE_DETECTED);
  assert.equal(r.count, 0);
});

test("challenge by URL halts even on an otherwise-normal DOM", () => {
  const r = capture("results-normal.html", "https://www.linkedin.com/checkpoint/challenge/x");
  assert.equal(r.status, CAPTURE_STATUS.CHALLENGE_DETECTED);
});

test("unsupported page URL: reported, nothing captured", () => {
  const r = capture("results-normal.html", "https://www.linkedin.com/feed/");
  assert.equal(r.status, CAPTURE_STATUS.UNSUPPORTED_PAGE);
  assert.equal(r.count, 0);
});

// ---- supported-page detection (PR #121 correction) ----------------------

test("supported detection: only people/lead search result routes match", () => {
  assert.equal(ex.isSupportedResultsUrl("https://www.linkedin.com/sales/search/people?page=2"), true);
  assert.equal(ex.isSupportedResultsUrl("https://www.linkedin.com/sales/search/people/"), true);
  assert.equal(ex.isSupportedResultsUrl("https://www.linkedin.com/sales/search/results/people"), true);
  // No broad /search/ fallback anymore:
  assert.equal(ex.isSupportedResultsUrl("https://www.linkedin.com/sales/search/company?page=1"), false);
  assert.equal(ex.isSupportedResultsUrl("https://www.linkedin.com/sales/search/accounts"), false);
  assert.equal(ex.isSupportedResultsUrl("https://www.linkedin.com/sales/home"), false);
  assert.equal(ex.isSupportedResultsUrl("https://www.linkedin.com/feed/"), false);
  assert.equal(ex.isSupportedResultsUrl("https://evil.example.com/sales/search/people"), false);
});

test("rejected-surface detection flags account/company explicitly", () => {
  assert.equal(ex.isRejectedSalesSurface("https://www.linkedin.com/sales/search/company"), true);
  assert.equal(ex.isRejectedSalesSurface("https://www.linkedin.com/sales/search/accounts"), true);
  assert.equal(ex.isRejectedSalesSurface("https://www.linkedin.com/sales/company/123"), true);
  assert.equal(ex.isRejectedSalesSurface("https://www.linkedin.com/sales/search/people"), false);
  assert.equal(ex.isRejectedSalesSurface("https://www.linkedin.com/feed/"), false);
});

test("unsupported: account search page is rejected with a clear reason, nothing captured", () => {
  const r = capture("results-account-search.html", "https://www.linkedin.com/sales/search/company?page=1");
  assert.equal(r.status, CAPTURE_STATUS.UNSUPPORTED_PAGE);
  assert.equal(r.count, 0);
  assert.equal(r.pageWarnings[0].reason, "rejected_sales_surface");
});

test("unsupported: company page is rejected, nothing captured", () => {
  const r = capture("results-account-search.html", "https://www.linkedin.com/sales/company/1234567");
  assert.equal(r.status, CAPTURE_STATUS.UNSUPPORTED_PAGE);
  assert.equal(r.count, 0);
  assert.equal(r.pageWarnings[0].reason, "rejected_sales_surface");
});

test("unsupported: generic Sales Navigator page is not captured", () => {
  const r = capture("results-normal.html", "https://www.linkedin.com/sales/home");
  assert.equal(r.status, CAPTURE_STATUS.UNSUPPORTED_PAGE);
  assert.equal(r.count, 0);
  assert.equal(r.pageWarnings[0].reason, "not_people_search");
});

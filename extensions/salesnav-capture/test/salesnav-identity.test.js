/**
 * DAT-018 A — canonical LinkedIn profile URL derived from a Sales Navigator
 * lead URL, and B — Company Name capture eligibility.
 *
 * The rule being protected: a derivation must be conservative and marked. A
 * supported lead URL yields the canonical `/in/<member-id>`; anything else
 * yields null with a reason. The original Sales Navigator URL is preserved as
 * source evidence in every case.
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

global.self = global;
const constants = require("../src/common/constants.js");
const normalize = require("../src/common/normalize.js");
const extraction = require("../src/common/extraction.js");

const { WARNINGS, SKIP_REASONS } = constants;
const FIXTURES = path.join(__dirname, "fixtures");

function capture(name) {
  const dom = new JSDOM(fs.readFileSync(path.join(FIXTURES, name), "utf8"));
  return extraction.extractPage(dom.window.document, {
    sourceSearchUrl: "https://www.linkedin.com/sales/search/people?page=1",
    capturedAt: "2026-07-27T00:00:00.000Z",
  });
}

// --- A. member id parsing ------------------------------------------------------

test("ordinary lead URL yields the member id, and no URL is built from it", () => {
  const r = normalize.salesNavMemberId("https://www.linkedin.com/sales/lead/ACwAAAB1x9k");
  assert.equal(r.id, "ACwAAAB1x9k");
  assert.equal(r.reason, null);
  // DAT-019: the helper that turned this into `/in/<member-id>` is gone.
  assert.equal(normalize.profileUrlFromSalesNavLead, undefined);
});

test("query strings, fragments and search-context suffixes are stripped first", () => {
  const expected = "ACwAAAB1x9k";
  const variants = [
    "https://www.linkedin.com/sales/lead/ACwAAAB1x9k,NAME_SEARCH,3f2a",
    "https://www.linkedin.com/sales/lead/ACwAAAB1x9k?trk=results&sessionId=xyz",
    "https://www.linkedin.com/sales/lead/ACwAAAB1x9k#profile",
    "https://www.linkedin.com/sales/lead/ACwAAAB1x9k,NAME_SEARCH,3f2a?trk=x#y",
    "https://www.linkedin.com/sales/lead/ACwAAAB1x9k/",
    "/sales/lead/ACwAAAB1x9k",
    "//www.linkedin.com/sales/lead/ACwAAAB1x9k",
    "HTTPS://WWW.LINKEDIN.COM/sales/lead/ACwAAAB1x9k",
  ];
  for (const v of variants) {
    assert.equal(normalize.salesNavMemberId(v).id, expected, v);
  }
});

test("extra route material after the identifier is ignored, not absorbed", () => {
  const r = normalize.salesNavMemberId(
    "https://www.linkedin.com/sales/lead/ACwAAAB1x9k/detail/activity"
  );
  assert.equal(r.id, "ACwAAAB1x9k");
});

test("malformed or missing identifiers are refused, never fabricated", () => {
  const refused = [
    ["https://www.linkedin.com/sales/lead/", "not_a_lead_url"],
    ["https://www.linkedin.com/sales/lead/ab", "malformed_identifier"],
    ["https://www.linkedin.com/sales/lead/bad id", "malformed_identifier"],
    ["https://www.linkedin.com/sales/lead/has.dots", "malformed_identifier"],
    ["https://www.linkedin.com/sales/lead/" + "x".repeat(200), "malformed_identifier"],
    // A different Sales Navigator route: its segment is not the member id.
    ["https://www.linkedin.com/sales/people/ACwAAAB1x9k", "not_a_lead_url"],
    // Never trust a look-alike host.
    ["https://evil.example.com/sales/lead/ACwAAAB1x9k", "non_linkedin_host"],
    ["https://linkedin.com.evil.example/sales/lead/ACwAAAB1x9k", "non_linkedin_host"],
    ["javascript:alert(1)", "unparseable"],
    ["", "empty"],
    [null, "empty"],
  ];
  for (const [input, reason] of refused) {
    const r = normalize.salesNavMemberId(input);
    assert.equal(r.id, null, `must not read an identifier out of ${String(input)}`);
    assert.equal(r.reason, reason, String(input));
  }
});

test("a subdomain LinkedIn host still yields the same member id", () => {
  const r = normalize.salesNavMemberId("https://in.linkedin.com/sales/lead/ACwAAAB1x9k");
  assert.equal(r.id, "ACwAAAB1x9k");
});

// --- A. identifiers inside extraction -----------------------------------------

test("a visible /in/ link is recorded as observed, alongside the member id", () => {
  const r = capture("results-observed-profile-url.html");
  const rec = r.records[0];
  assert.equal(rec.linkedinProfileUrl, "https://www.linkedin.com/in/dana-observed");
  assert.equal(rec.linkedinProfileUrlSource, "observed");
  // DAT-019 keeps BOTH identifier forms when the row shows both. Observed
  // together on one row, they relate the member id to the handle without
  // anything being inferred.
  assert.equal(rec.linkedinMemberId, "ACwAAAQ2zzz");
  assert.equal(rec.salesNavLeadUrl, "https://www.linkedin.com/sales/lead/ACwAAAQ2zzz");
  assert.ok(!rec.warnings.some((w) => w.code === WARNINGS.DERIVED_VALUE));
});

test("with no visible link there is no profile URL, only the member id", () => {
  const r = capture("results-normal.html");
  const rec = r.records[0];
  assert.equal(rec.linkedinProfileUrl, null);
  assert.equal(rec.linkedinProfileUrlSource, null);
  assert.match(rec.linkedinMemberId, /^[A-Za-z0-9_-]{3,128}$/);
  assert.ok(rec.salesNavLeadUrl.includes("/sales/lead/"));
  // The rule this test exists for is that no VALUE is derived: the profile URL
  // and its source both stay null, asserted above. The resolving alias is a
  // separate field and a separate claim.
  //
  // The absence of the handle is now reported as provenance rather than as a
  // fault, precisely because an alias exists for this row — so a derived_value
  // warning here is expected, and it is attached to the field it explains.
  const derived = rec.warnings.filter((w) => w.code === WARNINGS.DERIVED_VALUE);
  assert.deepEqual(
    derived.map((w) => w.field),
    ["linkedinProfileUrl"],
    "the only derivation claim is about the absent handle, not about a value"
  );
});

// --- B. company-name eligibility ----------------------------------------------

test("rows with no company name are withheld, with a truthful reason", () => {
  const r = capture("results-missing-company.html");
  assert.equal(r.status, constants.CAPTURE_STATUS.OK);
  assert.equal(r.visibleCount, 4);
  assert.equal(r.count, 1);
  assert.equal(r.skippedCount, 3);
  assert.deepEqual(
    r.skipped.map((x) => x.reason),
    [
      SKIP_REASONS.MISSING_COMPANY_NAME,
      SKIP_REASONS.MISSING_COMPANY_NAME,
      SKIP_REASONS.MISSING_COMPANY_NAME,
    ]
  );
  assert.deepEqual(r.pageWarnings, [
    { code: "rows_skipped", reason: SKIP_REASONS.MISSING_COMPANY_NAME, count: 3 },
  ]);
});

test("blank and whitespace-only company names count as missing", () => {
  const r = capture("results-missing-company.html");
  const skippedNames = r.skipped.map((x) => x.rawFullName);
  assert.ok(skippedNames.includes("Absent Company"), "no company element at all");
  assert.ok(skippedNames.includes("Empty Company"), "empty company element");
  assert.ok(skippedNames.includes("Whitespace Company"), "whitespace-only company");
});

test("skipping unusable rows does not abort the rest of the page", () => {
  const r = capture("results-missing-company.html");
  assert.equal(r.records.length, 1);
  const kept = r.records[0];
  assert.equal(kept.rawFullName, "Valid Person");
  assert.equal(kept.companyName, "Northwind Freight");
  assert.equal(kept.title, "Head of Operations");
  // The surviving row is fully formed. Its identity is the member id; no
  // profile URL was visible, so none is claimed (DAT-019).
  assert.equal(kept.linkedinProfileUrl, null);
  assert.equal(kept.linkedinMemberId, "ACwAAAV4ddd");
});

test("a skipped row is never turned into a record by inference", () => {
  const r = capture("results-missing-company.html");
  // Nothing in the skipped set leaked into records, and no company was invented
  // from headline, location or a neighbouring row.
  const keptNames = r.records.map((x) => x.rawFullName);
  for (const s of r.skipped) assert.ok(!keptNames.includes(s.rawFullName));
  assert.ok(r.records.every((x) => x.companyName && x.companyName.trim()));
});

test("skipped entries carry only position, reason and the visible name", () => {
  const r = capture("results-missing-company.html");
  for (const s of r.skipped) {
    assert.deepEqual(Object.keys(s).sort(), ["rawFullName", "reason", "sourcePosition"]);
    assert.equal(typeof s.sourcePosition, "number");
  }
});

test("a page where every row lacks a company is OK with zero records", () => {
  const r = capture("results-alternate-selectors.html");
  assert.equal(r.status, constants.CAPTURE_STATUS.OK);
  assert.equal(r.count, 0);
  assert.equal(r.records.length, 0);
  assert.equal(r.skippedCount, 2);
});

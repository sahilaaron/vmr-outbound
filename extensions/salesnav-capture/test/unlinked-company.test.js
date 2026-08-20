/**
 * A company LinkedIn/Sales Navigator page is OPTIONAL ENRICHMENT. It is never a
 * prerequisite for capturing the person.
 *
 * Production feedback: a very large share of otherwise valid contacts were being
 * lost because the person's employer was shown as plain text with no company
 * page to link to. Two things had to be true at once for that to happen, and
 * this file pins both of them down:
 *
 *   1. `extractCompanyName` read the employer only from a company anchor or a
 *      `data-anonymize="company-name"` node, so an unlinked employer produced no
 *      company name at all;
 *   2. `extractPage` then dropped any row with no company name, so the missing
 *      link took the whole person with it.
 *
 * The invariant now: person identity decides whether a Contact exists. The
 * company name is best-effort and the company URL is enrichment, and neither is
 * ever fabricated to satisfy the other.
 *
 * The whole path is exercised, not just the DOM: extraction -> dedupe ->
 * contact-capture payload -> the payload validator the backend runs the same
 * JSON Schema against.
 */
"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");

global.self = global;
const ex = require("../src/common/extraction.js");
const dedupe = require("../src/common/dedupe.js");
const contactSchema = require("../src/common/contact-schema.js");
const { WARNINGS, CAPTURE_STATUS } = require("../src/common/constants.js");
const { loadFixtureDoc, SUPPORTED_URL } = require("./helpers.js");

function capture(fixture) {
  const doc = loadFixtureDoc(fixture, SUPPORTED_URL);
  return ex.extractPage(doc, {
    sourceSearchUrl: SUPPORTED_URL,
    capturedAt: "2026-08-20T00:00:00.000Z",
  });
}

function byName(result) {
  return Object.fromEntries(result.records.map((r) => [r.rawFullName, r]));
}

function hasMissing(rec, field) {
  return (rec.warnings || []).some(
    (w) => w.code === WARNINGS.MISSING_FIELD && w.field === field
  );
}

// --- 1. the linked company is untouched --------------------------------------

test("a linked company still yields both the name and the URL", () => {
  const r = capture("results-unlinked-company.html");
  const jane = byName(r)["Jane Doe"];
  assert.ok(jane, "the linked row is captured");
  assert.equal(jane.title, "VP Marketing");
  assert.equal(jane.companyName, "Acme Corporation");
  assert.equal(jane.salesNavCompanyUrl, "https://www.linkedin.com/sales/company/1234567");
  assert.ok(!hasMissing(jane, "companyName"));
  assert.equal(jane._selectorsUsed.companyName, 'a[data-anonymize="company-name"]');
});

test("the pre-existing linked-company fixtures are byte-for-byte unchanged", () => {
  // The regression guard for "did the new strategies disturb the old path?".
  const normal = capture("results-normal.html");
  assert.equal(normal.count, 3);
  assert.deepEqual(
    normal.records.map((x) => [x.companyName, x.salesNavCompanyUrl]),
    [
      ["Northwind Logistics", "https://www.linkedin.com/sales/company/1234567"],
      ["Cliffside Software, Inc.", "https://www.linkedin.com/sales/company/7654321"],
      ["Harbor Freight Collective", "https://www.linkedin.com/sales/company/2468013"],
    ]
  );
  const alternate = capture("results-alternate-company.html");
  assert.deepEqual(
    alternate.records.map((x) => [x.companyName, x.salesNavCompanyUrl]),
    [
      ["Novaline Freight", "https://www.linkedin.com/sales/company/4242"],
      ["Harbor Analytics", "https://www.linkedin.com/sales/company/5151"],
    ]
  );
});

// --- 2. the primary regression: a plain-text company ---------------------------

test("a plain-text company is captured, name kept, URL absent", () => {
  const r = capture("results-unlinked-company.html");
  const john = byName(r)["John Smith"];
  assert.ok(john, "the unlinked row must not be filtered out");
  assert.equal(john.title, "Head of Procurement");
  assert.equal(john.companyName, "Example Industries");
  assert.equal(john.companyLinkedInUrl, null);
  assert.equal(john.salesNavCompanyUrl, null);
  // A company with no page is complete, not faulty. Nothing reports it as a gap.
  assert.ok(!hasMissing(john, "companyName"));
  assert.ok(
    !(john.warnings || []).some((w) => /company.*url/i.test(String(w.field))),
    "the absence of a company page is not a warning"
  );
});

test("every unlinked shape the surface produces yields the visible name", () => {
  const rows = byName(capture("results-unlinked-company.html"));
  // Same subtitle line, "at" connective.
  assert.equal(rows["John Smith"].companyName, "Example Industries");
  // Its own subtitle line — the unlinked twin of `.artdeco-entity-lockup__subtitle a`.
  assert.equal(rows["Lena Fischer"].companyName, "Novaline Freight");
  // Same subtitle line, middot connective.
  assert.equal(rows["Samuel Adeyemi"].companyName, "Harbor Analytics");
  // A dedicated company node that simply is not an anchor.
  assert.equal(rows["Amara Okafor"].companyName, "Southgate Haulage");
  for (const name of ["John Smith", "Lena Fischer", "Samuel Adeyemi", "Amara Okafor"]) {
    assert.equal(rows[name].companyLinkedInUrl, null, `${name} gets no fabricated URL`);
    assert.equal(rows[name].salesNavCompanyUrl, null, `${name} gets no fabricated URL`);
  }
});

// --- 3. the count ---------------------------------------------------------------

test("a mixed page captures every person, not only the linked ones", () => {
  const r = capture("results-unlinked-company.html");
  assert.equal(r.status, CAPTURE_STATUS.OK);
  // 3 linked + 4 unlinked. Before the fix this page yielded 3.
  assert.equal(r.count, 7);
  assert.equal(r.visibleCount, 7);
  assert.equal(r.records.filter((x) => x.salesNavCompanyUrl).length, 3);
  assert.equal(r.records.filter((x) => !x.salesNavCompanyUrl).length, 4);
  assert.deepEqual(r.pageWarnings, [], "no row is reported as withheld");
});

// --- 4. no company information at all -------------------------------------------

test("a person with no readable company is captured with null company fields", () => {
  const r = capture("results-company-connective-only.html");
  assert.equal(r.count, 3);
  const alice = byName(r)["Alice Brown"];
  assert.equal(alice.title, "Director");
  assert.equal(alice.companyName, null);
  assert.equal(alice.companyLinkedInUrl, null);
  assert.equal(alice.salesNavCompanyUrl, null);
  assert.equal(alice.salesNavLeadUrl, "https://www.linkedin.com/sales/lead/ACwAAAY7ggg");
  assert.ok(hasMissing(alice, "companyName"), "the gap is reported, not hidden");
});

test("a connective is never mistaken for a company name", () => {
  const rows = byName(capture("results-company-connective-only.html"));
  // The title itself contains " at ". A title is never split on a connective,
  // so no company is manufactured out of "scale".
  assert.equal(rows["Rosa Iglesias"].title, "Operations Manager at scale");
  assert.equal(rows["Rosa Iglesias"].companyName, null);
  // A dangling "at" with nothing after it leaves nothing.
  assert.equal(rows["Tomas Berg"].companyName, null);
});

// --- 5. rows never contaminate one another ---------------------------------------

test("company text and links never cross a row boundary", () => {
  const r = capture("results-unlinked-company.html");
  const rows = byName(r);
  // Every linked row keeps its OWN URL, and no unlinked row acquires one from a
  // neighbour on either side. Row order in the fixture deliberately alternates.
  assert.deepEqual(
    r.records.map((x) => [x.rawFullName, x.companyName, x.salesNavCompanyUrl]),
    [
      ["Jane Doe", "Acme Corporation", "https://www.linkedin.com/sales/company/1234567"],
      ["John Smith", "Example Industries", null],
      [
        "Priya Raghunathan",
        "Harbor Freight Collective",
        "https://www.linkedin.com/sales/company/2468013",
      ],
      ["Lena Fischer", "Novaline Freight", null],
      ["Samuel Adeyemi", "Harbor Analytics", null],
      [
        "Marcus O’Neill",
        "Cliffside Software, Inc.",
        "https://www.linkedin.com/sales/company/7654321",
      ],
      ["Amara Okafor", "Southgate Haulage", null],
    ]
  );
  // And each row's lead URL is its own, which is what a cross-row read would
  // most easily corrupt.
  assert.equal(new Set(r.records.map((x) => x.salesNavLeadUrl)).size, 7);
  assert.equal(rows["John Smith"].location, "Leeds, United Kingdom");
  assert.equal(rows["Amara Okafor"].location, "Dublin, Ireland");
});

// --- 6. the payload the backend receives ------------------------------------------

function submissionFor(records) {
  return contactSchema.buildSubmission({
    clientSubmissionId: contactSchema.newId(),
    captureMode: "salesnav_people_search",
    submittedAt: "2026-08-20T00:00:00.000Z",
    extensionVersion: "2.1.0",
    campaignId: null,
    metadata: { labels: [], note: null },
    contacts: records.map((rec) =>
      contactSchema.buildResultRowCapture({
        record: rec,
        clientCaptureId: contactSchema.newId(),
        adapterVersion: "test",
        metadata: { labels: [], note: null },
      })
    ),
  });
}

test("a capture with a company name and no company URL is a valid submission", () => {
  const r = capture("results-unlinked-company.html");
  const john = byName(r)["John Smith"];
  const payload = submissionFor([john]);
  const hint = payload.contacts[0].current_employment_hint;
  assert.equal(hint.company_name, "Example Industries");
  assert.equal(hint.company_linkedin_url, null);
  assert.equal(hint.company_linkedin_id, null);
  const { valid, errors } = contactSchema.validateSubmission(payload);
  assert.deepEqual(errors, []);
  assert.equal(valid, true);
});

test("a capture with no company information at all is a valid submission", () => {
  const alice = byName(capture("results-company-connective-only.html"))["Alice Brown"];
  const payload = submissionFor([alice]);
  const hint = payload.contacts[0].current_employment_hint;
  assert.equal(hint.company_name, null);
  assert.equal(hint.company_linkedin_url, null);
  // The person is still identifiable, which is the requirement that does apply.
  assert.equal(payload.contacts[0].person.full_name, "Alice Brown");
  assert.equal(
    payload.contacts[0].person.salesnav_lead_url,
    "https://www.linkedin.com/sales/lead/ACwAAAY7ggg"
  );
  const { valid, errors } = contactSchema.validateSubmission(payload);
  assert.deepEqual(errors, []);
  assert.equal(valid, true);
});

test("the whole mixed page survives as one submission", () => {
  const r = capture("results-unlinked-company.html");
  const payload = submissionFor(r.records);
  assert.equal(payload.contacts.length, 7);
  const { valid, errors } = contactSchema.validateSubmission(payload);
  assert.deepEqual(errors, []);
  assert.equal(valid, true);
  assert.equal(
    payload.contacts.filter((c) => c.current_employment_hint.company_name).length,
    7,
    "every row on this page shows a company name, linked or not"
  );
});

// --- 7. identity is unchanged ------------------------------------------------------

test("dedupe still keys on the person, never on the company", () => {
  const r = capture("results-unlinked-company.html");
  const john = byName(r)["John Smith"];
  // The same person read twice: once with a company page visible, once without.
  // Company data must not make them two people.
  const withCompanyUrl = Object.assign({}, john, {
    companyLinkedInUrl: "https://www.linkedin.com/company/example-industries",
    sourcePosition: 9,
  });
  const withoutCompanyAtAll = Object.assign({}, john, {
    companyName: null,
    sourcePosition: 12,
  });
  const merged = dedupe.mergeBatch([], [john, withCompanyUrl, withoutCompanyAtAll]);
  assert.equal(merged.records.length, 1, "one person, however their employer reads");
  assert.equal(merged.added, 1);
  assert.equal(merged.collapsed, 2);
  assert.equal(merged.records[0]._stableKey, john.salesNavLeadUrl);
});

test("two different people at the same unlinked company stay two people", () => {
  const r = capture("results-unlinked-company.html");
  const rows = byName(r);
  const sameCompany = Object.assign({}, rows["Lena Fischer"], {
    companyName: rows["John Smith"].companyName,
  });
  const merged = dedupe.mergeBatch([], [rows["John Smith"], sameCompany]);
  assert.equal(merged.records.length, 2);
  assert.equal(merged.collapsed, 0);
});

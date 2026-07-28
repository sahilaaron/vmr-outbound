"use strict";
/**
 * DAT-019 / #195 — a Sales Navigator member id is an identifier, not a URL.
 *
 * DAT-018 A made a results row without a visible `/in/` link derive one from the
 * lead URL: `https://www.linkedin.com/in/<opaque-member-id>`.
 *
 * That alias does resolve — LinkedIn's `/in/` route accepts the opaque member id
 * and redirects to the person (confirmed by Sahil, 2026-07-28). The defect is not
 * that it points nowhere. It is that identity is matched by exact normalized
 * string, so the alias and the person's vanity handle are two different keys for
 * one human: capture them from a results row and from their own profile page and
 * you get two identities that can never match. The member id is also
 * case-sensitive, while the vanity-URL normalizer lowercases slugs.
 *
 * Three committed contracts already forbid it:
 *
 *   docs/contact_input_contract.md   "Identity is never repaired from a lead URL."
 *   contact-capture.schema.json      "Null keeps identity honestly uncertain;
 *                                     it is never invented."
 *   app/models/linkedin_profile.py   salesnav_lead_url is "NEVER an identity key".
 *
 * These tests hold the extension to those contracts: the member id is captured
 * and named for what it is, and the profile URL stays null until a real one is
 * on the page.
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
const contactSchema = require("../src/common/contact-schema.js");

const { WARNINGS } = constants;
const FIXTURES = path.join(__dirname, "fixtures");

function capture(name) {
  const dom = new JSDOM(fs.readFileSync(path.join(FIXTURES, name), "utf8"));
  return extraction.extractPage(dom.window.document, {
    sourceSearchUrl: "https://www.linkedin.com/sales/search/people?page=1",
    capturedAt: "2026-07-27T00:00:00.000Z",
  });
}

// --- 1. the fabrication is gone ----------------------------------------------

test("a row with no visible /in/ link gets no profile URL at all", () => {
  const rec = capture("results-normal.html").records[0];

  assert.ok(rec.salesNavLeadUrl.includes("/sales/lead/"), "precondition: a lead URL is present");
  assert.equal(rec.linkedinProfileUrl, null, "identity is never repaired from a lead URL");
  assert.equal(rec.linkedinProfileUrlSource, null);
});

test("nothing that looks like a public profile URL is built from the member id", () => {
  for (const rec of capture("results-normal.html").records) {
    if (!rec.linkedinMemberId) continue;
    assert.notEqual(
      rec.linkedinProfileUrl,
      "https://www.linkedin.com/in/" + rec.linkedinMemberId,
      "the opaque member id must never be dressed up as a vanity URL"
    );
  }
});

test("no derived_value warning is claimed for a profile URL that was not derived", () => {
  const rec = capture("results-normal.html").records[0];
  const derived = (rec.warnings || []).filter(
    (w) => w.code === WARNINGS.DERIVED_VALUE && w.field === "linkedinProfileUrl"
  );
  assert.deepEqual(derived, []);
});

test("the absent profile URL is reported as missing, not passed over in silence", () => {
  const rec = capture("results-normal.html").records[0];
  const missing = (rec.warnings || []).find(
    (w) => w.code === WARNINGS.MISSING_FIELD && w.field === "linkedinProfileUrl"
  );
  assert.ok(missing, "an uncertain identity must be visible to the operator");
});

// --- 2. the member id is a first-class identifier ----------------------------

test("the member id is parsed from the lead URL and kept under its own name", () => {
  const rec = capture("results-normal.html").records[0];
  assert.match(rec.linkedinMemberId, /^[A-Za-z0-9_-]{3,128}$/);
  assert.equal(rec.linkedinMemberId, normalize.salesNavMemberId(rec.salesNavLeadUrl).id);
});

test("the raw lead URL survives untouched beside it", () => {
  const rec = capture("results-normal.html").records[0];
  assert.ok(rec.salesNavLeadUrl.startsWith("https://www.linkedin.com/sales/lead/"));
});

test("the member id keeps its exact case, and the search suffix is split off first", () => {
  // The identifier is case-sensitive: the alias LinkedIn accepts carries the
  // case through verbatim, so folding it would break the very value it names.
  // Everything after the first comma is volatile search context, not identity.
  const raw =
    "https://www.linkedin.com/sales/lead/ACwAAAACaUgB2RAj8vfkHcwfcSBgD7GEr0BIIkU," +
    "NAME_SEARCH,ctTs?_ntb=14jZD%2BMORH27zguMHaY8gg%3D%3D";
  const member = normalize.salesNavMemberId(raw);
  assert.equal(member.id, "ACwAAAACaUgB2RAj8vfkHcwfcSBgD7GEr0BIIkU");
  assert.notEqual(member.id, member.id.toLowerCase(), "precondition: mixed case");
});

test("a malformed lead URL yields no member id and no invented one", () => {
  const refused = [
    "https://www.linkedin.com/sales/lead/",
    "https://www.linkedin.com/sales/lead/ab",
    "https://evil.example.com/sales/lead/ACwAAAB1x9k",
    "",
    null,
  ];
  for (const input of refused) {
    assert.equal(normalize.salesNavMemberId(input).id, null, String(input));
  }
});

// --- 3. an observed URL always wins, and both identifiers are kept ------------

test("a visible /in/ link is used as-is and is not replaced by the member id", () => {
  const rec = capture("results-observed-profile-url.html").records[0];
  assert.equal(rec.linkedinProfileUrl, "https://www.linkedin.com/in/dana-observed");
  assert.equal(rec.linkedinProfileUrlSource, "observed");
});

test("a row showing both a lead URL and a real /in/ link keeps both identifiers", () => {
  // This pair is the deterministic bridge: one page, one person, both identifier
  // forms observed together. Nothing has to be inferred to relate them.
  const rec = capture("results-observed-profile-url.html").records[0];
  assert.equal(rec.linkedinProfileUrl, "https://www.linkedin.com/in/dana-observed");
  assert.equal(rec.linkedinMemberId, "ACwAAAQ2zzz");
  assert.equal(rec.salesNavLeadUrl, "https://www.linkedin.com/sales/lead/ACwAAAQ2zzz");
});

// --- 4. what reaches the wire ------------------------------------------------

function personOf(rec) {
  return contactSchema.buildResultRowCapture({
    record: rec,
    clientCaptureId: "11111111-1111-4111-8111-111111111111",
    capturedAt: "2026-07-27T00:00:00.000Z",
    sourceSearchUrl: "https://www.linkedin.com/sales/search/people",
    adapterVersion: "salesnav-people-results-adapter/1",
    metadata: null,
  }).person;
}

test("the submitted capture carries a null profile URL rather than a fabricated one", () => {
  const person = personOf(capture("results-normal.html").records[0]);
  assert.equal(person.linkedin_profile_url, null);
  assert.equal(person.linkedin_public_identifier, null);
  assert.ok(person.salesnav_lead_url.includes("/sales/lead/"));
});

test("the submitted capture declares the member id under its own property", () => {
  const person = personOf(capture("results-normal.html").records[0]);
  assert.match(person.salesnav_member_id, /^[A-Za-z0-9_-]{3,128}$/);
  // and it is not smuggled into an identity field
  assert.notEqual(person.linkedin_profile_url, person.salesnav_member_id);
  assert.notEqual(person.linkedin_public_identifier, person.salesnav_member_id);
});

test("an observed row submits the real handle as the public identifier", () => {
  const person = personOf(capture("results-observed-profile-url.html").records[0]);
  assert.equal(person.linkedin_profile_url, "https://www.linkedin.com/in/dana-observed");
  assert.equal(person.linkedin_public_identifier, "dana-observed");
  assert.equal(person.salesnav_member_id, "ACwAAAQ2zzz");
});

test("the capture contract accepts the member id and still validates", () => {
  const rec = capture("results-normal.html").records[0];
  const submission = contactSchema.buildSubmission({
    clientSubmissionId: "22222222-2222-4222-8222-222222222222",
    captureMode: constants.CAPTURE_MODES.SALESNAV_PEOPLE_SEARCH,
    submittedAt: "2026-07-27T00:00:00.000Z",
    extensionVersion: "2.0.0",
    metadata: { labels: [], note: null },
    contacts: [
      contactSchema.buildResultRowCapture({
        record: rec,
        clientCaptureId: "33333333-3333-4333-8333-333333333333",
        capturedAt: "2026-07-27T00:00:00.000Z",
        sourceSearchUrl: "https://www.linkedin.com/sales/search/people",
        adapterVersion: "salesnav-people-results-adapter/1",
        metadata: null,
      }),
    ],
  });
  const result = contactSchema.validateSubmission(submission);
  assert.deepEqual(result.errors || [], []);
  assert.equal(result.valid, true);
});

// --- 5. a person profile capture is untouched by any of this -----------------

test("nothing here changes a normal person-profile capture", () => {
  const person = contactSchema.buildProfileCapture({
    clientCaptureId: "44444444-4444-4444-8444-444444444444",
    extraction: {
      capturedAt: "2026-07-27T00:00:00.000Z",
      sourceUrl: "https://www.linkedin.com/in/danawhitfield",
      profile: {
        full_name: "Dana Whitfield",
        linkedin_profile_url: "https://www.linkedin.com/in/danawhitfield",
        public_identifier: "danawhitfield",
        warnings: [],
      },
      experiences: [],
    },
    metadata: null,
  }).person;

  assert.equal(person.linkedin_profile_url, "https://www.linkedin.com/in/danawhitfield");
  assert.equal(person.linkedin_public_identifier, "danawhitfield");
  assert.equal(person.salesnav_lead_url, null);
  assert.equal(person.salesnav_member_id, null);
});

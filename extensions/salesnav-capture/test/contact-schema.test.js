"use strict";
/**
 * Contact-first payload construction and validation (DAT-013).
 *
 * These prove the product rules the refactor exists for: a submission carries
 * people with optional filing context, an uncertain identity stays uncertain,
 * labels and notes are bounded, and the extension refuses to send anything the
 * committed contract would reject.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const contactSchema = require("../src/common/contact-schema.js");
const constants = require("../src/common/constants.js");
const profileExtraction = require("../src/common/profile-extraction.js");
const extraction = require("../src/common/extraction.js");
const { loadFixtureDoc, SUPPORTED_URL } = require("./helpers.js");

const PROFILE_URL = "https://www.linkedin.com/in/test-profile";
const CAPTURED_AT = "2026-07-26T10:00:00.000Z";

function profileDoc(name) {
  const html = fs.readFileSync(path.join(__dirname, "fixtures-profile", name), "utf8");
  return new JSDOM(html, { url: PROFILE_URL }).window.document;
}

function profileCapture(overrides) {
  const doc = profileDoc("profile-basic.html");
  const ex = profileExtraction.extractProfile(doc, {
    sourceUrl: PROFILE_URL,
    capturedAt: CAPTURED_AT,
  });
  return contactSchema.buildProfileCapture(
    Object.assign(
      {
        extraction: ex,
        clientCaptureId: "11111111-2222-3333-4444-555555555555",
        pageTitle: "Test Profile | LinkedIn",
        metadata: null,
      },
      overrides || {}
    )
  );
}

function resultRowCaptures() {
  const doc = loadFixtureDoc("results-normal.html", SUPPORTED_URL);
  const page = extraction.extractPage(doc, {
    sourceSearchUrl: SUPPORTED_URL,
    capturedAt: CAPTURED_AT,
  });
  return page.records.map((rec, i) =>
    contactSchema.buildResultRowCapture({
      record: rec,
      clientCaptureId: `row-capture-id-${i}-0000-0000`,
      adapterVersion: "salesnav-people-results-adapter/1",
      metadata: null,
    })
  );
}

function submission(overrides) {
  return contactSchema.buildSubmission(
    Object.assign(
      {
        clientSubmissionId: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        captureMode: constants.CAPTURE_MODES.LINKEDIN_PROFILE,
        submittedAt: CAPTURED_AT,
        extensionVersion: "2.0.0",
        metadata: null,
        contacts: [profileCapture()],
      },
      overrides || {}
    )
  );
}

// --- Campaign filing is optional --------------------------------------------

test("a built submission defaults to Contact-only Campaign filing", () => {
  const payload = submission();
  assert.equal(payload.campaign_id, null);
  assert.equal(contactSchema.validateSubmission(payload).valid, true);
});

test("a valid optional Campaign id is accepted", () => {
  const payload = submission({ campaignId: "11111111-2222-4333-8444-555555555555" });
  assert.equal(payload.campaign_id, "11111111-2222-4333-8444-555555555555");
  assert.equal(contactSchema.validateSubmission(payload).valid, true);
});

test("a malformed Campaign id is rejected", () => {
  const payload = submission({ campaignId: "camp-1" });
  const r = contactSchema.validateSubmission(payload);
  assert.equal(r.valid, false);
  assert.ok(r.errors.some((e) => /campaign_id must be a UUID/.test(e)));
});

// --- Profile captures --------------------------------------------------------

test("a profile capture carries the person, the current-role hint, and experience", () => {
  const capture = profileCapture();
  assert.equal(capture.source.surface, "linkedin_person_profile");
  assert.equal(capture.source.operator_triggered, true);
  assert.equal(capture.person.linkedin_profile_url, PROFILE_URL);
  assert.equal(capture.person.linkedin_public_identifier, "test-profile");
  assert.ok(capture.person.full_name);
  assert.equal(capture.person.first_name, capture.person.full_name.split(" ")[0]);
  assert.ok(capture.experience_observations.length > 0);
  const current = capture.experience_observations.find((e) => e.is_current === true);
  if (current) {
    assert.equal(capture.current_employment_hint.title, current.job_title);
    assert.equal(capture.current_employment_hint.company_name, current.company_name);
  }
  assert.equal(contactSchema.validateSubmission(submission()).valid, true);
});

test("excluding the experience section removes it from the payload but keeps the person", () => {
  const capture = profileCapture({ excludedSections: ["experience"] });
  assert.deepEqual(capture.experience_observations, []);
  assert.deepEqual(capture.extraction.excluded_sections, ["experience"]);
  assert.ok(capture.person.full_name);
  assert.equal(contactSchema.validateSubmission(submission({ contacts: [capture] })).valid, true);
});

test("the verbatim adapter output travels as the raw snapshot", () => {
  const capture = profileCapture();
  assert.equal(typeof capture.raw_snapshot, "object");
  assert.ok(capture.raw_snapshot.profile || capture.raw_snapshot.status);
});

// --- Sales Navigator result rows ---------------------------------------------

test("result rows become captures with an employment hint and no experience history", () => {
  const captures = resultRowCaptures();
  assert.ok(captures.length >= 2);
  for (const capture of captures) {
    assert.equal(capture.source.surface, "salesnav_people_results");
    assert.deepEqual(capture.experience_observations, []);
    assert.equal(typeof capture.current_employment_hint, "object");
    assert.equal(capture.raw_snapshot._stableKey, undefined, "internal review aids are stripped");
  }
  const payload = submission({
    captureMode: constants.CAPTURE_MODES.SALESNAV_PEOPLE_SEARCH,
    contacts: captures,
  });
  assert.deepEqual(contactSchema.validateSubmission(payload).errors, []);
});

test("a row with no main profile URL keeps identity uncertain instead of repairing it", () => {
  const capture = contactSchema.buildResultRowCapture({
    record: {
      rawFullName: "Dana Whitfield",
      title: "Head of Operations",
      companyName: "Northwind Logistics",
      linkedinProfileUrl: null,
      salesNavLeadUrl: "https://www.linkedin.com/sales/lead/ACwAAAB1x9k",
      capturedAt: CAPTURED_AT,
      warnings: [{ code: "missing_field", field: "linkedinProfileUrl" }],
    },
    clientCaptureId: "row-capture-id-x-0000-0000",
    metadata: null,
  });
  assert.equal(capture.person.linkedin_profile_url, null);
  assert.equal(capture.person.linkedin_public_identifier, null);
  assert.equal(capture.person.salesnav_lead_url, "https://www.linkedin.com/sales/lead/ACwAAAB1x9k");
  assert.equal(capture.extraction.status, "partial");
});

test("a Sales Navigator lead URL is never promoted into the profile URL slot", () => {
  const capture = contactSchema.buildResultRowCapture({
    record: {
      rawFullName: "Dana Whitfield",
      linkedinProfileUrl: "https://www.linkedin.com/sales/lead/ACwAAAB1x9k",
      salesNavLeadUrl: "https://www.linkedin.com/sales/lead/ACwAAAB1x9k",
      capturedAt: CAPTURED_AT,
      warnings: [],
    },
    clientCaptureId: "row-capture-id-y-0000-0000",
    metadata: null,
  });
  assert.equal(capture.person.linkedin_profile_url, null);
});

// --- Identity validation -----------------------------------------------------

test("a capture with no URL and no name is refused", () => {
  const capture = profileCapture();
  capture.person.linkedin_profile_url = null;
  capture.person.salesnav_lead_url = null;
  capture.person.full_name = null;
  const r = contactSchema.validateSubmission(submission({ contacts: [capture] }));
  assert.equal(r.valid, false);
  assert.ok(r.errors.some((e) => /no profile URL, lead URL, or name/.test(e)));
});

test("a deceptive profile host is refused", () => {
  for (const bad of [
    "https://linkedin.com.evil.example/in/person",
    "http://www.linkedin.com/in/person",
    "https://www.linkedin.com/in/person/details/experience",
    "https://www.linkedin.com/company/acme",
  ]) {
    const capture = profileCapture();
    capture.person.linkedin_profile_url = bad;
    const r = contactSchema.validateSubmission(submission({ contacts: [capture] }));
    assert.equal(r.valid, false, `${bad} must be refused`);
  }
});

test("a repeated client_capture_id inside one submission is refused", () => {
  const a = profileCapture();
  const b = profileCapture();
  const r = contactSchema.validateSubmission(submission({ contacts: [a, b] }));
  assert.equal(r.valid, false);
  assert.ok(r.errors.some((e) => /is repeated in this submission/.test(e)));
});

// --- Labels and notes ---------------------------------------------------------

test("labels are optional and default to an empty list", () => {
  const payload = submission();
  assert.deepEqual(payload.operator_metadata, { labels: [], note: null });
  assert.equal(contactSchema.validateSubmission(payload).valid, true);
});

test("label sanitizing collapses whitespace and case-insensitive repeats", () => {
  assert.deepEqual(
    contactSchema.sanitizeLabels(["Healthcare", " healthcare ", "Venture  Capital", "", null, 7]),
    ["Healthcare", "Venture Capital"]
  );
});

test("labels are bounded in count and length", () => {
  const many = Array.from({ length: 60 }, (_, i) => `label-${i}`);
  assert.equal(contactSchema.sanitizeLabels(many).length, constants.LIMITS.MAX_LABELS);
  const long = "x".repeat(200);
  assert.equal(
    contactSchema.sanitizeLabels([long])[0].length,
    constants.LIMITS.MAX_LABEL_LENGTH
  );
});

test("an empty note becomes null rather than an empty string", () => {
  assert.equal(contactSchema.sanitizeNote("   "), null);
  assert.equal(contactSchema.sanitizeNote("Met at SaaStr. "), "Met at SaaStr.");
  assert.equal(contactSchema.sanitizeNote("y".repeat(5000)).length, constants.LIMITS.MAX_NOTE_LENGTH);
});

test("a note and labels survive into the submission envelope", () => {
  const payload = submission({
    metadata: { labels: ["Healthcare", "Healthcare"], note: " Follow up after September. " },
  });
  assert.deepEqual(payload.operator_metadata.labels, ["Healthcare"]);
  assert.equal(payload.operator_metadata.note, "Follow up after September.");
  assert.equal(contactSchema.validateSubmission(payload).valid, true);
});

// --- Envelope rules -----------------------------------------------------------

test("an empty submission is refused", () => {
  const r = contactSchema.validateSubmission(submission({ contacts: [] }));
  assert.equal(r.valid, false);
  assert.ok(r.errors.some((e) => /contacts must not be empty/.test(e)));
});

test("an unknown capture mode is refused", () => {
  const r = contactSchema.validateSubmission(submission({ captureMode: "autopilot" }));
  assert.equal(r.valid, false);
});

test("operator_triggered may never be false", () => {
  const capture = profileCapture();
  capture.source.operator_triggered = false;
  const r = contactSchema.validateSubmission(submission({ contacts: [capture] }));
  assert.equal(r.valid, false);
});

test("serialization reports its own size bound", () => {
  const s = contactSchema.serializePayload(submission());
  assert.ok(s.bytes > 0);
  assert.equal(s.withinLimit, true);
  assert.equal(JSON.parse(s.json).schema_version, constants.CONTACT_CAPTURE_SCHEMA_VERSION);
});

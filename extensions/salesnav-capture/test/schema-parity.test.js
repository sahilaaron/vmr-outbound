"use strict";
/**
 * Parity proof between the dependency-free extension validators and the
 * COMMITTED JSON Schemas (docs/profile-intake.schema.json,
 * docs/company-intake.schema.json).
 *
 * A minimal JSON-Schema-subset evaluator (covering exactly the constructs the
 * two committed schemas use) runs each corpus payload through the schema file,
 * and the result is compared with the extension validator:
 *
 *   1. SOUNDNESS (always): any payload the extension validator ACCEPTS must be
 *      schema-valid. The extension can never send something the backend
 *      contract rejects.
 *   2. AGREEMENT (corpus): every representative valid/invalid payload produces
 *      the same accept/reject under both, except cases explicitly marked
 *      `validatorStricter` (parsed-URL rules where the extension is
 *      deliberately harder to fool than a regex can express) — and for those
 *      the validator must REJECT while remaining sound.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const profileExtraction = require("../src/common/profile-extraction.js");
const profileSchema = require("../src/common/profile-schema.js");

const DOCS = path.join(__dirname, "..", "docs");
const PROFILE_SCHEMA = JSON.parse(fs.readFileSync(path.join(DOCS, "profile-intake.schema.json"), "utf8"));
const COMPANY_SCHEMA = JSON.parse(fs.readFileSync(path.join(DOCS, "company-intake.schema.json"), "utf8"));

// The minimal JSON-Schema-subset evaluator lives in test/json-schema-subset.js
// so the DAT-013 contact-first parity test proves the same property with the
// same evaluator rather than a second, subtly different one.
const { evaluate, collectKeywords, SUPPORTED } = require("./json-schema-subset.js");

test("the parity evaluator covers every keyword the committed schemas use", () => {
  for (const schema of [PROFILE_SCHEMA, COMPANY_SCHEMA]) {
    const found = new Set();
    collectKeywords(schema, found);
    const unknown = [...found].filter((k) => !SUPPORTED.has(k));
    assert.deepEqual(unknown, [], `unsupported keywords: ${unknown}`);
  }
});

// ---- Corpus -----------------------------------------------------------------

function baseProfilePayload() {
  const url = "https://www.linkedin.com/in/test-profile";
  const html = fs.readFileSync(
    path.join(__dirname, "fixtures-profile", "profile-basic.html"), "utf8");
  const doc = new JSDOM(html, { url }).window.document;
  const extraction = profileExtraction.extractProfile(doc, {
    sourceUrl: url,
    capturedAt: "2026-07-24T10:00:00.000Z",
  });
  return profileSchema.buildProfilePayload({
    extraction,
    clientCaptureId: "11111111-2222-3333-4444-555555555555",
    campaignId: "campaign-1",
    extensionVersion: "1.0.0",
  });
}

// Representative company payload (the company ADAPTER ships in a later PR of
// the stack; the CONTRACT is defined here, so parity uses a hand-built payload).
const COMPANY_EXAMPLE = {
  schema_version: "linkedin-company-capture/1.0.0",
  client_capture_id: "11111111-2222-3333-4444-555555555555",
  campaign_id: null,
  captured_at: "2026-07-24T10:00:00.000Z",
  source: "chrome-extension:linkedin-company-capture",
  source_url: "https://www.linkedin.com/company/meridian-works/about/",
  extraction: {
    adapter_version: "linkedin-company-profile-adapter/1",
    extension_version: "1.0.0",
    status: "ok",
    surface: "linkedin_company_profile",
    missing_sections: [],
    excluded_sections: [],
    page_warnings: [],
  },
  company: {
    company_linkedin_url: "https://www.linkedin.com/company/meridian-works",
    company_linkedin_id: "meridian-works",
    name: "Meridian Works",
    website: "https://meridianworks.example",
    industry: "Facilities Services",
    size_range: "51-200 employees",
    employee_count_raw: "142 associated members",
    employee_count: 142,
    headquarters_text: "Austin, Texas, United States",
    founded_year: 2011,
    founded_raw: "2011",
    specialties: "Workplace ops, Vendor management",
    observed_at: "2026-07-24T10:00:00.000Z",
    raw_lines: ["Meridian Works"],
    warnings: [],
  },
};

function withProfile(fn) {
  const p = JSON.parse(JSON.stringify(baseProfilePayload()));
  fn(p);
  return p;
}
function withCompany(fn) {
  const p = JSON.parse(JSON.stringify(COMPANY_EXAMPLE));
  fn(p);
  return p;
}

// Each corpus entry: { name, kind, payload, expectValid, validatorStricter? }
const CORPUS = [
  { name: "valid built profile payload", kind: "profile", payload: baseProfilePayload(), expectValid: true },
  { name: "valid committed company example", kind: "company", payload: COMPANY_EXAMPLE, expectValid: true },
  { name: "profile with campaign null", kind: "profile", payload: withProfile((p) => { p.campaign_id = null; }), expectValid: true },
  { name: "empty experiences allowed", kind: "profile", payload: withProfile((p) => { p.experiences = []; }), expectValid: true },
  { name: "company /about URL", kind: "company", payload: withCompany((p) => { p.company.company_linkedin_url = "https://www.linkedin.com/company/meridian-works/about"; }), expectValid: true },

  { name: "wrong schema_version", kind: "profile", payload: withProfile((p) => { p.schema_version = "linkedin-profile-capture/2.0.0"; }), expectValid: false },
  { name: "wrong source", kind: "profile", payload: withProfile((p) => { p.source = "someone-else"; }), expectValid: false },
  { name: "short capture id", kind: "profile", payload: withProfile((p) => { p.client_capture_id = "short"; }), expectValid: false },
  { name: "overlong capture id", kind: "profile", payload: withProfile((p) => { p.client_capture_id = "x".repeat(129); }), expectValid: false },
  { name: "wrong extraction.status", kind: "profile", payload: withProfile((p) => { p.extraction.status = "done"; }), expectValid: false },
  { name: "wrong extraction.surface", kind: "profile", payload: withProfile((p) => { p.extraction.surface = "linkedin_company_profile"; }), expectValid: false },
  { name: "missing excluded_sections", kind: "profile", payload: withProfile((p) => { delete p.extraction.excluded_sections; }), expectValid: false },
  { name: "non-string missing_sections item", kind: "profile", payload: withProfile((p) => { p.extraction.missing_sections = [7]; }), expectValid: false },
  { name: "non-string raw_lines item", kind: "profile", payload: withProfile((p) => { p.profile.raw_lines = [null]; }), expectValid: false },
  { name: "non-object warning item", kind: "profile", payload: withProfile((p) => { p.profile.warnings = ["w"]; }), expectValid: false },
  { name: "undeclared top-level key", kind: "profile", payload: withProfile((p) => { p.extra = 1; }), expectValid: false },
  { name: "undeclared nested profile key", kind: "profile", payload: withProfile((p) => { p.profile.email = "x@example.com"; }), expectValid: false },
  { name: "undeclared experience key", kind: "profile", payload: withProfile((p) => { p.experiences[0].note = "x"; }), expectValid: false },
  { name: "date part with extra key", kind: "profile", payload: withProfile((p) => { p.experiences[0].start_date = { year: 2021, month: 1, day: 2 }; }), expectValid: false },
  { name: "date part year out of range", kind: "profile", payload: withProfile((p) => { p.experiences[0].start_date = { year: 20, month: 1 }; }), expectValid: false },
  { name: "bad layout enum", kind: "profile", payload: withProfile((p) => { p.experiences[0].layout = "nested"; }), expectValid: false },
  { name: "negative connection count", kind: "profile", payload: withProfile((p) => { p.profile.connection_count = -1; }), expectValid: false },
  { name: "non-linkedin profile URL", kind: "profile", payload: withProfile((p) => { p.profile.linkedin_profile_url = "https://example.com/in/person"; }), expectValid: false },
  { name: "profile sub-route URL", kind: "profile", payload: withProfile((p) => { p.profile.linkedin_profile_url = "https://www.linkedin.com/in/p/details/experience"; }), expectValid: false },

  { name: "company /school/ URL", kind: "company", payload: withCompany((p) => { p.company.company_linkedin_url = "https://www.linkedin.com/school/some-university"; }), expectValid: false },
  { name: "company deceptive host", kind: "company", payload: withCompany((p) => { p.company.company_linkedin_url = "https://linkedin.com.evil.example/company/x"; }), expectValid: false },
  { name: "company founded_year out of range", kind: "company", payload: withCompany((p) => { p.company.founded_year = 22; }), expectValid: false },
  { name: "company undeclared nested key", kind: "company", payload: withCompany((p) => { p.company.revenue = 1; }), expectValid: false },
  { name: "company missing required field", kind: "company", payload: withCompany((p) => { delete p.company.headquarters_text; }), expectValid: false },

  // Parsed-URL strictness the schema regex cannot fully express: the schema
  // pattern is anchored and already rejects these hosts, but URL-encoding
  // tricks may slip a regex; the validator must reject regardless.
  {
    name: "userinfo@ deceptive URL",
    kind: "profile",
    payload: withProfile((p) => { p.profile.linkedin_profile_url = "https://linkedin.com@evil.example/in/person"; }),
    expectValid: false,
    validatorStricter: true,
  },
];

test("soundness + corpus agreement between validator and committed schemas", () => {
  for (const entry of CORPUS) {
    const schema = entry.kind === "profile" ? PROFILE_SCHEMA : COMPANY_SCHEMA;
    const validate = entry.kind === "profile"
      ? profileSchema.validateProfilePayload
      : profileSchema.validateCompanyPayload;
    const schemaOk = evaluate(schema, entry.payload);
    const validatorOk = validate(entry.payload).valid;

    // 1. Soundness: validator acceptance implies schema validity.
    if (validatorOk) {
      assert.ok(schemaOk, `UNSOUND: validator accepted schema-invalid payload (${entry.name})`);
    }
    // 2. Expected outcome under the validator.
    assert.equal(validatorOk, entry.expectValid, `validator disagreed on: ${entry.name}`);
    // 3. Schema agreement (except documented stricter-URL cases, where the
    //    validator must reject even if the schema regex would allow it).
    if (entry.validatorStricter) {
      assert.equal(validatorOk, false, `stricter case must be rejected: ${entry.name}`);
    } else {
      assert.equal(schemaOk, entry.expectValid, `schema disagreed on: ${entry.name}`);
    }
  }
});

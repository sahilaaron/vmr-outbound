"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const profileExtraction = require("../src/common/profile-extraction.js");
const profileSchema = require("../src/common/profile-schema.js");
const { COMPANY_SCHEMA_VERSION, COMPANY_SOURCE_IDENTIFIER } = require("../src/common/constants.js");

const FIXTURE_DIR = path.join(__dirname, "fixtures-profile");
const PROFILE_URL = "https://www.linkedin.com/in/test-profile";

function validProfilePayload() {
  const html = fs.readFileSync(path.join(FIXTURE_DIR, "profile-basic.html"), "utf8");
  const doc = new JSDOM(html, { url: PROFILE_URL }).window.document;
  const extraction = profileExtraction.extractProfile(doc, {
    sourceUrl: PROFILE_URL,
    capturedAt: "2026-07-24T10:00:00.000Z",
  });
  return profileSchema.buildProfilePayload({
    extraction,
    clientCaptureId: "11111111-2222-3333-4444-555555555555",
    campaignId: null,
    extensionVersion: "1.0.0",
  });
}

function validCompanyPayload() {
  return {
    schema_version: COMPANY_SCHEMA_VERSION,
    client_capture_id: "11111111-2222-3333-4444-555555555555",
    campaign_id: null,
    captured_at: "2026-07-24T10:00:00.000Z",
    source: COMPANY_SOURCE_IDENTIFIER,
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
      headquarters_text: "Austin, Texas",
      founded_year: 2011,
      founded_raw: "2011",
      specialties: null,
      observed_at: "2026-07-24T10:00:00.000Z",
      raw_lines: ["Meridian Works"],
      warnings: [],
    },
  };
}

function mutateProfile(fn) {
  const p = JSON.parse(JSON.stringify(validProfilePayload()));
  fn(p);
  return profileSchema.validateProfilePayload(p);
}

function mutateCompany(fn) {
  const p = JSON.parse(JSON.stringify(validCompanyPayload()));
  fn(p);
  return profileSchema.validateCompanyPayload(p);
}

test("baselines are valid", () => {
  assert.deepEqual(profileSchema.validateProfilePayload(validProfilePayload()).errors, []);
  assert.deepEqual(profileSchema.validateCompanyPayload(validCompanyPayload()).errors, []);
});

// ---- URL strictness: parsed, never substring -------------------------------

test("company /school/ URLs are rejected (not a supported surface)", () => {
  const r = mutateCompany((p) => {
    p.company.company_linkedin_url = "https://www.linkedin.com/school/some-university";
  });
  assert.ok(!r.valid);
  assert.ok(r.errors.some((e) => e.includes("company_linkedin_url")));
});

test("deceptive hosts containing LinkedIn text are rejected", () => {
  const deceptive = [
    "https://linkedin.com.evil.example/in/person",
    "https://example.com/linkedin.com/in/person",
    "https://evil.example/company/linkedin.com",
    "http://www.linkedin.com/in/person", // https required
    "https://notlinkedin.com/in/person",
  ];
  for (const url of deceptive) {
    assert.ok(
      !mutateProfile((p) => { p.profile.linkedin_profile_url = url; }).valid,
      `profile accepted deceptive URL: ${url}`
    );
  }
  const deceptiveCompany = [
    "https://linkedin.com.evil.example/company/x",
    "https://evil.example/company/linkedin.com",
    "https://example.com/linkedin.com/company/x",
  ];
  for (const url of deceptiveCompany) {
    assert.ok(
      !mutateCompany((p) => { p.company.company_linkedin_url = url; }).valid,
      `company accepted deceptive URL: ${url}`
    );
  }
});

test("profile sub-routes and query/fragment context are rejected", () => {
  for (const url of [
    "https://www.linkedin.com/in/person/details/experience/",
    "https://www.linkedin.com/in/person/recent-activity/all/",
    "https://www.linkedin.com/in/person?trk=share",
    "https://www.linkedin.com/in/person#about",
    "https://www.linkedin.com/in/",
  ]) {
    assert.ok(
      !mutateProfile((p) => { p.profile.linkedin_profile_url = url; }).valid,
      `accepted sub-route/context URL: ${url}`
    );
  }
  // The exact allowed shapes still pass.
  assert.ok(mutateProfile((p) => { p.profile.linkedin_profile_url = "https://linkedin.com/in/person"; }).valid);
  assert.ok(mutateProfile((p) => { p.profile.linkedin_profile_url = "https://www.linkedin.com/in/person/"; }).valid);
  assert.ok(mutateCompany((p) => { p.company.company_linkedin_url = "https://www.linkedin.com/company/x/about"; }).valid);
  assert.ok(!mutateCompany((p) => { p.company.company_linkedin_url = "https://www.linkedin.com/company/x/posts"; }).valid);
});

test("validator URL rules agree with PageSurfaceDetector routes", () => {
  const surface = require("../src/common/surface.js");
  const urls = [
    "https://www.linkedin.com/in/person",
    "https://www.linkedin.com/in/person/",
    "https://www.linkedin.com/in/person/details/experience/",
    "https://linkedin.com.evil.example/in/person",
    "https://www.linkedin.com/company/x",
    "https://www.linkedin.com/school/x",
  ];
  for (const url of urls) {
    // Everything the validator accepts, the surface detector also treats as
    // the corresponding supported route (validator is never more permissive).
    if (profileSchema.isValidProfileIdentityUrl(url)) {
      assert.ok(surface.isSupportedPersonProfileUrl(url), `surface rejects validator-accepted ${url}`);
    }
    if (profileSchema.isValidCompanyIdentityUrl(url)) {
      assert.ok(surface.isSupportedCompanyProfileUrl(url), `surface rejects validator-accepted ${url}`);
    }
  }
});

// ---- Enum / const / bounds strictness ---------------------------------------

test("wrong extraction.surface is rejected", () => {
  assert.ok(!mutateProfile((p) => { p.extraction.surface = "linkedin_company_profile"; }).valid);
  assert.ok(!mutateCompany((p) => { p.extraction.surface = "linkedin_person_profile"; }).valid);
});

test("invalid extraction.status is rejected", () => {
  for (const bad of ["complete", "failed", "", null]) {
    assert.ok(!mutateProfile((p) => { p.extraction.status = bad; }).valid, String(bad));
  }
});

test("client_capture_id longer than the schema maximum is rejected", () => {
  const r = mutateProfile((p) => { p.client_capture_id = "x".repeat(129); });
  assert.ok(!r.valid);
  assert.ok(mutateProfile((p) => { p.client_capture_id = "x".repeat(128); }).valid);
});

test("missing extraction.excluded_sections is rejected", () => {
  const r = mutateProfile((p) => { delete p.extraction.excluded_sections; });
  assert.ok(!r.valid);
  assert.ok(r.errors.some((e) => e.includes("excluded_sections")));
});

// ---- Array item types -------------------------------------------------------

test("non-string items in missing/excluded sections and raw lines are rejected", () => {
  assert.ok(!mutateProfile((p) => { p.extraction.missing_sections = ["ok", 42]; }).valid);
  assert.ok(!mutateProfile((p) => { p.extraction.excluded_sections = [{}]; }).valid);
  assert.ok(!mutateProfile((p) => { p.profile.raw_lines = ["line", null]; }).valid);
  assert.ok(!mutateProfile((p) => { p.experiences[0].raw_lines = [1]; }).valid);
  assert.ok(!mutateCompany((p) => { p.company.raw_lines = [["nested"]]; }).valid);
});

test("malformed warning items are rejected", () => {
  assert.ok(!mutateProfile((p) => { p.profile.warnings = ["missing_field"]; }).valid);
  assert.ok(!mutateProfile((p) => { p.experiences[0].warnings = [null]; }).valid);
  assert.ok(!mutateProfile((p) => { p.extraction.page_warnings = [3]; }).valid);
  assert.ok(!mutateCompany((p) => { p.company.warnings = ["oops"]; }).valid);
});

// ---- additionalProperties: false --------------------------------------------

test("undeclared top-level properties are rejected", () => {
  assert.ok(!mutateProfile((p) => { p.injected = true; }).valid);
  assert.ok(!mutateCompany((p) => { p.records = []; }).valid);
});

test("undeclared nested properties are rejected wherever the schema forbids them", () => {
  assert.ok(!mutateProfile((p) => { p.extraction.debug = "x"; }).valid);
  assert.ok(!mutateProfile((p) => { p.profile.email = "leak@example.com"; }).valid);
  assert.ok(!mutateProfile((p) => { p.experiences[0].note = "x"; }).valid);
  assert.ok(!mutateProfile((p) => { p.experiences[0].start_date = { year: 2020, month: 1, day: 5 }; }).valid);
  assert.ok(!mutateCompany((p) => { p.company.revenue = "secret"; }).valid);
});

test("date parts require both keys and stay bounded", () => {
  assert.ok(!mutateProfile((p) => { p.experiences[0].start_date = { year: 2020 }; }).valid);
  assert.ok(!mutateProfile((p) => { p.experiences[0].start_date = { year: 20, month: 1 }; }).valid);
  assert.ok(!mutateProfile((p) => { p.experiences[0].start_date = { year: 2020, month: 13 }; }).valid);
});

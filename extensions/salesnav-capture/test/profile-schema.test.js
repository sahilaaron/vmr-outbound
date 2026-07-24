"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const profileExtraction = require("../src/common/profile-extraction.js");
const profileSchema = require("../src/common/profile-schema.js");
const {
  PROFILE_SCHEMA_VERSION,
  COMPANY_SCHEMA_VERSION,
  PROFILE_SOURCE_IDENTIFIER,
  COMPANY_SOURCE_IDENTIFIER,
} = require("../src/common/constants.js");

const FIXTURE_DIR = path.join(__dirname, "fixtures-profile");
const PROFILE_URL = "https://www.linkedin.com/in/test-profile";

function extraction(name) {
  const html = fs.readFileSync(path.join(FIXTURE_DIR, name), "utf8");
  const doc = new JSDOM(html, { url: PROFILE_URL }).window.document;
  return profileExtraction.extractProfile(doc, {
    sourceUrl: PROFILE_URL,
    capturedAt: "2026-07-24T10:00:00.000Z",
  });
}

function buildPayload(name, overrides) {
  return profileSchema.buildProfilePayload(
    Object.assign(
      {
        extraction: extraction(name || "profile-basic.html"),
        clientCaptureId: "11111111-2222-3333-4444-555555555555",
        campaignId: null,
        extensionVersion: "1.0.0",
      },
      overrides || {}
    )
  );
}

test("built profile payload validates against the contract", () => {
  const payload = buildPayload();
  assert.equal(payload.schema_version, PROFILE_SCHEMA_VERSION);
  assert.equal(payload.source, PROFILE_SOURCE_IDENTIFIER);
  const v = profileSchema.validateProfilePayload(payload);
  assert.deepEqual(v.errors, []);
  assert.ok(v.valid);
});

test("payload matches the committed JSON Schema's required envelope", () => {
  const schemaDoc = JSON.parse(
    fs.readFileSync(path.join(__dirname, "..", "docs", "profile-intake.schema.json"), "utf8")
  );
  const payload = buildPayload();
  for (const key of schemaDoc.required) {
    assert.ok(key in payload, `payload missing required key ${key}`);
  }
  // No extra top-level keys beyond the schema's properties.
  for (const key of Object.keys(payload)) {
    assert.ok(key in schemaDoc.properties, `payload has undeclared key ${key}`);
  }
  assert.equal(schemaDoc.properties.schema_version.const, PROFILE_SCHEMA_VERSION);
});

test("experience history is nested, never flattened", () => {
  const payload = buildPayload("profile-chained.html");
  assert.ok(Array.isArray(payload.experiences));
  assert.equal(payload.experiences.length, 3);
  for (const e of payload.experiences) {
    assert.equal(typeof e, "object");
    assert.ok(Array.isArray(e.raw_lines));
  }
});

test("operator exclusion of the experience section is honored and recorded", () => {
  const payload = buildPayload("profile-basic.html", { excludedSections: ["experience"] });
  assert.equal(payload.experiences.length, 0);
  assert.deepEqual(payload.extraction.excluded_sections, ["experience"]);
  assert.ok(profileSchema.validateProfilePayload(payload).valid);
});

test("validation rejects wrong version, short capture id, and bad profile URL", () => {
  const payload = buildPayload();

  let bad = JSON.parse(JSON.stringify(payload));
  bad.schema_version = "linkedin-profile-capture/2.0.0";
  assert.ok(!profileSchema.validateProfilePayload(bad).valid);

  bad = JSON.parse(JSON.stringify(payload));
  bad.client_capture_id = "short";
  assert.ok(!profileSchema.validateProfilePayload(bad).valid);

  bad = JSON.parse(JSON.stringify(payload));
  bad.profile.linkedin_profile_url = "https://example.com/in/whoever";
  assert.ok(!profileSchema.validateProfilePayload(bad).valid);

  bad = JSON.parse(JSON.stringify(payload));
  bad.experiences[0].start_date = { year: 20, month: 1 };
  assert.ok(!profileSchema.validateProfilePayload(bad).valid);
});

test("campaign id empty string becomes null; explicit id is kept", () => {
  assert.equal(buildPayload("profile-basic.html", { campaignId: "" }).campaign_id, null);
  assert.equal(
    buildPayload("profile-basic.html", { campaignId: "camp-1" }).campaign_id,
    "camp-1"
  );
});

test("company payload validator enforces the company contract", () => {
  const companyPayload = {
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
      specialties: "Workplace ops, Vendor management",
      observed_at: "2026-07-24T10:00:00.000Z",
      raw_lines: ["Meridian Works"],
      warnings: [],
    },
  };
  const v = profileSchema.validateCompanyPayload(companyPayload);
  assert.deepEqual(v.errors, []);

  const bad = JSON.parse(JSON.stringify(companyPayload));
  bad.company.company_linkedin_url = "https://www.linkedin.com/in/person";
  assert.ok(!profileSchema.validateCompanyPayload(bad).valid);

  const badYear = JSON.parse(JSON.stringify(companyPayload));
  badYear.company.founded_year = 22;
  assert.ok(!profileSchema.validateCompanyPayload(badYear).valid);
});

test("serializePayload enforces the size limit", () => {
  const payload = buildPayload();
  const s = profileSchema.serializePayload(payload);
  assert.ok(s.withinLimit);
  assert.ok(s.bytes > 100);
});

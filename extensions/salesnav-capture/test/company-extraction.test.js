"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const companyExtraction = require("../src/common/company-extraction.js");
const profileSchema = require("../src/common/profile-schema.js");
const { CAPTURE_STATUS, SURFACES } = require("../src/common/constants.js");

const FIXTURE_DIR = path.join(__dirname, "fixtures-company");
const ABOUT_URL = "https://www.linkedin.com/company/meridian-works/about/";

function extract(name, url) {
  const html = fs.readFileSync(path.join(FIXTURE_DIR, name), "utf8");
  const doc = new JSDOM(html, { url: url || ABOUT_URL }).window.document;
  return companyExtraction.extractCompany(doc, {
    sourceUrl: url || ABOUT_URL,
    capturedAt: "2026-07-24T10:00:00.000Z",
  });
}

test("about page yields the full firmographic set", () => {
  const r = extract("company-about.html");
  assert.equal(r.surface, SURFACES.COMPANY_PROFILE);
  const c = r.company;
  assert.equal(c.name, "Meridian Works");
  assert.equal(c.company_linkedin_url, "https://www.linkedin.com/company/meridian-works");
  assert.equal(c.company_linkedin_id, "meridian-works");
  assert.equal(c.website, "https://meridianworks.example");
  assert.equal(c.industry, "Facilities Services");
  assert.equal(c.size_range, "51-200 employees");
  assert.equal(c.employee_count_raw, "142 associated members");
  assert.equal(c.employee_count, 142);
  assert.equal(c.headquarters_text, "Austin, Texas, United States");
  assert.equal(c.founded_year, 2011);
  assert.equal(c.founded_raw, "2011");
  assert.match(c.specialties, /Vendor management/);
  assert.ok(c.raw_lines.length > 0);
  assert.equal(r.status, CAPTURE_STATUS.OK);
});

test("home page (no details list) reports the About section missing", () => {
  const url = "https://www.linkedin.com/company/harbor-analytics/";
  const r = extract("company-home.html", url);
  assert.equal(r.status, CAPTURE_STATUS.PARTIAL);
  assert.ok(r.missingSections.includes("about_details"));
  assert.equal(r.company.name, "Harbor Analytics");
  assert.equal(r.company.website, null);
  // Displayed employee count is still captured from the visible text.
  assert.equal(r.company.employee_count_raw, "87 employees");
  assert.equal(r.company.employee_count, 87);
});

test("missing fields stay null with warnings; ambiguous founded text is not parsed", () => {
  const url = "https://www.linkedin.com/company/delta-verify/about/";
  const r = extract("company-minimal.html", url);
  const c = r.company;
  assert.equal(c.website, null);
  assert.equal(c.headquarters_text, null);
  assert.equal(c.founded_raw, "Founded in the early 2000s");
  assert.equal(c.founded_year, null); // never guessed from "2000s"
  assert.ok(c.warnings.some((w) => w.field === "founded_year"));
  assert.ok(c.warnings.some((w) => w.field === "website"));
  assert.equal(r.status, CAPTURE_STATUS.PARTIAL);
});

test("unavailable company page fails visibly", () => {
  const r = extract("company-unavailable.html", "https://www.linkedin.com/company/gone-co/");
  assert.equal(r.status, CAPTURE_STATUS.UNAVAILABLE_PROFILE);
  assert.equal(r.company, null);
});

test("person-profile URLs are rejected in company mode", () => {
  const r = extract("company-about.html", "https://www.linkedin.com/in/morgan-vale");
  assert.equal(r.status, CAPTURE_STATUS.UNSUPPORTED_PAGE);
});

test("company payload built from the adapter validates against the contract", () => {
  const extraction = extract("company-about.html");
  const payload = profileSchema.buildCompanyPayload({
    extraction,
    clientCaptureId: "11111111-2222-3333-4444-555555555555",
    campaignId: null,
    extensionVersion: "1.0.0",
  });
  const v = profileSchema.validateCompanyPayload(payload);
  assert.deepEqual(v.errors, []);
  // Committed schema envelope agreement.
  const schemaDoc = JSON.parse(
    fs.readFileSync(path.join(__dirname, "..", "docs", "company-intake.schema.json"), "utf8")
  );
  for (const key of schemaDoc.required) assert.ok(key in payload, key);
});

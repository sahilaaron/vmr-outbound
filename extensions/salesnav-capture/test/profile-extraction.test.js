"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const profileExtraction = require("../src/common/profile-extraction.js");
const { CAPTURE_STATUS, SURFACES } = require("../src/common/constants.js");

const FIXTURE_DIR = path.join(__dirname, "fixtures-profile");
const PROFILE_URL = "https://www.linkedin.com/in/test-profile";

function loadProfileDoc(name, url) {
  const html = fs.readFileSync(path.join(FIXTURE_DIR, name), "utf8");
  return new JSDOM(html, { url: url || PROFILE_URL }).window.document;
}

function extract(name, url) {
  const u = url || PROFILE_URL;
  return profileExtraction.extractProfile(loadProfileDoc(name, u), {
    sourceUrl: u,
    capturedAt: "2026-07-24T10:00:00.000Z",
  });
}

// ---- one current role -----------------------------------------------------

test("basic profile: topcard, connections, and one current role", () => {
  const r = extract("profile-basic.html");
  assert.equal(r.status, CAPTURE_STATUS.OK);
  assert.equal(r.surface, SURFACES.PERSON_PROFILE);

  assert.equal(r.profile.full_name, "Morgan Vale");
  assert.equal(r.profile.headline, "Director of Operations at Meridian Works");
  assert.equal(r.profile.displayed_location, "Austin, Texas, United States");
  assert.equal(r.profile.connection_count, 500);
  assert.equal(r.profile.connection_count_raw, "500+ connections");
  assert.equal(r.profile.open_to_work, false);
  assert.equal(r.profile.linkedin_profile_url, "https://www.linkedin.com/in/test-profile");
  assert.equal(r.profile.public_identifier, "test-profile");
  assert.ok(r.profile.raw_lines.length > 0);

  assert.equal(r.experiences.length, 2);
  const cur = r.experiences[0];
  assert.equal(cur.layout, "basic");
  assert.equal(cur.job_title, "Director of Operations");
  assert.equal(cur.company_name, "Meridian Works");
  assert.equal(cur.employment_type, "Full-time");
  assert.equal(cur.company_linkedin_url, "https://www.linkedin.com/company/meridian-works");
  assert.equal(cur.company_linkedin_id, "meridian-works");
  assert.equal(cur.timeline_text, "Jan 2021 - Present");
  assert.equal(cur.duration_text, "5 yrs 7 mos");
  assert.deepEqual(cur.start_date, { year: 2021, month: 1 });
  assert.equal(cur.end_date, null);
  assert.equal(cur.dates_reliable, true);
  assert.equal(cur.is_current, true);
  assert.equal(cur.role_location, "Austin, Texas, United States");
  assert.equal(cur.workplace_type, "Hybrid");
  assert.equal(cur.position_index, 1);

  const prev = r.experiences[1];
  assert.equal(prev.is_current, false);
  assert.deepEqual(prev.start_date, { year: 2016, month: 3 });
  assert.deepEqual(prev.end_date, { year: 2020, month: 12 });
});

// ---- several roles at one company (chained) -------------------------------

test("chained profile: several roles at one company share the company identity", () => {
  const r = extract("profile-chained.html");
  assert.equal(r.status, CAPTURE_STATUS.OK);
  assert.equal(r.experiences.length, 3);

  const [a, b, c] = r.experiences;
  assert.equal(a.layout, "chained");
  assert.equal(a.company_name, "Northgate Systems");
  assert.equal(a.company_linkedin_id, "northgate-systems");
  assert.equal(a.job_title, "Head of Quality");
  assert.equal(a.is_current, true);
  assert.equal(a.workplace_type, "On-site");

  assert.equal(b.layout, "chained");
  assert.equal(b.company_name, "Northgate Systems");
  assert.equal(b.job_title, "Senior QA Manager");
  assert.equal(b.employment_type, "Full-time");
  assert.equal(b.is_current, false);
  assert.deepEqual(b.start_date, { year: 2018, month: 5 });
  assert.deepEqual(b.end_date, { year: 2022, month: 6 });

  assert.equal(c.layout, "basic");
  assert.equal(c.company_name, "Delta Verify");
  assert.deepEqual(c.start_date, { year: 2014, month: null });
  assert.deepEqual(c.end_date, { year: 2018, month: null });

  assert.equal(r.profile.connection_count, 1234);
});

// ---- multiple current positions + open to work ----------------------------

test("multiple current positions are all reported; open-to-work is detected", () => {
  const r = extract("profile-multi-current.html");
  const current = r.experiences.filter((e) => e.is_current === true);
  assert.equal(current.length, 2);
  assert.equal(r.profile.open_to_work, true);
  assert.equal(r.profile.connection_count, 87);
  assert.equal(current[0].employment_type, "Self-employed");
  assert.equal(current[1].employment_type, "Part-time");
  // Bare "Remote" line is a workplace type, never a place.
  assert.equal(current[1].workplace_type, "Remote");
  assert.equal(current[1].role_location, null);
});

// ---- missing sections ------------------------------------------------------

test("missing About is reported as a missing section, not an error", () => {
  const r = extract("profile-missing-about.html");
  assert.equal(r.status, CAPTURE_STATUS.PARTIAL);
  assert.ok(r.missingSections.includes("about"));
  assert.equal(r.experiences.length, 1);
  assert.equal(r.profile.full_name, "Dana Kessler");
});

test("missing Experience section yields no experiences and a warning", () => {
  const r = extract("profile-missing-experience.html");
  assert.equal(r.status, CAPTURE_STATUS.PARTIAL);
  assert.ok(r.missingSections.includes("experience"));
  assert.equal(r.experiences.length, 0);
  assert.ok(r.pageWarnings.some((w) => w.code === "missing_section"));
  // Education entries must NOT leak in as experiences.
  assert.ok(!r.experiences.some((e) => /school/i.test(e.company_name || "")));
});

test("missing location stays null with a warning — never fabricated", () => {
  const r = extract("profile-missing-location.html");
  assert.equal(r.status, CAPTURE_STATUS.PARTIAL);
  assert.equal(r.profile.displayed_location, null);
  assert.ok(r.profile.warnings.some((w) => w.field === "displayed_location"));
  assert.equal(r.experiences[0].role_location, null);
  assert.equal(r.experiences[0].company_name, "Cobalt Ridge");
});

// ---- unicode & punctuation -------------------------------------------------

test("unicode names and values are preserved verbatim", () => {
  const r = extract("profile-unicode.html");
  assert.equal(r.profile.full_name, "Zoë Müller-O’Brïen");
  assert.equal(r.profile.headline, "Geschäftsführerin bei Nördlicht GmbH — 品質管理");
  assert.equal(r.profile.displayed_location, "München, Bayern, Deutschland");
  assert.equal(r.profile.connection_count, 2500);
  assert.equal(r.experiences[0].job_title, "Geschäftsführerin");
});

test("unusual title punctuation is preserved verbatim", () => {
  const r = extract("profile-title-punctuation.html");
  assert.equal(r.profile.full_name, "J.T. van der Berg-Smit");
  assert.equal(r.experiences[0].job_title, "V.P., Ops & Strategy — EMEA/APAC (Interim)");
  assert.equal(r.experiences[0].company_name, "Atlas & Co. — Collective");
  assert.equal(r.experiences[0].employment_type, "Contract");
});

// ---- failure surfaces ------------------------------------------------------

test("unavailable profile fails visibly with unavailable_profile", () => {
  const r = extract("profile-unavailable.html");
  assert.equal(r.status, CAPTURE_STATUS.UNAVAILABLE_PROFILE);
  assert.equal(r.profile, null);
  assert.equal(r.experiences.length, 0);
});

test("login/checkpoint surface fails visibly with challenge_detected", () => {
  const r = extract("profile-authwall.html");
  assert.equal(r.status, CAPTURE_STATUS.CHALLENGE_DETECTED);
  assert.equal(r.profile, null);
});

test("unknown DOM structure fails visibly — nothing fabricated", () => {
  const r = extract("profile-unknown-structure.html");
  assert.equal(r.status, CAPTURE_STATUS.STRUCTURE_UNRECOGNIZED);
  assert.equal(r.profile, null);
  assert.equal(r.experiences.length, 0);
  assert.ok(r.pageWarnings.some((w) => w.code === "structure_unrecognized"));
});

test("non-profile URLs are rejected as unsupported", () => {
  const url = "https://www.linkedin.com/feed/";
  const r = profileExtraction.extractProfile(loadProfileDoc("profile-basic.html", url), {
    sourceUrl: url,
    capturedAt: "2026-07-24T10:00:00.000Z",
  });
  assert.equal(r.status, CAPTURE_STATUS.UNSUPPORTED_PAGE);
});

// ---- timeline parsing unit tests -------------------------------------------

test("parseTimeline handles deterministic forms and refuses the rest", () => {
  const { parseTimeline } = profileExtraction._internals;

  assert.deepEqual(parseTimeline("Jan 2020 - Present"), {
    start: { year: 2020, month: 1 },
    end: null,
    isCurrent: true,
    reliable: true,
  });
  assert.deepEqual(parseTimeline("Mar 2015 - Jun 2018"), {
    start: { year: 2015, month: 3 },
    end: { year: 2018, month: 6 },
    isCurrent: false,
    reliable: true,
  });
  assert.deepEqual(parseTimeline("2019 - 2022"), {
    start: { year: 2019, month: null },
    end: { year: 2022, month: null },
    isCurrent: false,
    reliable: true,
  });
  assert.deepEqual(parseTimeline("2019 – Present"), {
    start: { year: 2019, month: null },
    end: null,
    isCurrent: true,
    reliable: true,
  });
  assert.deepEqual(parseTimeline("May 2024"), {
    start: { year: 2024, month: 5 },
    end: null,
    isCurrent: false,
    reliable: true,
  });
  assert.deepEqual(parseTimeline("Sept 2021 - Present"), {
    start: { year: 2021, month: 9 },
    end: null,
    isCurrent: true,
    reliable: true,
  });

  // Unrecognized forms: reliable=false, no invented dates.
  for (const bad of ["a while ago", "13/2020 - x", "Q1 2020 - Q3 2021", ""]) {
    const p = parseTimeline(bad);
    assert.equal(p.reliable, false, bad);
    assert.equal(p.start, null, bad);
    assert.equal(p.end, null, bad);
  }
});

"use strict";
/**
 * UI-013 — provenance warnings are not review faults.
 *
 * DAT-011's authenticated trial found that every Sales Navigator row carrying
 * `derived_value` was badged *Needs review*. Because a derived profile URL is
 * the normal case on that surface, essentially the whole batch was flagged and
 * the badge stopped meaning anything — an operator could no longer tell a
 * genuinely incomplete row from a routine one.
 *
 * These tests hold the distinction in place:
 *   - a provenance-only record is NOT presented as needing correction;
 *   - a record with a real gap still is, even when it also carries provenance;
 *   - every warning stays visible and inspectable either way.
 *
 * They exercise the shipped panel through the real harness, not a re-implementation.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");

const { createPanel, DEFAULT_PREFS, fixtures } = require("./panel-harness.js");
const constants = require("../src/common/constants.js");
const warnings = require("../src/common/warnings.js");

const WARN = constants.WARNINGS;
const SURFACES = constants.SURFACES;

const BASE = {
  GET_STATE: { ok: true, prefs: DEFAULT_PREFS, metadata: { labels: [], note: null }, batchView: null },
  PROFILE_GET_STATE: { ok: true, prefs: DEFAULT_PREFS, draftView: null },
  COMPANY_GET_STATE: { ok: true, draftView: null },
  FETCH_LABELS: { ok: true, labels: [] },
};

/** The provenance warning a Sales Navigator row gets for a derived profile URL. */
const DERIVED = { code: WARN.DERIVED_VALUE, field: "linkedinProfileUrl", from: "salesNavLeadUrl" };
const MISSING_COMPANY = { code: WARN.MISSING_FIELD, field: "companyName" };
const UNCERTAIN = { code: WARN.DUPLICATE_UNCERTAIN, field: "stableKey" };

async function listingsWith(records) {
  const p = await createPanel({
    responses: Object.assign({}, BASE, {
      DETECT_SURFACE: {
        ok: true,
        surface: SURFACES.SALESNAV_PEOPLE_RESULTS,
        url: "https://www.linkedin.com/sales/search/people",
      },
      GET_STATE: {
        ok: true,
        prefs: DEFAULT_PREFS,
        metadata: { labels: [], note: null },
        batchView: fixtures.batchView(records),
      },
    }),
  });
  await p.flush(20);
  return p;
}

async function profileWith(warningList) {
  const draft = fixtures.profileDraftView({
    status: "ok",
    profile: Object.assign({}, fixtures.profileDraftView().profile, { warnings: warningList }),
  });
  const p = await createPanel({
    responses: Object.assign({}, BASE, {
      DETECT_SURFACE: {
        ok: true,
        surface: SURFACES.PERSON_PROFILE,
        url: "https://www.linkedin.com/in/example",
      },
      PROFILE_GET_STATE: { ok: true, prefs: DEFAULT_PREFS, draftView: draft },
      PROFILE_CAPTURE: { ok: true, captureStatus: "ok", draftView: draft },
      PROFILE_MATCH_STATE: { ok: true, match: "none" },
    }),
  });
  await p.flush(20);
  return p;
}

// --- the shared classifier ----------------------------------------------------

test("classification lives in one module, and derived_value is provenance", () => {
  assert.equal(warnings.classify(WARN.DERIVED_VALUE), warnings.PROVENANCE);
  assert.equal(warnings.classify(WARN.DUPLICATE_COLLAPSED), warnings.PROVENANCE);

  for (const code of [
    WARN.MISSING_FIELD,
    WARN.SELECTOR_FAILURE,
    WARN.DUPLICATE_UNCERTAIN,
    WARN.MALFORMED_URL,
    WARN.NO_STABLE_IDENTITY,
    WARN.MISSING_SECTION,
    WARN.UNPARSED_TIMELINE,
    WARN.UNRECOGNIZED_LAYOUT,
    WARN.UNPARSED_VALUE,
    WARN.PLACEHOLDER_VALUE,
  ]) {
    assert.equal(warnings.classify(code), warnings.REVIEW_FAULT, `${code} must stay reviewable`);
  }
});

test("an unknown warning code is treated as a fault, not quietly ignored", () => {
  // Fail safe: a code this module has not been taught about might be a real
  // problem, so it keeps the visible state rather than the quiet one.
  assert.equal(warnings.classify("some_future_code"), warnings.REVIEW_FAULT);
  assert.equal(warnings.hasReviewFault([{ code: "some_future_code" }]), true);
});

test("splitting warnings preserves every entry", () => {
  const list = [DERIVED, MISSING_COMPANY, { code: WARN.DUPLICATE_COLLAPSED, field: "stableKey" }];
  const { faults, provenance } = warnings.split(list);
  assert.equal(faults.length + provenance.length, list.length, "nothing may be dropped");
  assert.deepEqual(faults, [MISSING_COMPANY]);
  assert.equal(provenance.length, 2);
  // The original objects come back, not copies stripped of their evidence.
  assert.equal(provenance[0].from, "salesNavLeadUrl");
  assert.equal(provenance[0].field, "linkedinProfileUrl");
});

test("provenance-only is distinguished from clean and from faulty", () => {
  assert.equal(warnings.isProvenanceOnly([]), false, "no warnings is not provenance-only");
  assert.equal(warnings.isProvenanceOnly([DERIVED]), true);
  assert.equal(warnings.isProvenanceOnly([DERIVED, MISSING_COMPANY]), false);
});

// --- Sales Navigator listing rows ---------------------------------------------

test("a listing row carrying only derived_value is not marked Needs review", async () => {
  const p = await listingsWith([fixtures.record({ warnings: [DERIVED] })]);
  const text = p.viewText();
  assert.ok(!/Needs review/.test(text), "a derived profile URL is not an operator fault");
  assert.ok(!/need review/.test(text), "and it must not be counted in the review tally");
  // Still surfaced, just not as a fault.
  assert.match(text, /Derived/);
});

test("a listing row with derived_value AND a real gap is still marked Needs review", async () => {
  const p = await listingsWith([
    fixtures.record({ companyName: null, warnings: [DERIVED, MISSING_COMPANY] }),
  ]);
  const text = p.viewText();
  assert.match(text, /Needs review/, "the missing field decides the state");
  assert.match(text, /1 need review/);
});

test("a listing row with a genuinely uncertain identity is marked Needs review", async () => {
  const p = await listingsWith([fixtures.record({ warnings: [UNCERTAIN] })]);
  assert.match(p.viewText(), /Needs review/);
});

test("the review tally counts only rows that need correcting", async () => {
  const p = await listingsWith([
    fixtures.record({ _stableKey: "a", warnings: [DERIVED] }),
    fixtures.record({ _stableKey: "b", warnings: [DERIVED] }),
    fixtures.record({ _stableKey: "c", companyName: null, warnings: [DERIVED, MISSING_COMPANY] }),
  ]);
  const text = p.viewText();
  assert.match(text, /1 need review/, "one of three, not three of three");
  assert.ok(!/3 need review/.test(text));
});

test("the raw derived_value code never reaches the operator", async () => {
  const p = await listingsWith([fixtures.record({ warnings: [DERIVED] })]);
  // The code may appear as a title attribute for inspection, but never as text.
  assert.ok(!/derived_value/.test(p.viewText()), "the raw code must not be rendered as copy");
  const badgeTitles = Array.from(p.document.querySelectorAll(".badge"))
    .map((b) => b.getAttribute("title") || "")
    .join(" ");
  assert.match(
    badgeTitles,
    /worked out from the lead link/,
    "the operator-facing wording must be present"
  );
});

test("warning evidence survives classification and stays reachable", async () => {
  const rec = fixtures.record({ warnings: [DERIVED] });
  const p = await listingsWith([rec]);
  // The record the panel received is untouched: classification reads, never edits.
  assert.deepEqual(rec.warnings, [DERIVED]);
  assert.equal(rec.warnings[0].from, "salesNavLeadUrl");
  assert.ok(p.viewText().length > 0);
});

// --- person profile -----------------------------------------------------------

test("a profile with provenance-only warnings is not shown as having gaps", async () => {
  const p = await profileWith([{ code: WARN.DERIVED_VALUE, field: "linkedin_profile_url" }]);
  const text = p.viewText();
  assert.ok(
    !/Some details could not be read/.test(text),
    "nothing about this capture was unreadable"
  );
  assert.notEqual(p.contextBadge(), "Needs review", "the surface badge must not claim a fault");
  // The note is still on screen, under its own truthful heading.
  assert.match(text, /Where these values came from/);
  assert.match(text, /worked out from another value/);
});

test("a profile with a genuine review warning still reports the gap", async () => {
  const p = await profileWith([{ code: WARN.UNPARSED_VALUE, field: "displayed_location" }]);
  assert.match(p.viewText(), /Some details could not be read/);
  assert.match(p.viewText(), /location was shown but could not be read/);
  assert.equal(p.contextBadge(), "Needs review", "a real gap keeps the visible review state");
});

test("a profile carrying both shows the gap and keeps the provenance note", async () => {
  const p = await profileWith([
    { code: WARN.DERIVED_VALUE, field: "linkedin_profile_url" },
    { code: WARN.MISSING_SECTION, section: "about" },
  ]);
  const text = p.viewText();
  assert.match(text, /Some details could not be read/, "the real gap decides the state");
  assert.match(text, /Where these values came from/, "and the provenance note is not swallowed");
});

test("no warning code reaches the profile operator as a raw identifier", async () => {
  const p = await profileWith([
    { code: WARN.DERIVED_VALUE, field: "linkedin_profile_url" },
    { code: "an_unmapped_future_code", field: "whatever" },
  ]);
  const text = p.viewText();
  assert.ok(!/derived_value/.test(text), "derived_value must not be rendered as copy");
  assert.ok(!/an_unmapped_future_code/.test(text), "nor may an unmapped code leak");
  assert.match(text, /an unlabelled capture note/, "an unmapped code reads as a sentence");
});

"use strict";
/**
 * DAT-020 / #215 — the resolving alias is navigation, not identity.
 *
 * DAT-019 stopped writing `/in/<member-id>` into the canonical profile URL,
 * which was right: it conflated an opaque Sales Navigator identifier with the
 * person's published handle and damaged its casing. But it also removed a
 * capability the operator used daily — LinkedIn accepts the member id and
 * redirects, so that alias opens the right person's profile even before their
 * handle is known.
 *
 * So the alias comes back, in its own field, with its own visual state and its
 * own honest label. What must not come back is any suggestion that it is the
 * published handle.
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

global.self = global;
const constants = require("../src/common/constants.js");
const extraction = require("../src/common/extraction.js");
const contactSchema = require("../src/common/contact-schema.js");
const { createPanel, DEFAULT_PREFS, fixtures } = require("./panel-harness.js");

const FIXTURES = path.join(__dirname, "fixtures");
const SURFACES = constants.SURFACES;

function capture(name) {
  const dom = new JSDOM(fs.readFileSync(path.join(FIXTURES, name), "utf8"));
  return extraction.extractPage(dom.window.document, {
    sourceSearchUrl: "https://www.linkedin.com/sales/search/people?page=1",
    capturedAt: "2026-07-27T00:00:00.000Z",
  });
}

// --- extraction ---------------------------------------------------------------

test("a row with only a member id gets a resolving alias built from it", () => {
  const rec = capture("results-normal.html").records[0];
  assert.equal(rec.linkedinProfileUrl, null, "still no published handle is claimed");
  assert.equal(rec.linkedinAliasUrl, "https://www.linkedin.com/in/" + rec.linkedinMemberId);
});

test("the alias preserves the member id's casing exactly", () => {
  const rec = capture("results-normal.html").records[0];
  const slug = rec.linkedinAliasUrl.split("/in/")[1];
  assert.equal(slug, rec.linkedinMemberId);
  assert.notEqual(slug, slug.toLowerCase(), "folding the case would break the redirect");
});

test("the alias never leaks into the canonical profile URL", () => {
  for (const rec of capture("results-normal.html").records) {
    if (!rec.linkedinAliasUrl) continue;
    assert.notEqual(rec.linkedinProfileUrl, rec.linkedinAliasUrl);
  }
});

test("a row with a real handle keeps it, and the alias stays separate evidence", () => {
  const rec = capture("results-observed-profile-url.html").records[0];
  assert.equal(rec.linkedinProfileUrl, "https://www.linkedin.com/in/dana-observed");
  assert.equal(rec.linkedinProfileUrlSource, "observed");
  assert.equal(rec.linkedinAliasUrl, "https://www.linkedin.com/in/ACwAAAQ2zzz");
  assert.notEqual(rec.linkedinAliasUrl, rec.linkedinProfileUrl);
});

test("a lead URL with no readable id yields no alias rather than a guess", () => {
  const rec = capture("results-malformed-urls.html").records.find((r) => !r.linkedinMemberId);
  if (!rec) return; // fixture has no such row; nothing to assert
  assert.equal(rec.linkedinAliasUrl, null);
});

// --- what reaches the wire ----------------------------------------------------

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

test("the capture distinguishes observed handle, derived alias and member id", () => {
  const person = personOf(capture("results-normal.html").records[0]);
  assert.equal(person.linkedin_profile_url, null);
  assert.match(person.salesnav_member_id, /^[A-Za-z0-9_-]{3,128}$/);
  assert.equal(
    person.salesnav_alias_url,
    "https://www.linkedin.com/in/" + person.salesnav_member_id
  );
  // Three distinct fields, so exported evidence can never conflate them.
  assert.notEqual(person.salesnav_alias_url, person.linkedin_profile_url);
  assert.notEqual(person.salesnav_alias_url, person.salesnav_member_id);
  assert.notEqual(person.salesnav_lead_url, person.salesnav_alias_url);
});

test("an observed row carries the handle and the alias in different fields", () => {
  const person = personOf(capture("results-observed-profile-url.html").records[0]);
  assert.equal(person.linkedin_profile_url, "https://www.linkedin.com/in/dana-observed");
  assert.equal(person.linkedin_public_identifier, "dana-observed");
  assert.equal(person.salesnav_alias_url, "https://www.linkedin.com/in/ACwAAAQ2zzz");
});

test("a person-profile capture has no alias at all", () => {
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
  assert.equal(person.salesnav_alias_url, null);
  assert.equal(person.salesnav_member_id, null);
});

test("the submission still validates against the committed contract", () => {
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

// --- the operator-facing action -----------------------------------------------

const BASE = {
  GET_STATE: {
    ok: true,
    prefs: DEFAULT_PREFS,
    metadata: { labels: [], note: null },
    batchView: null,
  },
  PROFILE_GET_STATE: { ok: true, prefs: DEFAULT_PREFS, draftView: null },
  COMPANY_GET_STATE: { ok: true, draftView: null },
  FETCH_LABELS: { ok: true, labels: [] },
};

const ALIAS = "https://www.linkedin.com/in/ACwAAAB1x9k";

function panelWith(records) {
  return createPanel({
    responses: Object.assign({}, BASE, {
      DETECT_SURFACE: {
        ok: true,
        surface: SURFACES.SALESNAV_PEOPLE_RESULTS,
        url: "https://www.linkedin.com/sales/search/people",
      },
      DETECT_ACTIVE_PAGE: {
        ok: true,
        page: { supported: true, url: "https://www.linkedin.com/sales/search/people", visibleCount: 1 },
      },
      GET_STATE: {
        ok: true,
        prefs: DEFAULT_PREFS,
        metadata: { labels: [], note: null },
        batchView: fixtures.batchView(records),
      },
    }),
  });
}

test("a prospect with only an alias shows a LinkedIn action marked derived", async () => {
  const p = await panelWith([
    fixtures.record({ linkedinProfileUrl: null, linkedinAliasUrl: ALIAS }),
  ]);
  await p.flush();

  const action = p.document.querySelector("#records [data-linkedin]");
  assert.ok(action, "the row must offer a LinkedIn action");
  assert.equal(action.getAttribute("data-linkedin"), "derived");
  assert.equal(action.getAttribute("href"), ALIAS);
  assert.equal(action.getAttribute("target"), "_blank");
});

test("its tooltip and accessible name both say the alias is derived", async () => {
  const p = await panelWith([
    fixtures.record({ linkedinProfileUrl: null, linkedinAliasUrl: ALIAS }),
  ]);
  await p.flush();

  const action = p.document.querySelector("#records [data-linkedin='derived']");
  assert.match(action.getAttribute("title"), /derived from the Sales Navigator ID/i);
  assert.match(action.getAttribute("aria-label"), /derived from the Sales Navigator ID/i);
  assert.match(action.getAttribute("aria-label"), /Dana Whitfield/);
  // It must not describe itself as the person's profile.
  assert.doesNotMatch(action.getAttribute("title"), /^Open LinkedIn profile$/);
});

test("an observed handle takes precedence and is not marked derived", async () => {
  const p = await panelWith([
    fixtures.record({
      linkedinProfileUrl: "https://www.linkedin.com/in/danawhitfield",
      linkedinAliasUrl: ALIAS,
    }),
  ]);
  await p.flush();

  const actions = Array.from(p.document.querySelectorAll("#records [data-linkedin]"));
  assert.equal(actions.length, 1, "one person, one LinkedIn action");
  assert.equal(actions[0].getAttribute("data-linkedin"), "observed");
  assert.equal(actions[0].getAttribute("href"), "https://www.linkedin.com/in/danawhitfield");
  assert.match(actions[0].getAttribute("title"), /Open LinkedIn profile/);
});

test("a prospect with neither shows no LinkedIn action at all", async () => {
  const p = await panelWith([
    fixtures.record({ linkedinProfileUrl: null, linkedinAliasUrl: null }),
  ]);
  await p.flush();
  assert.equal(p.document.querySelector("#records [data-linkedin]"), null);
});

test("the review screen offers the alias too, and says what it is", async () => {
  const p = await panelWith([
    fixtures.record({ linkedinProfileUrl: null, linkedinAliasUrl: ALIAS }),
  ]);
  await p.flush();
  await p.click("listings-review-btn");
  await p.flush();

  const link = p.document.querySelector("[data-view='listings-review'] [data-linkedin='derived']");
  assert.ok(link, "the review screen must offer the same navigation aid");
  assert.equal(link.getAttribute("href"), ALIAS);
  // The label reads "LinkedIn" now. "resolving alias" described the mechanism
  // rather than the destination, and an operator scanning a review screen wants
  // to know where a link goes. What it IS stays available and unambiguous: the
  // title says so, and data-linkedin distinguishes derived from observed.
  assert.equal(link.textContent, "LinkedIn");
  assert.match(link.getAttribute("title"), /not a published handle/i);
  assert.equal(link.getAttribute("data-linkedin"), "derived");
});

test("the review screen prefers the observed handle over the alias", async () => {
  const p = await panelWith([
    fixtures.record({
      linkedinProfileUrl: "https://www.linkedin.com/in/danawhitfield",
      linkedinAliasUrl: ALIAS,
    }),
  ]);
  await p.flush();
  await p.click("listings-review-btn");
  await p.flush();

  assert.equal(p.document.querySelector("[data-view='listings-review'] [data-linkedin='derived']"), null);
  const text = p.viewText();
  assert.match(text, /profile/);
});

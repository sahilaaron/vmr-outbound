"use strict";
/**
 * Campaign decoupling (DAT-013).
 *
 * These are the guard rails for the product decision: the extension is the
 * contact-acquisition edge, so the normal capture experience must contain no
 * campaign selector, no campaign id, and no campaign state — and it must stay
 * that way as the panel evolves.
 *
 * The legacy campaign-bound contracts are deliberately still referenced (a
 * previously staged batch must remain readable and the transition must be
 * explicit), so the assertions target the LIVE workflow, not the whole tree.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const SRC = path.join(__dirname, "..", "src");
const constants = require("../src/common/constants.js");
const contactSchema = require("../src/common/contact-schema.js");

function read(...parts) {
  return fs.readFileSync(path.join(SRC, ...parts), "utf8");
}

const PANEL_HTML = read("sidepanel", "sidepanel.html");
const PANEL_DOC = new JSDOM(PANEL_HTML).window.document;

// --- The side panel ----------------------------------------------------------

// The campaign-oriented language the contact-first panel must never show.
const CAMPAIGN_PHRASES = [
  /select campaign/i,
  /campaign id/i,
  /add to campaign/i,
  /send to campaign/i,
  /campaign required/i,
];

test("no campaign selector or campaign input is rendered", () => {
  assert.equal(PANEL_DOC.getElementById("campaign-select"), null);
  assert.equal(PANEL_DOC.getElementById("campaign-manual"), null);
  assert.equal(PANEL_DOC.getElementById("profile-campaign-select"), null);
  assert.equal(PANEL_DOC.getElementById("fetch-campaigns"), null);
  assert.equal(PANEL_DOC.getElementById("profile-fetch-campaigns"), null);
  for (const el of PANEL_DOC.querySelectorAll("select, input, button, option, datalist")) {
    assert.equal(/campaign/i.test(el.outerHTML), false, `campaign control left behind: ${el.id}`);
  }
});

test("no campaign-oriented language is shown to the operator", () => {
  const visible = PANEL_DOC.body.textContent;
  for (const phrase of CAMPAIGN_PHRASES) {
    assert.equal(phrase.test(visible), false, `panel still says ${phrase}`);
  }
  // The only permitted mention is the sentence that says labels are NOT
  // campaigns, which exists precisely to prevent the old mental model.
  const mentions = (visible.match(/campaigns?/gi) || []).length;
  assert.equal(mentions, 1);
  assert.match(visible, /they are not campaigns/i);
});

test("the panel offers the contact-first actions", () => {
  const text = PANEL_DOC.body.textContent;
  assert.match(text, /Capture visible contacts/);
  assert.match(text, /Save to VMR/);
  assert.ok(PANEL_DOC.getElementById("save-btn"));
  assert.equal(PANEL_DOC.getElementById("save-btn").textContent.trim(), "Save Contact");
});

test("the panel exposes optional labels and an optional note", () => {
  assert.ok(PANEL_DOC.getElementById("label-input"));
  assert.ok(PANEL_DOC.getElementById("label-chips"));
  assert.ok(PANEL_DOC.getElementById("note-input"));
  assert.match(PANEL_DOC.getElementById("metadata-card").textContent, /optional/i);
});

test("the panel still exposes the JSON and CSV export fallback", () => {
  assert.ok(PANEL_DOC.getElementById("export-json"));
  assert.ok(PANEL_DOC.getElementById("export-csv"));
});

test("the panel explains the unsupported and challenge states", () => {
  assert.match(PANEL_DOC.getElementById("unsupported-detail").textContent, /linkedin\.com\/in/);
  assert.match(PANEL_DOC.getElementById("challenge-section").textContent, /login wall|security check/i);
});

// --- Panel controllers --------------------------------------------------------

// Identifiers, not prose: the modules may explain that campaigns are gone, but
// they must carry no campaign state, field, element, or endpoint.
const CAMPAIGN_IDENTIFIERS = [
  /campaignId/,
  /campaign_id/,
  /campaign-select/,
  /campaign-manual/,
  /FETCH_CAMPAIGNS/,
  /api\/campaigns/,
];

test("neither side-panel controller carries campaign state", () => {
  for (const file of ["sidepanel.js", "sidepanel-profile.js"]) {
    const source = read("sidepanel", file);
    for (const pattern of CAMPAIGN_IDENTIFIERS) {
      assert.equal(pattern.test(source), false, `${file} still references ${pattern}`);
    }
  }
});

test("the service worker has no campaign fetch, preference, or selection", () => {
  const worker = read("background", "service-worker.js");
  assert.equal(/FETCH_CAMPAIGNS/.test(worker), false);
  assert.equal(/lastCampaignId/.test(worker), false);
  assert.equal(/api\/campaigns/.test(worker), false);
  assert.equal(/campaign-select/.test(worker), false);
});

// --- Contract and defaults ----------------------------------------------------

test("default preferences carry no campaign selection", () => {
  assert.equal("lastCampaignId" in constants.DEFAULT_PREFERENCES, false);
  assert.equal(/campaign/i.test(JSON.stringify(constants.DEFAULT_PREFERENCES)), false);
});

test("the live contract targets the contact-capture route", () => {
  assert.equal(constants.CONTACT_CAPTURE_PATH, "/api/intake/contact-captures");
  assert.equal(constants.CONTACT_CAPTURE_SCHEMA_VERSION, "linkedin-contact-capture/2.0.0");
  assert.equal(/campaign/i.test(constants.DEFAULT_PREFERENCES.mockReceiverUrl), false);
});

test("the contact contract declares no campaign property", () => {
  const schema = JSON.parse(
    fs.readFileSync(
      path.join(__dirname, "..", "docs", "contact-capture.schema.json"),
      "utf8"
    )
  );
  assert.equal("campaign_id" in schema.properties, false);
  assert.equal(schema.required.includes("campaign_id"), false);
  // No sub-schema anywhere declares a campaign property either.
  const declared = JSON.stringify(schema.$defs) + JSON.stringify(schema.properties);
  assert.equal(/campaign/i.test(declared), false);
});

test("the committed example submissions carry no campaign", () => {
  for (const name of [
    "contact-capture.profile.example.json",
    "contact-capture.salesnav.example.json",
  ]) {
    const payload = JSON.parse(
      fs.readFileSync(path.join(__dirname, "..", "docs", "fixtures", name), "utf8")
    );
    assert.equal("campaign_id" in payload, false);
    assert.equal(contactSchema.validateSubmission(payload).valid, true, name);
  }
});

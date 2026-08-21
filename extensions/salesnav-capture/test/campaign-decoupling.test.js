"use strict";
/**
 * Optional Campaign filing stays decoupled from Contact acquisition.
 *
 * The selector is a convenience after the Contact-first decision: empty is a
 * valid durable choice, the Campaign id is top-level filing context, and Labels
 * remain reusable Collections rather than Campaign membership.
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
const PANEL_JS = read("sidepanel", "sidepanel.js");
const WORKER_JS = read("background", "service-worker.js");

test("the Campaign selector is explicitly optional and defaults to Contact only", () => {
  const select = PANEL_DOC.getElementById("campaign-select");
  assert.ok(select);
  assert.equal(select.value, "");
  assert.match(select.options[0].textContent, /Save Contact only/i);
  assert.match(PANEL_DOC.getElementById("campaign-feedback").textContent, /No Campaign is required/i);
  assert.ok(PANEL_DOC.getElementById("campaign-refresh"));
});

test("Labels remain separate from Campaign filing", () => {
  assert.ok(PANEL_DOC.getElementById("label-input"));
  assert.ok(PANEL_DOC.getElementById("label-chips"));
  assert.ok(PANEL_DOC.getElementById("note-input"));
  assert.match(PANEL_DOC.getElementById("metadata-card").textContent, /not campaigns/i);
});

test("the selector loads Campaigns but never changes the Save Contact action", () => {
  assert.match(PANEL_JS, /FETCH_CAMPAIGNS/);
  assert.match(PANEL_JS, /SET_FILING_CONTEXT/);
  assert.ok(PANEL_DOC.getElementById("save-btn"));
  assert.equal(PANEL_DOC.getElementById("save-btn").textContent.trim(), "Save Contact");
});

test("Campaign filing context persists separately from capture drafts and preferences", () => {
  assert.equal(constants.CONTACT_STORAGE.FILING_CONTEXT, "cc_filing_context");
  assert.equal("campaignId" in constants.DEFAULT_PREFERENCES, false);
  assert.match(WORKER_JS, /getFilingContext/);
  assert.match(WORKER_JS, /chrome\.storage\.local\.set/);
  assert.match(WORKER_JS, /campaignId: filing\.campaignId/);
  assert.match(WORKER_JS, /FETCH_CAMPAIGNS/);
  assert.equal(constants.CAMPAIGNS_PATH, "/api/campaigns");
});

test("the contact contract makes campaign_id optional and nullable", () => {
  const schema = JSON.parse(
    fs.readFileSync(path.join(__dirname, "..", "docs", "contact-capture.schema.json"), "utf8")
  );
  assert.ok(schema.properties.campaign_id);
  assert.equal(schema.required.includes("campaign_id"), false);
  assert.deepEqual(schema.properties.campaign_id.type, ["string", "null"]);
});

test("submission builder supports both Contact-only and Campaign filing", () => {
  const base = {
    clientSubmissionId: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    captureMode: "linkedin_profile",
    submittedAt: "2026-07-29T10:00:00.000Z",
    extensionVersion: "2.1.0",
    metadata: { labels: [], note: null },
    contacts: [],
  };
  assert.equal(contactSchema.buildSubmission(base).campaign_id, null);
  assert.equal(
    contactSchema.buildSubmission({
      ...base,
      campaignId: "11111111-2222-4333-8444-555555555555",
    }).campaign_id,
    "11111111-2222-4333-8444-555555555555"
  );
});

test("committed examples show the valid Contact-only choice", () => {
  for (const name of [
    "contact-capture.profile.example.json",
    "contact-capture.salesnav.example.json",
  ]) {
    const payload = JSON.parse(
      fs.readFileSync(path.join(__dirname, "..", "docs", "fixtures", name), "utf8")
    );
    assert.equal(payload.campaign_id, null);
    assert.equal(contactSchema.validateSubmission(payload).valid, true, name);
  }
});

test("the local export is back, and it is not a way to file into a Campaign", () => {
  // It was removed in #280 on the reasoning that a reviewed contact is saved
  // into VMR Outbound or not saved at all. That is true of ACQUISITION and says
  // nothing about the operator's own backup of the rows they just reviewed, so
  // the download is restored — as a purely local action that sends nothing.
  assert.ok(PANEL_DOC.getElementById("export-json"));
  assert.ok(PANEL_DOC.getElementById("export-csv"));
  // And it stays outside the Campaign story entirely: an export names no
  // campaign, requests no filing, and cannot enrol anybody.
  assert.ok(!/EXPORT_CAPTURED_CONTACTS[\s\S]{0,400}campaign/i.test(PANEL_JS));
  assert.ok(!/buildCapturedExport[\s\S]{0,600}campaign_id/i.test(WORKER_JS));
});

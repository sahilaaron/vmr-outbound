"use strict";
/**
 * DAT-018 C — compact panel layout.
 *
 * The panel is an operator tool, so space spent on decoration is space taken
 * from the work. These assertions hold the layout decisions in place as the
 * panel evolves: no Mode card, no slogan, a compact surface chip under the
 * heading, and the archived-drafts affordance retained ONLY because it is the
 * sole route back to recoverable data.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const SRC = path.join(__dirname, "..", "src");

function read(...parts) {
  return fs.readFileSync(path.join(SRC, ...parts), "utf8");
}

const PANEL_HTML = read("sidepanel", "sidepanel.html");
const PANEL_DOC = new JSDOM(PANEL_HTML).window.document;
const PANEL_JS = read("sidepanel", "sidepanel.js");
const PROFILE_JS = read("sidepanel", "sidepanel-profile.js");
const WORKER_JS = read("background", "service-worker.js");

// --- the Mode card is gone ----------------------------------------------------

test("the dedicated Mode section no longer exists", () => {
  assert.equal(PANEL_DOC.getElementById("mode-card"), null);
  assert.equal(PANEL_DOC.getElementById("mode-status"), null);
  assert.equal(PANEL_DOC.getElementById("mode-detail"), null);
  // And nothing still tries to paint into it.
  for (const [name, src] of [
    ["sidepanel.js", PANEL_JS],
    ["sidepanel-profile.js", PROFILE_JS],
  ]) {
    assert.ok(!src.includes('"mode-status"'), `${name} still writes to mode-status`);
    assert.ok(!src.includes('"mode-detail"'), `${name} still writes to mode-detail`);
    assert.ok(!src.includes('"mode-card"'), `${name} still writes to mode-card`);
  }
});

test("a Mode heading is not rendered anywhere in the panel", () => {
  const labels = Array.from(PANEL_DOC.querySelectorAll(".step-label")).map((el) =>
    el.textContent.trim().toLowerCase()
  );
  assert.ok(!labels.includes("mode"), "a 'Mode' section label is still present");
});

// --- the compact surface indicator --------------------------------------------

test("a compact surface chip sits directly beneath the panel heading", () => {
  const chip = PANEL_DOC.getElementById("surface-indicator");
  assert.ok(chip, "surface-indicator is missing");
  const header = PANEL_DOC.querySelector("header.app-header");
  assert.ok(header.contains(chip), "the surface chip must live in the header");
  const title = PANEL_DOC.getElementById("app-title");
  assert.ok(
    title.compareDocumentPosition(chip) & 4, // DOCUMENT_POSITION_FOLLOWING
    "the surface chip must come after the heading"
  );
});

test("the surface labels are short chips, not sentences", () => {
  for (const label of ["SalesNav Listing", "LinkedIn Profile", "LinkedIn Company"]) {
    assert.ok(PROFILE_JS.includes(`"${label}"`), `missing compact label: ${label}`);
  }
  // The old long-form labels are gone.
  assert.ok(!PROFILE_JS.includes('"Sales Navigator Listings"'));
  assert.ok(!PROFILE_JS.includes('"LinkedIn Person Profile"'));
});

test("the refresh control survives the layout change", () => {
  assert.ok(PANEL_DOC.getElementById("refresh-mode"), "the Refresh control was lost");
});

// --- the slogan is gone -------------------------------------------------------

test("the 'Save the person first' subtitle is removed from the panel", () => {
  assert.ok(!PANEL_HTML.includes("Save the person first"));
  assert.equal(PANEL_DOC.querySelector("header .subtitle"), null);
});

// --- Workflow updated ---------------------------------------------------------

test("the 'Workflow updated' notice is gone", () => {
  assert.ok(!PANEL_HTML.includes("Workflow updated"));
  assert.equal(PANEL_DOC.getElementById("migration-card"), null);
  assert.equal(PANEL_DOC.getElementById("migration-message"), null);
});

test("the archived-drafts affordance is retained and named for its action", () => {
  // Code inspection justification: `exportLegacyArchive` reads storage key
  // `cc_legacy_v1_archive`, and this button is the only caller. Removing it
  // while an archive exists would strand recoverable data — so this card is
  // kept, renamed to describe the operator action rather than a workflow event.
  const card = PANEL_DOC.getElementById("archive-card");
  assert.ok(card, "the archived-drafts card must be retained");
  assert.ok(card.hasAttribute("hidden"), "it must start hidden");
  assert.ok(PANEL_DOC.getElementById("migration-export"), "the download button is required");
  const label = card.querySelector(".step-label").textContent.toLowerCase();
  assert.ok(
    label.includes("archived drafts"),
    "the label must describe the data, not a workflow event"
  );
  assert.ok(WORKER_JS.includes("exportLegacyArchive"), "the export path must still exist");
});

test("the archive card is shown only while an archive actually exists", () => {
  // Not on the presence of a notice — that was the old, valueless trigger.
  assert.ok(PANEL_JS.includes("info.hasArchive"), "must gate on hasArchive");
  assert.ok(!PANEL_JS.includes("info.notice"), "must not gate on the retired notice");
});

test("discarding clears the archive so the card cannot silently return", () => {
  assert.ok(PANEL_JS.includes("DISCARD_LEGACY_ARCHIVE"));
  assert.ok(WORKER_JS.includes("DISCARD_LEGACY_ARCHIVE"));
  assert.ok(
    WORKER_JS.includes("async function discardLegacyArchive"),
    "discard must be implemented, not just messaged"
  );
});

// --- what must NOT have been lost ---------------------------------------------

test("explicit save and capture controls are preserved", () => {
  for (const id of ["capture-btn", "save-btn", "profile-capture-btn"]) {
    assert.ok(PANEL_DOC.getElementById(id), `control ${id} must survive the layout change`);
  }
});

test("warning and challenge surfaces are preserved", () => {
  assert.ok(PANEL_DOC.getElementById("challenge-section"));
  assert.ok(PANEL_DOC.getElementById("unsupported-section"));
  assert.ok(PANEL_DOC.getElementById("detect-status"));
});

test("the skipped-row report exists and starts hidden", () => {
  const card = PANEL_DOC.getElementById("skipped-card");
  assert.ok(card, "skipped-card is missing");
  assert.ok(card.hasAttribute("hidden"));
  assert.ok(PANEL_DOC.getElementById("skipped-summary"));
  assert.ok(PANEL_DOC.getElementById("skipped-list"));
  assert.ok(PANEL_JS.includes("renderSkipped"));
});

// --- the non-goals, asserted --------------------------------------------------

/** Strip comments so this scans CODE, not the prose that disclaims these very
 *  behaviours. Without this the assertion fires on its own documentation. */
function codeOnly(src) {
  return src.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/(^|[^:])\/\/.*$/gm, "$1");
}

test("no pagination, navigation, or anti-bot behaviour is introduced", () => {
  const CONTENT = codeOnly(read("content", "content-script.js"));
  const SCROLLER = codeOnly(read("common", "scroller.js"));
  const forbidden = [
    /location\s*=\s*/,
    /location\.assign/,
    /location\.replace/,
    /window\.open/,
    /\.click\(\)/,
    /next-?page/i,
    /paginat/i,
    /captcha/i,
    /webdriver/i,
    /navigator\.userAgent\s*=/,
  ];
  for (const src of [CONTENT, SCROLLER]) {
    for (const re of forbidden) {
      assert.ok(!re.test(src), `capture path must not contain ${re}`);
    }
  }
});

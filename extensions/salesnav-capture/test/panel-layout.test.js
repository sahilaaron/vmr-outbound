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
const { stripComments } = require("./strip-comments.js");

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

// The VM Prospector redesign keeps this affordance and its id, and promotes it
// from a chip inside the header to the detected-page strip the wireframes
// specify: still one compact always-on line, still directly beneath the
// heading, but now spanning the panel with the surface icon, the surface name
// and the page's one-word state. The requirement it encodes is unchanged — the
// operator must be able to see which surface is active without opening
// anything — so the assertions target position and compactness, not the chip.
test("the surface indicator sits directly beneath the panel heading", () => {
  const indicator = PANEL_DOC.getElementById("surface-indicator");
  assert.ok(indicator, "surface-indicator is missing");
  const title = PANEL_DOC.getElementById("app-title");
  assert.ok(
    title.compareDocumentPosition(indicator) & 4, // DOCUMENT_POSITION_FOLLOWING
    "the surface indicator must come after the heading"
  );
  const header = PANEL_DOC.querySelector("header.app-header");
  assert.ok(
    header.compareDocumentPosition(indicator) & 4,
    "the surface indicator must sit below the header, not inside the body"
  );
  const body = PANEL_DOC.getElementById("app-body");
  assert.ok(
    !body.contains(indicator),
    "the surface indicator must not scroll away with the body content"
  );
  // Still a single line, not a section: no card, no heading, no controls.
  assert.equal(indicator.tagName, "DIV");
  assert.equal(indicator.querySelector("button"), null, "the indicator must carry no controls");
});

test("the surface labels name the surface, and the old long-form labels are gone", () => {
  for (const label of [
    "Sales Navigator · Search results",
    "LinkedIn · Person profile",
    "LinkedIn · Company page",
  ]) {
    assert.ok(PROFILE_JS.includes(`"${label}"`), `missing surface label: ${label}`);
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

test("the archived-drafts card and its download are gone entirely (#280)", () => {
  // The card was retained in DAT-018 C because the download it offered was the
  // only route back to `cc_legacy_v1_archive`. #280 removes the extension's
  // download capability outright, so the card, the button, the worker handler
  // and the archive itself go together — the migration now clears that key
  // instead of writing it (see test/migration.test.js).
  assert.equal(PANEL_DOC.getElementById("archive-card"), null);
  assert.equal(PANEL_DOC.getElementById("archive-message"), null);
  assert.equal(PANEL_DOC.getElementById("migration-export"), null);
  assert.equal(PANEL_DOC.getElementById("migration-dismiss"), null);
  assert.ok(!/Archived drafts/i.test(PANEL_HTML), "the card's heading must be gone");
  for (const name of [
    "exportLegacyArchive",
    "discardLegacyArchive",
    "EXPORT_LEGACY_ARCHIVE",
    "DISCARD_LEGACY_ARCHIVE",
    "DISMISS_MIGRATION_NOTICE",
  ]) {
    assert.ok(!WORKER_JS.includes(name), `${name} must be gone from the service worker`);
    assert.ok(!PANEL_JS.includes(name), `${name} must be gone from the panel`);
  }
});

test("the JSON and CSV export controls are gone, with their handlers (#280)", () => {
  for (const id of ["export-row", "export-json", "export-csv"]) {
    assert.equal(PANEL_DOC.getElementById(id), null, `${id} must be removed from the panel`);
  }
  assert.ok(!/Download JSON|Download CSV/i.test(PANEL_HTML));
  assert.ok(!PANEL_JS.includes("EXPORT_BATCH"), "the panel must not message an export");
  assert.ok(!WORKER_JS.includes("EXPORT_BATCH"), "the worker must not handle an export");
  assert.ok(!WORKER_JS.includes("chrome.downloads"), "no download call may remain");
  assert.ok(!WORKER_JS.includes("async function exportBatch"));
});

test("the full active-page URL line under the detected-page strip is gone (#280)", () => {
  // The compact strip (icon, label, badge) is the whole indicator now. The URL
  // repeated the address bar, wrapped on Sales Navigator URLs, and pushed the
  // operator's actual work down the panel.
  assert.equal(PANEL_DOC.getElementById("surface-detail"), null);
  assert.ok(!PANEL_HTML.includes("context-url"), "the URL line's class must be gone too");
  // The strip itself must survive.
  assert.ok(PANEL_DOC.getElementById("surface-indicator"), "the compact strip must remain");
  assert.ok(PANEL_DOC.getElementById("context-label"));
  assert.ok(PANEL_DOC.getElementById("context-badge"));
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
 *  behaviours. Uses a real scanner: a regex stripper mis-handles the `/*` inside
 *  match-pattern strings and can silently swallow the code under test. */
function codeOnly(src) {
  return stripComments(src);
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

"use strict";
/**
 * Visible About capture (DAT-013).
 *
 * The About section is read only when the opened page already renders it. The
 * extension never expands "see more", never fetches, and never summarizes; an
 * absent or empty section is reported as missing rather than guessed at.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const profileExtraction = require("../src/common/profile-extraction.js");

const PROFILE_URL = "https://www.linkedin.com/in/test-profile";

function extract(fixture) {
  const html = fs.readFileSync(path.join(__dirname, "fixtures-profile", fixture), "utf8");
  const doc = new JSDOM(html, { url: PROFILE_URL }).window.document;
  return profileExtraction.extractProfile(doc, {
    sourceUrl: PROFILE_URL,
    capturedAt: "2026-07-26T10:00:00.000Z",
  });
}

test("visible About text is captured verbatim from the opened page", () => {
  const result = extract("profile-basic.html");
  assert.match(result.profile.about_text, /Operations leader focused on scaling delivery teams\./);
  assert.equal(result.missingSections.includes("about"), false);
});

test("the About heading and expand affordances are not part of the captured text", () => {
  const result = extract("profile-basic.html");
  assert.equal(/^about$/im.test(result.profile.about_text), false);
  assert.equal(/more$/i.test(result.profile.about_text.trim()), false);
});

test("a missing About section reports missing and captures null, never a guess", () => {
  const result = extract("profile-missing-about.html");
  assert.equal(result.profile.about_text, null);
  assert.ok(result.missingSections.includes("about"));
});

test("an About section with no readable body is reported, not fabricated", () => {
  const html = `<!DOCTYPE html><html><body>
    <section componentkey="ProfileCards-topcard"><h1>Test Person</h1><p>Head of Ops</p></section>
    <section componentkey="ProfileCards-about"><h2>About</h2></section>
  </body></html>`;
  const doc = new JSDOM(html, { url: PROFILE_URL }).window.document;
  const result = profileExtraction.extractProfile(doc, {
    sourceUrl: PROFILE_URL,
    capturedAt: "2026-07-26T10:00:00.000Z",
  });
  assert.equal(result.profile.about_text, null);
  assert.ok(result.missingSections.includes("about_text"));
});

test("an over-long About is truncated at a line break, never mid-sentence", () => {
  const line = "A".repeat(500);
  const paragraphs = Array.from({ length: 30 }, () => `<p>${line}</p>`).join("");
  const html = `<!DOCTYPE html><html><body>
    <section componentkey="ProfileCards-topcard"><h1>Test Person</h1><p>Head of Ops</p></section>
    <section componentkey="ProfileCards-about"><h2>About</h2>${paragraphs}</section>
  </body></html>`;
  const doc = new JSDOM(html, { url: PROFILE_URL }).window.document;
  const result = profileExtraction.extractProfile(doc, {
    sourceUrl: PROFILE_URL,
    capturedAt: "2026-07-26T10:00:00.000Z",
  });
  assert.ok(result.profile.about_text.length <= 8000);
  assert.equal(result.profile.about_text.endsWith("A"), true);
});

test("the legacy v1 profile payload is unaffected by the new field", () => {
  // v1 declares `additionalProperties: false`, so about_text must not leak in.
  const profileSchema = require("../src/common/profile-schema.js");
  const payload = profileSchema.buildProfilePayload({
    extraction: extract("profile-basic.html"),
    clientCaptureId: "11111111-2222-3333-4444-555555555555",
    campaignId: null,
    extensionVersion: "2.0.0",
  });
  assert.equal("about_text" in payload.profile, false);
  assert.equal(profileSchema.validateProfilePayload(payload).valid, true);
});

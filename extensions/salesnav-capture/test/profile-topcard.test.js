"use strict";
/**
 * DAT-016 — top-card extraction must be structural, never positional.
 *
 * The fixtures exercised here model the shapes actually observed on real
 * profiles: a name in an <h2>, no <section> in the card, hashed-only classes,
 * a verification badge nested in an <a>, an unlabelled line inside the name
 * row, zero/one/two degree badges, a connection region rendered as one, two or
 * four nodes or left completely empty, and LinkedIn's literal "--" headline
 * placeholder.
 *
 * Every fixture is synthetic and authored from a structural description. No
 * captured markup and no real profile content is committed.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const profileExtraction = require("../src/common/profile-extraction.js");
const { CAPTURE_STATUS, WARNINGS } = require("../src/common/constants.js");

const FIXTURE_DIR = path.join(__dirname, "fixtures-profile");
const PROFILE_URL = "https://www.linkedin.com/in/test-profile";

function readFixture(name) {
  return fs.readFileSync(path.join(FIXTURE_DIR, name), "utf8");
}

function extractHtml(html, url) {
  const u = url || PROFILE_URL;
  const doc = new JSDOM(html, { url: u }).window.document;
  return profileExtraction.extractProfile(doc, {
    sourceUrl: u,
    capturedAt: "2026-07-26T10:00:00.000Z",
  });
}

function extract(name, url) {
  return extractHtml(readFixture(name), url);
}

function warningCodes(result) {
  return (result.profile ? result.profile.warnings : []).map((w) => w.code);
}

function warningFor(result, field) {
  return (result.profile ? result.profile.warnings : []).find((w) => w.field === field) || null;
}

// ---- the full modern top card --------------------------------------------

test("modern top card: every field resolves structurally", () => {
  const r = extract("profile-modern-topcard.html");

  assert.equal(r.profile.full_name, "Marguerite Okonkwo-Bayer");
  assert.equal(
    r.profile.headline,
    "Head of Manufacturing Operations at Tamaki Foundry"
  );
  assert.equal(r.profile.displayed_location, "Wellington, Wellington Region, New Zealand");
  assert.equal(r.profile.connection_count, 500);
  assert.equal(r.profile.connection_count_raw, "500+ connections");
  assert.equal(r.profile.open_to_work, false);
});

test("the unlabelled name-row line is never mistaken for the headline", () => {
  // The fixture carries an extra <p> between the name and the degree badges.
  // A parser that took "the first <p> after the name" would return it.
  const r = extract("profile-modern-topcard.html");
  assert.notEqual(r.profile.headline, "Origami");
  assert.ok(r.profile.raw_lines.includes("Origami"), "the line is still preserved in raw_lines");
});

test("the company · school line is never mistaken for the location", () => {
  const r = extract("profile-modern-topcard.html");
  assert.notEqual(
    r.profile.displayed_location,
    "Tamaki Foundry · Victoria University of Wellington"
  );
});

test("the verification badge nested in an <a> does not contaminate the name", () => {
  const r = extract("profile-modern-topcard.html");
  assert.equal(r.profile.full_name, "Marguerite Okonkwo-Bayer");
});

test("the top card excludes About, Experience and Education", () => {
  // The container is found by climbing from the name heading. The failure this
  // guards against is the climb running to <main> and swallowing the rest of
  // the page, which is what produced 200-node captures during design.
  const r = extract("profile-modern-topcard.html");
  const joined = r.profile.raw_lines.join(" | ");
  assert.ok(!/Manufacturing operations, tooling/.test(joined), "About text leaked into the top card");
  assert.ok(!/Plant Manager/.test(joined), "Experience text leaked into the top card");
  assert.ok(!/BE \(Hons\)/.test(joined), "Education text leaked into the top card");
});

// ---- hashed classes are corroboration, never selection --------------------

test("extraction is identical when every hashed class is removed", () => {
  // LinkedIn's classes are build output and rotate on any deploy. Nothing in
  // the extractor may depend on them, so stripping them must change nothing.
  const original = readFixture("profile-modern-topcard.html");
  const declassed = original.replace(/\sclass="[^"]*"/g, "");

  const a = extractHtml(original);
  const b = extractHtml(declassed);

  assert.deepEqual(b.profile.full_name, a.profile.full_name);
  assert.deepEqual(b.profile.headline, a.profile.headline);
  assert.deepEqual(b.profile.displayed_location, a.profile.displayed_location);
  assert.deepEqual(b.profile.connection_count, a.profile.connection_count);
});

test("a hashed class appearing on the wrong role changes nothing", () => {
  // `_3ab7a3ad` was, on a two-sample reading, taken for "the location class".
  // A third profile carried it on an unrelated line in the name row. Moving it
  // onto the headline must not move the location.
  const original = readFixture("profile-modern-topcard.html");
  const moved = original.replace(
    '<p class="_687a5045 _8c535ff6">Head of Manufacturing Operations at Tamaki Foundry</p>',
    '<p class="_687a5045 _3ab7a3ad">Head of Manufacturing Operations at Tamaki Foundry</p>'
  );
  assert.notEqual(moved, original, "the replacement did not apply");

  const r = extractHtml(moved);
  assert.equal(r.profile.headline, "Head of Manufacturing Operations at Tamaki Foundry");
  assert.equal(r.profile.displayed_location, "Wellington, Wellington Region, New Zealand");
});

// ---- connections -----------------------------------------------------------

test("an empty connection container yields null, never zero", () => {
  const r = extract("profile-empty-connections.html");
  assert.equal(r.profile.connection_count, null);
  assert.equal(r.profile.connection_count_raw, null);
  assert.equal(r.status, CAPTURE_STATUS.PARTIAL);

  const w = warningFor(r, "connection_count");
  assert.ok(w, "a warning must be raised");
  assert.equal(w.code, WARNINGS.MISSING_FIELD);

  // The rest of the card still parses.
  assert.equal(r.profile.full_name, "Tobias Wrenfield");
  assert.equal(r.profile.headline, "Principal Acoustic Consultant");
  assert.equal(r.profile.displayed_location, "Galway, County Galway, Ireland");
});

test("a follower count is never promoted into the connection count", () => {
  const r = extract("profile-followers-only.html");
  assert.equal(r.profile.connection_count, null);
  const w = warningFor(r, "connection_count");
  assert.ok(w, "a warning must be raised");
  assert.equal(
    w.code,
    WARNINGS.UNPARSED_VALUE,
    "the region was present but unpairable — that is not the same as absent"
  );
  assert.equal(r.profile.headline, "Creator & Newsletter Author");
  assert.equal(r.profile.displayed_location, "Bergen, Vestland, Norway");
  assert.equal(r.profile.open_to_work, true);
});

test("small connection counts parse exactly", () => {
  assert.equal(extract("profile-placeholder-headline.html").profile.connection_count, 9);
  assert.equal(extract("profile-name-suffix.html").profile.connection_count, 488);
});

test("parseCountRegion handles every observed arity", () => {
  const { parseCountRegion } = profileExtraction._internals;
  const blocks = (...texts) => texts.map((text) => ({ text, el: null }));

  // one node
  let r = parseCountRegion(blocks("500+ connections"));
  assert.equal(r.connections, 500);

  // two nodes
  r = parseCountRegion(blocks("500+", "connections"));
  assert.equal(r.connections, 500);

  // four nodes: followers · count label
  r = parseCountRegion(blocks("29,777 followers", "·", "500+", "connections"));
  assert.equal(r.followers, 29777);
  assert.equal(r.connections, 500);

  // empty region
  r = parseCountRegion(blocks());
  assert.equal(r.connections, null);
  assert.equal(r.sawRegion, false);

  // a bare count with no label is left unpaired, not assumed
  r = parseCountRegion(blocks("312"));
  assert.equal(r.connections, null);
  assert.equal(r.sawRegion, true);
  assert.equal(r.unpaired, true);
});

// ---- headline --------------------------------------------------------------

test("the '--' placeholder headline becomes null with an explicit warning", () => {
  const r = extract("profile-placeholder-headline.html");
  assert.equal(r.profile.headline, null);

  const w = warningFor(r, "headline");
  assert.ok(w, "a warning must be raised");
  assert.equal(w.code, WARNINGS.PLACEHOLDER_VALUE);
  assert.equal(w.raw, "--");
  assert.equal(r.status, CAPTURE_STATUS.PARTIAL);
});

test("a missing degree badge does not shift the headline or location", () => {
  // Previously the absent-degree case was only seen on a self-view, making
  // "third-party implies a degree badge" a tempting assumption. It is false.
  const r = extract("profile-placeholder-headline.html");
  assert.equal(r.profile.displayed_location, "Utrecht, Utrecht, Netherlands");
});

test("a headline that looks like a pronoun pair is kept", () => {
  // "Founder/CEO" must not be discarded as a pronoun line. Pronouns are
  // matched as an exact set, never by the presence of a slash.
  const html = `<!DOCTYPE html><html><head><title>Ada Ferreira | LinkedIn</title></head><body>
    <main><div><div class="card">
      <div class="row"><h2>Ada Ferreira</h2><p>· 2nd</p></div>
      <p>Founder/CEO</p>
      <div class="loc"><p>Porto, Porto, Portugal</p><p>·</p><p><a>Contact info</a></p></div>
      <div><span>640</span><span>connections</span></div>
      <button type="button">Connect</button>
    </div></div></main></body></html>`;
  const r = extractHtml(html);
  assert.equal(r.profile.headline, "Founder/CEO");
  assert.equal(r.profile.displayed_location, "Porto, Porto, Portugal");
  assert.equal(r.profile.connection_count, 640);
});

// ---- names -----------------------------------------------------------------

test("names carrying punctuation are captured verbatim and never split", () => {
  assert.equal(
    extract("profile-placeholder-headline.html").profile.full_name,
    "Rosalind (Roz) Achterberg"
  );
  assert.equal(extract("profile-name-suffix.html").profile.full_name, "Desmond Ilunga, PE");
});

// ---- logo slots ------------------------------------------------------------

test("a school figure with no image does not break extraction", () => {
  // The <figure> proves the slot exists; it does not prove a logo rendered.
  const r = extract("profile-name-suffix.html");
  assert.equal(r.profile.full_name, "Desmond Ilunga, PE");
  assert.equal(r.profile.headline, "Structural Engineer | Bridges & Heavy Civil");
  assert.equal(r.profile.displayed_location, "Milan, Lombardy, Italy");
});

test("a mutual-connections line is never mistaken for a field", () => {
  const r = extract("profile-name-suffix.html");
  assert.notEqual(r.profile.displayed_location, "Beatriz Amado is a mutual connection");
  assert.notEqual(r.profile.headline, "Beatriz Amado is a mutual connection");
});

// ---- an interrupted top card (found during authenticated C1) ----------------

test("a promo block between the name and the card does not become the headline", () => {
  // Live C1 finding. One profile's top-card container held a promotional line
  // and an ad-preferences panel BETWEEN the name and the real rows. Taking "the
  // first unaccounted-for block after the name" returned the promo text as the
  // headline and a dropdown option as the location — both confident, both
  // wrong, neither warned about.
  const r = extract("profile-interrupted-topcard.html");

  assert.equal(r.profile.full_name, "Wilhelmina Farsight");
  assert.equal(r.profile.headline, "Principal Hydrologist at Kestrel Basin Survey");
  assert.equal(r.profile.displayed_location, "Tromsø, Troms og Finnmark, Norway");
  assert.equal(r.profile.connection_count, 500);
});

test("dropdown options in the card can never become a field", () => {
  // The panel contains an option shaped exactly like a location and two shaped
  // like counts. None of them is profile content.
  const r = extract("profile-interrupted-topcard.html");

  assert.notEqual(r.profile.displayed_location, "Greater Nowhere Area, Somewhere");
  assert.notEqual(r.profile.displayed_location, "Remote");
  assert.notEqual(r.profile.headline, "Greater Nowhere Area, Somewhere");
  assert.notEqual(r.profile.headline, "Audience location");
  assert.equal(r.profile.connection_count, 500, "a percentage option is not a count");

  // `raw_lines` is deliberately NOT filtered: it is verbatim page text kept so
  // the backend can re-derive a value this parser version did not understand.
  // Page furniture appearing there is noise in the evidence, not corruption of
  // a field — the guarantee is about what becomes a *field*.
  assert.ok(r.profile.raw_lines.length > 0);
});

test("a role=button control is recognised without being a <button>", () => {
  // Live profiles render actions as <div role="button"> as often as <button>.
  // An unrecognised action label competes for the headline and the location.
  const r = extract("profile-interrupted-topcard.html");

  assert.notEqual(r.profile.headline, "Reactivate");
  assert.notEqual(r.profile.headline, "Try Premium for free.");
  assert.notEqual(r.profile.displayed_location, "Follow this hydrologist");
  assert.notEqual(r.profile.displayed_location, "Message");
});

test("the interrupted card still reports its company and school row", () => {
  // The credential row must stay classified, not leak into a field.
  const r = extract("profile-interrupted-topcard.html");
  assert.notEqual(r.profile.headline, "Kestrel Basin Survey · University of Tromsø");
  assert.notEqual(r.profile.displayed_location, "Kestrel Basin Survey · University of Tromsø");
});

// ---- the name is never a section heading -----------------------------------

test("a section heading is never taken for the person's name", () => {
  // A page whose only headings are section titles has no top card. Capturing
  // "About" as somebody's name is exactly the silent wrong answer #167 removes.
  const html = `<!DOCTYPE html><html><head><title>LinkedIn</title></head><body>
    <main>
      <section><h2>About</h2><p>Some text.</p></section>
      <section><h2>Activity</h2><p>Posts.</p></section>
      <section><h2>Experience</h2><p>Roles.</p></section>
    </main></body></html>`;
  const r = extractHtml(html);
  assert.equal(r.status, CAPTURE_STATUS.STRUCTURE_UNRECOGNIZED);
  assert.equal(r.profile, null, "nothing may be captured from an unrecognised page");
  assert.ok(r.pageWarnings.length > 0);
});

test("a section heading below the name does not become the name", () => {
  const r = extract("profile-modern-topcard.html");
  assert.notEqual(r.profile.full_name, "About");
  assert.notEqual(r.profile.full_name, "Experience");
  assert.notEqual(r.profile.full_name, "Education");
});

// ---- logo slots corroborate the company · school row -----------------------

test("the company · school row is located by its logo slots, not by position", () => {
  // Move the credential row ABOVE the headline. A positional parser would now
  // return it as the headline; the svg[id] logo slots identify it regardless.
  const original = readFixture("profile-modern-topcard.html");
  const headline =
    '<p class="_687a5045 _8c535ff6">Head of Manufacturing Operations at Tamaki Foundry</p>\n';
  const credentials =
    '        <div class="_ed7c9012">\n' +
    '          <figure class="_a7b8c9d0"><svg id="company-accent-4"></svg><img alt="Company logo" /></figure>\n' +
    '          <p class="_687a5045 _7ba7f145">Tamaki Foundry · Victoria University of Wellington</p>\n' +
    '          <figure class="_a7b8c9d0"><svg id="school-accent-4"></svg><img alt="School logo" /></figure>\n' +
    "        </div>\n";
  assert.ok(original.includes(headline), "headline anchor not found");
  assert.ok(original.includes(credentials), "credential row anchor not found");

  const swapped = original.replace(headline, "").replace(credentials, credentials + "        " + headline);
  const r = extractHtml(swapped);

  assert.equal(r.profile.headline, "Head of Manufacturing Operations at Tamaki Foundry");
  assert.equal(r.profile.displayed_location, "Wellington, Wellington Region, New Zealand");
});

test("a school figure holding only a placeholder svg still marks the row", () => {
  const r = extract("profile-name-suffix.html");
  assert.notEqual(
    r.profile.headline,
    "Cortez Bridgeworks · Politecnico di Milano",
    "the credential row must not be read as the headline"
  );
  assert.equal(r.profile.headline, "Structural Engineer | Bridges & Heavy Civil");
});

// ---- action buttons --------------------------------------------------------

test("a profile-specific action button is recognised structurally", () => {
  // The action set is open-ended; buttons are identified by being controls,
  // not by matching a hardcoded label list.
  const r = extract("profile-modern-topcard.html");
  assert.notEqual(r.profile.headline, "Visit my website");
  assert.notEqual(r.profile.displayed_location, "Visit my website");
});

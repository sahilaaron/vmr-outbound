"use strict";
/**
 * VM Prospector side panel — the states an operator actually sees.
 *
 * These drive the REAL panel (src/sidepanel/sidepanel.html + the shipped
 * controllers) through a stubbed `chrome.*` and assert what is on screen: which
 * detected interface, which step, which dominant action, and — the part that
 * matters most — that a gap, a skip, a partial result or a failure stays
 * visible instead of being smoothed away by the redesign.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");

const { createPanel, DEFAULT_PREFS, fixtures } = require("./panel-harness.js");

const SRC = path.join(__dirname, "..", "src");
const SURFACES = require("../src/common/constants.js").SURFACES;

const BASE = {
  GET_STATE: { ok: true, prefs: DEFAULT_PREFS, metadata: { labels: [], note: null }, batchView: null },
  PROFILE_GET_STATE: { ok: true, prefs: DEFAULT_PREFS, draftView: null },
  COMPANY_GET_STATE: { ok: true, draftView: null },
  FETCH_LABELS: { ok: true, labels: [] },
  SET_PREFS: (m) => ({ ok: true, prefs: Object.assign({}, DEFAULT_PREFS, m.prefs) }),
};

function panelOn(surface, extra) {
  return createPanel({
    responses: Object.assign({}, BASE, { DETECT_SURFACE: { ok: true, surface, url: "https://www.linkedin.com/" } }, extra),
  });
}

// --- detected interfaces ------------------------------------------------------

test("Sales Navigator listings: rows are selectable and the count is honest", async () => {
  const rows = [
    fixtures.record(),
    fixtures.record({ rawFullName: "Wei Zhang", companyName: "Delta Manufacturing", _stableKey: "k2" }),
  ];
  const p = await panelOn(SURFACES.SALESNAV_PEOPLE_RESULTS, {
    DETECT_ACTIVE_PAGE: {
      ok: true,
      page: { supported: true, url: "https://www.linkedin.com/sales/search/people", visibleCount: 2 },
    },
    CAPTURE_ACTIVE_PAGE: {
      ok: true,
      captureStatus: "ok",
      added: 2,
      collapsed: 0,
      uncertain: 0,
      batchView: fixtures.batchView(rows),
    },
  });
  await p.flush();
  assert.equal(p.view(), "listings-select");
  assert.match(p.contextLabel(), /Sales Navigator/);

  await p.click("capture-btn");
  assert.equal(p.view(), "listings-select");
  assert.match(p.viewText(), /Dana Whitfield/);
  assert.match(p.viewText(), /Wei Zhang/);
  // A ticked box means "included in the save" — the operator selects, not excludes.
  const boxes = p.document.querySelectorAll("#records input[type=checkbox]");
  assert.equal(boxes.length, 2);
  assert.ok(Array.from(boxes).every((b) => b.checked));
  assert.equal(p.$("listings-review-btn").textContent.trim(), "Review selected (2)");
  assert.equal(p.steps().text, "Step 1 of 3");
});

test("person profile: a clean read shows what was confirmed and offers capture", async () => {
  const p = await panelOn(SURFACES.PERSON_PROFILE, {
    PROFILE_CAPTURE: { ok: true, captureStatus: "ok", draftView: fixtures.profileDraftView() },
    PROFILE_DETECT: { ok: true, page: { surface: SURFACES.PERSON_PROFILE, experienceEntryCount: 1 } },
    PROFILE_MATCH_STATE: { ok: true, match: "none" },
  });
  await p.flush();
  assert.equal(p.view(), "person-review");
  assert.match(p.contextLabel(), /LinkedIn · Person profile/);
  assert.match(p.viewText(), /Dana Whitfield/);
  assert.match(p.viewText(), /Read from this page/);
  assert.match(p.viewText(), /Confirmed/);
  assert.equal(p.$("person-continue-btn").textContent.trim(), "Capture prospect");
});

test("company page: a website on the page is reported as shown, not inferred", async () => {
  const p = await panelOn(SURFACES.COMPANY_PROFILE, {
    COMPANY_GET_STATE: { ok: true, draftView: fixtures.companyDraftView() },
  });
  await p.flush();
  assert.equal(p.view(), "company-review");
  assert.match(p.contextLabel(), /LinkedIn · Company page/);
  assert.match(p.viewText(), /northwind-logistics\.com/);
  assert.match(p.viewText(), /Shown on this page/);
});

test("company page: a missing website is a stated gap, never a guess", async () => {
  const draft = fixtures.companyDraftView();
  draft.company.website = null;
  draft.company.industry = null;
  const p = await panelOn(SURFACES.COMPANY_PROFILE, { COMPANY_GET_STATE: { ok: true, draftView: draft } });
  await p.flush();
  assert.match(p.viewText(), /Domain not confirmed/);
  assert.match(p.viewText(), /Not shown on this page/);
  assert.match(p.viewText(), /A company name on its own isn't enough/);
  assert.equal(p.contextBadge(), "Needs review");
});

// --- blocked pages ------------------------------------------------------------

test("unsupported page: the reason is specific, and nothing was read", async () => {
  const p = await panelOn(SURFACES.UNSUPPORTED, {
    DETECT_SURFACE: {
      ok: true,
      surface: SURFACES.UNSUPPORTED,
      reason: "profile_subroute",
      url: "https://www.linkedin.com/in/dana/details/experience/",
    },
  });
  await p.flush();
  assert.equal(p.view(), "unsupported");
  assert.match(p.$("unsupported-detail").textContent, /profile sub-page/);
  assert.equal(p.actions(), "blocked");
  assert.equal(p.steps().hidden, true, "a blocked page is not step 1 of anything");
});

test("a Sales Navigator lead or account page is unsupported, and says so", async () => {
  const p = await panelOn(SURFACES.UNSUPPORTED, {
    DETECT_SURFACE: {
      ok: true,
      surface: SURFACES.UNSUPPORTED,
      reason: "unsupported_sales_surface",
      url: "https://www.linkedin.com/sales/lead/ACwAA",
    },
  });
  await p.flush();
  assert.equal(p.view(), "unsupported");
  assert.match(p.$("unsupported-detail").textContent, /people-search results page/);
});

test("sign-in or security check: the panel refuses to act", async () => {
  const p = await panelOn(SURFACES.CHALLENGE);
  await p.flush();
  assert.equal(p.view(), "challenge");
  assert.match(p.viewText(), /login wall or a security check/);
  assert.match(p.viewText(), /never acts during a security check/);
});

test("an unavailable page reports that nothing was read", async () => {
  const p = await panelOn(SURFACES.UNAVAILABLE);
  await p.flush();
  assert.equal(p.view(), "unavailable");
  assert.match(p.viewText(), /removed or hidden/);
});

// --- gaps stay visible --------------------------------------------------------

test("a partial profile read names what could not be read", async () => {
  const draft = fixtures.profileDraftView({
    status: "partial",
    missingSections: ["experience"],
    experiences: [],
    experienceCount: 0,
    currentRoles: [],
  });
  draft.profile.displayed_location = null;
  draft.profile.warnings = [{ code: "missing_field", field: "displayed_location" }];
  const p = await panelOn(SURFACES.PERSON_PROFILE, {
    PROFILE_GET_STATE: { ok: true, prefs: DEFAULT_PREFS, draftView: draft },
    PROFILE_CAPTURE: { ok: true, captureStatus: "partial", draftView: draft },
    PROFILE_MATCH_STATE: { ok: true, match: "none" },
  });
  await p.flush();
  assert.match(p.viewText(), /Some details could not be read/);
  assert.match(p.viewText(), /Missing company/);
  assert.match(p.viewText(), /location was not on the page/);
  assert.match(p.viewText(), /will not fill them in/);
  assert.equal(p.contextBadge(), "Needs review");
});

test("an already-saved person is told so, and the action becomes Refresh", async () => {
  const p = await panelOn(SURFACES.PERSON_PROFILE, {
    PROFILE_GET_STATE: { ok: true, prefs: DEFAULT_PREFS, draftView: fixtures.profileDraftView() },
    PROFILE_CAPTURE: { ok: true, captureStatus: "ok", draftView: fixtures.profileDraftView() },
    PROFILE_MATCH_STATE: { ok: true, match: "exact" },
  });
  await p.flush();
  assert.match(p.viewText(), /already in VM Prospector/);
  assert.match(p.viewText(), /never overwrites/);
  assert.equal(p.$("save-btn").textContent.trim(), "Refresh Contact");
  assert.equal(p.contextBadge(), "Already saved");
});

test("skipped rows are reported with their reason and never counted as captured", async () => {
  const p = await panelOn(SURFACES.SALESNAV_PEOPLE_RESULTS, {
    DETECT_ACTIVE_PAGE: {
      ok: true,
      page: { supported: true, url: "https://www.linkedin.com/sales/search/people", visibleCount: 3 },
    },
    CAPTURE_ACTIVE_PAGE: {
      ok: true,
      captureStatus: "ok",
      added: 1,
      collapsed: 0,
      uncertain: 0,
      skippedCount: 2,
      skipped: [
        { sourcePosition: 2, rawFullName: "Priya Raghunathan", reason: "no_company_name" },
        { sourcePosition: 5, rawFullName: null, reason: "no_company_name" },
      ],
      batchView: fixtures.batchView([fixtures.record()]),
    },
  });
  await p.flush();
  await p.click("capture-btn");
  assert.equal(p.$("skipped-card").hidden, false);
  assert.match(p.$("skipped-summary").textContent, /2 visible rows skipped/);
  assert.match(p.$("skipped-summary").textContent, /Nothing was guessed/);
  assert.match(p.$("skipped-list").textContent, /Priya Raghunathan/);
  assert.match(p.$("skipped-list").textContent, /no_company_name/);
  assert.match(p.$("capture-feedback").textContent, /2 skipped — no company name/);
  // The skipped rows are not in the batch and cannot be selected.
  assert.equal(p.document.querySelectorAll("#records input[type=checkbox]").length, 1);
});

// --- saving, outcome, failure -------------------------------------------------

async function listingsAtReview(saveResponse) {
  const p = await panelOn(SURFACES.SALESNAV_PEOPLE_RESULTS, {
    DETECT_ACTIVE_PAGE: {
      ok: true,
      page: { supported: true, url: "https://www.linkedin.com/sales/search/people", visibleCount: 2 },
    },
    GET_STATE: {
      ok: true,
      prefs: DEFAULT_PREFS,
      metadata: { labels: [], note: null },
      batchView: fixtures.batchView([fixtures.record(), fixtures.record({ rawFullName: "Wei Zhang", _stableKey: "k2" })]),
    },
    SAVE_INCLUDED_CONTACTS: saveResponse,
  });
  await p.flush();
  await p.click("listings-review-btn");
  return p;
}

test("the review step states exactly what will be submitted", async () => {
  const p = await listingsAtReview({ ok: true, result: {} });
  assert.equal(p.view(), "listings-review");
  assert.equal(p.steps().text, "Step 2 of 3");
  assert.equal(p.$("review-total").textContent, "2 prospects");
  assert.equal(p.$("save-btn").textContent.trim(), "Capture 2 prospects");
  assert.match(p.viewText(), /Save to VMR/);
  assert.equal(p.$("metadata-card").hidden, false, "labels and note belong to the review step");
});

test("a successful save reports the backend's own outcomes", async () => {
  const p = await listingsAtReview({
    ok: true,
    result: {
      counts: { created: 1, staged_unmatched: 1 },
      results: [
        { outcome: "created", contactUrl: "http://127.0.0.1:8000/contacts/1" },
        { outcome: "staged_unmatched" },
      ],
      workbenchUrl: "http://127.0.0.1:8000/contact-captures/9",
    },
  });
  await p.click("save-btn");
  assert.equal(p.view(), "outcome");
  assert.equal(p.steps().text, "Done");
  assert.match(p.viewText(), /2 of 2 prospects saved/);
  assert.match(p.viewText(), /captured as a new contact/);
  assert.match(p.viewText(), /staged as a new person/);
  assert.match(p.viewText(), /1 need review in VM Prospector/);
  assert.match(p.viewText(), /Identity was not guessed/);
  assert.equal(p.connection(), "Connected");
});

test("an unreachable backend says nothing was saved and keeps the draft", async () => {
  const p = await listingsAtReview({ ok: false, error: "network_error" });
  await p.click("save-btn");
  assert.equal(p.view(), "outcome");
  assert.equal(p.steps().text, "Failed");
  assert.match(p.viewText(), /Connection lost/);
  assert.match(p.viewText(), /Nothing was saved/);
  assert.match(p.viewText(), /still here/);
  assert.equal(p.connection(), "Not connected");
  assert.equal(p.$("outcome-primary").hidden, false);
  assert.equal(p.$("outcome-primary").textContent.trim(), "Try again");
  // The file fallback is offered when the backend cannot be reached.
  assert.equal(p.$("export-row").hidden, false);
});

test("a rejected submission is not offered a pointless retry", async () => {
  const p = await listingsAtReview({
    ok: false,
    error: "receiver_rejected",
    status: 422,
    body: { error: "validation_failed", details: [1, 2] },
  });
  await p.click("save-btn");
  assert.match(p.viewText(), /failed backend validation/);
  assert.match(p.viewText(), /2 validation issue/);
  assert.equal(p.$("outcome-primary").hidden, true, "validation failures must not offer retry");
});

test("a refused loopback permission explains that nothing was sent", async () => {
  const p = await createPanel({
    responses: Object.assign({}, BASE, {
      DETECT_SURFACE: { ok: true, surface: SURFACES.SALESNAV_PEOPLE_RESULTS, url: "https://www.linkedin.com/sales/search/people" },
      DETECT_ACTIVE_PAGE: { ok: true, page: { supported: true, url: "https://www.linkedin.com/sales/search/people", visibleCount: 1 } },
      GET_STATE: { ok: true, prefs: DEFAULT_PREFS, metadata: { labels: [], note: null }, batchView: fixtures.batchView() },
    }),
    permission: { granted: false, grantOnRequest: false },
  });
  await p.flush();
  await p.click("listings-review-btn");
  await p.click("save-btn");
  assert.match(p.viewText(), /Allow VM Prospector to reach the app/);
  assert.match(p.viewText(), /Nothing has been sent/);
  assert.equal(p.connection(), "Not allowed yet");
  assert.equal(p.$("outcome-primary").textContent.trim(), "Allow and save");
});

test("a retained draft and its saved outcome survive reopening the panel", async () => {
  const p = await panelOn(SURFACES.PERSON_PROFILE, {
    PROFILE_GET_STATE: {
      ok: true,
      prefs: DEFAULT_PREFS,
      draftView: fixtures.profileDraftView(),
      lastResult: {
        counts: { created: 1 },
        results: [{ outcome: "created", contactUrl: "http://127.0.0.1:8000/contacts/1" }],
      },
    },
    PROFILE_CAPTURE: { ok: true, captureStatus: "ok", draftView: fixtures.profileDraftView() },
    PROFILE_MATCH_STATE: { ok: true, match: "none" },
  });
  await p.flush();
  // The draft is still reviewable...
  assert.match(p.$("profile-review").textContent, /Dana Whitfield/);
  // ...and the outcome was restored without recapturing or resaving.
  assert.match(p.$("save-state").textContent, /Prospect saved/);
  assert.ok(!p.sent.some((m) => m.type === "SAVE_CONTACT"), "reopening must not resave");
  assert.ok(
    !p.sent.some((m) => m.type === "CAPTURE_ACTIVE_PAGE"),
    "reopening must not recapture the listing batch"
  );
});

// --- naming -------------------------------------------------------------------

test("the product is VM Prospector everywhere the operator can see", () => {
  const html = fs.readFileSync(path.join(SRC, "sidepanel", "sidepanel.html"), "utf8");
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "manifest.json"), "utf8"));
  assert.equal(manifest.name, "VM Prospector");
  assert.equal(manifest.action.default_title, "VM Prospector");
  assert.match(html, /<title>VM Prospector<\/title>/);
  for (const stale of ["VM Sales", "VMR Contact Capture"]) {
    assert.ok(!html.includes(stale), `panel still says ${stale}`);
    assert.ok(!JSON.stringify(manifest).includes(stale), `manifest still says ${stale}`);
  }
});

test("the panel heading and the accessible connection label name the product", async () => {
  const p = await panelOn(SURFACES.UNSUPPORTED);
  await p.flush();
  assert.equal(p.$("app-title").textContent.trim(), "VM Prospector");
  assert.match(p.$("conn-status").getAttribute("aria-label"), /VM Prospector/);
});

// --- safety -------------------------------------------------------------------

test("no side-panel source assigns innerHTML or outerHTML", () => {
  for (const file of ["sidepanel.js", "sidepanel-profile.js", "shell.js"]) {
    const src = fs.readFileSync(path.join(SRC, "sidepanel", file), "utf8");
    assert.ok(!/\.innerHTML\s*=/.test(src), `${file} assigns innerHTML`);
    assert.ok(!/\.outerHTML\s*=/.test(src), `${file} assigns outerHTML`);
    assert.ok(!/insertAdjacentHTML/.test(src), `${file} uses insertAdjacentHTML`);
  }
});

test("captured page content is rendered as text, never as markup", async () => {
  const hostile = '<img src=x onerror="alert(1)">';
  const p = await panelOn(SURFACES.SALESNAV_PEOPLE_RESULTS, {
    DETECT_ACTIVE_PAGE: { ok: true, page: { supported: true, url: "https://www.linkedin.com/sales/search/people", visibleCount: 1 } },
    GET_STATE: {
      ok: true,
      prefs: DEFAULT_PREFS,
      metadata: { labels: [], note: null },
      batchView: fixtures.batchView([fixtures.record({ rawFullName: hostile })]),
    },
  });
  await p.flush();
  assert.equal(p.document.querySelectorAll("#records img").length, 0);
  assert.match(p.$("records").textContent, /onerror/);
});

// --- side-panel fit -----------------------------------------------------------

test("the shell keeps the dominant action out of the scrolling region", async () => {
  const p = await panelOn(SURFACES.UNSUPPORTED);
  const body = p.$("app-body");
  const actions = p.document.querySelector(".app-actions");
  assert.ok(actions, "the sticky action footer is missing");
  assert.ok(!body.contains(actions), "actions must not scroll away with the body");
});

test("the stylesheet adapts to the narrowest realistic side panel", () => {
  const css = fs.readFileSync(path.join(SRC, "sidepanel", "sidepanel.css"), "utf8");
  assert.match(css, /@media \(max-width: 344px\)/);
  // Long values must wrap rather than force the panel to scroll sideways.
  assert.match(css, /overflow-wrap: anywhere/);
});

// --- typography ---------------------------------------------------------------

test("the three design-system faces are bundled, not substituted", () => {
  const fontsCss = fs.readFileSync(path.join(SRC, "sidepanel", "fonts.css"), "utf8");
  const tokens = fs.readFileSync(path.join(SRC, "sidepanel", "tokens.css"), "utf8");
  const dir = path.join(SRC, "sidepanel", "fonts");

  // Every face the design system specifies is declared and reachable locally.
  for (const family of ["Manrope", "Source Sans 3", "IBM Plex Mono"]) {
    assert.ok(fontsCss.includes(`font-family: "${family}"`), `${family} is not declared`);
  }
  const referenced = [...fontsCss.matchAll(/url\("fonts\/([^"]+)"\)/g)].map((m) => m[1]);
  for (const file of referenced) {
    assert.ok(fs.existsSync(path.join(dir, file)), `missing bundled font file: ${file}`);
  }

  // Every weight the stylesheet actually asks for must be a real file, or the
  // browser synthesises it and the face stops being the designed one.
  const declared = new Set(
    [...fontsCss.matchAll(/font-family: "([^"]+)";\s*\n\s*font-style: normal;\s*\n\s*font-weight: (\d+)/g)].map(
      (m) => `${m[1]}/${m[2]}`
    )
  );
  const required = [
    ["Manrope", [600, 700, 800]],
    ["Source Sans 3", [400, 600, 700]],
    ["IBM Plex Mono", [400, 500, 600]],
  ];
  for (const [family, weights] of required) {
    for (const weight of weights) {
      assert.ok(declared.has(`${family}/${weight}`), `${family} ${weight} is not bundled`);
    }
  }

  // And each role actually resolves to its face first, with the platform stack
  // kept behind it for text outside the bundled subsets.
  assert.match(tokens, /--font-display:\s*"Manrope"/);
  assert.match(tokens, /--font-body:\s*"Source Sans 3"/);
  assert.match(tokens, /--font-mono:\s*"IBM Plex Mono"/);
  for (const role of ["--font-display", "--font-body", "--font-mono"]) {
    const decl = tokens.slice(tokens.indexOf(role));
    assert.match(decl.slice(0, decl.indexOf(";")), /,/, `${role} has no fallback stack`);
  }

  // Bundled fonts are redistributed, so the licence ships with them.
  assert.ok(fs.existsSync(path.join(dir, "OFL.txt")), "the font licence is not bundled");
  assert.match(fs.readFileSync(path.join(dir, "OFL.txt"), "utf8"), /SIL Open Font License/);
});

test("the panel links the bundled faces before the tokens that use them", () => {
  const html = fs.readFileSync(path.join(SRC, "sidepanel", "sidepanel.html"), "utf8");
  assert.ok(html.indexOf('href="fonts.css"') > -1, "fonts.css is not linked");
  assert.ok(
    html.indexOf('href="fonts.css"') < html.indexOf('href="tokens.css"'),
    "fonts.css must be linked before tokens.css"
  );
});

test("no remote font or stylesheet is referenced from the panel", () => {
  const html = fs.readFileSync(path.join(SRC, "sidepanel", "sidepanel.html"), "utf8");
  const tokens = fs.readFileSync(path.join(SRC, "sidepanel", "tokens.css"), "utf8");
  const css = fs.readFileSync(path.join(SRC, "sidepanel", "sidepanel.css"), "utf8");
  const fontsCss = fs.readFileSync(path.join(SRC, "sidepanel", "fonts.css"), "utf8");
  for (const [name, src] of [
    ["sidepanel.html", html],
    ["tokens.css", tokens],
    ["sidepanel.css", css],
    ["fonts.css", fontsCss],
  ]) {
    const withoutComments = src.replace(/<!--[\s\S]*?-->/g, "").replace(/\/\*[\s\S]*?\*\//g, "");
    const remote = (withoutComments.match(/https?:\/\/[^\s"'()]+/g) || []).filter(
      (u) => !/^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])/.test(u)
    );
    assert.deepEqual(remote, [], `${name} references a remote resource`);
    assert.ok(!/@import/.test(src), `${name} uses @import`);
  }
});

// --- the connection badge -----------------------------------------------------
//
// It used to be written only as a side effect of a save, which made it a record of
// the last save rather than a statement about the backend. A failed save latched it
// to "Not connected" and nothing could clear it — so it stayed wrong after the
// backend came back, on the one screen whose whole purpose is the connection.

test("a cold open probes the backend instead of reporting Not checked", async () => {
  const p = await panelOn(SURFACES.SALESNAV_SEARCH, {
    PROBE_BACKEND: { ok: true, state: "connected" },
  });
  await p.flush();
  await p.flush();
  assert.equal(p.$("conn-text").textContent, "Connected");
});

test("an unreachable backend is reported as such, not as Ready", async () => {
  const p = await panelOn(SURFACES.SALESNAV_SEARCH, {
    PROBE_BACKEND: { ok: false, state: "unreachable", error: "timeout" },
  });
  await p.flush();
  await p.flush();
  assert.equal(p.$("conn-text").textContent, "Not connected");
});

test("a recovered backend clears the badge rather than staying stuck", async () => {
  // The exact defect: one failed save latched "Not connected", and
  // refreshPermissionState() returned early for that state forever after.
  let reachable = false;
  const p = await panelOn(SURFACES.SALESNAV_SEARCH, {
    PROBE_BACKEND: () =>
      reachable ? { ok: true, state: "connected" } : { ok: false, state: "unreachable" },
  });
  await p.flush();
  await p.flush();
  assert.equal(p.$("conn-text").textContent, "Not connected");

  // The backend comes back, and the operator opens settings to check.
  reachable = true;
  p.click("settings-toggle");
  await p.flush();
  await p.flush();
  assert.equal(
    p.$("conn-text").textContent,
    "Connected",
    "a probe on open must be able to clear a latched failure"
  );
});

test("a denied loopback permission is not reported as an unreachable backend", async () => {
  // Two different problems with two different fixes. Telling an operator to start
  // a server that is already running wastes their time.
  const p = await createPanel({
    responses: Object.assign({}, BASE, {
      DETECT_SURFACE: { ok: true, surface: SURFACES.SALESNAV_SEARCH, url: "https://www.linkedin.com/" },
      PROBE_BACKEND: { ok: false, state: "unreachable" },
    }),
    permission: { granted: false },
  });
  await p.flush();
  await p.flush();
  assert.equal(p.$("conn-text").textContent, "Not allowed yet");
});

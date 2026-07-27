"use strict";
/**
 * Render every VM Prospector side-panel state to a standalone HTML file.
 *
 *   node tools/render-panel-states.js [outdir]
 *
 * Each file is the REAL panel — the shipped `sidepanel.html`, the shipped
 * stylesheets, and the shipped controllers driven through a stubbed `chrome.*`
 * — serialized after the state has been reached. Nothing is mocked up by hand,
 * so a screenshot of one of these is a screenshot of what ships.
 *
 * Dev tooling only: it is never loaded by the extension.
 */
const fs = require("fs");
const path = require("path");

const { createPanel, DEFAULT_PREFS, fixtures } = require("../test/panel-harness.js");
const SURFACES = require("../src/common/constants.js").SURFACES;

const SRC = path.join(__dirname, "..", "src", "sidepanel");

const BASE = {
  GET_STATE: { ok: true, prefs: DEFAULT_PREFS, metadata: { labels: [], note: null }, batchView: null },
  PROFILE_GET_STATE: { ok: true, prefs: DEFAULT_PREFS, draftView: null },
  COMPANY_GET_STATE: { ok: true, draftView: null },
  FETCH_LABELS: { ok: true, labels: [] },
  SET_PREFS: (m) => ({ ok: true, prefs: Object.assign({}, DEFAULT_PREFS, m.prefs) }),
};

const LISTING_PAGE = {
  ok: true,
  page: { supported: true, url: "https://www.linkedin.com/sales/search/people", visibleCount: 25 },
};

function rows() {
  return [
    fixtures.record(),
    fixtures.record({
      rawFullName: "Marcus O'Neill",
      title: "VP Revenue Operations",
      companyName: "Cliffside Software",
      location: "Austin, Texas",
      _stableKey: "k2",
    }),
    fixtures.record({
      rawFullName: "Priya Raghunathan",
      title: "Director of Procurement",
      companyName: null,
      location: null,
      warnings: [{ code: "missing_field", field: "companyName" }],
      _stableKey: "k3",
    }),
    fixtures.record({
      rawFullName: "Wei Zhang",
      title: "Plant Manager",
      companyName: "Delta Manufacturing",
      linkedinProfileUrl: null,
      salesNavLeadUrl: null,
      warnings: [{ code: "no_stable_identity", field: "stableKey" }],
      _stableKey: null,
    }),
  ];
}

const STATES = [
  {
    name: "A0-detecting",
    async build() {
      // Held at the classification step: the detect never answers, which is
      // exactly the frame the operator sees while the page is being read.
      return createPanel({
        responses: Object.assign({}, BASE, { DETECT_SURFACE: () => new Promise(() => {}) }),
      });
    },
  },
  {
    name: "A1-listings-select",
    async build() {
      const p = await createPanel({
        responses: Object.assign({}, BASE, {
          DETECT_SURFACE: { ok: true, surface: SURFACES.SALESNAV_PEOPLE_RESULTS, url: LISTING_PAGE.page.url },
          DETECT_ACTIVE_PAGE: LISTING_PAGE,
          GET_STATE: {
            ok: true,
            prefs: DEFAULT_PREFS,
            metadata: { labels: [], note: null },
            batchView: fixtures.batchView(rows()),
          },
        }),
      });
      await p.flush();
      return p;
    },
  },
  {
    name: "A1b-listings-skipped-rows",
    async build() {
      const p = await createPanel({
        responses: Object.assign({}, BASE, {
          DETECT_SURFACE: { ok: true, surface: SURFACES.SALESNAV_PEOPLE_RESULTS, url: LISTING_PAGE.page.url },
          DETECT_ACTIVE_PAGE: LISTING_PAGE,
          CAPTURE_ACTIVE_PAGE: {
            ok: true,
            captureStatus: "ok",
            added: 4,
            collapsed: 1,
            uncertain: 0,
            skippedCount: 2,
            skipped: [
              { sourcePosition: 3, rawFullName: "Alex Moreau", reason: "no_company_name" },
              { sourcePosition: 7, rawFullName: null, reason: "no_company_name" },
            ],
            batchView: fixtures.batchView(rows()),
          },
        }),
      });
      await p.flush();
      await p.click("capture-btn");
      return p;
    },
  },
  {
    name: "A1c-listings-reading-cancellable",
    async build() {
      // Held mid-pass: the read never resolves, which is the frame in which the
      // operator can stop it.
      const p = await createPanel({
        responses: Object.assign({}, BASE, {
          DETECT_SURFACE: { ok: true, surface: SURFACES.SALESNAV_PEOPLE_RESULTS, url: LISTING_PAGE.page.url },
          DETECT_ACTIVE_PAGE: LISTING_PAGE,
          CAPTURE_ACTIVE_PAGE: () => new Promise(() => {}),
        }),
      });
      await p.flush();
      await p.click("capture-btn");
      await p.emit({ type: "CS_SCROLL_PROGRESS", passId: 1, progress: { phase: "step", rows: 18 } });
      return p;
    },
  },
  {
    name: "A2-listings-empty",
    async build() {
      const p = await createPanel({
        responses: Object.assign({}, BASE, {
          DETECT_SURFACE: { ok: true, surface: SURFACES.SALESNAV_PEOPLE_RESULTS, url: LISTING_PAGE.page.url },
          DETECT_ACTIVE_PAGE: LISTING_PAGE,
          CAPTURE_ACTIVE_PAGE: {
            ok: true,
            captureStatus: "empty",
            added: 0,
            pageWarnings: [],
            batchView: fixtures.batchView([]),
          },
        }),
      });
      await p.flush();
      await p.click("capture-btn");
      return p;
    },
  },
  {
    name: "A3-listings-review",
    async build() {
      const p = await listingsWithSave({ ok: true, result: {} });
      await p.click("listings-review-btn");
      return p;
    },
  },
  {
    name: "A4-saving",
    async build() {
      // Held mid-flight: the save never resolves, which is exactly the frame.
      const p = await listingsWithSave(() => new Promise(() => {}));
      await p.click("listings-review-btn");
      p.$("save-btn").dispatchEvent(new p.window.Event("click", { bubbles: true }));
      await p.flush();
      return p;
    },
  },
  {
    name: "A5-listings-result-partial",
    async build() {
      const p = await listingsWithSave({
        ok: true,
        result: {
          counts: { created: 2, refreshed_exact_match: 1, staged_unmatched: 1 },
          results: [
            { outcome: "created", contactUrl: "http://127.0.0.1:8000/contacts/1" },
            { outcome: "created" },
            { outcome: "refreshed_exact_match" },
            { outcome: "staged_unmatched" },
          ],
          workbenchUrl: "http://127.0.0.1:8000/contact-captures/42",
        },
      });
      await p.click("listings-review-btn");
      await p.click("save-btn");
      return p;
    },
  },
  {
    name: "A6-backend-unavailable",
    async build() {
      const p = await listingsWithSave({ ok: false, error: "network_error" });
      await p.click("listings-review-btn");
      await p.click("save-btn");
      return p;
    },
  },
  {
    name: "B1-person-ready",
    build: () => person({ match: "none" }),
  },
  {
    name: "B2-person-already-saved",
    build: () => person({ match: "exact" }),
  },
  {
    name: "B3-person-partial",
    build: () => {
      const draft = fixtures.profileDraftView({
        status: "partial",
        missingSections: ["experience"],
        experiences: [],
        experienceCount: 0,
        currentRoles: [],
      });
      draft.profile.full_name = "Priya Raghunathan";
      draft.profile.headline = "Director of Procurement";
      draft.profile.displayed_location = null;
      draft.profile.warnings = [{ code: "missing_field", field: "displayed_location" }];
      return person({ match: "none", draft });
    },
  },
  {
    name: "B4-person-confirm",
    async build() {
      const p = await person({ match: "none" });
      await p.click("person-continue-btn");
      return p;
    },
  },
  {
    name: "B5-person-details",
    async build() {
      const p = await person({ match: "none" });
      await p.click("person-continue-btn");
      await p.click("person-details-btn");
      return p;
    },
  },
  {
    name: "B6-person-saved",
    async build() {
      const p = await person({
        match: "none",
        save: {
          ok: true,
          result: {
            counts: { created: 1 },
            results: [{ outcome: "created", contactUrl: "http://127.0.0.1:8000/contacts/7" }],
            workbenchUrl: "http://127.0.0.1:8000/contact-captures/7",
          },
        },
      });
      await p.click("person-continue-btn");
      await p.click("save-btn");
      return p;
    },
  },
  {
    name: "B7-person-failed",
    async build() {
      const p = await person({
        match: "none",
        save: { ok: false, error: "receiver_rejected", status: 422, body: { error: "validation_failed", details: [1] } },
      });
      await p.click("person-continue-btn");
      await p.click("save-btn");
      return p;
    },
  },
  {
    name: "C1-company-ready",
    build: () => company(fixtures.companyDraftView()),
  },
  {
    name: "C2-company-domain-not-confirmed",
    build: () => {
      const draft = fixtures.companyDraftView();
      draft.company.name = "Harbor Freight Collective";
      draft.company.website = null;
      draft.company.industry = null;
      draft.company.size_range = null;
      draft.company.headquarters_text = null;
      return company(draft);
    },
  },
  {
    name: "C4-company-confirm",
    async build() {
      const p = await company(fixtures.companyDraftView());
      await p.click("company-continue-btn");
      return p;
    },
  },
  {
    name: "C5-company-saved",
    async build() {
      const p = await company(fixtures.companyDraftView(), {
        COMPANY_SEND: {
          ok: true,
          result: { outcome: "stored", snapshotId: "snap-1", workbenchUrl: "http://127.0.0.1:8000/imports/3" },
        },
      });
      await p.click("company-continue-btn");
      await p.click("company-send-btn");
      return p;
    },
  },
  {
    name: "S1-unsupported",
    build: () =>
      blocked(SURFACES.UNSUPPORTED, {
        reason: "profile_subroute",
        url: "https://www.linkedin.com/in/danawhitfield/details/experience/",
      }),
  },
  {
    name: "S2-challenge",
    build: () => blocked(SURFACES.CHALLENGE, { url: "https://www.linkedin.com/checkpoint/challenge" }),
  },
  {
    name: "S3-unavailable",
    build: () => blocked(SURFACES.UNAVAILABLE, { url: "https://www.linkedin.com/company/unavailable" }),
  },
  {
    name: "S4-permission-needed",
    async build() {
      const p = await createPanel({
        responses: Object.assign({}, BASE, {
          DETECT_SURFACE: { ok: true, surface: SURFACES.SALESNAV_PEOPLE_RESULTS, url: LISTING_PAGE.page.url },
          DETECT_ACTIVE_PAGE: LISTING_PAGE,
          GET_STATE: {
            ok: true,
            prefs: DEFAULT_PREFS,
            metadata: { labels: [], note: null },
            batchView: fixtures.batchView(rows()),
          },
        }),
        permission: { granted: false, grantOnRequest: false },
      });
      await p.flush();
      await p.click("listings-review-btn");
      await p.click("save-btn");
      return p;
    },
  },
  {
    name: "S5-archived-drafts",
    async build() {
      const p = await createPanel({
        responses: Object.assign({}, BASE, {
          DETECT_SURFACE: { ok: true, surface: SURFACES.PERSON_PROFILE, url: "https://www.linkedin.com/in/danawhitfield" },
          PROFILE_GET_STATE: { ok: true, prefs: DEFAULT_PREFS, draftView: fixtures.profileDraftView() },
          PROFILE_CAPTURE: { ok: true, captureStatus: "ok", draftView: fixtures.profileDraftView() },
          PROFILE_MATCH_STATE: { ok: true, match: "none" },
          GET_STATE: {
            ok: true,
            prefs: DEFAULT_PREFS,
            metadata: { labels: [], note: null },
            batchView: null,
            migration: { hasArchive: true },
          },
        }),
      });
      await p.flush();
      return p;
    },
  },
  {
    name: "S7-settings",
    async build() {
      const p = await blocked(SURFACES.UNSUPPORTED, { reason: "unrecognized_route", url: "https://example.com/" });
      await p.click("settings-toggle");
      return p;
    },
  },
];

async function listingsWithSave(saveResponse) {
  const p = await createPanel({
    responses: Object.assign({}, BASE, {
      DETECT_SURFACE: { ok: true, surface: SURFACES.SALESNAV_PEOPLE_RESULTS, url: LISTING_PAGE.page.url },
      DETECT_ACTIVE_PAGE: LISTING_PAGE,
      GET_STATE: {
        ok: true,
        prefs: DEFAULT_PREFS,
        metadata: { labels: ["Healthcare"], note: null },
        batchView: fixtures.batchView(rows()),
      },
      SAVE_INCLUDED_CONTACTS: saveResponse,
    }),
  });
  await p.flush();
  return p;
}

async function person(options) {
  const o = options || {};
  const draft = o.draft || fixtures.profileDraftView();
  const p = await createPanel({
    responses: Object.assign({}, BASE, {
      DETECT_SURFACE: { ok: true, surface: SURFACES.PERSON_PROFILE, url: "https://www.linkedin.com/in/danawhitfield" },
      PROFILE_GET_STATE: { ok: true, prefs: DEFAULT_PREFS, draftView: draft },
      PROFILE_CAPTURE: { ok: true, captureStatus: draft.status, draftView: draft },
      PROFILE_DETECT: { ok: true, page: { surface: SURFACES.PERSON_PROFILE, experienceEntryCount: draft.experienceCount } },
      PROFILE_MATCH_STATE: { ok: true, match: o.match || "none" },
      SAVE_CONTACT: o.save || { ok: true, result: {} },
    }),
  });
  await p.flush();
  return p;
}

async function company(draft, extra) {
  const p = await createPanel({
    responses: Object.assign(
      {},
      BASE,
      {
        DETECT_SURFACE: { ok: true, surface: SURFACES.COMPANY_PROFILE, url: "https://www.linkedin.com/company/northwind/about" },
        COMPANY_GET_STATE: { ok: true, draftView: draft },
      },
      extra
    ),
  });
  await p.flush();
  return p;
}

async function blocked(surface, detect) {
  const p = await createPanel({
    responses: Object.assign({}, BASE, {
      DETECT_SURFACE: Object.assign({ ok: true, surface }, detect),
    }),
  });
  await p.flush();
  return p;
}

async function main() {
  const outdir = path.resolve(process.argv[2] || path.join(__dirname, "..", "..", "..", "panel-states"));
  fs.mkdirSync(path.join(outdir, "assets"), { recursive: true });
  fs.mkdirSync(path.join(outdir, "fonts"), { recursive: true });
  for (const file of ["fonts.css", "tokens.css", "sidepanel.css"]) {
    fs.copyFileSync(path.join(SRC, file), path.join(outdir, file));
  }
  fs.copyFileSync(path.join(SRC, "assets", "vmr-mark.svg"), path.join(outdir, "assets", "vmr-mark.svg"));
  // The bundled faces travel with the snapshot, so a rendered state shows the
  // panel's real typography rather than whatever the viewer happens to have.
  for (const font of fs.readdirSync(path.join(SRC, "fonts"))) {
    fs.copyFileSync(path.join(SRC, "fonts", font), path.join(outdir, "fonts", font));
  }

  const written = [];
  for (const state of STATES) {
    const panel = await state.build();
    // Let any init still in flight settle before the DOM is frozen.
    await panel.flush();
    // Checked-ness and field values are DOM *properties*; serializing drops
    // them, which would make a snapshot show every box unticked. Reflect them
    // to attributes so the file renders the state that was actually reached.
    for (const input of panel.document.querySelectorAll("input")) {
      if (input.type === "checkbox" || input.type === "radio") {
        if (input.checked) input.setAttribute("checked", "");
        else input.removeAttribute("checked");
      } else if (input.value) {
        input.setAttribute("value", input.value);
      }
    }
    for (const select of panel.document.querySelectorAll("select")) {
      for (const option of select.options) {
        if (option.value === select.value) option.setAttribute("selected", "");
        else option.removeAttribute("selected");
      }
    }
    const html = panel.dom.serialize();
    const file = path.join(outdir, `${state.name}.html`);
    fs.writeFileSync(file, html, "utf8");
    written.push({ name: state.name, view: panel.view(), file });
  }
  process.stdout.write(JSON.stringify({ outdir, states: written }, null, 2) + "\n");
}

if (require.main === module) {
  main().catch((e) => {
    process.stderr.write(String((e && e.stack) || e) + "\n");
    process.exit(1);
  });
}

module.exports = { STATES };

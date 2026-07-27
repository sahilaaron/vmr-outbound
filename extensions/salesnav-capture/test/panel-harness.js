"use strict";
/**
 * Side-panel test harness.
 *
 * Loads the REAL shipped side panel — `src/sidepanel/sidepanel.html`, its
 * stylesheets, and the real shared modules and controllers — into jsdom with a
 * stubbed `chrome.*`, so a test drives exactly what the operator sees. Nothing
 * about the panel is re-implemented here; only the browser edges (messaging,
 * optional permissions, the manifest) are faked.
 *
 * Used by test/sidepanel-ui.test.js and by tools/render-panel-states.js.
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const SRC = path.join(__dirname, "..", "src");

const SCRIPTS = [
  ["common", "constants.js"],
  ["common", "normalize.js"],
  ["common", "dedupe.js"],
  ["common", "schema.js"],
  ["common", "contact-schema.js"],
  ["common", "permissions.js"],
  ["common", "handoff.js"],
  ["common", "live-sync.js"],
  ["sidepanel", "shell.js"],
  ["sidepanel", "sidepanel.js"],
  ["sidepanel", "sidepanel-profile.js"],
];

const DEFAULT_PREFS = {
  backendBaseUrl: "http://127.0.0.1:8000",
  sendTarget: "mock",
  mockReceiverUrl: "http://127.0.0.1:8787/api/intake/contact-captures",
  maxRecordsPerBatch: 500,
  recentLabels: [],
};

/** Let queued promise callbacks (and any zero-delay timers) run. */
function flush(times) {
  let p = Promise.resolve();
  for (let i = 0; i < (times || 6); i += 1) {
    p = p.then(() => new Promise((r) => setTimeout(r, 0)));
  }
  return p;
}

/**
 * @param {object} options
 *   responses: message type -> value | (msg) => value
 *   permission: { granted, grantOnRequest }
 */
async function createPanel(options) {
  const o = options || {};
  const responses = Object.assign({}, o.responses);
  const permission = Object.assign({ granted: true, grantOnRequest: true }, o.permission);
  const sent = [];

  const html = fs
    .readFileSync(path.join(SRC, "sidepanel", "sidepanel.html"), "utf8")
    .replace(/<script\s+src="[^"]*"><\/script>/g, "");

  const dom = new JSDOM(html, {
    runScripts: "dangerously",
    url: "https://panel.test/sidepanel.html",
    pretendToBeVisual: true,
  });
  const window = dom.window;

  function handle(message) {
    sent.push(message);
    const entry = responses[message && message.type];
    if (entry === undefined) return { ok: false, error: "unhandled", type: message && message.type };
    return typeof entry === "function" ? entry(message) : entry;
  }

  const noopEvent = { addListener() {}, removeListener() {} };
  window.chrome = {
    runtime: {
      lastError: null,
      sendMessage(message, callback) {
        Promise.resolve()
          .then(() => handle(message))
          .then((result) => callback && callback(result));
      },
      getManifest: () => ({ version: "2.0.0", name: "VM Prospector" }),
      onMessage: noopEvent,
    },
    permissions: {
      contains: () => Promise.resolve(permission.granted),
      request: () => Promise.resolve(permission.grantOnRequest),
    },
    tabs: { onUpdated: noopEvent, onActivated: noopEvent, onRemoved: noopEvent },
    webNavigation: { onHistoryStateUpdated: noopEvent, onCompleted: noopEvent },
  };
  window.confirm = () => true;

  // jsdom fires its OWN DOMContentLoaded a tick after construction. Wait for
  // it before loading the controllers, so their listeners see exactly one
  // event — the explicit one below — and each init runs once.
  await new Promise((resolve) => {
    if (window.document.readyState === "loading") {
      window.document.addEventListener("DOMContentLoaded", () => setTimeout(resolve, 0));
    } else {
      setTimeout(resolve, 0);
    }
  });

  for (const parts of SCRIPTS) {
    window.eval(fs.readFileSync(path.join(SRC, ...parts), "utf8"));
  }
  window.document.dispatchEvent(new window.Event("DOMContentLoaded"));

  const document = window.document;
  const $ = (id) => document.getElementById(id);

  return {
    dom,
    window,
    document,
    $,
    sent,
    permission,
    responses,
    flush,
    /** The view currently on screen. */
    view() {
      const node = Array.from(document.querySelectorAll("[data-view]")).find((n) => !n.hidden);
      return node ? node.getAttribute("data-view") : null;
    },
    /** The action group currently on screen. */
    actions() {
      const node = Array.from(document.querySelectorAll("[data-actions]")).find((n) => !n.hidden);
      return node ? node.getAttribute("data-actions") : null;
    },
    /** Visible text of the shell + body, as the operator would read it. */
    text() {
      return document.querySelector(".app").textContent.replace(/\s+/g, " ").trim();
    },
    viewText() {
      const node = Array.from(document.querySelectorAll("[data-view]")).find((n) => !n.hidden);
      return node ? node.textContent.replace(/\s+/g, " ").trim() : "";
    },
    contextLabel: () => $("context-label").textContent,
    contextBadge: () => $("context-badge").textContent.trim(),
    connection: () => $("conn-text").textContent,
    steps() {
      const wrap = $("steps");
      return {
        hidden: wrap.hidden,
        text: $("steps-text").textContent,
        on: Array.from(wrap.querySelectorAll(".steps-track i")).map((i) => i.className),
      };
    },
    async click(id) {
      $(id).dispatchEvent(new window.Event("click", { bubbles: true }));
      await flush();
    },
    async check(id, checked) {
      $(id).checked = checked;
      $(id).dispatchEvent(new window.Event("change", { bubbles: true }));
      await flush();
    },
  };
}

// ---- fixtures ---------------------------------------------------------------

function record(overrides) {
  return Object.assign(
    {
      rawFullName: "Dana Whitfield",
      firstName: "Dana",
      lastName: "Whitfield",
      title: "Head of Operations",
      companyName: "Northwind Logistics",
      location: "Greater Chicago Area",
      linkedinProfileUrl: "https://www.linkedin.com/in/danawhitfield",
      salesNavLeadUrl: null,
      companyLinkedInUrl: null,
      warnings: [],
      _excluded: false,
      _stableKey: "https://www.linkedin.com/in/danawhitfield",
    },
    overrides
  );
}

function batchView(records, overrides) {
  const list = records || [record()];
  const excluded = list.filter((r) => r._excluded).length;
  return Object.assign(
    {
      records: list,
      pagesCaptured: ["1"],
      summary: {
        total: list.length,
        included: list.length - excluded,
        excluded,
        withMissingFields: list.filter((r) =>
          (r.warnings || []).some((w) => w.code === "missing_field")
        ).length,
        selectorFailures: 0,
        uncertainIdentity: 0,
      },
    },
    overrides
  );
}

function profileDraftView(overrides) {
  return Object.assign(
    {
      clientCaptureId: "capture-1",
      capturedAt: "2026-07-27T10:00:00.000Z",
      status: "ok",
      profile: {
        full_name: "Dana Whitfield",
        headline: "Head of Operations at Northwind Logistics",
        displayed_location: "Greater Chicago Area",
        linkedin_profile_url: "https://www.linkedin.com/in/danawhitfield",
        connection_count: 500,
        about_text: null,
        open_to_work: null,
        warnings: [],
      },
      experiences: [
        {
          position_index: 1,
          job_title: "Head of Operations",
          company_name: "Northwind Logistics",
          company_linkedin_url: "https://www.linkedin.com/company/northwind",
          timeline_text: "2021 — Present",
          duration_text: "5 yrs",
          is_current: true,
          warnings: [],
        },
      ],
      experienceCount: 1,
      currentRoles: [{ job_title: "Head of Operations", company_name: "Northwind Logistics" }],
      missingSections: [],
      pageWarnings: [],
      excludedSections: [],
    },
    overrides
  );
}

function companyDraftView(overrides) {
  return Object.assign(
    {
      clientCaptureId: "company-1",
      capturedAt: "2026-07-27T10:00:00.000Z",
      status: "ok",
      company: {
        name: "Northwind Logistics",
        company_linkedin_url: "https://www.linkedin.com/company/northwind",
        website: "northwind-logistics.com",
        industry: "Freight & Logistics",
        size_range: "501-1,000",
        employee_count_raw: "780 employees",
        headquarters_text: "Chicago, Illinois",
        founded_raw: "1998",
        specialties: null,
        warnings: [],
      },
      missingSections: [],
      pageWarnings: [],
    },
    overrides
  );
}

module.exports = {
  createPanel,
  flush,
  DEFAULT_PREFS,
  fixtures: { record, batchView, profileDraftView, companyDraftView },
};

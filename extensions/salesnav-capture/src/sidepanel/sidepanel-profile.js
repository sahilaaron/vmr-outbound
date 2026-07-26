/**
 * Side-panel mode controller: which supported surface is in the active tab, the
 * person-profile capture workflow, and the company-evidence workflow.
 *
 * Contact-first: the primary action for a person is "Save Contact", or "Refresh
 * Contact" when the backend already knows that exact profile URL. No campaign is
 * selected, required, or stored anywhere in this panel.
 *
 * Exactly one mode's sections are visible at a time:
 *   Sales Navigator Listings · LinkedIn Person Profile · LinkedIn Company
 *   Profile · Unsupported Page · Challenge / Login Required
 *
 * All captured text is rendered with textContent (never innerHTML). Nothing is
 * transmitted without the operator pressing Save.
 */
(function () {
  "use strict";

  const { constants, handoff, permissions } = self.SNCapture;
  const { SURFACES, CAPTURE_STATUS } = constants;
  const panel = self.VMRPanel;
  const $ = (id) => document.getElementById(id);
  const el = panel.el;
  const send = panel.send;
  const setStatus = panel.setStatus;

  let currentDraft = null;
  let currentMode = null;

  // ---- mode switching ------------------------------------------------------

  const MODE_LABELS = {
    [SURFACES.SALESNAV_PEOPLE_RESULTS]: "Sales Navigator Listings",
    [SURFACES.PERSON_PROFILE]: "LinkedIn Person Profile",
    [SURFACES.COMPANY_PROFILE]: "LinkedIn Company Profile",
    [SURFACES.CHALLENGE]: "Challenge / Login Required",
    [SURFACES.UNAVAILABLE]: "Profile Unavailable",
    [SURFACES.UNSUPPORTED]: "Unsupported Page",
  };

  // The labels/note and Save cards belong to the two CONTACT workflows only.
  // Company evidence is not a person, so it keeps its own save button.
  const CONTACT_MODES = new Set([SURFACES.SALESNAV_PEOPLE_RESULTS, SURFACES.PERSON_PROFILE]);

  function showSections(mode) {
    currentMode = mode;
    $("salesnav-sections").hidden = mode !== SURFACES.SALESNAV_PEOPLE_RESULTS;
    $("profile-sections").hidden = mode !== SURFACES.PERSON_PROFILE;
    $("company-sections").hidden = mode !== SURFACES.COMPANY_PROFILE;
    $("metadata-card").hidden = !CONTACT_MODES.has(mode);
    $("save-card").hidden = !CONTACT_MODES.has(mode);
    $("unsupported-section").hidden =
      mode !== SURFACES.UNSUPPORTED && mode !== SURFACES.UNAVAILABLE;
    $("challenge-section").hidden = mode !== SURFACES.CHALLENGE;

    if (mode === SURFACES.SALESNAV_PEOPLE_RESULTS) panel.syncBatchSaveAction();
    else if (mode === SURFACES.PERSON_PROFILE) syncProfileSaveAction();
    else panel.setSaveHandler({ handler: null, reset: true });
  }

  async function refreshMode() {
    const statusEl = $("mode-status");
    const detailEl = $("mode-detail");
    const r = await send({ type: "DETECT_SURFACE" });
    const mode = (r && r.surface) || SURFACES.UNSUPPORTED;
    const label = MODE_LABELS[mode] || "Unsupported Page";
    const cls =
      mode === SURFACES.CHALLENGE || mode === SURFACES.UNAVAILABLE
        ? "status-err"
        : mode === SURFACES.UNSUPPORTED
          ? "status-warn"
          : "status-ok";
    setStatus(statusEl, cls, label);
    detailEl.textContent = (r && r.url) || "";
    showSections(mode);

    if (mode === SURFACES.SALESNAV_PEOPLE_RESULTS) panel.refreshDetect();

    if (mode === SURFACES.PERSON_PROFILE) {
      // DOM-level refinement (login wall / structure) + entry count badge.
      const d = await send({ type: "PROFILE_DETECT" });
      if (d && d.ok && d.page) {
        if (d.page.surface === SURFACES.CHALLENGE) {
          setStatus(statusEl, "status-err", MODE_LABELS[SURFACES.CHALLENGE]);
          showSections(SURFACES.CHALLENGE);
          return;
        }
        if (d.page.surface === SURFACES.UNAVAILABLE) {
          setStatus(statusEl, "status-err", MODE_LABELS[SURFACES.UNAVAILABLE]);
          showSections(SURFACES.UNAVAILABLE);
          return;
        }
        $("profile-exp-badge").textContent =
          d.page.experienceEntryCount != null
            ? `${d.page.experienceEntryCount} experience entr${d.page.experienceEntryCount === 1 ? "y" : "ies"} visible`
            : "";
      }
    }
  }

  // ---- review rendering ----------------------------------------------------

  function reviewRow(label, value) {
    return el("div", { class: "meta" }, [
      el("span", { class: "small muted", text: label + ": " }),
      el("span", { class: "small", text: value != null && value !== "" ? String(value) : "—" }),
    ]);
  }

  function renderDraft(draftView) {
    currentDraft = draftView;
    const box = $("profile-review");
    box.textContent = "";
    $("profile-exclude-exp-row").hidden = !draftView;
    if (!draftView) {
      box.appendChild(el("p", { class: "muted small", text: "No profile captured yet." }));
      syncProfileSaveAction();
      return;
    }
    const p = draftView.profile || {};
    const currentRole = draftView.currentRoles[0] || null;

    const head = el("div", { class: "record" }, [
      el("div", { class: "toprow" }, [el("span", { class: "name", text: p.full_name || "(no name)" })]),
      reviewRow("Headline", p.headline),
      reviewRow("Location", p.displayed_location),
      reviewRow(
        "Current role",
        currentRole ? [currentRole.job_title, currentRole.company_name].filter(Boolean).join(" @ ") : null
      ),
      reviewRow("Current company", currentRole ? currentRole.company_name : null),
      reviewRow("LinkedIn URL", p.linkedin_profile_url),
      reviewRow("Experience entries", draftView.experienceCount),
      reviewRow("Connections", p.connection_count),
      reviewRow("Open to work", p.open_to_work === true ? "yes" : p.open_to_work === false ? "no" : null),
      reviewRow("About", p.about_text ? truncate(p.about_text, 240) : null),
      reviewRow("Captured at", draftView.capturedAt),
      reviewRow("Capture status", draftView.status),
    ]);
    box.appendChild(head);

    if (draftView.missingSections.length) {
      box.appendChild(
        el("div", { class: "warns" }, draftView.missingSections.map((s) =>
          el("span", { class: "badge badge-warn", text: "missing: " + s })
        ))
      );
    }
    const warnCodes = new Set();
    for (const w of (p.warnings || []).concat(draftView.pageWarnings || [])) {
      if (w && w.code) warnCodes.add(w.code + (w.field ? ":" + w.field : ""));
    }
    for (const e of draftView.experiences) {
      for (const w of e.warnings || []) if (w && w.code) warnCodes.add(w.code + (w.field ? ":" + w.field : ""));
    }
    if (warnCodes.size) {
      box.appendChild(
        el("div", { class: "warns" }, Array.from(warnCodes).slice(0, 12).map((c) =>
          el("span", { class: "badge badge-warn", text: c })
        ))
      );
    }

    for (const e of draftView.experiences) {
      box.appendChild(
        el("div", { class: "record" + (draftView.excludedSections.includes("experience") ? " excluded" : "") }, [
          el("div", { class: "toprow" }, [
            el("span", { class: "name", text: `${e.position_index}. ${e.job_title || "(no title)"}` }),
          ]),
          reviewRow("Company", e.company_name),
          reviewRow("Timeline", [e.timeline_text, e.duration_text].filter(Boolean).join(" · ")),
          reviewRow("Type", e.employment_type),
          reviewRow("Role location", [e.role_location, e.workplace_type].filter(Boolean).join(" · ")),
          reviewRow("Current", e.is_current === true ? "yes" : e.is_current === false ? "no" : "—"),
        ])
      );
    }

    $("profile-exclude-exp").checked = draftView.excludedSections.includes("experience");
    syncProfileSaveAction();
  }

  function truncate(text, max) {
    const s = String(text);
    return s.length > max ? s.slice(0, max) + "…" : s;
  }

  /**
   * Label the shared Save button for the captured person. The backend is asked
   * only whether this exact profile URL already has a contact — existence, never
   * contact data — so the operator knows in advance whether they are creating a
   * record or refreshing one. If the backend cannot answer, the action stays
   * "Save Contact" rather than guessing.
   */
  async function syncProfileSaveAction() {
    if (currentMode !== SURFACES.PERSON_PROFILE) return;
    if (!currentDraft) {
      panel.setSaveHandler({ handler: null, label: "Save Contact", disabled: true });
      return;
    }
    panel.setSaveHandler({
      handler: () => send({ type: "SAVE_CONTACT" }),
      label: "Save Contact",
    });
    const match = await send({ type: "PROFILE_MATCH_STATE" });
    if (currentMode !== SURFACES.PERSON_PROFILE || !currentDraft) return;
    if (match && match.ok && match.match === "exact") {
      panel.setSaveHandler({ handler: () => send({ type: "SAVE_CONTACT" }), label: "Refresh Contact" });
    } else if (match && match.ok && match.match === "ambiguous") {
      panel.setSaveHandler({
        handler: () => send({ type: "SAVE_CONTACT" }),
        label: "Save Contact (identity ambiguous)",
      });
    }
  }

  // ---- actions -------------------------------------------------------------

  async function doCapture() {
    const fb = $("profile-capture-feedback");
    fb.textContent = "Reading the page…";
    $("profile-capture-btn").disabled = true;
    const r = await send({ type: "PROFILE_CAPTURE" });
    $("profile-capture-btn").disabled = false;
    if (!r || !r.ok) {
      fb.textContent = (r && (r.message || r.error)) || "Capture failed.";
      return;
    }
    const map = {
      [CAPTURE_STATUS.CHALLENGE_DETECTED]: "Login/security check detected — nothing captured.",
      [CAPTURE_STATUS.UNAVAILABLE_PROFILE]: "This profile is unavailable — nothing captured.",
      [CAPTURE_STATUS.UNSUPPORTED_PAGE]: "Not a supported main profile page — nothing captured.",
      [CAPTURE_STATUS.STRUCTURE_UNRECOGNIZED]:
        "Profile detected but the page structure was not recognized. Nothing captured.",
    };
    if (map[r.captureStatus]) {
      fb.textContent = map[r.captureStatus];
    } else {
      fb.textContent =
        r.captureStatus === CAPTURE_STATUS.PARTIAL
          ? "Captured with gaps — review the missing sections below."
          : "Captured. Review before saving.";
    }
    renderDraft(r.draftView);
    panel.setSaveHandler({ handler: () => send({ type: "SAVE_CONTACT" }), label: "Save Contact", reset: true });
    syncProfileSaveAction();
  }

  async function doClear() {
    if (!confirm("Clear the reviewed profile draft?")) return;
    const r = await send({ type: "PROFILE_CLEAR" });
    if (r && r.ok) {
      renderDraft(null);
      panel.setSaveHandler({ handler: null, label: "Save Contact", disabled: true, reset: true });
      $("profile-capture-feedback").textContent = "Draft cleared.";
    }
  }

  async function ensureHostPermission(url) {
    const pattern = permissions.originPatternForUrl(url);
    if (!pattern) return { granted: false, pattern: null };
    try {
      const has = await chrome.permissions.contains({ origins: [pattern] });
      if (has) return { granted: true, pattern };
      const granted = await chrome.permissions.request({ origins: [pattern] });
      return { granted, pattern };
    } catch (e) {
      return { granted: false, pattern, reason: String(e && e.message) };
    }
  }

  // ---- company mode: company evidence, never a contact ---------------------

  function renderCompanyDraft(draftView) {
    const box = $("company-review");
    box.textContent = "";
    $("company-send-btn").disabled = !draftView;
    if (!draftView) {
      box.appendChild(el("p", { class: "muted small", text: "No company captured yet." }));
      return;
    }
    const c = draftView.company || {};
    box.appendChild(
      el("div", { class: "record" }, [
        el("div", { class: "toprow" }, [el("span", { class: "name", text: c.name || "(no name)" })]),
        reviewRow("LinkedIn", c.company_linkedin_url),
        reviewRow("Website", c.website),
        reviewRow("Industry", c.industry),
        reviewRow("Size", c.size_range),
        reviewRow("Displayed employees", c.employee_count_raw),
        reviewRow("Headquarters (displayed)", c.headquarters_text),
        reviewRow("Founded", c.founded_raw),
        reviewRow("Specialties", c.specialties),
        reviewRow("Captured at", draftView.capturedAt),
        reviewRow("Capture status", draftView.status),
      ])
    );
    if (draftView.missingSections.length) {
      box.appendChild(
        el("div", { class: "warns" }, draftView.missingSections.map((s) =>
          el("span", { class: "badge badge-warn", text: "missing: " + s })
        ))
      );
    }
    const warnCodes = new Set();
    for (const w of (c.warnings || []).concat(draftView.pageWarnings || [])) {
      if (w && w.code) warnCodes.add(w.code + (w.field ? ":" + w.field : ""));
    }
    if (warnCodes.size) {
      box.appendChild(
        el("div", { class: "warns" }, Array.from(warnCodes).slice(0, 12).map((cd) =>
          el("span", { class: "badge badge-warn", text: cd })
        ))
      );
    }
  }

  async function doCompanyCapture() {
    const fb = $("company-capture-feedback");
    fb.textContent = "Reading the page…";
    $("company-capture-btn").disabled = true;
    const r = await send({ type: "COMPANY_CAPTURE" });
    $("company-capture-btn").disabled = false;
    if (!r || !r.ok) {
      fb.textContent = (r && (r.message || r.error)) || "Capture failed.";
      return;
    }
    const map = {
      [CAPTURE_STATUS.CHALLENGE_DETECTED]: "Login/security check detected — nothing captured.",
      [CAPTURE_STATUS.UNAVAILABLE_PROFILE]: "This company page is unavailable — nothing captured.",
      [CAPTURE_STATUS.UNSUPPORTED_PAGE]: "Not a supported company page — nothing captured.",
      [CAPTURE_STATUS.STRUCTURE_UNRECOGNIZED]:
        "Company page detected but its structure was not recognized. Nothing captured.",
    };
    fb.textContent =
      map[r.captureStatus] ||
      (r.captureStatus === CAPTURE_STATUS.PARTIAL
        ? "Captured with gaps — open the About page for full firmographics if needed."
        : "Captured. Review before saving.");
    renderCompanyDraft(r.draftView);
    $("company-send-state").textContent = "";
    $("company-send-actions").textContent = "";
  }

  async function doCompanyClear() {
    if (!confirm("Clear the reviewed company draft?")) return;
    const r = await send({ type: "COMPANY_CLEAR" });
    if (r && r.ok) {
      renderCompanyDraft(null);
      $("company-send-state").textContent = "";
      $("company-send-actions").textContent = "";
    }
  }

  async function doCompanySend() {
    const state = $("company-send-state");
    const actions = $("company-send-actions");
    actions.textContent = "";
    const perm = await ensureHostPermission(panel.backendBase() + constants.COMPANY_INTAKE_PATH);
    if (!perm.granted) {
      setStatus(state, "status-err", "Loopback access was not granted. Approve it to save.");
      actions.appendChild(el("button", { class: "btn btn-ghost", text: "Retry", on: { click: doCompanySend } }));
      return;
    }
    setStatus(state, "status-neutral", "Saving…");
    const r = await send({ type: "COMPANY_SEND" });
    if (r && r.ok) {
      renderCompanyStagedResult(r.result);
    } else {
      const detail = handoff.describeSendError(r);
      setStatus(state, "status-err", detail.headline);
      if (detail.canRetry !== false) {
        actions.appendChild(el("button", { class: "btn btn-ghost", text: "Retry", on: { click: doCompanySend } }));
      }
    }
  }

  function renderCompanyStagedResult(result) {
    if (!result) return;
    const state = $("company-send-state");
    const actions = $("company-send-actions");
    actions.textContent = "";
    const already = result.alreadyReceived ? " (already received — idempotent)" : "";
    setStatus(
      state,
      "status-ok",
      `Stored${already}: outcome ${result.outcome || "stored"}` +
        (result.snapshotId ? ` · id ${result.snapshotId}` : "")
    );
    if (result.workbenchUrl && handoff.isOpenableWorkbenchUrl(result.workbenchUrl)) {
      actions.appendChild(
        el("a", {
          class: "btn btn-primary",
          text: "Open company record",
          attrs: { href: result.workbenchUrl, target: "_blank", rel: "noreferrer" },
        })
      );
    }
  }

  // ---- wire up -------------------------------------------------------------

  async function init() {
    $("refresh-mode").addEventListener("click", refreshMode);
    $("profile-capture-btn").addEventListener("click", doCapture);
    $("profile-clear-btn").addEventListener("click", doClear);
    $("profile-exclude-exp").addEventListener("change", async () => {
      const r = await send({ type: "PROFILE_TOGGLE_SECTION", section: "experience" });
      if (r && r.ok) renderDraft(r.draftView);
    });

    $("company-capture-btn").addEventListener("click", doCompanyCapture);
    $("company-clear-btn").addEventListener("click", doCompanyClear);
    $("company-send-btn").addEventListener("click", doCompanySend);

    const state = await send({ type: "PROFILE_GET_STATE" });
    if (state && state.ok) {
      panel.setPrefs(state.prefs);
      if (state.metadata) panel.setMetadata(state.metadata);
      renderDraft(state.draftView);
      // Recovery: a saved outcome (and the reviewed draft that produced it)
      // survives panel close/reopen without recapture or resave.
      if (state.lastResult) panel.renderSaveResult(state.lastResult);
    }
    const companyState = await send({ type: "COMPANY_GET_STATE" });
    if (companyState && companyState.ok) {
      renderCompanyDraft(companyState.draftView);
      if (companyState.lastResult) renderCompanyStagedResult(companyState.lastResult);
    }
    refreshMode();
  }

  document.addEventListener("DOMContentLoaded", init);
})();

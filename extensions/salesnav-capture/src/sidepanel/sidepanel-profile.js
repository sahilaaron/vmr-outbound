/**
 * Side-panel controller for the dual-mode workflow (DAT-012C).
 *
 * Owns the mode banner (which supported surface is in the active tab) and the
 * person-profile capture mode. The existing SalesNav controller (sidepanel.js)
 * is untouched; this module only toggles section visibility so exactly one
 * mode's workflow is shown:
 *
 *   Sales Navigator Listings · LinkedIn Person Profile · LinkedIn Company
 *   Profile · Unsupported Page · Challenge / Login Required
 *
 * All scraped text is rendered with textContent (never innerHTML). Nothing is
 * transmitted without the operator pressing Send.
 */
(function () {
  "use strict";

  const { constants, handoff, permissions } = self.SNCapture;
  const { SURFACES, CAPTURE_STATUS } = constants;
  const $ = (id) => document.getElementById(id);

  let currentDraft = null;
  let profilePrefs = null;

  function send(message) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage(message, (resp) => {
        if (chrome.runtime.lastError) {
          resolve({ ok: false, error: "runtime_error", detail: chrome.runtime.lastError.message });
        } else {
          resolve(resp);
        }
      });
    });
  }

  function el(tag, opts, children) {
    const node = document.createElement(tag);
    if (opts) {
      if (opts.class) node.className = opts.class;
      if (opts.text != null) node.textContent = opts.text;
      if (opts.attrs) for (const [k, v] of Object.entries(opts.attrs)) node.setAttribute(k, v);
      if (opts.on) for (const [k, v] of Object.entries(opts.on)) node.addEventListener(k, v);
    }
    for (const c of children || []) if (c) node.appendChild(c);
    return node;
  }

  function setStatus(elm, cls, text) {
    elm.className = "status " + cls;
    elm.textContent = text;
  }

  // ---- mode switching ------------------------------------------------------

  const MODE_LABELS = {
    [SURFACES.SALESNAV_PEOPLE_RESULTS]: "Sales Navigator Listings",
    [SURFACES.PERSON_PROFILE]: "LinkedIn Person Profile",
    [SURFACES.COMPANY_PROFILE]: "LinkedIn Company Profile",
    [SURFACES.CHALLENGE]: "Challenge / Login Required",
    [SURFACES.UNAVAILABLE]: "Profile Unavailable",
    [SURFACES.UNSUPPORTED]: "Unsupported Page",
  };

  function showSections(mode) {
    $("salesnav-sections").hidden = mode !== SURFACES.SALESNAV_PEOPLE_RESULTS;
    $("profile-sections").hidden = mode !== SURFACES.PERSON_PROFILE;
    $("company-section").hidden = mode !== SURFACES.COMPANY_PROFILE;
    $("unsupported-section").hidden =
      mode !== SURFACES.UNSUPPORTED && mode !== SURFACES.UNAVAILABLE;
    $("challenge-section").hidden = mode !== SURFACES.CHALLENGE;
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
    $("profile-send-btn").disabled = !draftView;
    $("profile-exclude-exp-row").hidden = !draftView;
    if (!draftView) {
      box.appendChild(el("p", { class: "muted small", text: "No profile captured yet." }));
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
      reviewRow("Experience entries", draftView.experienceCount),
      reviewRow("Connections", p.connection_count),
      reviewRow("Open to work", p.open_to_work === true ? "yes" : p.open_to_work === false ? "no" : null),
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
          : "Captured. Review before sending.";
    }
    renderDraft(r.draftView);
    // A new capture invalidates any previously staged-result display.
    $("profile-send-state").textContent = "";
    $("profile-send-actions").textContent = "";
  }

  async function doClear() {
    if (!confirm("Clear the reviewed profile draft?")) return;
    const r = await send({ type: "PROFILE_CLEAR" });
    if (r && r.ok) {
      renderDraft(null);
      $("profile-send-state").textContent = "";
      $("profile-send-actions").textContent = "";
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

  function backendBase() {
    return String((profilePrefs || {}).backendBaseUrl || "").replace(/\/$/, "");
  }

  async function doSend() {
    const state = $("profile-send-state");
    const actions = $("profile-send-actions");
    actions.textContent = "";
    const perm = await ensureHostPermission(backendBase() + constants.PROFILE_INTAKE_PATH);
    if (!perm.granted) {
      setStatus(
        state,
        "status-err",
        perm.pattern
          ? `Loopback access to ${perm.pattern} was not granted. Approve it to send.`
          : "Backend URL must be a loopback (127.0.0.1 / localhost) URL."
      );
      actions.appendChild(el("button", { class: "btn btn-ghost", text: "Retry", on: { click: doSend } }));
      return;
    }
    setStatus(state, "status-neutral", "Sending…");
    const r = await send({ type: "PROFILE_SEND" });
    if (r && r.ok) {
      renderStagedResult(r.result);
    } else {
      const detail = handoff.describeSendError(r);
      setStatus(state, "status-err", detail.headline);
      if (detail.detail) state.appendChild(el("div", { class: "small muted", text: detail.detail }));
      // The reviewed draft is preserved: a retry re-sends the SAME
      // client_capture_id, which the backend treats idempotently.
      if (detail.canRetry !== false) {
        actions.appendChild(el("button", { class: "btn btn-ghost", text: "Retry", on: { click: doSend } }));
      }
    }
  }

  function renderStagedResult(result) {
    if (!result) return;
    const state = $("profile-send-state");
    const actions = $("profile-send-actions");
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
          text: "Open snapshot record",
          attrs: { href: result.workbenchUrl, target: "_blank", rel: "noreferrer" },
        })
      );
    }
  }

  // ---- campaigns (reuses the backend campaign endpoint) ---------------------

  async function fetchCampaigns() {
    const sel = $("profile-campaign-select");
    const perm = await ensureHostPermission(backendBase() + "/api/campaigns");
    if (!perm.granted) {
      sel.title = "Grant loopback access to fetch campaigns.";
      return;
    }
    const r = await send({ type: "FETCH_CAMPAIGNS" });
    if (!r || !r.ok) {
      sel.title = "Could not fetch campaigns (" + ((r && r.error) || "error") + ").";
      return;
    }
    sel.textContent = "";
    sel.appendChild(el("option", { text: "— none selected —", attrs: { value: "" } }));
    for (const c of r.campaigns) {
      sel.appendChild(el("option", { text: `${c.name} (${c.status})`, attrs: { value: c.id } }));
    }
    if (profilePrefs && profilePrefs.lastCampaignId) sel.value = profilePrefs.lastCampaignId;
  }

  // ---- wire up -------------------------------------------------------------

  async function init() {
    $("refresh-mode").addEventListener("click", refreshMode);
    $("profile-capture-btn").addEventListener("click", doCapture);
    $("profile-clear-btn").addEventListener("click", doClear);
    $("profile-send-btn").addEventListener("click", doSend);
    $("profile-fetch-campaigns").addEventListener("click", fetchCampaigns);
    $("profile-campaign-select").addEventListener("change", async (e) => {
      profilePrefs = (await send({ type: "SET_PREFS", prefs: { lastCampaignId: e.target.value || "" } })).prefs;
    });
    $("profile-exclude-exp").addEventListener("change", async () => {
      const r = await send({ type: "PROFILE_TOGGLE_SECTION", section: "experience" });
      if (r && r.ok) renderDraft(r.draftView);
    });

    const state = await send({ type: "PROFILE_GET_STATE" });
    if (state && state.ok) {
      profilePrefs = state.prefs;
      renderDraft(state.draftView);
      // Recovery: a staged result (and the reviewed draft that produced it)
      // survives panel close/reopen without recapture or resend.
      if (state.lastResult) renderStagedResult(state.lastResult);
    }
    refreshMode();
  }

  document.addEventListener("DOMContentLoaded", init);
})();

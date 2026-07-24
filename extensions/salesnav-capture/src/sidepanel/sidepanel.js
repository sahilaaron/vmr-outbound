/**
 * Side panel controller. Pure DOM rendering — every piece of scraped text is set
 * via textContent (never innerHTML) so captured values cannot inject markup.
 */
(function () {
  "use strict";

  const { constants, handoff } = self.SNCapture;
  const WARN = constants.WARNINGS;

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

  const $ = (id) => document.getElementById(id);
  let currentBatch = null;
  let currentPrefs = null;

  // ---- element helpers ----------------------------------------------------

  function el(tag, opts, children) {
    const node = document.createElement(tag);
    if (opts) {
      if (opts.class) node.className = opts.class;
      if (opts.text != null) node.textContent = opts.text;
      if (opts.title) node.title = opts.title;
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

  // ---- detection ----------------------------------------------------------

  async function refreshDetect() {
    const statusEl = $("detect-status");
    const detailEl = $("detect-detail");
    setStatus(statusEl, "status-neutral", "Checking active tab…");
    detailEl.textContent = "";
    const r = await send({ type: "DETECT_ACTIVE_PAGE" });
    if (!r || !r.ok) {
      setStatus(statusEl, "status-warn", "No Sales Navigator page in the active tab.");
      detailEl.textContent =
        (r && r.message) || "Open and authenticate a Sales Navigator search, then Refresh.";
      $("capture-btn").disabled = true;
      $("page-badge").textContent = "page ?";
      return;
    }
    const page = r.page;
    if (page.challengeDetected) {
      setStatus(statusEl, "status-err", "Security challenge detected — capture halted.");
      detailEl.textContent =
        "Resolve the LinkedIn check in the page yourself, then Refresh. The extension will not act during a challenge.";
      $("capture-btn").disabled = true;
    } else if (!page.supported) {
      setStatus(statusEl, "status-warn", "This page is not a supported Sales Navigator results view.");
      detailEl.textContent = page.url || "";
      $("capture-btn").disabled = true;
    } else {
      setStatus(statusEl, "status-ok", `Supported results page · ${page.visibleCount} rows currently visible`);
      detailEl.textContent = page.url || "";
      $("capture-btn").disabled = false;
    }
    const p = (function () {
      try { return new URL(page.url).searchParams.get("page"); } catch (_e) { return null; }
    })();
    $("page-badge").textContent = "page " + (p || "1");
  }

  // ---- capture ------------------------------------------------------------

  async function capture() {
    const fb = $("capture-feedback");
    fb.textContent = "Capturing…";
    $("capture-btn").disabled = true;
    const r = await send({ type: "CAPTURE_ACTIVE_PAGE" });
    $("capture-btn").disabled = false;
    if (!r || !r.ok) {
      fb.textContent = (r && (r.message || r.error)) || "Capture failed.";
      return;
    }
    if (r.captureStatus !== constants.CAPTURE_STATUS.OK) {
      const w = (r.pageWarnings && r.pageWarnings[0]) || {};
      const map = {
        challenge_detected: "Security challenge detected — nothing captured.",
        unsupported_page: "Not a supported results page — nothing captured.",
        structure_unrecognized:
          "Results page detected but no rows could be parsed. Page structure may have changed. Nothing captured.",
        empty: "No visible records found.",
      };
      fb.textContent = map[r.captureStatus] || w.message || "Nothing captured.";
    } else {
      const parts = [`+${r.added} added`];
      if (r.collapsed) parts.push(`${r.collapsed} duplicate(s) collapsed`);
      if (r.uncertain) parts.push(`${r.uncertain} uncertain identity`);
      if (r.overLimit) parts.push("batch limit reached — extra rows skipped");
      fb.textContent = parts.join(" · ");
    }
    renderBatch(r.batchView);
    refreshDetect();
  }

  // ---- batch rendering ----------------------------------------------------

  function renderBatch(batchView) {
    if (!batchView) return;
    currentBatch = batchView;
    const s = batchView.summary;

    const tiles = [
      ["included", s.included],
      ["excluded", s.excluded],
      ["missing fields", s.withMissingFields],
      ["uncertain id", s.uncertainIdentity],
      ["selector fails", s.selectorFailures],
      ["pages", (batchView.pagesCaptured || []).length],
    ];
    const grid = $("summary");
    grid.textContent = "";
    for (const [k, n] of tiles) {
      grid.appendChild(
        el("div", { class: "summary-tile" }, [
          el("span", { class: "n", text: String(n) }),
          el("span", { class: "k", text: k }),
        ])
      );
    }
    renderRecords();
  }

  function warnLabel(code) {
    const map = {
      [WARN.MISSING_FIELD]: "missing",
      [WARN.SELECTOR_FAILURE]: "selector fail",
      [WARN.DUPLICATE_UNCERTAIN]: "uncertain id",
      [WARN.DUPLICATE_COLLAPSED]: "dupe seen",
      [WARN.MALFORMED_URL]: "bad url",
      [WARN.NO_STABLE_IDENTITY]: "no stable id",
    };
    return map[code] || code;
  }

  function renderRecords() {
    const box = $("records");
    box.textContent = "";
    if (!currentBatch || !currentBatch.records.length) {
      box.appendChild(el("p", { class: "muted small", text: "No records captured yet." }));
      return;
    }
    const onlyIssues = $("only-issues").checked;
    currentBatch.records.forEach((rec, index) => {
      const warns = rec.warnings || [];
      if (onlyIssues && warns.length === 0) return;

      const nameRow = el("div", { class: "toprow" }, [
        el("span", { class: "name", text: rec.rawFullName || "(no name)" }),
        (function () {
          const cb = el("label", { class: "checkbox small" });
          const input = el("input", {
            attrs: { type: "checkbox" },
            on: {
              change: async () => {
                const view = await send({
                  type: "TOGGLE_EXCLUDE",
                  stableKey: rec._stableKey || null,
                  index,
                });
                if (view && view.ok) renderBatch(view.batchView);
              },
            },
          });
          input.checked = !!rec._excluded;
          cb.appendChild(input);
          cb.appendChild(document.createTextNode(" exclude"));
          return cb;
        })(),
      ]);

      const meta = el("div", { class: "meta" }, [
        el("div", { text: [rec.title, rec.companyName].filter(Boolean).join(" · ") || "—" }),
        rec.location ? el("div", { text: rec.location }) : null,
      ]);

      const links = el("div", { class: "links meta" });
      if (rec.linkedinProfileUrl) links.appendChild(el("a", { text: "profile", attrs: { href: rec.linkedinProfileUrl, target: "_blank", rel: "noreferrer" } }));
      if (rec.salesNavLeadUrl) links.appendChild(el("a", { text: "lead", attrs: { href: rec.salesNavLeadUrl, target: "_blank", rel: "noreferrer" } }));
      if (rec.companyLinkedInUrl) links.appendChild(el("a", { text: "company", attrs: { href: rec.companyLinkedInUrl, target: "_blank", rel: "noreferrer" } }));

      const warnBox = el("div", { class: "warns" });
      const uniqueCodes = Array.from(new Set(warns.map((w) => w.code)));
      for (const code of uniqueCodes) {
        const fields = warns.filter((w) => w.code === code && w.field).map((w) => w.field);
        const label = warnLabel(code) + (fields.length ? ": " + fields.join(", ") : "");
        warnBox.appendChild(el("span", { class: "badge badge-warn", text: label }));
      }

      const card = el("div", { class: "record" + (rec._excluded ? " excluded" : "") }, [
        nameRow,
        meta,
        links,
        warns.length ? warnBox : null,
      ]);
      box.appendChild(card);
    });
  }

  // ---- campaigns ----------------------------------------------------------

  async function fetchCampaigns() {
    const sel = $("campaign-select");
    // Requesting campaigns is also a backend call — request loopback access first.
    const base = String((currentPrefs || {}).backendBaseUrl || "").replace(/\/$/, "");
    const perm = await ensureHostPermission(base + "/api/campaigns");
    if (!perm.granted) {
      sel.title = "Grant loopback access to fetch campaigns, or enter an ID manually.";
      return;
    }
    const r = await send({ type: "FETCH_CAMPAIGNS" });
    if (!r || !r.ok) {
      sel.title = "Could not fetch campaigns (" + ((r && r.error) || "error") + "). Enter an ID manually.";
      return;
    }
    sel.textContent = "";
    sel.appendChild(el("option", { text: "— none selected —", attrs: { value: "" } }));
    for (const c of r.campaigns) {
      sel.appendChild(
        el("option", { text: `${c.name} (${c.status})`, attrs: { value: c.id } })
      );
    }
    if (currentPrefs && currentPrefs.lastCampaignId) sel.value = currentPrefs.lastCampaignId;
  }

  async function persistCampaign(id) {
    currentPrefs = (await send({ type: "SET_PREFS", prefs: { lastCampaignId: id || "" } })).prefs;
  }

  // ---- export / send ------------------------------------------------------

  /** Compute the loopback target URL for a given send target from saved prefs. */
  function targetUrl(target) {
    const p = currentPrefs || {};
    if (target === "mock") return p.mockReceiverUrl || "";
    return String(p.backendBaseUrl || "").replace(/\/$/, "") + constants.INTAKE_PATH;
  }

  /**
   * Ensure the OPTIONAL loopback host permission is granted before a backend/mock
   * call. Requests it (with the current click gesture) if not already held.
   */
  async function ensureHostPermission(url) {
    const pattern = self.SNCapture.permissions.originPatternForUrl(url);
    if (!pattern) return { granted: false, pattern: null, reason: "not_loopback" };
    try {
      const has = await chrome.permissions.contains({ origins: [pattern] });
      if (has) return { granted: true, pattern };
      const granted = await chrome.permissions.request({ origins: [pattern] });
      return { granted, pattern };
    } catch (e) {
      return { granted: false, pattern, reason: String(e && e.message) };
    }
  }

  async function doExport(format) {
    const state = $("send-state");
    const r = await send({ type: "EXPORT_BATCH", format });
    if (r && r.ok) setStatus(state, "status-ok", `Downloaded ${r.filename} (${r.records} records).`);
    else setStatus(state, "status-err", (r && (r.message || r.error)) || "Export failed.");
  }

  async function doSend() {
    const state = $("send-state");
    const actions = $("send-actions");
    actions.textContent = "";
    const target = $("send-target").value;
    // Request loopback access up front, within this click gesture.
    const perm = await ensureHostPermission(targetUrl(target));
    if (!perm.granted) {
      setStatus(
        state,
        "status-err",
        perm.pattern
          ? `Loopback access to ${perm.pattern} was not granted. Approve it to send.`
          : "Send target must be a loopback (127.0.0.1 / localhost) URL."
      );
      actions.appendChild(el("button", { class: "btn btn-ghost", text: "Retry", on: { click: doSend } }));
      return;
    }
    setStatus(state, "status-neutral", "Sending…");
    const r = await send({ type: "SEND_BATCH", target });
    if (r && r.ok) {
      // The worker persisted a safe result summary; render it (also restores on
      // reopen). The reviewed batch is intentionally NOT cleared here.
      const result = r.result || handoff.sanitizeStageResult(r.body || {}, {});
      renderStagedResult(result);
    } else {
      const detail = handoff.describeSendError(r);
      setStatus(state, "status-err", detail.headline);
      if (detail.detail) {
        state.appendChild(el("div", { class: "small muted", text: detail.detail }));
      }
      // The reviewed draft and its exclusions are preserved (send never mutates
      // the batch), so Retry re-sends the SAME client_batch_id — idempotent.
      if (detail.canRetry !== false) {
        actions.appendChild(el("button", { class: "btn btn-ghost", text: "Retry", on: { click: doSend } }));
      }
    }
  }

  /**
   * Render the successful-staging state and, distinctly, an Open in workbench
   * action. Staging and opening the workbench stay separate operator actions;
   * the workbench is never opened automatically. Used both right after a send
   * and to restore the state when the popup is reopened.
   */
  function renderStagedResult(result) {
    if (!result) return;
    const state = $("send-state");
    const actions = $("send-actions");
    actions.textContent = "";
    const already = result.alreadyReceived ? " (already received — idempotent)" : "";
    setStatus(
      state,
      "status-ok",
      `Staged${already}: ${result.recordCount != null ? result.recordCount + " records" : "ok"}` +
        (result.stagingId ? ` · id ${result.stagingId}` : "")
    );
    if (result.warningCount) {
      state.appendChild(el("div", { class: "small muted", text: `Backend warnings: ${result.warningCount}` }));
    }
    if (result.expiresAt) {
      state.appendChild(el("div", { class: "tiny muted", text: "Expires: " + result.expiresAt }));
    }
    // Only open a URL the backend returned if it is a known local workbench
    // destination; a non-loopback/unexpected URL is refused (never opened).
    if (result.workbenchUrl && handoff.isOpenableWorkbenchUrl(result.workbenchUrl)) {
      actions.appendChild(
        el("a", { class: "btn btn-primary", text: "Open staged batch in workbench", attrs: { href: result.workbenchUrl, target: "_blank", rel: "noreferrer" } })
      );
    } else {
      state.appendChild(
        el("div", { class: "tiny muted", text: "Open the batch from the workbench Imports list (no safe workbench link was returned)." })
      );
    }
  }

  // ---- settings -----------------------------------------------------------

  function loadPrefsIntoUi(prefs) {
    currentPrefs = prefs;
    $("backend-url").value = prefs.backendBaseUrl || "";
    $("mock-url").value = prefs.mockReceiverUrl || "";
    $("max-records").value = prefs.maxRecordsPerBatch || 500;
    $("send-target").value = prefs.sendTarget || "mock";
    if (prefs.lastCampaignId) $("campaign-manual").value = prefs.lastCampaignId;
  }

  async function saveSettings() {
    const patch = {
      backendBaseUrl: $("backend-url").value.trim(),
      mockReceiverUrl: $("mock-url").value.trim(),
      maxRecordsPerBatch: Math.max(1, Math.min(500, parseInt($("max-records").value, 10) || 500)),
      sendTarget: $("send-target").value,
    };
    const r = await send({ type: "SET_PREFS", prefs: patch });
    if (r && r.ok) {
      currentPrefs = r.prefs;
      setStatus($("send-state"), "status-ok", "Settings saved.");
    }
  }

  // ---- wire up ------------------------------------------------------------

  async function init() {
    $("refresh-detect").addEventListener("click", refreshDetect);
    $("capture-btn").addEventListener("click", capture);
    $("clear-btn").addEventListener("click", async () => {
      if (!confirm("Clear the entire draft batch? This cannot be undone.")) return;
      const r = await send({ type: "CLEAR_BATCH" });
      if (r && r.ok) renderBatch(r.batchView);
    });
    $("only-issues").addEventListener("change", renderRecords);
    $("fetch-campaigns").addEventListener("click", fetchCampaigns);
    $("campaign-select").addEventListener("change", (e) => {
      $("campaign-manual").value = e.target.value;
      persistCampaign(e.target.value);
    });
    $("campaign-manual").addEventListener("change", (e) => persistCampaign(e.target.value.trim()));
    $("send-target").addEventListener("change", (e) => send({ type: "SET_PREFS", prefs: { sendTarget: e.target.value } }));
    $("export-json").addEventListener("click", () => doExport("json"));
    $("export-csv").addEventListener("click", () => doExport("csv"));
    $("send-btn").addEventListener("click", doSend);
    $("save-settings").addEventListener("click", saveSettings);

    const state = await send({ type: "GET_STATE" });
    if (state && state.ok) {
      loadPrefsIntoUi(state.prefs);
      renderBatch(state.batchView);
      // Recovery: if a batch was already staged (popup was closed/reloaded, or
      // navigation failed), restore the staged state + Open in workbench action
      // without requiring a recapture or resend.
      if (state.lastResult) renderStagedResult(state.lastResult);
    }
    refreshDetect();
  }

  document.addEventListener("DOMContentLoaded", init);
})();

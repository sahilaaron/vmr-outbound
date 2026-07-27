/**
 * Side-panel core: shared helpers, the optional labels/note card, the shared
 * Save action, and the Sales Navigator results workflow.
 *
 * Contact-first: there is no campaign selector, no campaign id, and no campaign
 * state anywhere in this panel. The operator captures visible people, includes
 * or excludes them, optionally labels and annotates them, and saves them as
 * permanent contacts.
 *
 * Pure DOM rendering — every piece of captured text is set via textContent
 * (never innerHTML) so captured values cannot inject markup.
 */
(function () {
  "use strict";

  const { constants, contactSchema, handoff } = self.SNCapture;
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
  let currentMetadata = { labels: [], note: null };
  let saveHandler = null;

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

  /**
   * Ensure the OPTIONAL loopback host permission is granted before a backend
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

  function backendBase() {
    return String((currentPrefs || {}).backendBaseUrl || "").replace(/\/$/, "");
  }

  /** The loopback URL a save would target, for the permission prompt. */
  function saveTargetUrl() {
    const p = currentPrefs || {};
    if ((p.sendTarget || "mock") === "mock") return p.mockReceiverUrl || "";
    return backendBase() + constants.CONTACT_CAPTURE_PATH;
  }

  // ---- labels + note ------------------------------------------------------

  function renderMetadata() {
    const chips = $("label-chips");
    chips.textContent = "";
    for (const label of currentMetadata.labels) {
      chips.appendChild(
        el("span", { class: "badge" }, [
          el("span", { text: label }),
          el("button", {
            class: "btn btn-ghost",
            text: " ×",
            attrs: { "aria-label": `Remove label ${label}`, type: "button" },
            on: { click: () => removeLabel(label) },
          }),
        ])
      );
    }
    if (!currentMetadata.labels.length) {
      chips.appendChild(el("span", { class: "tiny muted", text: "No labels." }));
    }
    if ($("note-input").value !== (currentMetadata.note || "")) {
      $("note-input").value = currentMetadata.note || "";
    }
  }

  async function persistMetadata(patch) {
    const r = await send({ type: "SET_OPERATOR_METADATA", metadata: patch });
    if (r && r.ok) {
      currentMetadata = r.metadata;
      renderMetadata();
    }
    return currentMetadata;
  }

  async function addLabelFromInput() {
    const input = $("label-input");
    const raw = input.value;
    if (!raw.trim()) return;
    const next = contactSchema.sanitizeLabels([...currentMetadata.labels, raw]);
    input.value = "";
    await persistMetadata({ labels: next });
  }

  async function removeLabel(label) {
    const next = currentMetadata.labels.filter((l) => l !== label);
    await persistMetadata({ labels: next });
  }

  async function refreshLabelSuggestions() {
    const list = $("label-suggestions");
    const known = new Set((currentPrefs && currentPrefs.recentLabels) || []);
    const r = await send({ type: "FETCH_LABELS" });
    if (r && r.ok) for (const name of r.labels) known.add(name);
    list.textContent = "";
    for (const name of Array.from(known).sort()) {
      list.appendChild(el("option", { attrs: { value: name } }));
    }
  }

  // ---- shared save action -------------------------------------------------

  /**
   * Register which workflow the shared Save button drives. The label is the
   * operator's promise about what will happen: "Save Contact" for a new person,
   * "Refresh Contact" when the backend already knows this exact profile URL.
   */
  function setSaveHandler(options) {
    const opts = options || {};
    saveHandler = opts.handler || null;
    const btn = $("save-btn");
    btn.textContent = opts.label || "Save Contact";
    btn.disabled = !opts.handler || opts.disabled === true;
    $("export-row").hidden = opts.showExport !== true;
    if (opts.reset) {
      $("save-state").textContent = "";
      $("save-actions").textContent = "";
    }
  }

  const OUTCOME_LABELS = {
    created: "created",
    refreshed_exact_match: "refreshed",
    exact_match_unchanged: "already current",
    staged_unmatched: "staged (new person)",
    staged_ambiguous: "needs review (ambiguous)",
    duplicate_in_submission: "duplicate in this batch",
    suppressed: "suppressed — untouched",
  };

  function renderSaveResult(result) {
    if (!result) return;
    const state = $("save-state");
    const actions = $("save-actions");
    state.textContent = "";
    actions.textContent = "";
    const already = result.alreadyReceived ? " (already saved — idempotent)" : "";
    setStatus(state, "status-ok", `Saved${already}.`);

    const counts = result.counts || {};
    const grid = el("div", { class: "summary-grid" });
    let shown = 0;
    for (const [key, label] of Object.entries(OUTCOME_LABELS)) {
      const n = counts[key] || 0;
      if (!n) continue;
      shown += 1;
      grid.appendChild(
        el("div", { class: "summary-tile" }, [
          el("span", { class: "n", text: String(n) }),
          el("span", { class: "k", text: label }),
        ])
      );
    }
    if (counts.labels_applied) {
      grid.appendChild(
        el("div", { class: "summary-tile" }, [
          el("span", { class: "n", text: String(counts.labels_applied) }),
          el("span", { class: "k", text: "labels applied" }),
        ])
      );
    }
    if (counts.notes_recorded) {
      grid.appendChild(
        el("div", { class: "summary-tile" }, [
          el("span", { class: "n", text: String(counts.notes_recorded) }),
          el("span", { class: "k", text: "notes recorded" }),
        ])
      );
    }
    if (shown) state.appendChild(grid);

    const first = (result.results || [])[0] || {};
    if (first.contactUrl) {
      actions.appendChild(
        el("a", {
          class: "btn btn-primary",
          text: "Open contact",
          attrs: { href: first.contactUrl, target: "_blank", rel: "noreferrer" },
        })
      );
    }
    if (result.workbenchUrl) {
      actions.appendChild(
        el("a", {
          class: "btn",
          text: (result.results || []).length > 1 ? "Open saved contacts" : "Open capture record",
          attrs: { href: result.workbenchUrl, target: "_blank", rel: "noreferrer" },
        })
      );
    } else if (first.captureUrl) {
      actions.appendChild(
        el("a", {
          class: "btn",
          text: "Open capture record",
          attrs: { href: first.captureUrl, target: "_blank", rel: "noreferrer" },
        })
      );
    }
    if (!first.contactUrl && !result.workbenchUrl && !first.captureUrl) {
      state.appendChild(
        el("div", {
          class: "tiny muted",
          text: "Open the record from the workbench (no safe link was returned).",
        })
      );
    }
  }

  async function doSave() {
    if (!saveHandler) return;
    const state = $("save-state");
    const actions = $("save-actions");
    actions.textContent = "";
    const perm = await ensureHostPermission(saveTargetUrl());
    if (!perm.granted) {
      setStatus(
        state,
        "status-err",
        perm.pattern
          ? `Loopback access to ${perm.pattern} was not granted. Approve it to save.`
          : "Save target must be a loopback (127.0.0.1 / localhost) URL."
      );
      actions.appendChild(el("button", { class: "btn btn-ghost", text: "Retry", on: { click: doSave } }));
      return;
    }
    setStatus(state, "status-neutral", "Saving…");
    const r = await saveHandler();
    if (r && r.ok) {
      renderSaveResult(r.result);
      return;
    }
    const detail = handoff.describeSendError(r);
    setStatus(state, "status-err", detail.headline);
    if (detail.detail) state.appendChild(el("div", { class: "small muted", text: detail.detail }));
    // The reviewed draft is preserved on every recoverable failure, so Retry
    // re-sends the SAME client_submission_id — the backend replays it.
    if (detail.canRetry !== false) {
      actions.appendChild(el("button", { class: "btn btn-ghost", text: "Retry", on: { click: doSave } }));
    }
  }

  // ---- results-page detection ---------------------------------------------

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
        empty: "No visible contacts found.",
      };
      fb.textContent = map[r.captureStatus] || w.message || "Nothing captured.";
    } else {
      const parts = [`+${r.added} added`];
      if (r.collapsed) parts.push(`${r.collapsed} duplicate(s) collapsed`);
      if (r.uncertain) parts.push(`${r.uncertain} uncertain identity`);
      if (r.skippedCount) {
        parts.push(`${r.skippedCount} skipped — no company name`);
      }
      if (r.overLimit) parts.push("batch limit reached — extra rows skipped");
      fb.textContent = parts.join(" · ");
      renderSkipped(r.skipped, r.skippedCount);
    }
    renderBatch(r.batchView);
    refreshDetect();
  }

  /**
   * List the rows this page showed but did not offer, and why (DAT-018 B).
   * A skipped row is never added to the batch and can never be submitted; the
   * company is never inferred from headline, school, location or nearby text.
   */
  function renderSkipped(skipped, count) {
    const card = $("skipped-card");
    const list = $("skipped-list");
    if (!card || !list) return;
    const rows = skipped || [];
    if (!count) {
      card.hidden = true;
      list.textContent = "";
      return;
    }
    card.hidden = false;
    list.textContent = "";
    $("skipped-summary").textContent =
      `${count} visible row${count === 1 ? "" : "s"} skipped: no Company Name on the page. ` +
      "Nothing was guessed and nothing was submitted for them.";
    for (const row of rows) {
      list.appendChild(
        el("div", { class: "meta" }, [
          el("span", { class: "small muted", text: `row ${row.sourcePosition}: ` }),
          el("span", { class: "small", text: row.rawFullName || "(no name read)" }),
          el("span", { class: "badge badge-warn", text: row.reason }),
        ])
      );
    }
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
    syncBatchSaveAction();
  }

  /** Keep the shared Save button honest about how many contacts it would save. */
  function syncBatchSaveAction() {
    if ($("salesnav-sections").hidden) return;
    const included = currentBatch ? currentBatch.summary.included : 0;
    setSaveHandler({
      handler: () => send({ type: "SAVE_INCLUDED_CONTACTS" }),
      label: included === 1 ? "Save 1 included contact" : `Save ${included} included contacts`,
      disabled: included === 0,
      showExport: true,
    });
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
      box.appendChild(el("p", { class: "muted small", text: "No contacts captured yet." }));
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

  // ---- export -------------------------------------------------------------

  async function doExport(format) {
    const state = $("save-state");
    const r = await send({ type: "EXPORT_BATCH", format });
    if (r && r.ok) setStatus(state, "status-ok", `Downloaded ${r.filename} (${r.records} contacts).`);
    else setStatus(state, "status-err", (r && (r.message || r.error)) || "Export failed.");
  }

  // ---- migration notice ---------------------------------------------------

  /**
   * DAT-018 C. The old "Workflow updated" card mixed two things: a status
   * notice about the campaign-era retirement, and the ONLY route back to
   * archived drafts held in local storage.
   *
   * Code inspection (service-worker `exportLegacyArchive`, storage key
   * `cc_legacy_v1_archive`) shows the archive is genuinely recoverable state:
   * if this affordance disappears while an archive exists, those drafts become
   * unreachable. The notice text is not — it tells the operator nothing they
   * can act on.
   *
   * So the notice is gone and the card now appears ONLY while an archive
   * exists, named for the action it offers rather than for a workflow event.
   */
  function renderMigration(state) {
    const card = $("archive-card");
    const info = (state && state.migration) || {};
    if (!info.hasArchive) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    $("archive-message").textContent =
      "Drafts from the campaign-era workflow are still stored locally. They " +
      "cannot be submitted under the current contract — download them if you " +
      "still need them, then discard.";
  }

  // ---- settings -----------------------------------------------------------

  function loadPrefsIntoUi(prefs) {
    currentPrefs = prefs;
    $("backend-url").value = prefs.backendBaseUrl || "";
    $("mock-url").value = prefs.mockReceiverUrl || "";
    $("max-records").value = prefs.maxRecordsPerBatch || 500;
    $("send-target").value = prefs.sendTarget || "mock";
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
      setStatus($("save-state"), "status-ok", "Settings saved.");
    }
  }

  // ---- shared panel API ---------------------------------------------------
  //
  // sidepanel-profile.js owns mode switching and the profile/company workflows;
  // it drives the shared metadata and Save cards through this small surface so
  // there is exactly one implementation of each.
  self.VMRPanel = {
    send,
    el,
    setStatus,
    ensureHostPermission,
    backendBase,
    setSaveHandler,
    renderSaveResult,
    refreshLabelSuggestions,
    syncBatchSaveAction,
    getPrefs: () => currentPrefs,
    setPrefs: (p) => {
      currentPrefs = p;
    },
    setMetadata: (m) => {
      currentMetadata = m || { labels: [], note: null };
      renderMetadata();
    },
    refreshDetect,
  };

  // ---- wire up ------------------------------------------------------------

  async function init() {
    $("refresh-detect").addEventListener("click", refreshDetect);
    $("capture-btn").addEventListener("click", capture);
    $("clear-btn").addEventListener("click", async () => {
      if (!confirm("Clear the entire capture batch? This cannot be undone.")) return;
      const r = await send({ type: "CLEAR_BATCH" });
      if (r && r.ok) renderBatch(r.batchView);
    });
    $("only-issues").addEventListener("change", renderRecords);
    $("export-json").addEventListener("click", () => doExport("json"));
    $("export-csv").addEventListener("click", () => doExport("csv"));
    $("save-btn").addEventListener("click", doSave);
    $("save-settings").addEventListener("click", saveSettings);
    $("send-target").addEventListener("change", (e) =>
      send({ type: "SET_PREFS", prefs: { sendTarget: e.target.value } })
    );

    $("label-add").addEventListener("click", addLabelFromInput);
    $("label-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        addLabelFromInput();
      }
    });
    $("note-input").addEventListener("change", (e) => persistMetadata({ note: e.target.value }));
    $("metadata-clear").addEventListener("click", async () => {
      const r = await send({ type: "CLEAR_OPERATOR_METADATA" });
      if (r && r.ok) {
        currentMetadata = r.metadata;
        $("note-input").value = "";
        renderMetadata();
      }
    });
    $("migration-dismiss").addEventListener("click", async () => {
      await send({ type: "DISCARD_LEGACY_ARCHIVE" });
      $("archive-card").hidden = true;
    });
    $("migration-export").addEventListener("click", () =>
      send({ type: "EXPORT_LEGACY_ARCHIVE" })
    );

    const state = await send({ type: "GET_STATE" });
    if (state && state.ok) {
      loadPrefsIntoUi(state.prefs);
      currentMetadata = state.metadata || { labels: [], note: null };
      renderMetadata();
      renderMigration(state);
      renderBatch(state.batchView);
      // Recovery: if contacts were already saved (panel closed/reloaded, or a
      // navigation failed), restore the outcome without recapturing or resaving.
      if (state.lastResult) renderSaveResult(state.lastResult);
    }
    refreshLabelSuggestions();
    refreshDetect();
  }

  document.addEventListener("DOMContentLoaded", init);
})();

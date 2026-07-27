/**
 * Side-panel core: shared helpers, the optional labels/note card, the shared
 * Save action and outcome, and the Sales Navigator listings workflow.
 *
 * Contact-first: there is no campaign selector, no campaign id, and no campaign
 * state anywhere in this panel. The operator selects visible people, reviews the
 * selected set, optionally labels and annotates it, and saves them as permanent
 * contacts.
 *
 * Presentation is the VM Prospector shell (src/sidepanel/shell.js): header,
 * detected-page strip, three-step rail, one scrolling body, one sticky action.
 * Behaviour is unchanged — the same messages, the same warnings, the same draft
 * retention, the same retry semantics.
 *
 * Pure DOM rendering — every piece of captured text is set via textContent
 * (never innerHTML) so captured values cannot inject markup.
 */
(function () {
  "use strict";

  const { constants, contactSchema, handoff } = self.SNCapture;
  const WARN = constants.WARNINGS;
  const shell = self.VMRShell;
  const { el, badge, callout, kv, statusLine, box, paragraph } = shell;

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
  // The view the operator returns to when they leave an outcome or settings.
  let returnView = null;

  // ---- shared view plumbing ------------------------------------------------

  // Views that also show the optional labels/note card, and views that show
  // the outcome cards. Kept here so the two controllers agree.
  const METADATA_VIEWS = new Set(["listings-review", "person-confirm"]);
  // The two review/confirm views share one action group so the Save button has
  // exactly one implementation.
  const ACTIONS_FOR = {
    "listings-review": "save",
    "person-confirm": "save",
    // Every blocked page offers the same single way forward: look again.
    unsupported: "blocked",
    challenge: "blocked",
    unavailable: "blocked",
  };
  // Views the operator is *in the middle of* — page re-detection repaints the
  // detected-page strip underneath them but must not yank the body away.
  const STICKY_VIEWS = new Set(["outcome", "settings"]);

  /** Switch the body and the sticky action group together. */
  function showView(name) {
    shell.setView(name);
    const actionsKey = ACTIONS_FOR[name] || name;
    for (const node of document.querySelectorAll("[data-actions]")) {
      node.hidden = node.getAttribute("data-actions") !== actionsKey;
    }
    $("metadata-card").hidden = !METADATA_VIEWS.has(name);
    if (!STICKY_VIEWS.has(name)) returnView = name;
  }

  function isSticky() {
    return STICKY_VIEWS.has(shell.getView());
  }

  function setFeedback(elm, text, tone) {
    elm.textContent = text || "";
    if (tone) elm.setAttribute("data-tone", tone);
    else elm.removeAttribute("data-tone");
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

  /**
   * Reflect the connection state in the header dot. The panel never polls the
   * backend, so this only ever states what it actually knows.
   */
  function setConnection(state) {
    shell.setConnection(state);
    const line = $("settings-connection");
    if (line) line.textContent = $("conn-text").textContent;
  }

  async function refreshPermissionState() {
    const pattern = self.SNCapture.permissions.originPatternForUrl(saveTargetUrl());
    if (!pattern) return;
    if (shell.getConnection() === "connected" || shell.getConnection() === "unreachable") return;
    try {
      const has = await chrome.permissions.contains({ origins: [pattern] });
      setConnection(has ? "allowed" : "not_allowed");
    } catch (_e) {
      /* leave the state as it is rather than claiming something untrue */
    }
  }

  // ---- labels + note ------------------------------------------------------

  function renderMetadata() {
    const chips = $("label-chips");
    chips.textContent = "";
    for (const label of currentMetadata.labels) {
      chips.appendChild(
        el("span", { class: "chip" }, [
          el("span", { class: "txt", text: label }),
          el("button", {
            text: "×",
            attrs: { "aria-label": `Remove label ${label}`, type: "button" },
            on: { click: () => removeLabel(label) },
          }),
        ])
      );
    }
    if (!currentMetadata.labels.length) {
      chips.appendChild(el("span", { class: "p tiny muted", text: "No labels." }));
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

  // How each backend outcome reads to the operator, and the tone it carries.
  // Every outcome the contract can return has an entry: an unrecognised one
  // still renders, under its raw code, rather than being swallowed.
  const OUTCOMES = {
    created: { label: "captured as a new contact", tone: "success", short: "New" },
    refreshed_exact_match: { label: "refreshed an existing contact", tone: "brand", short: "Updated" },
    exact_match_unchanged: { label: "already current", tone: "brand", short: "Unchanged" },
    staged_unmatched: { label: "staged as a new person", tone: "warning", short: "Needs review" },
    staged_ambiguous: { label: "staged — identity ambiguous", tone: "warning", short: "Needs review" },
    duplicate_in_submission: { label: "duplicate within this batch", tone: "neutral", short: "Duplicate" },
    suppressed: { label: "suppressed — left untouched", tone: "danger", short: "Suppressed" },
  };

  function outcomeInfo(code) {
    return (
      OUTCOMES[code] || {
        label: String(code || "unknown outcome").replace(/_/g, " "),
        tone: "neutral",
        short: String(code || "outcome"),
      }
    );
  }

  /**
   * Paint a successful submission. Counts and per-record outcomes come from the
   * backend response only — nothing is inferred, and a record that needs review
   * stays visible as needing review.
   */
  function renderSaveResult(result) {
    if (!result) return;
    setConnection("connected");
    const state = $("save-state");
    const actions = $("save-actions");
    state.textContent = "";
    actions.textContent = "";
    $("saving-card").hidden = true;
    $("save-card").hidden = false;
    $("company-result-card").hidden = true;

    const counts = result.counts || {};
    const results = result.results || [];
    const total = results.length;
    const saved = Object.entries(counts)
      .filter(([key]) => key in OUTCOMES)
      .reduce((sum, [, n]) => sum + (Number(n) || 0), 0);
    const headline =
      total > 1
        ? `${saved || total} of ${total} prospects saved`
        : result.alreadyReceived
          ? "Already saved (idempotent)"
          : "Prospect saved";

    state.appendChild(
      callout(
        "success",
        headline,
        result.alreadyReceived
          ? "This submission had already been received — it was replayed, not duplicated."
          : "Saved to the VM Prospector workflow."
      )
    );

    const lines = box({ sunk: true }, [el("span", { class: "eyebrow", text: "What happened" })]);
    let shown = 0;
    for (const [code, n] of Object.entries(counts)) {
      if (!n || !(code in OUTCOMES)) continue;
      shown += 1;
      const info = outcomeInfo(code);
      const line = el("div", { class: "line" }, [
        el("span", { class: "t" }, [
          el("b", { text: String(n) }),
          el("span", { text: " " + info.label }),
        ]),
        badge(info.short, { tone: info.tone }),
      ]);
      lines.appendChild(line);
    }
    if (counts.labels_applied) {
      lines.appendChild(
        statusLine("Labels applied", badge(String(counts.labels_applied), { tone: "brand" }))
      );
      shown += 1;
    }
    if (counts.notes_recorded) {
      lines.appendChild(
        statusLine("Notes recorded", badge(String(counts.notes_recorded), { tone: "brand" }))
      );
      shown += 1;
    }
    if (shown) state.appendChild(lines);

    // Records the backend could not resolve on its own stay called out by name
    // count, so a partial success never reads as a clean one.
    const needsReview = results.filter(
      (r) => r.outcome === "staged_unmatched" || r.outcome === "staged_ambiguous"
    ).length;
    if (needsReview) {
      state.appendChild(
        box({ tone: "warning" }, [
          el("span", { class: "box-title", text: `${needsReview} need review in VM Prospector` }),
          paragraph(
            "These were saved with their gaps visible. Identity was not guessed — resolve them in the app.",
            { tiny: true }
          ),
        ])
      );
    }

    const first = results[0] || {};
    if (first.contactUrl) {
      actions.appendChild(
        el("a", {
          class: "btn btn-primary full",
          text: "Open contact",
          attrs: { href: first.contactUrl, target: "_blank", rel: "noreferrer" },
        })
      );
    }
    if (result.workbenchUrl) {
      actions.appendChild(
        el("a", {
          class: "btn full",
          text: total > 1 ? "Open captured contacts" : "Open capture record",
          attrs: { href: result.workbenchUrl, target: "_blank", rel: "noreferrer" },
        })
      );
    } else if (first.captureUrl) {
      actions.appendChild(
        el("a", {
          class: "btn full",
          text: "Open capture record",
          attrs: { href: first.captureUrl, target: "_blank", rel: "noreferrer" },
        })
      );
    }
    if (!first.contactUrl && !result.workbenchUrl && !first.captureUrl) {
      state.appendChild(
        paragraph("Open the record from the workbench (no safe link was returned).", {
          tiny: true,
          muted: true,
        })
      );
    }

    $("outcome-primary").hidden = true;
    $("outcome-back").textContent = "Back to this page";
    showView("outcome");
    shell.setSteps(3, { done: true, label: "Done" });
  }

  /** Paint a failed submission: what failed, what survived, one way forward. */
  function renderSaveFailure(detail, options) {
    const o = options || {};
    const state = $("save-state");
    const actions = $("save-actions");
    state.textContent = "";
    actions.textContent = "";
    $("saving-card").hidden = true;
    $("save-card").hidden = false;
    $("company-result-card").hidden = true;

    state.appendChild(
      callout(
        o.tone || "danger",
        o.title || "Capture failed",
        o.body || "Nothing was saved. What you reviewed is still here."
      )
    );
    const info = box({ sunk: true }, [
      el("span", { class: "eyebrow", text: "Details" }),
      paragraph(detail.headline),
    ]);
    if (detail.detail) info.appendChild(paragraph(detail.detail, { tiny: true, muted: true }));
    info.appendChild(
      el("p", { class: "detail-block", text: "code: " + (detail.code || "unknown") })
    );
    state.appendChild(info);

    if (detail.canRetry !== false) {
      const btn = $("outcome-primary");
      btn.hidden = false;
      btn.textContent = o.retryLabel || "Try again";
      btn.onclick = o.onRetry || doSave;
      state.appendChild(
        paragraph("Retrying is safe — the same submission is replayed, never duplicated.", {
          tiny: true,
          muted: true,
        })
      );
    } else {
      $("outcome-primary").hidden = true;
    }
    $("export-row").hidden = o.showExport !== true;
    $("outcome-back").textContent = "Back to review";
    showView("outcome");
    shell.setSteps(3, { state: "failed", label: "Failed" });
  }

  async function doSave() {
    if (!saveHandler) return;
    const isBatch = currentBatch && shell.getView() !== "person-confirm";
    $("save-card").hidden = true;
    $("company-result-card").hidden = true;
    $("saving-card").hidden = false;
    const included = currentBatch ? currentBatch.summary.included : 1;
    $("saving-title").textContent = isBatch
      ? included === 1
        ? "Saving 1 prospect"
        : `Saving ${included} prospects`
      : "Saving prospect";
    showView("outcome");
    shell.setSteps(3, {});
    setConnection("saving");
    const savingActions = document.querySelector('[data-actions="saving"]');
    for (const node of document.querySelectorAll("[data-actions]")) node.hidden = true;
    if (savingActions) savingActions.hidden = false;

    const perm = await ensureHostPermission(saveTargetUrl());
    if (!perm.granted) {
      setConnection("not_allowed");
      renderSaveFailure(
        {
          code: "permission_denied",
          headline: perm.pattern
            ? `Loopback access to ${perm.pattern} was not granted.`
            : "Save target must be a loopback (127.0.0.1 / localhost) URL.",
          detail: "Nothing has been sent.",
          canRetry: !!perm.pattern,
        },
        {
          tone: "warning",
          title: "Allow VM Prospector to reach the app",
          body: "Chrome asks once, the first time you save. Nothing has been sent.",
          retryLabel: "Allow and save",
        }
      );
      return;
    }

    const r = await saveHandler();
    if (r && r.ok) {
      renderSaveResult(r.result);
      return;
    }
    const detail = handoff.describeSendError(r);
    const unreachable = detail.code === "network_error" || detail.code === "timeout";
    setConnection(unreachable ? "unreachable" : "connected");
    renderSaveFailure(detail, {
      title: unreachable ? "Connection lost" : "Capture failed",
      body: unreachable
        ? "VM Prospector didn't answer. Nothing was saved, and what you reviewed is still here."
        : "Nothing was saved. What you reviewed is still here.",
      showExport: !!isBatch,
    });
  }

  // ---- results-page detection ---------------------------------------------

  async function refreshDetect() {
    const statusEl = $("detect-status");
    const detailEl = $("detect-detail");
    setFeedback(statusEl, "Checking active tab…");
    detailEl.textContent = "";
    detailEl.hidden = true;
    const r = await send({ type: "DETECT_ACTIVE_PAGE" });
    if (!r || !r.ok) {
      setFeedback(
        statusEl,
        (r && r.message) || "Open and authenticate a Sales Navigator search, then check again.",
        "warn"
      );
      $("capture-btn").disabled = true;
      return;
    }
    const page = r.page;
    if (page.challengeDetected) {
      setFeedback(
        statusEl,
        "Security challenge detected — capture halted. Resolve the LinkedIn check in the page yourself, then check again.",
        "bad"
      );
      $("capture-btn").disabled = true;
    } else if (!page.supported) {
      setFeedback(statusEl, "This page is not a supported Sales Navigator results view.", "warn");
      $("capture-btn").disabled = true;
    } else {
      const rows = page.visibleCount;
      setFeedback(
        statusEl,
        currentBatch && currentBatch.records.length
          ? `${rows} row${rows === 1 ? "" : "s"} currently visible on this page.`
          : `${rows} row${rows === 1 ? "" : "s"} currently visible. Read the page to list them.`
      );
      $("capture-btn").disabled = false;
    }
    detailEl.textContent = page.url || "";
    detailEl.hidden = !page.url;
    paintListingsContext(page);
  }

  /** The detected-page strip for listings mode. */
  function paintListingsContext(page) {
    const count = currentBatch ? currentBatch.records.length : 0;
    const included = currentBatch ? currentBatch.summary.included : 0;
    let pageBadge = null;
    if (count) {
      pageBadge =
        shell.getView() === "listings-review"
          ? { text: `${included} selected`, tone: "neutral" }
          : { text: `${count} found`, tone: count ? "neutral" : "warning" };
    } else if (page && page.supported) {
      const p = (function () {
        try {
          return new URL(page.url).searchParams.get("page");
        } catch (_e) {
          return null;
        }
      })();
      pageBadge = { text: "page " + (p || "1"), tone: "neutral", dot: false };
    }
    shell.setContext({
      icon: "users",
      label: "Sales Navigator · Search results",
      badge: pageBadge,
      url: page && page.url ? page.url : "",
    });
  }

  // ---- capture ------------------------------------------------------------

  // The read pass currently in flight, if any: {seq, passId, cancelled}. One at
  // a time — the capture button is disabled while a pass runs — but the seq
  // still guards the result, because a pass that was superseded must not repaint
  // over whatever the operator is looking at now.
  let activeCapture = null;
  let captureSeq = 0;

  /** Show or hide the read-pass progress card and its Stop control together. */
  function setCaptureProgress(active, options) {
    const o = options || {};
    $("capture-progress").hidden = !active;
    const cancelBtn = $("capture-cancel-btn");
    cancelBtn.hidden = !active;
    cancelBtn.disabled = !active || o.stopping === true;
    if (active) {
      $("capture-progress-title").textContent = o.title || "Reading this page";
      if (o.detail) $("capture-progress-detail").textContent = o.detail;
    }
    $("loading-note").hidden = !!active;
  }

  /**
   * Stop the running read pass (DAT-018 D).
   *
   * This is an operator action, not a failure: the capture still resolves, the
   * rows already on the page are still returned and kept, the batch and the
   * reviewed draft survive, and nothing is submitted. Pressing it again while
   * the stop is being honoured does nothing — one cancel per pass.
   */
  async function cancelCapture() {
    if (!activeCapture || activeCapture.cancelled) return;
    activeCapture.cancelled = true;
    setCaptureProgress(true, {
      title: "Stopping…",
      detail: "Keeping everything that has already loaded.",
      stopping: true,
    });
    await send({ type: "CANCEL_CAPTURE" });
  }

  /**
   * Progress from the content script's scroll pass. Advisory only: it is
   * discarded once the operator has cancelled, and once the pass it belongs to
   * is no longer the one in flight, so a late event cannot restart the progress
   * card or overwrite the cancelled state.
   */
  function onScrollProgress(message) {
    if (!activeCapture || activeCapture.cancelled) return;
    if (activeCapture.passId == null) activeCapture.passId = message.passId || null;
    else if (message.passId != null && message.passId !== activeCapture.passId) return;
    const p = message.progress || {};
    if (p.phase === "done") return;
    const rows = Number(p.rows);
    if (!Number.isFinite(rows)) return;
    $("capture-progress-detail").textContent =
      `${rows} row${rows === 1 ? "" : "s"} loaded so far. Stopping keeps every one of them.`;
  }

  async function capture() {
    const fb = $("capture-feedback");
    const seq = ++captureSeq;
    activeCapture = { seq, passId: null, cancelled: false };
    setFeedback(fb, "Reading this page…");
    $("capture-btn").disabled = true;
    $("listings-retry-btn").disabled = true;
    showView("loading");
    setCaptureProgress(true, {
      detail:
        "Loading the rows this page has already been scrolled to. You can stop at any point — whatever has loaded is kept.",
    });

    const r = await send({ type: "CAPTURE_ACTIVE_PAGE" });

    // A newer pass took over while this one was in flight; it owns the view.
    if (!activeCapture || activeCapture.seq !== seq) return;
    const wasCancelled =
      activeCapture.cancelled || (r && r.scroll && r.scroll.stopReason === "cancelled");
    activeCapture = null;
    setCaptureProgress(false);
    $("capture-btn").disabled = false;
    $("listings-retry-btn").disabled = false;
    if (!r || !r.ok) {
      renderBatch(r && r.batchView ? r.batchView : currentBatch);
      showView(currentBatch && currentBatch.records.length ? "listings-select" : "listings-empty");
      setFeedback(fb, (r && (r.message || r.error)) || "Capture failed.", "bad");
      return;
    }
    if (r.captureStatus !== constants.CAPTURE_STATUS.OK) {
      const w = (r.pageWarnings && r.pageWarnings[0]) || {};
      const map = {
        challenge_detected: "Security challenge detected — nothing captured.",
        unsupported_page: "Not a supported results page — nothing captured.",
        structure_unrecognized:
          "Results page detected but no rows could be parsed. The page structure may have changed. Nothing captured.",
        empty: "No visible prospects found.",
      };
      // A pass the operator stopped before any row loaded is a cancellation,
      // not a page with nothing on it. Saying "no prospects found" there would
      // blame the page for the operator's own action.
      const message = wasCancelled
        ? "Stopped before any rows loaded. Nothing was captured, and nothing was sent."
        : map[r.captureStatus] || w.message || "Nothing captured.";
      renderSkipped(r.skipped, r.skippedCount);
      renderBatch(r.batchView);
      if (!currentBatch || !currentBatch.records.length) {
        $("listings-empty-detail").textContent = message;
        showView("listings-empty");
      } else {
        setFeedback(fb, message, wasCancelled ? null : "warn");
        showView("listings-select");
      }
      refreshDetect();
      return;
    }
    const parts = [];
    if (wasCancelled) parts.push("Stopped");
    parts.push(`+${r.added} added`);
    if (r.collapsed) parts.push(`${r.collapsed} duplicate(s) collapsed`);
    if (r.uncertain) parts.push(`${r.uncertain} uncertain identity`);
    if (r.skippedCount) {
      parts.push(`${r.skippedCount} skipped — no company name`);
    }
    if (r.overLimit) parts.push("batch limit reached — extra rows skipped");
    if (wasCancelled) parts.push("read the page again to load more");
    setFeedback(fb, parts.join(" · "));
    renderSkipped(r.skipped, r.skippedCount);
    renderBatch(r.batchView);
    showView("listings-select");
    refreshDetect();
  }

  /**
   * List the rows this page showed but did not offer, and why (DAT-018 B).
   * A skipped row is never added to the batch and can never be submitted; the
   * company is never inferred from headline, school, location or nearby text.
   *
   * Presented as a warning box rather than hidden behind the row list: a
   * skipped row is information the operator needs to trust the count, so it is
   * never collapsed away and never rolled into the "captured" total.
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
        el("div", { class: "line" }, [
          el("span", { class: "t" }, [
            el("span", { class: "mono", text: `row ${row.sourcePosition} · ` }),
            el("span", { text: row.rawFullName || "(no name read)" }),
          ]),
          badge(row.reason, { tone: "warning" }),
        ])
      );
    }
  }

  // ---- batch rendering ----------------------------------------------------

  function renderBatch(batchView) {
    if (!batchView) return;
    currentBatch = batchView;
    renderRecords();
    renderReview();
    syncBatchSaveAction();
  }

  /** Keep the shared Save button honest about how many contacts it would save. */
  function syncBatchSaveAction() {
    const included = currentBatch ? currentBatch.summary.included : 0;
    const reviewBtn = $("listings-review-btn");
    const hasRows = !!(currentBatch && currentBatch.records.length);
    reviewBtn.hidden = !hasRows;
    reviewBtn.disabled = included === 0;
    reviewBtn.textContent = `Review selected (${included})`;
    $("clear-btn").hidden = !hasRows;
    const captureBtn = $("capture-btn");
    captureBtn.textContent = hasRows ? "Read this page again" : "Capture visible contacts";
    captureBtn.className = hasRows ? "btn full" : "btn btn-primary full";
    if (shell.getView() === "listings-review" || !currentBatch) {
      setSaveHandler({
        handler: () => send({ type: "SAVE_INCLUDED_CONTACTS" }),
        label: included === 1 ? "Capture 1 prospect" : `Capture ${included} prospects`,
        disabled: included === 0,
        showExport: true,
      });
    }
  }

  function warnLabel(code) {
    const map = {
      [WARN.MISSING_FIELD]: "missing",
      [WARN.SELECTOR_FAILURE]: "could not be located",
      [WARN.DUPLICATE_UNCERTAIN]: "uncertain identity",
      [WARN.DUPLICATE_COLLAPSED]: "duplicate collapsed",
      [WARN.MALFORMED_URL]: "profile URL not normalized",
      [WARN.NO_STABLE_IDENTITY]: "no stable link",
    };
    return map[code] || code;
  }

  function recordWarningCodes(rec) {
    return Array.from(new Set((rec.warnings || []).map((w) => w.code)));
  }

  function recordTone(rec) {
    const codes = recordWarningCodes(rec);
    if (codes.includes(WARN.NO_STABLE_IDENTITY)) return "danger";
    if (codes.length) return "warning";
    return null;
  }

  /** A1 · the selectable row list. A ticked box means "included in the save". */
  function renderRecords() {
    const boxEl = $("records");
    boxEl.textContent = "";
    const hasRows = !!(currentBatch && currentBatch.records.length);
    $("selectall-row").hidden = !hasRows;
    $("filter-row").hidden = !hasRows;
    if (!hasRows) {
      boxEl.appendChild(
        paragraph("No prospects captured from this page yet.", { muted: true })
      );
      return;
    }

    const included = currentBatch.summary.included;
    const total = currentBatch.records.length;
    $("select-all").checked = included === total;
    $("select-all").indeterminate = included > 0 && included < total;
    $("select-all-label").textContent = `Select all (${total})`;
    const flagged = currentBatch.records.filter((r) => (r.warnings || []).length).length;
    $("select-all-aside").textContent = flagged ? `${flagged} need review` : "";

    const onlyIssues = $("only-issues").checked;
    currentBatch.records.forEach((rec, index) => {
      const warns = rec.warnings || [];
      if (onlyIssues && warns.length === 0) return;

      const tone = recordTone(rec);
      const checkbox = el("input", {
        attrs: {
          type: "checkbox",
          "aria-label": `Include ${rec.rawFullName || "this prospect"} in the capture`,
        },
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
      checkbox.checked = !rec._excluded;

      const meta = [rec.title, rec.companyName].filter(Boolean).join(" · ");
      const metaNode = el("span", { class: "prospect-meta" });
      if (meta) metaNode.appendChild(el("span", { text: meta }));
      if (!rec.companyName) {
        if (meta) metaNode.appendChild(el("span", { text: " · " }));
        metaNode.appendChild(el("span", { class: "missing", text: "company not shown" }));
      }
      if (!meta && rec.companyName) metaNode.appendChild(el("span", { text: rec.companyName }));

      const body = el("div", { class: "prospect-body" }, [
        el("span", { class: "prospect-name", text: rec.rawFullName || "(name not shown)" }),
        metaNode,
        rec.location ? el("span", { class: "prospect-meta", text: rec.location }) : null,
      ]);

      const codes = recordWarningCodes(rec);
      let rowBadge = null;
      if (codes.includes(WARN.NO_STABLE_IDENTITY)) {
        rowBadge = badge("No stable link", {
          tone: "danger",
          title: "No profile or lead URL was on the row — it can be saved, but it will be staged for review.",
        });
      } else if (codes.length) {
        rowBadge = badge("Needs review", {
          tone: "warning",
          title: codes.map(warnLabel).join(", "),
        });
      }

      const row = el(
        "div",
        {
          class: "prospect" + (rec._excluded ? " deselected" : ""),
          attrs: tone ? { "data-tone": tone } : {},
        },
        [el("label", { class: "check stacked" }, [checkbox]), body, rowBadge]
      );
      boxEl.appendChild(row);
    });
  }

  /** A3 · the reviewed set: what will be submitted, and with what gaps. */
  function renderReview() {
    const badges = $("review-badges");
    const grid = $("summary");
    const list = $("review-records");
    badges.textContent = "";
    grid.textContent = "";
    list.textContent = "";
    if (!currentBatch) return;
    const s = currentBatch.summary;
    const records = currentBatch.records.filter((r) => !r._excluded);

    const ready = records.filter((r) => !(r.warnings || []).length).length;
    const review = records.length - ready;
    if (ready) badges.appendChild(badge(`${ready} ready`, { tone: "success", dot: true }));
    if (review)
      badges.appendChild(badge(`${review} need review`, { tone: "warning", dot: true }));

    const tiles = [
      ["selected", s.included],
      ["deselected", s.excluded],
      ["missing fields", s.withMissingFields],
      ["uncertain id", s.uncertainIdentity],
      ["selector fails", s.selectorFailures],
      ["pages", (currentBatch.pagesCaptured || []).length],
    ];
    for (const [k, n] of tiles) {
      grid.appendChild(
        el("div", { class: "tile" }, [
          el("span", { class: "n", text: String(n) }),
          el("span", { class: "k", text: k }),
        ])
      );
    }

    for (const rec of records) {
      const codes = recordWarningCodes(rec);
      const tone = recordTone(rec);
      const card = box(tone ? { tone } : {}, [
        el("div", { class: "line" }, [
          el("span", { class: "t prospect-name", text: rec.rawFullName || "(name not shown)" }),
          codes.length
            ? badge(codes.includes(WARN.NO_STABLE_IDENTITY) ? "No stable link" : "Needs review", {
                tone: tone === "danger" ? "danger" : "warning",
              })
            : badge("Ready", { tone: "success" }),
        ]),
        kv("Company", rec.companyName, { missing: !rec.companyName, emptyText: "Missing company" }),
        rec.title ? kv("Role", rec.title) : null,
      ]);
      if (codes.length) {
        const warnRow = el("div", { class: "badge-row" });
        for (const code of codes) {
          const fields = (rec.warnings || [])
            .filter((w) => w.code === code && w.field)
            .map((w) => w.field);
          warnRow.appendChild(
            badge(warnLabel(code) + (fields.length ? ": " + fields.join(", ") : ""), {
              tone: "warning",
              title: code,
            })
          );
        }
        card.appendChild(warnRow);
        card.appendChild(
          paragraph("Will be saved and flagged for review. Nothing is guessed.", { tiny: true })
        );
      }
      const links = el("div", { class: "prospect-links" });
      if (rec.linkedinProfileUrl)
        links.appendChild(
          el("a", {
            text: "profile",
            attrs: { href: rec.linkedinProfileUrl, target: "_blank", rel: "noreferrer" },
          })
        );
      if (rec.salesNavLeadUrl)
        links.appendChild(
          el("a", {
            text: "lead",
            attrs: { href: rec.salesNavLeadUrl, target: "_blank", rel: "noreferrer" },
          })
        );
      if (rec.companyLinkedInUrl)
        links.appendChild(
          el("a", {
            text: "company",
            attrs: { href: rec.companyLinkedInUrl, target: "_blank", rel: "noreferrer" },
          })
        );
      if (links.childNodes.length) card.appendChild(links);
      list.appendChild(card);
    }

    const n = s.included;
    $("review-total").textContent = `${n} prospect${n === 1 ? "" : "s"}`;
  }

  /** Select or deselect every captured row, one toggle per row that changes. */
  async function setAllSelected(selected) {
    if (!currentBatch) return;
    const records = currentBatch.records;
    for (let i = 0; i < records.length; i += 1) {
      const rec = records[i];
      if (!!rec._excluded === !selected) continue;
      const view = await send({
        type: "TOGGLE_EXCLUDE",
        stableKey: rec._stableKey || null,
        index: i,
      });
      if (view && view.ok) currentBatch = view.batchView;
    }
    renderBatch(currentBatch);
  }

  // ---- export -------------------------------------------------------------

  async function doExport(format) {
    const state = $("save-state");
    const r = await send({ type: "EXPORT_BATCH", format });
    const note = paragraph(
      r && r.ok
        ? `Downloaded ${r.filename} (${r.records} contacts).`
        : (r && (r.message || r.error)) || "Export failed.",
      { tiny: true, muted: true }
    );
    state.appendChild(note);
  }

  // ---- archived campaign-era drafts ---------------------------------------

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
      await refreshPermissionState();
      closeSettings();
    }
  }

  function openSettings() {
    const version =
      (chrome.runtime.getManifest && chrome.runtime.getManifest().version) || "unknown";
    $("settings-version").textContent = version;
    $("settings-connection").textContent = $("conn-text").textContent;
    showView("settings");
  }

  function closeSettings() {
    showView(returnView || "loading");
  }

  // ---- shared panel API ---------------------------------------------------
  //
  // sidepanel-profile.js owns mode switching and the profile/company workflows;
  // it drives the shared metadata, Save and outcome cards through this small
  // surface so there is exactly one implementation of each.
  self.VMRPanel = {
    send,
    el,
    ensureHostPermission,
    backendBase,
    setSaveHandler,
    renderSaveResult,
    renderSaveFailure,
    refreshLabelSuggestions,
    syncBatchSaveAction,
    setConnection,
    refreshPermissionState,
    showView,
    isSticky,
    setFeedback,
    doSave,
    getPrefs: () => currentPrefs,
    setPrefs: (p) => {
      currentPrefs = p;
    },
    setMetadata: (m) => {
      currentMetadata = m || { labels: [], note: null };
      renderMetadata();
    },
    getBatch: () => currentBatch,
    refreshDetect,
    setReturnView: (v) => {
      returnView = v;
    },
    getReturnView: () => returnView,
  };

  // ---- wire up ------------------------------------------------------------

  async function init() {
    $("settings-toggle").appendChild(shell.icon("gear", 14));
    $("empty-icon").appendChild(shell.icon("search", 18));

    $("capture-btn").addEventListener("click", capture);
    $("listings-retry-btn").addEventListener("click", capture);
    $("capture-cancel-btn").addEventListener("click", cancelCapture);
    if (chrome.runtime.onMessage && chrome.runtime.onMessage.addListener) {
      chrome.runtime.onMessage.addListener((message) => {
        if (message && message.type === "CS_SCROLL_PROGRESS") onScrollProgress(message);
      });
    }
    $("listings-review-btn").addEventListener("click", () => {
      showView("listings-review");
      syncBatchSaveAction();
      renderReview();
      $("person-details-btn").hidden = true;
      $("save-back-btn").textContent = "Back to selection";
      $("save-back-btn").onclick = () => {
        showView("listings-select");
        renderRecords();
      };
      refreshPermissionState();
    });
    $("clear-btn").addEventListener("click", async () => {
      if (!confirm("Clear the entire capture batch? This cannot be undone.")) return;
      const r = await send({ type: "CLEAR_BATCH" });
      if (r && r.ok) {
        renderSkipped(null, 0);
        renderBatch(r.batchView);
        showView("listings-select");
      }
    });
    $("select-all").addEventListener("change", (e) => setAllSelected(e.target.checked));
    $("only-issues").addEventListener("change", renderRecords);
    $("export-json").addEventListener("click", () => doExport("json"));
    $("export-csv").addEventListener("click", () => doExport("csv"));
    $("save-btn").addEventListener("click", doSave);
    $("save-settings").addEventListener("click", saveSettings);
    $("settings-close").addEventListener("click", closeSettings);
    $("settings-toggle").addEventListener("click", () => {
      if (shell.getView() === "settings") closeSettings();
      else openSettings();
    });
    $("outcome-back").addEventListener("click", () => showView(returnView || "listings-select"));
    $("send-target").addEventListener("change", async (e) => {
      const r = await send({ type: "SET_PREFS", prefs: { sendTarget: e.target.value } });
      if (r && r.ok) currentPrefs = r.prefs;
      refreshPermissionState();
    });

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
    $("migration-export").addEventListener("click", () => send({ type: "EXPORT_LEGACY_ARCHIVE" }));

    showView("loading");

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
    refreshPermissionState();
    refreshLabelSuggestions();
  }

  document.addEventListener("DOMContentLoaded", init);
})();

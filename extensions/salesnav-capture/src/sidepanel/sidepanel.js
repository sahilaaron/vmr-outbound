/**
 * Side-panel core: shared helpers, the optional labels/note card, the shared
 * Save action and outcome, and the Sales Navigator listings workflow.
 *
 * Contact-first: Campaign selection is an optional, durable filing shortcut.
 * The operator can always save permanent Contacts without choosing one.
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

  const { constants, contactSchema, handoff, normalize, warnings: warningClass } = self.SNCapture;
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
  let currentFilingContext = { campaignId: null };
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

  // ---- UI-016: which page does the outcome on screen belong to? ------------
  //
  // An outcome is about one specific capture — this person, this company, this
  // search. Sticking to it while the operator is standing in it is right;
  // sticking to it once they have navigated somewhere else is how one profile's
  // result would come to sit above another profile's page. The context recorded
  // beside the retained result is what tells the two apart.

  let retainedContext = null;

  const CONTEXT_KIND_FOR_SURFACE = {
    [constants.SURFACES.SALESNAV_PEOPLE_RESULTS]: "listings",
    [constants.SURFACES.PERSON_PROFILE]: "profile",
    [constants.SURFACES.COMPANY_PROFILE]: "company",
  };

  /** Record the page an outcome belongs to; null when it has none. */
  function setRetainedContext(context) {
    retainedContext = context && context.kind ? context : null;
  }

  /**
   * Whether the retained outcome belongs to the page just detected.
   *
   *   "match"   — this outcome is about this page
   *   "other"   — it is about a different page
   *   "unknown" — it has no page: a save in flight, a failed save, or a result
   *               stored before UI-016. Not knowing is stated, never guessed;
   *               the caller decides what to do with it.
   */
  function retainedStatus(surfaceName, url) {
    if (!retainedContext) return "unknown";
    const kind = CONTEXT_KIND_FOR_SURFACE[surfaceName] || null;
    if (kind !== retainedContext.kind) return "other";
    // A results batch is captured across several pages of one search, so it is
    // placed by workflow rather than by URL.
    if (kind === "listings") return "match";
    if (!retainedContext.url) return "unknown";
    const here = normalize.normalizeLinkedInUrl(url);
    if (!here.valid) return "unknown";
    return here.url === retainedContext.url ? "match" : "other";
  }

  function setFeedback(elm, text, tone) {
    elm.textContent = text || "";
    if (tone) elm.setAttribute("data-tone", tone);
    else elm.removeAttribute("data-tone");
  }

  /**
   * Ensure the host permission for a backend call is held.
   *
   * The hosted VMR deployment is a REQUIRED host permission (manifest
   * `host_permissions`), so it is granted at install and this returns
   * immediately without opening a dialog. Only the optional loopback
   * development origins are still requested, with the current click gesture.
   */
  async function ensureHostPermission(url) {
    const perms = self.SNCapture.permissions;
    const pattern = perms.originPatternForUrl(url);
    if (!pattern) return { granted: false, pattern: null, reason: "not_loopback" };
    if (!perms.requiresRuntimeGrant(url)) return { granted: true, pattern, required: true };
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
    // "connected" is a probed fact and is not downgraded to a permission state.
    // "unreachable" deliberately IS re-derived: it used to be sticky, so a badge
    // that latched on one failed save stayed wrong forever, including after the
    // backend came back.
    if (shell.getConnection() === "connected") return;
    try {
      const has = await chrome.permissions.contains({ origins: [pattern] });
      setConnection(has ? "allowed" : "not_allowed");
    } catch (_e) {
      /* leave the state as it is rather than claiming something untrue */
    }
  }

  /**
   * Ask the backend whether it is reachable, and say so.
   *
   * The badge used to be written only as a side effect of a save, which made it a
   * record of the last save rather than a statement about the backend. Probing is
   * read-only and costs one request against an endpoint the panel already calls.
   *
   * Silent when permission has not been granted yet: an operator who has not
   * approved loopback access has not failed at anything, and "Not allowed yet"
   * already says the true thing.
   */
  async function probeConnection() {
    const pattern = self.SNCapture.permissions.originPatternForUrl(saveTargetUrl());
    if (!pattern) return;
    try {
      const granted = await chrome.permissions.contains({ origins: [pattern] });
      if (!granted) {
        setConnection("not_allowed");
        return;
      }
    } catch (_e) {
      return;
    }
    const r = await send({ type: "PROBE_BACKEND" });
    if (!r || !r.state) return;
    setConnection(r.state);
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

  // ---- optional Campaign filing ------------------------------------------

  function renderCampaignSelection(campaigns) {
    const select = $("campaign-select");
    const selected = currentFilingContext.campaignId || "";
    select.textContent = "";
    select.appendChild(el("option", { text: "Save Contact only", attrs: { value: "" } }));
    let selectedFound = !selected;
    for (const campaign of campaigns || []) {
      const option = el("option", {
        text: campaign.name,
        attrs: { value: campaign.id },
      });
      if (campaign.id === selected) selectedFound = true;
      select.appendChild(option);
    }
    if (selected && !selectedFound) {
      select.appendChild(
        el("option", {
          text: "Previously selected Campaign (unavailable)",
          attrs: { value: selected },
        })
      );
    }
    select.value = selected;
  }

  async function persistCampaignSelection(campaignId) {
    const r = await send({
      type: "SET_FILING_CONTEXT",
      filingContext: { campaignId: campaignId || null },
    });
    if (r && r.ok) currentFilingContext = r.filingContext;
    renderCampaignSelection([]);
    await refreshCampaigns(false);
  }

  async function refreshCampaigns(requestPermission) {
    const feedback = $("campaign-feedback");
    if (requestPermission) {
      const permission = await ensureHostPermission(backendBase() + constants.CAMPAIGNS_PATH);
      if (!permission.granted) {
        feedback.textContent = "Allow access to the local backend to load Campaigns.";
        feedback.setAttribute("data-tone", "warning");
        return;
      }
    }
    const r = await send({ type: "FETCH_CAMPAIGNS" });
    if (!r || !r.ok) {
      renderCampaignSelection([]);
      feedback.textContent =
        currentFilingContext.campaignId
          ? "The saved Campaign choice could not be refreshed. Capture still saves the Contact."
          : "Campaigns are unavailable. You can still save the Contact.";
      feedback.setAttribute("data-tone", "warning");
      return;
    }
    renderCampaignSelection(r.campaigns);
    feedback.textContent = currentFilingContext.campaignId
      ? "The Contact will also be filed into the selected Campaign."
      : "No Campaign is required. This saves the permanent Contact only.";
    feedback.removeAttribute("data-tone");
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
  function renderSaveResult(result, context) {
    if (!result) return;
    // UI-016: the outcome and the page it belongs to are set together, from one
    // value, so a painted outcome can never disagree with where it came from.
    setRetainedContext(context);
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
    if (counts.campaign_filings_applied) {
      lines.appendChild(
        statusLine(
          "Added to Campaign",
          badge(String(counts.campaign_filings_applied), { tone: "success" })
        )
      );
      shown += 1;
    }
    if (counts.campaign_filings_pending) {
      lines.appendChild(
        statusLine(
          "Campaign filing waiting",
          badge(String(counts.campaign_filings_pending), { tone: "warning" })
        )
      );
      shown += 1;
    }
    if (counts.campaign_filings_failed) {
      lines.appendChild(
        statusLine(
          "Campaign filing failed (Contact saved)",
          badge(String(counts.campaign_filings_failed), { tone: "danger" })
        )
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
    // UI-016: a failure is not about a saved page — nothing was saved. It has no
    // context, so re-detection holds it where the operator is standing rather
    // than deciding it belongs elsewhere.
    setRetainedContext(null);
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
    $("outcome-back").textContent = "Back to review";
    showView("outcome");
    shell.setSteps(3, { state: "failed", label: "Failed" });
  }

  async function doSave() {
    if (!saveHandler) return;
    // A save in flight has no saved page yet; the outcome is placed when the
    // backend answers (UI-016).
    setRetainedContext(null);
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
      // The worker returns the context it stored beside the result, so the
      // outcome painted now and the outcome restored after a reopen are placed
      // by exactly the same value.
      renderSaveResult(r.result, r.resultContext);
      return;
    }
    const detail = handoff.describeSendError(r);
    const unreachable = detail.code === "network_error" || detail.code === "timeout";
    // Not being signed in is not a failed backend: the reviewed draft is intact,
    // and the one thing that fixes it is the sign-in action — offered right here
    // so the operator never has to go looking for the connection screen.
    const needsSignIn = detail.code === "account_link_required";
    if (needsSignIn) renderAccount({ connected: false, accountEmail: null });
    setConnection(needsSignIn ? "sign_in_required" : unreachable ? "unreachable" : "connected");
    renderSaveFailure(detail, {
      tone: needsSignIn ? "warning" : "danger",
      title: needsSignIn
        ? "Connect to VMR Outbound"
        : unreachable
          ? "Connection lost"
          : "Capture failed",
      body: needsSignIn
        ? "Nothing was sent. What you reviewed is still here — sign in and save again."
        : unreachable
          ? "The backend didn't answer. Nothing was saved, and what you reviewed is still here."
          : "Nothing was saved. What you reviewed is still here.",
      retryLabel: needsSignIn ? "Sign in to VMR Outbound" : undefined,
      onRetry: needsSignIn
        ? async () => {
            if (await connectAccount()) await doSave();
          }
        : undefined,
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
      });
    }
  }

  // Operator-facing wording for every code this surface can receive. A raw code
  // must never reach the operator, so the fallback below is a sentence, not the
  // identifier — and any code added to the vocabulary without a label here will
  // read as unexplained rather than as machine noise.
  const WARN_LABELS = {
    [WARN.MISSING_FIELD]: "missing",
    [WARN.SELECTOR_FAILURE]: "could not be located",
    [WARN.DUPLICATE_UNCERTAIN]: "uncertain identity",
    [WARN.DUPLICATE_COLLAPSED]: "seen more than once on the page",
    [WARN.MALFORMED_URL]: "profile URL not normalized",
    [WARN.NO_STABLE_IDENTITY]: "no stable link",
    [WARN.PLACEHOLDER_VALUE]: "the page showed a placeholder, not a value",
    // UI-013: the value is present and usable. This says where it came from.
    [WARN.DERIVED_VALUE]: "LinkedIn link resolved from the Sales Navigator ID",
  };

  function warnLabel(code) {
    return WARN_LABELS[code] || "an unlabelled capture note";
  }

  function recordWarningCodes(rec) {
    return warningClass.codes(rec.warnings || []);
  }

  /** Codes that mean the operator must look at this record. */
  function recordFaultCodes(rec) {
    return warningClass.codes(warningClass.split(rec.warnings || []).faults);
  }

  /** Codes that only explain where a value came from. */
  function recordProvenanceCodes(rec) {
    return warningClass.codes(warningClass.split(rec.warnings || []).provenance);
  }

  /** True when this record needs an operator's attention, not merely a note. */
  function recordNeedsReview(rec) {
    return warningClass.hasReviewFault(rec.warnings || []);
  }

  function recordTone(rec) {
    if (recordWarningCodes(rec).includes(WARN.NO_STABLE_IDENTITY)) return "danger";
    // Provenance-only records are not toned: nothing about them is wrong.
    if (recordNeedsReview(rec)) return "warning";
    return null;
  }

  /** The badge a record carries, or null when it is clean and unannotated. */
  function recordBadge(rec) {
    const faults = recordFaultCodes(rec);
    if (faults.includes(WARN.NO_STABLE_IDENTITY)) {
      return badge("No stable link", {
        tone: "danger",
        title:
          "No profile or lead URL was on the row — it can be saved, but it will be staged for review.",
      });
    }
    // The badge stays, but it should now be rare rather than universal.
    //
    // It used to appear on nearly every row, which made it worthless — a badge
    // that is always on carries no information. The cause was upstream, not
    // here: a row whose LinkedIn link came from the resolving alias was reported
    // as a MISSING_FIELD fault even though the panel was showing a working link
    // for it. That is now classified as provenance (see src/common/extraction.js),
    // so this badge is left for rows an operator genuinely has to look at.
    if (faults.length) {
      return badge("Needs review", { tone: "warning", title: faults.map(warnLabel).join(", ") });
    }
    const provenance = recordProvenanceCodes(rec);
    if (provenance.length) {
      // Visible and inspectable, but not a fault: the record is complete.
      return badge("Derived", {
        tone: "info",
        title: provenance.map(warnLabel).join(", "),
      });
    }
    return null;
  }

  /** A1 · the selectable row list. A ticked box means "included in the save". */
  // ---- DAT-020: the LinkedIn action beside a prospect ------------------------
  //
  // Two different things, deliberately distinguishable rather than merged into
  // one "open LinkedIn" button:
  //
  //   observed  the handle the row actually showed. Authoritative identity.
  //   derived   https://www.linkedin.com/in/<verbatim-member-id>. LinkedIn
  //             accepts it and redirects, so it opens the right person — but it
  //             is a navigation aid built from an opaque id, not the published
  //             handle, and it never becomes canonical identity.
  //
  // An observed handle always wins, visually and semantically. The derived alias
  // says so in its own label, its title and its accessible name, so an operator
  // is never told the system knows a handle it does not.
  function linkedInAction(rec, name) {
    const who = name || "this prospect";
    if (rec.linkedinProfileUrl) {
      return el("a", {
        class: "li-action observed",
        text: "in",
        attrs: {
          href: rec.linkedinProfileUrl,
          target: "_blank",
          rel: "noreferrer",
          title: "Open LinkedIn profile",
          "aria-label": `Open the LinkedIn profile of ${who}`,
          "data-linkedin": "observed",
        },
      });
    }
    if (rec.linkedinAliasUrl) {
      return el("a", {
        class: "li-action derived",
        text: "in",
        attrs: {
          href: rec.linkedinAliasUrl,
          target: "_blank",
          rel: "noreferrer",
          title: "Open resolving LinkedIn alias — derived from the Sales Navigator ID",
          "aria-label": `Open the resolving LinkedIn alias for ${who}, derived from the Sales Navigator ID`,
          "data-linkedin": "derived",
        },
      });
    }
    return null;
  }

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
    // UI-013: counted on review faults, not on "carries any warning at all".
    const flagged = currentBatch.records.filter(recordNeedsReview).length;
    $("select-all-aside").textContent = flagged ? `${flagged} need review` : "";

    const onlyIssues = $("only-issues").checked;
    currentBatch.records.forEach((rec, index) => {
      // UI-013: the triage filter follows the review state. Filtering on "has
      // any warning" would put the whole batch back on screen and undo the
      // point of the classification.
      if (onlyIssues && !recordNeedsReview(rec)) return;

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

      const rowBadge = recordBadge(rec);

      const row = el(
        "div",
        {
          class: "prospect" + (rec._excluded ? " deselected" : ""),
          attrs: tone ? { "data-tone": tone } : {},
        },
        [
          el("label", { class: "check stacked" }, [checkbox]),
          body,
          rowBadge,
          // Last, so the icon is the right-most thing in the row whether or not
          // a badge is present. It is the one control on the card, and a
          // consistent position is worth more than grouping it with the text.
          linkedInAction(rec, rec.rawFullName),
        ]
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

    // UI-013: "ready" means nothing needs correcting. A record can be ready and
    // still carry provenance notes — those are rendered below, not counted here.
    const ready = records.filter((r) => !recordNeedsReview(r)).length;
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
      const faults = recordFaultCodes(rec);
      const tone = recordTone(rec);
      const card = box(tone ? { tone } : {}, [
        el("div", { class: "line" }, [
          el("span", { class: "t prospect-name", text: rec.rawFullName || "(name not shown)" }),
          faults.length
            ? badge(faults.includes(WARN.NO_STABLE_IDENTITY) ? "No stable link" : "Needs review", {
                tone: tone === "danger" ? "danger" : "warning",
              })
            : badge("Ready", { tone: "success" }),
        ]),
        kv("Company", rec.companyName, { missing: !rec.companyName, emptyText: "Missing company" }),
        rec.title ? kv("Role", rec.title) : null,
      ]);
      // Every warning is rendered, whatever its class — UI-013 changes how they
      // are toned and summarised, never whether the operator can see them.
      if (codes.length) {
        const warnRow = el("div", { class: "badge-row" });
        for (const code of codes) {
          const fields = (rec.warnings || [])
            .filter((w) => w.code === code && w.field)
            .map((w) => w.field);
          warnRow.appendChild(
            badge(warnLabel(code) + (fields.length ? ": " + fields.join(", ") : ""), {
              tone: warningClass.isProvenance(code) ? "info" : "warning",
              title: code,
            })
          );
        }
        card.appendChild(warnRow);
        if (!faults.length) {
          card.appendChild(
            paragraph(
              "Complete. The note above records where a value came from — nothing needs correcting.",
              { tiny: true }
            )
          );
        }
      }
      const links = el("div", { class: "prospect-links" });
      if (rec.linkedinProfileUrl)
        links.appendChild(
          el("a", {
            text: "profile",
            attrs: { href: rec.linkedinProfileUrl, target: "_blank", rel: "noreferrer" },
          })
        );
      // DAT-020: only when no handle was observed, so the review screen never
      // offers two competing "this is their profile" links for one person.
      else if (rec.linkedinAliasUrl)
        links.appendChild(
          el("a", {
            text: "LinkedIn",
            attrs: {
              href: rec.linkedinAliasUrl,
              target: "_blank",
              rel: "noreferrer",
              title: "Derived from the Sales Navigator ID, not a published handle",
              "aria-label": `Open the resolving LinkedIn alias for ${rec.rawFullName || "this prospect"}, derived from the Sales Navigator ID`,
              "data-linkedin": "derived",
            },
          })
        );
      if (rec.salesNavLeadUrl)
        links.appendChild(
          el("a", {
            text: "Sales Navigator",
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

  // ---- connection ----------------------------------------------------------

  function loadPrefsIntoUi(prefs) {
    currentPrefs = prefs;
    $("max-records").value = prefs.maxRecordsPerBatch || 500;
  }

  // ---- the VMR Outbound account link ---------------------------------------
  //
  // The panel never sees a token, an installation id, a backend URL or a
  // credential — the worker answers with two facts: whether this install is
  // linked, and to which account. Everything below renders those two facts and
  // offers the only two actions that exist.

  // null until the worker has answered, so an install whose state is not known
  // yet is never accused of being signed out.
  let currentAccount = null;

  /** Paint the connection card, the sign-in prompt and the header badge. */
  function renderAccount(account) {
    if (account) currentAccount = account;
    const known = !!currentAccount;
    const connected = known && currentAccount.connected === true;
    const email = (known && currentAccount.accountEmail) || null;

    const stateLine = $("account-state");
    if (stateLine) {
      stateLine.textContent = connected
        ? "Connected to VMR Outbound"
        : known
          ? "Not connected to VMR Outbound"
          : "Checking your VMR Outbound connection…";
    }
    const badgeSlot = $("account-badge");
    if (badgeSlot) {
      badgeSlot.textContent = "";
      if (known) {
        badgeSlot.appendChild(
          connected
            ? badge("Connected", { tone: "success", dot: true })
            : badge("Sign in needed", { tone: "warning", dot: true })
        );
      }
    }
    const emailLine = $("account-email");
    if (emailLine) {
      emailLine.textContent = email || "—";
      emailLine.className = email ? "v" : "v empty";
    }
    const note = $("account-note");
    if (note) {
      note.textContent = !known
        ? "Captures are saved into your own VMR Outbound account."
        : connected
          ? "Captures are saved into this VMR Outbound account. The connection is remembered, including after you restart Chrome."
          : "Sign in once. VM Prospector then saves captured contacts into your own VMR Outbound account.";
    }
    // Neither action is offered until the answer is actually known: an install
    // that is still being checked has not failed at anything, and offering a
    // sign-in there would make a working link look broken.
    const connectBtn = $("account-connect");
    if (connectBtn) {
      connectBtn.hidden = !known || connected;
      connectBtn.textContent = "Sign in to VMR Outbound";
    }
    const disconnectBtn = $("account-disconnect");
    if (disconnectBtn) disconnectBtn.hidden = !connected;

    // The one prompt an operator ever needs: shown only while there is nothing
    // linked, above whatever they are doing, because nothing can be saved until
    // it is done.
    const card = $("signin-card");
    if (card) card.hidden = !known || connected;

    // Only downgrade the header when the link is genuinely the thing standing in
    // the way; a probed "connected" is a stronger fact and is left alone.
    if (known && !connected && shell.getConnection() !== "saving") {
      setConnection("sign_in_required");
    }
  }

  /**
   * Ask the worker for the link, letting it connect silently first.
   *
   * The silent attempt is the whole point of the design: an operator already
   * signed in to VMR Outbound gets a linked extension with no window and no
   * click, and a restart re-authorizes from the stored refresh token without
   * anybody being asked for anything.
   */
  async function refreshAccount(autoConnect) {
    const r = await send({ type: "GET_ACCOUNT_STATE", autoConnect: autoConnect === true });
    if (!r || !r.ok || !r.account) return null;
    renderAccount(r.account);
    return r.account;
  }

  /** The single interactive sign-in action. */
  async function connectAccount() {
    const buttons = [$("account-connect"), $("signin-btn")].filter(Boolean);
    const message = $("signin-message");
    for (const btn of buttons) btn.disabled = true;
    // Say that something is happening before anything opens. The sign-in window
    // can take a moment to appear, and a button that greys out with no other
    // change is indistinguishable from a click that did nothing.
    if (message) message.textContent = "Opening the VMR Outbound sign-in window…";
    // The hosted deployment is a REQUIRED host permission, so this resolves
    // immediately and opens no dialog. It still runs because a development
    // install can be pointed at a loopback backend, which is an optional
    // permission and does need the click's gesture to request it.
    const permission = await ensureHostPermission(
      backendBase() + constants.ACCOUNT_LINK_PATHS.TOKEN
    );
    if (!permission.granted) {
      for (const btn of buttons) btn.disabled = false;
      if (message) {
        message.textContent =
          "Allow VM Prospector to reach the configured backend, then sign in. " +
          "Nothing has been sent.";
      }
      setConnection("not_allowed");
      return false;
    }
    const r = await send({ type: "CONNECT_ACCOUNT" });
    for (const btn of buttons) btn.disabled = false;
    if (r && r.account) renderAccount(r.account);
    if (r && r.ok) {
      setConnection("connected");
      await probeConnection();
      return true;
    }
    // One of the categories `account-link.js` classifies, so the panel names
    // what actually went wrong instead of collapsing every failure into "the
    // window was closed, or VMR Outbound declined this install".
    const described = handoff.describeSendError({ error: (r && r.error) || "sign_in_failed" });
    if (message) message.textContent = `${described.headline} ${described.detail}`.trim();
    return false;
  }

  async function disconnectAccount() {
    if (!confirm("Disconnect this browser from VMR Outbound? Captures will stop until you sign in again.")) {
      return;
    }
    const r = await send({ type: "DISCONNECT_ACCOUNT" });
    if (r && r.account) renderAccount(r.account);
    else renderAccount({ connected: false, accountEmail: null });
    setConnection("sign_in_required");
  }

  // ---- development overrides -----------------------------------------------
  //
  // Built here rather than in sidepanel.html so an ordinary panel's DOM carries
  // no trace of them — no backend field, no mock receiver, no credential input,
  // nothing to find in a screenshot or a saved page. They appear only when the
  // worker reports the development gate (an object at chrome.storage.local key
  // `vmr_dev_overrides` with `enabled: true`), which nothing in any shipped UI
  // can write: it has to be created by hand from the extension's own devtools
  // console on an unpacked build. The worker enforces the same gate on the
  // messages below, so building this section is not what authorises anything.

  let devFields = null;

  function devField(labelText, node) {
    return el("div", { class: "field" }, [
      el("label", { class: "field-label", text: labelText }),
      node,
    ]);
  }

  function renderDevSettings(enabled) {
    const host = $("dev-settings");
    if (!host) return;
    host.textContent = "";
    host.hidden = !enabled;
    devFields = null;
    if (!enabled) return;

    const prefs = currentPrefs || {};
    const target = el("select", { class: "input" }, [
      el("option", { text: "Hosted VMR backend", attrs: { value: "backend" } }),
      el("option", { text: "Mock receiver (development)", attrs: { value: "mock" } }),
    ]);
    target.value = prefs.sendTarget || "backend";
    const backendUrl = el("input", { class: "input", attrs: { type: "text" } });
    backendUrl.value = prefs.backendBaseUrl || "";
    const mockUrl = el("input", { class: "input", attrs: { type: "text" } });
    mockUrl.value = prefs.mockReceiverUrl || "";
    const credential = el("input", {
      class: "input",
      attrs: { type: "password", autocomplete: "off", spellcheck: "false" },
    });
    const credentialState = el("span", { class: "v", text: "Not set" });
    const credentialFeedback = el("p", { class: "p tiny muted" });

    devFields = { target, backendUrl, mockUrl, credential, credentialState, credentialFeedback };

    host.appendChild(el("span", { class: "eyebrow", text: "Development overrides" }));
    host.appendChild(devField("Where captures go", target));
    host.appendChild(devField("Backend base URL", backendUrl));
    host.appendChild(devField("Mock receiver URL", mockUrl));
    host.appendChild(
      el("div", { class: "field" }, [
        el("label", { class: "field-label", text: "Legacy capture credential" }),
        credential,
        el("div", { class: "badge-row" }, [
          credentialState,
          el("button", {
            class: "btn btn-sm",
            text: "Set",
            attrs: { type: "button" },
            on: { click: saveDevCredential },
          }),
          el("button", {
            class: "btn btn-sm",
            text: "Clear",
            attrs: { type: "button" },
            on: { click: clearDevCredential },
          }),
        ]),
        credentialFeedback,
      ])
    );
    host.appendChild(
      el("div", { class: "kv" }, [
        el("span", { class: "k", text: "Extension ID" }),
        el("span", { class: "v mono", text: (chrome.runtime && chrome.runtime.id) || "unknown" }),
      ])
    );
    void refreshDevCredentialState();
  }

  function showDevCredentialState(state) {
    if (!devFields) return;
    if (state && state.storageAvailable === false) {
      devFields.credentialState.textContent = "Unavailable in this browser";
      return;
    }
    devFields.credentialState.textContent =
      state && state.hasCredential ? "Set for this session" : "Not set";
  }

  async function refreshDevCredentialState() {
    const r = await send({ type: "GET_CREDENTIAL_STATE" });
    showDevCredentialState(r);
  }

  async function saveDevCredential() {
    if (!devFields) return;
    const value = devFields.credential.value;
    const r = await send({ type: "SET_CAPTURE_CREDENTIAL", credential: value });
    // Cleared on every path, including the refusal: a rejected paste is still a
    // secret and has no business sitting in a DOM node.
    devFields.credential.value = "";
    if (r && r.ok) {
      showDevCredentialState(r);
      devFields.credentialFeedback.textContent = "Credential set for this browser session.";
      await probeConnection();
      return;
    }
    showDevCredentialState({ hasCredential: false });
    const described = handoff.describeSendError({ error: r && r.error });
    devFields.credentialFeedback.textContent = `${described.headline} ${described.detail}`.trim();
  }

  async function clearDevCredential() {
    if (!devFields) return;
    const r = await send({ type: "CLEAR_CAPTURE_CREDENTIAL" });
    devFields.credential.value = "";
    showDevCredentialState(r);
    devFields.credentialFeedback.textContent = "Credential cleared.";
  }

  async function saveSettings() {
    const patch = {
      maxRecordsPerBatch: Math.max(1, Math.min(500, parseInt($("max-records").value, 10) || 500)),
    };
    // Only ever sent from a panel the development gate has unlocked; the worker
    // drops these keys again if the gate is not actually present.
    if (devFields) {
      patch.backendBaseUrl = devFields.backendUrl.value.trim();
      patch.mockReceiverUrl = devFields.mockUrl.value.trim();
      patch.sendTarget = devFields.target.value;
    }
    const r = await send({ type: "SET_PREFS", prefs: patch });
    if (r && r.ok) {
      currentPrefs = r.prefs;
      await refreshPermissionState();
      // This screen is where "can it reach the backend?" is actually being
      // asked, and a saved setting is exactly when the answer changes.
      await probeConnection();
      closeSettings();
    }
  }

  function openSettings() {
    const version =
      (chrome.runtime.getManifest && chrome.runtime.getManifest().version) || "unknown";
    $("settings-version").textContent = version;
    $("settings-connection").textContent = $("conn-text").textContent;
    renderAccount(null);
    void refreshAccount(false);
    showView("settings");
    // Re-check on open rather than mirroring a possibly stale badge. This is the
    // one screen whose whole purpose is the connection.
    void probeConnection();
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
    renderAccount,
    showView,
    isSticky,
    setRetainedContext,
    retainedStatus,
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
    setFilingContext: (value) => {
      currentFilingContext = value || { campaignId: null };
      renderCampaignSelection([]);
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
    $("save-btn").addEventListener("click", doSave);
    $("save-settings").addEventListener("click", saveSettings);
    $("settings-close").addEventListener("click", closeSettings);
    $("settings-toggle").addEventListener("click", () => {
      if (shell.getView() === "settings") closeSettings();
      else openSettings();
    });
    $("outcome-back").addEventListener("click", () => showView(returnView || "listings-select"));
    $("account-connect").addEventListener("click", connectAccount);
    $("account-disconnect").addEventListener("click", disconnectAccount);
    $("signin-btn").addEventListener("click", connectAccount);

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
    $("campaign-select").addEventListener("change", (e) =>
      persistCampaignSelection(e.target.value)
    );
    $("campaign-refresh").addEventListener("click", () => refreshCampaigns(true));

    showView("loading");

    const state = await send({ type: "GET_STATE" });
    if (state && state.ok) {
      loadPrefsIntoUi(state.prefs);
      renderDevSettings(!!(state.dev && state.dev.enabled));
      if (state.account) renderAccount(state.account);
      currentMetadata = state.metadata || { labels: [], note: null };
      currentFilingContext = state.filingContext || { campaignId: null };
      renderMetadata();
      renderCampaignSelection([]);
      renderBatch(state.batchView);
      // Recovery: if contacts were already saved (panel closed/reloaded, or a
      // navigation failed), restore the outcome without recapturing or resaving.
      // The context travels with it so the first page detection can tell whether
      // this outcome is still the page the operator is looking at (UI-016).
      if (state.lastResult) renderSaveResult(state.lastResult, state.lastResultContext);
    }
    refreshPermissionState();
    refreshLabelSuggestions();
    refreshCampaigns(false);
    // The cold open: connect silently if the operator is already signed in to
    // VMR Outbound, so the ordinary case needs no click at all. Only after that
    // has had its chance is the backend probed, or the operator asked to sign in.
    await refreshAccount(true);
    // On a cold open, say something true about the backend rather than "Not
    // checked" until the operator happens to attempt a save.
    void probeConnection();
  }

  document.addEventListener("DOMContentLoaded", init);
})();

/**
 * Side-panel mode controller: which supported surface is in the active tab, the
 * person-profile capture workflow, and the company-evidence workflow.
 *
 * Contact-first: the primary action for a person is "Save Contact", or "Refresh
 * Contact" when the backend already knows that exact profile URL. No campaign is
 * selected, required, or stored anywhere in this panel.
 *
 * Exactly one detected interface is on screen at a time, chosen by the page the
 * operator already opened — there is no manual mode selector:
 *   Sales Navigator listings · person profile · company profile ·
 *   unsupported page · sign-in or security check · page unavailable.
 *
 * All captured text is rendered with textContent (never innerHTML). Nothing is
 * transmitted without the operator pressing Save.
 */
(function () {
  "use strict";

  const { constants, handoff, permissions, liveSync, warnings: warningClass } = self.SNCapture;
  const { SURFACES, CAPTURE_STATUS } = constants;
  const panel = self.VMRPanel;
  const shell = self.VMRShell;
  const { el, badge, callout, kv, statusLine, box, paragraph } = shell;
  const $ = (id) => document.getElementById(id);
  const send = panel.send;

  let currentDraft = null;
  let currentCompanyDraft = null;
  let currentMode = null;
  // UI-016: consumed by the first paintMode after the panel opens. See the
  // guard in paintMode for why a starting panel needs telling apart from a
  // running one.
  let firstPaint = true;
  let profileMatch = null;
  // The match state arrives after the draft is first painted, so the review has
  // to be repainted once when it lands — and exactly once, never in a loop.
  let renderedMatch = null;

  // ---- warning presentation ------------------------------------------------
  //
  // Warning codes are stable machine strings meant for the backend. The
  // operator reviewing a capture needs to know what was not read and why, so
  // each code renders as a sentence with the raw code kept on the title
  // attribute. A code with no entry falls back to the raw string: a new warning
  // must stay visible rather than be swallowed.

  const WARNING_LABELS = {
    missing_field: (what) => `${what || "a field"} was not on the page`,
    unparsed_value: (what) => `${what || "a value"} was shown but could not be read`,
    placeholder_value: (what) => `${what || "a field"} showed a placeholder, not a value`,
    selector_failure: (what) => `${what || "a field"} could not be located`,
    malformed_url: () => "the profile URL could not be normalized",
    missing_section: (what) => `${what || "a section"} was not on the page`,
    unparsed_timeline: () => "a role's dates could not be read",
    unrecognized_layout: (what) => `${what || "a block"} used an unrecognised layout`,
    no_stable_identity: () => "no stable identity could be established",
    duplicate_uncertain_identity: () => "identity is uncertain — this may be a duplicate",
    // UI-013 — provenance, not a fault. The value is present; this says how it
    // was produced so a derivation can never read as an observation.
    derived_value: (what) => `${what || "a value"} was worked out from another value on the page`,
    duplicate_collapsed: () => "seen more than once and recorded once",
  };

  // Wire-contract field names read as machine identifiers. The operator sees
  // the field as it is labelled in the review card above.
  const FIELD_LABELS = {
    full_name: "name",
    displayed_location: "location",
    connection_count: "connections",
    connection_count_raw: "connections",
    linkedin_profile_url: "profile URL",
    timeline_text: "role dates",
    company_name: "company",
    job_title: "job title",
    experience_entry: "an experience entry",
  };

  function fieldLabel(field) {
    if (!field) return null;
    return FIELD_LABELS[field] || String(field).replace(/_/g, " ");
  }

  /** A contract section name ("experience") as the operator reads it. */
  function sectionLabel(section) {
    const s = String(section || "").replace(/_/g, " ");
    return s ? s.charAt(0).toUpperCase() + s.slice(1) : "A section";
  }

  /**
   * Badges for one set of warning lists.
   *
   * UI-013: `only` selects a class — "faults", "provenance", or undefined for
   * both. The classification comes from common/warnings.js; this function only
   * decides wording and tone. A code with no label here reads as an unlabelled
   * note rather than leaking its raw identifier to the operator.
   */
  function warningBadges(warningLists, only) {
    const seen = new Map();
    for (const w of warningClass.flatten(warningLists)) {
      const provenance = warningClass.isProvenance(w.code);
      if (only === "faults" && provenance) continue;
      if (only === "provenance" && !provenance) continue;
      const raw = w.field || w.section || null;
      const what = fieldLabel(raw);
      const key = w.code + (raw ? ":" + raw : "");
      if (seen.has(key)) continue;
      const build = WARNING_LABELS[w.code];
      seen.set(key, {
        label: build ? build(what) : "an unlabelled capture note",
        tone: provenance ? "info" : "warning",
      });
    }
    return Array.from(seen.entries())
      .slice(0, 12)
      .map(([key, v]) => badge(v.label, { tone: v.tone, title: key }));
  }

  function allWarnings(draftView) {
    if (!draftView) return [];
    const p = draftView.profile || {};
    return [p.warnings, draftView.pageWarnings].concat(
      (draftView.experiences || []).map((e) => e.warnings)
    );
  }

  /**
   * True when something about this capture needs the operator's attention.
   *
   * UI-013: provenance notes no longer count. A profile whose only warnings say
   * where a value came from is complete, and saying otherwise made the review
   * badge meaningless. A partial capture status and missing sections are still
   * genuine gaps and still count.
   */
  function hasGaps(draftView) {
    if (!draftView) return false;
    if (draftView.status === CAPTURE_STATUS.PARTIAL) return true;
    if ((draftView.missingSections || []).length) return true;
    return warningClass.hasReviewFault(warningClass.flatten(allWarnings(draftView)));
  }

  // ---- mode switching ------------------------------------------------------

  const MODE_CONTEXT = {
    [SURFACES.SALESNAV_PEOPLE_RESULTS]: {
      icon: "users",
      label: "Sales Navigator · Search results",
    },
    [SURFACES.PERSON_PROFILE]: { icon: "user", label: "LinkedIn · Person profile" },
    [SURFACES.COMPANY_PROFILE]: { icon: "building", label: "LinkedIn · Company page" },
    [SURFACES.CHALLENGE]: {
      icon: "alert",
      label: "LinkedIn needs your attention",
      tone: "warn",
    },
    [SURFACES.UNAVAILABLE]: {
      icon: "alert",
      label: "This page is no longer available",
      tone: "muted",
    },
    [SURFACES.UNSUPPORTED]: { icon: "page", label: "This page isn't supported", tone: "muted" },
  };

  const DEFAULT_VIEW = {
    [SURFACES.SALESNAV_PEOPLE_RESULTS]: "listings-select",
    [SURFACES.PERSON_PROFILE]: "person-review",
    [SURFACES.COMPANY_PROFILE]: "company-review",
    [SURFACES.CHALLENGE]: "challenge",
    [SURFACES.UNAVAILABLE]: "unavailable",
    [SURFACES.UNSUPPORTED]: "unsupported",
  };

  // Why a page is unsupported, in the operator's terms. An unrecognised reason
  // falls back to the generic guidance rather than inventing an explanation.
  const UNSUPPORTED_REASONS = {
    profile_subroute:
      "You're on a profile sub-page. Open the main profile (linkedin.com/in/…) to capture this person.",
    unsupported_sales_surface:
      "This Sales Navigator view isn't supported. Open a people-search results page (/sales/search/people).",
    not_linkedin:
      "Open a LinkedIn profile (linkedin.com/in/…), a company page, or a Sales Navigator people-search results page to capture.",
    unrecognized_route:
      "Open a LinkedIn profile (linkedin.com/in/…), a company page, or a Sales Navigator people-search results page to capture.",
  };

  /**
   * Paint the shell for the detected surface and, unless the operator is in the
   * middle of an outcome or the settings, switch the body to that mode's view.
   *
   * UI-011: the strip is painted from the SAME detect result that decides the
   * mode and targets the parser. Painting it on demand is how it once came to
   * display one profile's URL beside another profile's data.
   */
  function paintMode(detected) {
    const r = detected || {};
    const mode = r.surface || SURFACES.UNSUPPORTED;
    const cold = firstPaint;
    firstPaint = false;
    // On a cold open there is no previous mode to differ from, so `changed`
    // would be true no matter what the operator was last looking at. That is
    // exactly what made a restored outcome unreachable (UI-016 / D-8): the
    // guard below could protect a running panel and never a starting one.
    // Whether a restored outcome survives is decided by `outcomeHolds`, which
    // asks about the page rather than about the surface kind.
    const changed = !cold && mode !== currentMode;
    const onOutcome = shell.getView() === "outcome";
    const holds = onOutcome && outcomeHolds(mode, r.url, cold);
    currentMode = mode;

    if (mode === SURFACES.SALESNAV_PEOPLE_RESULTS) {
      // The listings controller owns its own strip (it also carries the row
      // count and page number).
      panel.refreshDetect();
    } else {
      const ctx = MODE_CONTEXT[mode] || MODE_CONTEXT[SURFACES.UNSUPPORTED];
      shell.setContext({
        icon: ctx.icon,
        label: ctx.label,
        tone: ctx.tone,
        badge: contextBadgeFor(mode),
        url: r.url || "",
      });
    }

    // The live-tab follower reports the surface and the URL but not the
    // classifier's reason, so an explicit detect is what makes the guidance
    // specific. Without one, the generic copy stands rather than a wrong one.
    if (mode === SURFACES.UNSUPPORTED && r.reason) {
      $("unsupported-detail").textContent =
        UNSUPPORTED_REASONS[r.reason] || UNSUPPORTED_REASONS.unrecognized_route;
    }

    // A different page means the previous outcome is no longer what the
    // operator is looking at; anything else leaves their place alone.
    const keepsPlace = panel.isSticky() && !changed && (!onOutcome || holds);
    if (!keepsPlace) {
      panel.showView(DEFAULT_VIEW[mode] || "unsupported");
      if (mode === SURFACES.PERSON_PROFILE) syncPersonActions();
      if (mode === SURFACES.COMPANY_PROFILE) syncCompanyActions();
    } else if (holds) {
      // The outcome stays, but "Back to this page" has to land on the page the
      // operator is actually looking at — not on whatever the panel happened to
      // be showing while it was starting up.
      panel.setReturnView(DEFAULT_VIEW[mode] || "unsupported");
    }
    return mode;
  }

  /**
   * Whether the outcome currently on screen is still the operator's place.
   *
   * An outcome belongs to the page it was captured from, so "is this still that
   * page?" — not "did the surface kind change?" — is the question that releases
   * it. Asked this way it answers for a panel that has just opened as well as
   * one that has been running, and it still lets genuine navigation through.
   *
   * A cold open restores only an outcome that can be placed on this page: an
   * outcome whose page is unknown is not put back, so one profile's result can
   * never appear above another profile's. While running, only a positive
   * mismatch releases the view — a save in flight or a failed save has no page
   * of its own and must stay where the operator is standing.
   */
  function outcomeHolds(mode, url, cold) {
    const status = panel.retainedStatus(mode, url);
    return cold ? status === "match" : status !== "other";
  }

  /** The one-word state of the detected page, shown in the strip. */
  function contextBadgeFor(mode) {
    if (mode === SURFACES.PERSON_PROFILE) {
      if (!currentDraft) return null;
      if (profileMatch === "exact") return { text: "Already saved", tone: "brand" };
      if (hasGaps(currentDraft)) return { text: "Needs review", tone: "warning" };
      return { text: "Ready to capture", tone: "success" };
    }
    if (mode === SURFACES.COMPANY_PROFILE) {
      if (!currentCompanyDraft) return null;
      const c = currentCompanyDraft.company || {};
      if (!c.website) return { text: "Needs review", tone: "warning" };
      return { text: "Ready to capture", tone: "success" };
    }
    if (mode === SURFACES.CHALLENGE) return { text: "Blocked", tone: "warning" };
    if (mode === SURFACES.UNAVAILABLE) return { text: "Unavailable", tone: "danger" };
    return null;
  }

  async function refreshMode() {
    const detected = await send({ type: "DETECT_SURFACE" });
    const mode = paintMode(detected);

    if (mode === SURFACES.PERSON_PROFILE) {
      // DOM-level refinement (login wall / structure) + entry count badge.
      const d = await send({ type: "PROFILE_DETECT" });
      if (d && d.ok && d.page) {
        if (d.page.surface === SURFACES.CHALLENGE || d.page.surface === SURFACES.UNAVAILABLE) {
          paintMode({ surface: d.page.surface, url: detected && detected.url });
          return;
        }
        $("profile-exp-badge").textContent =
          d.page.experienceEntryCount != null
            ? `${d.page.experienceEntryCount} experience entr${d.page.experienceEntryCount === 1 ? "y" : "ies"} visible on the page`
            : "";
      }
    }
  }

  // ---- person: review (B1 / B2 / B3) ---------------------------------------

  function identityBlock(name, lines) {
    const text = el("div", { class: "identity-text" }, [
      el("span", { class: "identity-name", text: name || "(name not shown)" }),
    ]);
    for (const line of lines || []) {
      if (!line) continue;
      text.appendChild(
        el("span", {
          class: "identity-line" + (line.sub ? " sub" : "") + (line.missing ? " missing" : ""),
          text: line.text,
        })
      );
    }
    return el("div", { class: "identity" }, [
      el("span", {
        class: "identity-avatar",
        text: shell.initialOf(name),
        attrs: { "aria-hidden": "true" },
      }),
      text,
    ]);
  }

  function confirmedBadge(present, presentText, missingText) {
    return present
      ? badge(presentText || "Confirmed", { tone: "success" })
      : badge(missingText || "Not on the page", { tone: "warning" });
  }

  function renderDraft(draftView) {
    currentDraft = draftView;
    const boxEl = $("profile-review");
    boxEl.textContent = "";
    if (!draftView) {
      boxEl.appendChild(paragraph("No profile captured yet.", { muted: true }));
      syncPersonActions();
      return;
    }
    const p = draftView.profile || {};
    const currentRole = (draftView.currentRoles || [])[0] || null;

    boxEl.appendChild(
      identityBlock(p.full_name, [
        p.headline ? { text: p.headline } : null,
        p.displayed_location
          ? { text: p.displayed_location, sub: true }
          : { text: "Location not shown", missing: true },
      ])
    );

    if (profileMatch === "exact") {
      boxEl.appendChild(
        callout(
          "brand",
          "This person is already in VM Prospector",
          "Capturing again records what the page shows today. It never overwrites what's already there."
        )
      );
    } else if (profileMatch === "ambiguous") {
      boxEl.appendChild(
        callout(
          "warning",
          "Identity is ambiguous",
          "More than one contact could match this profile. Saving stages it for review — nothing is merged automatically."
        )
      );
    }

    // UI-013: faults drive the "could not be read" box; provenance notes are
    // rendered separately, below, so nothing is hidden and nothing is mislabelled.
    const gapBadges = warningBadges(allWarnings(draftView), "faults");
    const provenanceBadges = warningBadges(allWarnings(draftView), "provenance");
    const missing = draftView.missingSections || [];

    if (gapBadges.length || missing.length) {
      const warnBox = box({ tone: "warning" }, [
        el("span", { class: "box-title", text: "Some details could not be read" }),
      ]);
      warnBox.appendChild(
        kv("Current company", currentRole && currentRole.company_name, {
          missing: !(currentRole && currentRole.company_name),
          emptyText: "Missing company",
        })
      );
      warnBox.appendChild(
        kv("Location", p.displayed_location, {
          missing: !p.displayed_location,
          emptyText: "Not shown",
        })
      );
      warnBox.appendChild(
        statusLine(
          "Experience",
          draftView.experienceCount
            ? badge(`${draftView.experienceCount} entries`, { tone: "success" })
            : badge("Not loaded", { tone: "warning" })
        )
      );
      for (const section of missing) {
        warnBox.appendChild(statusLine(sectionLabel(section), badge("Section missing", { tone: "warning" })));
      }
      if (gapBadges.length) {
        warnBox.appendChild(el("div", { class: "badge-row" }, gapBadges));
      }
      boxEl.appendChild(warnBox);
      boxEl.appendChild(
        box({ sunk: true }, [
          paragraph(
            "Scroll the profile so the missing sections load, then read the page again. Empty fields stay empty — VM Prospector will not fill them in."
          ),
        ])
      );
    } else {
      boxEl.appendChild(
        box({}, [
          el("span", { class: "eyebrow", text: "Current company" }),
          el("div", { class: "line" }, [
            el("span", {
              class: "t prospect-name",
              text: (currentRole && currentRole.company_name) || "Not shown",
            }),
            confirmedBadge(!!(currentRole && currentRole.company_name)),
          ]),
          currentRole && currentRole.job_title
            ? paragraph(currentRole.job_title, { tiny: true, muted: true })
            : null,
        ])
      );
      boxEl.appendChild(
        box({ sunk: true }, [
          el("span", { class: "eyebrow", text: "Read from this page" }),
          statusLine("Name, headline, location", confirmedBadge(!!p.full_name)),
          statusLine("Profile URL", confirmedBadge(!!p.linkedin_profile_url)),
          statusLine(
            `Experience (${draftView.experienceCount} entr${draftView.experienceCount === 1 ? "y" : "ies"})`,
            confirmedBadge(draftView.experienceCount > 0, "Confirmed", "Not loaded")
          ),
        ])
      );
    }

    // Provenance notes, whichever branch ran above. Always rendered, never
    // toned as a fault: these say how a value was produced, not that it is wrong.
    if (provenanceBadges.length) {
      boxEl.appendChild(
        box({ sunk: true }, [
          el("span", { class: "eyebrow", text: "Where these values came from" }),
          el("div", { class: "badge-row" }, provenanceBadges),
          paragraph("Recorded so a worked-out value is never mistaken for one read off the page.", {
            tiny: true,
            muted: true,
          }),
        ])
      );
    }

    if ((draftView.experiences || []).length) {
      const excluded = (draftView.excludedSections || []).includes("experience");
      const list = box({ sunk: true }, [
        el("div", { class: "line" }, [
          el("span", { class: "eyebrow t", text: "Experience" }),
          excluded ? badge("Excluded from the save", { tone: "neutral" }) : null,
        ]),
      ]);
      for (const e of draftView.experiences) {
        list.appendChild(
          box({}, [
            el("span", {
              class: "prospect-name",
              text: `${e.position_index}. ${e.job_title || "(no title)"}`,
            }),
            kv("Company", e.company_name),
            kv("Timeline", [e.timeline_text, e.duration_text].filter(Boolean).join(" · ")),
            kv("Current", e.is_current === true ? "yes" : e.is_current === false ? "no" : null),
          ])
        );
      }
      boxEl.appendChild(list);
    }

    $("profile-exclude-exp").checked = (draftView.excludedSections || []).includes("experience");
    $("profile-exclude-exp-row").hidden = false;
    syncPersonActions();
  }

  function truncate(text, max) {
    const s = String(text);
    return s.length > max ? s.slice(0, max) + "…" : s;
  }

  // ---- person: confirm (B4) ------------------------------------------------

  function renderPersonConfirm() {
    const host = $("person-confirm-body");
    host.textContent = "";
    if (!currentDraft) {
      host.appendChild(paragraph("Nothing has been read from this page yet.", { muted: true }));
      return;
    }
    const p = currentDraft.profile || {};
    const currentRole = (currentDraft.currentRoles || [])[0] || null;
    const currentEntry = (currentDraft.experiences || []).find((e) => e.is_current === true) || {};

    host.appendChild(
      box({}, [
        el("span", { class: "eyebrow brand", text: "Person" }),
        kv("Name", p.full_name, { strong: true }),
        kv("Role", currentRole && currentRole.job_title, { emptyText: "Not shown" }),
        kv("Location", p.displayed_location, { emptyText: "Not shown" }),
        kv("LinkedIn", p.linkedin_profile_url, { mono: true }),
        p.about_text
          ? kv("About", truncate(p.about_text, 120))
          : kv("About", null, { emptyText: "Not captured" }),
      ])
    );

    const association = box({}, [
      el("span", { class: "eyebrow brand", text: "Company association" }),
      kv("Company", currentRole && currentRole.company_name, {
        strong: true,
        missing: !(currentRole && currentRole.company_name),
        emptyText: "Missing company",
      }),
      kv("Current role", currentRole && currentRole.job_title, { emptyText: "Not shown" }),
      kv("Company page", currentEntry.company_linkedin_url, {
        mono: true,
        emptyText: "Not linked",
      }),
      statusLine("Website", badge("Not confirmed", { tone: "warning" })),
    ]);
    host.appendChild(association);

    host.appendChild(
      box({ sunk: true }, [
        el("span", { class: "eyebrow", text: "Included in the save" }),
        statusLine(
          "Experience section",
          (currentDraft.excludedSections || []).includes("experience")
            ? badge("Excluded", { tone: "neutral" })
            : badge(`${currentDraft.experienceCount} entries`, { tone: "success" })
        ),
        statusLine(
          "Page evidence",
          badge(currentDraft.status === CAPTURE_STATUS.PARTIAL ? "Partial" : "Complete", {
            tone: currentDraft.status === CAPTURE_STATUS.PARTIAL ? "warning" : "success",
          })
        ),
      ])
    );
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
    profileMatch = match && match.ok ? match.match : null;
    if (profileMatch === "exact") {
      panel.setSaveHandler({
        handler: () => send({ type: "SAVE_CONTACT" }),
        label: "Refresh Contact",
      });
    } else if (profileMatch === "ambiguous") {
      panel.setSaveHandler({
        handler: () => send({ type: "SAVE_CONTACT" }),
        label: "Save Contact (identity ambiguous)",
      });
    }
    shell.setContext({
      icon: MODE_CONTEXT[SURFACES.PERSON_PROFILE].icon,
      label: MODE_CONTEXT[SURFACES.PERSON_PROFILE].label,
      badge: contextBadgeFor(SURFACES.PERSON_PROFILE),
      url: $("surface-detail").textContent,
    });
    if (profileMatch !== renderedMatch) {
      renderedMatch = profileMatch;
      renderDraft(currentDraft);
    }
  }

  /** Keep the person action group in step with whether a draft exists. */
  function syncPersonActions() {
    const has = !!currentDraft;
    $("person-continue-btn").disabled = !has;
    $("person-continue-btn").textContent =
      profileMatch === "exact" ? "Update this prospect" : "Capture prospect";
    $("profile-capture-btn").textContent = has ? "Read this page again" : "Read this profile page";
    $("profile-capture-btn").className = has ? "btn full" : "btn btn-primary full";
    $("profile-clear-btn").hidden = !has;
    syncProfileSaveAction();
  }

  // ---- person actions ------------------------------------------------------

  async function doCapture() {
    const fb = $("profile-capture-feedback");
    panel.setFeedback(fb, "Reading this page…");
    $("profile-capture-btn").disabled = true;
    const r = await send({ type: "PROFILE_CAPTURE" });
    $("profile-capture-btn").disabled = false;
    if (!r || !r.ok) {
      panel.setFeedback(fb, (r && (r.message || r.error)) || "Capture failed.", "bad");
      return;
    }
    const blocked = {
      [CAPTURE_STATUS.CHALLENGE_DETECTED]: "Login/security check detected — nothing captured.",
      [CAPTURE_STATUS.UNAVAILABLE_PROFILE]: "This profile is unavailable — nothing captured.",
      [CAPTURE_STATUS.UNSUPPORTED_PAGE]: "Not a supported main profile page — nothing captured.",
      [CAPTURE_STATUS.STRUCTURE_UNRECOGNIZED]:
        "Profile detected but the page structure was not recognized. Nothing captured.",
    };
    if (blocked[r.captureStatus]) {
      panel.setFeedback(fb, blocked[r.captureStatus], "warn");
    } else {
      panel.setFeedback(
        fb,
        r.captureStatus === CAPTURE_STATUS.PARTIAL
          ? "Captured with gaps — review what could not be read."
          : "Captured. Review before saving."
      );
    }
    renderDraft(r.draftView);
    panel.showView("person-review");
  }

  async function doClear() {
    if (!confirm("Clear the reviewed profile draft?")) return;
    const r = await send({ type: "PROFILE_CLEAR" });
    if (r && r.ok) {
      profileMatch = null;
      renderDraft(null);
      panel.setSaveHandler({ handler: null, label: "Save Contact", disabled: true, reset: true });
      panel.setFeedback($("profile-capture-feedback"), "Draft cleared.");
      panel.showView("person-review");
    }
  }

  function openPersonConfirm() {
    renderPersonConfirm();
    panel.showView("person-confirm");
    $("person-details-btn").hidden = false;
    $("save-back-btn").textContent = "Back to review";
    $("save-back-btn").onclick = () => panel.showView("person-review");
    syncProfileSaveAction();
    panel.refreshPermissionState();
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
    currentCompanyDraft = draftView;
    const boxEl = $("company-review");
    boxEl.textContent = "";
    if (!draftView) {
      boxEl.appendChild(paragraph("No company captured yet.", { muted: true }));
      syncCompanyActions();
      return;
    }
    const c = draftView.company || {};

    const identity = identityBlock(c.name, [
      c.industry
        ? {
            text: [c.industry, c.size_range && `${c.size_range} employees`]
              .filter(Boolean)
              .join(" · "),
          }
        : { text: "Industry not shown", missing: true },
      c.headquarters_text ? { text: c.headquarters_text, sub: true } : null,
    ]);
    identity.querySelector(".identity-avatar").classList.add("square");
    boxEl.appendChild(identity);

    boxEl.appendChild(
      box({}, [
        el("span", { class: "eyebrow", text: "Read from this page" }),
        kv("Industry", c.industry, { emptyText: "Not shown" }),
        kv("Company size", c.size_range, { emptyText: "Not shown" }),
        kv("Displayed employees", c.employee_count_raw, { emptyText: "Not shown" }),
        kv("Headquarters", c.headquarters_text, { emptyText: "Not shown" }),
        kv("Founded", c.founded_raw, { emptyText: "Not shown" }),
        kv("Company page", c.company_linkedin_url, { mono: true }),
      ])
    );

    // The domain is the identity of a company: it is stated plainly, and a name
    // on its own never stands in for it.
    if (c.website) {
      boxEl.appendChild(
        box({ tone: "success" }, [
          kv("Website", c.website, { strong: true }),
          el("div", {}, [badge("Shown on this page", { tone: "success", dot: true })]),
        ])
      );
    } else {
      boxEl.appendChild(
        box({ tone: "warning" }, [
          el("span", { class: "box-title", text: "Domain not confirmed" }),
          kv("Website", null, { missing: true, emptyText: "Not shown on this page" }),
          paragraph(
            "A company name on its own isn't enough to identify a company. VM Prospector confirms the domain before this becomes a company record.",
            { tiny: true }
          ),
        ])
      );
      boxEl.appendChild(
        box({ sunk: true }, [
          paragraph(
            "If this is the home page, open About and read it again for website, industry, size and headquarters."
          ),
        ])
      );
    }

    if ((draftView.missingSections || []).length) {
      boxEl.appendChild(
        el(
          "div",
          { class: "badge-row" },
          draftView.missingSections.map((s) => badge("missing: " + s, { tone: "warning" }))
        )
      );
    }
    const companyBadges = warningBadges([c.warnings, draftView.pageWarnings]);
    if (companyBadges.length) {
      boxEl.appendChild(el("div", { class: "badge-row" }, companyBadges));
    }
    syncCompanyActions();
  }

  function syncCompanyActions() {
    const has = !!currentCompanyDraft;
    $("company-continue-btn").disabled = !has;
    $("company-capture-btn").textContent = has
      ? "Read this page again"
      : "Read this company page";
    $("company-capture-btn").className = has ? "btn full" : "btn btn-primary full";
    $("company-clear-btn").hidden = !has;
    $("company-send-btn").disabled = !has;
  }

  function renderCompanyConfirm() {
    const host = $("company-confirm-body");
    host.textContent = "";
    if (!currentCompanyDraft) {
      host.appendChild(paragraph("Nothing has been read from this page yet.", { muted: true }));
      return;
    }
    const c = currentCompanyDraft.company || {};
    host.appendChild(
      box({}, [
        el("span", { class: "box-title", text: "Is this the right company?" }),
        kv("Name", c.name, { strong: true }),
        kv("Company page", c.company_linkedin_url, { mono: true }),
        kv("Headquarters", c.headquarters_text, { emptyText: "Not shown" }),
      ])
    );
    if (c.website) {
      host.appendChild(
        box({ tone: "success" }, [
          el("span", { class: "eyebrow", text: "Website" }),
          el("div", { class: "line" }, [
            el("span", { class: "t prospect-name", text: c.website }),
            badge("Shown on this page", { tone: "success" }),
          ]),
        ])
      );
    } else {
      host.appendChild(
        box({ tone: "warning" }, [
          el("span", { class: "eyebrow", text: "Website" }),
          el("div", { class: "line" }, [
            el("span", { class: "t", text: "Not shown on this page" }),
            badge("Missing", { tone: "warning" }),
          ]),
        ])
      );
    }
    host.appendChild(
      box({ sunk: true }, [
        el("span", { class: "eyebrow", text: "Domain states this panel can report" }),
        statusLine("Shown on this page", badge("Confirmed", { tone: "success" })),
        statusLine("Not shown", badge("Missing", { tone: "warning" })),
        paragraph(
          "Whether the domain matches a company VM Prospector already knows is decided in the app, not here.",
          { tiny: true, muted: true }
        ),
      ])
    );
  }

  async function doCompanyCapture() {
    const fb = $("company-capture-feedback");
    panel.setFeedback(fb, "Reading this page…");
    $("company-capture-btn").disabled = true;
    const r = await send({ type: "COMPANY_CAPTURE" });
    $("company-capture-btn").disabled = false;
    if (!r || !r.ok) {
      panel.setFeedback(fb, (r && (r.message || r.error)) || "Capture failed.", "bad");
      return;
    }
    const blocked = {
      [CAPTURE_STATUS.CHALLENGE_DETECTED]: "Login/security check detected — nothing captured.",
      [CAPTURE_STATUS.UNAVAILABLE_PROFILE]: "This company page is unavailable — nothing captured.",
      [CAPTURE_STATUS.UNSUPPORTED_PAGE]: "Not a supported company page — nothing captured.",
      [CAPTURE_STATUS.STRUCTURE_UNRECOGNIZED]:
        "Company page detected but its structure was not recognized. Nothing captured.",
    };
    panel.setFeedback(
      fb,
      blocked[r.captureStatus] ||
        (r.captureStatus === CAPTURE_STATUS.PARTIAL
          ? "Captured with gaps — open the About page for full firmographics if needed."
          : "Captured. Review before saving."),
      blocked[r.captureStatus] ? "warn" : null
    );
    renderCompanyDraft(r.draftView);
    $("company-send-state").textContent = "";
    $("company-send-actions").textContent = "";
    panel.showView("company-review");
  }

  async function doCompanyClear() {
    if (!confirm("Clear the reviewed company draft?")) return;
    const r = await send({ type: "COMPANY_CLEAR" });
    if (r && r.ok) {
      renderCompanyDraft(null);
      $("company-send-state").textContent = "";
      $("company-send-actions").textContent = "";
      panel.showView("company-review");
    }
  }

  function openCompanyConfirm() {
    renderCompanyConfirm();
    panel.showView("company-confirm");
    panel.refreshPermissionState();
  }

  function showCompanyOutcome() {
    $("saving-card").hidden = true;
    $("save-card").hidden = true;
    $("company-result-card").hidden = false;
    panel.showView("outcome");
  }

  async function doCompanySend() {
    const state = $("company-send-state");
    const actions = $("company-send-actions");
    // A save in flight has no saved page yet; the outcome is placed when the
    // backend answers (UI-016).
    panel.setRetainedContext(null);
    state.textContent = "";
    actions.textContent = "";
    $("save-card").hidden = true;
    $("company-result-card").hidden = true;
    $("saving-card").hidden = false;
    $("saving-title").textContent = "Saving company evidence";
    panel.showView("outcome");
    shell.setSteps(3, {});
    panel.setConnection("saving");
    for (const node of document.querySelectorAll("[data-actions]")) node.hidden = true;
    const savingActions = document.querySelector('[data-actions="saving"]');
    if (savingActions) savingActions.hidden = false;

    const perm = await ensureHostPermission(panel.backendBase() + constants.COMPANY_INTAKE_PATH);
    if (!perm.granted) {
      panel.setConnection("not_allowed");
      showCompanyOutcome();
      shell.setSteps(3, { state: "failed", label: "Failed" });
      state.appendChild(
        callout(
          "warning",
          "Allow VM Prospector to reach the app",
          "Loopback access was not granted, so nothing has been sent."
        )
      );
      actions.appendChild(
        el("button", {
          class: "btn btn-primary full",
          text: "Allow and save",
          attrs: { type: "button" },
          on: { click: doCompanySend },
        })
      );
      return;
    }

    const r = await send({ type: "COMPANY_SEND" });
    if (r && r.ok) {
      panel.setConnection("connected");
      renderCompanyStagedResult(r.result, r.resultContext);
      return;
    }
    // Nothing was saved, so this outcome belongs to no page.
    panel.setRetainedContext(null);
    const detail = handoff.describeSendError(r);
    const unreachable = detail.code === "network_error" || detail.code === "timeout";
    panel.setConnection(unreachable ? "unreachable" : "connected");
    showCompanyOutcome();
    shell.setSteps(3, { state: "failed", label: "Failed" });
    state.appendChild(
      callout(
        "danger",
        unreachable ? "Connection lost" : "Capture failed",
        unreachable
          ? "The backend didn't answer. Nothing was saved, and what you reviewed is still here."
          : "Nothing was saved. What you reviewed is still here."
      )
    );
    state.appendChild(
      box({ sunk: true }, [
        el("span", { class: "eyebrow", text: "Details" }),
        paragraph(detail.headline),
        el("p", { class: "detail-block", text: "code: " + (detail.code || "unknown") }),
      ])
    );
    if (detail.canRetry !== false) {
      actions.appendChild(
        el("button", {
          class: "btn btn-primary full",
          text: "Try again",
          attrs: { type: "button" },
          on: { click: doCompanySend },
        })
      );
    }
  }

  function renderCompanyStagedResult(result, context) {
    if (!result) return;
    // UI-016: painted and placed from one value, exactly as the person outcome is.
    panel.setRetainedContext(context);
    const state = $("company-send-state");
    const actions = $("company-send-actions");
    state.textContent = "";
    actions.textContent = "";
    showCompanyOutcome();
    shell.setSteps(3, { done: true, label: "Done" });
    const already = result.alreadyReceived;
    state.appendChild(
      callout(
        "success",
        already ? "Already saved (idempotent)" : "Saved successfully",
        ((currentCompanyDraft && currentCompanyDraft.company && currentCompanyDraft.company.name) ||
          "This company") + " · saved to the VM Prospector workflow."
      )
    );
    const activity = box({ sunk: true }, [el("span", { class: "eyebrow", text: "Activity" })]);
    activity.appendChild(statusLine("Company page saved", badge("Done", { tone: "success" })));
    activity.appendChild(statusLine("Page evidence kept", badge("Done", { tone: "success" })));
    const website =
      currentCompanyDraft && currentCompanyDraft.company
        ? currentCompanyDraft.company.website
        : null;
    activity.appendChild(
      statusLine(
        "Domain",
        website
          ? badge(website, { tone: "success" })
          : badge("Not shown — review in the app", { tone: "warning" })
      )
    );
    if (result.outcome) {
      activity.appendChild(statusLine("Backend outcome", badge(result.outcome, { tone: "brand" })));
    }
    state.appendChild(activity);
    if (result.workbenchUrl && handoff.isOpenableWorkbenchUrl(result.workbenchUrl)) {
      actions.appendChild(
        el("a", {
          class: "btn btn-primary full",
          text: "Open company workspace",
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
    $("person-continue-btn").addEventListener("click", openPersonConfirm);
    $("person-details-btn").addEventListener("click", () => panel.showView("person-details"));
    $("person-details-done").addEventListener("click", openPersonConfirm);
    $("profile-exclude-exp").addEventListener("change", async () => {
      const r = await send({ type: "PROFILE_TOGGLE_SECTION", section: "experience" });
      if (r && r.ok) renderDraft(r.draftView);
    });

    $("company-capture-btn").addEventListener("click", doCompanyCapture);
    $("company-clear-btn").addEventListener("click", doCompanyClear);
    $("company-continue-btn").addEventListener("click", openCompanyConfirm);
    $("company-back-btn").addEventListener("click", () => panel.showView("company-review"));
    $("company-send-btn").addEventListener("click", doCompanySend);

    const state = await send({ type: "PROFILE_GET_STATE" });
    if (state && state.ok) {
      panel.setPrefs(state.prefs);
      if (state.metadata) panel.setMetadata(state.metadata);
      if (state.filingContext) panel.setFilingContext(state.filingContext);
      renderDraft(state.draftView);
      // Recovery: a saved outcome (and the reviewed draft that produced it)
      // survives panel close/reopen without recapture or resave. Reading state
      // is all this does — no capture, no submission, nothing sent.
      //
      // A capture read after that save removes the result in the worker, so a
      // newer unsent draft is never sitting behind an older outcome by the time
      // the panel asks. The context restored alongside it decides whether the
      // outcome is put back on this page or left behind on its own.
      if (state.lastResult) panel.renderSaveResult(state.lastResult, state.lastResultContext);
    }
    const companyState = await send({ type: "COMPANY_GET_STATE" });
    if (companyState && companyState.ok) {
      renderCompanyDraft(companyState.draftView);
      if (companyState.lastResult) {
        renderCompanyStagedResult(companyState.lastResult, companyState.lastResultContext);
      }
    }
    // One explicit classification before the live follower takes over: it is
    // the only call that returns the classifier's *reason*, which is what makes
    // an unsupported page's guidance specific instead of generic.
    await refreshMode();
    startLiveSync();
  }

  // ---- UI-011: follow the active tab ---------------------------------------

  const PHASE_TEXT = {
    waiting_for_supported_profile: "Waiting for a LinkedIn profile…",
    profile_detected: "Profile detected — reading…",
    loading_profile_content: "Reading this profile…",
    preview_ready: "Preview ready.",
    additional_content_loaded: "Additional content loaded — preview updated.",
    unsupported_surface: "This LinkedIn page is not a person profile.",
    completed_with_warnings: "Preview ready — review what could not be read.",
  };

  let sync = null;

  function startLiveSync() {
    if (sync) {
      sync.start();
      return;
    }
    sync = liveSync.createLiveSync({
      chrome,
      // The two capabilities this controller gets. Neither writes anything:
      // DETECT_SURFACE classifies the tab, PROFILE_CAPTURE reads the DOM into a
      // local draft. No backend call, no contact, no promotion, no campaign.
      detect: () => send({ type: "DETECT_SURFACE" }),
      preview: async () => {
        // `live` marks this as the panel looking, not the operator reading. A
        // read the operator asked for replaces what they had saved; a preview
        // that runs by itself every time the panel opens must not, or the
        // retained result would survive exactly one reopen (UI-016).
        const r = await send({ type: "PROFILE_CAPTURE", live: true });
        return r && r.ok ? r.draftView : null;
      },
      onState: (state) => {
        // Provenance and payload are painted from one state object, so they
        // cannot disagree.
        if (state.surface === SURFACES.PERSON_PROFILE) {
          renderDraft(state.draft);
        }
        paintMode({ surface: state.surface, url: state.url });
        const feedback = $("profile-capture-feedback");
        if (feedback) panel.setFeedback(feedback, PHASE_TEXT[state.phase] || "");
      },
    });
    sync.start();
  }

  document.addEventListener("DOMContentLoaded", init);
})();

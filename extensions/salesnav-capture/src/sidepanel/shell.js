/**
 * VM Prospector side-panel shell.
 *
 * Owns the parts of the panel that every detected interface shares: the
 * header (product, connection state, settings toggle), the detected-page
 * strip, the three-step rail, which view is on screen, which action group
 * is in the sticky footer, and the small set of presentation primitives
 * (badge, callout, key/value row, icon) the views are built from.
 *
 * Nothing here talks to the network, the tab, or storage — it is pure
 * presentation, so the capture, warning, draft and submission behaviour
 * lives untouched in the controllers that call it.
 *
 * Every node is built with createElement / createElementNS and filled with
 * textContent. No innerHTML anywhere: captured page values can never be
 * rendered as markup.
 */
(function () {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const $ = (id) => document.getElementById(id);

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

  /**
   * Inline icon set (Lucide-derived line icons, stroke 1.75 on a 24 grid —
   * the design system's iconography). Built as SVG nodes rather than markup
   * so the panel needs no icon font, no remote request, and no innerHTML.
   */
  const ICON_PATHS = {
    users: ["M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2", "M22 21v-2a4 4 0 0 0-3-3.87"],
    user: ["M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"],
    building: ["M4 3h16v18H4z", "M9 8h2M9 12h2M13 8h2M13 12h2M10 21v-4h4v4"],
    search: ["M20 20l-3.5-3.5"],
    gear: [
      "M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1",
    ],
    alert: ["M12 8v5", "M12 16.5v.01"],
    page: ["M14 3v5h5", "M14 3H6v18h12V8z"],
  };

  const ICON_CIRCLES = {
    users: [{ cx: 9, cy: 7, r: 4 }],
    user: [{ cx: 12, cy: 7, r: 4 }],
    search: [{ cx: 11, cy: 11, r: 7 }],
    gear: [{ cx: 12, cy: 12, r: 3 }],
    alert: [{ cx: 12, cy: 12, r: 9 }],
  };

  function icon(name, size) {
    const svg = document.createElementNS(SVG_NS, "svg");
    const px = String(size || 13);
    svg.setAttribute("width", px);
    svg.setAttribute("height", px);
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.85");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    for (const c of ICON_CIRCLES[name] || []) {
      const circle = document.createElementNS(SVG_NS, "circle");
      circle.setAttribute("cx", String(c.cx));
      circle.setAttribute("cy", String(c.cy));
      circle.setAttribute("r", String(c.r));
      svg.appendChild(circle);
    }
    for (const d of ICON_PATHS[name] || []) {
      const path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", d);
      svg.appendChild(path);
    }
    return svg;
  }

  // ---- presentation primitives -------------------------------------------

  /**
   * Status pill. Tone is always paired with the word it labels and, when a
   * dot is requested, with a shape — colour is never the only signal.
   */
  function badge(text, opts) {
    const o = opts || {};
    const node = el("span", {
      class: "badge",
      attrs: o.tone ? { "data-tone": o.tone } : {},
      title: o.title || undefined,
    });
    if (o.dot) node.appendChild(el("span", { class: "dot" }));
    node.appendChild(el("span", { text: String(text) }));
    return node;
  }

  function callout(tone, title, bodyText) {
    const node = el("div", { class: "callout", attrs: { "data-tone": tone } }, [
      title ? el("div", { class: "callout-title", text: title }) : null,
    ]);
    if (bodyText) node.appendChild(el("div", { class: "callout-body", text: bodyText }));
    return node;
  }

  /**
   * A label/value line. `value` null or "" renders as a visible em dash so a
   * field that was not read stays visibly empty instead of disappearing.
   */
  function kv(label, value, opts) {
    const o = opts || {};
    const has = value != null && value !== "";
    const classes = ["v"];
    if (o.mono) classes.push("mono");
    if (o.strong) classes.push("strong");
    if (!has) classes.push("empty");
    if (o.missing) classes.push("missing");
    return el("div", { class: "kv" }, [
      el("span", { class: "k", text: label }),
      el("span", {
        class: classes.join(" "),
        text: has ? String(value) : o.emptyText || "—",
      }),
    ]);
  }

  /** A label + badge line (the "Read from this page" / activity rows). */
  function statusLine(label, badgeNode) {
    return el("div", { class: "line" }, [
      el("span", { class: "t", text: label }),
      badgeNode || null,
    ]);
  }

  function box(opts, children) {
    const o = opts || {};
    const classes = ["box"];
    if (o.sunk) classes.push("sunk");
    if (o.dashed) classes.push("dashed");
    return el(
      "div",
      { class: classes.join(" "), attrs: o.tone ? { "data-tone": o.tone } : {} },
      children
    );
  }

  function paragraph(text, opts) {
    const o = opts || {};
    const classes = ["p"];
    if (o.muted) classes.push("muted");
    if (o.tiny) classes.push("tiny");
    return el("p", { class: classes.join(" "), text });
  }

  /** Initial used by the person/company avatar placeholder. */
  function initialOf(name) {
    const s = String(name || "").trim();
    if (!s) return "?";
    return s.slice(0, 1).toUpperCase();
  }

  // ---- header: connection state ------------------------------------------
  //
  // The panel does not poll the backend — there is no health endpoint and
  // adding one would be a new backend contract. The dot therefore reports
  // only what the panel actually knows: whether loopback access has been
  // granted, whether a save is in flight, and how the last save ended.

  const CONNECTION = {
    unknown: { tone: "idle", text: "Not checked" },
    allowed: { tone: "ok", text: "Ready" },
    saving: { tone: "busy", text: "Saving" },
    connected: { tone: "ok", text: "Connected" },
    unreachable: { tone: "bad", text: "Not connected" },
    not_allowed: { tone: "warn", text: "Not allowed yet" },
  };

  let connectionState = "unknown";

  function setConnection(state) {
    const next = CONNECTION[state] ? state : "unknown";
    connectionState = next;
    const info = CONNECTION[next];
    const wrap = $("conn-status");
    if (!wrap) return;
    wrap.setAttribute("data-tone", info.tone);
    $("conn-text").textContent = info.text;
    wrap.setAttribute("aria-label", "Connection to VM Prospector: " + info.text);
  }

  function getConnection() {
    return connectionState;
  }

  // ---- detected-page strip -------------------------------------------------

  /**
   * Paint the detected-page strip. `label` is the plain-language name of the
   * surface the operator is on; the badge is the one-word state of that page.
   */
  function setContext(options) {
    const o = options || {};
    const iconWrap = $("context-icon");
    const strip = $("surface-indicator");
    if (!strip) return;
    strip.setAttribute("data-tone", o.tone || "default");
    iconWrap.textContent = "";
    if (o.icon) iconWrap.appendChild(icon(o.icon, 13));
    $("context-label").textContent = o.label || "";
    const badgeSlot = $("context-badge");
    badgeSlot.textContent = "";
    if (o.badge) {
      badgeSlot.appendChild(
        badge(o.badge.text, { tone: o.badge.tone, dot: o.badge.dot !== false })
      );
    }
    const url = $("surface-detail");
    url.textContent = o.url || "";
    url.hidden = !o.url;
    url.title = o.url || "";
  }

  // ---- step rail -----------------------------------------------------------

  /**
   * `step` is 1..3, or 0 while the page is still being classified. `state`
   * "failed" marks the third segment as failed rather than complete.
   */
  function setSteps(step, options) {
    const o = options || {};
    const wrap = $("steps");
    if (!wrap) return;
    if (!step) {
      wrap.hidden = true;
      return;
    }
    wrap.hidden = false;
    wrap.setAttribute("data-state", o.state || "");
    const segs = wrap.querySelectorAll(".steps-track i");
    segs.forEach((seg, i) => {
      seg.className = "";
      if (i < step) seg.className = o.state === "failed" && i === 2 ? "failed" : "on";
    });
    const text = o.label || (o.done ? "Done" : `Step ${step} of 3`);
    $("steps-text").textContent = text;
    wrap.setAttribute("aria-label", "Capture progress: " + text);
  }

  // ---- views and action groups --------------------------------------------

  // Which step each view sits on, so the rail and the body can never
  // disagree. `null` means the flow does not apply to this state at all
  // (blocked pages, settings) and the rail is hidden.
  const VIEW_STEPS = {
    loading: { step: 0 },
    "listings-select": { step: 1 },
    "listings-empty": { step: 1 },
    "listings-review": { step: 2 },
    "person-review": { step: 1 },
    "person-confirm": { step: 2 },
    "person-details": { step: 2 },
    "company-review": { step: 1 },
    "company-confirm": { step: 2 },
    // Saving, saved and failed all live in one view; the controller sets the
    // rail's final state (in progress / done / failed) when it knows it.
    outcome: { step: 3 },
    unsupported: { step: null },
    challenge: { step: null },
    unavailable: { step: null },
    settings: { step: null },
  };

  let currentView = null;

  function setView(name) {
    currentView = name;
    for (const node of document.querySelectorAll("[data-view]")) {
      node.hidden = node.getAttribute("data-view") !== name;
    }
    for (const node of document.querySelectorAll("[data-actions]")) {
      node.hidden = node.getAttribute("data-actions") !== name;
    }
    const spec = VIEW_STEPS[name] || { step: null };
    setSteps(spec.step, spec);
    const body = $("app-body");
    if (body) body.scrollTop = 0;
    $("settings-toggle").setAttribute("aria-expanded", name === "settings" ? "true" : "false");
  }

  function getView() {
    return currentView;
  }

  self.VMRShell = {
    el,
    icon,
    badge,
    callout,
    kv,
    statusLine,
    box,
    paragraph,
    initialOf,
    setConnection,
    getConnection,
    setContext,
    setSteps,
    setView,
    getView,
    VIEW_STEPS,
  };
})();

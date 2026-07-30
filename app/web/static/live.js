/**
 * Keep a monitor page current without a manual reload.
 *
 * The web UI is otherwise entirely server-rendered with no JavaScript, and that
 * convention is worth keeping — every interaction here is a form and a redirect,
 * which is why the app has no build step and no dependencies. This is a considered
 * exception rather than a crack in it, for one reason: an operator watching a queue
 * drain has to know what the state is *now*, and a page that can only be correct at
 * the moment it was requested cannot tell them. The alternative inside the
 * convention is <meta http-equiv="refresh">, which throws away scroll position and
 * any half-typed input on every tick.
 *
 * What it does: re-fetches the current URL, parses it, and replaces the contents of
 * <main>. Scroll position survives because the document is never navigated.
 *
 * What it deliberately does not do:
 *   - run anywhere except pages that opt in with data-live on <body>
 *   - refresh while a form on the page has focus, so it cannot eat what an operator
 *     is halfway through typing
 *   - refresh a backgrounded tab, which would poll all day for nobody
 *   - retry forever: repeated failures back off and then stop, because a page
 *     silently hammering a dead server is worse than a stale one
 *   - touch anything outside <main>, so the nav and any flash message stay put
 *
 * No dependency, no inline script, no remote code.
 */
(function () {
  "use strict";

  var body = document.body;
  var seconds = parseInt(body.getAttribute("data-live") || "0", 10);
  if (!seconds || seconds < 2) return;

  var main = document.querySelector(".main-inner");
  if (!main) return;

  var STORAGE_KEY = "vmr-live-paused";
  var failures = 0;
  var MAX_FAILURES = 3;
  var timer = null;

  var toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "live-toggle";

  function paused() {
    try {
      return window.sessionStorage.getItem(STORAGE_KEY) === "1";
    } catch (e) {
      return false;
    }
  }

  function setPaused(value) {
    try {
      window.sessionStorage.setItem(STORAGE_KEY, value ? "1" : "0");
    } catch (e) {
      /* a private-mode failure must not break the page */
    }
  }

  function paint() {
    var off = paused();
    toggle.textContent = off ? "Auto-update off" : "Auto-update on";
    toggle.setAttribute("data-state", off ? "off" : "on");
    toggle.setAttribute("aria-pressed", off ? "false" : "true");
    toggle.title = off
      ? "Paused. The page will not change until you reload or switch this back on."
      : "Refreshing every " + seconds + "s. The nav, and any message above, stay put.";
  }

  function typing() {
    var el = document.activeElement;
    if (!el) return false;
    var tag = (el.tagName || "").toLowerCase();
    return tag === "input" || tag === "select" || tag === "textarea" || el.isContentEditable;
  }

  function tick() {
    if (paused() || document.hidden || typing()) return;
    fetch(window.location.href, {
      headers: { "X-Requested-With": "vmr-live" },
      credentials: "same-origin",
    })
      .then(function (response) {
        if (!response.ok) throw new Error("status " + response.status);
        return response.text();
      })
      .then(function (html) {
        var parsed = new DOMParser().parseFromString(html, "text/html");
        var next = parsed.querySelector(".main-inner");
        if (!next) throw new Error("no main content");
        // Never swap while focus moved into a field mid-flight.
        if (typing()) return;
        main.replaceChildren.apply(main, Array.prototype.slice.call(next.childNodes));
        failures = 0;
      })
      .catch(function () {
        failures += 1;
        if (failures >= MAX_FAILURES) {
          setPaused(true);
          paint();
          toggle.textContent = "Auto-update stopped";
          toggle.title = "The page stopped updating after repeated failures. Reload to resume.";
          if (timer) window.clearInterval(timer);
        }
      });
  }

  toggle.addEventListener("click", function () {
    setPaused(!paused());
    paint();
  });

  paint();
  body.appendChild(toggle);
  timer = window.setInterval(tick, seconds * 1000);
})();

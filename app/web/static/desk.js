/**
 * Keyboard movement on the inline sending desk.
 *
 * Vertical is people, horizontal is emails, Escape closes. The targets are
 * plain links the server rendered as data attributes on `[data-desk]`, so
 * the keyboard does exactly what the pointer does and nothing consequential
 * happens on a single keystroke — Mark actioned, Skip and Undo stay buttons.
 *
 * External file because the deployed CSP is `script-src 'self'`.
 */
(function () {
  "use strict";

  function editing(target) {
    if (!target) return false;
    var tag = (target.tagName || "").toLowerCase();
    return (
      tag === "input" ||
      tag === "textarea" ||
      tag === "select" ||
      tag === "button" ||
      target.isContentEditable
    );
  }

  function go(url) {
    if (url) window.location.assign(url);
  }

  document.addEventListener("keydown", function (event) {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return;
    var desk = document.querySelector("[data-desk]");
    if (!desk || editing(event.target)) return;
    var open = document.querySelector("details[open] .v2-edit-form");
    if (open && open.contains(document.activeElement)) return;

    switch (event.key) {
      case "j":
      case "J":
      case "ArrowDown":
        event.preventDefault();
        go(desk.getAttribute("data-next-person"));
        break;
      case "k":
      case "K":
      case "ArrowUp":
        event.preventDefault();
        go(desk.getAttribute("data-prev-person"));
        break;
      case "ArrowRight":
        event.preventDefault();
        go(desk.getAttribute("data-next-email"));
        break;
      case "ArrowLeft":
        event.preventDefault();
        go(desk.getAttribute("data-prev-email"));
        break;
      case "Escape":
        event.preventDefault();
        go(desk.getAttribute("data-close"));
        break;
      default:
        break;
    }
  });

  // Keep the workbook heading in a stable position after a selection.
  var desk = document.querySelector("[data-desk]");
  if (desk && window.location.hash === "#ready") {
    var heading = desk.querySelector(".v2-workbook-head");
    if (heading && typeof heading.scrollIntoView === "function") {
      heading.scrollIntoView({ block: "start", behavior: "instant" });
      window.scrollBy(0, -72);
    }
  }
})();

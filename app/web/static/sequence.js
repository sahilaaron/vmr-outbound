/**
 * Copy control for one email on the sending desk (Campaign > Overview).
 *
 * One delegated listener on `document`, driven by `data-` attributes, exactly
 * like `campaigns.js`. The deployed Content-Security-Policy is
 * `script-src 'self'` with no nonce and no `unsafe-inline`, so an inline
 * handler would not run at all -- and a copy button that silently does nothing
 * is worse than no copy button.
 *
 * What is copied is the *exact* email text. The visible subject and body are
 * rendered without the spreadsheet `neutralize` projection precisely so that
 * what an operator reads, what they copy, and what the message actually says
 * are one string. Neutralization belongs at the CSV boundary, where a cell is
 * evaluated by a spreadsheet; it does not belong in an email.
 */
(function () {
  "use strict";

  var FEEDBACK_ID = "seq-copy-status";
  var RESET_MS = 2400;

  /** The live region every button reports through. One per page, not one per
   * button: a screen reader should not receive seven queued announcements
   * because somebody copied seven messages. */
  function statusRegion() {
    return document.getElementById(FEEDBACK_ID);
  }

  function announce(message, failed) {
    var region = statusRegion();
    if (!region) return;
    region.textContent = message;
    // A success message may fade; a failure must not. If copying did not
    // happen, the operator has to be told and left told, because the only
    // recovery is a manual selection they have to make themselves.
    region.classList.toggle("is-error", Boolean(failed));
  }

  function textFor(button) {
    var doc = button.ownerDocument;
    var subjectId = button.getAttribute("data-copy-subject");
    var bodyId = button.getAttribute("data-copy-body");
    var kind = button.getAttribute("data-copy");
    var subjectNode = subjectId ? doc.getElementById(subjectId) : null;
    var bodyNode = bodyId ? doc.getElementById(bodyId) : null;
    // Read from the rendered nodes rather than from attributes so the copied
    // text is provably the text on screen, and so a full body is not duplicated
    // into the markup twice.
    var subject = subjectNode ? subjectNode.textContent : "";
    var body = bodyNode ? bodyNode.textContent : "";
    // One kind: the desk offers one Copy per email, and it copies the whole
    // email. A button asking for anything else copies nothing, on purpose.
    if (kind === "full") return "Subject: " + subject + "\n\n" + body;
    return null;
  }

  /** Last-resort copy for browsers where `navigator.clipboard` is absent or
   * refuses. `navigator.clipboard` needs a secure context -- HTTPS or
   * localhost -- so plain HTTP on any other host has no async clipboard at
   * all, which makes this path a requirement rather than a nicety. */
  function legacyCopy(text) {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "readonly");
    area.setAttribute("aria-hidden", "true");
    area.style.position = "fixed";
    area.style.top = "-1000px";
    area.style.opacity = "0";
    document.body.appendChild(area);
    var ok = false;
    try {
      area.select();
      ok = document.execCommand("copy");
    } catch (error) {
      ok = false;
    }
    document.body.removeChild(area);
    return ok;
  }

  /** Select the source text in place so the operator can finish the copy with
   * the keyboard. Used only when both programmatic paths have failed. */
  function selectSource(button) {
    var doc = button.ownerDocument;
    var id = button.getAttribute("data-copy-body");
    var node = id ? doc.getElementById(id) : null;
    if (!node || !window.getSelection || !doc.createRange) return;
    var range = doc.createRange();
    range.selectNodeContents(node);
    var selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  function flash(button, message) {
    var original = button.getAttribute("data-copy-label") || button.textContent;
    button.setAttribute("data-copy-label", original);
    button.textContent = message;
    window.setTimeout(function () {
      button.textContent = button.getAttribute("data-copy-label") || original;
    }, RESET_MS);
  }

  function succeeded(button, label) {
    flash(button, "Copied");
    announce(label + " copied.", false);
  }

  function failed(button, label) {
    selectSource(button);
    announce(
      label +
        " could not be copied by this browser. The text is selected — press Ctrl-C, or Cmd-C on a Mac.",
      true
    );
  }

  function copy(button) {
    var text = textFor(button);
    if (text === null) return;
    var label = button.getAttribute("data-copy-label-what") || "Message";
    // Focus deliberately stays on the button throughout: moving it would lose
    // a keyboard operator's place in a page of seven messages.
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () {
          succeeded(button, label);
        },
        function () {
          if (legacyCopy(text)) succeeded(button, label);
          else failed(button, label);
        }
      );
      return;
    }
    if (legacyCopy(text)) succeeded(button, label);
    else failed(button, label);
  }

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!(target instanceof Element)) return;
    var button = target.closest("[data-copy]");
    if (!button) return;
    event.preventDefault();
    copy(button);
  });
})();

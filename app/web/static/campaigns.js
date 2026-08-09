/** Keep destructive campaign archiving behind an explicit browser confirmation. */
(function () {
  "use strict";

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    var message = form.getAttribute("data-archive-confirm");
    if (message && !window.confirm(message)) event.preventDefault();
  });
})();

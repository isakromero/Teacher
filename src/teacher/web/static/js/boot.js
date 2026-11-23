// Classic (non-module) script loaded in <head> before the page module.
// It makes failures visible on the page instead of only in the console: a page script or
// stylesheet that fails to load, an uncaught exception, or an unhandled promise rejection
// adds a message to the #page-error banner. Page modules call window.teacherShowError too.
(function () {
  "use strict";

  var pending = [];

  function show(message) {
    var host = document.getElementById("page-error");
    if (!host) {
      pending.push(message);
      return;
    }
    var line = document.createElement("p");
    line.textContent = message;
    host.appendChild(line);
    host.hidden = false;
  }

  window.teacherShowError = show;

  document.addEventListener("DOMContentLoaded", function () {
    var queued = pending;
    pending = [];
    queued.forEach(show);
  });

  // Capture phase: resource load errors do not bubble, but they do pass through window.
  window.addEventListener(
    "error",
    function (event) {
      var target = event.target;
      if (target && target !== window && target.tagName) {
        if (target.tagName === "SCRIPT") {
          show("A page script failed to load (" + target.src + "). Reload the page to try again.");
        } else if (target.tagName === "LINK") {
          show("A stylesheet failed to load (" + target.href + ").");
        }
        return;
      }
      show("Unexpected error: " + (event.message || "unknown error") + ". Reload the page to try again.");
    },
    true
  );

  window.addEventListener("unhandledrejection", function (event) {
    var reason = event.reason;
    var text = reason && reason.message ? reason.message : String(reason);
    show("Unexpected error: " + text);
  });
})();

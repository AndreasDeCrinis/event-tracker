(function () {
  "use strict";

  var storagePrefix = "event-job-tracker:collapse:";

  function storageKey(details) {
    return storagePrefix + details.dataset.collapseStateKey;
  }

  function getStoredState(details) {
    try {
      return window.localStorage.getItem(storageKey(details));
    } catch (error) {
      return null;
    }
  }

  function setStoredState(details) {
    try {
      window.localStorage.setItem(storageKey(details), details.open ? "open" : "closed");
    } catch (error) {
      // Browsers can block localStorage; the UI should still work normally.
    }
  }

  function restoreState(details) {
    var storedState = getStoredState(details);

    if (storedState === "open") {
      details.open = true;
    } else if (storedState === "closed") {
      details.open = false;
    }
  }

  function initializeCollapsibleState() {
    var collapsibles = document.querySelectorAll("details[data-collapse-state-key]");

    collapsibles.forEach(function (details) {
      restoreState(details);
      details.addEventListener("toggle", function () {
        setStoredState(details);
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializeCollapsibleState);
  } else {
    initializeCollapsibleState();
  }
})();

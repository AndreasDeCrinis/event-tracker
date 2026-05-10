(function () {
  "use strict";

  function initializeTodoToggles() {
    document.querySelectorAll("[data-todo-toggle]").forEach(function (checkbox) {
      checkbox.addEventListener("change", function () {
        checkbox.form.submit();
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializeTodoToggles);
  } else {
    initializeTodoToggles();
  }
})();

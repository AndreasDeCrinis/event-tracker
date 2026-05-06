(function () {
  "use strict";

  function readTemplateOptions(form) {
    var dataElement = form.querySelector("[data-event-template-options]");
    if (!dataElement) {
      return {};
    }

    try {
      return JSON.parse(dataElement.textContent).reduce(function (templates, template) {
        templates[String(template.id)] = template;
        return templates;
      }, {});
    } catch (error) {
      return {};
    }
  }

  function field(form, name) {
    return form.querySelector('[data-event-template-field="' + name + '"]');
  }

  function addDays(dateValue, days) {
    var parts = dateValue.split("-").map(function (part) {
      return parseInt(part, 10);
    });

    if (parts.length !== 3 || parts.some(isNaN)) {
      return "";
    }

    var date = new Date(Date.UTC(parts[0], parts[1] - 1, parts[2]));
    date.setUTCDate(date.getUTCDate() + days);
    return [
      date.getUTCFullYear(),
      String(date.getUTCMonth() + 1).padStart(2, "0"),
      String(date.getUTCDate()).padStart(2, "0"),
    ].join("-");
  }

  function applyTemplate(form, template) {
    var startsOn = field(form, "starts_on");
    var endsOn = field(form, "ends_on");
    var syncToGoogleCalendar = field(form, "sync_to_google_calendar");

    field(form, "name").value = template.eventName;
    field(form, "starts_at_time").value = template.startsAtTime;
    field(form, "ends_at_time").value = template.endsAtTime;
    field(form, "location").value = template.location;
    field(form, "booking_status").value = template.bookingStatus;
    field(form, "notes").value = template.notes;
    syncToGoogleCalendar.checked = template.syncToGoogleCalendar;

    if (startsOn.value) {
      endsOn.value = addDays(startsOn.value, template.durationDays - 1);
    }
  }

  function initializeTemplateLoader(form) {
    var select = form.querySelector("[data-event-template-select]");
    if (!select) {
      return;
    }

    var templates = readTemplateOptions(form);
    var startsOn = field(form, "starts_on");

    function loadSelectedTemplate() {
      var template = templates[select.value];
      if (template) {
        applyTemplate(form, template);
      }
    }

    select.addEventListener("change", loadSelectedTemplate);
    startsOn.addEventListener("change", loadSelectedTemplate);
  }

  function initializeTemplateLoaders() {
    document.querySelectorAll("[data-event-template-form]").forEach(initializeTemplateLoader);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializeTemplateLoaders);
  } else {
    initializeTemplateLoaders();
  }
})();

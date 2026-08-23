(function () {
  "use strict";

  let lastDialogTrigger = null;

  const associateFieldMessages = (root = document) => {
    window.KhanMandiUI?.associateFieldMessages(root);
  };

  const initialiseItemForm = (root = document) => {
    root.querySelectorAll("[data-inventory-item-form]").forEach((form) => {
      if (form.dataset.itemFormReady === "true") return;
      form.dataset.itemFormReady = "true";
      const expiry = form.querySelector("#id_tracks_expiry");
      const lots = form.querySelector("#id_tracks_lots");
      const shelf = form.querySelector("[data-shelf-life]");
      const syncTrackingFields = () => {
        if (expiry?.checked && lots) lots.checked = true;
        shelf?.classList.toggle("is-hidden", !expiry?.checked);
      };
      expiry?.addEventListener("change", syncTrackingFields);
      syncTrackingFields();
    });
  };

  const initialiseResponsiveTables = (root = document) => {
    const tables = [];
    if (root.matches?.("table.responsive-table")) tables.push(root);
    root.querySelectorAll?.("table.responsive-table").forEach((table) => tables.push(table));
    tables.forEach((table) => {
      const headings = [...table.querySelectorAll("thead th")];
      const labels = headings.map((heading, index) => {
        const label = heading.textContent.replace(/\s+/g, " ").trim();
        if (label) return label;
        return index === headings.length - 1 ? "إجراءات" : "";
      });
      table.querySelectorAll("tbody tr").forEach((row) => {
        const cells = [...row.children].filter((cell) => cell.matches("td"));
        if (cells.length === 1 && cells[0].hasAttribute("colspan")) {
          cells[0].dataset.emptyState = "";
          return;
        }
        cells.forEach((cell, index) => {
          if (!cell.hasAttribute("data-label") && labels[index]) {
            cell.dataset.label = labels[index];
          }
        });
      });
    });
  };

  // A region that scrolls sideways has to be reachable by keyboard, or its
  // hidden columns belong to mouse users only (WCAG 2.1.1). Only shells that
  // actually overflow become tab stops: making every table a stop would put a
  // dozen dead stops on a page whose tables all fit.
  const markScrollableTables = (root = document) => {
    const shells = [];
    if (root.matches?.(".data-table-shell")) shells.push(root);
    root.querySelectorAll?.(".data-table-shell").forEach((shell) => shells.push(shell));
    shells.forEach((shell) => {
      const overflows = shell.scrollWidth > shell.clientWidth + 1;
      if (!overflows || shell.hasAttribute("tabindex")) return;
      shell.setAttribute("tabindex", "0");
      if (!shell.hasAttribute("role")) shell.setAttribute("role", "region");
      if (!shell.hasAttribute("aria-label")) {
        const heading = shell.closest("section, div")?.querySelector("h2, h3");
        const caption = heading?.textContent.replace(/\s+/g, " ").trim();
        if (caption) shell.setAttribute("aria-label", caption);
      }
    });
  };

  const showToast = (message, variant = "success") => {
    let stack = document.querySelector(".toast-stack");
    if (!stack) {
      stack = document.createElement("div");
      stack.className = "toast-stack";
      stack.setAttribute("aria-live", "polite");
      stack.setAttribute("aria-atomic", "false");
      document.querySelector("#main-content")?.prepend(stack);
    }
    const toast = document.createElement("div");
    toast.className = `toast toast--${variant}`;
    toast.dataset.toast = "";
    toast.setAttribute("role", variant === "danger" ? "alert" : "status");
    const mark = document.createElement("span");
    mark.className = "toast__mark";
    mark.setAttribute("aria-hidden", "true");
    const copy = document.createElement("span");
    copy.textContent = message;
    const close = document.createElement("button");
    close.className = "toast__close";
    close.type = "button";
    close.dataset.toastDismiss = "";
    close.setAttribute("aria-label", "إغلاق الرسالة");
    close.textContent = "×";
    toast.append(mark, copy, close);
    stack.append(toast);
  };

  const setResultsBusy = (target, busy) => {
    const results = target?.closest?.("#list-results") ||
      (target?.id === "list-results" ? target : null);
    if (!results) return;
    results.classList.toggle("is-loading", busy);
    results.setAttribute("aria-busy", String(busy));
    if (!busy) results.removeAttribute("aria-busy");
  };

  const showListError = () => {
    const feedback = document.querySelector("#list-feedback");
    if (!feedback) return;
    feedback.textContent = "تعذّر تحديث النتائج. تحقق من الاتصال ثم أعد المحاولة.";
    feedback.hidden = false;
    feedback.focus?.();
  };

  document.addEventListener("htmx:configRequest", (event) => {
    const form = event.detail.elt?.closest?.("[data-prune-empty-params]");
    if (!form || !event.detail.parameters) return;
    Object.keys(event.detail.parameters).forEach((name) => {
      const value = event.detail.parameters[name];
      if (typeof value === "string" && value.trim() === "") {
        delete event.detail.parameters[name];
      }
    });
  });

  document.addEventListener("htmx:beforeRequest", (event) => {
    setResultsBusy(event.detail.target, true);
    const feedback = document.querySelector("#list-feedback");
    if (feedback) feedback.hidden = true;
  });

  document.addEventListener("htmx:afterSwap", (event) => {
    const target = event.detail.target;
    setResultsBusy(target, false);
    associateFieldMessages(target || document);
    initialiseItemForm(target || document);
    initialiseResponsiveTables(target || document);
    markScrollableTables(target || document);

    if (target?.matches?.("dialog")) {
      if (!target.open) target.showModal();
      window.requestAnimationFrame(() => {
        (target.querySelector("[data-error-summary]") ||
          target.querySelector("[data-inventory-autofocus], input:not([type='hidden'])"))?.focus();
      });
      return;
    }

    if (target?.id === "list-results") {
      const trigger = event.detail.requestConfig?.elt;
      if (trigger?.closest?.(".pagination")) {
        window.requestAnimationFrame(() => target.querySelector(".data-table-shell")?.focus());
      }
    }
    target?.querySelector?.("[data-inventory-autofocus]")?.focus();
  });

  document.addEventListener("htmx:responseError", (event) => {
    setResultsBusy(event.detail.target, false);
    if (event.detail.target?.closest?.("#list-results") || event.detail.target?.id === "list-results") {
      showListError();
    } else {
      showToast("تعذّر إكمال الطلب. تحقق من الاتصال ثم أعد المحاولة.", "danger");
    }
  });

  document.addEventListener("htmx:sendError", (event) => {
    setResultsBusy(event.detail.target, false);
    if (event.detail.target?.closest?.("#list-results") || event.detail.target?.id === "list-results") {
      showListError();
    } else {
      showToast("تعذّر الاتصال بالخادم. أعد المحاولة بعد قليل.", "danger");
    }
  });

  document.addEventListener("click", (event) => {
    const opener = event.target.closest("[hx-target='#package-unit-dialog']");
    if (opener) lastDialogTrigger = opener;

    const close = event.target.closest("[data-close-dialog]");
    if (close) close.closest("dialog")?.close();

  });

  document.addEventListener("close", (event) => {
    if (event.target.matches?.("#package-unit-dialog")) lastDialogTrigger?.focus();
  }, true);

  document.addEventListener("packageUnitCreated", () => {
    document.querySelector("#package-unit-dialog")?.close();
    showToast("تمت إضافة وحدة التعبئة بنجاح.");
  });

  document.addEventListener("submit", (event) => {
    const form = event.target.closest(
      "[data-inventory-form], body[data-module='inventory'] form[method='post']",
    );
    if (
      event.defaultPrevented ||
      !form ||
      (!form.noValidate && !form.checkValidity()) ||
      form.dataset.submitting === "true"
    ) {
      if (form?.dataset.submitting === "true") event.preventDefault();
      return;
    }
    form.dataset.submitting = "true";
    form.classList.add("is-submitting");
    const submit = form.querySelector("[data-inventory-submit]");
    if (submit) {
      submit.disabled = true;
      submit.setAttribute("aria-disabled", "true");
    }
  });

  const markFormDirty = (event) => {
    const form = event.target.closest?.("[data-inventory-form]");
    if (form && form.dataset.submitting !== "true") form.dataset.dirty = "true";
  };
  document.addEventListener("input", markFormDirty);
  document.addEventListener("change", markFormDirty);
  window.addEventListener("beforeunload", (event) => {
    const dirtyForm = document.querySelector(
      "[data-inventory-form][data-dirty='true']:not([data-submitting='true'])",
    );
    if (!dirtyForm) return;
    event.preventDefault();
    event.returnValue = "";
  });

  document.addEventListener("htmx:afterRequest", (event) => {
    if (event.detail.successful) return;
    const form = event.detail.elt?.closest?.(
      "[data-inventory-form], body[data-module='inventory'] form[method='post']",
    );
    if (!form) return;
    form.dataset.submitting = "false";
    form.classList.remove("is-submitting");
    const submit = form.querySelector("[data-inventory-submit]");
    if (submit) {
      submit.disabled = false;
      submit.removeAttribute("aria-disabled");
    }
  });

  associateFieldMessages();
  initialiseItemForm();
  initialiseResponsiveTables();
  markScrollableTables();
  // A table that fits at this width may not fit at the next one.
  window.addEventListener("resize", () => markScrollableTables(), { passive: true });
})();

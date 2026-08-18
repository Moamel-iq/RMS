(function () {
  "use strict";

  const body = document.body;
  const desktopQuery = window.matchMedia("(min-width: 1101px)");
  const nav = document.querySelector("[data-shell-nav]");
  const subnav = nav?.querySelector(".subnav");
  const navToggles = [...document.querySelectorAll("[data-nav-toggle]")];
  const topbarNavToggle = document.querySelector(".topbar__nav-toggle[data-nav-toggle]");
  const navClose = document.querySelector("[data-nav-close]");
  const collapseKey = "khan-mandi:navigation-collapsed";
  let lastNavTrigger = null;

  const readCollapsePreference = () => {
    try {
      return window.localStorage.getItem(collapseKey) === "true";
    } catch (_error) {
      return false;
    }
  };

  const writeCollapsePreference = (collapsed) => {
    try {
      window.localStorage.setItem(collapseKey, String(collapsed));
    } catch (_error) {
      // Storage can be unavailable in privacy modes; the shell still works.
    }
  };

  const syncNavigation = () => {
    if (!nav) return;
    if (desktopQuery.matches) {
      const collapsed = readCollapsePreference();
      body.classList.toggle("nav-collapsed", collapsed);
      document.documentElement.classList.remove("nav-collapsed-pref");
      body.classList.remove("nav-open");
      nav.removeAttribute("aria-hidden");
      nav.inert = false;
      if (collapsed) subnav?.setAttribute("aria-hidden", "true");
      else subnav?.removeAttribute("aria-hidden");
      if (subnav) subnav.inert = collapsed;
      navToggles.forEach((button) => button.setAttribute("aria-expanded", String(!collapsed)));
    } else {
      body.classList.remove("nav-collapsed");
      const open = body.classList.contains("nav-open");
      nav.setAttribute("aria-hidden", String(!open));
      nav.inert = !open;
      subnav?.removeAttribute("aria-hidden");
      if (subnav) subnav.inert = false;
      navToggles.forEach((button) => button.setAttribute("aria-expanded", String(open)));
    }
  };

  const closeMobileNavigation = () => {
    if (desktopQuery.matches || !body.classList.contains("nav-open")) return;
    body.classList.remove("nav-open");
    syncNavigation();
    lastNavTrigger?.focus();
  };

  navToggles.forEach((button) => {
    button.addEventListener("click", () => {
      if (desktopQuery.matches) {
        const collapsed = !body.classList.contains("nav-collapsed");
        body.classList.toggle("nav-collapsed", collapsed);
        writeCollapsePreference(collapsed);
        if (collapsed && subnav?.contains(button)) {
          window.requestAnimationFrame(() => topbarNavToggle?.focus());
        }
      } else if (body.classList.contains("nav-open")) {
        closeMobileNavigation();
        return;
      } else {
        lastNavTrigger = button;
        body.classList.add("nav-open");
      }
      syncNavigation();
      if (!desktopQuery.matches && body.classList.contains("nav-open")) {
        nav?.querySelector("a:not([aria-disabled='true'])")?.focus();
      }
    });
  });
  navClose?.addEventListener("click", closeMobileNavigation);
  desktopQuery.addEventListener("change", syncNavigation);
  syncNavigation();

  const commandDialog = document.querySelector("[data-command-dialog]");
  const commandInput = commandDialog?.querySelector("[data-command-input]");
  const commandItems = commandDialog
    ? [...commandDialog.querySelectorAll("[data-command-item]")]
    : [];
  const commandEmpty = commandDialog?.querySelector("[data-command-empty]");
  let lastCommandTrigger = null;

  const filterCommands = () => {
    const term = commandInput?.value.trim().toLocaleLowerCase("ar") || "";
    let visible = 0;
    commandItems.forEach((item) => {
      const matches = !term || item.textContent.toLocaleLowerCase("ar").includes(term);
      item.hidden = !matches;
      if (matches) visible += 1;
    });
    if (commandEmpty) commandEmpty.hidden = visible !== 0;
  };

  const visibleCommands = () => commandItems.filter((item) => !item.hidden);

  const openCommand = (trigger) => {
    if (!commandDialog) return;
    if (commandDialog.open) {
      commandInput?.focus();
      return;
    }
    lastCommandTrigger = trigger || document.activeElement;
    commandDialog.showModal();
    commandInput.value = "";
    filterCommands();
    window.requestAnimationFrame(() => commandInput.focus());
  };

  document.querySelectorAll("[data-command-open]").forEach((button) => {
    button.addEventListener("click", () => openCommand(button));
  });
  commandDialog?.querySelector("[data-command-close]")?.addEventListener("click", () => {
    commandDialog.close();
  });
  commandDialog?.addEventListener("close", () => lastCommandTrigger?.focus());
  commandInput?.addEventListener("input", filterCommands);
  commandInput?.addEventListener("keydown", (event) => {
    if (!["ArrowDown", "ArrowUp", "Enter"].includes(event.key)) return;
    const items = visibleCommands();
    if (!items.length) return;
    event.preventDefault();
    if (event.key === "Enter") items[0].click();
    else (event.key === "ArrowDown" ? items[0] : items[items.length - 1]).focus();
  });

  commandItems.forEach((item) => {
    item.addEventListener("keydown", (event) => {
      if (!["ArrowDown", "ArrowUp"].includes(event.key)) return;
      const items = visibleCommands();
      const current = items.indexOf(item);
      if (current < 0) return;
      event.preventDefault();
      const offset = event.key === "ArrowDown" ? 1 : -1;
      items[(current + offset + items.length) % items.length].focus();
    });
  });

  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLocaleLowerCase() === "k") {
      event.preventDefault();
      openCommand(document.activeElement);
    }
    if (event.key === "Escape") closeMobileNavigation();
    if (event.key === "Tab" && body.classList.contains("nav-open") && !desktopQuery.matches && nav) {
      const focusable = [...nav.querySelectorAll("a[href], button:not([disabled]), summary")]
        .filter((item) => !item.closest("[hidden]") && item.getClientRects().length);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  });

  document.addEventListener("click", (event) => {
    const dismiss = event.target.closest("[data-toast-dismiss]");
    if (dismiss) dismiss.closest("[data-toast]")?.remove();

    const reset = event.target.closest("[data-reset-filters]");
    if (!reset) return;
    const form = document.querySelector("form.toolbar[method='get']");
    if (!form) return;
    event.preventDefault();
    form.querySelectorAll("input:not([type='submit']), select").forEach((control) => {
      if (control.type === "checkbox" || control.type === "radio") control.checked = false;
      else control.value = "";
    });
    form.requestSubmit();
  });

  const confirmDialog = document.querySelector("[data-confirm-dialog]");
  const confirmTitle = confirmDialog?.querySelector("[data-confirm-title]");
  const confirmMessage = confirmDialog?.querySelector("[data-confirm-message]");
  const confirmButton = confirmDialog?.querySelector("[data-confirm-accept]");
  let pendingForm = null;
  let pendingSubmitter = null;

  document.addEventListener("submit", (event) => {
    const form = event.target.closest("form[data-confirm]");
    if (!form || form.dataset.confirmed === "true" || !confirmDialog) return;
    if (!form.checkValidity()) return;
    event.preventDefault();
    pendingForm = form;
    pendingSubmitter = event.submitter || null;
    confirmTitle.textContent = form.dataset.confirmTitle || "تأكيد الإجراء";
    confirmMessage.textContent = form.dataset.confirm || "هل تريد متابعة هذا الإجراء؟";
    if (!confirmDialog.open) confirmDialog.showModal();
    window.requestAnimationFrame(() => confirmButton.focus());
  });

  confirmButton?.addEventListener("click", () => {
    if (!pendingForm) return;
    const form = pendingForm;
    const submitter = pendingSubmitter;
    pendingForm = null;
    pendingSubmitter = null;
    confirmDialog.close();
    form.dataset.confirmed = "true";
    if (submitter) form.requestSubmit(submitter);
    else form.requestSubmit();
    delete form.dataset.confirmed;
  });
  confirmDialog?.querySelector("[data-confirm-cancel]")?.addEventListener("click", () => {
    pendingForm = null;
    pendingSubmitter = null;
    confirmDialog.close();
  });
  confirmDialog?.addEventListener("cancel", () => {
    pendingForm = null;
    pendingSubmitter = null;
  });

  const associateFieldMessages = (root = document) => {
    root.querySelectorAll("[data-field]").forEach((wrapper) => {
      const control = wrapper.querySelector("input, select, textarea");
      if (!control) return;
      const descriptions = [...wrapper.querySelectorAll("[data-field-description]")]
        .map((node) => node.id)
        .filter(Boolean);
      if (descriptions.length) control.setAttribute("aria-describedby", descriptions.join(" "));
      else control.removeAttribute("aria-describedby");
      if (wrapper.querySelector("[data-field-error]")) control.setAttribute("aria-invalid", "true");
      else control.removeAttribute("aria-invalid");
    });
  };
  window.KhanMandiUI = Object.assign(window.KhanMandiUI || {}, { associateFieldMessages });
  associateFieldMessages();

  const errorSummary = document.querySelector("[data-error-summary]");
  if (errorSummary) window.requestAnimationFrame(() => errorSummary.focus());

  body.classList.add("is-enhanced");
})();

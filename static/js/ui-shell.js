(function () {
  "use strict";

  const body = document.body;
  const desktopQuery = window.matchMedia("(min-width: 1101px)");
  const navigation = document.querySelector("[data-shell-nav]");
  const secondaryNavigation = navigation?.querySelector("[data-secondary-nav]");
  const navigationToggles = [...document.querySelectorAll("[data-nav-toggle]")];
  const topNavigationToggle = document.querySelector(".ui-header-tools__nav-toggle[data-nav-toggle]");
  const navigationBackdrop = document.querySelector("[data-nav-close]");
  const collapseKey = "khan-mandi:navigation-collapsed";
  let lastNavigationTrigger = null;

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
      /* The drawer remains usable when storage is unavailable. */
    }
  };

  const syncNavigation = () => {
    if (!navigation) return;
    if (desktopQuery.matches) {
      const collapsed = readCollapsePreference();
      body.classList.toggle("nav-collapsed", collapsed);
      document.documentElement.classList.remove("nav-collapsed-pref");
      body.classList.remove("nav-open");
      navigation.removeAttribute("aria-hidden");
      navigation.inert = collapsed;
      navigationToggles.forEach((button) => button.setAttribute("aria-expanded", String(!collapsed)));
      return;
    }

    body.classList.remove("nav-collapsed");
    const open = body.classList.contains("nav-open");
    navigation.setAttribute("aria-hidden", String(!open));
    navigation.inert = !open;
    navigationToggles.forEach((button) => button.setAttribute("aria-expanded", String(open)));
  };

  const closeMobileNavigation = ({ restoreFocus = true } = {}) => {
    if (desktopQuery.matches || !body.classList.contains("nav-open")) return;
    body.classList.remove("nav-open");
    syncNavigation();
    if (restoreFocus) lastNavigationTrigger?.focus();
  };

  navigationToggles.forEach((button) => {
    button.addEventListener("click", () => {
      if (desktopQuery.matches) {
        const collapsed = !body.classList.contains("nav-collapsed");
        body.classList.toggle("nav-collapsed", collapsed);
        writeCollapsePreference(collapsed);
        syncNavigation();
        if (collapsed && secondaryNavigation?.contains(button)) {
          window.requestAnimationFrame(() => topNavigationToggle?.focus());
        }
        return;
      }

      if (body.classList.contains("nav-open")) {
        closeMobileNavigation();
        return;
      }
      lastNavigationTrigger = button;
      body.classList.add("nav-open");
      syncNavigation();
      window.requestAnimationFrame(() => {
        navigation.querySelector("a[aria-current='page'], a[href]")?.focus();
      });
    });
  });

  navigationBackdrop?.addEventListener("click", () => closeMobileNavigation());
  desktopQuery.addEventListener("change", syncNavigation);
  syncNavigation();

  const commandDialog = document.querySelector("[data-command-dialog]");
  const commandInput = commandDialog?.querySelector("[data-command-input]");
  const commandItems = commandDialog ? [...commandDialog.querySelectorAll("[data-command-item]")] : [];
  const commandEmpty = commandDialog?.querySelector("[data-command-empty]");
  let lastCommandTrigger = null;

  const textTools = () => window.KhanMandiText;
  const foldTerm = (value) => textTools()?.foldQuery?.(value) ?? value.toLocaleLowerCase("ar");
  const foldLabel = (value) => textTools()?.fold?.(value) ?? value.toLocaleLowerCase("ar");
  const termMatches = (haystack, needle) => textTools()?.matches?.(haystack, needle) ?? haystack.includes(needle);

  const visibleCommands = () => commandItems.filter((item) => !item.hidden);
  const filterCommands = () => {
    const term = foldTerm(commandInput?.value.trim() || "");
    let visible = 0;
    commandItems.forEach((item) => {
      const label = item.dataset.commandText || foldLabel(item.textContent);
      item.dataset.commandText = label;
      const matches = !term || termMatches(label, term);
      item.hidden = !matches;
      if (matches) visible += 1;
    });
    if (commandEmpty) commandEmpty.hidden = visible !== 0;
  };

  const openCommand = (trigger) => {
    if (!commandDialog) return;
    if (commandDialog.open) {
      commandInput?.focus();
      return;
    }
    lastCommandTrigger = trigger || document.activeElement;
    commandDialog.showModal();
    if (commandInput) commandInput.value = "";
    filterCommands();
    window.requestAnimationFrame(() => commandInput?.focus());
  };

  document.querySelectorAll("[data-command-open]").forEach((button) => {
    button.addEventListener("click", () => openCommand(button));
  });
  commandDialog?.querySelector("[data-command-close]")?.addEventListener("click", () => commandDialog.close());
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

  const showToast = (message, tone = "danger") => {
    let region = document.querySelector(".ui-toast-region");
    if (!region) {
      region = document.createElement("div");
      region.className = "ui-toast-region";
      region.setAttribute("aria-live", "polite");
      region.setAttribute("aria-atomic", "false");
      document.querySelector("#main-content")?.prepend(region);
    }
    const toast = document.createElement("div");
    toast.className = `ui-toast ui-toast--${tone}`;
    toast.dataset.toast = "";
    toast.setAttribute("role", tone === "danger" ? "alert" : "status");
    const mark = document.createElement("span");
    mark.className = "ui-toast__mark";
    mark.setAttribute("aria-hidden", "true");
    const copy = document.createElement("span");
    copy.className = "ui-toast__copy";
    copy.textContent = message;
    const close = document.createElement("button");
    close.className = "ui-toast__close";
    close.type = "button";
    close.dataset.toastDismiss = "";
    close.setAttribute("aria-label", "إغلاق الرسالة");
    close.textContent = "×";
    toast.append(mark, copy, close);
    region.append(toast);
  };

  const associateFieldMessages = (root = document) => {
    root.querySelectorAll?.("[data-field], .ui-field").forEach((wrapper) => {
      const control = wrapper.querySelector("input, select, textarea");
      if (!control) return;
      const descriptions = [...wrapper.querySelectorAll("[data-field-description], .ui-field__help[id], .ui-field__error[id]")]
        .map((node) => node.id)
        .filter(Boolean);
      if (descriptions.length) control.setAttribute("aria-describedby", descriptions.join(" "));
      else control.removeAttribute("aria-describedby");
      if (wrapper.querySelector("[data-field-error], .ui-field__error")) control.setAttribute("aria-invalid", "true");
      else control.removeAttribute("aria-invalid");
    });
  };
  window.KhanMandiUI = Object.assign(window.KhanMandiUI || {}, { associateFieldMessages, showToast });
  associateFieldMessages();

  document.addEventListener("click", (event) => {
    const dismiss = event.target.closest("[data-toast-dismiss]");
    if (dismiss) dismiss.closest("[data-toast], .ui-toast")?.remove();

    const noticeDismiss = event.target.closest("[data-notice-dismiss]");
    if (noticeDismiss) noticeDismiss.closest("[data-dismissible-notice]")?.remove();

    const modalClose = event.target.closest("[data-modal-close], [data-close-dialog]");
    if (modalClose) modalClose.closest("dialog")?.close();

    const reset = event.target.closest("[data-reset-filters]");
    if (reset) {
      const form = reset.closest("form") || document.querySelector("form[method='get']");
      if (form) {
        event.preventDefault();
        form.querySelectorAll("input:not([type='submit']), select").forEach((control) => {
          if (control.type === "checkbox" || control.type === "radio") control.checked = false;
          else control.value = "";
        });
        form.requestSubmit();
      }
    }

    if (!event.target.closest(".ui-menu")) {
      document.querySelectorAll(".ui-menu[open]").forEach((menu) => menu.removeAttribute("open"));
    }
  });

  const confirmDialog = document.querySelector("[data-confirm-dialog]");
  const confirmTitle = confirmDialog?.querySelector("[data-confirm-title]");
  const confirmMessage = confirmDialog?.querySelector("[data-confirm-message]");
  const confirmButton = confirmDialog?.querySelector("[data-confirm-accept]");
  let pendingForm = null;
  let pendingSubmitter = null;
  let pendingRequest = null;

  const focusConfirmDefault = () => {
    const danger = confirmDialog?.dataset.severity !== "primary";
    (danger ? confirmDialog?.querySelector("[data-confirm-cancel]") : confirmButton)?.focus();
  };

  const dressConfirm = (source, message) => {
    if (!confirmDialog || !confirmButton || !confirmMessage || !confirmTitle) return;
    const data = source.dataset;
    confirmTitle.textContent = data.confirmTitle || "تأكيد الإجراء";
    confirmMessage.textContent = message || data.confirm || "هل تريد متابعة هذا الإجراء؟";
    let shown = 0;
    ["subject", "amount", "period"].forEach((name) => {
      const row = confirmDialog.querySelector(`[data-confirm-${name}-row]`);
      const cell = confirmDialog.querySelector(`[data-confirm-${name}]`);
      const value = data[`confirm${name[0].toUpperCase()}${name.slice(1)}`];
      if (!row || !cell) return;
      row.hidden = !value;
      cell.textContent = value || "";
      if (value) shown += 1;
    });
    const facts = confirmDialog.querySelector("[data-confirm-facts]");
    if (facts) facts.hidden = shown === 0;
    const reason = confirmDialog.querySelector("[data-confirm-reason-note]");
    if (reason) reason.hidden = data.confirmReason !== "required";
    const severity = data.confirmSeverity || "danger";
    confirmButton.className = `ui-button ui-button--${severity === "danger" ? "danger" : "primary"}`;
    confirmButton.textContent = data.confirmAccept || "متابعة";
    confirmDialog.dataset.severity = severity;
  };

  document.addEventListener("submit", (event) => {
    const form = event.target.closest("form[data-confirm]");
    if (!form || form.dataset.confirmed === "true" || !confirmDialog || !form.checkValidity()) return;
    event.preventDefault();
    pendingForm = form;
    pendingSubmitter = event.submitter || null;
    dressConfirm(form, form.dataset.confirm);
    if (!confirmDialog.open) confirmDialog.showModal();
    window.requestAnimationFrame(focusConfirmDefault);
  });

  document.addEventListener("htmx:confirm", (event) => {
    if (!event.detail.question || !confirmDialog) return;
    event.preventDefault();
    dressConfirm(event.detail.elt, event.detail.question);
    pendingForm = null;
    pendingSubmitter = null;
    pendingRequest = () => event.detail.issueRequest(true);
    if (!confirmDialog.open) confirmDialog.showModal();
    window.requestAnimationFrame(focusConfirmDefault);
  });

  confirmButton?.addEventListener("click", () => {
    if (pendingRequest) {
      const issue = pendingRequest;
      pendingRequest = null;
      confirmDialog.close();
      issue();
      return;
    }
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

  const clearConfirmation = () => {
    pendingForm = null;
    pendingSubmitter = null;
    pendingRequest = null;
  };
  confirmDialog?.querySelector("[data-confirm-cancel]")?.addEventListener("click", () => {
    clearConfirmation();
    confirmDialog.close();
  });
  confirmDialog?.addEventListener("cancel", clearConfirmation);

  document.addEventListener("htmx:beforeRequest", (event) => {
    if (event.detail.elt?.closest?.("[data-command-dialog]") && commandDialog?.open) {
      commandDialog.close();
    }
    const link = event.detail.elt?.closest?.(".ui-secondary-nav__item[hx-get]");
    if (!link) return;
    document.querySelectorAll(".ui-secondary-nav__item.is-active").forEach((item) => {
      item.classList.remove("is-active");
      item.removeAttribute("aria-current");
    });
    link.classList.add("is-active");
    link.setAttribute("aria-current", "page");
  });

  document.addEventListener("htmx:afterSwap", (event) => {
    const target = event.detail.target;
    associateFieldMessages(target || document);
    if (target?.matches?.("dialog")) {
      if (!target.open) target.showModal();
      window.requestAnimationFrame(() => target.querySelector("[autofocus], input:not([type='hidden']), button")?.focus());
    }
    if (target?.id === "main-content") {
      closeMobileNavigation({ restoreFocus: false });
      const heading = target.querySelector("h1");
      if (heading?.textContent.trim()) document.title = `${heading.textContent.trim()} · نظام خان مندي`;
      window.requestAnimationFrame(() => target.focus({ preventScroll: true }));
    }
    const errorSummary = target?.querySelector?.("[data-error-summary], .ui-error-summary");
    if (errorSummary) window.requestAnimationFrame(() => errorSummary.focus());
  });

  document.addEventListener("htmx:afterSettle", () => {
    const activeModule = document.querySelector(
      ".ui-primary-nav__item[aria-current='page'][data-module-key]",
    );
    if (activeModule?.dataset.moduleKey) body.dataset.module = activeModule.dataset.moduleKey;
  });

  document.addEventListener("htmx:responseError", () => {
    showToast("تعذّر إكمال الطلب. تحقق من الاتصال ثم أعد المحاولة.", "danger");
  });
  document.addEventListener("htmx:sendError", () => {
    showToast("تعذّر الاتصال بالخادم. أعد المحاولة بعد قليل.", "danger");
  });

  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLocaleLowerCase("ar") === "k") {
      event.preventDefault();
      openCommand(document.activeElement);
    }
    if (event.key === "Escape") {
      closeMobileNavigation();
      document.querySelectorAll(".ui-menu[open]").forEach((menu) => menu.removeAttribute("open"));
    }
    if (event.key === "Tab" && body.classList.contains("nav-open") && !desktopQuery.matches && navigation) {
      const focusable = [...navigation.querySelectorAll("a[href], button:not([disabled]), summary")]
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

  const errorSummary = document.querySelector("[data-error-summary], .ui-error-summary");
  if (errorSummary) window.requestAnimationFrame(() => errorSummary.focus());
  body.classList.add("is-enhanced");
})();

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

  /*
   * Arabic is written more than one way — أ إ آ for ا, ة for ه, ى for ي, both
   * digit sets, tashkeel, and the yeh and kaf of Persian and Kurdish keyboards.
   * `searchable-select.js` already folds all of it for the combobox; the
   * palette borrows that rather than keeping a second, weaker rule. Loading
   * order is not assumed: the fallback is the behaviour this had before.
   */
  const text = () => window.KhanMandiText;
  const foldTerm = (value) => text()?.foldQuery?.(value) ?? value.toLocaleLowerCase("ar");
  const foldLabel = (value) => text()?.fold?.(value) ?? value.toLocaleLowerCase("ar");
  const termMatches = (haystack, needle) =>
    text()?.matches?.(haystack, needle) ?? haystack.includes(needle);

  const filterCommands = () => {
    const term = foldTerm(commandInput?.value.trim() || "");
    let visible = 0;
    commandItems.forEach((item) => {
      // The label and the module it belongs to are both searchable, so
      // "مبيعات يوم" finds the sales-day screen from either word.
      const label = item.dataset.commandText || foldLabel(item.textContent);
      item.dataset.commandText = label;
      const matches = !term || termMatches(label, term);
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
  //: An htmx request held back until the reader answers the dialog.
  let pendingRequest = null;

  document.addEventListener("submit", (event) => {
    const form = event.target.closest("form[data-confirm]");
    if (!form || form.dataset.confirmed === "true" || !confirmDialog) return;
    if (!form.checkValidity()) return;
    event.preventDefault();
    pendingForm = form;
    pendingSubmitter = event.submitter || null;
    dressConfirm(form, form.dataset.confirm);
    if (!confirmDialog.open) confirmDialog.showModal();
    window.requestAnimationFrame(focusConfirmDefault);
  });

  /**
   * Where focus lands when the dialog opens.
   *
   * On a destructive act it is Cancel: a reader who hits Enter out of habit
   * should not post a journal, terminate an employee or delete a line. On an
   * ordinary confirmation it is the action, because that is the answer being
   * asked for. Either way focus is inside the dialog, and both buttons are one
   * Tab apart.
   */
  const focusConfirmDefault = () => {
    const danger = confirmDialog?.dataset.severity !== "primary";
    const target = danger
      ? confirmDialog?.querySelector("[data-confirm-cancel]")
      : confirmButton;
    target?.focus();
  };

  /**
   * Fill the shared dialog from an element's confirmation contract.
   *
   * `hx-confirm` alone produces the browser's own prompt: one line, no
   * severity, no document identity, no amount, and a button that says OK. On an
   * approval, a termination or a posting that is not a confirmation anybody can
   * act on — so every element that carries `hx-confirm` is drawn here instead,
   * with whatever context it declares.
   */
  const dressConfirm = (source, message) => {
    const data = source.dataset;
    confirmTitle.textContent = data.confirmTitle || "تأكيد الإجراء";
    confirmMessage.textContent = message || data.confirm || "هل تريد متابعة هذا الإجراء؟";

    const facts = confirmDialog.querySelector("[data-confirm-facts]");
    const rows = [
      ["subject", data.confirmSubject],
      ["amount", data.confirmAmount],
      ["period", data.confirmPeriod],
    ];
    let shown = 0;
    rows.forEach(([name, value]) => {
      const row = confirmDialog.querySelector(`[data-confirm-${name}-row]`);
      const cell = confirmDialog.querySelector(`[data-confirm-${name}]`);
      if (!row || !cell) return;
      row.hidden = !value;
      cell.textContent = value || "";
      if (value) shown += 1;
    });
    if (facts) facts.hidden = shown === 0;

    const reasonNote = confirmDialog.querySelector("[data-confirm-reason-note]");
    if (reasonNote) reasonNote.hidden = data.confirmReason !== "required";

    // Severity decides the colour of the last button and nothing else: the
    // wording is the real signal, so it is always the verb of the act.
    const severity = data.confirmSeverity || "danger";
    confirmButton.className = severity === "danger" ? "btn btn--danger" : "btn btn--primary";
    confirmButton.textContent = data.confirmAccept || "متابعة";
    confirmDialog.dataset.severity = severity;
  };

  /*
   * htmx asks before it sends. Answering the question here keeps one dialog,
   * one set of words and one keyboard contract for every module — Inventory and
   * Sales already used it; HR and Procurement asked through the browser.
   */
  document.addEventListener("htmx:confirm", (event) => {
    const question = event.detail.question;
    if (!question || !confirmDialog) return;
    event.preventDefault();
    const source = event.detail.elt;
    dressConfirm(source, question);
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
  confirmDialog?.querySelector("[data-confirm-cancel]")?.addEventListener("click", () => {
    pendingForm = null;
    pendingSubmitter = null;
    pendingRequest = null;
    confirmDialog.close();
  });
  confirmDialog?.addEventListener("cancel", () => {
    pendingRequest = null;
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

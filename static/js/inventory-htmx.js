(function () {
  const SCROLLABLE = "جدول عريض، يُمرَّر أفقياً";
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
    if (root.matches?.("table.ui-table--responsive")) tables.push(root);
    root.querySelectorAll?.("table.ui-table--responsive").forEach((table) => tables.push(table));
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
  /**
   * The name of the register a scroll region holds.
   *
   * A region announced as "region" tells the reader nothing about which of the
   * six tables on the page they have landed in, so the name is taken from the
   * table's own caption first, then the heading of the card that contains it.
   */
  const regionName = (shell) => {
    const caption = shell.querySelector("table > caption");
    if (caption) return caption.textContent.replace(/\s+/g, " ").trim();
    const card = shell.closest("section, .ui-form-card, .ui-card, .ui-data-card");
    const heading = card?.querySelector("h1, h2, h3");
    const name = heading?.textContent.replace(/\s+/g, " ").trim();
    if (name) return name;
    // A register that fills its screen has its name in the page heading rather
    // than in a card of its own.
    const page = document.querySelector(".ui-page-header__title, .ui-page-title, main h1");
    return page?.textContent.replace(/\s+/g, " ").trim() || "";
  };

  /**
   * Wrap a table that has no scroll region of its own.
   *
   * Forty-one tables across the templates sit directly in a card. While they
   * fit, nothing is wrong; when one does not — a wide register on a laptop, any
   * register on a phone — the page itself grows sideways and the reader pans
   * the whole screen, headings and navigation included, to read one column.
   * The wrapper keeps that scrolling inside the table where it belongs.
   */
  const containWideTables = (root = document) => {
    const tables = [];
    if (root.matches?.("table.ui-table")) tables.push(root);
    root.querySelectorAll?.("table.ui-table").forEach((table) => tables.push(table));
    tables.forEach((table) => {
      if (table.closest(".ui-table-scroll")) return;
      const shell = document.createElement("div");
      shell.className = "ui-table-scroll";
      shell.dataset.tableShell = "auto";
      table.parentNode.insertBefore(shell, table);
      shell.append(table);
    });
  };

  const markScrollableTables = (root = document) => {
    containWideTables(root);
    const shells = [];
    if (root.matches?.(".ui-table-scroll")) shells.push(root);
    root.querySelectorAll?.(".ui-table-scroll").forEach((shell) => shells.push(shell));
    shells.forEach((shell) => {
      const overflows = shell.scrollWidth > shell.clientWidth + 1;
      if (!overflows) {
        // A region that no longer overflows — the window was widened, or a
        // filter shortened the table — must stop being a dead tab stop.
        if (shell.dataset.scrollRegion === "auto") {
          shell.removeAttribute("tabindex");
          shell.removeAttribute("role");
          shell.removeAttribute("aria-label");
          delete shell.dataset.scrollRegion;
        }
        return;
      }
      if (shell.hasAttribute("tabindex")) return;
      shell.setAttribute("tabindex", "0");
      shell.dataset.scrollRegion = "auto";
      if (!shell.hasAttribute("role")) shell.setAttribute("role", "region");
      if (!shell.hasAttribute("aria-label")) {
        const name = regionName(shell);
        // Named, and said to be scrollable: the reader needs to know the arrow
        // keys will move something before they press one.
        shell.setAttribute("aria-label", name ? `${name} — ${SCROLLABLE}` : SCROLLABLE);
      }
    });
  };

  const showToast = (message, variant = "success") => {
    let stack = document.querySelector(".ui-toast-region");
    if (!stack) {
      stack = document.createElement("div");
      stack.className = "ui-toast-region";
      stack.setAttribute("aria-live", "polite");
      stack.setAttribute("aria-atomic", "false");
      document.querySelector("#main-content")?.prepend(stack);
    }
    const toast = document.createElement("div");
    toast.className = `ui-toast${variant === "success" ? "" : ` ui-toast--${variant}`}`;
    toast.dataset.toast = "";
    toast.setAttribute("role", variant === "danger" ? "alert" : "status");
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
    const swapped = cardFrameOf(event.detail.elt) || cardFrameOf(target);
    if (swapped) {
      swapped.dataset.cardState = "ready";
      stampRefreshed(swapped);
    }
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
      if (trigger?.closest?.(".ui-pagination")) {
        window.requestAnimationFrame(() => target.querySelector(".ui-table-scroll")?.focus());
      }
    }
    const errorSummary = target?.querySelector?.("[data-error-summary]");
    if (errorSummary) {
      window.requestAnimationFrame(() => errorSummary.focus());
    }
    target?.querySelector?.("[data-inventory-autofocus]")?.focus();
  });


  /* ------------------------------------------------------------------
     Lazy cards: loading, failure, retry, and when the figures last arrived.

     A dashboard card fetches itself. When that fetch fails the card kept its
     "جارٍ التحميل…" line for ever while one global toast — the same sentence,
     once per card — piled up in the corner. A manager reading the screen could
     not tell an empty card from a broken one, and nothing on the card offered
     to try again. Each card now answers for itself.
     ------------------------------------------------------------------ */

  const cardFrameOf = (element) =>
    element?.closest?.("[hx-get][hx-trigger~='load'], [data-card-frame]") || null;

  const stampRefreshed = (frame) => {
    const now = new Date();
    frame.dataset.refreshedAt = now.toISOString();
    frame.dataset.refreshedLabel = now.toLocaleTimeString("ar-IQ", { hour: "2-digit", minute: "2-digit" });
  };

  const cardFailure = (frame, reason) => {
    if (!frame) return false;
    const heading = frame.querySelector(".ui-card__header, h2, h3");
    const body = document.createElement("div");
    body.className = "ui-card-error";
    body.setAttribute("role", "status");
    const last = frame.dataset.refreshedLabel
      ? `<p class="ui-card-error__stamp">آخر تحديث ناجح: <bdi dir="ltr">${frame.dataset.refreshedLabel}</bdi></p>`
      : '<p class="ui-card-error__stamp">لم تصل أرقام هذه البطاقة بعد.</p>';
    body.innerHTML =
      `<p class="ui-card-error__reason">${reason}</p>${last}` +
      '<button class="ui-button ui-button--secondary ui-button--small" type="button" data-card-retry>إعادة المحاولة</button>';
    // Keep the card's own heading: the reader must still know which card failed.
    [...frame.children].forEach((child) => {
      if (child !== heading) child.remove();
    });
    frame.append(body);
    frame.dataset.cardState = "failed";
    return true;
  };

  const retryCard = (frame) => {
    const url = frame.getAttribute("hx-get");
    if (!url || !window.htmx) return;
    const swap = frame.getAttribute("hx-swap") || "innerHTML";
    frame.dataset.cardState = "loading";
    frame.querySelector(".ui-card-error")?.replaceWith(
      Object.assign(document.createElement("p"), {
        className: "ui-text-muted",
        textContent: "جارٍ التحميل…",
      }),
    );
    window.htmx.ajax("GET", url, { target: frame, swap });
  };

  document.addEventListener("click", (event) => {
    const retry = event.target.closest("[data-card-retry]");
    if (!retry) return;
    const frame = retry.closest("[hx-get], [data-card-frame]");
    if (frame) retryCard(frame);
  });

  /*
   * A request that never answers is the worst of the three states: the card
   * says "loading" and means "nothing is coming". Fifteen seconds is long
   * enough for a slow report and short enough that nobody waits on a dead
   * request.
   */
  const CARD_TIMEOUT = 15000;
  const pendingCards = new WeakMap();

  document.addEventListener("htmx:beforeRequest", (event) => {
    const frame = cardFrameOf(event.detail.elt);
    if (!frame) return;
    frame.dataset.cardState = "loading";
    window.clearTimeout(pendingCards.get(frame));
    pendingCards.set(
      frame,
      window.setTimeout(() => {
        if (frame.dataset.cardState === "loading") {
          cardFailure(frame, "تأخر تحميل هذه البطاقة أكثر من المعتاد.");
        }
      }, CARD_TIMEOUT),
    );
  });

  document.addEventListener("htmx:responseError", (event) => {
    setResultsBusy(event.detail.target, false);
    const status = event.detail.xhr?.status;
    const frame = cardFrameOf(event.detail.elt) || cardFrameOf(event.detail.target);
    if (frame) {
      // 403 is not a failure to explain away: the card is not this reader's.
      const reason =
        status === 403
          ? "هذه البطاقة خارج صلاحيتك."
          : status === 404
            ? "لم تعد هذه البطاقة موجودة."
            : "تعذّر تحميل هذه البطاقة.";
      cardFailure(frame, reason);
      return;
    }
    if (event.detail.target?.closest?.("#list-results") || event.detail.target?.id === "list-results") {
      showListError();
    } else {
      showToast("تعذّر إكمال الطلب. تحقق من الاتصال ثم أعد المحاولة.", "danger");
    }
  });

  document.addEventListener("htmx:sendError", (event) => {
    setResultsBusy(event.detail.target, false);
    const frame = cardFrameOf(event.detail.elt) || cardFrameOf(event.detail.target);
    if (frame) {
      cardFailure(frame, "تعذّر الاتصال بالخادم أثناء تحميل هذه البطاقة.");
      return;
    }
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

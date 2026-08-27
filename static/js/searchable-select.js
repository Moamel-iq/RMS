/*
 * Searchable select — a combobox drawn over the native <select>.
 *
 * Progressive enhancement, not a replacement: the <select> stays in the form
 * with its name and value, so the server sees exactly what it saw before and a
 * page without JavaScript still works. Only requiredness moves — to the visible
 * text box, where the browser can report it (see `mirrorState`). What changes
 * for the reader is a text box in front of it that filters the options as they
 * type — Arabic-aware (tashkeel, alef forms, ta marbuta, alef maqsura, the
 * Persian/Kurdish yeh and kaf, invisible marks and both digit sets are folded)
 * — with the WAI-ARIA combobox keyboard pattern.
 *
 * A select is enhanced when it offers enough options to be worth searching
 * (see ENHANCE_FROM) or says `data-search="always"`; `data-search="off"`,
 * multiple selects and list boxes are left alone. Swapped fragments (htmx)
 * are enhanced on arrival, and a select whose options are rewritten by script
 * is rebuilt from its new options.
 *
 * The listbox is positioned with `position: fixed` from the input's rectangle
 * so it can escape scrolling cards and tables; those coordinates come from
 * getBoundingClientRect(), which is geometry rather than layout direction, so
 * the same code serves RTL and LTR.
 */
(function () {
  "use strict";

  const ENHANCE_FROM = 6;
  const LIST_MAX_HEIGHT = 18 * 16;
  const arabic = (document.documentElement.lang || "ar").toLowerCase().startsWith("ar");
  const text = arabic
    ? { placeholder: "ابحث أو اختر…", empty: "لا نتائج مطابقة", hint: "اكتب للبحث، والأسهم للتنقل" }
    : { placeholder: "Search or choose…", empty: "No matching options", hint: "Type to search, arrows to move" };

  const DIGITS = { "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4", "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
    "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4", "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9" };

  /** Fold a string so that spelling variants still meet: the search is for a dish, not a keystroke. */
  const fold = (value) =>
    String(value ?? "")
      .normalize("NFKC")
      .replace(/[\u064B-\u0652\u0640\u200B-\u200F\u061C\u2060]/g, "")
      .replace(/[أإآٱ]/g, "ا")
      .replace(/ة/g, "ه")
      .replace(/ى/g, "ي")
      .replace(/[یيے]/g, "ي")
      .replace(/[کڪ]/g, "ك")
      .replace(/ؤ/g, "و")
      .replace(/ئ/g, "ي")
      .replace(/[٠-٩۰-۹]/g, (digit) => DIGITS[digit] || digit)
      .toLowerCase()
      .replace(/\s+/g, " ")
      .trim();

  /**
   * Fold what the reader typed.
   *
   * A zero-width mark inside a *label* is noise and `fold` drops it, leaving the
   * label one continuous string. Typed, the same mark is a word break — the
   * Persian and Kurdish layouts put ZWNJ on Shift+Space — so here it becomes a
   * space and the query splits into tokens, which match the joined label either
   * way round.
   */
  const foldQuery = (value) => fold(String(value ?? "").replace(/[\u200B-\u200D\u2060]/g, " "));

  const matches = (folded, query) => {
    if (!query) return true;
    return query.split(" ").every((token) => folded.includes(token));
  };

  /*
   * Published for the rest of the shell. The command palette searched by
   * `toLocaleLowerCase` alone, so a staff member who typed "المخزون" with a
   * hamza, a ta marbuta or Arabic-Indic digits was told there were no screens
   * by that name. One folding, one behaviour, one place to fix it.
   */
  window.KhanMandiText = Object.assign(window.KhanMandiText || {}, {
    fold,
    foldQuery,
    matches,
  });

  let sequence = 0;

  const readOptions = (select) => {
    const rows = [];
    const walk = (node, group) => {
      for (const child of node.children) {
        if (child.tagName === "OPTGROUP") {
          walk(child, child.label);
        } else if (child.tagName === "OPTION") {
          // A code-like value ("HALL", "APP-BALI") is worth matching; a database
          // id is not — "1" would otherwise match half the list.
          const searchableValue = /^\d+$/.test(child.value) ? "" : child.value;
          rows.push({
            option: child,
            label: child.textContent.trim(),
            group,
            disabled: child.disabled,
            folded: fold(`${child.textContent} ${searchableValue}`),
          });
        }
      }
    };
    walk(select, "");
    return rows;
  };

  /**
   * Which selects already carry a live combobox.
   *
   * A `data-` marker cannot answer that question: htmx's history cache stores
   * `body.innerHTML`, so pressing Back restores the wrapper, the text box and
   * the marker as plain HTML with every listener gone. A marker would make
   * `enhance` skip a dead box over a select that is one pixel wide and
   * untabbable — the page's filters would simply stop working. Membership of
   * this set cannot be serialized, so it is the truth.
   */
  const live = new WeakSet();

  /** Undo restored-but-dead markup so the select can be enhanced afresh. */
  const unwrapStale = (select) => {
    const stale = select.closest(".combo");
    if (!stale) return;
    // The popup sits on <body>, so it is reached through the box that names it.
    const controls = stale.querySelector(".combo__input")?.getAttribute("aria-controls");
    if (controls) document.getElementById(controls)?.remove();
    stale.querySelector(".combo__list")?.remove();
    stale.replaceWith(select);
    select.classList.remove("combo__native");
    select.removeAttribute("aria-hidden");
    select.removeAttribute("tabindex");
    delete select.dataset.comboReady;
  };


  const enhance = (select) => {
    if (live.has(select)) return;
    if (select.multiple || select.size > 1) return;
    const mode = select.dataset.search;
    if (mode === "off") return;
    if (mode !== "always" && select.options.length < ENHANCE_FROM) return;
    unwrapStale(select);
    live.add(select);
    select.dataset.comboReady = "true";

    sequence += 1;
    const baseId = select.id || `combo-${sequence}`;
    const wrapper = document.createElement("div");
    wrapper.className = "combo";
    wrapper.dataset.combo = "";

    const input = document.createElement("input");
    input.type = "text";
    input.className = "combo__input";
    input.id = `${baseId}-combo`;
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-haspopup", "listbox");
    input.setAttribute("aria-controls", `${baseId}-listbox`);
    input.setAttribute("autocomplete", "off");
    input.setAttribute("autocapitalize", "off");
    input.setAttribute("spellcheck", "false");
    input.placeholder = text.placeholder;
    input.title = text.hint;
    // Requiredness moves to the text box, and does not stay on the select.
    // Constraint validation reports on the first invalid control: asked to
    // report on a select that is one pixel wide and transparent, the browser
    // focuses something the reader cannot see and refuses the submit without a
    // message. The Django form still requires the field, and a page without
    // JavaScript keeps the attribute where the template put it.
    const mirrorState = () => {
      input.disabled = select.disabled;
      if (select.required) {
        input.required = true;
        select.required = false;
      }
    };
    mirrorState();
    const described = select.getAttribute("aria-describedby");
    if (described) input.setAttribute("aria-describedby", described);
    if (select.getAttribute("aria-invalid")) input.setAttribute("aria-invalid", select.getAttribute("aria-invalid"));
    // A select named by `aria-label` rather than a <label> would otherwise lose
    // its name to `aria-hidden`, and the box would be announced by its title —
    // "type to search" for every filter on the screen.
    const labelledBy = select.getAttribute("aria-labelledby");
    const labelText = select.getAttribute("aria-label");
    if (labelledBy) input.setAttribute("aria-labelledby", labelledBy);
    else if (labelText) input.setAttribute("aria-label", labelText);

    const list = document.createElement("ul");
    list.className = "combo__list";
    list.id = `${baseId}-listbox`;
    list.setAttribute("role", "listbox");
    list.hidden = true;

    // The label must reach the text box: that is what the reader clicks and what
    // the screen reader names. The native select keeps its id for anything that
    // looks it up, and steps out of the accessibility tree.
    if (select.id) {
      document.querySelectorAll(`label[for="${CSS.escape(select.id)}"]`).forEach((label) => {
        label.htmlFor = input.id;
      });
    }
    select.classList.add("combo__native");
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");

    select.parentNode.insertBefore(wrapper, select);
    wrapper.append(input, select);
    // The popup hangs off <body>, not off the wrapper. Nearly every select in
    // this app sits inside a bare <label>, and a click on an option inside that
    // label is forwarded to the label's control — which would re-open the list
    // the moment it was used. It is `position: fixed` and placed from the box's
    // rectangle, so where it lives in the tree changes nothing on screen.
    document.body.append(list);

    let rows = readOptions(select);
    let visible = [];
    let active = -1;
    let open = false;

    const selectedLabel = () => {
      const option = select.options[select.selectedIndex];
      return option && option.value !== "" ? option.textContent.trim() : "";
    };

    const showSelected = () => {
      input.value = selectedLabel();
    };

    const place = () => {
      const rect = input.getBoundingClientRect();
      // The visual viewport, not the layout one: on a phone the software
      // keyboard covers the bottom of the screen without changing
      // `window.innerHeight`, and the list would open behind it.
      const viewport = window.visualViewport;
      const viewportTop = viewport ? viewport.offsetTop : 0;
      const viewportBottom = viewportTop + (viewport ? viewport.height : window.innerHeight);
      const below = viewportBottom - rect.bottom;
      const above = rect.top - viewportTop;
      const openUp = below < Math.min(LIST_MAX_HEIGHT, 200) && above > below;
      const room = Math.max(120, (openUp ? above : below) - 12);
      list.style.position = "fixed";
      list.style.left = `${rect.left}px`;
      list.style.width = `${rect.width}px`;
      list.style.maxHeight = `${Math.min(LIST_MAX_HEIGHT, room)}px`;
      if (openUp) {
        list.style.top = "auto";
        list.style.bottom = `${window.innerHeight - rect.top + 4}px`;
      } else {
        list.style.bottom = "auto";
        list.style.top = `${rect.bottom + 4}px`;
      }
    };

    const setActive = (index, scroll = true) => {
      if (active >= 0 && visible[active]) visible[active].element.classList.remove("is-active");
      active = index;
      if (active >= 0 && visible[active]) {
        const element = visible[active].element;
        element.classList.add("is-active");
        input.setAttribute("aria-activedescendant", element.id);
        if (scroll) element.scrollIntoView({ block: "nearest" });
      } else {
        input.removeAttribute("aria-activedescendant");
      }
    };

    const render = (query) => {
      const folded = foldQuery(query);
      const fragment = document.createDocumentFragment();
      visible = [];
      let lastGroup = null;
      rows.forEach((row, index) => {
        if (!matches(row.folded, folded)) return;
        if (row.group !== lastGroup && row.group) {
          const heading = document.createElement("li");
          heading.className = "combo__group";
          heading.setAttribute("role", "presentation");
          heading.textContent = row.group;
          fragment.append(heading);
        }
        lastGroup = row.group;
        const item = document.createElement("li");
        item.className = "combo__option";
        item.id = `${baseId}-option-${index}`;
        item.setAttribute("role", "option");
        item.dataset.index = String(index);
        const selected = row.option.selected;
        item.setAttribute("aria-selected", String(selected));
        if (row.disabled) {
          item.setAttribute("aria-disabled", "true");
          item.classList.add("is-disabled");
        }
        if (row.label === "" || row.option.value === "") {
          item.classList.add("combo__option--blank");
          item.textContent = row.label || "—";
        } else {
          item.textContent = row.label;
        }
        fragment.append(item);
        visible.push({ row, element: item });
      });
      if (!visible.length) {
        const empty = document.createElement("li");
        empty.className = "combo__empty";
        empty.setAttribute("role", "presentation");
        empty.textContent = text.empty;
        fragment.append(empty);
      }
      list.replaceChildren(fragment);
      const current = visible.findIndex((entry) => entry.row.option.selected && entry.row.option.value !== "");
      const firstEnabled = visible.findIndex((entry) => !entry.row.disabled);
      setActive(current >= 0 ? current : firstEnabled, false);
    };

    const openList = (query) => {
      if (input.disabled) return;
      render(query);
      if (!open) {
        open = true;
        list.hidden = false;
        input.setAttribute("aria-expanded", "true");
        wrapper.classList.add("is-open");
      }
      place();
      if (active >= 0 && visible[active]) visible[active].element.scrollIntoView({ block: "nearest" });
    };

    const closeList = () => {
      if (!open) return;
      open = false;
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      wrapper.classList.remove("is-open");
      setActive(-1, false);
    };

    const commit = (row) => {
      if (!row || row.disabled) return;
      const changed = select.value !== row.option.value || select.selectedIndex !== row.option.index;
      select.value = row.option.value;
      showSelected();
      closeList();
      if (changed) select.dispatchEvent(new Event("change", { bubbles: true }));
    };

    /**
     * Leaving the box with free text.
     *
     * `acceptUnique` separates the two ways of leaving. Enter and Tab are the
     * reader saying "this one" — a single remaining candidate is taken. A blur
     * or a click elsewhere is not: half-typed text there must never become a
     * different selection, because on a filter form that change reloads the
     * screen for a record nobody chose.
     *
     * Text that still reads as the current selection settles on the current
     * selection, even when another option's label folds to the same string:
     * two branches may both be named الفرع الرئيسي, and opening the list and
     * clicking away must not move the choice from one to the other.
     */
    const settle = ({ acceptUnique = false } = {}) => {
      const typed = foldQuery(input.value);
      if (typed === fold(selectedLabel()) && select.value !== "") {
        showSelected();
        return;
      }
      if (typed === "") {
        // Emptying the box is only a clearing instruction when the reader then
        // commits it; a stray Backspace on the way out of the field must not
        // drop the filter the screen is showing.
        const blank = rows.find((row) => row.option.value === "" && !row.disabled);
        if (acceptUnique && blank && select.value !== "") commit(blank);
        else showSelected();
        return;
      }
      const candidates = rows.filter((row) => !row.disabled && row.option.value !== "" && matches(row.folded, typed));
      const exact =
        candidates.find((row) => row.option.selected && fold(row.label) === typed) ||
        candidates.find((row) => fold(row.label) === typed);
      if (exact) commit(exact);
      else if (acceptUnique && candidates.length === 1) commit(candidates[0]);
      else showSelected();
    };

    input.addEventListener("focus", () => {
      input.select();
    });
    input.addEventListener("click", () => {
      if (!open) openList("");
    });
    input.addEventListener("input", (event) => {
      // Searching is not editing: this box has no name and is never submitted.
      // Left to bubble, every keystroke would mark the surrounding form dirty
      // and the reader would be warned about "unsaved changes" they never made.
      event.stopPropagation();
      openList(input.value);
    });
    // The text box is not a form control; its own `change` must not reach
    // listeners that submit filter forms on change.
    input.addEventListener("change", (event) => event.stopPropagation());
    input.addEventListener("keydown", (event) => {
      switch (event.key) {
        case "ArrowDown":
        case "ArrowUp": {
          event.preventDefault();
          if (!open) {
            openList("");
            return;
          }
          const step = event.key === "ArrowDown" ? 1 : -1;
          let next = active;
          for (let i = 0; i < visible.length; i += 1) {
            next = (next + step + visible.length) % visible.length;
            if (!visible[next].row.disabled) break;
          }
          setActive(next);
          break;
        }
        case "Home":
        case "End":
          if (!open) return;
          event.preventDefault();
          setActive(event.key === "Home" ? 0 : visible.length - 1);
          break;
        case "Enter":
          if (!open) return;
          event.preventDefault();
          if (active >= 0 && visible[active]) commit(visible[active].row);
          else {
            settle({ acceptUnique: true });
            closeList();
          }
          break;
        case "Escape":
          if (!open) return;
          event.preventDefault();
          event.stopPropagation();
          showSelected();
          closeList();
          break;
        case "Tab":
          if (open) {
            settle({ acceptUnique: true });
            closeList();
          }
          break;
        default:
      }
    });
    input.addEventListener("blur", () => {
      // A click on an option blurs the input first; give the option its turn.
      window.setTimeout(() => {
        if (document.activeElement === input) return;
        if (open) settle();
        closeList();
      }, 120);
    });

    list.addEventListener("mousedown", (event) => {
      event.preventDefault();
    });
    list.addEventListener("click", (event) => {
      const item = event.target.closest(".combo__option");
      if (!item) return;
      event.preventDefault();
      event.stopPropagation();
      commit(rows[Number(item.dataset.index)]);
      input.focus();
    });
    list.addEventListener("mousemove", (event) => {
      const item = event.target.closest(".combo__option");
      if (!item) return;
      const index = visible.findIndex((entry) => entry.element === item);
      if (index >= 0 && index !== active) setActive(index, false);
    });

    // Programmatic changes (another script setting the value) keep the box honest.
    select.addEventListener("change", showSelected);
    const observer = new MutationObserver(() => {
      rows = readOptions(select);
      mirrorState();
      showSelected();
      if (open) render(input.value === selectedLabel() ? "" : input.value);
    });
    observer.observe(select, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled", "required"] });

    // Document-level listeners outlive a fragment htmx swaps away; the first
    // event after the wrapper has gone tears them down so a long session of
    // swaps does not accumulate dead handlers.
    const teardown = () => {
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
      window.visualViewport?.removeEventListener("resize", reposition);
      window.visualViewport?.removeEventListener("scroll", reposition);
      document.removeEventListener("pointerdown", outsidePointer);
      observer.disconnect();
      live.delete(select);
      // The popup lives on <body>; nothing else would remove it once the field
      // it belonged to has been swapped away.
      list.remove();
    };
    const reposition = () => {
      if (!wrapper.isConnected) return teardown();
      if (open) place();
      return undefined;
    };
    const outsidePointer = (event) => {
      if (!wrapper.isConnected) return teardown();
      // The popup is no longer inside the wrapper, so it is asked separately.
      if (open && !wrapper.contains(event.target) && !list.contains(event.target)) {
        settle();
        closeList();
      }
      return undefined;
    };
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    window.visualViewport?.addEventListener("resize", reposition);
    window.visualViewport?.addEventListener("scroll", reposition);
    document.addEventListener("pointerdown", outsidePointer);

    showSelected();
  };

  /**
   * A restored page brings back every popup as dead markup on <body>.
   *
   * Only whole-document restores land here, and a whole-document restore has
   * just replaced every live combobox anyway, so clearing the lot and letting
   * enhancement rebuild is both safe and complete.
   */
  const restore = () => {
    document.querySelectorAll("body > .combo__list").forEach((node) => node.remove());
    enhanceWithin(document);
  };

  const enhanceWithin = (root) => {
    const scope = root && root.querySelectorAll ? root : document;
    if (scope !== document && scope.matches && scope.matches("select")) enhance(scope);
    scope.querySelectorAll("select").forEach(enhance);
  };

  // After htmx, never before. htmx binds `hx-trigger="change from:find input"`
  // to the first input it finds inside the form, and it processes the document
  // on DOMContentLoaded — so a combobox injected during script execution (a
  // deferred script runs while the document is still "interactive") would be
  // the input those filters listen to. Both scripts are deferred and htmx is
  // first, so its listener is registered first and runs first.
  if (document.readyState === "complete") {
    enhanceWithin(document);
  } else {
    document.addEventListener("DOMContentLoaded", () => enhanceWithin(document));
  }
  document.addEventListener("htmx:afterSwap", (event) => enhanceWithin(event.detail?.target || event.target));
  document.addEventListener("htmx:oobAfterSwap", (event) => enhanceWithin(event.detail?.target || event.target));
  document.addEventListener("htmx:load", (event) => enhanceWithin(event.detail?.elt || event.target));
  // A page restored from the history cache is markup without listeners.
  document.addEventListener("htmx:historyRestore", restore);
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) restore();
  });
})();

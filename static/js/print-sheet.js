/*
 * The print sheet's toolbar.
 *
 * Every part of the heading is rendered by the server and shown or hidden
 * here, because what belongs on a printed statement depends on where it is
 * going: the owner's file wants the logo and the signature block, a working
 * copy for the accountant wants neither. The choice is remembered per reader,
 * so nobody re-ticks five boxes every time they print.
 *
 * Progressive: with no JavaScript the sheet still prints, with every part
 * showing, from the browser's own print command.
 */
(function () {
  "use strict";

  const STORE_KEY = "khan-mandi:print-parts";
  const toolbar = document.querySelector("[data-print-toolbar]");
  if (!toolbar) return;

  const boxes = [...toolbar.querySelectorAll("[data-print-part]")];

  const read = () => {
    try {
      const raw = window.localStorage.getItem(STORE_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (_error) {
      return null;
    }
  };

  const write = (state) => {
    try {
      window.localStorage.setItem(STORE_KEY, JSON.stringify(state));
    } catch (_error) {
      /* Storage can be unavailable; the sheet still prints as shown. */
    }
  };

  const apply = () => {
    const state = {};
    boxes.forEach((box) => {
      const part = box.getAttribute("data-print-part");
      state[part] = box.checked;
      document.body.classList.toggle(`print-without-${part}`, !box.checked);
    });
    write(state);
  };

  const saved = read();
  if (saved) {
    boxes.forEach((box) => {
      const part = box.getAttribute("data-print-part");
      if (typeof saved[part] === "boolean") box.checked = saved[part];
    });
  }
  apply();

  boxes.forEach((box) => box.addEventListener("change", apply));

  /*
   * Orientation is a per-print decision, not a saved preference: a wide
   * register wants the long side of the paper and the letter beside it does
   * not. A sheet that already declared landscape starts with the box ticked,
   * so the control reports the truth rather than contradicting it.
   *
   * The rule is written into a style element appended last, because `@page`
   * cannot be scoped to a class and the last declaration is the one that wins.
   */
  //: A4 content width in CSS pixels at 96dpi, portrait and landscape, after the
  //: margins the stylesheet declares.
  const PORTRAIT = ((21 - 2.4) / 2.54) * 96;
  const LANDSCAPE = ((29.7 - 2.2) / 2.54) * 96;
  //: 7pt of a 8.5pt table — the smallest type this system will put on paper.
  const MIN_SCALE = 7 / 8.5;

  const pageStyle = (rule) => {
    let style = document.getElementById("print-orientation");
    if (!style) {
      style = document.createElement("style");
      style.id = "print-orientation";
      document.head.append(style);
    }
    style.textContent = rule;
  };

  const LANDSCAPE_RULE = "@page { size: A4 landscape; margin: 1.1cm; }";
  const PORTRAIT_RULE = "@page { size: A4 portrait; margin: 1.4cm 1.2cm; }";

  const orientation = toolbar.querySelector("[data-print-landscape]");
  let readerChoseOrientation = false;
  if (orientation) {
    orientation.addEventListener("change", () => {
      readerChoseOrientation = true;
      pageStyle(orientation.checked ? LANDSCAPE_RULE : PORTRAIT_RULE);
    });
  }

  /**
   * How wide the widest table on this screen wants to be, at printed size.
   *
   * Measured rather than guessed: the same screen carries a four-column
   * statement and a fifteen-column register, and the difference between them
   * is the difference between a readable page and one with its last columns
   * cut off. The measurement runs under `is-print-measuring`, which applies the
   * printed typography, so what is measured is what will be printed.
   */
  const widestTable = () => {
    document.body.classList.add("is-print-measuring");
    let widest = 0;
    document.querySelectorAll("table").forEach((table) => {
      widest = Math.max(widest, table.scrollWidth, table.getBoundingClientRect().width);
    });
    document.body.classList.remove("is-print-measuring");
    return widest;
  };

  /*
   * Before every print — the toolbar button and Ctrl+P alike — the page decides
   * which way up the paper goes and, if even the long side is too narrow,
   * scales the offending table down rather than let the printer cut a column
   * off. A column that silently disappears is the one failure a printed
   * statement must not have; small type is a nuisance, a missing figure is a
   * wrong statement. The reader's own choice always wins.
   */
  window.addEventListener("beforeprint", () => {
    document.querySelectorAll("[data-print-fitted]").forEach((table) => {
      table.style.zoom = "";
      table.removeAttribute("data-print-fitted");
    });
    if (readerChoseOrientation) return;

    const needed = widestTable();
    if (needed <= PORTRAIT + 1) {
      pageStyle(PORTRAIT_RULE);
      return;
    }
    pageStyle(LANDSCAPE_RULE);
    if (orientation) orientation.checked = true;
    if (needed <= LANDSCAPE + 1) return;

    /*
     * Still too wide. Scaling is the last resort and it is bounded: the printed
     * table is 8.5pt, so a free scale factor took a register down to about 5pt
     * — smaller than the footnotes in a contract, and unreadable across a desk.
     * MIN_SCALE holds the floor at 7pt. What does not fit at that size is not
     * squeezed further; it runs on to a continuation page, where the reader can
     * still read it, because a register printed too small to read has lost the
     * columns just as surely as one printed off the edge.
     */
    const scale = Math.max(MIN_SCALE, LANDSCAPE / needed);
    document.querySelectorAll("table").forEach((table) => {
      if (table.scrollWidth > LANDSCAPE) {
        table.style.zoom = String(scale);
        table.setAttribute("data-print-fitted", "");
      }
    });
  });

  toolbar.querySelector("[data-print-now]")?.addEventListener("click", () => {
    window.print();
  });

  // Ctrl+P is a print too, so the heading is corrected on `beforeprint`
  // rather than on the button: a screen that names itself in its own <h1>
  // should say that on paper, not the name of the module it lives in.
  window.addEventListener("beforeprint", () => {
    const slot = document.querySelector("[data-print-title]");
    const heading = document.querySelector(".ui-page-header__title, .ui-page-title, .ui-page h1, main h1");
    if (slot && heading && heading.textContent.trim()) slot.textContent = heading.textContent.trim();
  });

  toolbar.querySelector("[data-print-back]")?.addEventListener("click", () => {
    // The sheet is opened from a screen; going back is the way home. A sheet
    // opened directly from a pasted URL has no history to return to, and the
    // referring screen is the honest fallback.
    if (window.history.length > 1) window.history.back();
    else if (document.referrer) window.location.assign(document.referrer);
  });
})();

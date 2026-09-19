(function () {
  "use strict";

  // The accounting page may be reached through an HTMX swap.  Do not attach a
  // second set of global handlers when the script is evaluated again.
  if (window.__accountTreeHandlersBound) return;
  window.__accountTreeHandlersBound = true;

  const selector = "[data-account-tree-toggle]";

  const buttonFor = (event) => {
    const element = event.detail?.requestConfig?.elt || event.detail?.elt || event.target;
    return element?.closest?.(selector) || null;
  };

  const childRows = (button) => {
    const table = button.closest("table");
    const nodeId = button.dataset.nodeId;
    if (!table || !nodeId) return [];
    return [...table.querySelectorAll(`tr[data-parent="${nodeId}"]`)];
  };

  const setExpanded = (button, expanded) => {
    button.setAttribute("aria-expanded", String(expanded));
    button.textContent = expanded ? "−" : "+";
    button.setAttribute(
      "aria-label",
      expanded ? button.dataset.labelClose : button.dataset.labelOpen,
    );
  };

  const collapseBranch = (button) => {
    const table = button.closest("table");
    const nodeId = button.dataset.nodeId;
    if (!table || !nodeId) return;

    const parents = new Set([nodeId]);
    let removed = true;
    while (removed) {
      removed = false;
      table.querySelectorAll("tr[data-parent]").forEach((row) => {
        if (!parents.has(row.dataset.parent)) return;
        if (row.dataset.nodeId) parents.add(row.dataset.nodeId);
        row.remove();
        removed = true;
      });
    }
    setExpanded(button, false);
  };

  // Capture the click before htmx sees it. A loaded branch closes locally;
  // opening it again requests one fresh fragment. Checking the actual rows as
  // well as aria-expanded recovers safely if an older cached script left the
  // attribute stale, and prevents duplicate rows from ever being appended.
  document.addEventListener(
    "click",
    (event) => {
      const button = event.target.closest?.(selector);
      if (
        !button ||
        (button.getAttribute("aria-expanded") !== "true" && childRows(button).length === 0)
      ) {
        return;
      }
      event.preventDefault();
      event.stopImmediatePropagation();
      collapseBranch(button);
    },
    true,
  );

  document.addEventListener("htmx:beforeRequest", (event) => {
    const button = buttonFor(event);
    if (!button) return;

    // A request may have been queued while the button state was stale.  The
    // branch is already present, so refuse the swap rather than duplicate it.
    if (childRows(button).length > 0) {
      event.preventDefault();
      setExpanded(button, true);
      return;
    }

    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  });

  document.addEventListener("htmx:afterSwap", (event) => {
    const button = buttonFor(event);
    if (button) setExpanded(button, true);
  });

  document.addEventListener("htmx:afterRequest", (event) => {
    const button = buttonFor(event);
    if (!button) return;
    button.disabled = false;
    button.removeAttribute("aria-busy");
    if (childRows(button).length > 0) setExpanded(button, true);
  });
})();

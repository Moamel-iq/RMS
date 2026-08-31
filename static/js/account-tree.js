(function () {
  "use strict";

  const selector = "[data-account-tree-toggle]";

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

  // Capture the click before htmx sees it. An open branch closes locally;
  // opening it again requests one fresh fragment. This guarantees that the
  // same children can never be appended twice beneath a parent.
  document.addEventListener(
    "click",
    (event) => {
      const button = event.target.closest?.(selector);
      if (!button || button.getAttribute("aria-expanded") !== "true") return;
      event.preventDefault();
      event.stopImmediatePropagation();
      collapseBranch(button);
    },
    true,
  );

  document.addEventListener("htmx:beforeRequest", (event) => {
    const button = event.target.closest?.(selector);
    if (!button) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  });

  document.addEventListener("htmx:afterSwap", (event) => {
    const button = event.detail.requestConfig?.elt?.closest?.(selector);
    if (button) setExpanded(button, true);
  });

  document.addEventListener("htmx:afterRequest", (event) => {
    const button = event.target.closest?.(selector);
    if (!button) return;
    button.disabled = false;
    button.removeAttribute("aria-busy");
    const nodeId = button.dataset.nodeId;
    const loaded = [...button.closest("table").querySelectorAll("tr[data-parent]")]
      .some((row) => row.dataset.parent === nodeId);
    if (loaded) setExpanded(button, true);
  });
})();

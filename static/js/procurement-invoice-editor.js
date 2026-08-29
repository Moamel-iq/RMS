(() => {
  const root = document.querySelector('.invoice-editor');
  if (!root) return;
  const rows = root.querySelector('#invoice-line-rows');
  const totalForms = root.querySelector('#id_lines-TOTAL_FORMS');
  const template = root.querySelector('#invoice-line-template');
  const addButton = root.querySelector('#add-invoice-line');
  const units = JSON.parse(root.querySelector('#invoice-item-units')?.textContent || '{}');
  const supplierTerms = JSON.parse(root.querySelector('#invoice-supplier-terms')?.textContent || '{}');
  const number = value => {
    const parsed = Number.parseFloat(String(value || '').replaceAll(',', ''));
    return Number.isFinite(parsed) ? parsed : 0;
  };
  const format = value => new Intl.NumberFormat('en-US', { maximumFractionDigits: 3 }).format(value);

  function updateRow(row) {
    const item = row.querySelector('select[name$="-item"]');
    const quantity = row.querySelector('input[name$="-quantity"]');
    const price = row.querySelector('input[name$="-unit_price"]');
    row.querySelector('[data-line-unit]').textContent = units[item?.value] || '—';
    row.querySelector('[data-line-total]').textContent = format(number(quantity?.value) * number(price?.value));
  }
  function updateTotals() {
    let linesTotal = 0;
    rows?.querySelectorAll('.invoice-line-row').forEach(row => {
      const deleted = row.querySelector('input[name$="-DELETE"]');
      if (deleted?.value === 'on') return;
      updateRow(row);
      linesTotal += number(row.querySelector('[data-line-total]')?.textContent);
    });
    const discount = number(root.querySelector('#id_discount_amount')?.value);
    root.querySelector('#invoice-lines-total').textContent = format(linesTotal);
    root.querySelector('#invoice-discount-total').textContent = format(discount);
    root.querySelector('#invoice-grand-total').textContent = format(Math.max(linesTotal - discount, 0));
  }
  function updateTerms() {
    const supplier = root.querySelector('#id_supplier');
    const date = root.querySelector('#id_invoice_date');
    const days = Number.parseInt(supplierTerms[supplier?.value] ?? '', 10);
    root.querySelector('#invoice-payment-terms').textContent = Number.isFinite(days) ? `${days} يوم` : '—';
    if (!date?.value || !Number.isFinite(days)) {
      root.querySelector('#invoice-due-date').textContent = '—';
      return;
    }
    const due = new Date(`${date.value}T12:00:00`);
    due.setDate(due.getDate() + days);
    root.querySelector('#invoice-due-date').textContent = due.toISOString().slice(0, 10);
  }
  addButton?.addEventListener('click', () => {
    const index = Number.parseInt(totalForms.value, 10);
    rows.insertAdjacentHTML('beforeend', template.innerHTML.replaceAll('__prefix__', String(index)).replaceAll('__number__', String(index + 1)));
    totalForms.value = String(index + 1);
    updateTotals();
    rows.lastElementChild?.querySelector('select')?.focus();
  });
  root.addEventListener('click', event => {
    const button = event.target.closest('[data-remove-line]');
    if (!button) return;
    const row = button.closest('.invoice-line-row');
    const deleted = row.querySelector('input[name$="-DELETE"]');
    deleted.value = 'on';
    row.hidden = true;
    updateTotals();
  });
  root.addEventListener('input', event => {
    if (event.target.matches('input[name$="-quantity"],input[name$="-unit_price"],#id_discount_amount')) updateTotals();
  });
  root.addEventListener('change', event => {
    if (event.target.matches('select[name$="-item"]')) updateTotals();
    if (event.target.matches('#id_supplier,#id_invoice_date')) updateTerms();
  });
  updateTotals();
  updateTerms();
})();

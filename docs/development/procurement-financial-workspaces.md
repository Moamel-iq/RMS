# The three Procurement financial workspaces

فواتير الموردين · التكاليف الإضافية · شروط الائتمان — what each one actually is,
and the two that are not what their labels suggest.

---

## The short version

| Sidebar entry | What backs it | New model? |
|---|---|---|
| فواتير الموردين | `SupplierInvoice` and its existing posting, matching, GRNI, PPV, payment and credit-note services | no |
| التكاليف الإضافية | `SupplierInvoiceLine` rows with `line_type = ACCOUNT` | **no** |
| شروط الائتمان | `Supplier.payment_terms_days`, plus the snapshots on invoices and orders | **no** |

All three entries were showing "قريباً". The invoice domain had existed since
Task 2.12; its navigation comment claimed the backing documents "do not exist",
which had stopped being true and stayed in the file.

---

## فواتير الموردين

The lifecycle is four states and was already implemented:

```
DRAFT → APPROVED → POSTED → REVERSED
```

There is **no `SUBMITTED`**. The approval step *is* the second pair of eyes; a
submit step before it would be a third state carrying no additional authority.

`APPROVED` is also where an invoice waits: a line billing for goods has no
determinate accounting until it is matched against the receipt it covers, so it
approves and then holds. Posted and reversed invoices are read-only, and
correction is reversal plus a replacement invoice — never an edit.

Duplicate protection already exists on the model as
`supplier_invoice_number_key`: the entered number is preserved for display and a
normalised value carries the uniqueness.

### The fragment contract

`supplier_invoice_create` used to answer an HTMX GET with a whole document — two
`<html>` elements, two navigation rails, a page that looks correct until
somebody swaps it into a panel.

The cause was structural. Lists already had the contract:
`settings/base_list.html` extends `list_base_template|default:"shell.html"` and
`InventoryListView` passes a fragment parent when `HX-Request` is present. Write
screens had no equivalent.

So write screens got the same contract rather than a special case:

- `templates/settings/_form_fragment.html` — the mirror of `_list_fragment.html`
- `templates/inventory/master_form.html` extends
  `form_base_template|default:"shell.html"`
- `InventoryWriteView.context()` supplies it from `is_htmx()`
- the supplier-invoice detail template and view do the same, covering that
  view's GET and both of its line-form POSTs
- `is_htmx()` moved from `InventoryListView` up to `InventoryViewMixin`, so a
  list and a form cannot disagree about what an HTMX request is

Every existing caller keeps rendering the shell; the default is unchanged.

---

## التكاليف الإضافية

**An additional cost is a `SupplierInvoiceLine` whose `line_type` is `ACCOUNT`.**
A charge that never entered stock — transport, delivery, handling, a repair, a
subscription — posting:

```
Dr the validated direct account carried by the line
Cr Supplier Payable
```

through the invoice's own posting service.

### Why there is no additional-cost document

Three reasons, and the first is sufficient:

1. **It would post twice.** A cost that is both an invoice line and an
   independent document has two paths to the ledger, and the supplier billed it
   once.
2. **It would need a second source identity, a second journal and a second
   payable.** Each is a place for the supplier balance to disagree with itself.
3. **Correction would fork.** A posted invoice is corrected by reversal and
   replacement; an independent cost document would need its own reversal, and
   the two would eventually be used against the same charge.

So the workspace has **no create, no post and no reverse**. A `DRAFT` line links
back to its invoice to be edited; anything past `DRAFT` is read-only, and the
screen says why. The invoice is the authoritative owner of the lifecycle.

### Landed cost is deferred

Capitalising freight or handling into inventory value is **not implemented**, and
not because nobody thought of it. No approved capitalisation policy exists: no
allocation basis, no revaluation shape, and no decision about what happens to
stock that has already been issued. An `ACCOUNT` line therefore charges an
expense or asset account directly and changes no moving average.

This is a deferral, not a gap in the code. It needs a policy decision before it
needs an implementation.

---

## شروط الائتمان

**A supplier's credit terms are one integer**, `Supplier.payment_terms_days`, and
every document that cares takes a **snapshot** at creation:
`SupplierInvoice.payment_terms_days` beside its own `due_date`, and the same pair
on `PurchaseOrder`.

### The snapshot is the whole design

A term table with codes and Arabic names would be a nicer master-data screen and
would change nothing about correctness, because correctness here is entirely the
snapshot. Reading terms live from the supplier at display time would silently
restate the due date of every historical invoice the moment somebody
renegotiated — including invoices already posted, already paid, and already
chased.

Proven on the development database, in a rolled-back transaction:

```
1. supplier at 14 days      invoice A snapshot = 14   due = 2026-09-02
2. supplier changed to 30
3. invoice A unchanged      snapshot = 14             due = 2026-09-02
4. invoice B                snapshot = 30             due = 2026-09-18
```

The workspace shows both figures side by side when they differ, because the
whole point is that they are allowed to: a screen showing only one would make a
correct system look broken to somebody who had just renegotiated.

### The Arabic labels

| Days | Label |
|---|---|
| 0 | عند الاستلام |
| 1 | يوم واحد |
| 2–10 | ‹n› أيام |
| 11+ | ‹n› يوم |

This is a function rather than a format string because Arabic pluralises
differently at 1, at 2–10 and at 11+. One format string gets two of the three
wrong, and the result reads as broken to a native speaker even when the number
is right.

### Dates are arguments, not clocks

Both workspaces take `today` as an explicit argument rather than reading the
server clock. Overdue is a claim about a named date; a screen that read the clock
would say something different at 23:59 and 00:01 with nothing having been edited.

---

## What was not created

- no `AdditionalCost`, `AdditionalCostType` or `LandedCost` model
- no `CreditTerm`, `PaymentTerm` or `SupplierCreditTerm` table
- no second supplier payable, journal source, GRNI or PPV calculation
- no migration at all — all three workspaces are reads over existing tables

Tests assert those absences directly
(`apps/procurement/tests/test_financial_workspaces.py`), because "we chose not to
build a second model" is exactly the kind of decision a later contributor
reverses by accident.

## Related

- `docs/tasks/task-2-0-procurement-domain-spec.md`
- `docs/invariants/procurement-invariants.md`
- `docs/decisions/ADR-022-supplier-return-valuation-and-purchase-variance.md`
- `docs/decisions/ADR-023-grni-clearing-and-three-way-matching.md`
